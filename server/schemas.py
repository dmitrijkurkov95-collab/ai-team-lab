"""Pydantic-схемы запросов и ответов.

Валидация здесь = буква контракта: непустой title до 200 символов,
статусы и приоритеты из закрытых списков. Всё, что не проходит, — 422.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Status = Literal["backlog", "in_progress", "review", "done"]
Priority = Literal["low", "normal", "high"]

TITLE_MAX = 200


def _clean_title(value: str) -> str:
    """Пробелы по краям режем, потом проверяем. Строка из пробелов = пустая."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("title не может быть пустым")
    if len(cleaned) > TITLE_MAX:
        raise ValueError(f"title длиннее {TITLE_MAX} символов")
    return cleaned


class CardCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    description: str = ""
    priority: Priority = "normal"
    status: Status = "backlog"

    @field_validator("title")
    @classmethod
    def check_title(cls, v: str) -> str:
        return _clean_title(v)


class CardUpdate(BaseModel):
    """Все поля опциональны: PATCH меняет только то, что прислали."""

    model_config = ConfigDict(extra="ignore")

    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[Status] = None
    priority: Optional[Priority] = None

    @field_validator("title")
    @classmethod
    def check_title(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_title(v)


class Card(BaseModel):
    id: int
    title: str
    description: str
    status: Status
    priority: Priority
    created_at: str
    updated_at: str


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def check_text(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("text не может быть пустым")
        return cleaned


class Comment(BaseModel):
    id: int
    card_id: int
    text: str
    created_at: str
