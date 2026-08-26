"""Слой хранения: SQLite через стандартную библиотеку.

Держим всё простым: две таблицы, явный SQL, никакой ORM. Для семи эндпоинтов
это честнее, чем тащить SQLAlchemy — меньше слоёв между контрактом и данными.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

DB_PATH = os.environ.get("KANBAN_DB", os.path.join(os.path.dirname(__file__), "kanban.db"))

STATUSES = ("backlog", "in_progress", "review", "done")
PRIORITIES = ("low", "normal", "high")
TITLE_MAX = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'backlog',
    priority    TEXT NOT NULL DEFAULT 'normal',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_card ON comments(card_id);
"""

SEED_CARDS = [
    ("Свёрстать колонки доски", "Четыре статуса, счётчик карточек в шапке колонки.", "done", "normal"),
    ("REST API карточек", "CRUD, фильтр по статусу, комментарии.", "review", "high"),
    ("Drag&drop между колонками", "Перетаскивание меняет статус карточки через PATCH.", "in_progress", "high"),
    ("Контракт-тесты по api-contract.md", "Покрыть все эндпоинты и граничные случаи.", "in_progress", "normal"),
    ("Пустые состояния и ошибки", "Показывать пользователю, что пошло не так, а не молчать.", "backlog", "low"),
    ("Деплой демо-стенда", "Поднять сборку одной командой для показа лиду.", "backlog", "normal"),
]

# Единственное соединение на процесс: FastAPI ходит из разных потоков,
# поэтому check_same_thread=False + сериализация на стороне sqlite3.
_conn: sqlite3.Connection | None = None


def utc_now() -> str:
    """ISO 8601 UTC с суффиксом Z, как требует контракт."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
        seed_if_empty(_conn)
    return _conn


def reset_conn() -> None:
    """Сбросить соединение (нужно тестам, которые подменяют KANBAN_DB)."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def seed_if_empty(conn: sqlite3.Connection) -> None:
    """Наполняем доску только если она пуста — чтобы рестарт не плодил дубли."""
    if conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]:
        return
    now = utc_now()
    conn.executemany(
        "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(t, d, s, p, now, now) for t, d, s, p in SEED_CARDS],
    )
    conn.commit()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# --- карточки ---------------------------------------------------------------

def list_cards(status: str | None = None) -> list[dict[str, Any]]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM cards WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM cards ORDER BY id").fetchall()
    return [row_to_dict(r) for r in rows]


def get_card(card_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return row_to_dict(row) if row else None


def create_card(title: str, description: str = "", priority: str = "normal",
                status: str = "backlog") -> dict[str, Any]:
    conn = get_conn()
    now = utc_now()
    cur = conn.execute(
        "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (title, description, status, priority, now, now),
    )
    conn.commit()
    return get_card(cur.lastrowid)  # type: ignore[arg-type]


def update_card(card_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Частичное обновление. Пустой набор полей — не ошибка, просто no-op."""
    current = get_card(card_id)
    if current is None:
        return None
    changed = {k: v for k, v in fields.items() if current.get(k) != v}
    if not changed:
        return current
    conn = get_conn()
    changed["updated_at"] = utc_now()
    assignments = ", ".join(f"{k} = ?" for k in changed)
    conn.execute(
        f"UPDATE cards SET {assignments} WHERE id = ?",
        (*changed.values(), card_id),
    )
    conn.commit()
    return get_card(card_id)


def delete_card(card_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    return cur.rowcount > 0


# --- комментарии ------------------------------------------------------------

def list_comments(card_id: int) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM comments WHERE card_id = ? ORDER BY created_at, id", (card_id,)
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def create_comment(card_id: int, text: str) -> dict[str, Any]:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO comments (card_id, text, created_at) VALUES (?, ?, ?)",
        (card_id, text, utc_now()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM comments WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)
