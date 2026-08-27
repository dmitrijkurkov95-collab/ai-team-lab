#!/usr/bin/env bash
# Поднимает весь стенд канбан-доски одной командой, без Docker.
#
#   ./run.sh                 # доска на http://localhost:5173
#   PORT=8080 ./run.sh       # другой порт
#   ./run.sh --fresh         # начать с чистой базы
#
# Оба процесса живут в одной группе: Ctrl+C гасит стенд целиком, без
# осиротевших uvicorn, которые потом держат порт и путают следующий запуск.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-5173}"
API_PORT="${API_PORT:-8000}"
DATA_DIR="${KANBAN_DATA:-$ROOT/data}"
DB_PATH="$DATA_DIR/kanban.db"
PYTHON="${PYTHON:-python3}"
READY_TIMEOUT="${READY_TIMEOUT:-40}"

for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

command -v "$PYTHON" >/dev/null || { echo "нужен python3 (или PYTHON=<путь>)" >&2; exit 1; }

# Данные лежат вне репозитория кода и переживают перезапуск стенда.
mkdir -p "$DATA_DIR"
if [[ "${FRESH:-0}" == "1" ]]; then
  rm -f "$DB_PATH" "$DB_PATH"-wal "$DB_PATH"-shm
  echo "[stack] база очищена: $DB_PATH"
fi

# Зависимости ставим только если их нет: обычный запуск не должен ходить в сеть.
if ! "$PYTHON" -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "[stack] ставлю зависимости сервера…"
  "$PYTHON" -m pip install -q -r "$ROOT/server/requirements.txt"
fi

PIDS=()
cleanup() {
  trap - EXIT INT TERM
  echo
  echo "[stack] останавливаю…"
  for pid in "${PIDS[@]:-}"; do
    [[ -z "$pid" ]] && continue
    kill "$pid" 2>/dev/null || true
    # Ждём завершения, а не «послали сигнал и ушли»: uvicorn закрывает сокет
    # не мгновенно, и следующий запуск ловил бы «address already in use».
    for ((w = 0; w < 20; w++)); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    # Не отпустил порт по-хорошему — добиваем: осиротевший процесс держит
    # порт и путает следующий запуск (проверено, ловил на себе).
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "[stack] остановлен"
}
trap cleanup EXIT INT TERM

echo "[stack] API   → http://localhost:$API_PORT"
KANBAN_DB="$DB_PATH" KANBAN_PORT="$API_PORT" "$PYTHON" "$ROOT/server/run.py" &
PIDS+=($!)

# Ждём готовности по health, а не «спим и надеемся»: на холодной машине
# uvicorn поднимается заметно дольше, чем на прогретой.
echo -n "[stack] жду готовности API"
for ((i = 0; i < READY_TIMEOUT * 2; i++)); do
  if "$PYTHON" - "$API_PORT" <<'PY' 2>/dev/null; then
import sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/v1/health", timeout=1) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
    echo " — готов"
    break
  fi
  # Сервер мог упасть (занятый порт, битая база) — не ждём таймаут впустую.
  if ! kill -0 "${PIDS[0]}" 2>/dev/null; then
    echo; echo "[stack] API не запустился — смотрите вывод выше" >&2; exit 1
  fi
  echo -n "."
  sleep 0.5
done

echo "[stack] доска → http://localhost:$PORT"
KANBAN_API="http://127.0.0.1:$API_PORT" "$PYTHON" "$ROOT/web/serve.py" --port "$PORT" &
PIDS+=($!)

echo "[stack] данные: $DB_PATH"
echo "[stack] Ctrl+C — остановить"

# Падение любого процесса гасит стенд: половина стенда хуже честной остановки.
wait -n
echo "[stack] один из процессов завершился" >&2
