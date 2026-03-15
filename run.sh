#!/bin/bash
# Auto-restart wrapper for qwe-qwe web server
cd "$(dirname "$0")"
source .venv/bin/activate

# Set QWE_LLM_URL and QWE_EMBED_URL if your LLM server is not on localhost
# export QWE_LLM_URL="http://your-ip:1234/v1"
# export QWE_EMBED_URL="http://your-ip:1234/v1"

while true; do
    echo "[$(date)] Starting qwe-qwe --web..."
    python3 -u server.py >> logs/web.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] qwe-qwe exited with code $EXIT_CODE, restarting in 3s..."
    sleep 3
done
