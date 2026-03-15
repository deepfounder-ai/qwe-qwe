#!/bin/bash
# Auto-restart wrapper for qwe-qwe web server
cd "$(dirname "$0")"
source .venv/bin/activate

while true; do
    echo "[$(date)] Starting qwe-qwe --web..."
    qwe-qwe --web 2>&1 | tee -a logs/web.log
    EXIT_CODE=$?
    echo "[$(date)] qwe-qwe exited with code $EXIT_CODE, restarting in 3s..."
    sleep 3
done
