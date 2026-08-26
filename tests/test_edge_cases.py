"""Граничные случаи и трактовки неоднозначностей.

Два слоя, и путать их нельзя:

* **Без маркера** — контракт это требует прямо. Падение = баг server/.
* **`@pytest.mark.gap`** — контракт не определяет поведение. Тест фиксирует
  текущую трактовку, чтобы она не поехала молча. Падение gap-теста = нужен
  ответ лида в `docs/api-contract.md`, а не правка кода.

История этого файла: раньше здесь было семь gap-мест. Пять из них лид
arbitriровал в разделе «Трактовки неоднозначных случаев» (PR #13, по моим же
находкам), формат меток закрыл PR #11 — всё это переехало в строгий слой.
Остался один незакрытый gap, он помечен и про него написано в tests/README.md.

Про 422: **форма тела** (`{"detail": "<строка>", "errors": [...]}`) обязательна
для любого 422 и проверяется строго везде.
"""
from __future__ import annotations

import re

import pytest

from contract_checks import assert_422_shape

# ---------- невалидные значения: прямо из контракта ----------

@pytest.mark.parametrize("bad", ["critical", "urgent", "NORMAL", ""])
def test_невалидный_priority_в_post_это_422(client, bad):
    assert client.post("/cards", json={"title": "t", "priority": bad}).status_code == 422


# None здесь намеренно НЕТ: явный null в PATCH трактуется как «поле не прислали»
# и отдаёт 200. Это единственный оставшийся gap — см. ниже.
@pytest.mark.parametrize("bad", ["archived", "BACKLOG", "in-progress", ""])
def test_невалидный_status_в_patch_это_422(client, make_card, bad):
    card = make_card()
    assert client.patch(f"/cards/{card['id']}", json={"status": bad}).status_code == 422


def test_валидные_значения_status_проходят_в_patch(client, make_card):
    card = make_card()
    for s in ("backlog", "in_progress", "review", "done"):
        assert client.patch(f"/cards/{card['id']}", json={"status": s}).status_code == 200, s


def test_валидные_значения_priority_проходят_в_patch(client, make_card):
    card = make_card()
    for p in ("low", "normal", "high"):
        assert client.patch(f"/cards/{card['id']}", json={"priority": p}).status_code == 200, p


# ---------- трактовки, закреплённые лидом (PR #13) ----------
#
# Были gap-местами, теперь требования контракта, раздел «Трактовки
# неоднозначных случаев». Маркер gap снят намеренно: падение этих тестов
# больше не «нужно решение лида», а баг реализации.

def test_трактовка1_невалидный_фильтр_статуса_это_422(client):
    """Контракт, трактовка 1: невалидный ?status= -> 422 (не пустой список, не 500)."""
    assert_422_shape(client.get("/cards", params={"status": "не_статус"}), "GET /cards?status=мусор")


def test_трактовка2_title_из_пробелов_это_422(client):
    """Контракт, трактовка 2: title из одних пробелов -> 422."""
    r = client.post("/cards", json={"title": "     "})
    assert r.status_code == 422
    assert_422_shape(r, "POST /cards title='     '")


def test_трактовка2_валидный_title_обрезается_по_краям(client, created):
    """Контракт, трактовка 2: валидный title обрезается по краям."""
    r = client.post("/cards", json={"title": "  с пробелами  "})
    assert r.status_code in (200, 201)
    created.append(r.json()["id"])
    assert r.json()["title"] == "с пробелами"


def test_трактовка3_не_число_в_пути_это_422_а_не_404(client):
    """Контракт, трактовка 3: /cards/abc -> 422, а не 404."""
    r = client.get("/cards/abc")
    assert r.status_code == 422, f"получено {r.status_code}, контракт требует 422"
    assert_422_shape(r, "GET /cards/abc")


def test_трактовка4_пустой_текст_комментария_это_422(client, make_card):
    """Контракт, трактовка 4: пустой текст комментария -> 422."""
    card = make_card()
    r = client.post(f"/cards/{card['id']}/comments", json={"text": ""})
    assert r.status_code == 422
    assert_422_shape(r, "POST /cards/{id}/comments text=''")


def test_трактовка4_комментарий_из_пробелов_это_422(client, make_card):
    card = make_card()
    r = client.post(f"/cards/{card['id']}/comments", json={"text": "   "})
    assert r.status_code == 422
    assert_422_shape(r, "POST /cards/{id}/comments text='   '")


def test_трактовка5_пустой_patch_это_200_без_изменений(client, make_card):
    """Контракт, трактовка 5: PATCH {} -> 200, карточка без изменений (идемпотентность)."""
    card = make_card(description="d", priority="high")
    r = client.patch(f"/cards/{card['id']}", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["description"] == "d" and body["priority"] == "high"
    assert body["updated_at"] == card["updated_at"], "no-op не должен трогать updated_at"


# ---------- формат меток: закрыто PR #11 ----------

def test_формат_метки_точно_как_в_примере_контракта(client, make_card):
    """Был xfail, пока реализация отдавала микросекунды. PR #11 влит — проверяем строго.

    Секундная точность важна не только «как в примере»: с ней updated_at может
    совпасть с created_at при быстром PATCH, и это законно. Тесты на метки
    обязаны сравнивать через >=, а не через >.
    """
    card = make_card()
    for field in ("created_at", "updated_at"):
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", card[field]), (
            f"{field}={card[field]!r}: контракт показывает формат без дробной секунды"
        )


# ---------- ЕДИНСТВЕННЫЙ оставшийся gap ----------

@pytest.mark.gap
@pytest.mark.parametrize("field", ["status", "priority", "title", "description"])
def test_gap_явный_null_в_patch_игнорируется(client, make_card, field):
    """Явный `null` в теле PATCH трактуется как «поле не прислали»: 200, ничего не меняется.

    Единственное место, которое лид не закрыл в арбитраже (PR #13). Контракт
    требует 422 на невалидный `status`/`priority`, но не говорит, считается ли
    `null` невалидным значением или отсутствием поля. Обе трактовки законны
    (`null` = «не менять» — общепринятая конвенция частичного обновления).

    Практический смысл: клиент, приславший `null` по ошибке, получит `200`
    и молча ничего не изменит. Предложение формулировки в контракт — в issue #16.
    """
    card = make_card(description="d", priority="high")
    r = client.patch(f"/cards/{card['id']}", json={field: None})
    assert r.status_code == 200, f"{field}: null -> {r.status_code}, ждём 200 (игнор)"
    body = r.json()
    assert body["description"] == "d" and body["priority"] == "high", (
        f"{field}: null не должен менять остальные поля"
    )


# ---------- пустая доска (разрушающий тест, включается явно) ----------

@pytest.mark.wipe
def test_пустая_доска_отдаёт_пустой_массив(empty_board):
    assert empty_board.get("/cards").json() == []


@pytest.mark.wipe
def test_на_пустой_доске_фильтр_тоже_пуст(empty_board):
    """Контракт: «фильтр ничего не нашёл -> 200 и пустой массив, это не ошибка»."""
    r = empty_board.get("/cards", params={"status": "backlog"})
    assert r.status_code == 200 and r.json() == []
