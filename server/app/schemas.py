"""Pydantic-схемы: ровно то, что описано в docs/api-contract.md."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Status(str, Enum):
    backlog = "backlog"
    in_progress = "in_progress"
    review = "review"
    done = "done"


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"


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

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        return _clean_title(v)

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

    @field_validator("title")
    @classmethod
    def validate_title(cls, v):
        return v if v is None else _clean_title(v)


class Card(BaseModel):
    id: int
    title: str
    description: str
    status: Status
    priority: Priority
    created_at: datetime
    updated_at: datetime


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
