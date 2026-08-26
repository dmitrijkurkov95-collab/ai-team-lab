/**
 * Адаптер API канбан-доски. Единственное место во фронтенде, которое знает
 * про HTTP и структуру эндпоинтов — остальной код работает с этими функциями.
 *
 * Контракт: docs/api-contract.md, base URL /api/v1.
 *
 * Переключение на другой хост — одна правка BASE_URL (или ?api= в адресе,
 * удобно, когда страница открыта как файл, а сервер живёт на другом порту).
 */

const DEFAULT_BASE = 'http://localhost:8000/api/v1';

/** ?api=http://host:port/api/v1 переопределяет базовый URL без правки кода. */
function resolveBase() {
  try {
    const fromQuery = new URLSearchParams(window.location.search).get('api');
    if (fromQuery) return fromQuery.replace(/\/+$/, '');
  } catch (_) {
    /* window может отсутствовать при прогоне в node — не страшно */
  }
  // Страница отдана по HTTP → ходим относительным путём на свой же origin.
  // serve.py проксирует /api на бэкенд, поэтому доска работает и на удалённом
  // стенде, где localhost:8000 браузеру недоступен.
  try {
    if (window.location.protocol.startsWith('http')) return '/api/v1';
  } catch (_) {
    /* игнорируем */
  }
  // Открыли файлом (file://) — идём напрямую на локальный бэкенд.
  return DEFAULT_BASE;
}

export const BASE_URL = resolveBase();

/** Ошибка API с кодом статуса — интерфейсу есть что показать пользователю. */
export class ApiError extends Error {
  constructor(message, status, details) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

/**
 * Приводит поле detail к человеческой строке.
 *
 * Контракт обещает `{"detail": "..."}` строкой, но FastAPI на 422 отдаёт
 * массив объектов `[{loc, msg, type}]`. Поддерживаем оба формата: расхождение
 * заведено отдельным вопросом к бэкенду, но интерфейс из-за него ломаться не должен.
 */
function formatDetail(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === 'string') return item;
      const loc = Array.isArray(item.loc)
        ? item.loc.filter((p) => p !== 'body' && p !== 'query').join('.')
        : '';
      const msg = item.msg || 'некорректное значение';
      return loc ? `${loc}: ${msg}` : msg;
    });
    return parts.join('; ') || fallback;
  }
  if (typeof detail === 'object') return JSON.stringify(detail);
  return String(detail);
}

async function request(path, { method = 'GET', body } = {}) {
  const url = `${BASE_URL}${path}`;
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (networkError) {
    // Сервер не поднят, не тот порт, CORS — пользователь должен это увидеть.
    throw new ApiError(
      `Нет связи с API (${url}). Сервер запущен?`,
      0,
      networkError.message,
    );
  }

  if (response.status === 204) return null;

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (_) {
      payload = null;
    }
  }

  if (!response.ok) {
    const fallback = `Ошибка ${response.status}`;
    const message = formatDetail(payload && payload.detail, fallback);
    throw new ApiError(message, response.status, payload);
  }

  return payload;
}

// --- карточки ---------------------------------------------------------------

/** Список карточек; пустой статус не отправляем — бэкенд ответит 422 на `?status=`. */
export function listCards(status) {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  return request(`/cards${query}`);
}

export function getCard(id) {
  return request(`/cards/${id}`);
}

export function createCard({ title, description = '', priority = 'normal', status = 'backlog' }) {
  return request('/cards', { method: 'POST', body: { title, description, priority, status } });
}

/** Частичное обновление: шлём только изменённые поля. */
export function updateCard(id, fields) {
  return request(`/cards/${id}`, { method: 'PATCH', body: fields });
}

export function moveCard(id, status) {
  return updateCard(id, { status });
}

export function deleteCard(id) {
  return request(`/cards/${id}`, { method: 'DELETE' });
}

// --- комментарии ------------------------------------------------------------

export function listComments(cardId) {
  return request(`/cards/${cardId}/comments`);
}

export function addComment(cardId, text) {
  return request(`/cards/${cardId}/comments`, { method: 'POST', body: { text } });
}

export const api = {
  BASE_URL,
  listCards,
  getCard,
  createCard,
  updateCard,
  moveCard,
  deleteCard,
  listComments,
  addComment,
};

export default api;
