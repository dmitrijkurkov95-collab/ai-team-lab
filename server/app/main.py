"""HTTP-слой канбан-доски.

Контракт: docs/api-contract.md. Base URL — /api/v1.
Роутеры ничего не знают про SQL: вся работа с данными — через Storage.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status as http_status
from fastapi.middleware.cors import CORSMiddleware

from .schemas import (
    Card,
    CardCreate,
    CardUpdate,
    Comment,
    CommentCreate,
    Priority,
    Status,
)
from .storage import DEFAULT_DB, Storage

API_PREFIX = "/api/v1"

_storage: Optional[Storage] = None


def get_storage() -> Storage:
    """Зависимость FastAPI — её же подменяют тесты."""
    assert _storage is not None, "storage is not initialised"
    return _storage


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _storage
    _storage = Storage(os.environ.get("KANBAN_DB", DEFAULT_DB))
    created = _storage.seed_if_empty()
    if created:
        print(f"[kanban] пустая база — засеял {created} карточек")
    yield
    _storage.close()
    _storage = None


app = FastAPI(
    title="Kanban Board API",
    version="1.0.0",
    description="REST API канбан-доски. Спринт 1: карточки, статусы, комментарии.",
    lifespan=lifespan,
)

# Фронт Agent-2 живёт на другом порту (file://, 5173, 3000...) — без CORS он
# упрётся в браузерную политику. Спринт 1 без авторизации, так что «*» безопасно.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _card_or_404(store: Storage, card_id: int) -> dict:
    card = store.get_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"Card {card_id} not found")
    return card


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "kanban-api",
        "version": "1.0.0",
        "api": API_PREFIX,
        "docs": "/docs",
        "health": f"{API_PREFIX}/health",
    }


@app.get(f"{API_PREFIX}/health", tags=["service"])
def health(store: Storage = Depends(get_storage)) -> dict:
    return {"status": "ok", "cards": len(store.list_cards())}


# ---------------------------------------------------------------- карточки


@app.get(f"{API_PREFIX}/cards", response_model=list[Card], tags=["cards"])
def list_cards(
    status: Optional[Status] = Query(default=None, description="фильтр по колонке"),
    store: Storage = Depends(get_storage),
) -> list[dict]:
    return store.list_cards(status.value if status else None)


@app.post(
    f"{API_PREFIX}/cards",
    response_model=Card,
    status_code=http_status.HTTP_201_CREATED,
    tags=["cards"],
)
def create_card(payload: CardCreate, store: Storage = Depends(get_storage)) -> dict:
    return store.create_card(
        title=payload.title,
        description=payload.description,
        status=payload.status.value,
        priority=payload.priority.value,
    )


@app.get(f"{API_PREFIX}/cards/{{card_id}}", response_model=Card, tags=["cards"])
def get_card(card_id: int, store: Storage = Depends(get_storage)) -> dict:
    return _card_or_404(store, card_id)


@app.patch(f"{API_PREFIX}/cards/{{card_id}}", response_model=Card, tags=["cards"])
def update_card(card_id: int, payload: CardUpdate, store: Storage = Depends(get_storage)) -> dict:
    _card_or_404(store, card_id)
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key in ("status", "priority"):
        if key in fields:
            fields[key] = fields[key].value if hasattr(fields[key], "value") else fields[key]
    updated = store.update_card(card_id, fields)
    if updated is None:  # гонка: карточку удалили между проверкой и апдейтом
        raise HTTPException(status_code=404, detail=f"Card {card_id} not found")
    return updated


@app.delete(
    f"{API_PREFIX}/cards/{{card_id}}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    tags=["cards"],
)
def delete_card(card_id: int, store: Storage = Depends(get_storage)) -> Response:
    if not store.delete_card(card_id):
        raise HTTPException(status_code=404, detail=f"Card {card_id} not found")
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------- комментарии


@app.get(
    f"{API_PREFIX}/cards/{{card_id}}/comments",
    response_model=list[Comment],
    tags=["comments"],
)
def list_comments(card_id: int, store: Storage = Depends(get_storage)) -> list[dict]:
    _card_or_404(store, card_id)
    return store.list_comments(card_id)


@app.post(
    f"{API_PREFIX}/cards/{{card_id}}/comments",
    response_model=Comment,
    status_code=http_status.HTTP_201_CREATED,
    tags=["comments"],
)
def add_comment(
    card_id: int, payload: CommentCreate, store: Storage = Depends(get_storage)
) -> dict:
    _card_or_404(store, card_id)
    return store.add_comment(card_id, payload.text)


__all__ = ["app", "Priority", "Status"]
