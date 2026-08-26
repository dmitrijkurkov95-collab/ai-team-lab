"""e2e полного стенда (задача #16): страница + API одним origin через прокси.

Здесь проверяется не API и не фронтенд по отдельности, а **связка**: что доска
отдаётся, что `/api/v1` жив через прокси `web/serve.py`, и что пользовательский
сценарий проходит целиком там, где его реально запускает браузер.

Контракт API отдельно и строже покрыт в tests/ — здесь только интеграция.
"""
from __future__ import annotations

import httpx
import pytest


def _cleanup(proxied, card_id):
    proxied.delete(f"/cards/{card_id}")


# ---------- стенд поднят ----------

def test_страница_доски_отдаётся(origin):
    r = httpx.get(f"{origin}/", timeout=10)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "Канбан-доска" in r.text
    # ключевые точки интерфейса на месте — иначе страница есть, а доски нет
    for marker in ('id="board"', 'id="btn-new"', 'id="card-form"'):
        assert marker in r.text, f"в странице нет {marker}"


def test_api_жив_через_прокси(proxied):
    r = proxied.get("/cards")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_статика_и_api_идут_с_одного_origin(origin):
    """Смысл прокси: браузеру не нужны два порта и CORS. Проверяем, что это правда."""
    page = httpx.get(f"{origin}/", timeout=10)
    api = httpx.get(f"{origin}/api/v1/cards", timeout=10)
    assert page.url.host == api.url.host and page.url.port == api.url.port
    assert page.status_code == 200 and api.status_code == 200


def test_seed_виден_через_прокси(proxied):
    """Доска не должна быть пустой при первом старте — критерий из задачи #2."""
    cards = proxied.get("/cards").json()
    assert cards, "через прокси доска пустая: seed не доехал"
    assert len({c["status"] for c in cards}) >= 3


# ---------- пользовательский сценарий одним origin ----------

def test_сценарий_создать_переместить_удалить(proxied):
    r = proxied.post("/cards", json={"title": "e2e: сквозной сценарий", "priority": "high"})
    assert r.status_code in (200, 201), f"создание через прокси: {r.status_code} {r.text}"
    card = r.json()
    cid = card["id"]
    try:
        assert card["status"] in ("backlog", "in_progress", "review", "done")

        moved = proxied.patch(f"/cards/{cid}", json={"status": "review"})
        assert moved.status_code == 200
        assert moved.json()["status"] == "review"

        # карточка реально переехала в другую колонку, а не только в ответе
        review = [c["id"] for c in proxied.get("/cards", params={"status": "review"}).json()]
        assert cid in review

        commented = proxied.post(f"/cards/{cid}/comments", json={"text": "e2e: комментарий"})
        assert commented.status_code in (200, 201)
        texts = [c["text"] for c in proxied.get(f"/cards/{cid}/comments").json()]
        assert "e2e: комментарий" in texts

        assert proxied.delete(f"/cards/{cid}").status_code == 204
        assert proxied.get(f"/cards/{cid}").status_code == 404
        assert cid not in [c["id"] for c in proxied.get("/cards").json()]
    finally:
        _cleanup(proxied, cid)


# ---------- прокси не искажает ответы ----------

def test_прокси_не_искажает_успешный_ответ(proxied, direct):
    """Один и тот же запрос через прокси и напрямую должен дать одинаковое тело."""
    a = proxied.get("/cards")
    b = direct.get("/cards")
    assert a.status_code == b.status_code
    assert a.json() == b.json()


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/cards/999999", None),
        ("post", "/cards", {"title": ""}),
        ("get", "/cards/abc", None),
    ],
)
def test_прокси_не_искажает_ошибки(proxied, direct, method, path, payload):
    """404/422 должны доходить до клиента с тем же кодом и той же формой тела.

    Прокси переписывает заголовки и сам ставит Content-Length — место, где
    ошибка легко превращается в 500 или теряет тело. Проверяем, что не превратилась.
    """
    kwargs = {"json": payload} if payload is not None else {}
    p = getattr(proxied, method)(path, **kwargs)
    d = getattr(direct, method)(path, **kwargs)
    assert p.status_code == d.status_code, f"прокси изменил код: {p.status_code} vs {d.status_code}"
    assert p.status_code in (404, 422)
    assert p.json() == d.json(), "прокси исказил тело ошибки"
    assert isinstance(p.json().get("detail"), str), "detail должен оставаться строкой"


def test_прокси_пробрасывает_204_без_тела(proxied):
    """DELETE отдаёт 204 без тела — классическое место, где прокси ломается."""
    cid = proxied.post("/cards", json={"title": "e2e: на удаление"}).json()["id"]
    r = proxied.delete(f"/cards/{cid}")
    assert r.status_code == 204
    assert r.content == b"", f"у 204 не должно быть тела, получено {r.content!r}"


def test_повторный_delete_через_прокси_это_404(proxied):
    cid = proxied.post("/cards", json={"title": "e2e: повторное удаление"}).json()["id"]
    assert proxied.delete(f"/cards/{cid}").status_code == 204
    r = proxied.delete(f"/cards/{cid}")
    assert r.status_code == 404
    assert isinstance(r.json().get("detail"), str)
