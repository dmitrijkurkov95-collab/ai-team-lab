"""Контракт-тесты экспорта и импорта (аудит #29, самый рискованный сценарий спринта 3).

Почему этот файл появился первым из всего аудита: импорт — единственная операция,
которая **пишет оптом**. Ошибка в CRUD портит одну карточку, ошибка в импорте
портит всю доску. #54 подтвердил это практикой: `POST /import` без авторизации
стирал доску.

Тесты разрушающие по природе (replace чистит базу), поэтому каждый работает
через фикстуру `board`, которая снимает состояние и возвращает его обратно.
"""
from __future__ import annotations

import pytest

from contract_checks import assert_422_shape


@pytest.fixture()
def board(client):
    """Приводит доску к известному состоянию, а после теста возвращает как было.

    Две вещи, которые здесь важны:

    1. `replace` вычищает базу, поэтому без восстановления тесты влияют друг
       на друга. Восстанавливаем через `replace`, а не по одной карточке: так
       сохраняются id и следующий тест видит ту же доску, а не перенумерованную.
    2. Состояние **не** должно предполагаться: если доску стёрли (своими же
       пробами или соседним тестом), тесты про merge падали на пустой базе.
       Поэтому базовую карточку создаём сами — тест не зависит от того,
       что лежало в базе до него.
    """
    snapshot = client.get("/export").json()
    if not snapshot["cards"]:
        client.post("/cards", json={"title": "базовая карточка для тестов"})
        snapshot = client.get("/export").json()
    yield client
    client.post("/import", params={"mode": "replace"}, json=snapshot)


def _card(title, **kw):
    return {"title": title, "description": "", "status": "backlog",
            "priority": "normal", **kw}


# ---------- GET /export ----------

def test_экспорт_имеет_обязательные_поля_контракта(board):
    d = board.get("/export")
    assert d.status_code == 200
    body = d.json()
    assert set(body) == {"format_version", "exported_at", "cards"}
    assert body["format_version"] == 1
    assert isinstance(body["cards"], list)


def test_экспорт_вкладывает_комментарии_в_карточку(board):
    card = board.post("/cards", json={"title": "экспорт с комментом"}).json()
    board.post(f"/cards/{card['id']}/comments", json={"text": "виден в экспорте"})
    exported = board.get("/export").json()
    found = next(c for c in exported["cards"] if c["id"] == card["id"])
    assert "comments" in found, "контракт: комментарии вложены в карточку"
    assert [c["text"] for c in found["comments"]] == ["виден в экспорте"]


def test_экспорт_не_файл_а_обычное_тело(board):
    """Контракт прямо: не Content-Disposition, а JSON в теле."""
    r = board.get("/export")
    assert "content-disposition" not in {k.lower() for k in r.headers}
    assert "application/json" in r.headers.get("content-type", "")


# ---------- POST /import: обязательность mode ----------

def test_импорт_без_mode_это_422(board):
    """Контракт: mode обязателен, дефолта нет. Стирание доски не должно быть
    тем, что происходит, когда параметр забыли."""
    r = board.post("/import", json={"format_version": 1, "cards": []})
    assert r.status_code == 422
    assert_422_shape(r, "POST /import без mode")


@pytest.mark.parametrize("mode", ["bogus", "", "REPLACE", "Replace"])
def test_неизвестный_mode_это_422(board, mode):
    r = board.post("/import", params={"mode": mode}, json={"format_version": 1, "cards": []})
    assert r.status_code == 422
    assert_422_shape(r, f"POST /import?mode={mode!r}")


# ---------- обязательные поля тела ----------

def test_пустое_тело_это_422_а_не_очистка_доски(board):
    """Самый дорогой сценарий: {} не должен трактоваться как «заменить на пустое»."""
    before = len(board.get("/cards").json())
    r = board.post("/import", params={"mode": "replace"}, json={})
    assert r.status_code == 422
    assert_422_shape(r, "POST /import {}")
    assert len(board.get("/cards").json()) == before, "{} не должен трогать доску"


def test_без_format_version_это_422(board):
    r = board.post("/import", params={"mode": "replace"}, json={"cards": []})
    assert r.status_code == 422
    assert_422_shape(r, "импорт без format_version")


def test_без_cards_это_422(board):
    r = board.post("/import", params={"mode": "replace"}, json={"format_version": 1})
    assert r.status_code == 422
    assert_422_shape(r, "импорт без cards")


def test_неизвестная_версия_формата_это_422(board):
    """Контракт: неизвестная версия -> 422, а не попытка угадать структуру."""
    r = board.post("/import", params={"mode": "replace"},
                   json={"format_version": 99, "cards": []})
    assert r.status_code == 422
    assert_422_shape(r, "format_version=99")


@pytest.mark.parametrize("cards", ["строка", 42, {"a": 1}, None])
def test_cards_не_массив_это_422(board, cards):
    r = board.post("/import", params={"mode": "replace"},
                   json={"format_version": 1, "cards": cards})
    assert r.status_code == 422
    assert_422_shape(r, f"cards={cards!r}")


# ---------- режимы ----------

def test_replace_сохраняет_id_из_файла(board):
    doc = {"format_version": 1, "cards": [_card("из файла", id=777)]}
    r = board.post("/import", params={"mode": "replace"}, json=doc)
    assert r.status_code == 200
    ids = [c["id"] for c in board.get("/cards").json()]
    assert ids == [777], f"replace обязан сохранить id, получено {ids}"


def test_merge_выдаёт_новые_id(board):
    existing = board.get("/cards").json()
    assert existing, "для merge нужна непустая доска"
    clash = existing[0]["id"]
    doc = {"format_version": 1, "cards": [_card("дозаписанная", id=clash)]}
    r = board.post("/import", params={"mode": "merge"}, json=doc)
    assert r.status_code == 200
    ids = [c["id"] for c in board.get("/cards").json()]
    assert len(ids) == len(existing) + 1, "merge дописывает, а не заменяет"
    new = [c for c in board.get("/cards").json() if c["title"] == "дозаписанная"]
    assert new and new[0]["id"] != clash, "merge не должен молча перезаписать чужую карточку"


def test_merge_не_трогает_существующие_карточки(board):
    before = {c["id"]: c["title"] for c in board.get("/cards").json()}
    board.post("/import", params={"mode": "merge"},
               json={"format_version": 1, "cards": [_card("новая")]})
    after = {c["id"]: c["title"] for c in board.get("/cards").json()}
    for cid, title in before.items():
        assert after.get(cid) == title, f"merge изменил существующую карточку {cid}"


def test_явный_пустой_cards_в_replace_очищает_доску(board):
    """Контракт: это осознанная команда, а не молчание."""
    r = board.post("/import", params={"mode": "replace"},
                   json={"format_version": 1, "cards": []})
    assert r.status_code == 200
    assert board.get("/cards").json() == []


def test_после_replace_счётчик_id_не_сталкивается_с_импортированным(board):
    board.post("/import", params={"mode": "replace"},
               json={"format_version": 1, "cards": [_card("с большим id", id=5000)]})
    created = board.post("/cards", json={"title": "следом"})
    assert created.status_code in (200, 201)
    assert created.json()["id"] > 5000, "новая карточка не должна collide с импортированной"


# ---------- атомарность ----------

def test_импорт_атомарен_ошибка_в_одной_карточке_отменяет_всё(board):
    before = board.get("/export").json()
    doc = {"format_version": 1, "cards": [
        _card("хорошая"),
        _card(""),            # пустой title — невалидно
        _card("тоже хорошая"),
    ]}
    r = board.post("/import", params={"mode": "replace"}, json=doc)
    assert r.status_code == 422
    assert_422_shape(r, "импорт с битой карточкой")
    after = board.get("/export").json()
    assert [c["id"] for c in after["cards"]] == [c["id"] for c in before["cards"]], (
        "половина импортированной доски не должна появляться"
    )


@pytest.mark.xfail(
    reason="НАЙДЕННЫЙ БАГ (issue #57): код 422 верный, но тело не по контракту. "
           "Контракт требует 422 -> {\"detail\": \"<строка>\", \"errors\": [...]}, "
           "а ручные проверки в импорте отдают только detail, без errors. "
           "Так ведут себя все 422, поднятые вручную, а не Pydantic'ом. "
           "Как починят — xfail снимется сам (XPASS).",
    strict=False,
)
def test_дубликаты_id_внутри_файла_это_422(board):
    doc = {"format_version": 1, "cards": [_card("первая", id=42), _card("вторая", id=42)]}
    r = board.post("/import", params={"mode": "replace"}, json=doc)
    assert r.status_code == 422
    assert_422_shape(r, "дубликаты id в файле")


def test_импорт_не_обходная_дверь_в_базу(board):
    """Валидация та же, что у POST /cards."""
    for bad in (
        _card("кривой статус", status="archived"),
        _card("кривой приоритет", priority="critical"),
        _card("x" * 201),
    ):
        r = board.post("/import", params={"mode": "replace"},
                       json={"format_version": 1, "cards": [bad]})
        assert r.status_code == 422, f"импорт пропустил невалидную карточку: {bad}"
        assert_422_shape(r, f"импорт {bad.get('title')!r}")


def test_карточка_без_id_получает_свой(board):
    r = board.post("/import", params={"mode": "replace"},
                   json={"format_version": 1, "cards": [_card("без id")]})
    assert r.status_code == 200
    cards = board.get("/cards").json()
    assert len(cards) == 1 and isinstance(cards[0]["id"], int)


def test_метки_из_файла_сохраняются(board):
    doc = {"format_version": 1, "cards": [
        _card("со своими метками", created_at="2020-01-01T00:00:00Z",
              updated_at="2020-01-02T00:00:00Z")]}
    board.post("/import", params={"mode": "replace"}, json=doc)
    card = board.get("/cards").json()[0]
    assert card["created_at"] == "2020-01-01T00:00:00Z"
    assert card["updated_at"] == "2020-01-02T00:00:00Z"


# ---------- round-trip ----------

def test_round_trip_даёт_эквивалентную_доску(board):
    """Контракт гарантирует: export -> import -> export эквивалентно."""
    board.post("/cards", json={"title": "для round-trip", "priority": "high"})
    cards = board.get("/cards").json()
    board.post(f"/cards/{cards[0]['id']}/comments", json={"text": "и комментарий"})

    first = board.get("/export").json()
    r = board.post("/import", params={"mode": "replace"}, json=first)
    assert r.status_code == 200
    second = board.get("/export").json()

    def norm(doc):
        """Сравниваем без exported_at (меняется по определению) и без card_id
        в комментариях (при merge id карточек новые, это законно)."""
        out = []
        for c in doc["cards"]:
            item = {k: v for k, v in c.items() if k != "comments"}
            item["comments"] = [
                {k: v for k, v in cm.items() if k != "card_id"}
                for cm in c.get("comments", [])
            ]
            out.append(item)
        return sorted(out, key=lambda c: c["id"])

    assert norm(second) == norm(first), "round-trip изменил доску"
    assert second["format_version"] == first["format_version"]


def test_import_принимает_ровно_то_что_отдал_export(board):
    """Слабая версия round-trip: документ экспорта должен импортироваться без правок."""
    doc = board.get("/export").json()
    r = board.post("/import", params={"mode": "replace"}, json=doc)
    assert r.status_code == 200, f"экспорт не импортируется как есть: {r.text[:200]}"
