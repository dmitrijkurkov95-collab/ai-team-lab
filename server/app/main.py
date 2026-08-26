"""HTTP-слой канбан-доски.

Контракт: docs/api-contract.md. Base URL — /api/v1.
Роутеры ничего не знают про SQL: вся работа с данными — через Storage.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status as http_status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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


def _humanize(error: dict) -> str:
    """Одна ошибка валидации Pydantic → короткая понятная фраза."""
    loc = [str(p) for p in error.get("loc", []) if p not in ("body", "query", "path")]
    field = ".".join(loc)
    msg = error.get("msg", "invalid value")
    # Pydantic префиксует пользовательские ValueError строкой "Value error, "
    msg = msg.removeprefix("Value error, ")
    return f"{field}: {msg}" if field else msg


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Контракт обещает `detail` строкой — приводим 422 к тому же формату, что и 404.

    По умолчанию FastAPI отдаёт `detail` массивом объектов, из-за чего клиенту
    приходится разбирать два разных формата ошибок. Нашёл Agent-2 при интеграции.
    Подробности не теряем: они уезжают в поле `errors` рядом.
    """
    errors = exc.errors()
    detail = "; ".join(_humanize(e) for e in errors) or "Validation error"
    safe_errors = json.loads(json.dumps(errors, default=str))  # ctx может быть несериализуем
    return JSONResponse(status_code=422, content={"detail": detail, "errors": safe_errors})


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


#: Потолок для явного ?limit= — защита от ?limit=100000.
MAX_LIMIT = 200


@app.get(f"{API_PREFIX}/cards", response_model=list[Card], tags=["cards"])
def list_cards(
    response: Response,
    status: Optional[Status] = Query(default=None, description="фильтр по колонке"),
    priority: Optional[Priority] = Query(default=None, description="фильтр по приоритету"),
    limit: Optional[int] = Query(
        default=None,
        ge=1,
        le=MAX_LIMIT,
        description=f"сколько карточек вернуть, 1…{MAX_LIMIT}; без параметра — все",
    ),
    store: Storage = Depends(get_storage),
) -> list[dict]:
    """Список карточек. Фильтры комбинируются, лимит применяется после сортировки.

    Урезание — только по явному запросу. Дефолтного лимита сознательно нет:
    доска рисует все колонки разом, и молчаливое обрезание выдачи означало бы,
    что интерфейс показывает неполную доску, не сообщая об этом. Ровно за это
    я отказался молча обрезать limit>потолка — принцип должен работать и здесь.

    Заголовок X-Total-Count отдаёт полное число карточек под фильтром, чтобы
    клиент мог обнаружить урезание, не угадывая.
    """
    status_value = status.value if status else None
    priority_value = priority.value if priority else None
    total = store.count_cards(status=status_value, priority=priority_value)
    response.headers["X-Total-Count"] = str(total)
    return store.list_cards(status=status_value, priority=priority_value, limit=limit)


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
