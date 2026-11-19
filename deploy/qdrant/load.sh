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
# Каждая загруженная коллекция записывается в реестр «коллекция → модель» (deploy/qdrant/registry.py):
# модель из meta.json набора, отпечаток это первый кусок набора (eval/fingerprint.py). По реестру
# сервис поиска проверяет коллекцию, прежде чем перейти на неё по алиасу.
# Другой корпус или другие схемы задаются окружением, например полный корпус или векторы другой модели:
#   SRC=cache/full/naive500_title SETS="contracts_full_rf2:6:2" deploy/qdrant/load.sh
#   SRC=cache/stage1_arctic/naive500_title SETS="contracts_rf2_arctic:6:2" deploy/qdrant/load.sh
#   LOAD_OPTS="--quantization int8 --on-disk" для замера предела (см. eval/qdrant_load.py),
#   LOAD_OPTS="--limit 1000" только первые 1000 кусков
#   REGISTER_AS=bge-m3 подписать набор в реестре не той моделью: только для подложенной ошибки
#   (deploy/scenes/model-switch.sh)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
VM="${1:-machine-1}"
SRC="${SRC:-cache/stage1/naive500_title}"; case "$SRC" in /*) ;; *) SRC="$ROOT/$SRC" ;; esac
SETS="${SETS:-contracts_rf2:6:2 contracts_rf3:3:3}"  # имя:шардов:копий
LOAD_OPTS="${LOAD_OPTS:-}"
REGISTER_AS="${REGISTER_AS:-}"
REMOTE=/home/ops.guest/load
[ -f "$SRC/points.jsonl" ] || { echo "нет $SRC: сначала python3 -I eval/prepare.py"; exit 1; }
# запись реестра считается до загрузки: набор без meta.json или с битыми файлами не грузим вовсе
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
if [ -n "$REGISTER_AS" ]; then
  "$PY" -I -B "$ROOT/eval/fingerprint.py" "$SRC" --model "$REGISTER_AS" > "$T/entry.json"
else
  "$PY" -I -B "$ROOT/eval/fingerprint.py" "$SRC" > "$T/entry.json"
fi
DIM="$("$PY" -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["dim"])' "$T/entry.json")"

limactl shell "$VM" sudo rm -rf "$REMOTE"
limactl shell "$VM" mkdir -p "$REMOTE"
limactl copy "$ROOT/eval/qdrant_load.py" "$VM:$REMOTE/qdrant_load.py"
limactl copy "$HERE/placement.py" "$VM:$REMOTE/placement.py"
limactl copy "$HERE/registry.py" "$VM:$REMOTE/registry.py"
limactl copy "$T/entry.json" "$VM:$REMOTE/entry.json"
limactl copy -r "$SRC" "$VM:$REMOTE/data"

limactl shell --workdir "$REMOTE" "$VM" sudo bash -s -- "$REMOTE" "$SETS" "$LOAD_OPTS" "$DIM" <<'EOS'
set -euo pipefail
cd "$1"; SETS=$2; LOAD_OPTS=$3; DIM=$4
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
    --dim "$DIM" --shards "$shards" --replicas "$replicas" $LOAD_OPTS
  python3 -I -B registry.py --url http://127.0.0.1:16333 --key-file "$K/rw" register --entry entry.json --collection "$name"
  names="$names $name"
done
# карта шардов: каждого пира спрашиваем через свой туннель
: > "$K/pods"; port=16340
for pod in $(kubectl -n qdrant get pods -l app.kubernetes.io/name=qdrant -o jsonpath='{.items[*].metadata.name}'); do
  forward "pod/$pod" "$port"
  echo "$pod $(kubectl -n qdrant get pod "$pod" -o jsonpath='{.spec.nodeName}') $port" >> "$K/pods"
  port=$((port + 1))
done
# shellcheck disable=SC2086
python3 -I -B placement.py "$K/rw" "$K/pods" $names
EOS
limactl shell "$VM" sudo rm -rf "$REMOTE"
