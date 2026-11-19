#!/bin/bash
# Сцена «заражённый под» (идея владельца 18.11): атакующий получил оболочку в поде поиска, как будто
# нашёл дыру в нашем коде. Под нагрузкой с machine-4:
#   1) попытки изнутри (attack_pod.py): интернет, DNS, API Kubernetes, соседние машины, Мак, стереть
#      и записать базу ключом пода;
#   2) тревоги Falco той же машины;
#   3) карантин: с пода снимается метка app=search, сервис и разрешающие правила сети его больше не видят,
#      остаётся default-deny (ни входа, ни выхода), а Kubernetes сразу поднимает чистый под;
#   4) заражённый под удаляется после паузы HOLD (время снять следы).
# Итог: что отбито, через сколько пришла первая тревога, ошибки у пользователя.
#   deploy/scenes/infected-pod.sh [machine-2]
#   RATE=20 WARM=15 HOLD=10 TAIL=15 по умолчанию
# Пишется под системный bash 3.2 на macOS. Прогнан на стенде 19.11.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
VM="${1:-machine-2}"
RATE="${RATE:-20}"; WARM="${WARM:-15}"; HOLD="${HOLD:-10}"; TAIL="${TAIL:-15}"
OUT="$ROOT/runs/infected-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) s  $*" | tee -a "$OUT/timeline.txt"; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }
running_search() {
  kc -n search get pods -l app=search --field-selector status.phase=Running \
    -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null | grep -c True || true
}

POD=$(kc -n search get pods -l app=search --field-selector "spec.nodeName=$VM,status.phase=Running" -o jsonpath='{.items[0].metadata.name}')
FALCO=$(kc -n falco get pods -l app.kubernetes.io/name=falco --field-selector "spec.nodeName=$VM" -o jsonpath='{.items[0].metadata.name}')
[ -n "$POD" ] && [ -n "$FALCO" ] || { echo "no search or Falco pod on $VM"; exit 1; }
neighbors=""
for m in machine-1 machine-2 machine-3; do [ "$m" = "$VM" ] || neighbors="$neighbors $(ip_of "$m")"; done
echo "pod $POD on $VM, Falco $FALCO, neighbors:$neighbors"

"$HERE/load.sh" start "$RATE"
mark "load started"
sleep "$WARM"

mark "attacker has a shell in pod $POD"
# Оболочка первой командой записывает своё время (T0, начало атаки по часам этой машины), читает
# ключ пода (Falco: CRITICAL) и запускает Python с попытками, код идёт по stdin
# shellcheck disable=SC2086,SC2016  # соседи это список адресов; $(…) раскрывает оболочка пода
kc -n search exec -i "$POD" -- sh -c 'T0=$(date +%s.%N); KEY=$(cat /run/secrets/qdrant/read-only-api-key); export KEY T0; exec python3 -I - "$@"' \
  sh $neighbors < "$HERE/attack_pod.py" > "$OUT/attack.tsv"
awk -F'\t' '$2 != "start" && $2 != "end" {print "    " $4 ": " $2 " (" $3 ")"}' "$OUT/attack.tsv"
mark "attempts finished"
sleep 3
# Метки времени ставит containerd той же машины: по ним считаем задержку тревог (infected_summary.py)
kc -n falco logs "$FALCO" --since=5m --timestamps > "$OUT/falco.log" 2>/dev/null || true

kc -n search label pod "$POD" app=quarantine --overwrite >/dev/null
mark "pod $POD quarantined: out of the service, network cut by default-deny"
for _ in $(seq 1 90); do [ "$(running_search)" -ge 3 ] && break; sleep 1; done
mark "clean pod up, search back to three replicas ($(running_search) ready)"
sleep "$HOLD"
kc -n search delete pod "$POD" --wait=false >/dev/null
mark "infected pod deleted"
sleep "$TAIL"
"$HERE/load.sh" stop "$OUT/load.json" | tee "$OUT/load.txt"

status=0
"$PY" -I -B "$HERE/infected_summary.py" "$OUT" > "$OUT/summary.md" || status=$?
cat "$OUT/summary.md"
echo "results in $OUT"
exit "$status"
