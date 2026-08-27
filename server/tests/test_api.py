"""Тесты сервера канбан-доски.

Это тесты Agent-1 на свою реализацию (контракт-сьют Agent-3 живёт в /tests).
Каждый тест — на своей временной БД, поэтому seed и порядок не влияют.
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app, get_storage  # noqa: E402
from app.storage import Storage  # noqa: E402

API = "/api/v1"

#: mode=replace стирает доску, поэтому требует подтверждения (#54).
CONFIRM = {"X-Confirm-Replace": "yes"}


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


def test_memory_db_is_rejected_with_clear_message(tmp_path):
    """:memory: несовместим с соединением-на-запрос — падаем внятно, а не 'no such table'."""
    with pytest.raises(ValueError, match="memory"):
        Storage(":memory:")


def test_health_endpoint(client):
    r = client.get(f"{API}/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ------------------------------------------------- параллельный доступ (#21)
#
# Дыра в покрытии, из-за которой баг дожил до спринта 2: все остальные тесты
# последовательные, а дефект проявляется только под конкурентной нагрузкой.
# На общем sqlite3.Connection с check_same_thread=False эти тесты падают
# с InterfaceError → 500.


def test_parallel_reads_never_return_5xx(client):
    """Доска грузит счётчики комментариев параллельно — это уронило сервер на стенде."""
    cards = [make_card(client, title=f"параллель {i}") for i in range(6)]
    for card in cards:
        client.post(f"{API}/cards/{card['id']}/comments", json={"text": "раз"})

    paths = [f"{API}/cards/{c['id']}/comments" for c in cards] * 15 + [f"{API}/cards"] * 15

    with ThreadPoolExecutor(max_workers=32) as pool:
        codes = list(pool.map(lambda p: client.get(p).status_code, paths))

    assert not [c for c in codes if c >= 500], f"5xx под параллельной нагрузкой: {Counter(codes)}"
    assert all(c == 200 for c in codes), Counter(codes)


def test_parallel_writes_all_persist(client):
    """Одновременные записи не должны терять данные и падать."""
    def create(i: int) -> int:
        return client.post(f"{API}/cards", json={"title": f"конкурент {i}"}).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(create, range(30)))

    assert set(codes) == {201}, Counter(codes)
    titles = {c["title"] for c in client.get(f"{API}/cards").json()}
    assert all(f"конкурент {i}" in titles for i in range(30)), "часть записей потерялась"


def test_parallel_mixed_load(client):
    """Смешанная нагрузка: чтения, записи и обновления одновременно."""
    card = make_card(client, title="под нагрузкой")

    def work(i: int) -> int:
        if i % 3 == 0:
            return client.get(f"{API}/cards").status_code
        if i % 3 == 1:
            return client.post(f"{API}/cards/{card['id']}/comments", json={"text": f"к{i}"}).status_code
        return client.patch(f"{API}/cards/{card['id']}", json={"status": "review"}).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(work, range(60)))

    assert not [c for c in codes if c >= 500], Counter(codes)


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
    with store._connect() as conn:  # noqa: SLF001 — намеренно пишем «старый» формат
        conn.execute(
            "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("старая запись", "", "backlog", "normal",
             "2026-08-26T12:00:00.742000Z", "2026-08-26T12:30:05.123456Z"),
        )

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


# ------------------------------------------------------------- пагинация (#27)


def test_offset_skips_first_cards(client):
    for i in range(5):
        make_card(client, title=f"карточка {i}")
    all_ids = [c["id"] for c in client.get(f"{API}/cards").json()]
    got = client.get(f"{API}/cards", params={"offset": 2}).json()
    assert [c["id"] for c in got] == all_ids[2:]


def test_offset_with_limit_gives_page(client):
    for i in range(10):
        make_card(client, title=f"карточка {i}")
    all_ids = [c["id"] for c in client.get(f"{API}/cards").json()]
    page2 = client.get(f"{API}/cards", params={"limit": 3, "offset": 3}).json()
    assert [c["id"] for c in page2] == all_ids[3:6]


def test_pages_cover_collection_without_gaps(client):
    """Страницы подряд дают ровно исходный список — без дыр и повторов."""
    for i in range(7):
        make_card(client, title=f"карточка {i}")
    expected = [c["id"] for c in client.get(f"{API}/cards").json()]
    collected: list[int] = []
    for offset in range(0, 9, 3):
        page = client.get(f"{API}/cards", params={"limit": 3, "offset": offset}).json()
        collected += [c["id"] for c in page]
    assert collected == expected


def test_offset_beyond_collection_is_200_and_empty(client):
    """Ушли за хвост — пустой список, а не 404: карточки могли удалить."""
    make_card(client, title="единственная")
    r = client.get(f"{API}/cards", params={"offset": 50})
    assert r.status_code == 200 and r.json() == []


def test_offset_zero_equals_no_offset(client):
    for i in range(3):
        make_card(client, title=f"карточка {i}")
    assert client.get(f"{API}/cards", params={"offset": 0}).json() == client.get(f"{API}/cards").json()


@pytest.mark.parametrize("offset", [-1, "abc", "", 1.5])
def test_invalid_offset_422(client, offset):
    assert client.get(f"{API}/cards", params={"offset": offset}).status_code == 422


def test_offset_combines_with_filters(client):
    for i in range(4):
        make_card(client, title=f"высокий {i}", priority="high")
    make_card(client, title="лишняя", priority="low")
    got = client.get(f"{API}/cards", params={"priority": "high", "limit": 2, "offset": 2}).json()
    assert len(got) == 2 and all(c["priority"] == "high" for c in got)


def test_total_count_ignores_offset(client):
    """X-Total-Count — размер выборки под фильтром, offset его не двигает."""
    for i in range(6):
        make_card(client, title=f"карточка {i}")
    r = client.get(f"{API}/cards", params={"limit": 2, "offset": 4})
    assert r.headers["X-Total-Count"] == "6" and len(r.json()) == 2


def test_offset_has_no_ceiling_unlike_limit(client):
    """Потолок нужен limit (защита ответа), offset ограничивать нечем — он не грузит выдачу."""
    make_card(client, title="одна")
    assert client.get(f"{API}/cards", params={"offset": 100000}).status_code == 200


def test_insert_between_pages_shifts_tail_by_design(client):
    """Честная слабость offset: вставка в начало сдвигает хвост, элемент повторится.

    Тест фиксирует поведение, а не притворяется, что его нет: карточки создаются
    с возрастающим id, поэтому вставка попадает в конец и страницы не едут.
    А вот удаление из начала сдвигает окно — это и проверяем.
    """
    for i in range(6):
        make_card(client, title=f"карточка {i}")
    ids = [c["id"] for c in client.get(f"{API}/cards").json()]
    page1 = client.get(f"{API}/cards", params={"limit": 3, "offset": 0}).json()
    client.delete(f"{API}/cards/{ids[0]}")  # удалили из уже показанной страницы
    page2 = client.get(f"{API}/cards", params={"limit": 3, "offset": 3}).json()
    # элемент ids[3] уже уехал в первую страницу — клиент его пропустит
    assert [c["id"] for c in page1] == ids[:3]
    assert ids[3] not in [c["id"] for c in page2]

# ---------------------------------------------------------------- поиск (#30)


def test_search_finds_by_title_substring(client):
    make_card(client, title="Сверстать колонки")
    make_card(client, title="Починить CI")
    got = client.get(f"{API}/cards", params={"q": "колонк"}).json()
    assert [c["title"] for c in got] == ["Сверстать колонки"]


def test_search_finds_by_description(client):
    make_card(client, title="Задача", description="нужен рефакторинг хранилища")
    make_card(client, title="Другая", description="ничего интересного")
    got = client.get(f"{API}/cards", params={"q": "рефакторинг"}).json()
    assert len(got) == 1 and got[0]["title"] == "Задача"


def test_search_is_case_insensitive_cyrillic(client):
    """Встроенный LIKE в SQLite регистронезависим только для ASCII — доска русская."""
    make_card(client, title="Дизайн Колонок")
    for term in ("дизайн", "ДИЗАЙН", "ДиЗаЙн"):
        got = client.get(f"{API}/cards", params={"q": term}).json()
        assert len(got) == 1, f"не нашлось по запросу {term!r}"


def test_search_is_case_insensitive_latin(client):
    make_card(client, title="Fix CI Pipeline")
    assert len(client.get(f"{API}/cards", params={"q": "ci pipeline"}).json()) == 1


def test_search_trims_whitespace(client):
    make_card(client, title="Поиск")
    assert len(client.get(f"{API}/cards", params={"q": "  поиск  "}).json()) == 1


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t"])
def test_blank_search_is_422(client, blank):
    """Пустой q — 422, а не «фильтр, который молча не фильтрует»."""
    r = client.get(f"{API}/cards", params={"q": blank})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)


def test_search_wildcards_are_literal(client):
    """'%' в запросе — обычный символ, а не маска «что угодно»."""
    make_card(client, title="Скидка 100% на всё")
    make_card(client, title="Обычная карточка")
    got = client.get(f"{API}/cards", params={"q": "100%"}).json()
    assert len(got) == 1 and "100%" in got[0]["title"]


def test_search_underscore_is_literal(client):
    make_card(client, title="файл_отчёт")
    make_card(client, title="файлХотчёт")
    got = client.get(f"{API}/cards", params={"q": "файл_"}).json()
    assert len(got) == 1 and got[0]["title"] == "файл_отчёт"


def test_search_no_matches_is_200_and_empty(client):
    make_card(client, title="что-то")
    r = client.get(f"{API}/cards", params={"q": "несуществующее"})
    assert r.status_code == 200 and r.json() == []


def test_search_combines_with_filters(client):
    make_card(client, title="Дизайн доски", status="done", priority="high")
    make_card(client, title="Дизайн иконок", status="backlog", priority="high")
    got = client.get(f"{API}/cards", params={"q": "дизайн", "status": "done"}).json()
    assert len(got) == 1 and got[0]["status"] == "done"


def test_search_combines_with_limit(client):
    for i in range(5):
        make_card(client, title=f"Поиск {i}")
    assert len(client.get(f"{API}/cards", params={"q": "поиск", "limit": 2}).json()) == 2


def test_total_count_respects_search(client):
    """X-Total-Count считается по тому же условию, что и выдача, — иначе доска решит, что она неполная."""
    for i in range(4):
        make_card(client, title=f"Поиск {i}")
    make_card(client, title="Мимо")
    r = client.get(f"{API}/cards", params={"q": "поиск", "limit": 2})
    assert r.headers["X-Total-Count"] == "4" and len(r.json()) == 2


def test_search_too_long_is_422(client):
    assert client.get(f"{API}/cards", params={"q": "я" * 201}).status_code == 422


def test_search_combines_with_pagination(client):
    """Критерий #30: поиск и пагинация (#27) работают вместе.

    Страницы внутри результата поиска не пересекаются и покрывают его целиком,
    а X-Total-Count считает совпадения, а не всю доску.
    """
    for i in range(7):
        make_card(client, title=f"Дизайн {i}")
    make_card(client, title="Мимо кассы")

    found = client.get(f"{API}/cards", params={"q": "дизайн"})
    assert found.headers["X-Total-Count"] == "7"
    expected = [c["id"] for c in found.json()]

    collected: list[int] = []
    for offset in range(0, 9, 3):
        page = client.get(f"{API}/cards", params={"q": "дизайн", "limit": 3, "offset": offset})
        assert page.headers["X-Total-Count"] == "7", "счётчик должен считать совпадения, а не всю доску"
        collected += [c["id"] for c in page.json()]
    assert collected == expected


def test_search_with_offset_beyond_matches_is_empty(client):
    make_card(client, title="Единственное совпадение")
    r = client.get(f"{API}/cards", params={"q": "совпадение", "offset": 10})
    assert r.status_code == 200 and r.json() == [] and r.headers["X-Total-Count"] == "1"


# ------------------------------------------------- дедлайны и сортировка (#31)


def test_card_has_null_deadline_by_default(client):
    assert make_card(client, title="без срока")["deadline"] is None


def test_create_card_with_deadline(client):
    card = make_card(client, title="со сроком", deadline="2026-09-01T18:00:00Z")
    assert card["deadline"] == "2026-09-01T18:00:00Z"


def test_deadline_in_the_past_is_allowed(client):
    """Прошлое разрешено: просрочка — состояние задачи, а не ошибка ввода."""
    card = make_card(client, title="просрочена", deadline="2020-01-01T00:00:00Z")
    assert card["deadline"] == "2020-01-01T00:00:00Z"


def test_deadline_with_offset_is_normalized_to_utc(client):
    """+03:00 приводится к UTC, чтобы сортировка по строке совпадала с хронологией."""
    card = make_card(client, title="мск", deadline="2026-09-01T21:00:00+03:00")
    assert card["deadline"] == "2026-09-01T18:00:00Z"


def test_deadline_without_timezone_treated_as_utc(client):
    card = make_card(client, title="без зоны", deadline="2026-09-01T18:00:00")
    assert card["deadline"] == "2026-09-01T18:00:00Z"


@pytest.mark.parametrize("bad", ["вчера", "2026-13-45T00:00:00Z", "", "   ", 42])
def test_invalid_deadline_is_422(client, bad):
    r = client.post(f"{API}/cards", json={"title": "плохой срок", "deadline": bad})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_patch_sets_deadline(client):
    card = make_card(client, title="задача")
    r = client.patch(f"{API}/cards/{card['id']}", json={"deadline": "2026-10-05T09:30:00Z"})
    assert r.status_code == 200 and r.json()["deadline"] == "2026-10-05T09:30:00Z"


def test_patch_null_clears_deadline(client):
    """`{"deadline": null}` — это «снять срок», а не «поле не передано»."""
    card = make_card(client, title="задача", deadline="2026-10-05T09:30:00Z")
    r = client.patch(f"{API}/cards/{card['id']}", json={"deadline": None})
    assert r.status_code == 200 and r.json()["deadline"] is None


def test_patch_without_deadline_keeps_it(client):
    card = make_card(client, title="задача", deadline="2026-10-05T09:30:00Z")
    r = client.patch(f"{API}/cards/{card['id']}", json={"title": "новое имя"})
    assert r.json()["deadline"] == "2026-10-05T09:30:00Z"


def test_default_sort_is_id_asc(client):
    """Без ?sort= порядок прежний — доска, не знающая про сортировку, не ломается."""
    for i in range(4):
        make_card(client, title=f"карточка {i}")
    ids = [c["id"] for c in client.get(f"{API}/cards").json()]
    assert ids == sorted(ids)


def test_sort_by_priority_is_semantic_not_alphabetical(client):
    """high > normal > low. По алфавиту было бы high, low, normal — бессмыслица."""
    make_card(client, title="низкий", priority="low")
    make_card(client, title="высокий", priority="high")
    make_card(client, title="обычный", priority="normal")
    got = client.get(f"{API}/cards", params={"sort": "priority", "order": "desc"}).json()
    assert [c["priority"] for c in got] == ["high", "normal", "low"]


def test_sort_by_deadline_asc(client):
    make_card(client, title="поздняя", deadline="2026-12-01T00:00:00Z")
    make_card(client, title="ранняя", deadline="2026-09-01T00:00:00Z")
    got = client.get(f"{API}/cards", params={"sort": "deadline"}).json()
    assert [c["title"] for c in got] == ["ранняя", "поздняя"]


def test_cards_without_deadline_go_last_asc(client):
    make_card(client, title="без срока")
    make_card(client, title="со сроком", deadline="2026-09-01T00:00:00Z")
    got = client.get(f"{API}/cards", params={"sort": "deadline"}).json()
    assert [c["title"] for c in got] == ["со сроком", "без срока"]


def test_cards_without_deadline_go_last_desc_too(client):
    """«Срока нет» — не «срок бесконечно ранний»: пустой хвост всегда в конце."""
    make_card(client, title="без срока")
    make_card(client, title="со сроком", deadline="2026-09-01T00:00:00Z")
    got = client.get(f"{API}/cards", params={"sort": "deadline", "order": "desc"}).json()
    assert [c["title"] for c in got] == ["со сроком", "без срока"]


def test_half_the_board_without_deadline(client):
    """Граничный случай из Issue: дедлайн есть у половины карточек."""
    for i in range(3):
        make_card(client, title=f"со сроком {i}", deadline=f"2026-09-0{i + 1}T00:00:00Z")
    for i in range(3):
        make_card(client, title=f"без срока {i}")
    got = client.get(f"{API}/cards", params={"sort": "deadline"}).json()
    with_deadline = [c for c in got if c["deadline"]]
    without = [c for c in got if not c["deadline"]]
    assert len(with_deadline) == 3 and len(without) == 3
    assert got[:3] == with_deadline, "карточки со сроком должны идти первыми"
    assert [c["id"] for c in without] == sorted(c["id"] for c in without), "тай-брейк по id"


def test_ties_broken_by_id_stably(client):
    """При равных ключах порядок стабилен — иначе страницы пагинации поедут."""
    for i in range(5):
        make_card(client, title=f"одинаковый приоритет {i}", priority="normal")
    first = [c["id"] for c in client.get(f"{API}/cards", params={"sort": "priority"}).json()]
    second = [c["id"] for c in client.get(f"{API}/cards", params={"sort": "priority"}).json()]
    assert first == second == sorted(first)


def test_sort_combines_with_pagination(client):
    for i in range(6):
        make_card(client, title=f"задача {i}", deadline=f"2026-09-0{i + 1}T00:00:00Z")
    page = client.get(
        f"{API}/cards", params={"sort": "deadline", "order": "desc", "limit": 2, "offset": 0}
    ).json()
    assert [c["title"] for c in page] == ["задача 5", "задача 4"]


def test_sort_combines_with_search_and_filters(client):
    make_card(client, title="Дизайн поздний", priority="high", deadline="2026-12-01T00:00:00Z")
    make_card(client, title="Дизайн ранний", priority="high", deadline="2026-09-01T00:00:00Z")
    make_card(client, title="Мимо", deadline="2026-01-01T00:00:00Z")
    got = client.get(
        f"{API}/cards", params={"q": "дизайн", "priority": "high", "sort": "deadline"}
    ).json()
    assert [c["title"] for c in got] == ["Дизайн ранний", "Дизайн поздний"]


@pytest.mark.parametrize("bad", ["title", "id", "deadline; DROP TABLE cards", ""])
def test_invalid_sort_is_422(client, bad):
    """Имя поля из запроса не должно доезжать до SQL — только из белого списка."""
    assert client.get(f"{API}/cards", params={"sort": bad}).status_code == 422


@pytest.mark.parametrize("bad", ["ascending", "up", ""])
def test_invalid_order_is_422(client, bad):
    assert client.get(f"{API}/cards", params={"order": bad}).status_code == 422


def test_sort_by_created(client):
    make_card(client, title="первая")
    make_card(client, title="вторая")
    got = client.get(f"{API}/cards", params={"sort": "created", "order": "desc"}).json()
    assert got[0]["title"] == "вторая"


def test_existing_database_gets_deadline_column(tmp_path):
    """Миграция: у старой базы без колонки deadline сервер не должен падать."""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'backlog', priority TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO cards (title, created_at, updated_at)
        VALUES ('старая карточка', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    store = Storage(str(db))
    cards = store.list_cards()
    assert len(cards) == 1 and cards[0]["deadline"] is None
    store.close()


# --------------------------- занятая база под нагрузкой (#47)


def test_busy_database_waits_and_succeeds(tmp_path):
    """Короткая блокировка должна пережидаться, а не превращаться в ошибку."""
    import sqlite3
    import threading
    import time

    db = tmp_path / "busy.db"
    store = Storage(str(db))
    store.create_card("исходная", "", "backlog", "normal")

    blocker = sqlite3.connect(db, timeout=1.0)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
        " VALUES ('lock','','backlog','normal','t','t')"
    )

    result = {}

    def write():
        try:
            result["card"] = store.create_card("дождалась", "", "backlog", "normal")
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    worker = threading.Thread(target=write)
    worker.start()
    time.sleep(1.5)  # держим заметно меньше BUSY_TIMEOUT
    blocker.rollback()
    blocker.close()
    worker.join(timeout=30)

    assert "error" not in result, f"ожидание не сработало: {result.get('error')}"
    assert result["card"]["title"] == "дождалась"
    store.close()


def test_exhausted_wait_raises_storage_busy_not_generic_error(tmp_path, monkeypatch):
    """Исчерпанное ожидание — StorageBusy (→503), а не голый OperationalError (→500).

    Именно этот путь ронял qa-load: клиент получал 500 на обычную очередь
    писателей и считал сервер сломанным.
    """
    import sqlite3

    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "BUSY_TIMEOUT", 0.3)

    db = tmp_path / "busy2.db"
    store = Storage(str(db))
    store.create_card("исходная", "", "backlog", "normal")

    blocker = sqlite3.connect(db, timeout=1.0)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO cards (title, description, status, priority, created_at, updated_at)"
        " VALUES ('lock','','backlog','normal','t','t')"
    )
    try:
        with pytest.raises(storage_module.StorageBusy):
            store.create_card("не пройдёт", "", "backlog", "normal")
    finally:
        blocker.rollback()
        blocker.close()
        store.close()


def test_busy_storage_returns_503_with_retry_after(client, monkeypatch):
    """HTTP-слой переводит StorageBusy в 503 + Retry-After, а не в 500."""
    from app import storage as storage_module

    def busy(*args, **kwargs):
        raise storage_module.StorageBusy("database is locked")

    monkeypatch.setattr(Storage, "create_card", busy)
    r = client.post(f"{API}/cards", json={"title": "во время блокировки"})

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"
    assert isinstance(r.json()["detail"], str)


def test_wal_mode_is_actually_enabled(tmp_path):
    """WAL обязан быть включён: без него читатели блокируют писателя."""
    import sqlite3

    db = tmp_path / "wal.db"
    store = Storage(str(db))
    with sqlite3.connect(db) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    store.close()

# ------------------------------------------------- экспорт / импорт (#35)


def test_export_has_version_and_cards(client):
    make_card(client, title="карточка")
    board = client.get(f"{API}/export").json()
    assert board["format_version"] == 1
    assert len(board["cards"]) == 1
    assert board["exported_at"].endswith("Z")


def test_export_includes_comments(client):
    card = make_card(client, title="с комментарием")
    client.post(f"{API}/cards/{card['id']}/comments", json={"text": "первый"})
    board = client.get(f"{API}/export").json()
    assert [c["text"] for c in board["cards"][0]["comments"]] == ["первый"]


def test_export_empty_board(client):
    board = client.get(f"{API}/export").json()
    assert board["cards"] == []


def test_round_trip_gives_equivalent_board(client):
    """Критерий #35: экспорт → импорт → экспорт даёт эквивалентную доску."""
    first = make_card(client, title="Первая", priority="high", deadline="2026-09-01T10:00:00Z")
    client.post(f"{API}/cards/{first['id']}/comments", json={"text": "обсудили"})
    client.post(f"{API}/cards/{first['id']}/comments", json={"text": "решили"})
    make_card(client, title="Вторая", description="детали", status="done")

    exported = client.get(f"{API}/export").json()
    assert client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=exported).status_code == 200
    again = client.get(f"{API}/export").json()

    # exported_at меняется по определению — сравниваем содержимое доски.
    assert again["cards"] == exported["cards"]


def test_replace_wipes_previous_board(client):
    make_card(client, title="старая")
    payload = {"format_version": 1, "cards": [{"id": 1, "title": "новая"}]}
    client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=payload)
    titles = [c["title"] for c in client.get(f"{API}/cards").json()]
    assert titles == ["новая"]


def test_replace_preserves_ids(client):
    payload = {"format_version": 1, "cards": [{"id": 77, "title": "с номером"}]}
    client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=payload)
    assert client.get(f"{API}/cards").json()[0]["id"] == 77


def test_new_card_after_replace_does_not_reuse_id(client):
    """AUTOINCREMENT должен продолжить с максимума, иначе id столкнутся."""
    client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json={"format_version": 1, "cards": [{"id": 50, "title": "а"}]})
    fresh = make_card(client, title="следующая")
    assert fresh["id"] > 50


def test_merge_keeps_existing_and_adds_new(client):
    make_card(client, title="своя")
    payload = {"format_version": 1, "cards": [{"id": 1, "title": "пришедшая"}]}
    r = client.post(f"{API}/import", params={"mode": "merge"}, json=payload)
    assert r.status_code == 200
    titles = sorted(c["title"] for c in client.get(f"{API}/cards").json())
    assert titles == ["пришедшая", "своя"]


def test_merge_does_not_overwrite_card_with_same_id(client):
    """id из файла в merge не сохраняются — чужая карточка не должна пропасть."""
    mine = make_card(client, title="моя важная")
    client.post(
        f"{API}/import",
        params={"mode": "merge"},
        json={"format_version": 1, "cards": [{"id": mine["id"], "title": "чужая"}]},
    )
    assert client.get(f"{API}/cards/{mine['id']}").json()["title"] == "моя важная"


def test_import_rejects_unknown_format_version(client):
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json={"format_version": 99, "cards": []})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_import_rejects_duplicate_ids_in_file(client):
    payload = {"format_version": 1, "cards": [{"id": 1, "title": "а"}, {"id": 1, "title": "б"}]}
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=payload)
    assert r.status_code == 422 and "duplicate" in r.json()["detail"]


@pytest.mark.parametrize(
    "bad_card",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "я" * 201},
        {"title": "ок", "priority": "urgent"},
        {"title": "ок", "status": "archived"},
        {"title": "ок", "deadline": "вчера"},
    ],
)
def test_import_validates_cards_like_post(client, bad_card):
    """Импорт не должен быть дырой в обход обычной валидации."""
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json={"format_version": 1, "cards": [bad_card]})
    assert r.status_code == 422


def test_failed_import_leaves_board_untouched(client):
    """Атомарность: битый файл не оставляет половину доски."""
    make_card(client, title="была до импорта")
    before = client.get(f"{API}/cards").json()
    client.post(
        f"{API}/import",
        json={"format_version": 1, "cards": [{"title": "хорошая"}, {"title": ""}]},
    )
    assert client.get(f"{API}/cards").json() == before


def test_import_empty_board_clears_everything(client):
    make_card(client, title="уйдёт")
    client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json={"format_version": 1, "cards": []})
    assert client.get(f"{API}/cards").json() == []


def test_import_card_without_id_gets_one(client):
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json={"format_version": 1, "cards": [{"title": "без id"}]})
    assert r.status_code == 200
    assert isinstance(client.get(f"{API}/cards").json()[0]["id"], int)


def test_import_reports_counts(client):
    payload = {"format_version": 1, "cards": [{"title": "а"}, {"title": "б"}]}
    body = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=payload).json()
    assert body == {"mode": "replace", "imported_cards": 2, "total_cards": 2}


def test_import_preserves_comments_and_timestamps(client):
    payload = {
        "format_version": 1,
        "cards": [
            {
                "id": 5,
                "title": "с историей",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "comments": [
                    {"id": 1, "text": "старый", "created_at": "2026-01-01T12:00:00Z"}
                ],
            }
        ],
    }
    client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=payload)
    card = client.get(f"{API}/cards/5").json()
    assert card["created_at"] == "2026-01-01T00:00:00Z"
    comments = client.get(f"{API}/cards/5/comments").json()
    assert comments[0]["text"] == "старый" and comments[0]["created_at"] == "2026-01-01T12:00:00Z"


def test_round_trip_after_merge_is_still_valid(client):
    make_card(client, title="исходная")
    exported = client.get(f"{API}/export").json()
    client.post(f"{API}/import", params={"mode": "merge"}, json=exported)
    again = client.get(f"{API}/export").json()
    assert len(again["cards"]) == 2
    assert client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=again).status_code == 200


# --- защита от мусорного импорта (ревью PR #45: Agent-2 и лид) ---


@pytest.mark.parametrize(
    "body",
    [
        {},                                    # пустой объект
        {"мусор": True},                       # чужой формат
        {"cards": [{"title": "есть"}]},        # нет format_version
        {"format_version": 1},                 # нет cards
    ],
)
def test_garbage_import_never_wipes_board(client, body):
    """Мусорное тело обязано дать 422 и не тронуть доску.

    Раньше `format_version` и `cards` имели значения по умолчанию, поэтому
    поля не были обязательными: `{}` проходил валидацию, трактовался как
    «заменить доску на пустую» и молча стирал всё с ответом 200.
    """
    make_card(client, title="не должна пропасть")
    before = client.get(f"{API}/cards").json()

    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=body)

    assert r.status_code == 422, f"тело {body!r} обязано быть отвергнуто"
    assert isinstance(r.json()["detail"], str)
    assert client.get(f"{API}/cards").json() == before, "доска не должна меняться"


@pytest.mark.parametrize("bad", ["не массив", 42, {"id": 1}, None])
def test_import_rejects_non_list_cards(client, bad):
    make_card(client, title="цела")
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json={"format_version": 1, "cards": bad})
    assert r.status_code == 422
    assert len(client.get(f"{API}/cards").json()) == 1


def test_explicit_empty_cards_still_clears_board(client):
    """Явное `"cards": []` — осознанная команда очистить, она остаётся легальной."""
    make_card(client, title="уйдёт по явной команде")
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json={"format_version": 1, "cards": []})
    assert r.status_code == 200 and client.get(f"{API}/cards").json() == []


def test_import_requires_explicit_mode(client):
    """Стирание доски не должно случаться из-за забытого параметра (ревью #45)."""
    make_card(client, title="цела без mode")
    r = client.post(f"{API}/import", json={"format_version": 1, "cards": []})
    assert r.status_code == 422
    assert len(client.get(f"{API}/cards").json()) == 1


# ------------------------------------------- безопасность импорта (#54)


def test_replace_without_confirmation_is_428(client):
    """Разрушительный режим требует явного подтверждения, доска цела."""
    make_card(client, title="не должна пропасть")
    r = client.post(f"{API}/import", params={"mode": "replace"},
                    json={"format_version": 1, "cards": []})
    assert r.status_code == 428
    assert "X-Confirm-Replace" in r.json()["detail"]
    assert len(client.get(f"{API}/cards").json()) == 1


@pytest.mark.parametrize("bad", ["", "no", "true", "YES ", "1"])
def test_replace_with_wrong_confirmation_is_428(client, bad):
    make_card(client, title="цела")
    r = client.post(f"{API}/import", params={"mode": "replace"},
                    headers={"X-Confirm-Replace": bad},
                    json={"format_version": 1, "cards": []})
    assert r.status_code == 428
    assert len(client.get(f"{API}/cards").json()) == 1


def test_replace_with_confirmation_works(client):
    make_card(client, title="уйдёт по подтверждённой команде")
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM,
                    json={"format_version": 1, "cards": []})
    assert r.status_code == 200 and client.get(f"{API}/cards").json() == []


def test_merge_does_not_require_confirmation(client):
    """merge ничего не теряет — трение здесь было бы без выигрыша."""
    make_card(client, title="своя")
    r = client.post(f"{API}/import", params={"mode": "merge"},
                    json={"format_version": 1, "cards": [{"title": "пришедшая"}]})
    assert r.status_code == 200 and len(client.get(f"{API}/cards").json()) == 2


def test_import_over_card_limit_is_413(client):
    from app.main import MAX_IMPORT_CARDS

    payload = {"format_version": 1,
               "cards": [{"title": f"к{i}"} for i in range(MAX_IMPORT_CARDS + 1)]}
    r = client.post(f"{API}/import", params={"mode": "merge"}, json=payload)
    assert r.status_code == 413 and str(MAX_IMPORT_CARDS) in r.json()["detail"]


def test_import_at_card_limit_passes(client):
    """Граница включительная: ровно потолок — legal."""
    from app.main import MAX_IMPORT_CARDS

    payload = {"format_version": 1,
               "cards": [{"title": f"к{i}"} for i in range(MAX_IMPORT_CARDS)]}
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=CONFIRM, json=payload)
    assert r.status_code == 200


def test_import_oversized_body_is_413(client):
    from app.main import MAX_IMPORT_BYTES

    r = client.post(
        f"{API}/import",
        params={"mode": "merge"},
        headers={"Content-Length": str(MAX_IMPORT_BYTES + 1), "Content-Type": "application/json"},
        content=b'{"format_version": 1, "cards": []}',
    )
    assert r.status_code == 413


# --- опциональный токен на мутирующие ручки ---


@pytest.fixture()
def secured(monkeypatch):
    """Включает защиту записи на время теста."""
    monkeypatch.setenv("KANBAN_WRITE_TOKEN", "s3cret")
    return {"X-Auth-Token": "s3cret"}


def test_reads_stay_open_when_token_set(client, secured):
    """Чтение не закрываем: доску можно показывать, менять — нет."""
    assert client.get(f"{API}/cards").status_code == 200
    assert client.get(f"{API}/health").status_code == 200


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.post(f"{API}/cards", json={"title": "без токена"}),
        lambda c: c.patch(f"{API}/cards/1", json={"status": "done"}),
        lambda c: c.delete(f"{API}/cards/1"),
        lambda c: c.post(f"{API}/cards/1/comments", json={"text": "т"}),
        lambda c: c.post(f"{API}/import", params={"mode": "merge"},
                         json={"format_version": 1, "cards": []}),
    ],
)
def test_writes_require_token_when_set(client, secured, call):
    r = call(client)
    assert r.status_code == 401, f"мутирующий запрос прошёл без токена: {r.status_code}"


def test_wrong_token_is_401(client, secured):
    r = client.post(f"{API}/cards", json={"title": "чужой токен"},
                    headers={"X-Auth-Token": "wrong"})
    assert r.status_code == 401


def test_correct_token_allows_write(client, secured):
    r = client.post(f"{API}/cards", json={"title": "с токеном"}, headers=secured)
    assert r.status_code == 201


def test_no_token_env_keeps_api_open(client):
    """Без переменной окружения ничего не меняется — локальная работа цела."""
    assert client.post(f"{API}/cards", json={"title": "открытый режим"}).status_code == 201


def test_token_and_confirmation_are_independent(client, secured):
    """Токен есть, подтверждения нет — всё равно 428, доска цела."""
    client.post(f"{API}/cards", json={"title": "цела"}, headers=secured)
    r = client.post(f"{API}/import", params={"mode": "replace"}, headers=secured,
                    json={"format_version": 1, "cards": []})
    assert r.status_code == 428
    assert len(client.get(f"{API}/cards").json()) == 1
