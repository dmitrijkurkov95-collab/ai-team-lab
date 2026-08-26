"""Строгий контракт: то, что docs/api-contract.md утверждает явно.

Падение любого теста здесь = расхождение реализации с контрактом = баг зоны server/.
Места, где контракт молчит, вынесены в test_edge_cases.py под маркером `gap`.
"""
from __future__ import annotations

import re

import pytest

STATUSES = {"backlog", "in_progress", "review", "done"}
PRIORITIES = {"low", "normal", "high"}
CARD_KEYS = {"id", "title", "description", "status", "priority", "created_at", "updated_at"}
# ISO 8601 UTC. Дробная секунда допустима стандартом, но пример в контракте без неё —
# это разбирается отдельно в тесте ниже, помеченном gap.
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


# ---------- форма сущности ----------

def test_карточка_имеет_ровно_поля_из_контракта(client, make_card):
    card = make_card()
    assert set(card) == CARD_KEYS, f"лишние/недостающие поля: {set(card) ^ CARD_KEYS}"


def test_типы_полей_карточки(client, make_card):
    card = make_card(description="описание", priority="high")
    assert isinstance(card["id"], int)
    assert isinstance(card["title"], str) and isinstance(card["description"], str)
    assert card["status"] in STATUSES
    assert card["priority"] in PRIORITIES


def test_метки_это_iso8601_utc(client, make_card):
    card = make_card()
    for field in ("created_at", "updated_at"):
        assert ISO_UTC.match(card[field]), f"{field}={card[field]!r} не ISO 8601 UTC"


# ---------- GET /cards ----------

def test_список_карточек_это_массив(client):
    r = client.get("/cards")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_список_отсортирован_по_возрастанию_id(client, make_card):
    make_card(title="первая")
    make_card(title="вторая")
    ids = [c["id"] for c in client.get("/cards").json()]
    assert ids == sorted(ids), "контракт: списки по возрастанию id"


@pytest.mark.parametrize("status", sorted(STATUSES))
def test_фильтр_по_статусу_отдаёт_только_его(client, status):
    got = client.get("/cards", params={"status": status})
    assert got.status_code == 200
    assert all(c["status"] == status for c in got.json())


def test_фильтр_показывает_карточку_в_её_колонке(client, make_card):
    card = make_card()
    client.patch(f"/cards/{card['id']}", json={"status": "done"})
    done = [c["id"] for c in client.get("/cards", params={"status": "done"}).json()]
    backlog = [c["id"] for c in client.get("/cards", params={"status": "backlog"}).json()]
    assert card["id"] in done, "после переноса карточка должна быть в фильтре done"
    assert card["id"] not in backlog, "и не должна оставаться в прежней колонке"


# ---------- POST /cards ----------

def test_создание_возвращает_карточку_с_дефолтами(client):
    r = client.post("/cards", json={"title": "минимальное тело"})
    assert r.status_code == 201, "201 — см. примечание про коды успеха в README"
    body = r.json()
    client.created.append(body["id"])  # type: ignore[attr-defined]
    assert body["title"] == "минимальное тело"
    assert body["description"] == "", "дефолт description — пустая строка"
    assert body["priority"] == "normal", "дефолт priority — normal"
    assert body["status"] in STATUSES


def test_созданная_карточка_видна_в_списке(client, make_card):
    card = make_card(title="видна в списке")
    assert card["id"] in [c["id"] for c in client.get("/cards").json()]


@pytest.mark.parametrize("priority", sorted(PRIORITIES))
def test_priority_принимается_всем_значениям_контракта(client, make_card, priority):
    assert make_card(priority=priority)["priority"] == priority


# ---------- GET /cards/{id} ----------

def test_чтение_одной_карточки_совпадает_со_списком(client, make_card):
    card = make_card()
    r = client.get(f"/cards/{card['id']}")
    assert r.status_code == 200
    assert r.json() == card


# ---------- PATCH /cards/{id} ----------

@pytest.mark.parametrize("status", sorted(STATUSES))
def test_смена_статуса_это_перемещение_по_доске(client, make_card, status):
    card = make_card()
    r = client.patch(f"/cards/{card['id']}", json={"status": status})
    assert r.status_code == 200
    assert r.json()["status"] == status


def test_частичное_обновление_не_затирает_остальные_поля(client, make_card):
    card = make_card(description="важное", priority="high")
    r = client.patch(f"/cards/{card['id']}", json={"title": "новый заголовок"})
    body = r.json()
    assert body["title"] == "новый заголовок"
    assert body["description"] == "важное", "PATCH частичный — description затёрся"
    assert body["priority"] == "high"


def test_updated_at_не_старше_created_at(client, make_card):
    """Сравниваем через >= намеренно: см. README, точность меток в контракте не задана."""
    card = make_card()
    client.patch(f"/cards/{card['id']}", json={"priority": "low"})
    fresh = client.get(f"/cards/{card['id']}").json()
    assert fresh["updated_at"] >= fresh["created_at"]


# ---------- DELETE /cards/{id} ----------

def test_удаление_возвращает_204(client, make_card):
    card = make_card()
    assert client.delete(f"/cards/{card['id']}").status_code == 204


def test_после_удаления_карточки_нет(client, make_card):
    card = make_card()
    client.delete(f"/cards/{card['id']}")
    assert client.get(f"/cards/{card['id']}").status_code == 404
    assert card["id"] not in [c["id"] for c in client.get("/cards").json()]


# ---------- комментарии ----------

def test_добавление_комментария(client, make_card):
    card = make_card()
    r = client.post(f"/cards/{card['id']}/comments", json={"text": "первый"})
    assert r.status_code == 201
    body = r.json()
    assert body["text"] == "первый"
    assert ISO_UTC.match(body["created_at"])


def test_комментарии_по_времени_создания(client, make_card):
    card = make_card()
    for i in range(3):
        client.post(f"/cards/{card['id']}/comments", json={"text": f"комментарий {i}"})
    texts = [c["text"] for c in client.get(f"/cards/{card['id']}/comments").json()]
    assert texts == [f"комментарий {i}" for i in range(3)], "контракт: по времени создания"


def test_комментарии_не_протекают_между_карточками(client, make_card):
    a, b = make_card(title="A"), make_card(title="B")
    client.post(f"/cards/{a['id']}/comments", json={"text": "только в A"})
    assert [c["text"] for c in client.get(f"/cards/{b['id']}/comments").json()] == []


def test_у_новой_карточки_комментариев_нет(client, make_card):
    card = make_card()
    r = client.get(f"/cards/{card['id']}/comments")
    assert r.status_code == 200 and r.json() == []


# ---------- 404 по контракту: {"detail": "..."} ----------

def _assert_404_with_string_detail(r, what):
    assert r.status_code == 404, f"{what} -> {r.status_code}, ждём 404"
    detail = r.json().get("detail")
    assert isinstance(detail, str), (
        f'контракт требует 404 {{"detail": "..."}} строкой, получено {detail!r} ({what})'
    )


def test_get_несуществующей_карточки_это_404_со_строкой(client):
    _assert_404_with_string_detail(client.get("/cards/999999"), "GET /cards/999999")


def test_patch_несуществующей_карточки_это_404_со_строкой(client):
    _assert_404_with_string_detail(
        client.patch("/cards/999999", json={"status": "done"}), "PATCH /cards/999999"
    )


def test_delete_несуществующей_карточки_это_404_со_строкой(client):
    _assert_404_with_string_detail(client.delete("/cards/999999"), "DELETE /cards/999999")


def test_список_комментариев_несуществующей_карточки_это_404_со_строкой(client):
    _assert_404_with_string_detail(
        client.get("/cards/999999/comments"), "GET /cards/999999/comments"
    )


def test_комментарий_к_несуществующей_карточке_это_404_со_строкой(client):
    _assert_404_with_string_detail(
        client.post("/cards/999999/comments", json={"text": "ау"}),
        "POST /cards/999999/comments",
    )
