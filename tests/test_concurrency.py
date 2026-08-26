"""Конкурентная нагрузка: ни один запрос не должен отдавать 5xx.

Закрывает дыру в покрытии из issue #21. Баг `sqlite3.InterfaceError` под
параллельными запросами дожил до спринта 2 именно потому, что **ни один сьют не
бил параллельно**: серверные тесты последовательны через TestClient,
контракт-сьют — последовательный httpx, UI-тесты гоняют браузер в одиночку.

Два свойства этого теста, о которых надо знать:

**1. Одних чтений недостаточно.** 240 параллельных GET на 76 карточках в 64
потока — стабильно ноль ошибок, пять прогонов подряд. Гонку создаёт только
смешанная нагрузка с записью. Первая версия теста была читательской и не ловила
вообще ничего.

**2. Тест вероятностный, и это измерено, а не предполагается.** На заведомо
багованном коде (одно общее соединение, `check_same_thread=False`) он ловит
проблему в **11 из 12 испытаний (~92%)** при текущих параметрах. Гонка
стохастическая: отдельные заходы проходят чисто (`[14, 0, 1]`), поэтому один
заход детектором быть не может — делаем пять.

Следствие: зелёный прогон **не доказывает** отсутствие гонки. Он доказывает,
что в пяти заходах подряд её не случилось. Для CI этого достаточно (тест
прогоняется на каждом PR), но полагаться на один локальный прогон не стоит.
На исправленном коде тест обязан быть зелёным всегда — вот это уже сигнал.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

WORKERS = 64
REQUESTS_PER_BURST = 400
BURSTS = 5
CARDS = 30


@pytest.fixture()
def load_pool(client, make_card):
    """Доска с данными: на почти пустой гонка проявляется заметно реже."""
    ids = [make_card(title=f"нагрузка {i}")["id"] for i in range(CARDS)]
    for cid in ids[:10]:
        client.post(f"/cards/{cid}/comments", json={"text": "комментарий"})
    return ids


def _one_burst(client, ids, created, lock):
    def work(i):
        cid = ids[i % len(ids)]
        kind = i % 5
        try:
            if kind == 0:
                r = client.post("/cards", json={"title": f"параллельная {i}"})
                if r.status_code in (200, 201):
                    with lock:
                        created.append(r.json()["id"])
                return ("POST", r.status_code)
            if kind == 1:
                return ("PATCH", client.patch(f"/cards/{cid}", json={"status": "review"}).status_code)
            if kind == 2:
                return ("COMMENT", client.post(f"/cards/{cid}/comments", json={"text": f"поток {i}"}).status_code)
            if kind == 3:
                return ("GET_COMMENTS", client.get(f"/cards/{cid}/comments").status_code)
            return ("GET_CARD", client.get(f"/cards/{cid}").status_code)
        except Exception as e:  # сеть/таймаут — тоже провал, не глотаем
            return (f"EXCEPTION:{type(e).__name__}", -1)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(work, range(REQUESTS_PER_BURST)))


def test_параллельные_запросы_не_отдают_5xx(client, created, load_pool):
    """Пять заходов смешанной нагрузки: ожидаем ноль 5xx и ноль ложных 404.

    Ложные 404 проверяются отдельно и не зря: при гонке запрос иногда
    возвращает пустой результат, и существующая карточка отдаётся как
    отсутствующая. Это тоже баг, но с кодом 404 — без отдельной проверки он
    выглядел бы как норма.
    """
    ids = load_pool
    new_ids: list[int] = []
    lock = threading.Lock()
    all_results = []
    per_burst = []

    for b in range(BURSTS):
        res = _one_burst(client, ids, new_ids, lock)
        all_results.extend(res)
        per_burst.append(sum(1 for _, code in res if code >= 500 or code == -1))

    created.extend(new_ids)  # уборка за собой: фикстура удалит их в teardown

    fivexx = [(k, c) for k, c in all_results if c >= 500 or c == -1]
    false_404 = [(k, c) for k, c in all_results if k == "GET_CARD" and c == 404]

    detail = (
        f"\nвсего запросов: {len(all_results)} ({BURSTS} заходов по {REQUESTS_PER_BURST}, {WORKERS} потоков)"
        f"\n5xx/исключений по заходам: {per_burst}"
        f"\nвиды отказов: {sorted({k for k, _ in fivexx})}"
        f"\nложных 404 на существующих карточках: {len(false_404)}"
        f"\n\nТест вероятностный: ~92% поимки на заведомо багованном коде."
        f"\nЗелёный прогон не доказывает отсутствие гонки — только что в {BURSTS} заходах её не случилось."
    )
    assert not fivexx, f"под конкурентной нагрузкой сервер отдаёт 5xx{detail}"
    assert not false_404, f"существующие карточки отдаются как 404 — симптом гонки курсора{detail}"


def test_последовательные_запросы_после_нагрузки_корректны(client, load_pool):
    """После конкурентного шторма состояние не должно разъехаться."""
    for cid in load_pool[:5]:
        r = client.get(f"/cards/{cid}")
        assert r.status_code == 200, f"карточка {cid} пропала после нагрузки"
        assert r.json()["id"] == cid
        comments = client.get(f"/cards/{cid}/comments")
        assert comments.status_code == 200
        assert isinstance(comments.json(), list)
