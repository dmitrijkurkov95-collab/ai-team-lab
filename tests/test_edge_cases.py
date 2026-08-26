"""Граничные случаи и места, где контракт молчит.

Два слоя, и путать их нельзя:

* **Без маркера** — контракт это требует прямо («пустой title или >200 -> 422»,
  «PATCH с невалидным status/priority -> 422»). Падение = баг server/.
* **`@pytest.mark.gap`** — контракт не определяет поведение. Тест фиксирует
  текущую трактовку, чтобы она не поехала молча. Падение gap-теста = нужен
  ответ лида в `docs/api-contract.md`, а не правка кода.

Список спорных мест продублирован в tests/README.md и в issue #2.
"""
from __future__ import annotations

import re

import pytest

# ---------- прямо из контракта: 422 ----------

def test_пустой_title_это_422(client):
    assert client.post("/cards", json={"title": ""}).status_code == 422


def test_title_длиннее_200_это_422(client):
    assert client.post("/cards", json={"title": "x" * 201}).status_code == 422


def test_title_ровно_200_символов_проходит(client):
    r = client.post("/cards", json={"title": "x" * 200})
    assert r.status_code == 201
    client.created.append(r.json()["id"])  # type: ignore[attr-defined]


def test_невалидный_priority_в_post_это_422(client):
    assert client.post("/cards", json={"title": "t", "priority": "critical"}).status_code == 422


def test_невалидный_status_в_patch_это_422(client, make_card):
    card = make_card()
    assert client.patch(f"/cards/{card['id']}", json={"status": "archived"}).status_code == 422


def test_невалидный_priority_в_patch_это_422(client, make_card):
    card = make_card()
    assert client.patch(f"/cards/{card['id']}", json={"priority": "urgent"}).status_code == 422


def test_валидные_значения_status_проходят_в_patch(client, make_card):
    card = make_card()
    for s in ("backlog", "in_progress", "review", "done"):
        assert client.patch(f"/cards/{card['id']}", json={"status": s}).status_code == 200, s


# ---------- GAP: контракт молчит ----------

@pytest.mark.gap
def test_gap_title_из_одних_пробелов_считается_пустым(client):
    """Контракт: «непустая строка». Является ли '   ' пустой — не определено.

    Зафиксировано: 422. Иначе на доске появляются невидимые карточки.
    """
    assert client.post("/cards", json={"title": "     "}).status_code == 422


@pytest.mark.gap
def test_gap_пробелы_по_краям_title_обрезаются(client):
    """Нормализация title контрактом не описана."""
    r = client.post("/cards", json={"title": "  с пробелами  "})
    assert r.status_code == 201
    client.created.append(r.json()["id"])  # type: ignore[attr-defined]
    assert r.json()["title"] == "с пробелами"


@pytest.mark.gap
def test_gap_коды_успеха_для_создания_201(client):
    """В контракте явно задан только 204 для DELETE. Коды создания не определены."""
    r = client.post("/cards", json={"title": "gap: код создания"})
    client.created.append(r.json()["id"])  # type: ignore[attr-defined]
    assert r.status_code == 201


@pytest.mark.gap
def test_gap_невалидный_фильтр_статуса_это_422(client):
    """Поведение при ?status=<мусор> в контракте не описано."""
    assert client.get("/cards", params={"status": "не_статус"}).status_code == 422


@pytest.mark.gap
def test_gap_не_число_в_пути_это_422_а_не_404(client):
    """/cards/abc — ошибка разбора параметра (422) или отсутствующая сущность (404)?

    Контракт не различает. Зафиксировано 422.
    """
    assert client.get("/cards/abc").status_code == 422


@pytest.mark.gap
def test_gap_пустой_текст_комментария_это_422(client, make_card):
    """Контракт описывает тело как {text}, но не говорит про пустой текст."""
    card = make_card()
    assert client.post(f"/cards/{card['id']}/comments", json={"text": ""}).status_code == 422
    assert client.post(f"/cards/{card['id']}/comments", json={"text": "   "}).status_code == 422


@pytest.mark.gap
def test_gap_пустое_тело_patch_не_ломает_карточку(client, make_card):
    """PATCH {} в контракте не описан. Ожидаем: карточка возвращается как есть."""
    card = make_card(description="d", priority="high")
    r = client.patch(f"/cards/{card['id']}", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["description"] == "d" and body["priority"] == "high"


@pytest.mark.gap
@pytest.mark.xfail(
    reason="Контракт показывает метку вида 2026-08-26T12:00:00Z (секунды), "
           "реализация в main отдаёт микросекунды. Расхождение с примером, "
           "но не с правилом «ISO 8601 UTC». Нужно решение лида.",
    strict=False,
)
def test_gap_формат_метки_точно_как_в_примере_контракта(client, make_card):
    card = make_card()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", card["created_at"])


# ---------- пустая доска (разрушающий тест, включается явно) ----------

@pytest.mark.wipe
def test_пустая_доска_отдаёт_пустой_массив(empty_board):
    assert empty_board.get("/cards").json() == []


@pytest.mark.wipe
def test_на_пустой_доске_фильтр_тоже_пуст(empty_board):
    assert empty_board.get("/cards", params={"status": "backlog"}).json() == []
