# Канбан-доска

Доска задач: колонки, карточки с приоритетом и сроком, комментарии, drag&drop. Репозиторий команды ИИ-агентов разработчиков, лид — team-lead-agent.

## Быстрый старт

**С Docker** — ничего, кроме Docker, ставить не нужно:

```bash
docker compose up
```

**Без Docker** — нужен только Python 3.11+:

```bash
./run.sh
```

Оба варианта поднимают весь стенд и открывают доску на **http://localhost:5173**. API и статика отдаются с одного адреса, поэтому второй порт наружу пробрасывать не нужно.

```bash
PORT=8080 ./run.sh        # другой порт
./run.sh --fresh          # начать с чистой базы
docker compose down       # остановить (данные сохранятся)
docker compose down -v    # остановить и удалить данные
```

Данные переживают перезапуск: `./run.sh` хранит базу в `./data/`, compose — в volume `kanban-data`. Пустая доска при первом запуске засевается демо-карточками.

## Безопасность

По умолчанию **API открыт на запись без авторизации** — так удобнее для локального стенда и демо. Это осознанный выбор, а не упущение.

**Перед тем как выставить доску наружу**, задайте токен — тогда все изменения потребуют заголовка `X-Auth-Token`, а чтение останется открытым:

```bash
KANBAN_WRITE_TOKEN=выберите-длинную-строку ./run.sh
curl -X POST http://localhost:5173/api/v1/cards \
     -H 'X-Auth-Token: выберите-длинную-строку' \
     -H 'Content-Type: application/json' -d '{"title": "Задача"}'
```

Импорт с заменой доски дополнительно требует подтверждения — случайный запрос данные не сотрёт:

```bash
curl -X POST 'http://localhost:5173/api/v1/import?mode=replace' \
     -H 'X-Confirm-Replace: yes' -H 'Content-Type: application/json' -d @board.json
```

Подробности и коды ответов — в [контракте](docs/api-contract.md).

## Что внутри

| Каталог | Что там |
|---|---|
| `server/` | REST API: FastAPI + SQLite ([запуск и детали](server/README.md)) |
| `web/` | Доска: ванильный JS, без сборки ([детали](web/README.md)) |
| `docs/api-contract.md` | **Контракт API** — источник правды для сервера, фронта и тестов |
| `tests/`, `tests_e2e/` | Контракт-тесты и e2e полного стенда |

## API коротко

Base URL `/api/v1`, полное описание — в [контракте](docs/api-contract.md).

```bash
curl http://localhost:5173/api/v1/cards                          # все карточки
curl 'http://localhost:5173/api/v1/cards?status=done&limit=10'   # фильтр и лимит
curl 'http://localhost:5173/api/v1/cards?q=дизайн'               # поиск
curl 'http://localhost:5173/api/v1/cards?sort=deadline&limit=5'  # ближайшие сроки
```

Интерактивная документация — http://localhost:5173/api/v1/../docs (или напрямую на API-порту `/docs`).

## Тесты

```bash
cd server && python3 -m pytest tests/ -q   # тесты сервера
python3 -m pytest tests/ -q                 # контракт-тесты (нужен поднятый стенд)
python3 -m pytest tests_e2e/ -q             # e2e: поднимает стенд сам
```

## Как мы работаем

- Задачи живут в [Issues](../../issues), работа — в ветках `feature/*`, `fix/*`, `refactor/*`
- В `main` попадает только через Pull Request после ревью
- Процесс и роли: [CONTRIBUTING.md](CONTRIBUTING.md) и [docs/team.md](docs/team.md)
