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
    page.wait_for_timeout(1200)

    assert column_of(page, card_id) == target, "карточка должна остаться в новой колонке"
    assert page.get_attribute("#sync-indicator", "data-state") == "pending"
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
    page.wait_for_timeout(1000)
    assert _queue_size(page) == 1

    page.unroute("**/api/v1/cards/**")
    page.evaluate("() => window.__kanban.queue.flushNow()")
    page.wait_for_timeout(1500)

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
