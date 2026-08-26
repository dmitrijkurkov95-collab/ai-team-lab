"""Строгий контракт: то, что docs/api-contract.md утверждает явно.

Падение любого теста здесь = расхождение реализации с контрактом = баг зоны server/.
Места, где контракт молчит, вынесены в test_edge_cases.py под маркером `gap`.
"""
from __future__ import annotations

import re

import pytest

from contract_checks import assert_404_string_detail, assert_422_shape

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

def test_создание_возвращает_карточку_с_дефолтами(client, created):
    r = client.post("/cards", json={"title": "минимальное тело"})
    assert r.status_code in (200, 201), "контракт: POST -> 201 (допустим 200)"
    body = r.json()
    created.append(body["id"])
    assert body["title"] == "минимальное тело"
    assert body["description"] == "", "дефолт description — пустая строка"
    assert body["priority"] == "normal", "дефолт priority — normal"
    assert body["status"] in STATUSES


def test_код_создания_карточки_по_контракту(client, created):
    """Раньше был в gap-слое: контракт молчал. После 8bf91f6 задан явно —
    `POST -> 201 (допустим 200)`. Переведён в строгий слой по ревью Agent-2.
    """
    r = client.post("/cards", json={"title": "код создания"})
    assert r.status_code in (200, 201), f"контракт: 201 или 200, получено {r.status_code}"
    created.append(r.json()["id"])


def test_код_создания_комментария_по_контракту(client, make_card):
    card = make_card()
    r = client.post(f"/cards/{card['id']}/comments", json={"text": "код создания"})
    assert r.status_code in (200, 201), f"контракт: 201 или 200, получено {r.status_code}"


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

def test_get_несуществующей_карточки_это_404_со_строкой(client):
    assert_404_string_detail(client.get("/cards/999999"), "GET /cards/999999")


def test_patch_несуществующей_карточки_это_404_со_строкой(client):
    assert_404_string_detail(
        client.patch("/cards/999999", json={"status": "done"}), "PATCH /cards/999999"
    )


def test_delete_несуществующей_карточки_это_404_со_строкой(client):
    assert_404_string_detail(client.delete("/cards/999999"), "DELETE /cards/999999")


def test_список_комментариев_несуществующей_карточки_это_404_со_строкой(client):
    assert_404_string_detail(
        client.get("/cards/999999/comments"), "GET /cards/999999/comments"
    )


def test_комментарий_к_несуществующей_карточке_это_404_со_строкой(client):
    assert_404_string_detail(
        client.post("/cards/999999/comments", json={"text": "ау"}),
        "POST /cards/999999/comments",
    )


# ---------- 422: форма тела по контракту ----------
#
# Добавлено по ревью Agent-2 в PR #10. Замечание было по делу: раньше 422
# проверялись только на КОД и только в edge_cases, поэтому сьют не поймал бы
# регресс формата ошибок, который команда только что чинила в PR #8.

def test_422_пустой_title_имеет_строковый_detail(client):
    assert_422_shape(client.post("/cards", json={"title": ""}), "POST /cards {title: ''}")


def test_422_длинный_title_имеет_строковый_detail(client):
    r = client.post("/cards", json={"title": "x" * 201})
    assert_422_shape(r, "POST /cards title>200")


def test_422_кривой_priority_в_post_имеет_строковый_detail(client):
    r = client.post("/cards", json={"title": "t", "priority": "critical"})
    assert_422_shape(r, "POST /cards priority=critical")


def test_422_кривой_status_в_patch_имеет_строковый_detail(client, make_card):
    card = make_card()
    r = client.patch(f"/cards/{card['id']}", json={"status": "archived"})
    assert_422_shape(r, "PATCH /cards/{id} status=archived")


def test_422_кривой_priority_в_patch_имеет_строковый_detail(client, make_card):
    card = make_card()
    r = client.patch(f"/cards/{card['id']}", json={"priority": "urgent"})
    assert_422_shape(r, "PATCH /cards/{id} priority=urgent")


def test_422_кривой_фильтр_статуса_имеет_строковый_detail(client):
    """Форма обязательна для любого 422; сам факт 422 здесь — gap (см. edge_cases)."""
    assert_422_shape(client.get("/cards", params={"status": "не_статус"}), "GET /cards?status=не_статус")


def test_422_не_число_в_пути_имеет_строковый_detail(client):
    assert_422_shape(client.get("/cards/abc"), "GET /cards/abc")


def test_422_пустой_комментарий_имеет_строковый_detail(client, make_card):
    card = make_card()
    r = client.post(f"/cards/{card['id']}/comments", json={"text": ""})
    assert_422_shape(r, "POST /cards/{id}/comments text=''")


def test_успешные_ответы_не_содержат_поля_ошибок(client, make_card):
    """Обратная проверка: форма ошибки не должна протекать в успешные ответы."""
    card = make_card()
    r = client.get(f"/cards/{card['id']}")
    assert "detail" not in r.json() and "errors" not in r.json()


# ---------- Параметры списка GET /cards (спринт 2, задача #14) ----------

def test_фильтр_по_priority(client, make_card):
    make_card(title="низкий", priority="low")
    make_card(title="высокий", priority="high")
    for p in ("low", "normal", "high"):
        got = client.get("/cards", params={"priority": p})
        assert got.status_code == 200
        assert all(c["priority"] == p for c in got.json()), f"в выдаче priority={p} чужие карточки"


def test_фильтры_status_и_priority_комбинируются(client, make_card):
    make_card(title="цель", priority="high")
    got = client.get("/cards", params={"status": "backlog", "priority": "high"})
    assert got.status_code == 200
    assert all(c["status"] == "backlog" and c["priority"] == "high" for c in got.json())


def test_фильтр_без_совпадений_это_200_и_пустой_массив(client):
    """Контракт: «фильтр ничего не нашёл -> 200 и [], это не ошибка»."""
    r = client.get("/cards", params={"priority": "low", "status": "done"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_limit_ограничивает_выдачу(client, make_card):
    for i in range(5):
        make_card(title=f"лимит {i}")
    r = client.get("/cards", params={"limit": 2})
    assert r.status_code == 200
    assert len(r.json()) == 2, "limit=2 должен вернуть ровно 2 карточки"


def test_limit_берёт_первые_по_возрастанию_id(client, make_card):
    """Контракт: «не больше N карточек, первые по возрастанию id»."""
    all_ids = [c["id"] for c in client.get("/cards").json()]
    limited = [c["id"] for c in client.get("/cards", params={"limit": 3}).json()]
    assert limited == all_ids[:3], "limit должен резать с начала, а не с конца"


def test_без_limit_возвращаются_все_карточки(client):
    """Контракт: дефолтного лимита нет — молчаливое обрезание запрещено."""
    r = client.get("/cards")
    total = r.headers.get("X-Total-Count")
    assert total is not None, "контракт требует заголовок X-Total-Count"
    assert len(r.json()) == int(total), "без limit выдача должна быть полной"


def test_x_total_count_не_учитывает_limit(client, make_card):
    """Контракт: X-Total-Count — сколько попадает под фильтр БЕЗ учёта limit."""
    for i in range(4):
        make_card(title=f"подсчёт {i}")
    full = int(client.get("/cards").headers["X-Total-Count"])
    r = client.get("/cards", params={"limit": 1})
    assert len(r.json()) == 1
    assert int(r.headers["X-Total-Count"]) == full, (
        "X-Total-Count не должен меняться от limit — иначе клиент не увидит, что выдача урезана"
    )


def test_x_total_count_учитывает_фильтр(client, make_card):
    make_card(title="только high", priority="high")
    high = client.get("/cards", params={"priority": "high"})
    assert int(high.headers["X-Total-Count"]) == len(high.json())


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_limit_вне_диапазона_это_422(client, bad):
    """Контракт: limit=0 и отрицательный -> 422."""
    r = client.get("/cards", params={"limit": bad})
    assert r.status_code == 422
    assert_422_shape(r, f"GET /cards?limit={bad}")


def test_limit_больше_потолка_это_422(client):
    """Контракт: limit>200 -> 422, а НЕ молчаливое обрезание."""
    r = client.get("/cards", params={"limit": 201})
    assert r.status_code == 422, "обрезание вместо 422 — клиент посчитает выдачу полной"
    assert_422_shape(r, "GET /cards?limit=201")


def test_limit_ровно_на_потолке_проходит(client):
    assert client.get("/cards", params={"limit": 200}).status_code == 200


@pytest.mark.parametrize("bad", ["abc", "", "1.5", "  "])
def test_limit_не_целое_это_422(client, bad):
    """Контракт: не число и пустой ?limit= -> 422."""
    r = client.get("/cards", params={"limit": bad})
    assert r.status_code == 422
    assert_422_shape(r, f"GET /cards?limit={bad!r}")


def test_невалидный_priority_в_фильтре_это_422(client):
    r = client.get("/cards", params={"priority": "critical"})
    assert r.status_code == 422
    assert_422_shape(r, "GET /cards?priority=critical")
