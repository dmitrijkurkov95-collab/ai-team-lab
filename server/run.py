#!/usr/bin/env python3
"""Запуск сервера одной командой: `python run.py` (порт 8000)."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("KANBAN_HOST", "0.0.0.0"),
        port=int(os.environ.get("KANBAN_PORT", "8000")),
        reload=os.environ.get("KANBAN_RELOAD") == "1",
    )
