#!/bin/bash
# Замер поиска на полном LegalBench-RAG (714 документов, 6 889 вопросов) по коллекции на стенде.
# Коллекцию заранее грузит deploy/qdrant/load.sh (SRC=cache/full/naive500_title SETS="contracts_full_rf2:6:2").
# Вопросы и их векторы готовит eval/prepare.py --full (cache/full). Чисел статьи для полного набора нет:
# у неё таблица 4 посчитана на мини-выборке, поэтому здесь только наш замер.
# К Qdrant ходим как load.sh: kubectl port-forward изнутри machine-1 (узлу напрямую Qdrant закрыт
# правилом сети), ключ только на чтение из Secret в файле 600, удаляется при выходе.
#   eval/run_full.sh [machine-1]          COLLECTION=contracts_full_rf2 по умолчанию
# Итог: cache/full/results/result_full.json и сводка в терминале. Пишется под системный bash 3.2.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VM="${1:-machine-1}"
COLLECTION="${COLLECTION:-contracts_full_rf2}"
SRC="$ROOT/cache/full"
REMOTE=/home/ops.guest/evalfull
[ -f "$SRC/queries.jsonl" ] && [ -f "$SRC/query_vectors.f32" ] || { echo "нет $SRC/queries.jsonl: сначала eval/prepare.py --full"; exit 1; }

limactl shell "$VM" sudo rm -rf "$REMOTE"
limactl shell "$VM" mkdir -p "$REMOTE"
for f in legalbench.py qdrant_eval.py; do limactl copy "$HERE/$f" "$VM:$REMOTE/$f"; done
limactl copy "$SRC/queries.jsonl" "$VM:$REMOTE/queries.jsonl"
limactl copy "$SRC/query_vectors.f32" "$VM:$REMOTE/query_vectors.f32"

limactl shell --workdir "$REMOTE" "$VM" sudo bash -s -- "$REMOTE" "$COLLECTION" <<'EOS'
set -euo pipefail
cd "$1"; C=$2
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K=$(mktemp -d); PF=""
trap 'kill $PF 2>/dev/null; rm -rf "$K"' EXIT
( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "read-only-api-key"}}' | base64 -d > "$K/ro" )
kubectl -n qdrant port-forward svc/qdrant 16334:6333 >/dev/null 2>&1 & PF=$!
for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:16334/" && break; sleep 1; done
python3 -I -B qdrant_eval.py --url http://127.0.0.1:16334 --key-file "$K/ro" --dir . --collection "$C" \
  --mode dense --out result_full.json
chown ops: result_full.json
EOS
mkdir -p "$SRC/results"
limactl copy "$VM:$REMOTE/result_full.json" "$SRC/results/result_full.json"
limactl shell "$VM" sudo rm -rf "$REMOTE"
/opt/homebrew/bin/python3 -I -B - "$SRC/results/result_full.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"{r['collection']}: вопросов {r['queries']}")
for d, v in r["datasets"].items():
    print(f"  {d:12s} recall@8 {v['8']['recall']:5.1f}  recall@64 {v['64']['recall']:5.1f}")
a = r["all"]
lat = r["latency_ms"]["live"]
print(f"  среднее по наборам recall@1/8/64 {a['1']['recall']:.1f} / {a['8']['recall']:.1f} / {a['64']['recall']:.1f}")
print(f"  HNSW совпал с точным на {r['live_vs_exact']['8']:.1f}% (k=8); живой поиск p50 {lat['p50']:.1f} мс, p95 {lat['p95']:.1f} мс")
EOF
