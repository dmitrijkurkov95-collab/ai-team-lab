/**
 * Очередь отложенных операций: доска остаётся живой, когда API недоступен.
 *
 * Принцип, по которому решается «копить или откатывать» — идемпотентность:
 *
 *   - Перемещение карточки (PATCH status) идемпотентно: повторить «переставить
 *     в done» можно сколько угодно раз, результат один. Такие операции копим
 *     и повторяем автоматически.
 *   - Создание и удаление автоматически НЕ повторяем: если ответ не дошёл,
 *     неизвестно, применился ли запрос, и повтор POST рискует создать дубль.
 *     Пользователь увидит ошибку и решит сам.
 *
 * Очередь живёт только в памяти. Переживать перезагрузку она сознательно не
 * умеет: сохранённая правка может относиться к карточке, которую уже удалили
 * с другого клиента, и её приходится инвалидировать — это отдельная задача,
 * а не часть устойчивости к падению сети.
 */
import { ApiError } from './api.js';

/** Пауза между попытками: 1с, 2с, 5с, 10с, дальше каждые 15с. */
const BACKOFF_STEPS = [1000, 2000, 5000, 10000, 15000];

export class SyncQueue {
  /**
   * @param {(op: object) => Promise<any>} perform выполняет операцию через API
   * @param {(state: object) => void} onChange уведомляет UI о смене состояния
   */
  constructor(perform, onChange) {
    this.perform = perform;
    this.onChange = onChange || (() => {});
    /** @type {Array<{id:number, kind:string, cardId:number, payload:object, attempts:number}>} */
    this.pending = [];
    this.flushing = false;
    this.timer = null;
    this.attempt = 0;
    this.lastError = null;
    this.seq = 0;
  }

  get size() {
    return this.pending.length;
  }

  /** Состояние для индикатора в шапке. */
  get state() {
    if (this.flushing) return 'syncing';
    if (this.pending.length) return 'pending';
    return 'synced';
  }

  notify() {
    this.onChange({
      state: this.state,
      size: this.pending.length,
      lastError: this.lastError,
      nextRetryMs: this.timer ? this.currentBackoff() : null,
    });
  }

  currentBackoff() {
    return BACKOFF_STEPS[Math.min(this.attempt, BACKOFF_STEPS.length - 1)];
  }

  /**
   * Поставить операцию в очередь.
   *
   * Схлопывание: несколько перемещений одной карточки бессмысленно повторять
   * по очереди — важен только последний статус. Заменяем предыдущую операцию,
   * иначе после долгого офлайна доска отыграет всю историю перетаскиваний.
   */
  enqueue(kind, cardId, payload) {
    if (kind === 'move') {
      const existing = this.pending.find((op) => op.kind === 'move' && op.cardId === cardId);
      if (existing) {
        existing.payload = payload;
        this.notify();
        this.schedule();
        return existing;
      }
    }
    const op = { id: ++this.seq, kind, cardId, payload, attempts: 0 };
    this.pending.push(op);
    this.notify();
    this.schedule();
    return op;
  }

  /** Немедленная попытка (кнопка «повторить сейчас» или возврат сети). */
  async flushNow() {
    this.clearTimer();
    this.attempt = 0;
    await this.flush();
  }

  clearTimer() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  schedule(delay = 0) {
    if (this.flushing || this.timer || !this.pending.length) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, delay);
    this.notify();
  }

  /**
   * Прогнать очередь. Операции идут по порядку; первая же сетевая ошибка
   * останавливает проход — если сети нет, остальные тоже не пройдут.
   */
  async flush() {
    if (this.flushing || !this.pending.length) return;
    this.flushing = true;
    this.notify();

    let networkFailure = false;
    while (this.pending.length) {
      const op = this.pending[0];
      op.attempts += 1;
      try {
        await this.perform(op);
        this.pending.shift();
        this.lastError = null;
      } catch (error) {
        const isNetwork = error instanceof ApiError && error.status === 0;
        const isServer = error instanceof ApiError && error.status >= 500;
        if (isNetwork || isServer) {
          // Сеть или сервер лежит — ждём и пробуем снова, операцию не теряем.
          this.lastError = error;
          networkFailure = true;
          break;
        }
        // 4xx: сервер понял запрос и отказал. Повтор не поможет — снимаем
        // операцию с очереди и отдаём ошибку наверх, чтобы UI откатил.
        this.pending.shift();
        this.lastError = error;
        op.reject && op.reject(error);
        if (op.onPermanentFailure) op.onPermanentFailure(error);
      }
    }

    this.flushing = false;
    if (networkFailure && this.pending.length) {
      const delay = this.currentBackoff();
      this.attempt += 1;
      this.schedule(delay);
    } else {
      this.attempt = 0;
    }
    this.notify();
  }

  /** Сбросить очередь (например, пользователь отказался от накопленных правок). */
  discard() {
    const dropped = this.pending.splice(0, this.pending.length);
    this.clearTimer();
    this.attempt = 0;
    this.lastError = null;
    this.notify();
    return dropped;
  }
}

export default SyncQueue;
