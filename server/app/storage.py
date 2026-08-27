"""Хранилище на SQLite: единственное место, где живёт SQL.

Отделено от роутеров намеренно — заменить SQLite на что угодно можно,
не трогая HTTP-слой.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Any, Iterator, Optional

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


def _ulower(value: Optional[str]) -> Optional[str]:
    """Unicode-совместимый lower() для SQLite (см. create_function в _connect)."""
    return value.lower() if isinstance(value, str) else value


def _escape_like(term: str) -> str:
    """Экранирует спецсимволы LIKE, чтобы поиск '100%' не стал маской.

    Без этого `%` и `_` в пользовательском запросе означали бы «любой текст»
    и «любой символ»: поиск по «100%» находил бы всё подряд.
    """
    for ch in ("\\", "%", "_"):
        term = term.replace(ch, "\\" + ch)
    return term


def utcnow() -> str:
    """Текущее время в формате меток контракта.

    Секундной точности достаточно: сортировки, где метки могут совпасть
    (список комментариев), дополнительно упорядочены по id.
    """
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


class Storage:
    """Доступ к SQLite. Соединение открывается на каждую операцию.

    Раньше здесь жило одно общее соединение с `check_same_thread=False`.
    FastAPI выполняет синхронные обработчики в пуле потоков, поэтому несколько
    запросов работали с одним `sqlite3.Connection` одновременно, затирали его
    внутреннее состояние и получали `InterfaceError` → клиенту `500`
    (до ~8% запросов под параллельной нагрузкой, issue #21).

    Флаг `check_same_thread=False` снимал проверку, но не делал объект
    потокобезопасным — он лишь разрешал то, от чего проверка защищала.

    Соединение на операцию: для локального файла это десятки микросекунд,
    состояние между запросами хранить не нужно, и рассуждать о потоках
    больше не приходится.
    """

    def __init__(self, path: str = DEFAULT_DB) -> None:
        if path == ":memory:":
            # У каждого соединения своя приватная БД в памяти, поэтому схема,
            # созданная при старте, следующему запросу уже не видна. Молча
            # отдавать «no such table» на первом же запросе — худший вариант.
            raise ValueError(
                'путь ":memory:" не поддерживается: соединение открывается на запрос, '
                "и каждое получило бы собственную пустую БД. "
                'Для тестов используйте файл (например, tmp_path / "test.db").'
            )
        self.path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # WAL: читатели не блокируют писателя. При нескольких соединениях
            # к одному файлу без этого легко поймать 'database is locked'.
            conn.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Соединение на операцию: открыли, поработали, закрыли.

        `timeout` — сколько ждать снятия блокировки записи, прежде чем
        отдать 'database is locked'. Пять секунд с запасом покрывают
        параллельные записи на файловой БД.
        """
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")  # каскад удаления комментариев
            # Встроенный LIKE в SQLite регистронезависим ТОЛЬКО для ASCII:
            # 'Карточка' LIKE '%карточка%' → 0 совпадений, а доска у нас русская.
            # Отдаём приведение регистра Python — он знает Unicode.
            conn.create_function("ulower", 1, _ulower, deterministic=True)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- служебное ----------

    def close(self) -> None:
        """Оставлено для совместимости: постоянных соединений больше нет."""
        return None

    def seed_if_empty(self) -> int:
        """Наполняет доску, только если карточек нет вообще."""
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]:
                return 0
            now = utcnow()
            conn.executemany(
                "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                [(t, d, s, p, now, now) for t, d, s, p in SEED_CARDS],
            )
            conn.execute(
                "INSERT INTO comments (card_id, text, created_at) VALUES (?,?,?)",
                (2, "Контракт — догма: пути и коды ошибок не меняем без PR в api-contract.md.", now),
            )
        return len(SEED_CARDS)

    # ---------- карточки ----------

    def list_cards(
        self,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        limit: Optional[int] = None,
        q: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Список карточек с опциональными фильтрами.

        Лимит применяется после сортировки по id, чтобы «первые N» были
        стабильны между запросами, а не зависели от порядка в файле БД.

        `q` — поиск подстроки в title и description, регистронезависимый
        и для кириллицы (через ulower, см. _connect).
        """
        sql = "SELECT * FROM cards"
        where, args = self._filters(status, priority, q)
        sql += where
        sql += " ORDER BY id ASC"  # контракт: по возрастанию id
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    @staticmethod
    def _filters(
        status: Optional[str], priority: Optional[str], q: Optional[str] = None
    ) -> tuple[str, list[Any]]:
        """Собирает WHERE один раз для list_cards и count_cards.

        Иначе X-Total-Count и сама выдача считались бы по разным условиям —
        ровно тот класс расхождений, который доска показывает как «неполная».
        """
        conditions: list[str] = []
        args: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            args.append(status)
        if priority is not None:
            conditions.append("priority = ?")
            args.append(priority)
        if q:
            conditions.append(
                "(ulower(title) LIKE ulower(?) ESCAPE '\\'"
                " OR ulower(description) LIKE ulower(?) ESCAPE '\\')"
            )
            pattern = f"%{_escape_like(q)}%"
            args += [pattern, pattern]
        return (" WHERE " + " AND ".join(conditions)) if conditions else "", args

    def count_cards(
        self, status: Optional[str] = None, priority: Optional[str] = None, q: Optional[str] = None
    ) -> int:
        """Сколько карточек попадает под фильтр — без учёта limit."""
        sql = "SELECT COUNT(*) AS n FROM cards"
        where, args = self._filters(status, priority, q)
        sql += where
        with self._connect() as conn:
            return int(conn.execute(sql, args).fetchone()["n"])

    def get_card(self, card_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return dict(row) if row else None

    def create_card(self, title: str, description: str, status: str, priority: str) -> dict[str, Any]:
        now = utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (title, description, status, priority, now, now),
            )
            new_id = cur.lastrowid
        return self.get_card(new_id)  # type: ignore[arg-type]

    def update_card(self, card_id: int, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        current = self.get_card(card_id)
        if current is None:
            return None
        if not fields:  # PATCH пустым телом — не ошибка, просто ничего не меняем
            return current
        sets = ", ".join(f"{k} = ?" for k in fields)
        args = list(fields.values()) + [utcnow(), card_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE cards SET {sets}, updated_at = ? WHERE id = ?", args)
        return self.get_card(card_id)

    def delete_card(self, card_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
            return cur.rowcount > 0

    # ---------- комментарии ----------

    def list_comments(self, card_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comments WHERE card_id = ? ORDER BY created_at ASC, id ASC",
                (card_id,),
            )
            return [dict(r) for r in rows]

    def add_comment(self, card_id: int, text: str) -> dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO comments (card_id, text, created_at) VALUES (?,?,?)",
                (card_id, text, utcnow()),
            )
            row = conn.execute("SELECT * FROM comments WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)
