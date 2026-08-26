/**
 * Логика канбан-доски. Всё общение с сервером идёт через api.js —
 * здесь только состояние, рендер и события.
 */
import api, { ApiError, BASE_URL } from './api.js';

const COLUMNS = [
  { status: 'backlog', label: 'Бэклог' },
  { status: 'in_progress', label: 'В работе' },
  { status: 'review', label: 'Ревью' },
  { status: 'done', label: 'Готово' },
];

const PRIORITY_LABEL = { low: 'низкий', normal: 'обычный', high: 'высокий' };
const STATUS_LABEL = Object.fromEntries(COLUMNS.map((c) => [c.status, c.label]));

/** Единственный источник правды на клиенте. */
const state = {
  cards: [],
  commentCounts: new Map(), // id карточки -> сколько комментариев (для чипа)
  editingId: null,
  detailId: null,
  dragId: null,
};

const $ = (id) => document.getElementById(id);
const board = $('board');

// ---------------------------------------------------------------- уведомления

/** Показываем пользователю, что произошло: молча падать нельзя. */
function toast(message, kind = 'error', title = null) {
  const box = document.createElement('div');
  box.className = `toast ${kind}`;
  const body = document.createElement('div');
  if (title) {
    const strong = document.createElement('strong');
    strong.textContent = title;
    body.appendChild(strong);
  }
  const p = document.createElement('p');
  p.textContent = message;
  body.appendChild(p);
  const close = document.createElement('button');
  close.className = 'ghost';
  close.textContent = '✕';
  close.addEventListener('click', () => box.remove());
  box.append(body, close);
  $('toasts').appendChild(box);
  if (kind !== 'error') setTimeout(() => box.remove(), 3500);
}

/** Единая точка обработки ошибок API — сообщение всегда доходит до человека. */
function reportError(error, context) {
  if (error instanceof ApiError) {
    const title = error.status ? `${context} — ошибка ${error.status}` : context;
    toast(error.message, 'error', title);
  } else {
    toast(String(error && error.message ? error.message : error), 'error', context);
  }
  console.error(context, error);
}

function setStatusLine(text) {
  $('status-line').textContent = text;
}

// ---------------------------------------------------------------- рендер

function priorityBadge(priority) {
  const span = document.createElement('span');
  span.className = `badge ${priority}`;
  span.textContent = PRIORITY_LABEL[priority] || priority;
  return span;
}

function cardNode(card) {
  const el = document.createElement('article');
  el.className = 'card';
  el.draggable = true;
  el.dataset.id = String(card.id);
  el.tabIndex = 0;

  const title = document.createElement('div');
  title.className = 'card-title';
  title.textContent = card.title;

  const meta = document.createElement('div');
  meta.className = 'card-meta';
  meta.appendChild(priorityBadge(card.priority));

  const id = document.createElement('span');
  id.className = 'card-id';
  id.textContent = `#${card.id}`;
  meta.appendChild(id);

  const count = state.commentCounts.get(card.id);
  if (count) {
    const chip = document.createElement('span');
    chip.className = 'comment-chip';
    chip.textContent = `💬 ${count}`;
    meta.append(chip);
  }

  el.append(title, meta);

  el.addEventListener('click', () => openDetail(card.id));
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openDetail(card.id);
    }
  });

  el.addEventListener('dragstart', (e) => {
    state.dragId = card.id;
    el.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(card.id));
  });
  el.addEventListener('dragend', () => {
    state.dragId = null;
    el.classList.remove('dragging');
    document.querySelectorAll('.column.dragover').forEach((c) => c.classList.remove('dragover'));
  });

  return el;
}

function columnNode({ status, label }) {
  const section = document.createElement('section');
  section.className = 'column';
  section.dataset.status = status;

  const head = document.createElement('div');
  head.className = 'column-head';
  const name = document.createElement('span');
  name.textContent = label;
  const count = document.createElement('span');
  count.className = 'count';
  const cards = state.cards.filter((c) => c.status === status);
  count.textContent = String(cards.length);
  head.append(name, count);

  const body = document.createElement('div');
  body.className = 'column-body';
  if (cards.length === 0) {
    const hint = document.createElement('div');
    hint.className = 'empty-hint';
    hint.textContent = 'Пусто — перетащите карточку сюда';
    body.appendChild(hint);
  } else {
    cards.forEach((card) => body.appendChild(cardNode(card)));
  }

  // drag&drop: колонка — зона сброса
  section.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    section.classList.add('dragover');
  });
  section.addEventListener('dragleave', (e) => {
    if (!section.contains(e.relatedTarget)) section.classList.remove('dragover');
  });
  section.addEventListener('drop', (e) => {
    e.preventDefault();
    section.classList.remove('dragover');
    const id = Number(e.dataTransfer.getData('text/plain') || state.dragId);
    if (id) moveCard(id, status);
  });

  section.append(head, body);
  return section;
}

function render() {
  board.replaceChildren(...COLUMNS.map(columnNode));
  setStatusLine(`${state.cards.length} карточек · API ${BASE_URL}`);
}

// ---------------------------------------------------------------- данные

async function loadBoard() {
  setStatusLine('загрузка…');
  try {
    state.cards = await api.listCards();
    render();
    loadCommentCounts(); // не блокируем отрисовку доски
  } catch (error) {
    state.cards = [];
    render();
    setStatusLine('нет связи с API');
    reportError(error, 'Не удалось загрузить доску');
  }
}

/** Счётчики комментариев — украшение, поэтому их сбои не тревожим пользователя. */
async function loadCommentCounts() {
  const results = await Promise.allSettled(
    state.cards.map(async (card) => [card.id, (await api.listComments(card.id)).length]),
  );
  let changed = false;
  for (const r of results) {
    if (r.status === 'fulfilled') {
      const [id, count] = r.value;
      if (state.commentCounts.get(id) !== count) {
        state.commentCounts.set(id, count);
        changed = true;
      }
    }
  }
  if (changed) render();
}

/** Оптимистичный перенос: сразу двигаем в UI, при ошибке откатываем. */
async function moveCard(id, status) {
  const card = state.cards.find((c) => c.id === id);
  if (!card || card.status === status) return;
  const previous = card.status;
  card.status = status;
  render();
  try {
    const updated = await api.moveCard(id, status);
    Object.assign(card, updated);
    render();
  } catch (error) {
    card.status = previous; // откат — доска не должна врать о состоянии сервера
    render();
    reportError(error, `Не удалось перенести карточку #${id}`);
  }
}

// ---------------------------------------------------------------- диалог карточки

const cardDialog = $('card-dialog');

function openCardDialog(card = null) {
  state.editingId = card ? card.id : null;
  $('card-dialog-title').textContent = card ? `Карточка #${card.id}` : 'Новая карточка';
  $('f-title').value = card ? card.title : '';
  $('f-description').value = card ? card.description : '';
  $('f-status').value = card ? card.status : 'backlog';
  $('f-priority').value = card ? card.priority : 'normal';
  $('f-title-error').textContent = '';
  $('btn-delete').hidden = !card;
  cardDialog.showModal();
  $('f-title').focus();
}

$('btn-new').addEventListener('click', () => openCardDialog());
$('btn-refresh').addEventListener('click', loadBoard);
$('btn-cancel').addEventListener('click', () => cardDialog.close());

$('card-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const title = $('f-title').value.trim();
  // Валидацию дублируем на клиенте: незачем гонять заведомо плохой запрос,
  // но серверный 422 всё равно обрабатываем — правда всегда за бэкендом.
  if (!title) {
    $('f-title-error').textContent = 'Заголовок не может быть пустым';
    return;
  }
  if (title.length > 200) {
    $('f-title-error').textContent = 'Не длиннее 200 символов';
    return;
  }
  const payload = {
    title,
    description: $('f-description').value.trim(),
    status: $('f-status').value,
    priority: $('f-priority').value,
  };
  $('btn-save').disabled = true;
  try {
    if (state.editingId) {
      await api.updateCard(state.editingId, payload);
      toast(`Карточка #${state.editingId} сохранена`, 'success');
    } else {
      const created = await api.createCard(payload);
      toast(`Создана карточка #${created.id}`, 'success');
    }
    cardDialog.close();
    await loadBoard();
  } catch (error) {
    if (error instanceof ApiError && error.status === 422) {
      $('f-title-error').textContent = error.message;
    } else {
      reportError(error, 'Не удалось сохранить карточку');
    }
  } finally {
    $('btn-save').disabled = false;
  }
});

$('btn-delete').addEventListener('click', async () => {
  const id = state.editingId;
  if (!id) return;
  if (!confirm(`Удалить карточку #${id}? Комментарии тоже пропадут.`)) return;
  try {
    await api.deleteCard(id);
    state.commentCounts.delete(id);
    cardDialog.close();
    toast(`Карточка #${id} удалена`, 'success');
    await loadBoard();
  } catch (error) {
    reportError(error, `Не удалось удалить карточку #${id}`);
  }
});

// ---------------------------------------------------------------- детали и комментарии

const detailDialog = $('detail-dialog');

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('ru-RU');
}

async function openDetail(id) {
  state.detailId = id;
  const card = state.cards.find((c) => c.id === id);
  if (!card) return;

  $('d-title').textContent = card.title;
  const badge = $('d-priority');
  badge.className = `badge ${card.priority}`;
  badge.textContent = PRIORITY_LABEL[card.priority] || card.priority;
  $('d-status').textContent = `#${card.id} · ${STATUS_LABEL[card.status] || card.status}`;
  $('d-dates').textContent = `создана ${formatDate(card.created_at)}`;
  $('d-description').textContent = card.description || 'Без описания';
  $('c-text').value = '';
  $('d-comments').replaceChildren(Object.assign(document.createElement('p'), {
    className: 'muted',
    textContent: 'загрузка комментариев…',
  }));
  detailDialog.showModal();
  await renderComments(id);
}

async function renderComments(id) {
  const box = $('d-comments');
  try {
    const comments = await api.listComments(id);
    state.commentCounts.set(id, comments.length);
    if (comments.length === 0) {
      box.replaceChildren(Object.assign(document.createElement('p'), {
        className: 'muted',
        textContent: 'Комментариев пока нет',
      }));
      return;
    }
    box.replaceChildren(...comments.map((c) => {
      const div = document.createElement('div');
      div.className = 'comment';
      const text = document.createElement('div');
      text.textContent = c.text;
      const time = document.createElement('time');
      time.dateTime = c.created_at;
      time.textContent = formatDate(c.created_at);
      div.append(text, time);
      return div;
    }));
  } catch (error) {
    box.replaceChildren(Object.assign(document.createElement('p'), {
      className: 'muted',
      textContent: 'Комментарии не загрузились',
    }));
    reportError(error, 'Не удалось загрузить комментарии');
  }
}

$('comment-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('c-text');
  const text = input.value.trim();
  const id = state.detailId;
  if (!text || !id) return;
  input.disabled = true;
  try {
    await api.addComment(id, text);
    input.value = '';
    await renderComments(id);
    render();
  } catch (error) {
    reportError(error, 'Не удалось добавить комментарий');
  } finally {
    input.disabled = false;
    input.focus();
  }
});

$('btn-detail-close').addEventListener('click', () => detailDialog.close());
$('btn-edit').addEventListener('click', () => {
  const card = state.cards.find((c) => c.id === state.detailId);
  detailDialog.close();
  if (card) openCardDialog(card);
});

// ---------------------------------------------------------------- старт

loadBoard();
