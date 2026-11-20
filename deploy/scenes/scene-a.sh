#!/bin/bash
# Сцена А для ролика и для замера: поиск под нагрузкой, машина выключается («выдернули шнур»)
# и через OFF секунд возвращается. Нагрузка идёт с machine-4 через вход, как у пользователя.
# Печатает ход событий с отметками времени и итог: ошибки у пользователя и сколько было бы без повтора.
#   deploy/scenes/scene-a.sh              машина кластера без лидеров (см. ниже)
#   deploy/scenes/scene-a.sh machine-3    именно эта: machine-1, machine-2 или machine-3
#   OFF=90 RATE=20 WARM=20 TAIL=20 по умолчанию
# Без аргумента выключается машина, которая не держит аренду лидера ни kube-controller-manager,
# ни kube-scheduler (аренды в kube-system, holderIdentity вида lima-machine-1_<uuid>); из таких
# первая по порядку machine-2, machine-3, machine-1. Причина: 19.11 выключили machine-2, она держала
# лидера controller-manager, и кластер признал её мёртвой через 43 с вместо ~25 с: сначала аренду
# должен перехватить новый лидер, и только он отсчитывает срок узла заново. Кто лидер и кого
# выключаем, печатается перед выключением. Явный аргумент сильнее выбора.
# OFF 90 с: признание узла ~25–43 с и ещё 30 с допуска подов поиска (tolerations в
# deploy/search/k8s.yaml), только потом под переезжает на живую машину.
# kubectl идёт с первой машины кластера, которая остаётся включённой.
# Пишется под системный bash 3.2 на macOS. Прогнан на стенде 19.11 (тогда OFF=60 и всегда machine-2);
# выбор машины по арендам и OFF=90 написаны 19.11 без стенда и прогнаны на стенде 20.11.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CLUSTER="machine-1 machine-2 machine-3"
ARG="${1:-}"
case "$ARG" in
  ""|machine-1|machine-2|machine-3) ;;
  *) echo "usage: deploy/scenes/scene-a.sh [machine-1|machine-2|machine-3]"; exit 1 ;;
esac
OFF="${OFF:-90}"; RATE="${RATE:-20}"; WARM="${WARM:-20}"; TAIL="${TAIL:-20}"
OUT="$ROOT/runs/scene-a-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) s  $*" | tee -a "$OUT/timeline.txt"; }
KC_VM=machine-1
kc() { limactl shell "$KC_VM" sudo k3s kubectl "$@"; }

# На входе строки «аренда держатель» (kube-controller-manager, kube-scheduler). Ставит PICK: первую
# из machine-2, machine-3, machine-1 без аренды лидера (две аренды занимают не больше двух машин из
# трёх, так что такая есть всегда); LEADER_VMS: машины с арендой; LEADERS: строку для отметки.
# Держатель неизвестного вида считается неизвестным и машину не занимает.
# tests/test_scene_a.py вынимает эту функцию и гоняет без стенда.
pick_vm() {
  local name holder m kcm="" sched="" cand
  while read -r name holder; do
    m="${holder%%_*}"; m="${m#lima-}"
    case "$m" in machine-1|machine-2|machine-3) ;; *) m="" ;; esac
    case "$name" in
      kube-controller-manager) kcm="$m" ;;
      kube-scheduler) sched="$m" ;;
    esac
  done
  LEADERS="kube-controller-manager on ${kcm:-unknown}, kube-scheduler on ${sched:-unknown}"
  LEADER_VMS="$kcm $sched"
  PICK=""
  for cand in machine-2 machine-3 machine-1; do
    if [ "$cand" != "$kcm" ] && [ "$cand" != "$sched" ]; then PICK="$cand"; break; fi
  done
}

"$HERE/load.sh" start "$RATE"
mark "load started"
sleep "$WARM"

# аренды читаются прямо перед выключением: лидер мог смениться, пока шла разминка
leases=$(kc -n kube-system get lease kube-controller-manager kube-scheduler \
  -o jsonpath='{range .items[*]}{.metadata.name} {.spec.holderIdentity}{"\n"}{end}' 2>/dev/null || true)
pick_vm <<<"$leases"
mark "leaders: $LEADERS"
if [ -n "$ARG" ]; then
  VM="$ARG"
  case " $LEADER_VMS " in
    *" $VM "*) why="as asked, it holds a leader lease, detection may take longer" ;;
    *) why="as asked, it holds no leader lease" ;;
  esac
else
  VM="$PICK"; why="it holds no leader lease"
fi
for m in $CLUSTER; do
  if [ "$m" != "$VM" ]; then KC_VM="$m"; break; fi
done
mark "powering off $VM, $why"

limactl stop --force "$VM" >> "$ROOT/runs/scene-vm.log" 2>&1
t_off=$(date +%s)
mark "$VM powered off"
dead=""; moved=""
while [ $(( $(date +%s) - t_off )) -lt "$OFF" ]; do
  if [ -z "$dead" ]; then
    s=$(kc get node "$VM" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    if [ -n "$s" ] && [ "$s" != True ]; then dead=1; mark "cluster marked $VM down (Ready=$s)"; fi
  elif [ -z "$moved" ]; then
    n=$(kc -n search get pods --field-selector status.phase=Running \
          -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | grep -vc "^$VM\$" || true)
    if [ "${n:-0}" -ge 3 ]; then moved=1; mark "search back to three replicas, without $VM"; fi
  fi
  sleep 1
done
if [ -z "$dead" ]; then mark "cluster has not marked $VM down within $OFF s"
elif [ -z "$moved" ]; then mark "search pods have not left $VM within $OFF s"
fi

mark "bringing $VM back"
"$HERE/return.sh" "$VM" | tee -a "$OUT/timeline.txt"
mark "$VM is back, search spread across machines again"
sleep "$TAIL"
"$HERE/load.sh" stop "$OUT/load.json" | tee "$OUT/summary.txt"
echo "results in $OUT"
