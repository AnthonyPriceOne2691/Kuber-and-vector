#!/bin/bash
# Поиск «от малого к большому» на LegalBench-RAG-mini: дети из eval/children.py грузятся во временную
# коллекцию, оценка ищет по детям и группирует их по родителю (qdrant_eval.py --group-by parent), так
# что в выдаче те же куски по 500 знаков, что у базы naive500_title, и recall@k сравним один к одному.
# Вопросы и их векторы те же, что у замера этапа 1 (cache/stage1). Коллекция удаляется в конце.
# Доступ как у eval/run_full.sh: kubectl port-forward изнутри machine-1, ключи из Secret в файлах 600.
#   eval/run_children.sh [machine-1]
#   SRC=cache/stage1_children/children200 COLLECTION=lbr_children200 MODE=dense по умолчанию
# Итог: cache/stage1_children/results/result_<коллекция>_<режим>.json и сводка. Системный bash 3.2.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VM="${1:-machine-1}"
SRC="${SRC:-cache/stage1_children/children200}"; case "$SRC" in /*) ;; *) SRC="$ROOT/$SRC" ;; esac
COLLECTION="${COLLECTION:-lbr_children200}"
MODE="${MODE:-dense}"
QDIR="$ROOT/cache/stage1"
REMOTE=/home/ops.guest/evalchildren
[ -f "$SRC/points.jsonl" ] || { echo "нет $SRC: сначала eval/children.py"; exit 1; }
[ -f "$QDIR/query_vectors.f32" ] || { echo "нет $QDIR/query_vectors.f32: сначала eval/prepare.py"; exit 1; }

limactl shell "$VM" sudo rm -rf "$REMOTE"
limactl shell "$VM" mkdir -p "$REMOTE"
for f in legalbench.py qdrant_load.py qdrant_eval.py; do limactl copy "$HERE/$f" "$VM:$REMOTE/$f"; done
limactl copy "$QDIR/queries.jsonl" "$VM:$REMOTE/queries.jsonl"
limactl copy "$QDIR/query_vectors.f32" "$VM:$REMOTE/query_vectors.f32"
limactl copy -r "$SRC" "$VM:$REMOTE/data"

limactl shell --workdir "$REMOTE" "$VM" sudo bash -s -- "$REMOTE" "$COLLECTION" "$MODE" <<'EOS'
set -euo pipefail
cd "$1"; C=$2; MODE=$3
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K=$(mktemp -d); PF=""
cleanup() {  # временная коллекция удаляется при любом выходе
  curl -s -o /dev/null -X DELETE -H "api-key: $(cat "$K/rw" 2>/dev/null)" "http://127.0.0.1:16336/collections/$C" || true
  kill $PF 2>/dev/null; rm -rf "$K"
}
trap cleanup EXIT
( umask 077
  kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "api-key"}}' | base64 -d > "$K/rw"
  kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "read-only-api-key"}}' | base64 -d > "$K/ro" )
kubectl -n qdrant port-forward svc/qdrant 16336:6333 >/dev/null 2>&1 & PF=$!
for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:16336/" && break; sleep 1; done
python3 -I -B qdrant_load.py --url http://127.0.0.1:16336 --key-file "$K/rw" --dir data --collection "$C" --shards 3 --replicas 1
python3 -I -B qdrant_eval.py --url http://127.0.0.1:16336 --key-file "$K/ro" --dir . --collection "$C" \
  --mode "$MODE" --group-by parent --out "result_${C}_${MODE}.json"
chown ops: "result_${C}_${MODE}.json"
EOS
mkdir -p "$ROOT/cache/stage1_children/results"
OUTF="$ROOT/cache/stage1_children/results/result_${COLLECTION}_${MODE}.json"
limactl copy "$VM:$REMOTE/result_${COLLECTION}_${MODE}.json" "$OUTF"
limactl shell "$VM" sudo rm -rf "$REMOTE"
/opt/homebrew/bin/python3 -I -B - "$OUTF" <<'EOF'
import json, statistics, sys
r = json.load(open(sys.argv[1]))
print(f"{r['collection']} [{r['mode']}], дети по родителю: вопросов {r['queries']}")
for d, v in r["datasets"].items():
    print(f"  {d:12s} recall@8 {v['8']['recall']:5.1f}  recall@64 {v['64']['recall']:5.1f}")
a = r["all"]
print(f"  среднее по наборам recall@1/8/64 {a['1']['recall']:.1f} / {a['8']['recall']:.1f} / {a['64']['recall']:.1f};"
      f" среднее по k recall {statistics.mean(v['recall'] for v in a.values()):.1f}, precision {statistics.mean(v['precision'] for v in a.values()):.2f}")
EOF
