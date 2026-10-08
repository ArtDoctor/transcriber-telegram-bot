import asyncio
import io
import logging
import os
import shutil
import tempfile
import time
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from telegram import Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import main
from main import (
    ALLOWED_DOCUMENT_SUFFIXES,
    AUDIO_CACHE,
    CACHE_DIR,
    call_elevenlabs_stt,
    cleanup_audio_cache,
    collect_words,
    download_youtube_media,
    env_bool,
    env_int,
    error_handler,
    extract_youtube_video_id,
    fallback_text,
    format_speaker_transcript,
    get_media_info,
    get_youtube_cookies_path,
    get_yt_dlp_js_runtimes,
    handle_callback_query,
    handle_media,
    handle_text,
    heartbeat_status,
    output_txt_name,
    process_media,
    process_youtube_download,
    safe_edit,
    sanitize_filename,
    start,
    trim_youtube_url,
)


class TestHelpers:
    def test_env_bool(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "true")
        assert env_bool("TEST_VAR") is True
        monkeypatch.setenv("TEST_VAR", "1")
        assert env_bool("TEST_VAR") is True
        monkeypatch.setenv("TEST_VAR", "yes")
        assert env_bool("TEST_VAR") is True
        monkeypatch.setenv("TEST_VAR", "on")
        assert env_bool("TEST_VAR") is True

        monkeypatch.setenv("TEST_VAR", "false")
        assert env_bool("TEST_VAR") is False
        monkeypatch.setenv("TEST_VAR", "0")
        assert env_bool("TEST_VAR") is False
        monkeypatch.setenv("TEST_VAR", "no")
        assert env_bool("TEST_VAR") is False
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env_bool("TEST_VAR", default=True) is True
        assert env_bool("TEST_VAR", default=False) is False

    def test_env_int(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "42")
        assert env_int("TEST_INT", default=10) == 42
        monkeypatch.setenv("TEST_INT", "   100  ")
        assert env_int("TEST_INT", default=10) == 100
        monkeypatch.setenv("TEST_INT", "")
        assert env_int("TEST_INT", default=10) == 10
        monkeypatch.delenv("TEST_INT", raising=False)
        assert env_int("TEST_INT", default=10) == 10

    def test_sanitize_filename(self):
        assert sanitize_filename("test file (1).mp3") == "test_file_1_.mp3"
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("!@#$%^&*()") == "audio"
        long_name = "a" * 150 + ".wav"
        sanitized = sanitize_filename(long_name)
        assert len(sanitized) <= 90

    def test_output_txt_name(self):
        assert output_txt_name("sample_audio.mp3") == "sample_audio.transcript.txt"
        assert output_txt_name("nested/dir/recording.m4a") == "recording.transcript.txt"


class TestYouTubeUrlTrimming:
    def test_trim_youtube_url_full_with_playlist(self):
        url = "https://www.youtube.com/watch?v=CzGTQseaM38&list=PLH7PIPKvCm38&index=8"
        assert extract_youtube_video_id(url) == "CzGTQseaM38"
        assert trim_youtube_url(url) == "https://www.youtube.com/watch?v=CzGTQseaM38"

    def test_trim_youtube_url_short(self):
        url = "https://youtu.be/CzGTQseaM38?si=abcdef123"
        assert extract_youtube_video_id(url) == "CzGTQseaM38"
        assert trim_youtube_url(url) == "https://www.youtube.com/watch?v=CzGTQseaM38"

    def test_trim_youtube_url_shorts(self):
        url = "https://www.youtube.com/shorts/CzGTQseaM38?feature=share"
        assert extract_youtube_video_id(url) == "CzGTQseaM38"
        assert trim_youtube_url(url) == "https://www.youtube.com/watch?v=CzGTQseaM38"

    def test_trim_youtube_url_embedded_in_text(self):
        text = "Check out this lecture: https://www.youtube.com/watch?v=CzGTQseaM38&t=10s it is great"
        assert extract_youtube_video_id(text) == "CzGTQseaM38"
        assert trim_youtube_url(text) == "https://www.youtube.com/watch?v=CzGTQseaM38"

    def test_trim_youtube_url_non_youtube(self):
        assert extract_youtube_video_id("https://vimeo.com/123456789") is None
        assert trim_youtube_url("https://vimeo.com/123456789") is None
        assert trim_youtube_url("Just plain text message") is None


class TestMediaInfo:
    def test_voice_media(self):
        msg = MagicMock()
        msg.voice.file_id = "v123"
        msg.voice.mime_type = "audio/ogg"
        msg.voice.file_size = 1024
        msg.message_id = 99
        msg.audio = None
        msg.video = None
        msg.document = None

        info = get_media_info(msg)
        assert info == {
            "file_id": "v123",
            "filename": "voice_99.ogg",
            "mime_type": "audio/ogg",
            "file_size": 1024,
        }

    def test_audio_media(self):
        msg = MagicMock()
        msg.voice = None
        msg.audio.file_id = "a123"
        msg.audio.file_name = "song.mp3"
        msg.audio.mime_type = "audio/mpeg"
        msg.audio.file_size = 2048
        msg.video = None
        msg.document = None

        info = get_media_info(msg)
        assert info == {
            "file_id": "a123",
            "filename": "song.mp3",
            "mime_type": "audio/mpeg",
            "file_size": 2048,
        }

    def test_video_media(self):
        msg = MagicMock()
        msg.voice = None
        msg.audio = None
        msg.video.file_id = "vid123"
        msg.video.file_name = "video.mp4"
        msg.video.mime_type = "video/mp4"
        msg.video.file_size = 5000
        msg.document = None

        info = get_media_info(msg)
        assert info == {
            "file_id": "vid123",
            "filename": "video.mp4",
            "mime_type": "video/mp4",
            "file_size": 5000,
        }

    def test_document_allowed_suffix(self):
        msg = MagicMock()
        msg.voice = None
        msg.audio = None
        msg.video = None
        msg.document.file_id = "doc123"
        msg.document.file_name = "recording.wav"
        msg.document.mime_type = "application/octet-stream"
        msg.document.file_size = 4096

        info = get_media_info(msg)
        assert info is not None
        assert info["filename"] == "recording.wav"

    def test_document_disallowed_suffix(self):
        msg = MagicMock()
        msg.voice = None
        msg.audio = None
        msg.video = None
        msg.document.file_id = "doc123"
        msg.document.file_name = "notes.pdf"
        msg.document.mime_type = "application/pdf"
        msg.document.file_size = 4096

        info = get_media_info(msg)
        assert info is None

    def test_no_media(self):
        msg = MagicMock()
        msg.voice = None
        msg.audio = None
        msg.video = None
        msg.document = None

        assert get_media_info(msg) is None


class TestTranscripts:
    def test_collect_words_single_channel(self):
        payload = {
            "words": [
                {"text": "Hello", "start": 0.1, "speaker_id": "speaker_0"},
                {"text": "world", "start": 0.5, "speaker_id": "speaker_0"},
            ]
        }
        words = collect_words(payload)
        assert len(words) == 2
        assert words[0]["text"] == "Hello"

    def test_collect_words_multichannel(self):
        payload = {
            "transcripts": [
                {
                    "channel_index": 0,
                    "words": [{"text": "Hello", "start": 0.5}],
                },
                {
                    "channel_index": 1,
                    "words": [{"text": "Hi", "start": 0.1}],
                },
            ]
        }
        words = collect_words(payload)
        assert len(words) == 2
        assert words[0]["text"] == "Hi"
        assert words[0]["speaker_id"] == "speaker_1"
        assert words[1]["text"] == "Hello"
        assert words[1]["speaker_id"] == "speaker_0"

    def test_fallback_text(self):
        payload_transcripts = {
            "transcripts": [
                {"text": "First line"},
                {"text": "Second line"},
            ]
        }
        assert fallback_text(payload_transcripts) == "[SPEAKER 1] First line\n[SPEAKER 2] Second line"

        payload_plain = {"text": "Simple transcript"}
        assert fallback_text(payload_plain) == "[SPEAKER 1] Simple transcript"

    def test_format_speaker_transcript_formatting(self):
        result = {
            "words": [
                {"text": "Hello", "speaker_id": "spk_a", "type": "word"},
                {"text": " there.", "speaker_id": "spk_a", "type": "word"},
                {"text": "Hi,", "speaker_id": "spk_b", "type": "word"},
                {"text": " how are you?", "speaker_id": "spk_b", "type": "word"},
                {"text": "I am good.", "speaker_id": "spk_a", "type": "word"},
            ]
        }
        transcript = format_speaker_transcript(result)
        expected = (
            "[SPEAKER 1] Hello there.\n"
            "[SPEAKER 2] Hi, how are you?\n"
            "[SPEAKER 1] I am good.\n"
        )
        assert transcript == expected

    def test_format_speaker_transcript_empty(self):
        result = {"words": [], "text": "Just plain text"}
        transcript = format_speaker_transcript(result)
        assert transcript == "[SPEAKER 1] Just plain text\n"


@pytest.mark.asyncio
class TestAsyncHandlers:
    async def test_safe_edit_success(self):
        msg = AsyncMock()
        await safe_edit(msg, "new status")
        msg.edit_text.assert_awaited_once_with("new status")

    async def test_safe_edit_ignores_message_not_modified(self):
        msg = AsyncMock()
        msg.edit_text.side_effect = BadRequest("Message is not modified")
        await safe_edit(msg, "same status")

    async def test_safe_edit_catches_other_telegram_errors(self):
        msg = AsyncMock()
        msg.edit_text.side_effect = TelegramError("Network issue")
        await safe_edit(msg, "status")

    async def test_heartbeat_status(self):
        msg = AsyncMock()
        stop_event = asyncio.Event()
        started_at = asyncio.get_event_loop().time()

        task = asyncio.create_task(heartbeat_status(msg, stop_event, started_at))
        await asyncio.sleep(0.05)
        stop_event.set()
        await task
        assert msg.edit_text.call_count >= 1

    async def test_start_command(self):
        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()

        await start(update, context)
        update.effective_message.reply_text.assert_awaited_once()
        args = update.effective_message.reply_text.call_args[0][0]
        assert "YouTube" in args

    async def test_handle_text_youtube_url(self):
        update = MagicMock()
        msg = AsyncMock()
        update.effective_message = msg
        msg.text = "https://www.youtube.com/watch?v=CzGTQseaM38&list=PLH7PIPKvCm38&index=8"
        context = MagicMock()

        await handle_text(update, context)
        msg.reply_text.assert_awaited_once()
        call_args = msg.reply_text.call_args
        text_sent = call_args[0][0]
        reply_markup = call_args[1]["reply_markup"]

        # Check trimmed URL is sent
        assert "https://www.youtube.com/watch?v=CzGTQseaM38" in text_sent
        assert "list=" not in text_sent
        # Check two buttons are sent
        assert len(reply_markup.inline_keyboard[0]) == 2
        btn_mp4, btn_mp3 = reply_markup.inline_keyboard[0]
        assert "MP4" in btn_mp4.text
        assert btn_mp4.callback_data == "yt:mp4:CzGTQseaM38"
        assert "MP3" in btn_mp3.text
        assert btn_mp3.callback_data == "yt:mp3:CzGTQseaM38"

    async def test_handle_text_regular_message(self):
        update = MagicMock()
        msg = AsyncMock()
        update.effective_message = msg
        msg.text = "Hello bot!"
        context = MagicMock()

        await handle_text(update, context)
        msg.reply_text.assert_awaited_once()
        text_sent = msg.reply_text.call_args[0][0]
        assert "YouTube link" in text_sent

    async def test_handle_callback_query_mp4(self):
        update = MagicMock()
        query = AsyncMock()
        update.callback_query = query
        query.data = "yt:mp4:CzGTQseaM38"
        query.message = AsyncMock()
        query.message.chat_id = 111
        query.message.message_id = 222
        context = MagicMock()

        with patch("main.process_youtube_download", AsyncMock()) as mock_dl:
            await handle_callback_query(update, context)
            query.answer.assert_awaited_once()
            # Wait for background task to run
            await asyncio.sleep(0.05)
            mock_dl.assert_called_once()
            assert mock_dl.call_args[1]["video_id"] == "CzGTQseaM38"
            assert mock_dl.call_args[1]["download_type"] == "mp4"

    async def test_handle_callback_query_mp3(self):
        update = MagicMock()
        query = AsyncMock()
        update.callback_query = query
        query.data = "yt:mp3:CzGTQseaM38"
        query.message = AsyncMock()
        query.message.chat_id = 111
        query.message.message_id = 222
        context = MagicMock()

        with patch("main.process_youtube_download", AsyncMock()) as mock_dl:
            await handle_callback_query(update, context)
            query.answer.assert_awaited_once()
            await asyncio.sleep(0.05)
            mock_dl.assert_called_once()
            assert mock_dl.call_args[1]["video_id"] == "CzGTQseaM38"
            assert mock_dl.call_args[1]["download_type"] == "mp3"

    async def test_handle_callback_query_transcribe_button(self, tmp_path):
        dummy_mp3 = tmp_path / "test.mp3"
        dummy_mp3.write_bytes(b"audio content")
        AUDIO_CACHE["test1234"] = {
            "path": dummy_mp3,
            "filename": "test.mp3",
            "title": "Test Title",
            "created_at": time.time(),
        }

        update = MagicMock()
        query = AsyncMock()
        update.callback_query = query
        query.data = "yt_tx:test1234"
        query.message = AsyncMock()
        query.message.chat_id = 111
        query.message.message_id = 222
        context = MagicMock()

        with patch("main.process_media", AsyncMock()) as mock_pm:
            await handle_callback_query(update, context)
            query.answer.assert_awaited_once_with("Starting transcription…")
            await asyncio.sleep(0.05)
            mock_pm.assert_called_once()
            assert mock_pm.call_args[1]["local_path"] == dummy_mp3

    async def test_process_youtube_download_mp4(self, tmp_path):
        context = MagicMock()
        bot = AsyncMock()
        context.bot = bot
        status_msg = AsyncMock()

        dummy_mp4 = tmp_path / "video.mp4"
        dummy_mp4.write_bytes(b"dummy mp4 video bytes")

        with patch("main.download_youtube_media", return_value=(dummy_mp4, {"title": "Test Video"})):
            await process_youtube_download(
                context=context,
                chat_id=123,
                reply_to_message_id=456,
                status_message=status_msg,
                video_id="CzGTQseaM38",
                download_type="mp4",
            )

        bot.send_video.assert_awaited_once()
        call_kwargs = bot.send_video.call_args[1]
        assert call_kwargs["chat_id"] == 123
        assert "Test Video" in call_kwargs["caption"]

    async def test_process_youtube_download_auto_compress_on_public_limit(self, tmp_path, monkeypatch):
        context = MagicMock()
        bot = AsyncMock()
        context.bot = bot
        status_msg = AsyncMock()

        dummy_large_mp4 = tmp_path / "large_video.mp4"
        dummy_large_mp4.write_bytes(b"x" * (60 * 1024 * 1024))  # 60 MB

        dummy_compressed_mp4 = tmp_path / "compressed.mp4"
        dummy_compressed_mp4.write_bytes(b"x" * (30 * 1024 * 1024))  # 30 MB

        monkeypatch.setattr(main, "TELEGRAM_BOT_API_BASE_URL", "")
        monkeypatch.setattr(main, "MAX_TELEGRAM_UPLOAD_BYTES", 50 * 1024 * 1024)

        def fake_compress(inp, out):
            out.write_bytes(b"x" * (30 * 1024 * 1024))
            return True

        with patch("main.download_youtube_media", return_value=(dummy_large_mp4, {"title": "Large Video"})), \
             patch("main.compress_video_for_telegram", side_effect=fake_compress):
            await process_youtube_download(
                context=context,
                chat_id=123,
                reply_to_message_id=456,
                status_message=status_msg,
                video_id="CzGTQseaM38",
                download_type="mp4",
            )

        bot.send_video.assert_awaited_once()
        call_kwargs = bot.send_video.call_args[1]
        assert "compressed to fit" in call_kwargs["caption"]

    async def test_process_youtube_download_oversized_exceeds_upload_limit(self, tmp_path, monkeypatch):
        context = MagicMock()
        bot = AsyncMock()
        context.bot = bot
        status_msg = AsyncMock()

        dummy_huge_mp4 = tmp_path / "huge.mp4"
        dummy_huge_mp4.write_bytes(b"x" * (120 * 1024 * 1024))  # 120 MB

        monkeypatch.setattr(main, "TELEGRAM_BOT_API_BASE_URL", "")
        monkeypatch.setattr(main, "MAX_TELEGRAM_UPLOAD_BYTES", 50 * 1024 * 1024)

        # Fails compression or still exceeds limit
        with patch("main.download_youtube_media", return_value=(dummy_huge_mp4, {"title": "Huge Video"})), \
             patch("main.compress_video_for_telegram", return_value=False):
            await process_youtube_download(
                context=context,
                chat_id=123,
                reply_to_message_id=456,
                status_message=status_msg,
                video_id="CzGTQseaM38",
                download_type="mp4",
            )

        bot.send_video.assert_not_called()
        status_msg.edit_text.assert_awaited()
        last_edit = status_msg.edit_text.call_args[0][0]
        assert "exceeds Telegram's transfer limit of 50 MB" in last_edit
        assert "docker compose up -d" in last_edit

    async def test_process_youtube_download_mp3_with_transcribe_button(self, tmp_path):
        context = MagicMock()
        bot = AsyncMock()
        context.bot = bot
        status_msg = AsyncMock()

        dummy_mp3 = tmp_path / "audio.mp3"
        dummy_mp3.write_bytes(b"dummy mp3 audio bytes")

        with patch("main.download_youtube_media", return_value=(dummy_mp3, {"title": "Test Audio"})):
            await process_youtube_download(
                context=context,
                chat_id=123,
                reply_to_message_id=456,
                status_message=status_msg,
                video_id="CzGTQseaM38",
                download_type="mp3",
            )

        bot.send_audio.assert_awaited_once()
        call_kwargs = bot.send_audio.call_args[1]
        assert call_kwargs["chat_id"] == 123
        assert "Test Audio" in call_kwargs["caption"]
        reply_markup = call_kwargs["reply_markup"]
        btn = reply_markup.inline_keyboard[0][0]
        assert "Transcribe Audio" in btn.text
        assert btn.callback_data.startswith("yt_tx:")

    async def test_process_media_local_path_success(self, tmp_path, monkeypatch):
        status_msg = AsyncMock()
        context = MagicMock()
        bot = AsyncMock()
        context.bot = bot

        local_audio = tmp_path / "audio.mp3"
        local_audio.write_bytes(b"dummy audio content")

        monkeypatch.setenv("ELEVENLABS_API_KEY", "fake_key")
        mock_stt_result = {
            "words": [
                {"text": "Hello world", "speaker_id": "spk_1", "type": "word"}
            ]
        }

        with patch("main.call_elevenlabs_stt", AsyncMock(return_value=mock_stt_result)):
            await process_media(
                context=context,
                chat_id=123,
                reply_to_message_id=456,
                status_message=status_msg,
                local_path=local_audio,
            )

        # Telegram get_file should NOT have been called since we had local_path
        bot.get_file.assert_not_called()
        bot.send_document.assert_awaited_once()

    async def test_handle_media_oversized(self, monkeypatch):
        update = MagicMock()
        msg = AsyncMock()
        update.effective_message = msg
        msg.voice = None
        msg.video = None
        msg.document = None
        msg.audio.file_id = "huge_audio"
        msg.audio.file_name = "huge.mp3"
        msg.audio.mime_type = "audio/mpeg"
        msg.audio.file_size = 50 * 1024 * 1024
        context = MagicMock()

        monkeypatch.setattr(main, "MAX_TELEGRAM_DOWNLOAD_BYTES", 20 * 1024 * 1024)

        await handle_media(update, context)
        msg.reply_text.assert_awaited_once()
        reply_content = msg.reply_text.call_args[0][0]
        assert "50.0 MB" in reply_content


class TestCacheAndRuntimes:
    def test_get_yt_dlp_js_runtimes(self):
        runtimes = get_yt_dlp_js_runtimes()
        # Should be dict or None, without raising exception
        assert runtimes is None or isinstance(runtimes, dict)

    def test_cleanup_audio_cache(self, tmp_path):
        dummy_old = tmp_path / "old.mp3"
        dummy_old.write_bytes(b"old")
        AUDIO_CACHE["old_tx"] = {
            "path": dummy_old,
            "created_at": time.time() - 4000,
        }

        dummy_new = tmp_path / "new.mp3"
        dummy_new.write_bytes(b"new")
        AUDIO_CACHE["new_tx"] = {
            "path": dummy_new,
            "created_at": time.time(),
        }

        cleanup_audio_cache()
        assert "old_tx" not in AUDIO_CACHE
        assert not dummy_old.exists()
        assert "new_tx" in AUDIO_CACHE
        assert dummy_new.exists()


class TestCookiesAndProxy:
    def test_get_youtube_cookies_path_from_text(self, monkeypatch):
        cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1799999999\tTEST\tVAL123"
        monkeypatch.setenv("YOUTUBE_COOKIES_TEXT", cookie_text)
        path = get_youtube_cookies_path()
        assert path is not None
        assert path.is_file()
        assert "TEST" in path.read_text()

    def test_get_youtube_cookies_path_from_file_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YOUTUBE_COOKIES_TEXT", raising=False)
        cookie_file = tmp_path / "custom_cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1799999999\tKEY\tVAL")
        monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookie_file))
        path = get_youtube_cookies_path()
        assert path == cookie_file.resolve()

    def test_get_youtube_cookies_path_none_when_empty(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_COOKIES_TEXT", raising=False)
        monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
        # Assuming no valid cookies.txt in current directory
        # Let's verify it doesn't crash
        path = get_youtube_cookies_path()
        assert path is None or path.is_file()

    def test_download_youtube_media_options_with_cookies(self, tmp_path):
        dummy_file = tmp_path / "song.mp3"
        dummy_file.write_bytes(b"data")
        fake_cookie = tmp_path / "cookies.txt"
        fake_cookie.write_text("fake cookies")

        with patch("main.get_youtube_cookies_path", return_value=fake_cookie), \
             patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_inst = MagicMock()
            mock_ydl.return_value.__enter__.return_value = mock_inst
            mock_inst.extract_info.return_value = {"title": "Song"}

            res_path, info = download_youtube_media("https://youtube.com/watch?v=123", "mp3", tmp_path)
            opts = mock_ydl.call_args[0][0]
            assert opts["cookiefile"] == str(fake_cookie)
            assert "extractor_args" not in opts
            assert opts["remote_components"] == ["ejs:github"]

    def test_download_youtube_media_options_without_cookies(self, tmp_path):
        dummy_file = tmp_path / "video.mp4"
        dummy_file.write_bytes(b"data")

        with patch("main.get_youtube_cookies_path", return_value=None), \
             patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_inst = MagicMock()
            mock_ydl.return_value.__enter__.return_value = mock_inst
            mock_inst.extract_info.return_value = {"title": "Video"}

            res_path, info = download_youtube_media("https://youtube.com/watch?v=123", "mp4", tmp_path)
            opts = mock_ydl.call_args[0][0]
            assert "cookiefile" not in opts
            assert opts["extractor_args"]["youtube"]["player_client"] == ["android", "visionos"]
            assert opts["remote_components"] == ["ejs:github"]

    def test_download_youtube_media_player_client_env(self, tmp_path, monkeypatch):
        dummy_file = tmp_path / "song.mp3"
        dummy_file.write_bytes(b"data")
        monkeypatch.setenv("YOUTUBE_PLAYER_CLIENT", "mweb,web")

        with patch("main.get_youtube_cookies_path", return_value=None), \
             patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_inst = MagicMock()
            mock_ydl.return_value.__enter__.return_value = mock_inst
            mock_inst.extract_info.return_value = {"title": "Song"}

            res_path, info = download_youtube_media("https://youtube.com/watch?v=123", "mp3", tmp_path)
            opts = mock_ydl.call_args[0][0]
            assert opts["extractor_args"]["youtube"]["player_client"] == ["mweb", "web"]


class TestMainApp:
    def test_main_missing_tokens(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="Set TELEGRAM_BOT_TOKEN"):
            main.main()

    def test_main_runs_polling(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "fake_key")

        mock_app = MagicMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with patch("main.ApplicationBuilder", return_value=mock_builder):
            main.main()

        assert mock_app.add_handler.call_count >= 3
        mock_app.add_error_handler.assert_called_with(error_handler)
        mock_app.run_polling.assert_called_once()


@pytest.mark.asyncio
class TestLiveApi:
    """Tests interacting with real services using the configured credentials in .env."""

    async def test_telegram_get_me(self):
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            pytest.skip("TELEGRAM_BOT_TOKEN not configured")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("ok") is True
            assert data.get("result", {}).get("is_bot") is True

    async def test_elevenlabs_stt_live(self):
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            pytest.skip("ELEVENLABS_API_KEY not configured")

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "silence.wav"
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00" * 32000)

            res = await call_elevenlabs_stt(
                path=wav_path,
                filename="silence.wav",
                mime_type="audio/wav",
                api_key=api_key,
            )
            assert isinstance(res, dict)
            assert "transcription_id" in res
