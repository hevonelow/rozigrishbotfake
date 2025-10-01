#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
# Install dependencies (platforms usually do this already)
pip install --no-cache-dir -r requirements.txt
exec python3 bot.py
