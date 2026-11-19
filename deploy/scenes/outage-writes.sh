#!/bin/bash
# Проба записи во время отказа. Пока machine-2 выключена и возвращается, в отдельную коллекцию
# outage_rf2 (6 шардов по 2 копии) идут записи через пир на machine-1. После возвращения
# сверяем: все копии шардов Active и совпадают, каждая подтверждённая запись на месте.
# Отвечает на вопрос плана «догоняет ли Qdrant копию сам или по команде».
#   deploy/scenes/outage-writes.sh [machine-2]          DURATION=150 OFF_AT=20 OFF=60
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VM="${1:-machine-2}"
DURATION="${DURATION:-150}"; OFF_AT="${OFF_AT:-20}"; OFF="${OFF:-60}"
REMOTE=/home/ops.guest/outage
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) s  $*"; }

limactl shell machine-1 sudo rm -rf "$REMOTE"
limactl shell machine-1 mkdir -p "$REMOTE"
limactl copy "$HERE/outage_writes.py" "machine-1:$REMOTE/outage_writes.py"

# писатель в фоне на machine-1: туннель к своему пиру, админский ключ только на время прогона
limactl shell --workdir "$REMOTE" machine-1 sudo bash -s -- "$REMOTE" "$DURATION" <<'EOS'
set -euo pipefail
cd "$1"
cat > run.sh <<'RUN'
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K=$(mktemp -d); PF=""
trap 'kill $PF 2>/dev/null; rm -rf "$K"; touch done' EXIT
( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "api-key"}}' | base64 -d > "$K/rw" )
POD=$(kubectl -n qdrant get pods -l app.kubernetes.io/name=qdrant --field-selector spec.nodeName=machine-1 -o jsonpath='{.items[0].metadata.name}')
kubectl -n qdrant port-forward "pod/$POD" 16350:6333 >/dev/null 2>&1 & PF=$!
for _ in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:16350/ && break; sleep 1; done
python3 -I -B outage_writes.py write --url http://127.0.0.1:16350 --key-file "$K/rw" --seconds "$1" --out writes.json > write.log 2>&1
RUN
chmod 700 run.sh
setsid nohup ./run.sh "$2" > run.log 2>&1 < /dev/null &
EOS
mark "writes started ($DURATION s)"

sleep "$OFF_AT"
limactl stop --force "$VM" >> "$ROOT/runs/scene-vm.log" 2>&1
mark "$VM powered off"
sleep "$OFF"
mark "bringing $VM back"
"$ROOT/deploy/vm/up.sh" "$VM" >> "$ROOT/runs/scene-vm.log" 2>&1
mark "$VM started, writes still running"

for _ in $(seq 1 $(( DURATION + 60 ))); do
  limactl shell machine-1 sudo test -f "$REMOTE/done" && break; sleep 1
done
mark "writes finished"
limactl shell machine-1 sudo cat "$REMOTE/write.log"
# копии сверяем, когда записи уже не идут: иначе числа точек у копий честно расходятся на лету
COLLS="outage_rf2 contracts_rf2 contracts_rf3" "$HERE/return.sh" "$VM"

limactl shell --workdir "$REMOTE" machine-1 sudo bash -s -- "$REMOTE" <<'EOS'
set -euo pipefail
cd "$1"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K=$(mktemp -d); PF=""
trap 'kill $PF 2>/dev/null; rm -rf "$K"' EXIT
( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "api-key"}}' | base64 -d > "$K/rw" )
POD=$(kubectl -n qdrant get pods -l app.kubernetes.io/name=qdrant --field-selector spec.nodeName=machine-1 -o jsonpath='{.items[0].metadata.name}')
kubectl -n qdrant port-forward "pod/$POD" 16351:6333 >/dev/null 2>&1 & PF=$!
for _ in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:16351/ && break; sleep 1; done
python3 -I -B outage_writes.py verify --url http://127.0.0.1:16351 --key-file "$K/rw" --log writes.json
curl -s -o /dev/null -X DELETE -H @<(printf 'api-key: %s\n' "$(cat "$K/rw")") http://127.0.0.1:16351/collections/outage_rf2
EOS
limactl copy "machine-1:$REMOTE/writes.json" "$ROOT/runs/outage-writes-$(date +%Y%m%d-%H%M%S).json" || true
limactl shell machine-1 sudo rm -rf "$REMOTE"
mark "done"
