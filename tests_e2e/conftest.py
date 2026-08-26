"""Подъём полного стенда для e2e: server/run.py + web/serve.py как подпроцессы.

Почему отдельная директория от tests/: у сьютов разный контракт на окружение.
Контракт-сьюту нужен один живой API, здесь — два процесса в правильном порядке
старта. Держать их в одной папке значит красить одну джобу CI по двум разным
причинам и заставлять локальный запуск `pytest tests` поднимать фронтенд.

Порты выбираются свободными программно: в CI несколько джоб идут на одном
раннере, и зашитый 8000/5173 однажды упадёт на ровном месте.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url: str, proc: subprocess.Popen, timeout: float = 25.0) -> None:
    """Ждём, пока процесс начнёт отвечать.

    Если процесс умер, не досиживаем таймаут — падаем сразу. Иначе отсутствующий
    serve.py превращается в 25 секунд молчания на каждый запуск.
    """
    deadline = time.time() + timeout
    last = "не начали опрашивать"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"процесс {proc.args} умер сразу с кодом {proc.returncode}, {url} не поднимется"
            )
        try:
            httpx.get(url, timeout=2)
            return
        except httpx.HTTPError as e:
            last = str(e)
        time.sleep(0.3)
    raise RuntimeError(f"{url} не ожил за {timeout}с: {last}")


def _drain_logs(procs: list[subprocess.Popen]) -> str:
    """Логи процессов для диагностики. Вызывать ТОЛЬКО после остановки процессов.

    Это не косметика: `p.stdout.read()` на живом процессе блокируется до EOF,
    то есть навсегда. Первая версия фикстуры читала логи до terminate — и при
    сломанном стенде pytest висел до таймаута вместо внятного падения.
    """
    parts = []
    for p in procs:
        out = ""
        if p.stdout is not None:
            try:
                out = p.stdout.read() or ""
            except ValueError:  # pipe уже закрыт
                out = "<лог недоступен>"
        parts.append(f"--- {' '.join(map(str, p.args))} (код {p.returncode}) ---\n{out}")
    return "\n".join(parts)


def _stop(procs: list[subprocess.Popen]) -> None:
    """Остановить процессы. Идемпотентно: зовётся и из ветки ошибки, и из finally."""
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    """Полный стенд: API на своём порту, фронтенд-прокси на своём, одна сессия на прогон."""
    api_port = free_port()
    web_port = free_port()
    db = tmp_path_factory.mktemp("e2e") / "kanban.db"

    server_env = {
        **os.environ,
        "KANBAN_DB": str(db),
        "KANBAN_PORT": str(api_port),
        "KANBAN_HOST": "127.0.0.1",
    }
    procs: list[subprocess.Popen] = []
    try:
        server = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=ROOT / "server",
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(server)

        web = subprocess.Popen(
            [sys.executable, "serve.py", "--port", str(web_port)],
            cwd=ROOT / "web",
            env={**os.environ, "KANBAN_API": f"http://127.0.0.1:{api_port}"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(web)

        try:
            wait_ready(f"http://127.0.0.1:{api_port}/api/v1/cards", server)
            wait_ready(f"http://127.0.0.1:{web_port}/", web)
        except RuntimeError as e:
            # Сначала останавливаем, потом читаем логи: read() на живом процессе
            # блокируется навсегда и вешает весь pytest вместо внятной ошибки.
            _stop(procs)
            raise RuntimeError(f"{e}\nЛоги процессов:\n{_drain_logs(procs)}") from None

        yield {
            "api_port": api_port,
            "web_port": web_port,
            "origin": f"http://127.0.0.1:{web_port}",
            "api_direct": f"http://127.0.0.1:{api_port}/api/v1",
            "api_proxied": f"http://127.0.0.1:{web_port}/api/v1",
        }
    finally:
        _stop(procs)


@pytest.fixture()
def origin(stack):
    """Origin стенда — тот самый, с которого доску видит браузер."""
    return stack["origin"]


@pytest.fixture()
def proxied(stack):
    """Клиент, который ходит ТОЛЬКО через прокси фронтенда, одним origin."""
    with httpx.Client(base_url=f"{stack['origin']}/api/v1", timeout=10) as c:
        yield c


@pytest.fixture()
def direct(stack):
    """Клиент напрямую в API — чтобы сравнивать с проксированным ответом."""
    with httpx.Client(base_url=stack["api_direct"], timeout=10) as c:
        yield c
