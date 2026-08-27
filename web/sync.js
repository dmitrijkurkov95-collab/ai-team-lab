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
 * Мир многопользовательский, поэтому операция может потерять смысл, пока ждёт
 * отправки: карточку удалили или изменили с другого клиента. Каждая операция
 * заканчивается явным исходом (см. OpOutcome) — вечных повторов не бывает.
 *
 * Очередь живёт только в памяти: переживание перезагрузки требует сверки
 * накопленного состояния с серверным, это отдельная задача.
 */
import { ApiError } from './api.js';

/** Пауза между попытками: 1с, 2с, 5с, 10с, дальше каждые 15с. */
const BACKOFF_STEPS = [1000, 2000, 5000, 10000, 15000];

/**
 * Исход операции — почему она ушла из очереди. UI выбирает по нему реакцию.
 *
 *   done        — применилась, всё хорошо
 *   gone        — объекта больше нет (404/410): правка потеряла смысл,
 *                 повторять нечего, карточку надо убрать с доски
 *   conflict    — сервер отверг из-за расхождения состояния (409/412):
 *                 кто-то изменил карточку раньше нас
 *   overwritten — применилась, но затёрла чужую правку (см. detectOverwrite)
 *   rejected    — прочий отказ 4xx: невалидные данные и подобное
 */
export const OpOutcome = {
  DONE: 'done',
  GONE: 'gone',
  CONFLICT: 'conflict',
  OVERWRITTEN: 'overwritten',
  REJECTED: 'rejected',
};

/** Коды, после которых объект считается исчезнувшим безвозвратно. */
const GONE_CODES = new Set([404, 410]);
/** Коды конфликта состояния. Сервер их сегодня не отдаёт — см. README. */
const CONFLICT_CODES = new Set([409, 412]);

export class SyncQueue {
  /**
   * @param {(op: object) => Promise<any>} perform выполняет операцию через API
   * @param {(state: object) => void} onChange уведомляет UI о смене состояния
   * @param {(op: object, outcome: string, info: object) => void} [onOutcome]
   *        вызывается, когда операция покидает очередь: применилась,
   *        инвалидирована, конфликтует или затёрла чужую правку
   */
  constructor(perform, onChange, onOutcome) {
    this.perform = perform;
    this.onChange = onChange || (() => {});
    this.onOutcome = onOutcome || (() => {});
    /** @type {Array<{id:number, kind:string, cardId:number, payload:object, attempts:number}>} */
    this.pending = [];
    this.flushing = false;
    this.timer = null;
    this.attempt = 0;
    this.lastError = null;
    /** Пауза, назначенная сервером через Retry-After (мс), если он её назвал. */
    this.retryAfterMs = null;
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
  enqueue(kind, cardId, payload, baseline = null) {
    if (kind === 'move') {
      const existing = this.pending.find((op) => op.kind === 'move' && op.cardId === cardId);
      if (existing) {
        existing.payload = payload;
        // baseline не трогаем: сравнивать нужно с состоянием, которое клиент
        // видел, когда начал править, а не с промежуточным своим же шагом.
        this.notify();
        this.schedule();
        return existing;
      }
    }
    const op = { id: ++this.seq, kind, cardId, payload, attempts: 0, baseline };
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
        const result = await this.perform(op);
        this.pending.shift();
        this.lastError = null;
        // Успех — но, возможно, мы затёрли чужую правку. Молчать об этом нельзя.
        const overwritten = detectOverwrite(op, result);
        this.settle(op, overwritten ? OpOutcome.OVERWRITTEN : OpOutcome.DONE, {
          result,
          previous: overwritten,
        });
      } catch (error) {
        const status = error instanceof ApiError ? error.status : null;
        const isNetwork = status === 0;
        const isServer = status !== null && status >= 500;
        if (isNetwork || isServer) {
          // Сеть или сервер лежит — ждём и пробуем снова, операцию не теряем.
          // 503 с Retry-After — не поломка, а «занято»: сервер сам называет
          // паузу (очередь писателей SQLite, issue #47). Своим backoff в этом
          // случае не гадаем.
          this.lastError = error;
          this.retryAfterMs = error && error.retryAfterMs ? error.retryAfterMs : null;
          networkFailure = true;
          break;
        }

        // Дальше — окончательные отказы: повторять бессмысленно, снимаем с очереди.
        this.pending.shift();
        this.lastError = error;

        if (GONE_CODES.has(status)) {
          // Карточку удалили с другого клиента, пока мы копили правку.
          // Правка потеряла объект — инвалидируем её и все прочие к той же карточке.
          const alsoDropped = this.dropOperationsFor(op.cardId);
          this.settle(op, OpOutcome.GONE, { error, alsoDropped });
        } else if (CONFLICT_CODES.has(status)) {
          this.settle(op, OpOutcome.CONFLICT, { error });
        } else {
          this.settle(op, OpOutcome.REJECTED, { error });
        }
      }
    }

    this.flushing = false;
    if (networkFailure && this.pending.length) {
      // Указание сервера (Retry-After) важнее нашей догадки.
      const delay = this.retryAfterMs || this.currentBackoff();
      this.attempt += 1;
      this.schedule(delay);
    } else {
      this.attempt = 0;
      this.retryAfterMs = null;
    }
    this.notify();
  }

  /**
   * Операция покинула очередь: сообщаем и ей самой, и подписчику.
   *
   * Колбэки на самой операции оставлены для обратной совместимости с кодом,
   * который вешал `onPermanentFailure` на результат `enqueue()`.
   */
  settle(op, outcome, info = {}) {
    op.outcome = outcome;
    if (outcome !== OpOutcome.DONE && outcome !== OpOutcome.OVERWRITTEN) {
      if (op.reject) op.reject(info.error);
      if (op.onPermanentFailure) op.onPermanentFailure(info.error, outcome);
    }
    if (op.onOutcome) op.onOutcome(outcome, info);
    this.onOutcome(op, outcome, info);
  }

  /**
   * Снять из очереди все операции по карточке — нужно, когда её удалили:
   * остальные правки к несуществующему объекту тоже обречены на 404.
   */
  dropOperationsFor(cardId) {
    const dropped = [];
    this.pending = this.pending.filter((op) => {
      if (op.cardId === cardId) {
        dropped.push(op);
        return false;
      }
      return true;
    });
    return dropped;
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

/**
 * Затёрла ли наша правка чужую?
 *
 * Версионирования в контракте нет (ни `version`, ни `ETag`), поэтому сервер не
 * может отвергнуть устаревшую запись — он всегда применяет последнюю. Политика
 * получается last-write-wins не по выбору, а по устройству API.
 *
 * Единственный доступный признак постфактум: `updated_at` карточки на момент
 * постановки операции в очередь против `updated_at`, который вернул сервер.
 * Наша правка двигает метку сама, поэтому сравниваем не с ответом, а с тем, что
 * было ДО нашего запроса — эту величину знает только вызывающий код, он и кладёт
 * её в `baseline`. Если карточка изменилась между постановкой и отправкой,
 * значит между ними влезла чужая запись, и мы её перекрыли.
 *
 * Метод честно неточен на дистанции меньше секунды: контракт хранит метки с
 * точностью до секунды, так что правка, пришедшая в ту же секунду, не заметится.
 * Надёжное решение — версии на сервере, это отдельная задача бэкенда.
 *
 * @returns {object|null} снимок чужого состояния, если перезапись обнаружена
 */
export function detectOverwrite(op, result) {
  if (!op.baseline || !result) return null;
  const before = op.baseline.updated_at;
  const seenByServer = op.baseline.serverUpdatedAt;
  if (!before || !seenByServer) return null;
  // serverUpdatedAt — метка, которую вернул сервер ПЕРЕД нашей записью.
  // Отличается от нашей базовой — значит, кто-то успел записать раньше.
  return seenByServer !== before ? { updated_at: seenByServer } : null;
}

export default SyncQueue;
