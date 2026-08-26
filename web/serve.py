#!/usr/bin/env python3
"""Дев-сервер фронтенда: статика + прокси на API.

Зачем прокси: когда доска открыта не с той же машины, где крутится бэкенд
(удалённый стенд, превью, другой хост), браузеру недоступен localhost:8000.
Отдаём страницу и API с одного origin — фронт ходит по относительному пути
`/api/v1`, CORS и разъезд портов перестают быть проблемой.

    python3 serve.py                      # :5173, API берётся с localhost:8000
    KANBAN_API=http://host:9000 python3 serve.py --port 8080
"""
from __future__ import annotations

import argparse
import os
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
API_TARGET = os.environ.get("KANBAN_API", "http://localhost:8000").rstrip("/")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}


class Handler(SimpleHTTPRequestHandler):
    """Статика из web/, всё под /api — на бэкенд."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # Тихий лог: интересуют только ошибки, а не каждый GET картинки.
    def log_message(self, fmt, *args):  # noqa: A003
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            super().log_message(fmt, *args)

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = f"{API_TARGET}{self.path}"

        req = urllib.request.Request(url, data=body, method=self.command)
        ctype = self.headers.get("Content-Type")
        if ctype:
            req.add_header("Content-Type", ctype)
        req.add_header("Accept", self.headers.get("Accept", "application/json"))

        try:
            with urllib.request.urlopen(req) as upstream:
                payload = upstream.read()
                self.send_response(upstream.status)
                for key, value in upstream.headers.items():
                    if key.lower() not in HOP_BY_HOP:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            # 404/422 — нормальная часть контракта, проксируем как есть.
            payload = exc.read()
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except urllib.error.URLError as exc:
            message = f'{{"detail": "API недоступен по адресу {API_TARGET}: {exc.reason}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/"):
            return self._proxy()
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        return self._proxy() if self.path.startswith("/api/") else self.send_error(405)

    def do_PATCH(self):  # noqa: N802
        return self._proxy() if self.path.startswith("/api/") else self.send_error(405)

    def do_DELETE(self):  # noqa: N802
        return self._proxy() if self.path.startswith("/api/") else self.send_error(405)

    def end_headers(self):
        # Дев-сервер: не даём браузеру кэшировать правки.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Дев-сервер канбан-доски")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5173")))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[web] доска: http://localhost:{args.port}")
    print(f"[web] /api/* проксируется на {API_TARGET}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] остановлен")


if __name__ == "__main__":
    main()
