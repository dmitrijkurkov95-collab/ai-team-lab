"""Pydantic-схемы: ровно то, что описано в docs/api-contract.md."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

#: Контракт: "2026-08-26T12:00:00Z" — ISO 8601 UTC без дробной секунды.
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _serialize_ts(value: datetime) -> str:
    """Гарантирует формат меток на границе API.

    Нормализуем при отдаче, а не только при записи: в уже существующих базах
    лежат строки с микросекундами, и мигрировать их ради формата вывода незачем.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime(TS_FORMAT)


class Status(str, Enum):
    backlog = "backlog"
    in_progress = "in_progress"
    review = "review"
    done = "done"


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"


def _clean_deadline(value):
    """Проверяет и нормализует дедлайн к формату меток контракта.

    Прошлое разрешено сознательно: запрет сломал бы импорт исторических досок
    и правку задним числом, а просроченная задача — нормальное состояние, а не
    ошибка ввода. Просрочку считает клиент (см. контракт), сервер хранит факт.

    Принимаем и `...Z`, и смещение (`+03:00`) — приводим к UTC, чтобы в базе
    лежал один формат и сортировка по строке совпадала с хронологией.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("deadline must be an ISO 8601 datetime or null")
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                "deadline must be ISO 8601, e.g. 2026-09-01T18:00:00Z"
            ) from None
    if parsed.tzinfo is None:
        # Без зоны трактуем как UTC: молча сдвинуть время на зону сервера —
        # худший вариант, дедлайн уехал бы на часы незаметно для пользователя.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime(TS_FORMAT)


def _clean_title(value: str) -> str:
    """Контракт: title — непустая строка до 200 символов.

    Строку из одних пробелов считаем пустой: иначе в доску попадают карточки
    с невидимым заголовком, а фронт рисует пустой прямоугольник.
    """
    if not isinstance(value, str):
        raise ValueError("title must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("title must not be empty")
    if len(trimmed) > 200:
        raise ValueError("title must be at most 200 characters")
    return trimmed


class CardCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    description: str = ""
    priority: Priority = Priority.normal
    # status в контракте у POST не описан, но принять его безопасно и удобно
    # (фронт может сразу создать карточку в нужной колонке). Default — backlog.
    status: Status = Status.backlog
    deadline: Optional[str] = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        return _clean_title(v)

    @field_validator("deadline", mode="before")
    @classmethod
    def validate_deadline(cls, v):
        return _clean_deadline(v)

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, v):
        return "" if v is None else v


class CardUpdate(BaseModel):
    """PATCH: частичное обновление. Отличаем «не передано» от «передано null»."""

    model_config = ConfigDict(extra="ignore")

    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[Priority] = None
    status: Optional[Status] = None
    deadline: Optional[str] = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, v):
        return v if v is None else _clean_title(v)

    @field_validator("deadline", mode="before")
    @classmethod
    def validate_deadline(cls, v):
        return _clean_deadline(v)


class Card(BaseModel):
    id: int
    title: str
    description: str
    status: Status
    priority: Priority
    created_at: datetime
    updated_at: datetime
    deadline: Optional[datetime] = None

    @field_serializer("deadline")
    def serialize_deadline(self, value: Optional[datetime], _info) -> Optional[str]:
        return None if value is None else _serialize_ts(value)

    @field_serializer("created_at", "updated_at")
    def serialize_ts(self, value: datetime, _info) -> str:
        return _serialize_ts(value)


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("text must not be empty")
        return trimmed


class Comment(BaseModel):
    id: int
    card_id: int
    text: str
    created_at: datetime

    @field_serializer("created_at")
    def serialize_ts(self, value: datetime, _info) -> str:
        return _serialize_ts(value)
