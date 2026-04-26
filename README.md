# Telegram ElevenLabs Transcriber Bot

Telegram bot that accepts voice/audio/video files, downloads them, sends them to ElevenLabs Scribe, and returns a speaker-labelled `.txt` transcript.

## Why the local Telegram Bot API server is needed

The public Telegram Bot API only lets bots download files up to about 20 MB. For larger files (like a 74 MB audio file), run Telegram's Bot API server yourself and point the bot at it. This setup uses Docker Compose to run both services.

## Setup

1. Put your secrets in `.env`:

   ```env
   TELEGRAM_BOT_TOKEN=...
   ELEVENLABS_API_KEY=...
   API_ID=...
   API_HASH=...
   ```

   `API_ID` and `API_HASH` come from <https://my.telegram.org/apps> and are used by the local Telegram Bot API server.

2. If this bot has previously used Telegram's public Bot API, log it out once so the local server can take over:

   ```bash
   set -a; source .env; set +a
   curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut"
   ```

3. Start the local Bot API server and the transcriber bot:

   ```bash
   docker compose up -d --build
   ```

4. Watch logs:

   ```bash
   docker compose logs -f bot telegram-bot-api
   ```

5. Send the bot a file larger than 20 MB. The compose setup sets:

   ```env
   TELEGRAM_BOT_API_BASE_URL=http://telegram-bot-api:8081/bot
   TELEGRAM_BOT_API_BASE_FILE_URL=http://telegram-bot-api:8081/file/bot
   TELEGRAM_LOCAL_MODE=true
   MAX_TELEGRAM_DOWNLOAD_BYTES=2147483648
   ```

## Local Python run without Docker

For small files, or if you already run `telegram-bot-api` yourself:

```bash
./setup.sh
./run.sh
```

If your local Bot API server is on the host at port `8081`, add this to `.env` before `./run.sh`:

```env
TELEGRAM_BOT_API_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_BOT_API_BASE_FILE_URL=http://127.0.0.1:8081/file/bot
MAX_TELEGRAM_DOWNLOAD_BYTES=2147483648
# Use TELEGRAM_LOCAL_MODE=true only if the bot process can read the Bot API
# server's --dir path directly at the same filesystem path.
```

## Useful commands

```bash
# restart after code/config changes
docker compose up -d --build

# stop everything
docker compose down

# stop and remove Bot API cached files too
docker compose down -v
```
