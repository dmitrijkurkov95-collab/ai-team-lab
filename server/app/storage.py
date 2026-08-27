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
    updated_at  TEXT    NOT NULL,
    deadline    TEXT             -- nullable: срок есть не у каждой задачи
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


#: Как сортировать: имя из API → SQL-выражение.
#: Тай-брейк по id добавляется всегда (см. _order_by): при равных ключах порядок
#: обязан быть стабильным, иначе страницы пагинации поедут — урок #11/#27.
SORT_EXPRESSIONS = {
    # id и created_at идут в одну сторону (AUTOINCREMENT), но created_at
    # оставляем явным: клиенту понятнее, а совпадение — деталь реализации.
    "created": "created_at",
    # По смыслу, а не по алфавиту: high > normal > low.
    "priority": "CASE priority WHEN 'high' THEN 3 WHEN 'normal' THEN 2 ELSE 1 END",
    "deadline": "deadline",
}

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
            self._migrate(conn)
            # WAL: читатели не блокируют писателя. При нескольких соединениях
            # к одному файлу без этого легко поймать 'database is locked'.
            conn.execute("PRAGMA journal_mode = WAL")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Догоняет схему на уже существующих базах.

        `CREATE TABLE IF NOT EXISTS` не добавляет колонки в живую таблицу:
        у всех, кто уже гонял сервер, файл БД остался бы без `deadline`,
        и первый же запрос упал бы с `no such column`. Проверяем и досыпаем.
        """
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
        if "deadline" not in columns:
            conn.execute("ALTER TABLE cards ADD COLUMN deadline TEXT")
        # Индекс создаём здесь, а не в SCHEMA: на старой базе SCHEMA выполняется
        # раньше ALTER, и индекс по ещё не существующей колонке уронил бы старт.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_deadline ON cards(deadline)")

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
        offset: Optional[int] = None,
        q: Optional[str] = None,
        sort: Optional[str] = None,
        order: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Список карточек с опциональными фильтрами.

        Лимит применяется после сортировки по id, чтобы «первые N» были
        стабильны между запросами, а не зависели от порядка в файле БД.

        `offset` пропускает первые N строк той же отсортированной выборки.
        SQLite не принимает OFFSET без LIMIT, поэтому при `offset` без `limit`
        подставляем `LIMIT -1` — документированный способ сказать «без предела».

        `q` — поиск подстроки в title и description, регистронезависимый
        и для кириллицы (через ulower, см. _connect).
        """
        sql = "SELECT * FROM cards"
        where, args = self._filters(status, priority, q)
        sql += where
        sql += self._order_by(sort, order)
        if limit is not None or offset:
            sql += " LIMIT ?"
            args.append(limit if limit is not None else -1)
        if offset:
            sql += " OFFSET ?"
            args.append(offset)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    @staticmethod
    def _order_by(sort: Optional[str], order: Optional[str]) -> str:
        """ORDER BY с тай-брейком по id и NULL-ами дедлайна в конце.

        Дефолт — `id ASC`: ровно то, что было до появления сортировки,
        поэтому доска, не знающая про `?sort=`, работает как раньше.

        Карточки без дедлайна всегда идут в конец, независимо от направления:
        «срока нет» — это не «срок бесконечно ранний», и пустой хвост в начале
        списка выглядел бы как баг, а не как сортировка.

        Тай-брейк по id идёт в ту же сторону, что и основной ключ. Метки
        округлены до секунды, поэтому у пачки карточек `created_at` совпадает;
        при жёстком `id ASC` запрос «сначала новые» показывал бы внутри такой
        пачки сначала старые — порядок был бы стабильным, но неверным на вид.
        """
        direction = "DESC" if (order or "").lower() == "desc" else "ASC"
        if not sort:
            return f" ORDER BY id {direction}"
        expression = SORT_EXPRESSIONS[sort]
        clause = ""
        if sort == "deadline":
            clause = "deadline IS NULL, "  # 0 (есть) раньше 1 (нет) — всегда
        return f" ORDER BY {clause}{expression} {direction}, id {direction}"

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

    def create_card(
        self,
        title: str,
        description: str,
        status: str,
        priority: str,
        deadline: Optional[str] = None,
    ) -> dict[str, Any]:
        now = utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO cards (title, description, status, priority, created_at, updated_at,"
                " deadline) VALUES (?,?,?,?,?,?,?)",
                (title, description, status, priority, now, now, deadline),
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

    # ---------- экспорт / импорт ----------

    def export_board(self) -> dict[str, Any]:
        """Вся доска одним снимком: карточки со своими комментариями."""
        with self._connect() as conn:
            cards = [dict(r) for r in conn.execute("SELECT * FROM cards ORDER BY id ASC")]
            comments = [
                dict(r)
                for r in conn.execute("SELECT * FROM comments ORDER BY card_id ASC, id ASC")
            ]
        by_card: dict[int, list[dict[str, Any]]] = {}
        for comment in comments:
            by_card.setdefault(comment["card_id"], []).append(comment)
        for card in cards:
            card["comments"] = by_card.get(card["id"], [])
        return {"cards": cards}

    def replace_board(self, cards: list[dict[str, Any]]) -> int:
        """Заменяет доску целиком, сохраняя id из файла.

        Всё в одной транзакции: битый файл не должен оставить половину доски.
        `_connect` откатывает при исключении, поэтому при ошибке база остаётся
        ровно такой, какой была до импорта.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM comments")
            conn.execute("DELETE FROM cards")
            for card in cards:
                conn.execute(
                    "INSERT INTO cards (id, title, description, status, priority,"
                    " created_at, updated_at, deadline) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        card["id"],
                        card["title"],
                        card["description"],
                        card["status"],
                        card["priority"],
                        card["created_at"],
                        card["updated_at"],
                        card.get("deadline"),
                    ),
                )
                for comment in card.get("comments", []):
                    conn.execute(
                        "INSERT INTO comments (id, card_id, text, created_at) VALUES (?,?,?,?)",
                        (comment["id"], card["id"], comment["text"], comment["created_at"]),
                    )
            # AUTOINCREMENT продолжит с максимума — иначе следующая созданная
            # карточка получила бы id, уже занятый импортированной.
            self._reset_autoincrement(conn)
        return len(cards)

    def merge_board(self, cards: list[dict[str, Any]]) -> int:
        """Добавляет карточки к существующим, выдавая им новые id.

        Id из файла сознательно не сохраняются: в непустой базе они почти
        наверняка заняты, а молча перезаписать чужую карточку с тем же
        номером — худшее, что может сделать импорт.
        """
        with self._connect() as conn:
            for card in cards:
                cur = conn.execute(
                    "INSERT INTO cards (title, description, status, priority,"
                    " created_at, updated_at, deadline) VALUES (?,?,?,?,?,?,?)",
                    (
                        card["title"],
                        card["description"],
                        card["status"],
                        card["priority"],
                        card["created_at"],
                        card["updated_at"],
                        card.get("deadline"),
                    ),
                )
                new_id = cur.lastrowid
                for comment in card.get("comments", []):
                    conn.execute(
                        "INSERT INTO comments (card_id, text, created_at) VALUES (?,?,?)",
                        (new_id, comment["text"], comment["created_at"]),
                    )
        return len(cards)

    @staticmethod
    def _reset_autoincrement(conn: sqlite3.Connection) -> None:
        """Двигает счётчик AUTOINCREMENT к максимальному занятому id.

        Иначе следующая созданная карточка получила бы id, уже занятый
        импортированной, и вставка упала бы на UNIQUE.

        `sqlite_sequence` — служебная таблица без UNIQUE-ограничения на `name`,
        поэтому `ON CONFLICT` к ней неприменим (ловил `OperationalError`):
        пробуем UPDATE, и только если строки для таблицы ещё нет — INSERT.
        Строка появляется лишь после первой вставки в таблицу с AUTOINCREMENT.
        """
        for table in ("cards", "comments"):
            row = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}").fetchone()
            highest = row["m"]
            updated = conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (highest, table)
            ).rowcount
            if not updated:
                conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, highest)
                )

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
