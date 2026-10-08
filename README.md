# Telegram ElevenLabs Transcriber & YouTube Downloader Bot

Telegram bot that accepts:
1. **Direct Media**: Voice notes, audio files, or video files, downloads them, transcribes them with ElevenLabs Scribe, and returns a speaker-labelled `.txt` transcript.
2. **YouTube Links**: Automatically trims extra tracking/playlist parameters, offers format selection (**MP4 1080p/Max** or **MP3 Audio**), and provides a one-click button on downloaded audio to run transcription.

---

## Features

- **Link Trimming**: Cleans YouTube links (e.g., `https://www.youtube.com/watch?v=CzGTQseaM38&list=PLH7PIPKvCm38&index=8` ➔ `https://www.youtube.com/watch?v=CzGTQseaM38`).
- **Interactive Download Buttons**: Choose between Video (`MP4 1080p/Max`) and Audio (`MP3`).
- **One-Click Transcription**: When downloading MP3, an inline **"📝 Transcribe Audio"** button is attached to the audio message.
- **Large File Support (up to 2 GB)**: Uses a local Telegram Bot API server container to bypass the public 20 MB / 50 MB limits.
- **Diarization & Multi-Speaker Detection**: Automatically formats transcripts with speaker labels (`[SPEAKER 1]`, `[SPEAKER 2]`).

---

## Why the local Telegram Bot API server is needed

The public Telegram Bot API only lets bots download files up to 20 MB and send files up to 50 MB. For larger media (like a 1080p video or long audio), run Telegram's Bot API server and point the bot at it. This setup uses Docker Compose to run both services.

---

## Setup & Deployment (VPS / Local)

### Option 1: Docker Compose (Recommended for VPS)

The Docker image automatically packages `ffmpeg`, `nodejs` (JavaScript runtime for yt-dlp), and all Python dependencies.

1. Put your secrets in `.env`:

   ```env
   TELEGRAM_BOT_TOKEN=...
   ELEVENLABS_API_KEY=...
   API_ID=...
   API_HASH=...
   ```

   `API_ID` and `API_HASH` come from <https://my.telegram.org/apps> and are used by the local Telegram Bot API server.

2. If this bot previously used Telegram's public Bot API, log it out once:

   ```bash
   ./logout.sh
   ```

3. Start services:

   ```bash
   docker compose up -d --build
   ```

4. Watch logs:

   ```bash
   docker compose logs -f bot telegram-bot-api
   ```

---

### Option 2: Bare-Metal Host / Local Python run

For small files, or if you run `telegram-bot-api` separately:

1. Ensure `ffmpeg` and `nodejs` are installed on the host VPS:

   ```bash
   sudo apt-get update && sudo apt-get install -y ffmpeg nodejs
   ```

2. Run setup:

   ```bash
   ./setup.sh
   ./run.sh
   ```

---

## Useful commands

```bash
# Run test suite
./venv/bin/pytest -v tests/

# Restart after code/config changes
docker compose up -d --build

# Stop everything
docker compose down

# Stop and remove Bot API cache files
docker compose down -v
```
