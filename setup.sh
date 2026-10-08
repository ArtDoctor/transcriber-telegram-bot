#!/bin/bash

set -euo pipefail

if ! command -v ffmpeg &> /dev/null; then
    echo "Warning: ffmpeg is not installed. Please install it ('sudo apt-get install ffmpeg') for YouTube audio/video processing."
fi

if ! command -v deno &> /dev/null && ! command -v node &> /dev/null; then
    echo "Notice: Neither Deno nor Node.js found. Installing Deno ('curl -fsSL https://deno.land/install.sh | sh') is recommended for YouTube challenge solving."
fi

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
