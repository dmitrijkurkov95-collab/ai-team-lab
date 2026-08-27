"""UI-тесты канбан-доски (Playwright).

Требуют поднятых сервисов:
    cd server && python run.py          # API на :8000
    cd web    && python3 serve.py       # доска на :5173

Запуск:
    pip install playwright pytest && python -m playwright install chromium
    cd web && python -m pytest tests/ -q

Тесты меняют данные на доске: гоняйте на отдельной базе, например
`KANBAN_DB=/tmp/ui.db python run.py`.
"""
from __future__ import annotations

import os
import uuid

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

WEB_URL = os.environ.get("KANBAN_WEB", "http://localhost:5173")
DRAG_SNIPPET = """
([cid, dst]) => {
  const card = document.querySelector(`article.card[data-id="${cid}"]`);
  const col = document.querySelector(`section[data-status="${dst}"]`);
  const dt = new DataTransfer();
  card.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
  col.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: dt}));
  col.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
  card.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
}
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(WEB_URL, wait_until="networkidle")
    pg.wait_for_timeout(700)
    pg.console_errors = errors  # type: ignore[attr-defined]
    yield pg
    pg.close()


def column_of(page, card_id) -> str:
    return page.locator(f'article.card[data-id="{card_id}"]').evaluate(
        "el => el.closest('section').dataset.status"
    )


def test_board_renders_four_columns(page):
    assert page.locator("section.column").count() == 4
    assert page.locator(".count").count() == 4


def test_cards_loaded_from_api(page):
    assert page.locator("article.card").count() >= 1
    assert "карточек" in page.inner_text("#status-line")


def test_column_counter_matches_cards(page):
    for status in ("backlog", "in_progress", "review", "done"):
        shown = page.inner_text(f'section[data-status="{status}"] .count')
        actual = page.locator(f'section[data-status="{status}"] article.card').count()
        assert int(shown) == actual, f"счётчик {status}: {shown} != {actual}"


def test_create_card_appears_on_board(page):
    # Уникальный заголовок: прогоны идут по общей доске, одинаковые названия
    # от прошлых запусков ломали бы проверку количества.
    title = f"UI-тест: новая карточка {uuid.uuid4().hex[:8]}"
    page.click("#btn-new")
    page.fill("#f-title", title)
    page.select_option("#f-status", "review")
    page.select_option("#f-priority", "high")
    page.click("#btn-save")
    page.wait_for_timeout(1200)
    created = page.locator('section[data-status="review"] article.card', has_text=title)
    assert created.count() == 1
    assert created.locator(".badge.high").count() == 1

    # Убираем за собой: тест не должен оставлять мусор на общей доске.
    card_id = created.first.get_attribute("data-id")
    page.request.delete(f"{WEB_URL}/api/v1/cards/{card_id}")


def test_empty_title_blocked_with_message(page):
    page.click("#btn-new")
    page.fill("#f-title", "   ")
    page.click("#btn-save")
    page.wait_for_timeout(300)
    assert "пуст" in page.inner_text("#f-title-error").lower()
    assert page.locator("#card-dialog").evaluate("d => d.open"), "диалог не должен закрываться"


def test_drag_and_drop_moves_card_and_persists(page):
    card_id = page.locator("article.card").first.get_attribute("data-id")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress", "backlog") if s != origin)

    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1200)

    assert column_of(page, card_id) == target
    server = page.request.get(f"{WEB_URL}/api/v1/cards/{card_id}").json()
    assert server["status"] == target, "перенос должен сохраниться на сервере"


def test_failed_move_rolls_back_and_warns(page):
    """Сервер отказал по существу (4xx) — доска не должна врать, что карточка переехала.

    Изменение против спринта 1: раньше откатывалась любая ошибка. Теперь откат
    только там, где повтор бессмыслен; обрыв сети копится в очереди — это
    проверяет test_логическая_ошибка_откатывает_а_не_копится и соседние.
    """
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body='{"detail":"status: недопустимое значение"}',
        )
        if route.request.method == "PATCH"
        else route.continue_(),
    )
    card_id = page.locator("article.card").first.get_attribute("data-id")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress", "backlog") if s != origin)

    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1200)

    assert column_of(page, card_id) == origin, "состояние должно откатиться"
    assert page.locator("#toasts .toast.error").count() >= 1, "пользователь должен увидеть ошибку"


def test_comments_load_and_add(page):
    page.locator("article.card").first.click()
    page.wait_for_timeout(900)
    assert page.locator("#detail-dialog").evaluate("d => d.open")

    text = "UI-тест: комментарий"
    page.fill("#c-text", text)
    page.click("#comment-form button[type=submit]")
    page.wait_for_timeout(1000)
    assert any(text in t for t in page.locator("#d-comments .comment").all_inner_texts())


def test_edit_card_from_detail(page):
    card_id = page.locator("article.card").first.get_attribute("data-id")
    page.locator(f'article.card[data-id="{card_id}"]').click()
    page.wait_for_timeout(700)
    page.click("#btn-edit")
    page.wait_for_timeout(400)

    original = page.locator(f'article.card[data-id="{card_id}"] .card-title').inner_text()
    new_title = f"UI-тест: переименовано {uuid.uuid4().hex[:8]}"
    page.fill("#f-title", new_title)
    page.click("#btn-save")
    page.wait_for_timeout(1200)
    assert page.locator(f'article.card[data-id="{card_id}"]', has_text=new_title).count() == 1

    # Возвращаем исходный заголовок: тест правит чужую карточку, а не свою.
    page.request.patch(f"{WEB_URL}/api/v1/cards/{card_id}", data={"title": original})


def test_no_console_errors(page):
    page.wait_for_timeout(500)
    assert not page.console_errors, f"ошибки в консоли: {page.console_errors[:3]}"


# ---------------------------------------------------------------- очередь синхронизации (#15)

def _queue_size(page) -> int:
    return page.evaluate("() => window.__kanban.queue.size")


def test_индикатор_синхронизации_по_умолчанию_всё_сохранено(page):
    assert page.get_attribute("#sync-indicator", "data-state") == "synced"
    assert "сохранено" in page.inner_text("#sync-label")
    assert page.locator("#btn-retry").is_hidden()


def test_при_обрыве_сети_правка_копится_а_не_теряется(page):
    """Сеть недоступна — карточка остаётся перемещённой, правка ждёт в очереди.

    Это отличие от спринта 1, где любая ошибка означала откат: перемещение
    идемпотентно, повторить его безопасно, поэтому доска не откатывает работу
    пользователя из-за пропавшей сети.
    """
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort() if route.request.method == "PATCH" else route.continue_(),
    )
    card_id = page.locator("article.card").first.get_attribute("data-id")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress", "backlog") if s != origin)

    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_function("() => window.__kanban.queue.size === 1", timeout=10000)

    assert column_of(page, card_id) == target, "карточка должна остаться в новой колонке"
    # Между попытками индикатор кратко показывает `syncing` — очередь как раз
    # повторяет операцию. Ждём паузы между повторами, иначе проверка ловит
    # случайную фазу цикла и мигает красным на ровном месте.
    page.wait_for_function(
        "() => document.getElementById('sync-indicator').dataset.state === 'pending'",
        timeout=10000,
    )
    assert _queue_size(page) == 1
    assert page.locator("#btn-retry").is_visible()


def test_повторные_перемещения_схлопываются_в_одну_операцию(page):
    """После долгого офлайна доска не должна отыгрывать всю историю перетаскиваний."""
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort() if route.request.method == "PATCH" else route.continue_(),
    )
    card_id = page.locator("article.card").first.get_attribute("data-id")
    origin = column_of(page, card_id)
    others = [s for s in ("done", "review", "in_progress", "backlog") if s != origin]

    for target in others:
        page.evaluate(DRAG_SNIPPET, [card_id, target])
        page.wait_for_timeout(400)

    assert _queue_size(page) == 1, "перемещения одной карточки схлопываются"


def test_после_возврата_сети_очередь_уходит_на_сервер(page):
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort() if route.request.method == "PATCH" else route.continue_(),
    )
    card_id = page.locator("article.card").first.get_attribute("data-id")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress", "backlog") if s != origin)
    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_function("() => window.__kanban.queue.size === 1", timeout=10000)
    assert _queue_size(page) == 1

    page.unroute("**/api/v1/cards/**")
    page.evaluate("() => window.__kanban.queue.flushNow()")
    # Ждём по условию, а не фиксированную паузу: отправка идёт по сети, и на
    # загруженной машине 1.5 с иногда не хватало — тест падал раз в несколько
    # прогонов, хотя код исправен.
    page.wait_for_function("() => window.__kanban.queue.size === 0", timeout=10000)

    assert _queue_size(page) == 0
    assert page.get_attribute("#sync-indicator", "data-state") == "synced"
    server = page.request.get(f"{WEB_URL}/api/v1/cards/{card_id}").json()
    assert server["status"] == target, "накопленная правка должна долететь до сервера"


def test_логическая_ошибка_откатывает_а_не_копится(page):
    """422 — сервер понял и отказал. Повтор бессмыслен: откатываем и говорим."""
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body='{"detail":"status: недопустимое значение"}',
        )
        if route.request.method == "PATCH"
        else route.continue_(),
    )
    card_id = page.locator("article.card").first.get_attribute("data-id")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress", "backlog") if s != origin)

    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1200)

    assert column_of(page, card_id) == origin, "при 4xx состояние откатывается"
    assert _queue_size(page) == 0, "операция снимается с очереди, а не повторяется вечно"
    assert page.locator("#toasts .toast.error").count() >= 1
    assert page.get_attribute("#sync-indicator", "data-state") == "synced"


def test_удаление_карточки_с_комментариями_предупреждает_об_их_числе(page):
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.dismiss()))

    created = page.request.post(f"{WEB_URL}/api/v1/cards", data={"title": "с обсуждением"}).json()
    for text in ("раз", "два", "три"):
        page.request.post(f"{WEB_URL}/api/v1/cards/{created['id']}/comments", data={"text": text})
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1200)

    page.locator(f'article.card[data-id="{created["id"]}"]').click()
    page.wait_for_timeout(800)
    page.click("#btn-edit")
    page.wait_for_timeout(400)
    page.click("#btn-delete")
    page.wait_for_timeout(1000)

    assert messages, "подтверждение должно появиться"
    assert "3 комментария" in messages[-1], messages[-1]
    assert "необратимо" in messages[-1], messages[-1]

    page.request.delete(f"{WEB_URL}/api/v1/cards/{created['id']}")


def test_удаление_карточки_без_комментариев_не_пугает_лишним(page):
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.dismiss()))

    created = page.request.post(f"{WEB_URL}/api/v1/cards", data={"title": "простая карточка"}).json()
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1200)

    page.locator(f'article.card[data-id="{created["id"]}"]').click()
    page.wait_for_timeout(800)
    page.click("#btn-edit")
    page.wait_for_timeout(400)
    page.click("#btn-delete")
    page.wait_for_timeout(800)

    assert messages, "подтверждение должно появиться"
    assert "исчезнет" not in messages[-1], f"нечего терять — не пугаем: {messages[-1]}"

    page.request.delete(f"{WEB_URL}/api/v1/cards/{created['id']}")


# ---------------------------------------------------------------- сверка с X-Total-Count (#21)

def test_нормальная_загрузка_не_поднимает_ложную_тревогу(page):
    """Полная выдача — молчим. Иначе предупреждение обесценится."""
    assert page.locator("#toasts .toast.error").count() == 0
    assert page.locator("article.card").count() >= 1


def test_неполная_выдача_замечена_по_x_total_count(page):
    """Сервер отдал 200 и неполный список — доска обязана сказать об этом.

    Реальный сценарий из #21: при гонке в БД запрос завершается «успешно», но
    часть карточек теряется. Ошибки нет, показывать нечего — молчаливая потеря
    данных. Ловим по расхождению длины списка с заголовком X-Total-Count.
    """
    import json as _json

    full = page.request.get(f"{WEB_URL}/api/v1/cards").json()
    assert len(full) >= 3, "для теста нужно хотя бы 3 карточки на доске"

    def truncate(route):
        if route.request.method == "GET" and route.request.url.endswith("/cards"):
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={"X-Total-Count": str(len(full)), "Access-Control-Allow-Origin": "*"},
                body=_json.dumps(full[:2]),
            )
        else:
            route.continue_()

    page.route("**/api/v1/cards", truncate)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1800)

    toasts = page.locator("#toasts .toast.error").all_inner_texts()
    assert any("получено" in t for t in toasts), f"предупреждение не показано: {toasts}"


def test_пустой_заголовок_total_count_не_считается_нулём(page):
    """`X-Total-Count: ''` — отсутствие данных, а не ноль записей.

    Замечание Agent-1 в ревью PR #24: `Number('')` даёт `0`, и на непустом
    списке доска сказала бы «получено 6 при заявленных 0». Сервер такого не
    отдаёт, но промежуточный прокси может.
    """
    import json as _json

    full = page.request.get(f"{WEB_URL}/api/v1/cards").json()

    def empty_header(route):
        if route.request.method == "GET" and route.request.url.endswith("/cards"):
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={"X-Total-Count": "", "Access-Control-Allow-Origin": "*"},
                body=_json.dumps(full),
            )
        else:
            route.continue_()

    page.route("**/api/v1/cards", empty_header)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)

    toasts = page.locator("#toasts .toast.error").all_inner_texts()
    assert not any("заявленных" in t or "получено" in t for t in toasts), (
        f"пустой заголовок не должен поднимать тревогу: {toasts}"
    )
    assert page.locator("article.card").count() == len(full)


def make_card(page, title="conflict-test", status="backlog"):
    """Своя карточка для теста конфликтов.

    Эти тесты удаляют карточки, поэтому брать первую попавшуюся с доски нельзя:
    прогон подряд исчерпает seed и следующие тесты останутся без данных.
    """
    created = page.request.post(
        f"{WEB_URL}/api/v1/cards",
        data={"title": title, "status": status},
        headers={"Content-Type": "application/json"},
    )
    assert created.status == 201, created.text()
    card_id = str(created.json()["id"])
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(700)
    page.wait_for_selector(f'article.card[data-id="{card_id}"]')
    return card_id


# --------------------------------------------------------------- конфликты (#28)


def test_удалённая_карточка_инвалидирует_правку_а_не_висит_в_очереди(page):
    """Карточку удалили с другого клиента, пока правка ждала сети.

    Повторять перенос бессмысленно — объекта больше нет. Операция обязана
    уйти из очереди, карточка — с доски, а пользователь должен узнать причину,
    а не смотреть на вечный счётчик «1 правка ждёт сети».
    """
    card_id = make_card(page, "gone-invalidate")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress", "backlog") if s != origin)

    # рвём сеть, чтобы правка осталась в очереди
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort()
        if route.request.method in ("PATCH", "GET")
        else route.continue_(),
    )
    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1200)
    assert page.evaluate("window.__kanban.queue.size") >= 1

    # «другой пользователь» удаляет карточку
    deleted = page.request.delete(f"{WEB_URL}/api/v1/cards/{card_id}")
    assert deleted.status == 204

    # сеть вернулась — повтор упрётся в 404
    page.unroute("**/api/v1/cards/**")
    page.evaluate("window.__kanban.queue.flushNow()")
    page.wait_for_timeout(2500)

    assert page.evaluate("window.__kanban.queue.size") == 0, "правка не должна висеть вечно"
    assert page.locator(f'article.card[data-id="{card_id}"]').count() == 0, "карточки больше нет"

    toasts = " ".join(page.locator("#toasts .toast").all_inner_texts())
    assert "удалена другим" in toasts, f"нужно объяснение, а не голый код: {toasts}"


def test_все_правки_удалённой_карточки_снимаются_разом(page):
    """404 по одной операции инвалидирует и остальные к той же карточке.

    Иначе пользователь получит серию одинаковых сообщений об одном событии.
    """
    card_id = make_card(page, "gone-multi")

    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort()
        if route.request.method in ("PATCH", "GET")
        else route.continue_(),
    )
    # две разные операции по одной карточке (move схлопывается, поэтому кладём вручную)
    page.evaluate(
        """(cid) => {
          const q = window.__kanban.queue;
          q.enqueue('move', Number(cid), {status: 'done'}, {status: 'backlog', updated_at: 'x'});
          q.pending.push({id: 999, kind: 'move', cardId: Number(cid),
                          payload: {status: 'review'}, attempts: 0, baseline: null});
        }""",
        card_id,
    )
    page.wait_for_timeout(400)
    assert page.evaluate("window.__kanban.queue.size") == 2

    assert page.request.delete(f"{WEB_URL}/api/v1/cards/{card_id}").status == 204

    page.unroute("**/api/v1/cards/**")
    page.evaluate("window.__kanban.queue.flushNow()")
    page.wait_for_timeout(2500)

    assert page.evaluate("window.__kanban.queue.size") == 0, "обе операции должны уйти"
    toasts = page.locator("#toasts .toast").all_inner_texts()
    gone = [t for t in toasts if "удалена другим" in t]
    assert len(gone) == 1, f"одно событие — одно сообщение, а не {len(gone)}: {toasts}"
    assert "Отменено правок: 2" in gone[0], f"нужно указать масштаб: {gone[0]}"


def test_перезапись_чужой_правки_не_проходит_молча(page):
    """Last-write-wins: наш перенос применился, но затёр чужое изменение.

    Версионирования в контракте нет, отвергнуть устаревшую запись сервер не
    может — поэтому единственная честная реакция — предупредить пользователя.
    """
    import time

    card_id = make_card(page, "overwrite-check")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress") if s != origin)

    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort() if route.request.method == "PATCH" else route.continue_(),
    )
    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1200)

    # метки времени в контракте посекундные — иначе чужая правка не отличима
    time.sleep(1.2)
    other = page.request.patch(
        f"{WEB_URL}/api/v1/cards/{card_id}",
        data={"description": "правка другого пользователя"},
        headers={"Content-Type": "application/json"},
    )
    assert other.status == 200

    page.unroute("**/api/v1/cards/**")
    page.evaluate("window.__kanban.queue.flushNow()")
    page.wait_for_timeout(2500)

    assert column_of(page, card_id) == target, "наша правка должна победить (LWW)"
    toasts = " ".join(page.locator("#toasts .toast").all_inner_texts())
    assert "перекрыла" in toasts or "параллельно" in toasts, (
        f"о перезаписи чужой правки надо сказать: {toasts}"
    )


def test_конфликт_409_обрабатывается_политикой_а_не_падением(page):
    """409 сервер сегодня не отдаёт — проверяем через мок.

    Версионирования в контракте нет, поэтому живой сервер этот путь не
    вызывает. Ветка существует, чтобы появление `If-Match` не потребовало
    переписывать очередь; тест фиксирует её поведение.
    """
    import json as _json

    card_id = make_card(page, "conflict-409")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress") if s != origin)

    def conflict(route):
        if route.request.method == "PATCH":
            route.fulfill(
                status=409,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
                body=_json.dumps({"detail": "Карточка изменена другим пользователем"}),
            )
        else:
            route.continue_()

    page.route("**/api/v1/cards/**", conflict)
    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(2500)

    assert page.evaluate("window.__kanban.queue.size") == 0, "конфликт не повторяют бесконечно"
    toasts = " ".join(page.locator("#toasts .toast").all_inner_texts())
    assert "Конфликт" in toasts or "успели изменить" in toasts, (
        f"пользователь должен понять, что случилось: {toasts}"
    )


# ----------------------------------------------------- живой рефреш (#32)


def test_доска_сама_подхватывает_чужие_карточки(page):
    """Второй пользователь создал карточку — она появляется без перезагрузки."""
    created = page.request.post(
        f"{WEB_URL}/api/v1/cards",
        data={"title": "poll-appears", "status": "review"},
        headers={"Content-Type": "application/json"},
    )
    assert created.status == 201
    card_id = created.json()["id"]

    # интервал опроса 10 с — ждём с запасом на медленный раннер
    page.wait_for_selector(f'article.card[data-id="{card_id}"]', timeout=20000)
    assert page.locator(f'article.card[data-id="{card_id}"]').count() == 1


def test_опрос_не_затирает_несинхронизированную_правку(page):
    """Главный инвариант: очередь важнее ответа сервера.

    Пользователь перетащил карточку, правка ещё не ушла. Фоновый опрос видит
    на сервере старый статус — и не имеет права вернуть карточку назад:
    это соврало бы о состоянии и спровоцировало повторное действие.
    """
    card_id = make_card(page, "poll-vs-queue")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress") if s != origin)

    # PATCH и GET карточки рвём, список (опрос) продолжает ходить
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort()
        if route.request.method in ("PATCH", "GET")
        else route.continue_(),
    )
    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1500)
    assert page.evaluate("window.__kanban.queue.size") >= 1

    # переживаем минимум один тик опроса
    page.wait_for_timeout(13000)

    assert column_of(page, card_id) == target, "опрос затёр неотправленную правку"
    assert page.evaluate("window.__kanban.queue.size") >= 1

    page.unroute("**/api/v1/cards/**")
    page.evaluate("window.__kanban.queue.flushNow()")
    page.wait_for_timeout(2500)
    assert page.evaluate("window.__kanban.queue.size") == 0
    server_status = page.request.get(f"{WEB_URL}/api/v1/cards/{card_id}").json()["status"]
    assert server_status == target, "после синхронизации состояния должны сойтись"


def test_сбой_опроса_не_обнуляет_доску(page):
    """Таймаут фонового запроса не должен мигать пустой доской.

    `loadBoard` при ошибке очищает список — это верно для первой загрузки,
    но недопустимо для тихого опроса: данные уже показаны.
    """
    before = page.locator("article.card").count()
    assert before > 0

    page.route("**/api/v1/**", lambda route: route.abort())
    page.wait_for_timeout(13000)

    assert page.locator("article.card").count() == before, "доска не должна опустеть"
    assert page.get_attribute("#freshness", "data-state") == "stale"
    assert "нет связи" in page.inner_text("#freshness")
    assert page.evaluate("window.__kanban.poller.failures") >= 1


def test_после_восстановления_связи_индикатор_снова_свежий(page):
    """Backoff сбрасывается, подпись возвращается к спокойной."""
    page.route("**/api/v1/**", lambda route: route.abort())
    page.wait_for_timeout(12000)
    assert page.evaluate("window.__kanban.poller.failures") >= 1

    page.unroute("**/api/v1/**")
    page.evaluate("window.__kanban.poller.pollNow()")
    page.wait_for_timeout(2000)

    assert page.evaluate("window.__kanban.poller.failures") == 0, "backoff должен сброситься"
    assert "обновлено" in page.inner_text("#freshness")
    assert page.get_attribute("#freshness", "data-state") == "live"


def test_backoff_растёт_при_повторных_сбоях(page):
    """Пауза увеличивается: 10с → 20с → 40с → 60с (потолок).

    Первая ошибка оставляет базовые 10 с — рост начинается со второй,
    чтобы одиночный таймаут не отправлял доску в долгий сон.
    """
    delays = page.evaluate(
        """() => {
          const p = window.__kanban.poller;
          const saved = p.failures;
          const out = [0,1,2,3,4,5].map(f => { p.failures = f; return p.currentDelay(); });
          p.failures = saved;
          return out;
        }"""
    )
    assert delays[0] == 10000, f"без ошибок — базовый интервал: {delays}"
    assert delays[1] == 10000, f"первая ошибка не должна удлинять паузу: {delays}"
    assert delays[2] == 20000 and delays[3] == 40000, f"рост нарушен: {delays}"
    assert delays[4] == 60000 and delays[5] == 60000, f"потолок 60 с не держится: {delays}"


def test_скрытая_вкладка_не_опрашивает_сервер(page):
    """Никто не смотрит — не тратим сеть и батарею."""
    page.wait_for_timeout(500)
    scheduled = page.evaluate(
        """() => {
          const p = window.__kanban.poller;
          const orig = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden');
          Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
          p.clearTimer();
          p.schedule();
          const armed = p.timer !== null;
          Object.defineProperty(document, 'hidden', orig || {configurable: true, get: () => false});
          p.schedule();
          return armed;
        }"""
    )
    assert scheduled is False, "в скрытой вкладке таймер опроса не должен взводиться"


# ------------------------------------------------- прокси дев-сервера


def test_прокси_не_падает_на_сырой_кириллице_в_query(page):
    """Сырые не-ASCII байты в query роняли прокси, а не запрос.

    `http.client` кодирует строку запроса в ASCII: `?q=задача` без
    процентного экранирования (curl, скрипты, ручная отладка) приводил к
    `UnicodeEncodeError` и обрыву соединения. Браузер такое кодирует сам,
    поэтому дыра не всплывала в UI-тестах — нашлась при ревью чужого PR.
    """
    r = page.request.get(f"{WEB_URL}/api/v1/cards?status=мусор")
    assert r.status == 422, f"ожидали честный 422, получили {r.status}"
    assert isinstance(r.json()["detail"], str)


def test_прокси_сохраняет_кириллицу_без_искажения(page):
    """Мало не упасть — нельзя и портить данные по дороге."""
    title = "Уникальное слово черёмуха"
    created = page.request.post(
        f"{WEB_URL}/api/v1/cards",
        data={"title": title},
        headers={"Content-Type": "application/json"},
    )
    assert created.status == 201

    found = page.request.get(f"{WEB_URL}/api/v1/cards?q=черёмуха").json()
    assert any(c["title"] == title for c in found), (
        f"поиск по кириллице через прокси должен находить карточку: {found}"
    )
    page.request.delete(f"{WEB_URL}/api/v1/cards/{created.json()['id']}")


def test_прокси_отвечает_на_битый_запрос_а_не_падает(page):
    """400 Bad request не должен превращаться в AttributeError.

    `end_headers` обращался к `self.path`, которого при неразобранной
    строке запроса ещё нет — обработчик падал вместо честного ответа.
    """
    import socket

    with socket.create_connection(("localhost", 5173), timeout=5) as sock:
        sock.sendall(b"GET /api/v1/cards?q=\xd0\xb7\xd0\xb0 \xff HTTP/1.1\r\nHost: x\r\n\r\n")
        reply = sock.recv(256)
    assert reply.startswith(b"HTTP/1."), f"сервер должен ответить, а не оборвать: {reply!r}"

    # и остаться живым для следующих запросов
    assert page.request.get(f"{WEB_URL}/api/v1/cards").status == 200


def test_503_переигрывается_очередью_а_не_пугает_пользователя(page):
    """Сервер занят другим писателем (#47) — это «повтори», а не «сломалось».

    Бэкенд специально отдаёт `503` вместо `500`, рассчитывая, что очередь
    правок такие ответы переигрывает. Проверяем, что расчёт верен: молчаливая
    регрессия здесь означала бы потерянную правку под нагрузкой.
    """
    import json as _json

    card_id = make_card(page, "busy-503")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress") if s != origin)

    attempts = {"n": 0}

    def busy(route):
        if route.request.method == "PATCH":
            attempts["n"] += 1
            if attempts["n"] <= 2:  # первые две попытки — база занята
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    headers={"Retry-After": "1", "Access-Control-Allow-Origin": "*"},
                    body=_json.dumps({"detail": "База временно занята другой записью"}),
                )
                return
        route.continue_()

    page.route("**/api/v1/cards/**", busy)
    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1500)

    assert page.evaluate("window.__kanban.queue.size") >= 1, "503 не должен выбрасывать правку"
    assert column_of(page, card_id) == target

    page.wait_for_function("() => window.__kanban.queue.size === 0", timeout=15000)
    assert attempts["n"] >= 3, f"должны были быть повторы, а было попыток: {attempts['n']}"

    server_status = page.request.get(f"{WEB_URL}/api/v1/cards/{card_id}").json()["status"]
    assert server_status == target, "после повторов правка обязана дойти"

    errors = " ".join(page.locator("#toasts .toast.error").all_inner_texts())
    assert "не удалось" not in errors.lower(), (
        f"временная занятость базы — не повод пугать пользователя: {errors}"
    )


def test_retry_after_с_сервера_важнее_собственного_backoff(page):
    """503 приходит с Retry-After — сервер называет паузу сам.

    Игнорировать её и гадать своим backoff значит либо долбить занятую базу
    раньше времени, либо ждать дольше, чем нужно.
    """
    parsed = page.evaluate(
        """async () => {
          const m = await import('/api.js');
          const before = performance.now();
          try {
            // сервер отвечает 503 с Retry-After — проверяем, что клиент его прочитал
            await m.updateCard(999999, {status: 'done'});
            return {error: 'ожидали отказ'};
          } catch (e) {
            return {status: e.status, retryAfterMs: e.retryAfterMs, name: e.name};
          }
        }"""
    )
    # На живом сервере карточки нет — придёт 404 без Retry-After, поле должно быть null,
    # а не undefined: контракт объекта ошибки обязан быть стабильным.
    assert parsed["name"] == "ApiError"
    assert parsed["retryAfterMs"] is None, f"без заголовка ожидаем null: {parsed}"


def test_очередь_ждёт_столько_сколько_просил_сервер(page):
    """Retry-After: 2 → пауза перед повтором берётся из заголовка, а не 1 с."""
    import json as _json

    card_id = make_card(page, "retry-after")
    origin = column_of(page, card_id)
    target = next(s for s in ("done", "review", "in_progress") if s != origin)

    def busy(route):
        if route.request.method == "PATCH":
            route.fulfill(
                status=503,
                content_type="application/json",
                headers={"Retry-After": "2", "Access-Control-Allow-Origin": "*"},
                body=_json.dumps({"detail": "занято"}),
            )
        else:
            route.continue_()

    page.route("**/api/v1/cards/**", busy)
    page.evaluate(DRAG_SNIPPET, [card_id, target])
    page.wait_for_timeout(1500)

    delay = page.evaluate("window.__kanban.queue.retryAfterMs")
    assert delay == 2000, f"пауза должна прийти из Retry-After: {delay}"
    assert page.evaluate("window.__kanban.queue.size") >= 1


def test_прокси_пробрасывает_заголовок_подтверждения(page):
    """Защита из #54 обязана работать и через прокси, а не только напрямую.

    Прокси пересылал только Content-Type и Accept, поэтому X-Confirm-Replace
    терялся: напрямую в API импорт с подтверждением давал 200, через прокси —
    428. Доска ходит только через прокси, то есть подтверждённый импорт был
    для неё недоступен в принципе.
    """
    board = page.request.get(f"{WEB_URL}/api/v1/export").json()

    without = page.request.post(
        f"{WEB_URL}/api/v1/import?mode=replace",
        data=board,
        headers={"Content-Type": "application/json"},
    )
    assert without.status == 428, "без подтверждения replace обязан отклоняться"

    with_confirm = page.request.post(
        f"{WEB_URL}/api/v1/import?mode=replace",
        data=board,
        headers={"Content-Type": "application/json", "X-Confirm-Replace": "yes"},
    )
    assert with_confirm.status == 200, (
        f"подтверждение не доехало до API через прокси: {with_confirm.status}"
    )
