/**
 * Периодический опрос доски: чужие правки появляются сами, без перезагрузки.
 *
 * Почему polling, а не WebSocket: контракт его не предусматривает, а доска
 * спринта — не биржевой терминал. Задержка в несколько секунд здесь ничего не
 * стоит, зато нет второго протокола, переподключений и состояния на сервере.
 *
 * Интервал 10 секунд — компромисс: чужая правка замечается в пределах
 * дыхания разговора («переставил — увидел»), а нагрузка остаётся смешной:
 * один GET на вкладку в 10 секунд. Меньше 5 секунд начинает шуметь при
 * перетаскивании, больше 20 — доска ощущается мёртвой.
 *
 * Экономия там, где она бесплатна:
 *   - вкладка скрыта → опрос останавливается совсем (никто не смотрит);
 *   - вкладка вернулась → немедленный опрос, не ждём тика;
 *   - браузер сообщил `offline` → не долбим сеть впустую.
 */

/** Базовый интервал в активной вкладке. */
export const POLL_INTERVAL_MS = 10000;

/** Нарастающая пауза при ошибках: 10с → 20с → 40с → 60с (потолок). */
const ERROR_BACKOFF_MS = [10000, 20000, 40000, 60000];

export class BoardPoller {
  /**
   * @param {() => Promise<void>} tick одна итерация опроса; должна бросить, если не удалось
   * @param {(info: {state: string, lastSuccessAt: number|null, failures: number}) => void} [onChange]
   */
  constructor(tick, onChange) {
    this.tick = tick;
    this.onChange = onChange || (() => {});
    this.timer = null;
    this.running = false;
    this.busy = false;
    this.failures = 0;
    this.lastSuccessAt = null;
    this.lastError = null;
    this._onVisibility = () => this.handleVisibility();
    this._onOnline = () => this.pollNow();
  }

  get state() {
    if (!this.running) return 'stopped';
    if (this.failures) return 'stale';
    return 'live';
  }

  /** Пауза до следующей попытки с учётом накопленных ошибок. */
  currentDelay() {
    if (!this.failures) return POLL_INTERVAL_MS;
    return ERROR_BACKOFF_MS[Math.min(this.failures - 1, ERROR_BACKOFF_MS.length - 1)];
  }

  notify() {
    this.onChange({
      state: this.state,
      lastSuccessAt: this.lastSuccessAt,
      failures: this.failures,
      lastError: this.lastError,
      nextDelayMs: this.running ? this.currentDelay() : null,
    });
  }

  start() {
    if (this.running) return;
    this.running = true;
    document.addEventListener('visibilitychange', this._onVisibility);
    window.addEventListener('online', this._onOnline);
    this.schedule();
    this.notify();
  }

  stop() {
    this.running = false;
    this.clearTimer();
    document.removeEventListener('visibilitychange', this._onVisibility);
    window.removeEventListener('online', this._onOnline);
    this.notify();
  }

  clearTimer() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  schedule(delay = this.currentDelay()) {
    this.clearTimer();
    if (!this.running) return;
    // Скрытую вкладку не опрашиваем: никто не смотрит, а батарея и сервер общие.
    if (typeof document !== 'undefined' && document.hidden) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.run();
    }, delay);
  }

  handleVisibility() {
    if (document.hidden) {
      this.clearTimer();
      return;
    }
    // Вкладка вернулась — показывать несвежее нельзя, опрашиваем сразу.
    this.pollNow();
  }

  /** Немедленный опрос (возврат вкладки, событие online, ручное обновление). */
  async pollNow() {
    this.clearTimer();
    await this.run();
  }

  async run() {
    if (!this.running || this.busy) return;
    // Браузер знает, что сети нет — не тратим попытку и не портим счётчик.
    if (typeof navigator !== 'undefined' && navigator.onLine === false) {
      this.schedule();
      return;
    }
    this.busy = true;
    try {
      await this.tick();
      this.failures = 0;
      this.lastError = null;
      this.lastSuccessAt = Date.now();
    } catch (error) {
      this.failures += 1;
      this.lastError = error;
    } finally {
      this.busy = false;
      this.notify();
      this.schedule();
    }
  }
}

/** «12 секунд назад» — человеческая формулировка свежести данных. */
export function formatAge(ms) {
  if (ms === null || ms === undefined) return 'нет данных';
  const sec = Math.round(ms / 1000);
  if (sec < 5) return 'только что';
  if (sec < 60) return `${sec} с назад`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} мин назад`;
  return `${Math.round(min / 60)} ч назад`;
}

export default BoardPoller;
