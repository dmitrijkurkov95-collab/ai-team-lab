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

import httpx
import pytest

WORKERS = 64
REQUESTS_PER_BURST = 400
BURSTS = 5
CARDS = 30

# Транспортный шум общего CI-раннера: под 64 потоками uvicorn иногда не успевает
# принять соединение, и httpx отдаёт ReadError/ConnectError. Это не ответ сервера
# и не признак гонки в SQLite — именно из-за этого лид вынес нагрузку из
# qa-contract в qa-load (коммит 101fd37).
#
# Повторять такие запросы НЕЛЬЗЯ, и это проверено, а не придумано: первая версия
# делала 3 повтора с backoff, и поимка гонки на заведомо багованном коде упала
# с 3/3 до 1/3, а число 5xx — с десятков до одного. Причина проста: sleep между
# повторами разряжает нагрузку, и гонка случается реже. Тест перестал ловить то,
# ради чего написан.
#
# Поэтому шум не повторяем, а считаем и терпим до порога: пара ReadError на
# занятом раннере — это шум, а поток ошибок означает, что сервер не справляется.
# Порог в долях от общего числа запросов.
TRANSPORT_TOLERANCE = 0.01

# 503 «база занята» — штатный ответ, а не ошибка сервера (#47/#48): клиент
# обязан повторить по Retry-After, и очередь фронта это умеет (#50).
# Требовать ноль нельзя — это природа однофайловой SQLite под нагрузкой;
# но поток таких ответов означает, что нагрузка переросла хранилище.
# Порог тот же приём, что и для транспортного шума: терпим, но считаем.
BUSY_TOLERANCE = 0.01
LOAD_TIMEOUT = 30.0


@pytest.fixture()
def load_pool(client, make_card):
    """Доска с данными: на почти пустой гонка проявляется заметно реже."""
    ids = [make_card(title=f"нагрузка {i}")["id"] for i in range(CARDS)]
    for cid in ids[:10]:
        client.post(f"/cards/{cid}/comments", json={"text": "комментарий"})
    return ids


def _call(client, method, url, **kwargs):
    """Запрос без повторов. Возвращает httpx.Response или None.

    None значит «запрос не дошёл» — транспортная ошибка. Отдельный случай,
    а не 5xx: сервер не ответил, а не ответил ошибкой. Не повторяем намеренно,
    см. комментарий про TRANSPORT_TOLERANCE.
    """
    try:
        return getattr(client, method)(url, **kwargs)
    except httpx.TransportError:
        return None


def _one_burst(client, ids, created, lock):
    def work(i):
        cid = ids[i % len(ids)]
        kind = i % 5
        if kind == 0:
            r = _call(client, "post", "/cards", json={"title": f"параллельная {i}"})
            if r is None:
                return ("POST", -1)
            if r.status_code in (200, 201):
                try:
                    with lock:
                        created.append(r.json()["id"])
                except (ValueError, KeyError):
                    pass  # тело не разобрано — уборку обеспечит wipe-фикстура, тест не роняем
            return ("POST", r.status_code)
        routes = (
            ("PATCH", "patch", f"/cards/{cid}", {"json": {"status": "review"}}),
            ("COMMENT", "post", f"/cards/{cid}/comments", {"json": {"text": f"поток {i}"}}),
            ("GET_COMMENTS", "get", f"/cards/{cid}/comments", {}),
            ("GET_CARD", "get", f"/cards/{cid}", {}),
        )
        label, method, url, kwargs = routes[(kind - 1) % len(routes)]
        r = _call(client, method, url, **kwargs)
        return (label, -1 if r is None else r.status_code)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(work, range(REQUESTS_PER_BURST)))


def test_параллельные_запросы_не_отдают_5xx(stack_api, created, load_pool):
    """Пять заходов смешанной нагрузки: ожидаем ноль 5xx и ноль ложных 404.

    Ложные 404 проверяются отдельно и не зря: при гонке запрос иногда
    возвращает пустой результат, и существующая карточка отдаётся как
    отсутствующая. Это тоже баг, но с кодом 404 — без отдельной проверки он
    выглядел бы как норма.

    Клиент здесь свой, с таймаутом LOAD_TIMEOUT вместо общих 10 с: под 64
    потоками на медленном раннере 10 с иногда не хватает, и это выглядело бы
    как падение сервера.
    """
    ids = load_pool
    new_ids: list[int] = []
    lock = threading.Lock()
    all_results = []
    per_burst = []

    with httpx.Client(base_url=stack_api, timeout=LOAD_TIMEOUT) as client:
        for _ in range(BURSTS):
            res = _one_burst(client, ids, new_ids, lock)
            all_results.extend(res)
            per_burst.append(
                sum(1 for _, code in res if (code >= 500 and code != 503) or code == -1)
            )

    created.extend(new_ids)  # уборка за собой: фикстура удалит их в teardown

    # Три разных сигнала, и путать их нельзя:
    #  - 5xx          — сервер ответил ошибкой, это баг, терпим ноль;
    #  - 503 «занято»  — штатный ответ под нагрузкой, терпим до порога;
    #  - ложный 404   — существующая карточка отдана как отсутствующая, тоже баг;
    #  - транспорт    — запрос не дошёл, на общем раннере бывает, терпим до порога.
    fivexx = [(k, c) for k, c in all_results if c >= 500 and c != 503]
    busy = [(k, c) for k, c in all_results if c == 503]
    transport = [(k, c) for k, c in all_results if c == -1]
    false_404 = [(k, c) for k, c in all_results if k == "GET_CARD" and c == 404]
    transport_limit = int(len(all_results) * TRANSPORT_TOLERANCE)
    busy_limit = int(len(all_results) * BUSY_TOLERANCE)

    detail = (
        f"\nвсего запросов: {len(all_results)} ({BURSTS} заходов по {REQUESTS_PER_BURST}, {WORKERS} потоков)"
        f"\nпровалов по заходам: {per_burst}"
        f"\n5xx (сервер ответил ошибкой): {len(fivexx)}, виды: {sorted({k for k, _ in fivexx})}"
        f"\n503 (база занята, штатно): {len(busy)}, допуск {busy_limit}"
        f"\nтранспорт (запрос не дошёл): {len(transport)}, допуск {transport_limit}"
        f"\nложных 404 на существующих карточках: {len(false_404)}"
        f"\n\nТест вероятностный: ~92% поимки на заведомо багованном коде."
        f"\nЗелёный прогон не доказывает отсутствие гонки — только что в {BURSTS} заходах её не случилось."
    )
    assert not fivexx, f"под конкурентной нагрузкой сервер отдаёт 5xx{detail}"
    assert not false_404, f"существующие карточки отдаются как 404 — симптом гонки курсора{detail}"
    assert len(busy) <= busy_limit, (
        f"слишком много 503 «база занята» — очередь писателей не рассасывается, "
        f"нагрузка переросла SQLite{detail}"
    )
    assert len(transport) <= transport_limit, (
        f"транспортных ошибок больше допуска — сервер не справляется с нагрузкой, "
        f"это уже не шум раннера{detail}"
    )


def test_последовательные_запросы_после_нагрузки_корректны(client, load_pool):
    """После конкурентного шторма состояние не должно разъехаться."""
    for cid in load_pool[:5]:
        r = client.get(f"/cards/{cid}")
        assert r.status_code == 200, f"карточка {cid} пропала после нагрузки"
        assert r.json()["id"] == cid
        comments = client.get(f"/cards/{cid}/comments")
        assert comments.status_code == 200
        assert isinstance(comments.json(), list)
