import asyncio
import contextlib
import io
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import InputFile, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
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

# Public Telegram Bot API only supports bot downloads up to 20 MB. A self-hosted
# telegram-bot-api server raises that limit dramatically; keep an explicit env
# override for deployments with stricter disk/network limits.
DEFAULT_DOWNLOAD_LIMIT = 2 * 1024 * 1024 * 1024 if TELEGRAM_BOT_API_BASE_URL else 20 * 1024 * 1024
MAX_TELEGRAM_DOWNLOAD_BYTES = env_int("MAX_TELEGRAM_DOWNLOAD_BYTES", DEFAULT_DOWNLOAD_LIMIT)

ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "scribe_v2")
ELEVENLABS_LANGUAGE_CODE = os.getenv("ELEVENLABS_LANGUAGE_CODE", "").strip() or None
ELEVENLABS_NUM_SPEAKERS = os.getenv("ELEVENLABS_NUM_SPEAKERS", "").strip() or None

MAX_CONCURRENT_TRANSCRIPTIONS = int(os.getenv("MAX_CONCURRENT_TRANSCRIPTIONS", "2"))
TRANSCRIPTION_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TRANSCRIPTIONS)

# Large Telegram files can make the local Bot API server spend several minutes
# fetching/caching the file before getFile returns.
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

# Use this only for stereo/multichannel recordings where each channel is one speaker.
# For normal mono recordings with multiple speakers, leave it false and use diarization.
ELEVENLABS_USE_MULTI_CHANNEL = env_bool("ELEVENLABS_USE_MULTI_CHANNEL", False)


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
        "Send me a voice note, audio file, or audio/video document. "
        "I’ll transcribe it with ElevenLabs Scribe v2 and send back a .txt file with speaker labels."
    )


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
    file_id: str,
    filename: str,
    mime_type: str | None,
) -> None:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        await safe_edit(status_message, "Missing ELEVENLABS_API_KEY environment variable.")
        return

    try:
        async with TRANSCRIPTION_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="tg_scribe_") as tmpdir:
                download_path = Path(tmpdir) / filename

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
                        filename=filename,
                        mime_type=mime_type,
                        api_key=api_key,
                    )
                finally:
                    stop_event.set()
                    with contextlib.suppress(Exception):
                        await pulse

                await safe_edit(status_message, "Formatting transcript…")

                transcript = format_speaker_transcript(result)
                output_name = output_txt_name(filename)

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
            "Telegram download limit set to %.0f MB",
            MAX_TELEGRAM_DOWNLOAD_BYTES / (1024 * 1024),
        )

    app: Application = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(media_filter, handle_media))
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()