"""Тесты сервера канбан-доски.

Это тесты Agent-1 на свою реализацию (контракт-сьют Agent-3 живёт в /tests).
Каждый тест — на своей временной БД, поэтому seed и порядок не влияют.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app, get_storage  # noqa: E402
from app.storage import Storage  # noqa: E402

API = "/api/v1"


@pytest.fixture()
def client(tmp_path):
    store = Storage(str(tmp_path / "test.db"))
    app.dependency_overrides[get_storage] = lambda: store
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    store.close()


def make_card(client, **kwargs):
    body = {"title": "Карточка"} | kwargs
    r = client.post(f"{API}/cards", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ создание


def test_create_card_defaults(client):
    card = make_card(client, title="Сверстать колонки")
    assert card["title"] == "Сверстать колонки"
    assert card["description"] == ""
    assert card["status"] == "backlog"
    assert card["priority"] == "normal"
    assert card["created_at"].endswith("Z") and card["updated_at"].endswith("Z")
    assert isinstance(card["id"], int)


def test_create_card_with_all_fields(client):
    card = make_card(client, title="Срочное", description="детали", priority="high", status="review")
    assert (card["priority"], card["status"], card["description"]) == ("high", "review", "детали")


@pytest.mark.parametrize(
    "payload",
    [
        {},                              # нет title
        {"title": ""},                   # пустой
        {"title": "   "},                # только пробелы
        {"title": "x" * 201},            # длиннее 200
        {"title": "ok", "priority": "urgent"},   # приоритета нет в контракте
        {"title": "ok", "status": "archived"},   # статуса нет в контракте
        {"title": 12345},                # не строка
    ],
)
def test_create_card_validation_422(client, payload):
    assert client.post(f"{API}/cards", json=payload).status_code == 422


def test_title_boundary_200_ok(client):
    card = make_card(client, title="x" * 200)
    assert len(card["title"]) == 200


def test_title_is_trimmed(client):
    assert make_card(client, title="  с пробелами  ")["title"] == "с пробелами"


# -------------------------------------------------------------------- чтение


def test_list_empty_returns_empty_array(client):
    r = client.get(f"{API}/cards")
    assert r.status_code == 200 and r.json() == []


def test_list_sorted_by_id_asc(client):
    for t in ("первая", "вторая", "третья"):
        make_card(client, title=t)
    ids = [c["id"] for c in client.get(f"{API}/cards").json()]
    assert ids == sorted(ids)


def test_filter_by_status(client):
    make_card(client, title="a", status="backlog")
    make_card(client, title="b", status="done")
    make_card(client, title="c", status="done")
    done = client.get(f"{API}/cards", params={"status": "done"}).json()
    assert len(done) == 2 and {c["status"] for c in done} == {"done"}


def test_filter_by_unknown_status_422(client):
    assert client.get(f"{API}/cards", params={"status": "nope"}).status_code == 422


def test_get_missing_card_404(client):
    r = client.get(f"{API}/cards/99999")
    assert r.status_code == 404 and "detail" in r.json()


# ---------------------------------------------------------------- обновление


def test_patch_moves_card_between_columns(client):
    card = make_card(client, title="переезд")
    r = client.patch(f"{API}/cards/{card['id']}", json={"status": "in_progress"})
    assert r.status_code == 200 and r.json()["status"] == "in_progress"


def test_patch_is_partial(client):
    card = make_card(client, title="исходный", description="описание", priority="high")
    updated = client.patch(f"{API}/cards/{card['id']}", json={"title": "новый"}).json()
    assert updated["title"] == "новый"
    assert updated["description"] == "описание"   # не затёрлось
    assert updated["priority"] == "high"


def test_patch_touches_updated_at(client):
    card = make_card(client, title="время")
    updated = client.patch(f"{API}/cards/{card['id']}", json={"status": "done"}).json()
    assert updated["updated_at"] >= card["updated_at"]
    assert updated["created_at"] == card["created_at"]


@pytest.mark.parametrize(
    "payload",
    [{"status": "nope"}, {"priority": "urgent"}, {"title": ""}, {"title": "x" * 201}],
)
def test_patch_validation_422(client, payload):
    card = make_card(client, title="валидация")
    assert client.patch(f"{API}/cards/{card['id']}", json=payload).status_code == 422


def test_patch_missing_card_404(client):
    assert client.patch(f"{API}/cards/4242", json={"status": "done"}).status_code == 404


def test_patch_empty_body_is_noop(client):
    card = make_card(client, title="без изменений")
    r = client.patch(f"{API}/cards/{card['id']}", json={})
    assert r.status_code == 200 and r.json()["title"] == "без изменений"


# ---------------------------------------------------------------- удаление


def test_delete_card_204_then_404(client):
    card = make_card(client, title="на удаление")
    assert client.delete(f"{API}/cards/{card['id']}").status_code == 204
    assert client.get(f"{API}/cards/{card['id']}").status_code == 404


def test_delete_missing_card_404(client):
    assert client.delete(f"{API}/cards/777").status_code == 404


def test_delete_card_cascades_comments(client):
    card = make_card(client, title="с комментариями")
    client.post(f"{API}/cards/{card['id']}/comments", json={"text": "привет"})
    client.delete(f"{API}/cards/{card['id']}")
    assert client.get(f"{API}/cards/{card['id']}/comments").status_code == 404


# ------------------------------------------------------------- комментарии


def test_add_and_list_comments_in_order(client):
    card = make_card(client, title="обсуждение")
    for text in ("первый", "второй", "третий"):
        r = client.post(f"{API}/cards/{card['id']}/comments", json={"text": text})
        assert r.status_code == 201
    listed = client.get(f"{API}/cards/{card['id']}/comments").json()
    assert [c["text"] for c in listed] == ["первый", "второй", "третий"]
    assert all(c["card_id"] == card["id"] for c in listed)


def test_comments_empty_for_new_card(client):
    card = make_card(client, title="тихая")
    assert client.get(f"{API}/cards/{card['id']}/comments").json() == []


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "   "}])
def test_comment_validation_422(client, payload):
    card = make_card(client, title="валидация комментария")
    assert client.post(f"{API}/cards/{card['id']}/comments", json=payload).status_code == 422


def test_comment_on_missing_card_404(client):
    assert client.post(f"{API}/cards/31337/comments", json={"text": "эй"}).status_code == 404
    assert client.get(f"{API}/cards/31337/comments").status_code == 404


# ------------------------------------------------------- хранение и сервис


def test_data_survives_restart(tmp_path):
    """Главное требование Issue: данные переживают рестарт процесса."""
    db = str(tmp_path / "persist.db")

    store1 = Storage(db)
    app.dependency_overrides[get_storage] = lambda: store1
    with TestClient(app) as c:
        card_id = c.post(f"{API}/cards", json={"title": "переживу рестарт"}).json()["id"]
        c.post(f"{API}/cards/{card_id}/comments", json={"text": "и я"})
    store1.close()

    store2 = Storage(db)  # новый процесс = новое подключение к тому же файлу
    app.dependency_overrides[get_storage] = lambda: store2
    with TestClient(app) as c:
        assert c.get(f"{API}/cards/{card_id}").json()["title"] == "переживу рестарт"
        assert [x["text"] for x in c.get(f"{API}/cards/{card_id}/comments").json()] == ["и я"]
    store2.close()
    app.dependency_overrides.clear()


def test_seed_runs_once_and_fills_board(tmp_path):
    store = Storage(str(tmp_path / "seed.db"))
    assert store.seed_if_empty() >= 5
    assert store.seed_if_empty() == 0          # повторно не сеет
    statuses = {c["status"] for c in store.list_cards()}
    assert len(statuses) >= 3                  # карточки в разных колонках
    store.close()


def test_health_endpoint(client):
    r = client.get(f"{API}/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ------------------------------------------------------- формат ошибок (контракт)


def test_422_detail_is_a_string(client):
    """Контракт: detail — строка. FastAPI по умолчанию отдаёт массив объектов.

    Регрессия, найденная Agent-2 при интеграции: клиенту приходилось разбирать
    два разных формата ошибок.
    """
    r = client.post(f"{API}/cards", json={"title": ""})
    assert r.status_code == 422
    payload = r.json()
    assert isinstance(payload["detail"], str), payload
    assert "title" in payload["detail"]


def test_422_keeps_machine_readable_errors(client):
    """Человеческая строка не должна стоить нам подробностей для отладки."""
    payload = client.post(f"{API}/cards", json={"title": "ok", "priority": "urgent"}).json()
    assert isinstance(payload["errors"], list) and payload["errors"]
    assert "priority" in payload["detail"]


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", f"{API}/cards", {"title": "x" * 201}),
        ("patch", f"{API}/cards/1", {"status": "archived"}),
        ("post", f"{API}/cards/1/comments", {"text": "  "}),
        ("get", f"{API}/cards?status=nope", None),
    ],
)
def test_all_422_paths_use_string_detail(client, method, path, body):
    make_card(client, title="цель для PATCH")
    r = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)


def test_404_detail_is_a_string_too(client):
    """404 уже был строкой — проверяем, что обработчик 422 его не сломал."""
    assert isinstance(client.get(f"{API}/cards/99999").json()["detail"], str)
