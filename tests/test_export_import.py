"""Контракт-тесты `GET /export` и `POST /import` вместе с защитами #54/#57.

Самая опасная ручка API: `mode=replace` стирает доску целиком. До этого файла
она проверялась только unit-тестами автора реализации — то есть тем же кодом,
что и писался. Здесь проверяется по тексту `docs/api-contract.md`, разделы
«Экспорт и импорт» и «Защита от разрушительных операций».

Разрушающие проверки (`mode=replace`, очистка доски) помечены `wipe` и по
умолчанию пропускаются: сьют может гоняться против общего стенда. Включаются
явно:

    KANBAN_ALLOW_WIPE=1 pytest tests/test_export_import.py

Неразрушающие проверки (валидация, `merge`, коды отказов) идут всегда: они не
меняют доску либо добавляют карточки, которые убираются в teardown.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

from contract_checks import assert_422_shape

CONFIRM = {"X-Confirm-Replace": "yes"}


@pytest.fixture()
def board(client):
    """Текущая доска целиком — эталон для round-trip и восстановления."""
    r = client.get("/export")
    assert r.status_code == 200, f"экспорт недоступен: {r.status_code}"
    return r.json()


@pytest.fixture()
def restore_board(client, board):
    """Возвращает доску в исходное состояние после разрушающего теста.

    Без неё упавший `wipe`-тест оставил бы стенд пустым для всех следующих.
    """
    yield board
    client.post("/import", params={"mode": "replace"}, headers=CONFIRM, json=board)


def minimal_doc(**overrides) -> dict:
    """Минимальный валидный документ импорта."""
    doc = {"format_version": 1, "cards": []}
    doc.update(overrides)
    return doc


# ------------------------------------------------------------ экспорт


def test_экспорт_отдаёт_обязательные_поля(board):
    assert board["format_version"] == 1
    assert isinstance(board["cards"], list)
    assert isinstance(board.get("exported_at"), str) and board["exported_at"].endswith("Z")


def test_экспорт_не_файл_а_обычное_тело(client):
    """Контракт: без Content-Disposition — так удобнее curl и фронту."""
    r = client.get("/export")
    assert "content-disposition" not in {k.lower() for k in r.headers}
    assert r.headers["content-type"].startswith("application/json")


def test_карточки_экспорта_содержат_комментарии(client, make_card):
    card = make_card(title="с комментарием")
    client.post(f"/cards/{card['id']}/comments", json={"text": "привет"})
    exported = client.get("/export").json()
    mine = next(c for c in exported["cards"] if c["id"] == card["id"])
    assert isinstance(mine["comments"], list) and mine["comments"][0]["text"] == "привет"
    assert "deadline" in mine, "контракт описывает deadline в карточке экспорта"


# ------------------------------------------- импорт: обязательные параметры


def test_импорт_без_mode_отклоняется(client):
    """Контракт: стирание доски не должно происходить, когда параметр забыли."""
    assert_422_shape(client.post("/import", json=minimal_doc()), "POST /import без ?mode=")


def test_импорт_с_неизвестным_mode_отклоняется(client):
    assert_422_shape(
        client.post("/import", params={"mode": "wipe"}, json=minimal_doc()),
        "?mode=wipe",
    )


@pytest.mark.parametrize("body", [
    {"cards": []},                       # нет format_version
    {"format_version": 1},               # нет cards
    {},                                  # пустое тело — не «заменить на пустую доску»
])
def test_импорт_без_обязательных_полей_отклоняется(client, body):
    """Найдено на ревью PR #45: `{}` не должно означать молчаливую потерю данных."""
    assert_422_shape(
        client.post("/import", params={"mode": "merge"}, json=body),
        f"тело {body}",
    )


@pytest.mark.parametrize("cards", ["строка", 42, {"a": 1}])
def test_cards_обязан_быть_массивом(client, cards):
    assert_422_shape(
        client.post("/import", params={"mode": "merge"}, json=minimal_doc(cards=cards)),
        f"cards={cards!r}",
    )


def test_неизвестная_версия_формата_отклоняется(client):
    """Контракт: «почти импортировалось» хуже честного отказа."""
    assert_422_shape(
        client.post("/import", params={"mode": "merge"}, json=minimal_doc(format_version=999)),
        "format_version=999",
    )


# ----------------------------------------------- импорт: валидация карточек


@pytest.mark.parametrize("bad_card,why", [
    ({"title": ""}, "пустой title"),
    ({"title": "   "}, "title из пробелов"),
    ({"title": "x" * 201}, "title длиннее 200"),
    ({"title": "ok", "status": "archived"}, "неизвестный status"),
    ({"title": "ok", "priority": "urgent"}, "неизвестный priority"),
    ({"title": "ok", "deadline": "вчера"}, "кривой deadline"),
])
def test_импорт_не_обходная_дверь_в_базу(client, bad_card, why):
    """Контракт: валидация та же, что у POST /cards."""
    assert_422_shape(
        client.post("/import", params={"mode": "merge"}, json=minimal_doc(cards=[bad_card])),
        f"импорт карточки: {why}",
    )


def test_дубликаты_id_в_файле_отклоняются_в_replace(client, board):
    """Контракт: дубликаты `id` внутри одного файла → `422`.

    Проверяем на `replace` без подтверждения было бы нельзя (там раньше
    сработает `428`), поэтому шлём с подтверждением, но заведомо кривой файл —
    он не должен доехать до записи, доска остаётся нетронутой.
    """
    cards = [{"id": 7, "title": "первая"}, {"id": 7, "title": "вторая"}]
    r = client.post("/import", params={"mode": "replace"}, headers=CONFIRM,
                    json=minimal_doc(cards=cards))
    assert r.status_code == 422, f"дубликаты id → {r.status_code}, ждём 422"
    assert isinstance(r.json().get("detail"), str)
    assert len(client.get("/cards").json()) == len(board["cards"]), (
        "отклонённый импорт не должен трогать доску"
    )


def test_форма_422_одинакова_для_ручных_проверок(client, board):
    """Контракт: `422` -> `{"detail": "<строка>", "errors": [...]}` — для любого источника.

    Раньше было расхождение (найдено этим сьютом, gap): `errors` добавлял
    только обработчик RequestValidationError, а отказы через HTTPException
    отдавали голый `detail`. Два формата под одним кодом — та же болезнь,
    что чинил PR #8, только наоборот.

    Закрыто в #62: ручные отказы бросаются через ContractError, который
    заполняет `errors`. Тест переехал из gap-слоя в строгий.
    """
    cards = [{"id": 7, "title": "a"}, {"id": 7, "title": "b"}]
    r = client.post("/import", params={"mode": "replace"}, headers=CONFIRM,
                    json=minimal_doc(cards=cards))
    assert_422_shape(r, "POST /import с дубликатами id")


def test_дубликаты_id_в_файле_отклоняются_в_merge(client, board):
    """Контракт: дубликаты `id` в файле -> `422`, независимо от режима.

    Раньше проверка стояла внутри ветки `replace`, и `merge` принимал кривой
    файл с `200` (найдено этим сьютом, gap). Практического ущерба не было —
    в merge id всё равно перевыдаются, — но клиент получал разный ответ на
    один и тот же испорченный документ в зависимости от режима.

    Закрыто в #62: проверка вынесена до ветвления по `mode`.
    """
    before = len(client.get("/cards").json())
    cards = [{"id": 7, "title": "дубль первый"}, {"id": 7, "title": "дубль второй"}]
    r = client.post("/import", params={"mode": "merge"}, json=minimal_doc(cards=cards))
    assert_422_shape(r, "POST /import?mode=merge с дубликатами id")
    assert len(client.get("/cards").json()) == before, (
        "отклонённый импорт не должен ничего добавлять на доску"
    )


def test_импорт_атомарен(client):
    """Ошибка в любой карточке → база остаётся ровно такой, какой была."""
    before = client.get("/cards").json()
    marker = f"zz{uuid.uuid4().hex[:8]}"
    cards = [{"title": f"валидная {marker}"}, {"title": ""}]
    r = client.post("/import", params={"mode": "merge"}, json=minimal_doc(cards=cards))
    assert r.status_code == 422
    after = client.get("/cards").json()
    assert [c["id"] for c in before] == [c["id"] for c in after], (
        "половина импортированной доски не должна появляться никогда"
    )
    assert not [c for c in after if marker in c["title"]]


# ----------------------------------------------------------- merge


def test_merge_дописывает_и_выдаёт_новые_id(client, created):
    """Контракт: в merge id из файла НЕ сохраняются."""
    before = {c["id"] for c in client.get("/cards").json()}
    marker = f"zz{uuid.uuid4().hex[:8]}"
    r = client.post("/import", params={"mode": "merge"},
                    json=minimal_doc(cards=[{"id": 999_001, "title": f"merge {marker}"}]))
    assert r.status_code == 200
    after = client.get("/cards").json()
    new = [c for c in after if c["id"] not in before]
    assert len(new) == 1
    created.append(new[0]["id"])  # уборка
    assert new[0]["id"] != 999_001, "merge обязан выдать свой id, а не взять из файла"


def test_merge_не_требует_подтверждения(client, created):
    """Контракт: merge ничего не теряет, трение там было бы без выигрыша."""
    marker = f"zz{uuid.uuid4().hex[:8]}"
    r = client.post("/import", params={"mode": "merge"},
                    json=minimal_doc(cards=[{"title": f"без хедера {marker}"}]))
    assert r.status_code == 200, "merge без X-Confirm-Replace обязан работать"
    for c in client.get("/cards", params={"q": marker}).json():
        created.append(c["id"])


def test_карточка_без_id_допустима(client, created):
    marker = f"zz{uuid.uuid4().hex[:8]}"
    r = client.post("/import", params={"mode": "merge"},
                    json=minimal_doc(cards=[{"title": f"без id {marker}"}]))
    assert r.status_code == 200
    for c in client.get("/cards", params={"q": marker}).json():
        created.append(c["id"])


# ------------------------------------------- защита replace (#54/#57)


def test_replace_без_подтверждения_отклоняется(client, board):
    """Контракт: 428 Precondition Required, и доска обязана остаться нетронутой."""
    r = client.post("/import", params={"mode": "replace"}, json=board)
    assert r.status_code == 428, f"replace без подтверждения → {r.status_code}, ждём 428"
    assert isinstance(r.json().get("detail"), str)
    assert len(client.get("/cards").json()) == len(board["cards"]), "доска не должна пострадать"


@pytest.mark.parametrize("value", ["no", "true", "1", "", "YES", "yes,yes"])
def test_подтверждение_сравнивается_точно(client, board, value):
    """Контракт: значение сравнивается точно — любое другое даёт тот же 428.

    Значения с пробелом по краям (`"yes "`) не проверяем: httpx отказывается
    отправлять такой заголовок на уровне протокола, до сервера он не дойдёт ни
    от одного нормального клиента. `"yes,yes"` проверяет ровно то же самое —
    что сравнение точное, а не «содержит yes».
    """
    r = client.post("/import", params={"mode": "replace"},
                    headers={"X-Confirm-Replace": value}, json=board)
    assert r.status_code == 428, f"X-Confirm-Replace: {value!r} → {r.status_code}, ждём 428"


@pytest.mark.wipe
def test_replace_с_подтверждением_заменяет_доску(client, restore_board):
    """Разрушающий: заменяем доску и восстанавливаем через фикстуру."""
    doc = minimal_doc(cards=[{"id": 4242, "title": "единственная после replace"}])
    r = client.post("/import", params={"mode": "replace"}, headers=CONFIRM, json=doc)
    assert r.status_code == 200
    cards = client.get("/cards").json()
    assert len(cards) == 1 and cards[0]["id"] == 4242, "в replace id из файла сохраняются"


@pytest.mark.wipe
def test_пустой_cards_в_replace_очищает_доску(client, restore_board):
    """Контракт: явный `"cards": []` — осознанная команда, а не молчание."""
    r = client.post("/import", params={"mode": "replace"}, headers=CONFIRM, json=minimal_doc())
    assert r.status_code == 200
    assert client.get("/cards").json() == []


@pytest.mark.wipe
def test_счётчик_id_продолжается_после_replace(client, restore_board):
    """Контракт: следующая созданная карточка не столкнётся с импортированной."""
    client.post("/import", params={"mode": "replace"}, headers=CONFIRM,
                json=minimal_doc(cards=[{"id": 5000, "title": "импортированная"}]))
    r = client.post("/cards", json={"title": "после импорта"})
    assert r.status_code == 201
    assert r.json()["id"] > 5000, f"id столкнулся с импортированным: {r.json()['id']}"


@pytest.mark.wipe
def test_round_trip_даёт_эквивалентную_доску(client, restore_board):
    """Контракт: export → import → export эквивалентны, меняется только exported_at."""
    before = client.get("/export").json()
    assert client.post("/import", params={"mode": "replace"},
                       headers=CONFIRM, json=before).status_code == 200
    after = client.get("/export").json()
    assert before["cards"] == after["cards"], "round-trip исказил доску"
    assert before["format_version"] == after["format_version"]


# ------------------------------------------------------ потолки импорта


def test_потолок_числа_карточек(client):
    """Контракт: больше 10 000 карточек → 413, граница включительная."""
    cards = [{"title": f"карточка {i}"} for i in range(10_001)]
    r = client.post("/import", params={"mode": "merge"}, json=minimal_doc(cards=cards))
    assert r.status_code == 413, f"10001 карточка → {r.status_code}, ждём 413"
    assert isinstance(r.json().get("detail"), str)


def test_потолок_размера_тела(client):
    """Контракт: тело больше 2 MB → 413."""
    big = "п" * 190  # длинный, но валидный title
    cards = [{"title": f"{big} {i}"} for i in range(9_000)]
    r = client.post("/import", params={"mode": "merge"}, json=minimal_doc(cards=cards))
    assert r.status_code == 413, f"тело >2MB → {r.status_code}, ждём 413"


# ------------------------------------- опциональная защита записи (токен)


@pytest.mark.skipif(
    not os.environ.get("KANBAN_WRITE_TOKEN"),
    reason="защита записи выключена: тест включается запуском сервера с KANBAN_WRITE_TOKEN",
)
class TestЗащитаЗаписи:
    """Контракт: при заданном KANBAN_WRITE_TOKEN мутации требуют X-Auth-Token.

    Класс пропускается, если переменная не задана: локальный стенд и демо не
    должны требовать настройки. В CI джоба может поднять сервер с токеном и
    получить эти проверки бесплатно.
    """

    @property
    def token(self) -> str:
        return os.environ["KANBAN_WRITE_TOKEN"]

    def test_чтение_остаётся_открытым(self, client):
        assert client.get("/cards").status_code == 200
        assert client.get("/export").status_code == 200

    @pytest.mark.parametrize("headers", [{}, {"X-Auth-Token": "неверный"}])
    def test_мутация_без_верного_токена_отклоняется(self, client, headers):
        r = client.post("/cards", json={"title": "без токена"}, headers=headers)
        assert r.status_code == 401, f"мутация с {headers} → {r.status_code}, ждём 401"

    def test_мутация_с_токеном_проходит(self, client, created):
        r = client.post("/cards", json={"title": "с токеном"},
                        headers={"X-Auth-Token": self.token})
        assert r.status_code == 201
        created.append(r.json()["id"])

    def test_токен_не_отменяет_подтверждения(self, client, board):
        """Контракт: подтверждение и токен независимы."""
        r = client.post("/import", params={"mode": "replace"},
                        headers={"X-Auth-Token": self.token}, json=board)
        assert r.status_code == 428, "верный токен не должен отменять X-Confirm-Replace"


# ------------------------------------------------------------ /health


@pytest.mark.gap
def test_health_отвечает_и_показывает_счёт(client):
    """`/health` в `docs/api-contract.md` НЕ описан — это gap, а не строгая норма.

    Ручка реально существует, ей пользуются `run.sh` (ожидание готовности),
    healthcheck в docker-compose и CI. То есть де-факто это часть интерфейса,
    на которую завязана инфраструктура, но контракт о ней молчит.

    Тест помечен `gap` сознательно: если он упадёт, это повод решить лидом —
    внести `/health` в контракт или убрать зависимость инфраструктуры от него.
    Молча закреплять недокументированное поведение как обязательное нельзя.
    """
    r = client.get("/health")
    assert r.status_code == 200, f"на /health завязаны run.sh и healthcheck: {r.status_code}"
    body = r.json()
    assert body.get("status") == "ok"
    assert isinstance(body.get("cards"), int), "счётчик карточек используется как признак живости"
