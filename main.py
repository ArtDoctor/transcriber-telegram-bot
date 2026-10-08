import asyncio
import contextlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yt_dlp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


TELEGRAM_BOT_API_BASE_URL = os.getenv("TELEGRAM_BOT_API_BASE_URL", "").strip().rstrip("/")
TELEGRAM_BOT_API_BASE_FILE_URL = os.getenv("TELEGRAM_BOT_API_BASE_FILE_URL", "").strip().rstrip("/")
if TELEGRAM_BOT_API_BASE_URL and not TELEGRAM_BOT_API_BASE_FILE_URL:
    TELEGRAM_BOT_API_BASE_FILE_URL = TELEGRAM_BOT_API_BASE_URL.removesuffix("/bot") + "/file/bot"
TELEGRAM_LOCAL_MODE = env_bool("TELEGRAM_LOCAL_MODE", False)

# Public Telegram Bot API supports downloads up to 20 MB and uploads up to 50 MB.
# A self-hosted telegram-bot-api server raises both limits to 2 GB (2048 MB).
DEFAULT_DOWNLOAD_LIMIT = 2 * 1024 * 1024 * 1024 if TELEGRAM_BOT_API_BASE_URL else 20 * 1024 * 1024
MAX_TELEGRAM_DOWNLOAD_BYTES = env_int("MAX_TELEGRAM_DOWNLOAD_BYTES", DEFAULT_DOWNLOAD_LIMIT)

DEFAULT_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024 if TELEGRAM_BOT_API_BASE_URL else 50 * 1024 * 1024
MAX_TELEGRAM_UPLOAD_BYTES = env_int("MAX_TELEGRAM_UPLOAD_BYTES", DEFAULT_UPLOAD_LIMIT)

ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "scribe_v2")
ELEVENLABS_LANGUAGE_CODE = os.getenv("ELEVENLABS_LANGUAGE_CODE", "").strip() or None
ELEVENLABS_NUM_SPEAKERS = os.getenv("ELEVENLABS_NUM_SPEAKERS", "").strip() or None

MAX_CONCURRENT_TRANSCRIPTIONS = int(os.getenv("MAX_CONCURRENT_TRANSCRIPTIONS", "2"))
TRANSCRIPTION_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TRANSCRIPTIONS)

MAX_CONCURRENT_YT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_YT_DOWNLOADS", "2"))
YT_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_YT_DOWNLOADS)

TELEGRAM_CONNECT_TIMEOUT = env_int("TELEGRAM_CONNECT_TIMEOUT", 30)
TELEGRAM_READ_TIMEOUT = env_int("TELEGRAM_READ_TIMEOUT", 900)
TELEGRAM_WRITE_TIMEOUT = env_int("TELEGRAM_WRITE_TIMEOUT", 900)
TELEGRAM_POOL_TIMEOUT = env_int("TELEGRAM_POOL_TIMEOUT", 30)

ALLOWED_DOCUMENT_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac", ".amr",
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".mpeg", ".mpg", ".3gp",
}

ELEVENLABS_TAG_AUDIO_EVENTS = env_bool("ELEVENLABS_TAG_AUDIO_EVENTS", False)
ELEVENLABS_NO_VERBATIM = env_bool("ELEVENLABS_NO_VERBATIM", False)
ELEVENLABS_USE_MULTI_CHANNEL = env_bool("ELEVENLABS_USE_MULTI_CHANNEL", False)

CACHE_DIR = Path(tempfile.gettempdir()) / "tg_scribe_cache"
CACHE_TTL_SECONDS = 3600
AUDIO_CACHE: dict[str, dict[str, Any]] = {}

YOUTUBE_URL_PATTERN = re.compile(
    r"(?:https?:\/\/)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com\/(?:watch\?(?:[^\s&#]*&)*v=|shorts\/|live\/|embed\/)|youtu\.be\/)"
    r"([A-Za-z0-9_-]{11})"
)


def extract_youtube_video_id(text: str) -> str | None:
    match = YOUTUBE_URL_PATTERN.search(text)
    return match.group(1) if match else None


def trim_youtube_url(text: str) -> str | None:
    vid = extract_youtube_video_id(text)
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def get_yt_dlp_js_runtimes() -> dict[str, Any] | None:
    """
    Locates an available JavaScript runtime (Node.js or Deno) for yt-dlp.
    """
    node_path = shutil.which("node") or shutil.which("nodejs") or shutil.which("deno")
    if not node_path:
        home_nvm = os.path.expanduser("~/.nvm/versions/node")
        if os.path.isdir(home_nvm):
            for version in sorted(os.listdir(home_nvm), reverse=True):
                candidate = os.path.join(home_nvm, version, "bin", "node")
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    node_path = candidate
                    break
    if node_path:
        runtime = "deno" if "deno" in Path(node_path).name.lower() else "node"
        return {runtime: {"path": node_path}}
    return None


def cleanup_audio_cache() -> None:
    """
    Remove cached audio files older than CACHE_TTL_SECONDS to avoid VPS disk leaks.
    """
    now = time.time()
    expired_ids = [
        tx_id for tx_id, data in AUDIO_CACHE.items()
        if now - data.get("created_at", 0) > CACHE_TTL_SECONDS
    ]
    for tx_id in expired_ids:
        data = AUDIO_CACHE.pop(tx_id, None)
        if data and "path" in data:
            with contextlib.suppress(Exception):
                Path(data["path"]).unlink(missing_ok=True)


def compress_video_for_telegram(input_path: Path, output_path: Path) -> bool:
    """
    Compresses video with ffmpeg to reduce file size to fit Telegram's public 50 MB limit.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return False
    cmd = [
        ffmpeg_path,
        "-y",
        "-i", str(input_path),
        "-vf", "scale='min(1280,iw)':-2",
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_path),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        return res.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0
    except Exception as exc:
        logging.warning("Video compression failed: %s", exc)
        return False


def get_youtube_cookies_path() -> Path | None:
    # 1. Directly from YOUTUBE_COOKIES_TEXT in .env
    cookies_text = os.getenv("YOUTUBE_COOKIES_TEXT", "").strip()
    if cookies_text and len(cookies_text) > 50:
        target = Path(tempfile.gettempdir()) / "yt_cookies.txt"
        target.write_text(cookies_text, encoding="utf-8")
        return target

    # 2. From YOUTUBE_COOKIES_FILE in .env
    env_file = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if env_file:
        p = Path(env_file)
        if p.is_file() and p.stat().st_size > 50:
            return p.resolve()

    # 3. From local cookies.txt file in workspace or subfolder
    for candidate in [Path("cookies.txt"), Path("cookies/cookies.txt")]:
        if candidate.is_file() and candidate.stat().st_size > 50:
            return candidate.resolve()

    return None


def download_youtube_media(url: str, download_type: str, out_dir: Path) -> tuple[Path, dict[str, Any]]:
    """
    Synchronously downloads media from YouTube using yt-dlp.
    download_type: 'mp4' (1080p/maxres) or 'mp3'
    """
    outtmpl = str(out_dir / "%(title).80s [%(id)s].%(ext)s")
    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                # Android and visionOS clients avoid triggering the web client datacenter IP bot challenge
                "player_client": ["android", "visionos"],
            }
        },
    }

    cookies_path = get_youtube_cookies_path()
    if cookies_path:
        ydl_opts["cookiefile"] = str(cookies_path)
        logging.info("Using YouTube cookies from %s", cookies_path)

    proxy = os.getenv("YOUTUBE_PROXY", "").strip()
    if proxy:
        ydl_opts["proxy"] = proxy

    js_runtimes = get_yt_dlp_js_runtimes()
    if js_runtimes:
        ydl_opts["js_runtimes"] = js_runtimes

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path

    if download_type == "mp4":
        ydl_opts["format"] = (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080]/"
            "best"
        )
        ydl_opts["merge_output_format"] = "mp4"
    elif download_type == "mp3":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        raise ValueError(f"Unsupported download_type: {download_type}")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    expected_ext = ".mp4" if download_type == "mp4" else ".mp3"
    candidates = [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() == expected_ext]
    if not candidates:
        candidates = [f for f in out_dir.iterdir() if f.is_file()]
    if not candidates:
        raise RuntimeError(f"No downloaded file found in {out_dir}")

    return candidates[0], info


def sanitize_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name[:90] or "audio"


def output_txt_name(input_filename: str) -> str:
    stem = sanitize_filename(Path(input_filename).stem or "transcript")
    return f"{stem}.transcript.txt"


def get_media_info(message: Any) -> dict[str, Any] | None:
    """
    Return file metadata from a Telegram message, or None if unsupported.
    """
    if message.voice:
        return {
            "file_id": message.voice.file_id,
            "filename": f"voice_{message.message_id}.ogg",
            "mime_type": message.voice.mime_type or "audio/ogg",
            "file_size": message.voice.file_size,
        }

    if message.audio:
        return {
            "file_id": message.audio.file_id,
            "filename": message.audio.file_name or f"audio_{message.message_id}.mp3",
            "mime_type": message.audio.mime_type or "audio/mpeg",
            "file_size": message.audio.file_size,
        }

    if message.video:
        return {
            "file_id": message.video.file_id,
            "filename": message.video.file_name or f"video_{message.message_id}.mp4",
            "mime_type": message.video.mime_type or "video/mp4",
            "file_size": message.video.file_size,
        }

    if message.document:
        filename = message.document.file_name or f"document_{message.message_id}"
        mime_type = message.document.mime_type or "application/octet-stream"
        suffix = Path(filename).suffix.lower()

        is_audio_or_video = mime_type.startswith("audio/") or mime_type.startswith("video/")
        is_allowed_suffix = suffix in ALLOWED_DOCUMENT_SUFFIXES

        if not (is_audio_or_video or is_allowed_suffix):
            return None

        return {
            "file_id": message.document.file_id,
            "filename": filename,
            "mime_type": mime_type,
            "file_size": message.document.file_size,
        }

    return None


async def safe_edit(message: Any, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logging.warning("Could not edit status message: %s", exc)
    except TelegramError as exc:
        logging.warning("Could not edit status message: %s", exc)


async def heartbeat_status(status_message: Any, stop_event: asyncio.Event, started_at: float) -> None:
    dots = ["", ".", "..", "..."]
    i = 0

    while not stop_event.is_set():
        elapsed = int(time.monotonic() - started_at)
        await safe_edit(
            status_message,
            f"Transcribing with ElevenLabs {ELEVENLABS_MODEL}{dots[i % len(dots)]}\n"
            f"Elapsed: {elapsed}s",
        )
        i += 1

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=8)


async def call_elevenlabs_stt(
    path: Path,
    filename: str,
    mime_type: str | None,
    api_key: str,
) -> dict[str, Any]:
    data: dict[str, str] = {
        "model_id": ELEVENLABS_MODEL,
        "timestamps_granularity": "word",
        "tag_audio_events": str(ELEVENLABS_TAG_AUDIO_EVENTS).lower(),
        "no_verbatim": str(ELEVENLABS_NO_VERBATIM).lower(),
        "file_format": "other",
    }

    if ELEVENLABS_LANGUAGE_CODE:
        data["language_code"] = ELEVENLABS_LANGUAGE_CODE

    if ELEVENLABS_USE_MULTI_CHANNEL:
        data["use_multi_channel"] = "true"
        data["diarize"] = "false"
    else:
        data["diarize"] = "true"
        if ELEVENLABS_NUM_SPEAKERS:
            data["num_speakers"] = ELEVENLABS_NUM_SPEAKERS

    headers = {"xi-api-key": api_key}

    timeout = httpx.Timeout(connect=30.0, read=1800.0, write=300.0, pool=30.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        with path.open("rb") as file_handle:
            response = await client.post(
                ELEVENLABS_STT_URL,
                headers=headers,
                data=data,
                files={
                    "file": (
                        filename,
                        file_handle,
                        mime_type or "application/octet-stream",
                    )
                },
            )

    if response.status_code >= 400:
        body = response.text[:1200]
        raise RuntimeError(f"ElevenLabs API error {response.status_code}: {body}")

    return response.json()


def collect_words(result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Handles both normal and multichannel ElevenLabs responses.
    """
    transcripts = result.get("transcripts")

    if isinstance(transcripts, list):
        words: list[dict[str, Any]] = []

        for transcript in transcripts:
            channel_index = transcript.get("channel_index")
            default_speaker = (
                f"speaker_{channel_index}" if channel_index is not None else "speaker_0"
            )

            for word in transcript.get("words") or []:
                item = dict(word)
                item.setdefault("speaker_id", default_speaker)
                words.append(item)

        return sorted(words, key=lambda w: float(w.get("start") or 0.0))

    return list(result.get("words") or [])


def fallback_text(result: dict[str, Any]) -> str:
    transcripts = result.get("transcripts")

    if isinstance(transcripts, list):
        chunks = []
        for i, transcript in enumerate(transcripts, start=1):
            text = (transcript.get("text") or "").strip()
            if text:
                chunks.append(f"[SPEAKER {i}] {text}")
        return "\n".join(chunks).strip()

    text = (result.get("text") or "").strip()
    return f"[SPEAKER 1] {text}" if text else ""


def format_speaker_transcript(result: dict[str, Any]) -> str:
    """
    Produces:
    [SPEAKER 1] text
    [SPEAKER 2] text
    ...
    """
    words = collect_words(result)

    if not words:
        return fallback_text(result) + "\n"

    speaker_map: dict[str, str] = {}
    lines: list[str] = []

    current_speaker: str | None = None
    buffer: list[str] = []

    def speaker_label(raw: str) -> str:
        if raw not in speaker_map:
            speaker_map[raw] = f"SPEAKER {len(speaker_map) + 1}"
        return speaker_map[raw]

    def flush() -> None:
        nonlocal buffer, current_speaker

        if current_speaker is None:
            buffer = []
            return

        text = "".join(buffer)
        text = re.sub(r"\s+", " ", text).strip()

        if text:
            lines.append(f"[{speaker_label(current_speaker)}] {text}")

        buffer = []

    for word in words:
        piece = str(word.get("text") or "")
        if not piece:
            continue

        word_type = word.get("type") or "word"
        if word_type not in {"word", "spacing", "audio_event"}:
            continue

        raw_speaker = word.get("speaker_id")
        if raw_speaker is None and "channel_index" in word:
            raw_speaker = f"speaker_{word.get('channel_index')}"
        raw_speaker = str(raw_speaker or current_speaker or "speaker_0")

        # Skip leading whitespace before the first real token in a segment.
        if current_speaker is None:
            if not piece.strip():
                continue
            current_speaker = raw_speaker

        if raw_speaker != current_speaker:
            # Ignore whitespace-only boundary tokens.
            if not piece.strip():
                continue

            flush()
            current_speaker = raw_speaker

        buffer.append(piece)

    flush()

    if not lines:
        return fallback_text(result) + "\n"

    return "\n".join(lines).strip() + "\n"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "👋 Welcome to the Transcriber & YouTube Downloader Bot!\n\n"
        "Here is what you can do:\n"
        "• Send a YouTube link: I’ll trim extra parameters (like playlists) and offer you options to download as MP4 (1080p/Max) or MP3.\n"
        "• Audio from YouTube includes a button to immediately transcribe it.\n"
        "• Send any voice note, audio file, or video: I’ll transcribe it with ElevenLabs Scribe and send back speaker labels."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    video_id = extract_youtube_video_id(text)
    if not video_id:
        await message.reply_text(
            "Send me a YouTube link to download, or an audio/video file or voice note to transcribe."
        )
        return

    trimmed_url = f"https://www.youtube.com/watch?v={video_id}"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video (MP4 1080p/Max)", callback_data=f"yt:mp4:{video_id}"),
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"yt:mp3:{video_id}"),
        ]
    ])

    await message.reply_text(
        f"🔗 Trimmed link:\n{trimmed_url}\n\n"
        "Choose how you want to download:",
        reply_markup=keyboard,
        disable_web_page_preview=False,
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data

    if data.startswith("yt:mp4:") or data.startswith("yt:mp3:"):
        await query.answer()
        download_type = "mp4" if data.startswith("yt:mp4:") else "mp3"
        video_id = data.split(":", 2)[2]

        label = "MP4 (1080p/Max)" if download_type == "mp4" else "MP3"
        status = await query.message.reply_text(f"Queuing YouTube {label} download…")

        asyncio.create_task(
            process_youtube_download(
                context=context,
                chat_id=query.message.chat_id,
                reply_to_message_id=query.message.message_id,
                status_message=status,
                video_id=video_id,
                download_type=download_type,
            )
        )
        return

    if data.startswith("yt_tx:"):
        tx_id = data.removeprefix("yt_tx:")
        await query.answer("Starting transcription…")
        status = await query.message.reply_text("Received. Queuing transcription…")

        cached = AUDIO_CACHE.get(tx_id)
        local_path: Path | None = None
        file_id: str | None = None
        filename: str | None = None

        if cached and Path(cached["path"]).is_file():
            local_path = Path(cached["path"])
            filename = cached.get("filename")
        else:
            audio = query.message.audio or query.message.document
            if not audio:
                await safe_edit(status, "Could not find audio on this message.")
                return
            file_id = audio.file_id
            filename = getattr(audio, "file_name", None) or f"audio_{query.message.message_id}.mp3"

        asyncio.create_task(
            process_media(
                context=context,
                chat_id=query.message.chat_id,
                reply_to_message_id=query.message.message_id,
                status_message=status,
                file_id=file_id,
                filename=filename or "audio.mp3",
                mime_type="audio/mpeg",
                local_path=local_path,
            )
        )
        return


async def process_youtube_download(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to_message_id: int,
    status_message: Any,
    video_id: str,
    download_type: str,
) -> None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    label = "MP4 (1080p/Max)" if download_type == "mp4" else "MP3"

    try:
        async with YT_DOWNLOAD_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="tg_yt_") as tmpdir:
                tmp_path = Path(tmpdir)
                await safe_edit(status_message, f"Downloading {label} from YouTube…")

                downloaded_file, info = await asyncio.to_thread(
                    download_youtube_media,
                    url,
                    download_type,
                    tmp_path,
                )

                file_size = downloaded_file.stat().st_size
                compressed_note = ""

                # If file exceeds Telegram limit on public API, attempt video compression with ffmpeg
                if file_size > MAX_TELEGRAM_UPLOAD_BYTES:
                    if download_type == "mp4" and not TELEGRAM_BOT_API_BASE_URL:
                        mb = file_size / (1024 * 1024)
                        await safe_edit(
                            status_message,
                            f"File size is {mb:.1f} MB (exceeds Telegram's 50 MB public limit). "
                            "Compressing video with ffmpeg to fit…",
                        )
                        compressed_file = tmp_path / f"compressed_{downloaded_file.name}"
                        success = await asyncio.to_thread(
                            compress_video_for_telegram, downloaded_file, compressed_file
                        )
                        if success and compressed_file.stat().st_size <= MAX_TELEGRAM_UPLOAD_BYTES:
                            downloaded_file = compressed_file
                            file_size = downloaded_file.stat().st_size
                            compressed_note = " (compressed to fit Telegram 50 MB limit)"

                if file_size > MAX_TELEGRAM_UPLOAD_BYTES:
                    mb = file_size / (1024 * 1024)
                    limit_mb = MAX_TELEGRAM_UPLOAD_BYTES / (1024 * 1024)
                    msg_text = (
                        f"⚠️ The downloaded file is about {mb:.1f} MB, which exceeds "
                        f"Telegram's transfer limit of {limit_mb:.0f} MB.\n\n"
                    )
                    if not TELEGRAM_BOT_API_BASE_URL:
                        msg_text += (
                            "💡 Solutions for large files:\n"
                            "1. Run with Docker Compose to enable up to 2 GB transfers:\n"
                            "   `docker compose up -d --build`\n"
                            "2. Or choose 🎵 Audio (MP3), which is much smaller."
                        )
                    else:
                        msg_text += "Try a shorter video or increase MAX_TELEGRAM_UPLOAD_BYTES."
                    await safe_edit(status_message, msg_text)
                    return

                title = str(info.get("title") or downloaded_file.stem)

                if download_type == "mp4":
                    await safe_edit(status_message, "Uploading video to Telegram…")
                    caption_text = f"🎬 {title[:180]}{compressed_note}"
                    try:
                        with downloaded_file.open("rb") as f:
                            await context.bot.send_video(
                                chat_id=chat_id,
                                video=InputFile(f, filename=downloaded_file.name),
                                caption=caption_text,
                                reply_to_message_id=reply_to_message_id,
                                read_timeout=TELEGRAM_READ_TIMEOUT,
                                write_timeout=TELEGRAM_WRITE_TIMEOUT,
                                connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                                pool_timeout=TELEGRAM_POOL_TIMEOUT,
                            )
                    except TelegramError as exc:
                        logging.warning("send_video failed, falling back to send_document: %s", exc)
                        with downloaded_file.open("rb") as f:
                            await context.bot.send_document(
                                chat_id=chat_id,
                                document=InputFile(f, filename=downloaded_file.name),
                                caption=caption_text,
                                reply_to_message_id=reply_to_message_id,
                                read_timeout=TELEGRAM_READ_TIMEOUT,
                                write_timeout=TELEGRAM_WRITE_TIMEOUT,
                                connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                                pool_timeout=TELEGRAM_POOL_TIMEOUT,
                            )
                    await safe_edit(status_message, "Done — video sent!")
                    return

                # MP3 download: save to local cache for fast transcription button
                await safe_edit(status_message, "Uploading audio to Telegram…")
                cleanup_audio_cache()
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tx_id = uuid.uuid4().hex[:12]
                cached_file = CACHE_DIR / f"{tx_id}_{downloaded_file.name}"
                shutil.copy2(downloaded_file, cached_file)

                AUDIO_CACHE[tx_id] = {
                    "path": cached_file,
                    "filename": downloaded_file.name,
                    "title": title,
                    "created_at": time.time(),
                }

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Transcribe Audio", callback_data=f"yt_tx:{tx_id}")]
                ])

                try:
                    with downloaded_file.open("rb") as f:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=InputFile(f, filename=downloaded_file.name),
                            title=title[:100],
                            caption=f"🎵 {title[:200]}",
                            reply_markup=keyboard,
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=TELEGRAM_READ_TIMEOUT,
                            write_timeout=TELEGRAM_WRITE_TIMEOUT,
                            connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                            pool_timeout=TELEGRAM_POOL_TIMEOUT,
                        )
                except TelegramError as exc:
                    logging.warning("send_audio failed, falling back to send_document: %s", exc)
                    with downloaded_file.open("rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=InputFile(f, filename=downloaded_file.name),
                            caption=f"🎵 {title[:200]}",
                            reply_markup=keyboard,
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=TELEGRAM_READ_TIMEOUT,
                            write_timeout=TELEGRAM_WRITE_TIMEOUT,
                            connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                            pool_timeout=TELEGRAM_POOL_TIMEOUT,
                        )
                await safe_edit(status_message, "Done — audio sent!")

    except Exception as exc:
        logging.exception("YouTube download failed")
        error_text = str(exc)
        if "Sign in to confirm you’re not a bot" in error_text or "Use --cookies" in error_text:
            error_text = (
                "⚠️ YouTube is requiring bot verification on your VPS IP address.\n\n"
                "💡 Quick fix:\n"
                "1. Export your YouTube cookies using a browser extension (such as 'Get cookies.txt LOCALLY').\n"
                "2. Save the file as 'cookies.txt' in the bot directory, or paste its text in .env as YOUTUBE_COOKIES_TEXT."
            )
        elif len(error_text) > 3500:
            error_text = error_text[:3500] + "…"
        await safe_edit(status_message, f"YouTube download failed:\n{error_text}")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    media = get_media_info(message)
    if not media:
        return

    file_size = media.get("file_size")
    if file_size and file_size > MAX_TELEGRAM_DOWNLOAD_BYTES:
        mb = file_size / (1024 * 1024)
        limit_mb = MAX_TELEGRAM_DOWNLOAD_BYTES / (1024 * 1024)
        await message.reply_text(
            f"This file is about {mb:.1f} MB, but this bot is configured for Telegram downloads "
            f"up to {limit_mb:.0f} MB. Send a smaller file or deploy with a local Bot API server."
        )
        return

    status = await message.reply_text("Received. Queuing transcription…")

    asyncio.create_task(
        process_media(
            context=context,
            chat_id=message.chat_id,
            reply_to_message_id=message.message_id,
            status_message=status,
            file_id=media["file_id"],
            filename=sanitize_filename(media["filename"]),
            mime_type=media.get("mime_type"),
        )
    )


async def process_media(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to_message_id: int,
    status_message: Any,
    file_id: str | None = None,
    filename: str | None = None,
    mime_type: str | None = None,
    local_path: Path | None = None,
) -> None:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        await safe_edit(status_message, "Missing ELEVENLABS_API_KEY environment variable.")
        return

    resolved_filename = filename or (local_path.name if local_path else "audio.mp3")

    try:
        async with TRANSCRIPTION_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="tg_scribe_") as tmpdir:
                if local_path and Path(local_path).is_file():
                    download_path = Path(local_path)
                    await safe_edit(status_message, "Audio ready. Sending to ElevenLabs…")
                else:
                    if not file_id:
                        await safe_edit(status_message, "No file available to transcribe.")
                        return

                    download_path = Path(tmpdir) / resolved_filename

                    await safe_edit(
                        status_message,
                        "Received. Asking the local Telegram Bot API server to fetch the file…\n"
                        "Large files may take a few minutes before download starts.",
                    )
                    tg_file = await context.bot.get_file(
                        file_id,
                        read_timeout=TELEGRAM_READ_TIMEOUT,
                        write_timeout=TELEGRAM_WRITE_TIMEOUT,
                        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                        pool_timeout=TELEGRAM_POOL_TIMEOUT,
                    )

                    await safe_edit(status_message, "Telegram file is ready. Downloading locally…")
                    await tg_file.download_to_drive(
                        custom_path=download_path,
                        read_timeout=TELEGRAM_READ_TIMEOUT,
                        write_timeout=TELEGRAM_WRITE_TIMEOUT,
                        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                        pool_timeout=TELEGRAM_POOL_TIMEOUT,
                    )
                    await safe_edit(status_message, "Downloaded. Sending to ElevenLabs…")

                stop_event = asyncio.Event()
                started_at = time.monotonic()
                pulse = asyncio.create_task(heartbeat_status(status_message, stop_event, started_at))

                try:
                    result = await call_elevenlabs_stt(
                        path=download_path,
                        filename=resolved_filename,
                        mime_type=mime_type,
                        api_key=api_key,
                    )
                finally:
                    stop_event.set()
                    with contextlib.suppress(Exception):
                        await pulse

                await safe_edit(status_message, "Formatting transcript…")

                transcript = format_speaker_transcript(result)
                output_name = output_txt_name(resolved_filename)

                bio = io.BytesIO(transcript.encode("utf-8"))
                bio.seek(0)

                await context.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(bio, filename=output_name),
                    filename=output_name,
                    caption=f"Done: {output_name}",
                    reply_to_message_id=reply_to_message_id,
                    read_timeout=TELEGRAM_READ_TIMEOUT,
                    write_timeout=TELEGRAM_WRITE_TIMEOUT,
                    connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=TELEGRAM_POOL_TIMEOUT,
                )

                # Clean up cached file if it was served from local cache
                if local_path and Path(local_path).is_file():
                    with contextlib.suppress(Exception):
                        Path(local_path).unlink(missing_ok=True)

                await safe_edit(status_message, "Done — transcript sent as a .txt file.")

    except Exception as exc:
        logging.exception("Transcription failed")
        error_text = str(exc)
        if len(error_text) > 3500:
            error_text = error_text[:3500] + "…"
        await safe_edit(status_message, f"Transcription failed:\n{error_text}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled bot error", exc_info=context.error)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Avoid logging Telegram URLs that contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your environment.")
    if not os.getenv("ELEVENLABS_API_KEY"):
        raise SystemExit("Set ELEVENLABS_API_KEY in your environment.")

    media_filter = filters.VOICE | filters.AUDIO | filters.VIDEO | filters.Document.ALL

    builder = ApplicationBuilder().token(token).concurrent_updates(4)

    if TELEGRAM_BOT_API_BASE_URL:
        logging.info("Using Telegram Bot API server: %s", TELEGRAM_BOT_API_BASE_URL)
        builder.base_url(TELEGRAM_BOT_API_BASE_URL)
        builder.base_file_url(TELEGRAM_BOT_API_BASE_FILE_URL)
        builder.local_mode(TELEGRAM_LOCAL_MODE)
        logging.info(
            "Telegram transfer limits: download <= %.0f MB, upload <= %.0f MB",
            MAX_TELEGRAM_DOWNLOAD_BYTES / (1024 * 1024),
            MAX_TELEGRAM_UPLOAD_BYTES / (1024 * 1024),
        )

    app: Application = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(media_filter, handle_media))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()