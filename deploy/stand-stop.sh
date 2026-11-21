#!/bin/bash
# Остановка стенда без удаления машин: фильтр Ollama на Маке, machine-4, потом серверы k3s по одному
# (limactl stop без --force, машины гасятся штатно). Запуск обратно: deploy/stand-start.sh.
#   deploy/stand-stop.sh
# Прогнан на стенде 21.11: фильтр, затем четыре машины, всего 1 мин 39 с.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/host/filter.sh" stop || true
for m in machine-4 machine-3 machine-2 machine-1; do
  t0=$(date +%s)
  limactl stop "$m" >/dev/null 2>&1 && echo "$m остановлена за $(( $(date +%s) - t0 )) с" || echo "$m: не остановилась"
done
