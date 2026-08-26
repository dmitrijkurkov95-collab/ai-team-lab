"""Проверки формата ответов, заданного контрактом.

Вынесено в отдельный модуль, а не в conftest: эти функции вызываются из тела
тестов, а не являются фикстурами. Держим в одном месте, потому что форму `422`
надо проверять и в строгом слое, и в gap-слое — дублировать ассерт нельзя,
иначе при правке контракта разойдутся две копии.
"""
from __future__ import annotations


def assert_404_string_detail(r, what: str) -> None:
    """Контракт: `404` -> `{"detail": "<строка>"}`."""
    assert r.status_code == 404, f"{what} -> {r.status_code}, ждём 404"
    detail = r.json().get("detail")
    assert isinstance(detail, str), (
        f'контракт требует 404 {{"detail": "<строка>"}}, получено {detail!r} ({what})'
    )


def assert_422_shape(r, what: str) -> None:
    """Контракт: `422` -> `{"detail": "<строка для человека>", "errors": [...]}`.

    Код здесь НЕ проверяем намеренно: для части путей (`?status=`, `/cards/abc`)
    сам факт 422 контрактом не задан — это gap. А вот форма тела обязательна для
    любого 422, поэтому её проверяем везде. Именно эта проверка ловит регресс
    бага, который команда чинила в PR #8: если обработчик RequestValidationError
    снесут, `detail` снова станет массивом, коды не изменятся — и тесты, которые
    смотрят только на код, останутся зелёными.
    """
    assert r.status_code == 422, f"{what} -> {r.status_code}, ждём 422"
    body = r.json()
    detail = body.get("detail")
    assert isinstance(detail, str), (
        f'контракт требует 422 {{"detail": "<строка>"}}, получено {detail!r} ({what}). '
        f"Это регресс формата ошибок из PR #8."
    )
    assert detail.strip(), f"detail пустой — клиенту нечего показать ({what})"
    assert isinstance(body.get("errors"), list), (
        f'контракт требует поле "errors" списком, получено {body.get("errors")!r} ({what})'
    )
