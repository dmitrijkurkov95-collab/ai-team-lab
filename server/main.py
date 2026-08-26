"""REST API канбан-доски. Контракт: docs/api-contract.md.

Base URL: http://localhost:8000/api/v1
Запуск:   python -m uvicorn server.main:app --reload --port 8000
          (или python server/main.py)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:  # запуск как пакет: uvicorn server.main:app
    from server import db
    from server.schemas import Card, CardCreate, CardUpdate, Comment, CommentCreate
except ImportError:  # запуск изнутри папки: uvicorn main:app
    import db  # type: ignore[no-redef]
    from schemas import Card, CardCreate, CardUpdate, Comment, CommentCreate  # type: ignore[no-redef]

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get_conn()  # создаём схему и заливаем seed при первом старте
    yield


app = FastAPI(title="Kanban API", version="1.0.0", lifespan=lifespan)

# Фронтенд Agent-2 живёт на другом порту/origin — без CORS доска не заведётся.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

API = "/api/v1"


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc: RequestValidationError) -> JSONResponse:
    """Контракт требует 422 {"detail": "..."} — строку, а не список объектов.

    FastAPI по умолчанию отдаёт detail массивом; приводим к человеческой строке,
    иначе клиент по букве контракта получает не то, что ему обещали.
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body",))
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(status_code=422, content={"detail": "; ".join(parts) or "validation error"})


def _card_or_404(card_id: int) -> dict:
    card = db.get_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"Карточка {card_id} не найдена")
    return card


@app.get(f"{API}/cards", response_model=list[Card])
def get_cards(status: Optional[str] = Query(default=None)) -> list[dict]:
    """Список карточек по возрастанию id, опционально фильтр ?status=.

    Пустое значение (`?status=`) трактуем как отсутствие фильтра: пустой
    `<select>` на фронте не должен получать ошибку на ровном месте.
    """
    if status == "":
        status = None
    if status is not None and status not in db.STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status: недопустимое значение '{status}', ожидается одно из {list(db.STATUSES)}",
        )
    return db.list_cards(status)


@app.post(f"{API}/cards", response_model=Card, status_code=201)
def post_card(payload: CardCreate) -> dict:
    return db.create_card(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        status=payload.status,
    )


@app.get(f"{API}/cards/{{card_id}}", response_model=Card)
def get_card(card_id: int) -> dict:
    return _card_or_404(card_id)


@app.patch(f"{API}/cards/{{card_id}}", response_model=Card)
def patch_card(card_id: int, payload: CardUpdate) -> dict:
    _card_or_404(card_id)
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    updated = db.update_card(card_id, fields)
    if updated is None:  # карточку удалили между проверкой и апдейтом
        raise HTTPException(status_code=404, detail=f"Карточка {card_id} не найдена")
    return updated


@app.delete(f"{API}/cards/{{card_id}}", status_code=204)
def remove_card(card_id: int) -> Response:
    if not db.delete_card(card_id):
        raise HTTPException(status_code=404, detail=f"Карточка {card_id} не найдена")
    return Response(status_code=204)


@app.get(f"{API}/cards/{{card_id}}/comments", response_model=list[Comment])
def get_comments(card_id: int) -> list[dict]:
    _card_or_404(card_id)
    return db.list_comments(card_id)


@app.post(f"{API}/cards/{{card_id}}/comments", response_model=Comment, status_code=201)
def post_comment(card_id: int, payload: CommentCreate) -> dict:
    _card_or_404(card_id)
    return db.create_comment(card_id, payload.text)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
