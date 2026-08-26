"""Тесты сервера канбан-доски.

Это тесты Agent-1 на свою реализацию (контракт-сьют Agent-3 живёт в /tests).
Каждый тест — на своей временной БД, поэтому seed и порядок не влияют.
"""

from __future__ import annotations

import os
import re
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


# ----------------------------------------------------- формат меток (контракт)

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_card_timestamps_match_contract_example(client):
    """Контракт: «2026-08-26T12:00:00Z» — без дробной секунды.

    Расхождение нашёл Agent-3 контракт-сьютом (xfail в PR #10).
    """
    card = make_card(client, title="метки времени")
    for field in ("created_at", "updated_at"):
        assert TS_RE.match(card[field]), f"{field}={card[field]!r}"


def test_comment_timestamp_matches_contract(client):
    card = make_card(client, title="метка комментария")
    comment = client.post(f"{API}/cards/{card['id']}/comments", json={"text": "раз"}).json()
    assert TS_RE.match(comment["created_at"]), comment["created_at"]


def test_timestamps_stay_clean_after_patch(client):
    card = make_card(client, title="после патча")
    updated = client.patch(f"{API}/cards/{card['id']}", json={"status": "done"}).json()
    assert TS_RE.match(updated["updated_at"]) and TS_RE.match(updated["created_at"])


def test_legacy_rows_with_microseconds_are_normalised(tmp_path):
    """Старые базы хранят метки с микросекундами — на выдаче формат всё равно чистый.

    Мигрировать существующие БД ради формата вывода незачем, поэтому нормализация
    стоит на границе API.
    """
    db = str(tmp_path / "legacy.db")
    store = Storage(db)
    store._conn.execute(  # noqa: SLF001 — намеренно пишем «старый» формат
        "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("старая запись", "", "backlog", "normal",
         "2026-08-26T12:00:00.742000Z", "2026-08-26T12:30:05.123456Z"),
    )
    store._conn.commit()  # noqa: SLF001

    app.dependency_overrides[get_storage] = lambda: store
    with TestClient(app) as c:
        card = c.get(f"{API}/cards").json()[0]
    app.dependency_overrides.clear()
    store.close()

    assert card["created_at"] == "2026-08-26T12:00:00Z"
    assert card["updated_at"] == "2026-08-26T12:30:05Z"


# --------------------------------------------- фильтры и лимит списка (#14)


def test_filter_by_priority(client):
    make_card(client, title="низкий", priority="low")
    make_card(client, title="высокий-1", priority="high")
    make_card(client, title="высокий-2", priority="high")
    high = client.get(f"{API}/cards", params={"priority": "high"}).json()
    assert len(high) == 2 and {c["priority"] for c in high} == {"high"}


@pytest.mark.parametrize("priority", ["low", "normal", "high"])
def test_filter_accepts_every_contract_priority(client, priority):
    make_card(client, title=f"карточка {priority}", priority=priority)
    got = client.get(f"{API}/cards", params={"priority": priority}).json()
    assert all(c["priority"] == priority for c in got) and got


def test_filter_by_unknown_priority_422(client):
    assert client.get(f"{API}/cards", params={"priority": "urgent"}).status_code == 422


def test_status_and_priority_combine(client):
    make_card(client, title="цель", status="done", priority="high")
    make_card(client, title="не та колонка", status="backlog", priority="high")
    make_card(client, title="не тот приоритет", status="done", priority="low")
    got = client.get(f"{API}/cards", params={"status": "done", "priority": "high"}).json()
    assert [c["title"] for c in got] == ["цель"]


def test_empty_filter_result_is_200_and_empty_list(client):
    """Ничего не нашлось — это не ошибка."""
    make_card(client, title="одна", priority="low")
    r = client.get(f"{API}/cards", params={"priority": "high"})
    assert r.status_code == 200 and r.json() == []


def test_limit_returns_first_n_by_id(client):
    for i in range(5):
        make_card(client, title=f"карточка {i}")
    got = client.get(f"{API}/cards", params={"limit": 3}).json()
    ids = [c["id"] for c in got]
    assert len(got) == 3 and ids == sorted(ids)


def test_limit_larger_than_collection_is_fine(client):
    make_card(client, title="единственная")
    assert len(client.get(f"{API}/cards", params={"limit": 200}).json()) == 1


def test_limit_combines_with_filters(client):
    for i in range(4):
        make_card(client, title=f"высокий {i}", priority="high", status="done")
    make_card(client, title="лишняя", priority="low")
    got = client.get(
        f"{API}/cards", params={"priority": "high", "status": "done", "limit": 2}
    ).json()
    assert len(got) == 2 and all(c["priority"] == "high" for c in got)


@pytest.mark.parametrize("limit", [0, -1, 201, 100000, "abc", ""])
def test_invalid_limit_422(client, limit):
    """Потолок — 422, а не молчаливое обрезание: клиент должен знать про урезание."""
    assert client.get(f"{API}/cards", params={"limit": limit}).status_code == 422


def test_limit_boundaries_are_inclusive(client):
    make_card(client, title="границы")
    assert client.get(f"{API}/cards", params={"limit": 1}).status_code == 200
    assert client.get(f"{API}/cards", params={"limit": 200}).status_code == 200


def test_no_default_limit_returns_everything(client):
    """Без ?limit= отдаём всё: молчаливое обрезание доски — обман интерфейса."""
    for i in range(55):
        client.post(f"{API}/cards", json={"title": f"карточка {i}"})
    assert len(client.get(f"{API}/cards").json()) == 55


def test_total_count_header_reveals_truncation(client):
    """X-Total-Count показывает полный размер выборки, даже когда limit урезал ответ."""
    for i in range(10):
        make_card(client, title=f"карточка {i}", priority="high")
    r = client.get(f"{API}/cards", params={"limit": 3})
    assert len(r.json()) == 3
    assert r.headers["X-Total-Count"] == "10"


def test_total_count_respects_filters(client):
    make_card(client, title="высокая", priority="high")
    make_card(client, title="низкая-1", priority="low")
    make_card(client, title="низкая-2", priority="low")
    r = client.get(f"{API}/cards", params={"priority": "low"})
    assert r.headers["X-Total-Count"] == "2" and len(r.json()) == 2


def test_comments_order_stable_within_same_second(client):
    """Метки округлены до секунды — порядок держим по id, иначе список «поедет»."""
    card = make_card(client, title="порядок комментариев")
    texts = [f"комментарий {i}" for i in range(6)]
    for t in texts:
        client.post(f"{API}/cards/{card['id']}/comments", json={"text": t})
    listed = [c["text"] for c in client.get(f"{API}/cards/{card['id']}/comments").json()]
    assert listed == texts
