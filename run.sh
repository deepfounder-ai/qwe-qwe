#!/bin/bash
# Auto-restart wrapper for qwe-qwe web server
cd "$(dirname "$0")"
source .venv/bin/activate

export QWE_LLM_URL="http://192.168.0.49:1234/v1"
export QWE_EMBED_URL="http://192.168.0.49:1234/v1"

while true; do
    echo "[$(date)] Starting qwe-qwe --web..."
    python3 -u server.py >> logs/web.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] qwe-qwe exited with code $EXIT_CODE, restarting in 3s..."
    sleep 3
done
