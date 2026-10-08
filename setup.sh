#!/bin/bash

set -euo pipefail

if ! command -v ffmpeg &> /dev/null; then
    echo "Warning: ffmpeg is not installed. Please install it ('sudo apt-get install ffmpeg') for YouTube audio/video processing."
fi

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
