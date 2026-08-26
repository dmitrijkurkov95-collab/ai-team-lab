"""Тесты сервера: свои сценарии Agent-1.

Контракт-сьют целиком — зона Agent-3 в tests/. Здесь проверяем то, за что
отвечаю я: что моя реализация делает обещанное, включая крайние случаи,
где контракт допускал трактовку.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API = "/api/v1"


@pytest.fixture()
def client(monkeypatch):
    """Свежая БД на каждый тест: временный файл + сброс соединения."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("KANBAN_DB", tmp.name)

    import db
    import main

    db.reset_conn()
    monkeypatch.setattr(db, "DB_PATH", tmp.name)
    with TestClient(main.app) as c:
        yield c
    db.reset_conn()
    os.unlink(tmp.name)


# --- seed и список ----------------------------------------------------------

def test_seed_creates_cards_in_different_statuses(client):
    cards = client.get(f"{API}/cards").json()
    assert 5 <= len(cards) <= 6
    assert len({c["status"] for c in cards}) >= 3


def test_seed_not_duplicated_on_restart(client):
    before = len(client.get(f"{API}/cards").json())
    import db
    db.seed_if_empty(db.get_conn())  # повторный вызов на непустой базе
    assert len(client.get(f"{API}/cards").json()) == before


def test_cards_sorted_by_id(client):
    ids = [c["id"] for c in client.get(f"{API}/cards").json()]
    assert ids == sorted(ids)


def test_filter_by_status(client):
    client.post(f"{API}/cards", json={"title": "Отфильтруй меня", "status": "review"})
    cards = client.get(f"{API}/cards", params={"status": "review"}).json()
    assert cards and all(c["status"] == "review" for c in cards)


def test_filter_by_bogus_status_is_422(client):
    assert client.get(f"{API}/cards", params={"status": "нет-такого"}).status_code == 422


# --- создание ---------------------------------------------------------------

def test_create_card_defaults(client):
    r = client.post(f"{API}/cards", json={"title": "Только заголовок"})
    assert r.status_code == 201
    card = r.json()
    assert card["description"] == ""
    assert card["priority"] == "normal"
    assert card["status"] == "backlog"
    assert card["created_at"].endswith("Z")


def test_create_card_trims_title(client):
    card = client.post(f"{API}/cards", json={"title": "  с пробелами  "}).json()
    assert card["title"] == "с пробелами"


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_create_card_invalid_title_is_422(client, title):
    r = client.post(f"{API}/cards", json={"title": title})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)  # контракт: detail — строка


def test_create_card_title_exactly_200_ok(client):
    r = client.post(f"{API}/cards", json={"title": "y" * 200})
    assert r.status_code == 201


def test_create_card_bogus_priority_is_422(client):
    assert client.post(f"{API}/cards", json={"title": "ок", "priority": "срочно"}).status_code == 422


# --- чтение и обновление ----------------------------------------------------

def test_get_missing_card_is_404(client):
    r = client.get(f"{API}/cards/999999")
    assert r.status_code == 404
    assert isinstance(r.json()["detail"], str)


def test_patch_moves_card_between_statuses(client):
    card = client.post(f"{API}/cards", json={"title": "Переезд"}).json()
    moved = client.patch(f"{API}/cards/{card['id']}", json={"status": "done"}).json()
    assert moved["status"] == "done"
    assert moved["updated_at"] >= card["updated_at"]


def test_patch_partial_keeps_other_fields(client):
    card = client.post(f"{API}/cards", json={"title": "Было", "description": "текст"}).json()
    patched = client.patch(f"{API}/cards/{card['id']}", json={"title": "Стало"}).json()
    assert patched["title"] == "Стало"
    assert patched["description"] == "текст"


def test_patch_empty_body_is_noop(client):
    card = client.post(f"{API}/cards", json={"title": "Без изменений"}).json()
    r = client.patch(f"{API}/cards/{card['id']}", json={})
    assert r.status_code == 200
    assert r.json() == card  # включая updated_at: ничего не менялось


@pytest.mark.parametrize("payload", [{"status": "халтура"}, {"priority": "ultra"}, {"title": ""}])
def test_patch_invalid_values_is_422(client, payload):
    card = client.post(f"{API}/cards", json={"title": "Цель"}).json()
    assert client.patch(f"{API}/cards/{card['id']}", json=payload).status_code == 422


def test_patch_missing_card_is_404(client):
    assert client.patch(f"{API}/cards/999999", json={"status": "done"}).status_code == 404


# --- удаление ---------------------------------------------------------------

def test_delete_card(client):
    card = client.post(f"{API}/cards", json={"title": "На удаление"}).json()
    assert client.delete(f"{API}/cards/{card['id']}").status_code == 204
    assert client.get(f"{API}/cards/{card['id']}").status_code == 404


def test_delete_missing_card_is_404(client):
    assert client.delete(f"{API}/cards/999999").status_code == 404


def test_delete_card_removes_its_comments(client):
    card = client.post(f"{API}/cards", json={"title": "С комментариями"}).json()
    client.post(f"{API}/cards/{card['id']}/comments", json={"text": "первый"})
    client.delete(f"{API}/cards/{card['id']}")
    import db
    assert db.list_comments(card["id"]) == []


# --- комментарии ------------------------------------------------------------

def test_add_and_list_comments_in_order(client):
    card = client.post(f"{API}/cards", json={"title": "Обсуждение"}).json()
    for text in ("раз", "два", "три"):
        assert client.post(f"{API}/cards/{card['id']}/comments", json={"text": text}).status_code == 201
    comments = client.get(f"{API}/cards/{card['id']}/comments").json()
    assert [c["text"] for c in comments] == ["раз", "два", "три"]
    assert all(c["card_id"] == card["id"] for c in comments)


def test_comments_empty_for_new_card(client):
    card = client.post(f"{API}/cards", json={"title": "Тишина"}).json()
    assert client.get(f"{API}/cards/{card['id']}/comments").json() == []


def test_comment_empty_text_is_422(client):
    card = client.post(f"{API}/cards", json={"title": "Цель"}).json()
    assert client.post(f"{API}/cards/{card['id']}/comments", json={"text": "   "}).status_code == 422


def test_comments_on_missing_card_are_404(client):
    assert client.get(f"{API}/cards/999999/comments").status_code == 404
    assert client.post(f"{API}/cards/999999/comments", json={"text": "ау"}).status_code == 404


# --- персистентность --------------------------------------------------------

def test_data_survives_reconnect(client):
    card = client.post(f"{API}/cards", json={"title": "Переживи рестарт"}).json()
    import db
    db.reset_conn()  # имитируем перезапуск процесса на том же файле БД
    assert db.get_card(card["id"])["title"] == "Переживи рестарт"


def test_empty_status_param_means_no_filter(client):
    """`?status=` — не фильтр, а его отсутствие: пустой select на фронте не ошибка."""
    r = client.get(f"{API}/cards", params={"status": ""})
    assert r.status_code == 200
    assert len(r.json()) == len(client.get(f"{API}/cards").json())
