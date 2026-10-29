#!/bin/bash
# Stack dump of the running trader (needs sudo because of the system's ptrace policy).
cd "$(dirname "$0")/.."
PID=$(pgrep -f "python -m src.main" | head -1)
[ -z "$PID" ] && { echo "trader not running"; exit 1; }
.venv/bin/py-spy dump --pid "$PID" > "logs/pyspy_$PID.txt" 2>&1 && echo "saved logs/pyspy_$PID.txt" || cat "logs/pyspy_$PID.txt"
