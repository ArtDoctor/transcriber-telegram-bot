#!/bin/bash

set -euo pipefail

set -a
. .env
set +a

source venv/bin/activate

python main.py
