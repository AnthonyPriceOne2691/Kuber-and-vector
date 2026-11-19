#!/bin/bash
# Сцена А для ролика и для замера: поиск под нагрузкой, машина выключается («выдернули шнур»)
# и через OFF секунд возвращается. Нагрузка идёт с machine-4 через вход, как у пользователя.
# Печатает ход событий с отметками времени и итог: ошибки у пользователя и сколько было бы без повтора.
#   deploy/scenes/scene-a.sh [machine-2]
#   OFF=60 RATE=20 WARM=20 TAIL=20 по умолчанию
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VM="${1:-machine-2}"
OFF="${OFF:-60}"; RATE="${RATE:-20}"; WARM="${WARM:-20}"; TAIL="${TAIL:-20}"
OUT="$ROOT/runs/scene-a-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) с  $*" | tee -a "$OUT/timeline.txt"; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }

"$HERE/load.sh" start "$RATE"
mark "нагрузка пошла"
sleep "$WARM"

limactl stop --force "$VM" >> "$ROOT/runs/scene-vm.log" 2>&1
t_off=$(date +%s)
mark "$VM выключена"
dead=""; moved=""
while [ $(( $(date +%s) - t_off )) -lt "$OFF" ]; do
  if [ -z "$dead" ]; then
    s=$(kc get node "$VM" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    if [ -n "$s" ] && [ "$s" != True ]; then dead=1; mark "кластер признал $VM мёртвой (Ready=$s)"; fi
  elif [ -z "$moved" ]; then
    n=$(kc -n search get pods --field-selector status.phase=Running \
          -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | grep -vc "^$VM\$" || true)
    if [ "${n:-0}" -ge 3 ]; then moved=1; mark "поиск снова в трёх копиях, без $VM"; fi
  fi
  sleep 1
done

mark "возвращаем $VM"
"$HERE/return.sh" "$VM" | tee -a "$OUT/timeline.txt"
mark "$VM вернулась, поиск разложен"
sleep "$TAIL"
"$HERE/load.sh" stop "$OUT/load.json" | tee "$OUT/summary.txt"
echo "итоги в $OUT"
