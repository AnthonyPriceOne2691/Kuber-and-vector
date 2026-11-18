#!/bin/bash
# Этап 1: загрузить куски в Qdrant на машине стенда и посчитать качество поиска.
# Сначала на Маке: python3 -I eval/prepare.py (нарезка и векторы).
#   eval/run_stage1.sh [machine-1]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VM="${1:-machine-1}"
SRC="$ROOT/cache/stage1"
REMOTE=/home/ops.guest/stage1
[ -f "$SRC/meta.json" ] || { echo "нет $SRC/meta.json: сначала eval/prepare.py"; exit 1; }
strategies=$(/opt/homebrew/bin/python3 -I -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["strategies"]))' "$SRC/meta.json")

limactl shell "$VM" sudo rm -rf "$REMOTE"
limactl shell "$VM" mkdir -p "$REMOTE"
for f in legalbench.py qdrant_load.py qdrant_eval.py; do limactl copy "$HERE/$f" "$VM:$REMOTE/$f"; done
limactl copy "$SRC/queries.jsonl" "$VM:$REMOTE/queries.jsonl"
limactl copy "$SRC/query_vectors.f32" "$VM:$REMOTE/query_vectors.f32"
for s in $strategies; do limactl copy -r "$SRC/$s" "$VM:$REMOTE/$s"; done

# Внутри машины: ключи из Secret во временные файлы 600, загрузка админским ключом,
# оценка ключом только на чтение; файлы ключей удаляются при выходе.
# shellcheck disable=SC2086
limactl shell --workdir "$REMOTE" "$VM" sudo bash -s -- "$REMOTE" $strategies <<'EOF'
set -euo pipefail
REMOTE=$1; shift
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
IP=$(kubectl -n qdrant get svc qdrant -o jsonpath='{.spec.clusterIP}')
K=$(mktemp -d); trap 'rm -rf "$K"' EXIT
( umask 077
  kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "api-key"}}' | base64 -d > "$K/rw"
  kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "read-only-api-key"}}' | base64 -d > "$K/ro" )
cd "$REMOTE"
for s in "$@"; do
  c="lbr_mini_${s}_bge_m3"
  python3 -I -B qdrant_load.py --url "http://$IP:6333" --key-file "$K/rw" --dir "$s" --collection "$c"
  python3 -I -B qdrant_eval.py --url "http://$IP:6333" --key-file "$K/ro" --dir . --collection "$c" --out "result_$s.json"
done
chown ops: result_*.json
EOF

mkdir -p "$SRC/results"
for s in $strategies; do limactl copy "$VM:$REMOTE/result_$s.json" "$SRC/results/result_$s.json"; done
/opt/homebrew/bin/python3 -I "$HERE/report.py" "$SRC" "$ROOT/eval/results/stage1.md"
