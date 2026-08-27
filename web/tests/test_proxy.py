"""Тесты дев-прокси `web/serve.py`.

Прокси стоит между браузером и API, поэтому его ошибки выглядят как баги
бэкенда: клиент шлёт корректный запрос и получает отказ. Здесь проверяется
именно прозрачность — что прокси ничего не теряет по дороге.

Поднимать настоящий бэкенд не нужно: вместо него заглушка, которая отвечает
эхом из полученных заголовков.

Запуск:
    cd web && python -m pytest tests/test_proxy.py -q
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EchoHandler(BaseHTTPRequestHandler):
    """Заглушка API: возвращает полученные заголовки, тело и метод."""

    def log_message(self, *args):  # тишина в выводе тестов
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        payload = json.dumps({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Total-Count", "42")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_DELETE = _reply


@pytest.fixture(scope="module")
def stack():
    """Заглушка API + прокси поверх неё, оба на свободных портах."""
    api_port, web_port = free_port(), free_port()

    api = ThreadingHTTPServer(("127.0.0.1", api_port), EchoHandler)
    threading.Thread(target=api.serve_forever, daemon=True).start()

    os.environ["KANBAN_API"] = f"http://127.0.0.1:{api_port}"
    sys.path.insert(0, WEB_DIR)
    for stale in ("serve",):
        sys.modules.pop(stale, None)
    import serve  # импорт после KANBAN_API: адрес читается на импорте

    web = ThreadingHTTPServer(("127.0.0.1", web_port), serve.Handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()

    yield f"http://127.0.0.1:{web_port}"

    web.shutdown()
    api.shutdown()


def call(base, path, method="GET", headers=None, body=None):
    req = urllib.request.Request(
        f"{base}{path}",
        data=body.encode() if isinstance(body, str) else body,
        method=method,
    )
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req) as r:
        return r.status, dict(r.headers), json.loads(r.read())


def test_custom_header_reaches_api(stack):
    """Регрессия: X-Confirm-Replace терялся, и mode=replace получал 428.

    Прокси фильтровал заголовки белым списком (Content-Type + Accept), поэтому
    подтверждение разрушительного импорта из #54/#57 не доезжало до сервера.
    """
    _, _, echo = call(
        stack,
        "/api/v1/import?mode=replace",
        method="POST",
        headers={"Content-Type": "application/json", "X-Confirm-Replace": "yes"},
        body='{"format_version":1,"cards":[]}',
    )
    assert echo["headers"].get("x-confirm-replace") == "yes"


def test_auth_headers_reach_api(stack):
    """Токен записи тоже не должен теряться — иначе клиент ловит 403."""
    _, _, echo = call(
        stack,
        "/api/v1/cards",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token",
            "X-Kanban-Token": "write-me",
        },
        body='{"title":"x"}',
    )
    assert echo["headers"].get("authorization") == "Bearer secret-token"
    assert echo["headers"].get("x-kanban-token") == "write-me"


def test_body_and_method_preserved(stack):
    status, _, echo = call(
        stack,
        "/api/v1/cards/1",
        method="PATCH",
        headers={"Content-Type": "application/json"},
        body='{"status":"done"}',
    )
    assert status == 200
    assert echo["method"] == "PATCH"
    assert echo["body"] == '{"status":"done"}'


def test_hop_by_hop_headers_not_forwarded(stack):
    """Host не подменяет адрес бэкенда, Connection не утекает дальше."""
    _, _, echo = call(
        stack,
        "/api/v1/cards",
        headers={"Connection": "keep-alive", "X-Keep": "1"},
    )
    assert "connection" not in echo["headers"] or echo["headers"]["connection"] != "keep-alive"
    assert echo["headers"].get("x-keep") == "1", "обычные заголовки должны проходить"
    assert "127.0.0.1" in echo["headers"].get("host", ""), "Host должен быть адресом API"


def test_response_headers_pass_back(stack):
    """X-Total-Count нужен доске для определения неполной выдачи (#24)."""
    _, headers, _ = call(stack, "/api/v1/cards")
    assert headers.get("X-Total-Count") == "42"


def test_non_ascii_query_survives(stack):
    """Кириллица в query не должна ронять прокси (регрессия #46).

    Браузер отправляет её уже процент-кодированной, поэтому и здесь запрос
    идёт в том же виде: проверяем, что прокси доносит значение без искажений.
    Сырой не-ASCII в строку запроса не кладём — этого не делает и клиент.
    """
    status, _, echo = call(stack, "/api/v1/cards?q=%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82")
    assert status == 200
    assert "%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82" in echo["path"], echo["path"]
