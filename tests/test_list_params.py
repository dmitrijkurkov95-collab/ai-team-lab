"""Контракт-тесты параметров `GET /cards`: `?q=`, `?offset=`, `?sort=`, `?order=`.

Закрывают дыру покрытия спринта 3: пагинация (#27), поиск (#30), дедлайны и
сортировка (#31) существовали только в unit-тестах автора реализации, то есть
проверялись тем же кодом, что и писался. Здесь они проверяются по тексту
`docs/api-contract.md`, разделы «Параметры списка», «Постраничная навигация»,
«Дедлайны и сортировка», «Поиск».

Карта дыр — аудит Agent-3/Agent-2 в #29.

Тесты не разрушают доску: работают на своих карточках с уникальным маркером
в заголовке и убирают их за собой через фикстуру `make_card`.
"""
from __future__ import annotations

import uuid

import pytest

from contract_checks import assert_422_shape


@pytest.fixture()
def marker() -> str:
    """Уникальная метка, чтобы искать только свои карточки на общей доске."""
    return f"zz{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------- поиск ?q=


def test_поиск_находит_по_заголовку(client, make_card, marker):
    make_card(title=f"карточка {marker} про дизайн")
    r = client.get("/cards", params={"q": marker})
    assert r.status_code == 200
    assert [c["title"] for c in r.json()] and all(marker in c["title"] for c in r.json())


def test_поиск_находит_по_описанию(client, make_card, marker):
    """Контракт: подстрока в `title` ИЛИ `description`."""
    make_card(title="карточка без метки в заголовке", description=f"описание {marker}")
    found = client.get("/cards", params={"q": marker}).json()
    assert len(found) == 1, "поиск обязан смотреть и в description"
    assert marker in found[0]["description"]


def test_поиск_без_учёта_регистра(client, make_card, marker):
    make_card(title=f"ДИЗАЙН {marker}")
    lower = client.get("/cards", params={"q": f"дизайн {marker}".lower()}).json()
    upper = client.get("/cards", params={"q": f"ДИЗАЙН {marker}".upper()}).json()
    assert len(lower) == 1 and len(upper) == 1, "регистр не должен влиять на поиск"


def test_поиск_кириллицы_без_учёта_регистра(client, make_card, marker):
    """Контракт оговаривает кириллицу отдельно: LIKE в SQLite сам её не приводит."""
    make_card(title=f"Карточка {marker}")
    assert len(client.get("/cards", params={"q": f"карточка {marker}"}).json()) == 1
    assert len(client.get("/cards", params={"q": f"КАРТОЧКА {marker}"}).json()) == 1


def test_поиск_это_подстрока_а_не_префикс(client, make_card, marker):
    make_card(title=f"Сверстать колонки {marker}")
    assert len(client.get("/cards", params={"q": f"колонк"}).json()) >= 1


def test_поиск_обрезает_пробелы_по_краям(client, make_card, marker):
    make_card(title=f"дизайн {marker}")
    plain = client.get("/cards", params={"q": marker}).json()
    padded = client.get("/cards", params={"q": f"  {marker}  "}).json()
    assert [c["id"] for c in plain] == [c["id"] for c in padded]


def test_поиск_проценты_и_подчёркивания_буквальные(client, make_card, marker):
    """Контракт: `%` и `_` — символы, а не маски LIKE."""
    make_card(title=f"скидка 100% {marker}")
    make_card(title=f"обычная карточка {marker}")
    found = client.get("/cards", params={"q": "100%"}).json()
    assert len(found) == 1, "`%` не должен работать как маска «что угодно»"
    assert "100%" in found[0]["title"]


def test_поиск_ничего_не_нашёл_это_не_ошибка(client):
    r = client.get("/cards", params={"q": "нетакогозаголовкаточно12345"})
    assert r.status_code == 200 and r.json() == []


@pytest.mark.parametrize("bad", ["", " ", "   ", "\t"])
def test_поиск_пустого_запроса_отклоняется(client, bad):
    """Контракт: пустой `q` → `422`, а не «фильтр, который молча не фильтрует»."""
    assert_422_shape(client.get("/cards", params={"q": bad}), f"?q={bad!r}")


def test_поиск_длиннее_200_символов_отклоняется(client):
    assert_422_shape(client.get("/cards", params={"q": "я" * 201}), "?q= 201 символ")


def test_поиск_ровно_200_символов_допустим(client):
    """Граница включительная — столько же, сколько максимальный title."""
    r = client.get("/cards", params={"q": "я" * 200})
    assert r.status_code == 200


def test_total_count_учитывает_поиск(client, make_card, marker):
    for i in range(3):
        make_card(title=f"{marker} карточка {i}")
    r = client.get("/cards", params={"q": marker})
    assert r.headers.get("X-Total-Count") == "3"


# ---------------------------------------------------------- пагинация ?offset=


@pytest.fixture()
def three_cards(make_card, marker):
    """Три карточки с общим маркером, по возрастанию id."""
    return [make_card(title=f"{marker} {i}") for i in range(3)]


def test_страницы_не_пересекаются(client, three_cards, marker):
    p1 = client.get("/cards", params={"q": marker, "limit": 2, "offset": 0}).json()
    p2 = client.get("/cards", params={"q": marker, "limit": 2, "offset": 2}).json()
    ids1, ids2 = [c["id"] for c in p1], [c["id"] for c in p2]
    assert len(ids1) == 2 and len(ids2) == 1
    assert not set(ids1) & set(ids2), "страницы не должны пересекаться"


def test_offset_ноль_эквивалентен_запросу_без_параметра(client, three_cards, marker):
    a = client.get("/cards", params={"q": marker}).json()
    b = client.get("/cards", params={"q": marker, "offset": 0}).json()
    assert [c["id"] for c in a] == [c["id"] for c in b]


def test_offset_без_limit_отдаёт_хвост(client, three_cards, marker):
    tail = client.get("/cards", params={"q": marker, "offset": 1}).json()
    assert len(tail) == 2, "offset без limit — все карточки, начиная с N-й"


def test_offset_за_пределами_выборки_пустой_список(client, three_cards, marker):
    """Контракт: не `404` — хвост, из которого карточки удалили, не ошибка клиента."""
    r = client.get("/cards", params={"q": marker, "offset": 9999})
    assert r.status_code == 200 and r.json() == []


def test_у_offset_нет_потолка(client):
    """В отличие от limit: большой offset не раздувает ответ, ограничивать нечего."""
    r = client.get("/cards", params={"offset": 10_000_000})
    assert r.status_code == 200, "у offset нет верхней границы по контракту"


@pytest.mark.parametrize("bad", ["-1", "abc", "", "1.5"])
def test_некорректный_offset_отклоняется(client, bad):
    assert_422_shape(client.get("/cards", params={"offset": bad}), f"?offset={bad!r}")


def test_total_count_не_зависит_от_offset(client, three_cards, marker):
    """Контракт: `X-Total-Count` — размер выборки под фильтром, якорь для страниц."""
    full = client.get("/cards", params={"q": marker})
    shifted = client.get("/cards", params={"q": marker, "offset": 2, "limit": 1})
    assert full.headers["X-Total-Count"] == shifted.headers["X-Total-Count"] == "3"


@pytest.mark.parametrize("bad", ["0", "-5", "201", "abc", ""])
def test_некорректный_limit_отклоняется(client, bad):
    """Потолок 200 отдаёт 422, а не молчаливое обрезание."""
    assert_422_shape(client.get("/cards", params={"limit": bad}), f"?limit={bad!r}")


def test_limit_ровно_200_допустим(client):
    assert client.get("/cards", params={"limit": 200}).status_code == 200


# ------------------------------------------------------ сортировка ?sort=


def test_сортировка_по_приоритету_по_смыслу_а_не_по_алфавиту(client, make_card, marker):
    """Контракт: `high > normal > low`. По алфавиту вышло бы high, low, normal."""
    make_card(title=f"{marker} l", priority="low")
    make_card(title=f"{marker} h", priority="high")
    make_card(title=f"{marker} n", priority="normal")
    got = [c["priority"] for c in
           client.get("/cards", params={"q": marker, "sort": "priority", "order": "desc"}).json()]
    assert got == ["high", "normal", "low"], f"порядок по смыслу нарушен: {got}"


def test_сортировка_по_созданию(client, three_cards, marker):
    asc = [c["id"] for c in
           client.get("/cards", params={"q": marker, "sort": "created", "order": "asc"}).json()]
    desc = [c["id"] for c in
            client.get("/cards", params={"q": marker, "sort": "created", "order": "desc"}).json()]
    assert asc == sorted(asc)
    assert desc == list(reversed(asc)), "desc должен быть строгим разворотом asc"


def test_тайбрейк_по_id_при_совпадении_ключа(client, three_cards, marker):
    """Метки округлены до секунды: у пачки карточек ключ совпадает.

    Без тай-брейка по id страницы пагинации поехали бы (урок #11/#27).
    """
    ids = [c["id"] for c in
           client.get("/cards", params={"q": marker, "sort": "created", "order": "asc"}).json()]
    assert ids == sorted(ids), f"при равных метках порядок должен быть по id: {ids}"


def test_дефолт_без_sort_это_id_по_возрастанию(client, three_cards, marker):
    ids = [c["id"] for c in client.get("/cards", params={"q": marker}).json()]
    assert ids == sorted(ids)


@pytest.mark.parametrize("param,bad", [("sort", "title"), ("sort", "id"), ("order", "bogus")])
def test_недопустимые_значения_сортировки_отклоняются(client, param, bad):
    """Белый список: имя поля из запроса не должно доезжать до SQL."""
    assert_422_shape(client.get("/cards", params={param: bad}), f"?{param}={bad}")


# ------------------------------------------------------------- дедлайны


def test_дедлайн_приводится_к_utc(client, make_card):
    """Контракт: ISO 8601 со смещением принимается, в базе — UTC."""
    card = make_card(title="со сроком", deadline="2026-09-01T21:00:00+03:00")
    assert card["deadline"].endswith("Z"), f"ожидали UTC с Z, получили {card['deadline']!r}"
    assert card["deadline"].startswith("2026-09-01T18:00"), card["deadline"]


def test_дедлайн_без_зоны_трактуется_как_utc(client, make_card):
    card = make_card(title="без зоны", deadline="2026-09-01T12:00:00")
    assert card["deadline"].startswith("2026-09-01T12:00"), card["deadline"]


def test_дедлайн_в_прошлом_разрешён(client, make_card):
    """Просроченная задача — нормальное состояние, а не ошибка ввода."""
    card = make_card(title="просрочка", deadline="2020-01-01T00:00:00Z")
    assert card["deadline"].startswith("2020-01-01")


@pytest.mark.parametrize("bad", ["вчера", "", "2026-13-45T99:00:00Z", 12345])
def test_кривой_дедлайн_отклоняется(client, bad):
    assert_422_shape(client.post("/cards", json={"title": "x", "deadline": bad}), f"deadline={bad!r}")


def test_снятие_дедлайна_через_null(client, make_card):
    """Контракт: `PATCH {"deadline": null}` — снять срок, это не «поле не передано»."""
    card = make_card(title="снимем срок", deadline="2026-09-01T00:00:00Z")
    r = client.patch(f"/cards/{card['id']}", json={"deadline": None})
    assert r.status_code == 200 and r.json()["deadline"] is None


def test_непереданный_дедлайн_сохраняется(client, make_card):
    card = make_card(title="срок остаётся", deadline="2026-09-01T00:00:00Z")
    r = client.patch(f"/cards/{card['id']}", json={"title": "новый заголовок"})
    assert r.json()["deadline"] == card["deadline"], "PATCH без поля не должен стирать срок"


def test_карточки_без_дедлайна_всегда_в_конце(client, make_card, marker):
    """Контракт: «срока нет» ≠ «срок бесконечно ранний» — хвост в конце при любом order."""
    make_card(title=f"{marker} без срока")
    make_card(title=f"{marker} со сроком", deadline="2026-09-01T00:00:00Z")
    for order in ("asc", "desc"):
        got = client.get("/cards", params={"q": marker, "sort": "deadline", "order": order}).json()
        assert got[-1]["deadline"] is None, f"при order={order} пустой срок должен быть в конце"


def test_сервер_не_вычисляет_просрочку(client, make_card):
    """Контракт: сервер отдаёт факт (deadline), а не мнение (is_overdue)."""
    card = make_card(title="просрочено", deadline="2020-01-01T00:00:00Z")
    assert "is_overdue" not in card, "флаг просрочки — зона клиента, он протухает в момент ответа"


# --------------------------------------------------------- комбинации


def test_параметры_комбинируются(client, make_card, marker):
    make_card(title=f"{marker} a", priority="high", status="done")
    make_card(title=f"{marker} b", priority="high")
    make_card(title=f"{marker} c", priority="low")
    r = client.get("/cards", params={
        "q": marker, "priority": "high", "sort": "created", "order": "desc", "limit": 1,
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1 and body[0]["priority"] == "high"
    assert r.headers["X-Total-Count"] == "2", "счёт до limit, но с учётом фильтров"
