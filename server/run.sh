#!/usr/bin/env bash
# Запуск сервера канбан-доски на порту 8000.
set -e
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
