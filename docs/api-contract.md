# Контракт API канбан-доски

Base URL: `http://localhost:8000/api/v1`
Формат: JSON везде. Ошибки: `404 {"detail": "..."}` для отсутствующего, `422` для невалидного тела.

## Сущность Card

```json
{
  "id": 42,
  "title": "Сделать дизайн колонок",
  "description": "optional, may be empty",
  "status": "backlog",
  "priority": "normal",
  "created_at": "2026-08-26T12:00:00Z",
  "updated_at": "2026-08-26T12:30:00Z"
}
```

- `status`: `backlog` | `in_progress` | `review` | `done`
- `priority`: `low` | `normal` | `high`
- `title`: непустая строка до 200 символов

## Эндпоинты

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/cards` | список всех; опционально `?status=` фильтр |
| POST | `/cards` | создать; тело: `{title, description?, priority?}` (defaults: empty, `normal`) |
| GET | `/cards/{id}` | одна карточка |
| PATCH | `/cards/{id}` | частичное обновление любых полей; смена `status` = перемещение по доске |
| DELETE | `/cards/{id}` | удалить, `204` |
| POST | `/cards/{id}/comments` | добавить комментарий `{text}` |
| GET | `/cards/{id}/comments` | список комментариев к карточке |

## Правила поведения

- `PATCH` с невалидным `status`/`priority` → `422`
- `POST /cards` с пустым `title` или >200 символов → `422`
- Списки возвращают массивы по возрастанию `id`; комментарии — по времени создания
- Часовые метки — ISO 8601 UTC

Контракт может расширяться (спринт 2+: поиск, теги, дедлайны) — изменения только через PR в этот файл с апрувом лида.
