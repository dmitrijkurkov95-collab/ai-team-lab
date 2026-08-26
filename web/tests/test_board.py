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
    title = "UI-тест: новая карточка"
    page.click("#btn-new")
    page.fill("#f-title", title)
    page.select_option("#f-status", "review")
    page.select_option("#f-priority", "high")
    page.click("#btn-save")
    page.wait_for_timeout(1200)
    created = page.locator('section[data-status="review"] article.card', has_text=title)
    assert created.count() == 1
    assert created.locator(".badge.high").count() == 1


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
    """Сервер отказал — доска не должна врать, что карточка переехала."""
    page.route(
        "**/api/v1/cards/**",
        lambda route: route.abort() if route.request.method == "PATCH" else route.continue_(),
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

    new_title = "UI-тест: переименовано"
    page.fill("#f-title", new_title)
    page.click("#btn-save")
    page.wait_for_timeout(1200)
    assert page.locator(f'article.card[data-id="{card_id}"]', has_text=new_title).count() == 1


def test_no_console_errors(page):
    page.wait_for_timeout(500)
    assert not page.console_errors, f"ошибки в консоли: {page.console_errors[:3]}"
