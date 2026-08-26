"""Хранилище на SQLite: единственное место, где живёт SQL.

Отделено от роутеров намеренно — заменить SQLite на что угодно можно,
не трогая HTTP-слой.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kanban.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'backlog',
    priority    TEXT    NOT NULL DEFAULT 'normal',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_comments_card ON comments(card_id, id);
"""

SEED_CARDS = [
    ("Свести архитектуру спринта 1", "Колонки, карточки, комментарии — без авторизации.", "done", "high"),
    ("REST API канбан-доски", "FastAPI + SQLite, всё по контракту api-contract.md.", "in_progress", "high"),
    ("Доска с drag&drop", "4 колонки, счётчики, бейдж приоритета.", "in_progress", "normal"),
    ("Контракт-тесты по API", "pytest + httpx против localhost:8000, edge cases.", "review", "normal"),
    ("Заполнить README проекта", "Как поднять сервер и фронт одной командой.", "backlog", "low"),
    ("Спринт 2: поиск и теги", "Пока только идея, обсудим после интеграции.", "backlog", "low"),
]


#: Формат меток из контракта: 2026-08-26T12:00:00Z — ISO 8601 UTC, без дробной секунды.
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> str:
    """Текущее время в формате меток контракта.

    Секундной точности достаточно: сортировки, где метки могут совпасть
    (список комментариев), дополнительно упорядочены по id.
    """
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


class Storage:
    def __init__(self, path: str = DEFAULT_DB) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- служебное ----------

    def close(self) -> None:
        self._conn.close()

    def seed_if_empty(self) -> int:
        """Наполняет доску, только если карточек нет вообще."""
        if self._conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]:
            return 0
        now = utcnow()
        self._conn.executemany(
            "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            [(t, d, s, p, now, now) for t, d, s, p in SEED_CARDS],
        )
        self._conn.execute(
            "INSERT INTO comments (card_id, text, created_at) VALUES (?,?,?)",
            (2, "Контракт — догма: пути и коды ошибок не меняем без PR в api-contract.md.", now),
        )
        self._conn.commit()
        return len(SEED_CARDS)

    # ---------- карточки ----------

    def list_cards(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cards"
        args: Iterable[Any] = ()
        if status is not None:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY id ASC"  # контракт: по возрастанию id
        return [dict(r) for r in self._conn.execute(sql, args)]

    def get_card(self, card_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return dict(row) if row else None

    def create_card(self, title: str, description: str, status: str, priority: str) -> dict[str, Any]:
        now = utcnow()
        cur = self._conn.execute(
            "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (title, description, status, priority, now, now),
        )
        self._conn.commit()
        return self.get_card(cur.lastrowid)  # type: ignore[arg-type]

    def update_card(self, card_id: int, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        current = self.get_card(card_id)
        if current is None:
            return None
        if not fields:  # PATCH пустым телом — не ошибка, просто ничего не меняем
            return current
        sets = ", ".join(f"{k} = ?" for k in fields)
        args = list(fields.values()) + [utcnow(), card_id]
        self._conn.execute(f"UPDATE cards SET {sets}, updated_at = ? WHERE id = ?", args)
        self._conn.commit()
        return self.get_card(card_id)

    def delete_card(self, card_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ---------- комментарии ----------

    def list_comments(self, card_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM comments WHERE card_id = ? ORDER BY created_at ASC, id ASC",
            (card_id,),
        )
        return [dict(r) for r in rows]

    def add_comment(self, card_id: int, text: str) -> dict[str, Any]:
        cur = self._conn.execute(
            "INSERT INTO comments (card_id, text, created_at) VALUES (?,?,?)",
            (card_id, text, utcnow()),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM comments WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)
