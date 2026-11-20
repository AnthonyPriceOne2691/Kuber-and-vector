#!/bin/bash
# Запуск остановленного стенда: машины уже созданы deploy/stand-up.sh и остановлены deploy/stand-stop.sh.
# Серверы k3s machine-1…3 стартуют одновременно. После полной остановки etcd ждёт кворума (двух членов
# из трёх), и k3s первой машины в одиночку не стартует: limactl start ждёт её до таймаута в 10 минут
# (так было 20.11, когда машины запускались по одной). Потом machine-4 и фильтр Ollama на Маке.
#   deploy/stand-start.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
mkdir -p "$ROOT/runs"
t0=$(date +%s)
pids=""
for m in machine-1 machine-2 machine-3; do
  limactl start "$m" > "$ROOT/runs/start-$m.log" 2>&1 & pids="$pids $!"
done
failed=0
for p in $pids; do wait "$p" || failed=1; done
[ "$failed" = 0 ] || echo "limactl start вернул ошибку, журналы runs/start-machine-*.log; проверяю узлы"
limactl start machine-4 > "$ROOT/runs/start-machine-4.log" 2>&1
"$HERE/host/filter.sh" start
for _ in $(seq 1 60); do
  ready=$(limactl shell machine-1 sudo k3s kubectl get nodes --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l | tr -d ' ')
  [ "$ready" = 3 ] && break
  sleep 5
done
[ "${ready:-0}" = 3 ] || { echo "узлов Ready: ${ready:-0} из 3"; exit 1; }
echo "стенд запущен за $(( $(date +%s) - t0 )) с: три узла Ready, machine-4 и фильтр Ollama работают"
