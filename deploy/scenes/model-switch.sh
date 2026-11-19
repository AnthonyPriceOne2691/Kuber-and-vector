#!/bin/bash
# Сцена «смена модели векторов на ходу». Под нагрузкой алиас contracts переходит с коллекции на векторах
# bge-m3 на коллекцию на векторах snowflake-arctic-embed2 одним запросом к Qdrant, поды не перезапускаются;
# качество до и после на экране, откат одной командой. Потом подложенная ошибка: алиас указывает на
# коллекцию, чьи векторы сделаны не той моделью, что подписана в реестре; поды её не берут и отвечают
# прежней парой. Нагрузка и все проверки идут через вход, как у пользователя.
#   deploy/scenes/model-switch.sh
#   RATE=20 WARM=15 HOLD=20 TAIL=10 по умолчанию; K=8 PER_DATASET=25 (качество: recall@K на первых
#   PER_DATASET вопросах каждого набора LegalBench-RAG-mini, эталон из data/legalbenchrag, живьём через вход)
#   ALIAS=contracts BASE=contracts_rf2 NEW=contracts_rf2_arctic
#   BAD=contracts_mislabeled BAD_SRC=cache/stage1_arctic/naive500_title BAD_POINTS=1000 TRAP_AS=bge-m3:
#   ловушка из первых BAD_POINTS кусков набора arctic, подписанная в реестре моделью TRAP_AS
# Нужно заранее: BASE и NEW загружены и записаны в реестр (deploy/qdrant/load.sh пишет сам, уже
# загруженные: deploy/qdrant/registry.sh register), образ поиска с SEARCH_ALIAS (deploy/search/k8s.yaml),
# фильтр Ollama на Маке пускает обе модели (deploy/host/filter.sh). Итоги в runs/model-switch-*/.
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
RATE="${RATE:-20}"; WARM="${WARM:-15}"; HOLD="${HOLD:-20}"; TAIL="${TAIL:-10}"
K="${K:-8}"; PER_DATASET="${PER_DATASET:-25}"
ALIAS="${ALIAS:-contracts}"; BASE="${BASE:-contracts_rf2}"; NEW="${NEW:-contracts_rf2_arctic}"
BAD="${BAD:-contracts_mislabeled}"; BAD_SRC="${BAD_SRC:-cache/stage1_arctic/naive500_title}"
BAD_POINTS="${BAD_POINTS:-1000}"; TRAP_AS="${TRAP_AS:-bge-m3}"
OUT="$ROOT/runs/model-switch-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) s  $*" | tee -a "$OUT/timeline.txt"; }
step() { "$@" 2>&1 | sed 's/^/          /' | tee -a "$OUT/timeline.txt"; }  # вывод проверки под отметкой
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }
reg() { "$ROOT/deploy/qdrant/registry.sh" "$@"; }
ms() {
  "$PY" -I -B "$HERE/model_switch.py" --url "https://$ENTRY" --cafile "$ROOT/runs/stand-ca.pem" \
    --api-key-file "$ROOT/runs/entry-api-key" "$@"
}
registered() {  # $1 коллекция есть в реестре (по снимку registry.sh show --json)
  "$PY" -I -c 'import json, sys
lines = [l for l in open(sys.argv[1]) if l.strip()]
sys.exit(0 if sys.argv[2] in json.loads(lines[-1])["registry"] else 1)' "$OUT/registry-before.json" "$1"
}

[ -f "$ROOT/runs/stand-ca.pem" ] && [ -f "$ROOT/runs/entry-api-key" ] \
  || { echo "missing runs/stand-ca.pem or runs/entry-api-key: run deploy/observer/install.sh first"; exit 1; }
[ -d "$ROOT/data/legalbenchrag/benchmarks" ] || { echo "missing data/legalbenchrag: quality needs the benchmark answers"; exit 1; }
ENTRY="$(ip_of machine-4)"

LOAD=""; PLANTED=""; DONE=""
finish() {  # сцена прервана: алиас обратно на BASE, ловушку убрать, нагрузку остановить
  local code=$?
  trap - EXIT INT TERM
  set +e
  if [ -z "$DONE" ]; then
    reg alias "$ALIAS" "$BASE" >/dev/null 2>&1
    if [ -n "$PLANTED" ]; then reg drop "$BAD" >/dev/null 2>&1; fi
    if [ -n "$LOAD" ]; then "$HERE/load.sh" stop "$OUT/load.json" >/dev/null 2>&1; fi
    echo "scene stopped (code $code): alias $ALIAS back on $BASE, see ${OUT#"$ROOT"/}"
  fi
  exit "$code"
}
trap finish EXIT
trap 'exit 130' INT TERM

# --- подготовка вне кадра: реестр на месте, алиас на BASE, ловушка готова ------------------------
echo "  prep  registry and trap (off camera)"
reg show --json > "$OUT/registry-before.json"
for c in "$BASE" "$NEW"; do
  registered "$c" || { echo "$c is not in the registry: deploy/qdrant/registry.sh register $c <set>"; exit 1; }
done
reg alias "$ALIAS" "$BASE" | sed 's/^/        /'
PLANTED=1  # с этого места уборка удаляет ловушку, даже если загрузка оборвётся на середине
if ! SRC="$BAD_SRC" SETS="$BAD:1:2" REGISTER_AS="$TRAP_AS" LOAD_OPTS="--limit $BAD_POINTS" \
    "$ROOT/deploy/qdrant/load.sh" > "$OUT/trap-load.log" 2>&1; then
  echo "trap load failed, see ${OUT#"$ROOT"/}/trap-load.log"
  exit 1
fi
echo "        trap $BAD: first $BAD_POINTS chunks of ${BAD_SRC#cache/}, registered as $TRAP_AS"
ms wait --collection "$BASE" --status verified --pods 3 --out "$OUT/wait-start.json" | sed 's/^/        /'

# --- сцена ----------------------------------------------------------------------------------------
result=0
t0=$(date +%s)
"$HERE/load.sh" start "$RATE"
LOAD=1
mark "load started: $RATE requests per second through the entry"
sleep "$WARM"

mark "who answers users now"
step ms pair --out "$OUT/pair-before.json"
mark "quality before the switch"
step ms quality --k "$K" --per-dataset "$PER_DATASET" --rate 20 --expect "$BASE" --label "before: " \
  --out "$OUT/quality-before.json" || result=1

mark "switching alias $ALIAS to $NEW: one request to Qdrant, no pod restarts"
step reg alias "$ALIAS" "$NEW"
step ms wait --collection "$NEW" --pods 3 --out "$OUT/wait-switch.json"
mark "quality after the switch"
step ms quality --k "$K" --per-dataset "$PER_DATASET" --rate 20 --expect "$NEW" --label "after:  " \
  --out "$OUT/quality-after.json" || result=1
step ms compare "$OUT/quality-before.json" "$OUT/quality-after.json" --out "$OUT/quality-compare.json" || result=1

mark "rollback: one command"
step reg alias "$ALIAS" "$BASE"
step ms wait --collection "$BASE" --pods 3 --out "$OUT/wait-rollback.json"

mark "planted error: $ALIAS -> $BAD, vectors made by another model than the registry says"
step reg alias "$ALIAS" "$BAD"
step ms hold --seconds "$HOLD" --collection "$BASE" --refused "$BAD" --pods 3 --out "$OUT/hold-trap.json" || result=1

mark "cleanup: alias back on $BASE, trap removed"
step reg alias "$ALIAS" "$BASE"
step ms wait --collection "$BASE" --status verified --pods 3 --out "$OUT/wait-cleanup.json" || result=1
step reg drop "$BAD"
PLANTED=""

sleep "$TAIL"
"$HERE/load.sh" stop "$OUT/load.json" > "$OUT/load.txt"
LOAD=""
mark "load stopped"
sed 's/^/          /' "$OUT/load.txt" | tee -a "$OUT/timeline.txt"
echo
"$PY" -I -B "$HERE/model_switch.py" summary "$OUT" | tee "$OUT/summary.txt" || result=1
echo "results in ${OUT#"$ROOT"/}"
DONE=1
exit "$result"
