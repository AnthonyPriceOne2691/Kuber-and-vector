#!/bin/bash
# Вторая схема: выключены две машины из трёх. Поиск переключается на contracts_rf3 (полная копия
# базы на каждой машине) и ходит в пир Qdrant на своей машине (сервис qdrant-local).
# Честная оговорка: у кластера из трёх серверов при двух выключенных нет кворума etcd, поэтому
# Kubernetes в это время ничего не переносит и не перезапускает. Работает то, что уже запущено
# на machine-1: вход видит один живой сервер поиска, тот читает свою копию базы.
#   deploy/scenes/two-off.sh                     VMS="machine-2 machine-3" OFF=60 RATE=10
# Алиас поиска (SEARCH_ALIAS) на время сцены выключается: иначе сервис ушёл бы по алиасу с contracts_rf3
# на рабочую коллекцию; в конце возвращается прежний.
# Пишется под системный bash 3.2 на macOS. Прогнан на стенде 19.11; выключение алиаса дописано 19.11 и прогнано на стенде 20.11.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VMS="${VMS:-machine-2 machine-3}"
OFF="${OFF:-60}"; RATE="${RATE:-10}"; WARM="${WARM:-20}"; TAIL="${TAIL:-20}"
OUT="$ROOT/runs/two-off-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) s  $*" | tee -a "$OUT/timeline.txt"; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }
use() {  # $1 коллекция для сервиса поиска, $2 алиас (пусто: без алиаса, ровно эта коллекция)
  kc -n search set env deploy/search "COLLECTION=$1" "SEARCH_ALIAS=$2" >/dev/null
  kc -n search rollout status deploy/search --timeout=180s >/dev/null
}
ALIAS_BEFORE=$(kc -n search get deploy search \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SEARCH_ALIAS")].value}')

use contracts_rf3 ""
mark "search switched to contracts_rf3 (full copy on every machine)"
"$HERE/load.sh" start "$RATE"
mark "load started"
sleep "$WARM"

for m in $VMS; do limactl stop --force "$m" >> "$ROOT/runs/scene-vm.log" 2>&1 & done
wait
mark "powered off: $VMS; Kubernetes has lost quorum"
sleep "$OFF"

mark "bringing back: $VMS"
# shellcheck disable=SC2086
"$HERE/return.sh" $VMS | tee -a "$OUT/timeline.txt"
sleep "$TAIL"
"$HERE/load.sh" stop "$OUT/load.json" | tee "$OUT/summary.txt"
use contracts_rf2 "$ALIAS_BEFORE"
mark "search back on contracts_rf2${ALIAS_BEFORE:+, alias $ALIAS_BEFORE}"
echo "results in $OUT"
