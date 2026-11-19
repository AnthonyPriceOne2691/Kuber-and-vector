#!/bin/bash
# Возвращение машин после отказа: включить, дождаться узла и пира Qdrant, дождаться, пока все
# копии шардов снова Active и совпадают, и разложить поиск обратно по трём машинам
# (сам Kubernetes поды обратно не переносит). Печатает время каждого шага от старта.
#   deploy/scenes/return.sh [machine-2 [machine-3]]
#   COLLS="contracts_rf2 contracts_rf3" по умолчанию; WAIT=300 сколько ждать копий
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
[ $# -gt 0 ] || set -- machine-2
COLLS="${COLLS:-contracts_rf2 contracts_rf3}"
WAIT="${WAIT:-300}"
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) s  $*"; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }
mkdir -p "$ROOT/runs"

"$ROOT/deploy/vm/up.sh" "$@" >> "$ROOT/runs/scene-vm.log" 2>&1
mark "machines started: $*"

for m in "$@"; do
  for _ in $(seq 1 150); do
    s=$(kc get node "$m" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    [ "$s" = True ] && break; sleep 2
  done
  [ "$s" = True ] || { echo "node $m did not become Ready"; exit 1; }
  mark "node $m Ready"
  for _ in $(seq 1 150); do
    r=$(kc -n qdrant get pods -l app.kubernetes.io/name=qdrant --field-selector "spec.nodeName=$m" -o jsonpath='{.items[*].status.containerStatuses[*].ready}' 2>/dev/null || true)
    [ "$r" = true ] && break; sleep 2
  done
  [ "$r" = true ] || { echo "Qdrant peer on $m not ready"; exit 1; }
  mark "Qdrant peer on $m ready"
done

limactl copy "$HERE/replicas.py" machine-1:/tmp/replicas.py
limactl shell --workdir /tmp machine-1 sudo bash -s -- "$COLLS" "$WAIT" <<'EOS'
set -euo pipefail
COLLS=$1; WAIT=$2
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K=$(mktemp -d); PF=""
trap 'kill $PF 2>/dev/null; rm -rf "$K" /tmp/replicas.py' EXIT
( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "read-only-api-key"}}' | base64 -d > "$K/ro" )
port=16360
for pod in $(kubectl -n qdrant get pods -l app.kubernetes.io/name=qdrant -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n qdrant port-forward "pod/$pod" "$port:6333" >/dev/null 2>&1 & PF="$PF $!"
  for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:$port/" && break; sleep 1; done
  echo "$pod $(kubectl -n qdrant get pod "$pod" -o jsonpath='{.spec.nodeName}') $port" >> "$K/pods"
  port=$((port + 1))
done
# shellcheck disable=SC2086
python3 -I -B /tmp/replicas.py "$K/ro" "$K/pods" $COLLS --wait "$WAIT"
EOS
mark "shard replicas in place"

kc -n search rollout restart deploy/search >/dev/null
kc -n search rollout status deploy/search --timeout=180s >/dev/null
mark "search spread across machines again"
kc -n search get pods -o wide
