#!/bin/bash
# Фильтр Ollama на Маке: start | stop | status. Журнал в runs/ollama-filter.log.
# Слушает только адрес моста машин (192.168.105.1); мост появляется со стартом первой машины,
# поэтому фильтр сам ждёт его до двух минут (--wait).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
LOG="$ROOT/runs/ollama-filter.log"
PY=/opt/homebrew/bin/python3
pid_of() { ps -axo pid=,command= | grep "[o]llama_filter.py" | awk '{print $1}' | head -1; }

case "${1:-status}" in
  start)
    if [ -n "$(pid_of)" ]; then echo "фильтр уже работает (pid $(pid_of))"; exit 0; fi
    mkdir -p "$ROOT/runs"; touch "$LOG"
    from=$(( $(wc -l < "$LOG") + 1 ))  # смотрим только строки этого запуска
    nohup "$PY" -I -B "$HERE/ollama_filter.py" --listen 192.168.105.1 >> "$LOG" 2>&1 &
    disown || true
    for _ in $(seq 1 60); do
      tail -n +"$from" "$LOG" | grep -q '"event": "listening"' && [ -n "$(pid_of)" ] && break
      sleep 2
    done
    [ -n "$(pid_of)" ] || { echo "фильтр не запустился, см. $LOG"; exit 1; }
    echo "фильтр работает (pid $(pid_of)), журнал $LOG" ;;
  stop)
    p="$(pid_of)"; [ -n "$p" ] && kill "$p" && echo "фильтр остановлен" || echo "фильтр не запущен" ;;
  status)
    p="$(pid_of)"; [ -n "$p" ] && echo "фильтр работает (pid $p)" || echo "фильтр не запущен" ;;
  *) echo "start | stop | status"; exit 1 ;;
esac
