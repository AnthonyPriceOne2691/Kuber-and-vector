#!/bin/bash
# Загружает куски с названием документа (naive500_title, лучшая ветка замера) в кластерный Qdrant
# двумя схемами (решение владельца 18.11: «попробовать обе»):
#   contracts_rf2  6 шардов по 2 копии: каждая машина держит 4 шарда из 6, переживает отказ одной
#   contracts_rf3  3 шарда по 3 копии: полная копия на каждой машине, переживает отказ двух
# Загрузка идёт изнутри машины кластера админским ключом из Secret; ключ живёт в файле 600
# и удаляется при выходе. Повторный запуск не плодит дублей: id точки из хэша куска.
# К Qdrant ходим через kubectl port-forward: это туннель через API Kubernetes под RBAC,
# поэтому NetworkPolicy («Qdrant принимает только поиск и своих пиров») загрузке не мешает.
#   deploy/qdrant/load.sh [machine-1]
# Другой корпус или другие схемы задаются окружением, например полный корпус:
#   SRC=cache/full/naive500_title SETS="contracts_full_rf2:6:2" deploy/qdrant/load.sh
#   LOAD_OPTS="--quantization int8 --on-disk" для замера предела (см. eval/qdrant_load.py)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VM="${1:-machine-1}"
SRC="${SRC:-cache/stage1/naive500_title}"; case "$SRC" in /*) ;; *) SRC="$ROOT/$SRC" ;; esac
SETS="${SETS:-contracts_rf2:6:2 contracts_rf3:3:3}"  # имя:шардов:копий
LOAD_OPTS="${LOAD_OPTS:-}"
REMOTE=/home/ops.guest/load
[ -f "$SRC/points.jsonl" ] || { echo "нет $SRC: сначала python3 -I eval/prepare.py"; exit 1; }

limactl shell "$VM" sudo rm -rf "$REMOTE"
limactl shell "$VM" mkdir -p "$REMOTE"
limactl copy "$ROOT/eval/qdrant_load.py" "$VM:$REMOTE/qdrant_load.py"
limactl copy "$HERE/placement.py" "$VM:$REMOTE/placement.py"
limactl copy -r "$SRC" "$VM:$REMOTE/data"

limactl shell --workdir "$REMOTE" "$VM" sudo bash -s -- "$REMOTE" "$SETS" "$LOAD_OPTS" <<'EOS'
set -euo pipefail
cd "$1"; SETS=$2; LOAD_OPTS=$3
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K=$(mktemp -d); PF=""
trap 'kill $PF 2>/dev/null; rm -rf "$K"' EXIT
( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "api-key"}}' | base64 -d > "$K/rw" )
forward() { # $1 цель (svc/qdrant или pod/qdrant-N), $2 локальный порт
  kubectl -n qdrant port-forward "$1" "$2:6333" >/dev/null 2>&1 & PF="$PF $!"
  for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:$2/" && return 0; sleep 1; done
  echo "port-forward $1 не поднялся"; return 1
}
forward svc/qdrant 16333
names=""
for set in $SETS; do
  IFS=: read -r name shards replicas <<< "$set"
  # shellcheck disable=SC2086  # LOAD_OPTS это список флагов
  python3 -I -B qdrant_load.py --url http://127.0.0.1:16333 --key-file "$K/rw" --dir data --collection "$name" \
    --shards "$shards" --replicas "$replicas" $LOAD_OPTS
  names="$names $name"
done
# карта шардов: каждого пира спрашиваем через свой туннель
: > "$K/pods"; port=16340
for pod in $(kubectl -n qdrant get pods -o jsonpath='{.items[*].metadata.name}'); do
  forward "pod/$pod" "$port"
  echo "$pod $(kubectl -n qdrant get pod "$pod" -o jsonpath='{.spec.nodeName}') $port" >> "$K/pods"
  port=$((port + 1))
done
# shellcheck disable=SC2086
python3 -I -B placement.py "$K/rw" "$K/pods" $names
EOS
limactl shell "$VM" sudo rm -rf "$REMOTE"
