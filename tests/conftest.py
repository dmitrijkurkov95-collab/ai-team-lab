"""Фикстуры контракт-сьюта: живой HTTP, изоляция, честная разметка спорных мест.

ВАЖНО. Сьют бьёт по живому серверу (httpx -> http://localhost:8000/api/v1),
а не через fastapi.TestClient. Причина принципиальная: фронтенд ходит по сети,
и TestClient проверяет не то, что увидит фронтенд (прокси, сериализация,
коды на уровне ASGI). Задача #4 прямо требует гонять тесты против localhost:8000.
"""
from __future__ import annotations

import os

import httpx
import pytest

API = os.environ.get("KANBAN_API", "http://localhost:8000/api/v1")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "gap: место, где контракт молчит — падение требует решения лида, а не правки кода",
    )
    config.addinivalue_line(
        "markers",
        "wipe: разрушающие тесты на пустой доске — только с KANBAN_ALLOW_WIPE=1",
    )


@pytest.fixture(scope="session", autouse=True)
def server_reachable():
    """Падаем сразу и внятно, если сервер не поднят — иначе будет 100 непонятных ошибок."""
    try:
        httpx.get(f"{API}/cards", timeout=5)
    except httpx.HTTPError as e:
        pytest.exit(
            f"Сервер недоступен по {API}: {e}\n"
            f"Поднимите: cd server && python run.py   (или KANBAN_API=<url> pytest)",
            returncode=2,
        )


@pytest.fixture()
def created():
    """Реестр карточек, созданных тестом.

    Отдельная фикстура, а не атрибут, навешенный на httpx.Client: по замечанию
    обоих ревьюеров `c.created = [...]` — это негласный контракт между фикстурами,
    который легко порвать. Явный список читается честнее.
    """
    return []


@pytest.fixture()
def client(created):
    """HTTP-клиент. В teardown убираем созданные тестом карточки и говорим, если не вышло."""
    with httpx.Client(base_url=API, timeout=10) as c:
        yield c
    failed: list[tuple[int, int]] = []
    with httpx.Client(base_url=API, timeout=10) as c:
        for cid in created:
            try:
                r = c.delete(f"/cards/{cid}")
            except httpx.HTTPError as e:  # сеть отвалилась — молча не глотаем
                failed.append((cid, -1))
                print(f"\n[teardown] не смог удалить карточку {cid}: {e}")
                continue
            # 404 допустим: тест мог удалить карточку сам
            if r.status_code not in (204, 404):
                failed.append((cid, r.status_code))
    if failed:
        # Не роняем тест из-за уборки, но и не молчим: иначе мусор копится на доске незаметно.
        print(f"\n[teardown] ВНИМАНИЕ: карточки не удалены {failed} — на доске останется мусор")


@pytest.fixture()
def make_card(client, created):
    """Создаёт карточку и запоминает её для уборки."""

    def _make(**overrides):
        payload = {"title": "тестовая карточка", **overrides}
        r = client.post("/cards", json=payload)
        assert r.status_code == 201, f"не смог создать карточку для теста: {r.status_code} {r.text}"
        created.append(r.json()["id"])
        return r.json()

    return _make


@pytest.fixture()
def empty_board(client):
    """Временно опустошает доску и восстанавливает её.

    Разрушающая фикстура, поэтому по умолчанию ПРОПУСКАЕТСЯ: доска может быть
    общей. Включается явно — KANBAN_ALLOW_WIPE=1 pytest -m wipe.
    """
    if os.environ.get("KANBAN_ALLOW_WIPE") != "1":
        pytest.skip("нужен KANBAN_ALLOW_WIPE=1 — фикстура стирает доску")
    backup = client.get("/cards").json()
    for c in backup:
        client.delete(f"/cards/{c['id']}")
    yield client
    # Восстановление в два шага намеренно: тело POST по контракту — {title,
    # description?, priority?}, статус туда не входит. Создать и сразу получить
    # нужный status одним запросом нельзя — поэтому создаём и двигаем через PATCH.
    # Иначе вся доска молча вернётся в backlog (это была реальная ошибка, поймана прогоном).
    for c in backup:
        r = client.post(
            "/cards",
            json={
                "title": c["title"],
                "description": c["description"],
                "priority": c["priority"],
            },
        )
        if r.status_code == 201 and c["status"] != r.json()["status"]:
            client.patch(f"/cards/{r.json()['id']}", json={"status": c["status"]})
