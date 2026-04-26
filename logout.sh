#!/bin/bash

set -euo pipefail

set -a
source .env
set +a

TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut"

echo "Logged out from Telegram Bot API"