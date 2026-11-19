#!/bin/bash
# Замер предела стенда на синтетике: сколько векторов влезает и сколько запросов в секунду стенд
# держит, пока не растёт p99. Коллекция synth_* растёт ступенями (STAGES), векторы случайные
# (eval/limit_synth.py), исходные на диске, в памяти int8. На каждой ступени сторож следит за
# памятью и диском машин и останавливает загрузку до OOM; потом генератор panel/loadgen.py
# с machine-4 идёт через вход ступенями темпа (eval/limit_qps.py). Итог в eval/results/limit.md,
# сырые данные в runs/limit/<прогон>/. Подробности в deploy/limit/README.md.
#   deploy/limit/limit.sh run              замер целиком; RUN=runs/limit/… продолжает прерванный
#   deploy/limit/limit.sh status           синтетические коллекции, память и диск машин, куда ходит поиск
#   deploy/limit/limit.sh clean            поиск обратно на прежнюю коллекцию, удалить все synth_*
#   deploy/limit/limit.sh report [RUN…]    пересобрать eval/results/limit.md (по умолчанию последний прогон)
# Окружение: SCHEMA=rf2 (synth_rf2, 6 шардов по 2 копии) или rf3 (synth_rf3, 3 по 3);
#   STAGES="100000 250000 500000 1000000"; RATES="5 10 20 30 40 50"; WARM=10; MEASURE=60;
#   QUANT=int8 или none; ON_DISK=1; KEEP=1 оставить коллекцию после прогона;
#   QUESTIONS=panel/questions.txt; GUARD_OPTS и QPS_OPTS флаги для limit_synth.py load и limit_qps.py.
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY=/opt/homebrew/bin/python3
SCHEMA="${SCHEMA:-rf2}"
case "$SCHEMA" in
  rf2) COLL=synth_rf2; SHARDS=6; REPLICAS=2 ;;
  rf3) COLL=synth_rf3; SHARDS=3; REPLICAS=3 ;;
  *) echo "SCHEMA: rf2 или rf3"; exit 1 ;;
esac
STAGES="${STAGES:-100000 250000 500000 1000000}"
RATES="${RATES:-5 10 20 30 40 50}"
WARM="${WARM:-10}"; MEASURE="${MEASURE:-60}"
QUANT="${QUANT:-int8}"; ON_DISK="${ON_DISK:-1}"; KEEP="${KEEP:-0}"
QUESTIONS="${QUESTIONS:-$ROOT/panel/questions.txt}"
GUARD_OPTS="${GUARD_OPTS:-}"; QPS_OPTS="${QPS_OPTS:-}"
REMOTE=/home/ops.guest/limit   # machine-1: загрузчик и сторож
QDIR=/tmp/limit-qps            # machine-4: генератор; ключ входа лежит тут только на время лестницы
PORT=16370
t0=$(date +%s)
mark() { echo "  +$(( $(date +%s) - t0 )) с  $*"; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }

# limit_synth.py внутри machine-1: ключ из Secret во временный файл 600, туннель kubectl к пиру
# Qdrant на этой же машине. Туннель перезапускается, если оборвался: загрузка идёт долго, а повтор
# порции безопасен. По выходе туннель и ключ убираются. $1 какой ключ: api-key или read-only-api-key.
synth() {
  local key="$1"; shift
  limactl shell --workdir "$REMOTE" machine-1 sudo bash -s -- "$REMOTE" "$PORT" "$key" "$@" <<'EOS'
set -euo pipefail
cd "$1"; PORT=$2; KEYNAME=$3; shift 3
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
POD=$(kubectl -n qdrant get pods -l app.kubernetes.io/name=qdrant --field-selector spec.nodeName=machine-1 \
  -o jsonpath='{.items[0].metadata.name}')
[ -n "$POD" ] || { echo "на machine-1 нет пира Qdrant"; exit 1; }
K=$(mktemp -d); PF=""
cleanup() {
  if [ -n "$PF" ]; then kill "$PF" 2>/dev/null || true; fi
  pkill -f "port-forward pod/$POD $PORT:6333" 2>/dev/null || true
  rm -rf "$K"
}
trap cleanup EXIT
trap 'exit 143' TERM HUP INT
( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template="{{index .data \"$KEYNAME\"}}" | base64 -d > "$K/key" )
( while true; do kubectl -n qdrant port-forward "pod/$POD" "$PORT:6333" >/dev/null 2>&1; sleep 1; done ) & PF=$!
for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 1; done
python3 -I -B limit_synth.py "$@" --url "http://127.0.0.1:$PORT" --key-file "$K/key"
EOS
}

prepare_m1() {  # загрузчик на machine-1 в чистую папку
  limactl shell machine-1 sudo rm -rf "$REMOTE"
  limactl shell machine-1 mkdir -p "$REMOTE"
  limactl copy "$ROOT/eval/limit_synth.py" "machine-1:$REMOTE/limit_synth.py"
}

m1_stop() {  # загрузчик и туннель, оставшиеся от прерванного прогона: без терминала ssh их сам не гасит
  limactl shell machine-1 sudo bash -s -- stop "$REMOTE" "$PORT" <<'EOS' || true
pkill -f "bash -s -- $2 " 2>/dev/null
pkill -f "[l]imit_synth.py" 2>/dev/null
pkill -f "port-forward pod/[^ ]* $3:6333" 2>/dev/null
rm -rf "$2"
exit 0
EOS
}

m4_stop() {
  limactl shell machine-4 bash -c "pkill -f '[l]imit_qps.py' || true; rm -rf $QDIR" >/dev/null 2>&1 || true
}

prepare_m4() {  # генератор и вопросы на machine-4; ключ входа копируется на каждую лестницу отдельно
  ENTRY="$(limactl shell machine-4 bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1")"
  [ -n "$ENTRY" ] || { echo "у machine-4 нет адреса на lima0"; exit 1; }
  if limactl shell machine-4 bash -c "ps -eo args | grep -q '[l]oadgen.py'"; then
    echo "на machine-4 уже идёт генератор нагрузки (deploy/scenes/load.sh stop): замер исказится"; exit 1
  fi
  limactl shell machine-4 bash -c "rm -rf $QDIR && mkdir -m 700 $QDIR"
  limactl copy "$ROOT/panel/loadgen.py" "machine-4:$QDIR/loadgen.py"
  limactl copy "$ROOT/eval/limit_qps.py" "machine-4:$QDIR/limit_qps.py"
  limactl copy "$QUESTIONS" "machine-4:$QDIR/questions.txt"
  limactl copy "$ROOT/runs/stand-ca.pem" "machine-4:$QDIR/ca.pem"
}

stage_qps() {  # $1 число векторов, метка ступени
  limactl copy "$ROOT/runs/entry-api-key" "machine-4:$QDIR/key"
  limactl shell machine-4 chmod 600 "$QDIR/key"
  # shellcheck disable=SC2086  # QPS_OPTS это список флагов
  limactl shell --workdir "$QDIR" machine-4 python3 -I -B limit_qps.py --loadgen loadgen.py \
    --url "https://$ENTRY/search" --cafile ca.pem --api-key-file key --questions questions.txt \
    --collection "$COLL" --label "$1" --rates "$RATES" --warm "$WARM" --measure "$MEASURE" \
    --out "qps-$1.json" $QPS_OPTS
  limactl shell machine-4 rm -f "$QDIR/key"
  limactl copy "machine-4:$QDIR/qps-$1.json" "$OUT/qps-$1.json"
}

current_collection() {
  kc -n search get deploy search -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="COLLECTION")].value}'
}

use_collection() {  # $1 коллекция для сервиса поиска; поды сменяются по одному, поиск не прерывается
  kc -n search set env deploy/search "COLLECTION=$1" >/dev/null
  kc -n search rollout status deploy/search --timeout=300s >/dev/null
}

field() {  # $1 файл JSON, $2 поле верхнего уровня; пусто, если поля нет
  "$PY" -I -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"
}

latest_prev() {  # на какой коллекции был поиск до последнего прогона
  local f
  f=$(ls -t "$ROOT"/runs/limit/*/previous-collection 2>/dev/null | head -1 || true)
  if [ -n "$f" ]; then cat "$f"; else echo contracts_rf2; fi
}

SWITCHED=""; DONE=""; OUT=""; PREV=""; ENTRY=""
finish() {
  local code=$?
  trap - EXIT INT TERM
  set +e
  if [ -n "$SWITCHED" ]; then
    mark "поиск обратно на $PREV"
    use_collection "$PREV" || echo "  поиск не вернулся на $PREV: deploy/limit/limit.sh clean"
  fi
  m4_stop
  m1_stop
  if [ "$KEEP" = 1 ]; then
    echo "  $COLL оставлена (KEEP=1); убрать: deploy/limit/limit.sh clean"
  else
    prepare_m1 >/dev/null 2>&1 && synth api-key clean --collection "$COLL" \
      || echo "  $COLL не удалилась: deploy/limit/limit.sh clean"
    limactl shell machine-1 sudo rm -rf "$REMOTE" >/dev/null 2>&1
  fi
  if [ -z "$DONE" ]; then
    echo "прогон прерван (код $code). Продолжить: RUN=${OUT#"$ROOT"/} deploy/limit/limit.sh run"
  fi
  exit "$code"
}

run_all() {
  [ -f "$ROOT/runs/stand-ca.pem" ] && [ -f "$ROOT/runs/entry-api-key" ] \
    || { echo "нет runs/stand-ca.pem или runs/entry-api-key: сначала deploy/observer/install.sh"; exit 1; }
  [ -f "$QUESTIONS" ] || { echo "нет файла вопросов $QUESTIONS"; exit 1; }
  case "${RUN:-}" in
    "") OUT="$ROOT/runs/limit/$(date +%Y%m%d-%H%M%S)-$COLL" ;;
    /*) OUT="$RUN" ;;
    *) OUT="$ROOT/$RUN" ;;
  esac
  # куда вернуть поиск, узнаём до ловушки: если он уже на synth_*, ловушка не должна удалить его коллекцию
  if [ -f "$OUT/previous-collection" ]; then
    PREV=$(cat "$OUT/previous-collection")
  else
    PREV=$(current_collection)
    case "$PREV" in
      ""|synth_*) echo "поиск сейчас на «$PREV», а не на рабочей коллекции: сначала deploy/limit/limit.sh clean"; exit 1 ;;
    esac
  fi
  mkdir -p "$OUT"
  [ -f "$OUT/previous-collection" ] || echo "$PREV" > "$OUT/previous-collection"
  [ -f "$OUT/stages.txt" ] || echo "$STAGES" > "$OUT/stages.txt"
  if [ "$ON_DISK" = 1 ]; then DISK="--on-disk"; else DISK=""; fi
  echo "замер предела: $COLL ($SHARDS шардов по $REPLICAS копии), ступени $STAGES, темпы $RATES в секунду"
  echo "итоги в ${OUT#"$ROOT"/}; оценка времени от часа до нескольких часов, дольше всего строится индекс"
  trap finish EXIT
  trap 'exit 130' INT TERM

  m1_stop
  prepare_m1
  if [ -f "$OUT/baseline.json" ]; then
    limactl copy "$OUT/baseline.json" "machine-1:$REMOTE/baseline.json"
  else
    mark "снимок до синтетики"
    synth read-only-api-key snapshot --out baseline.json
    limactl copy "machine-1:$REMOTE/baseline.json" "$OUT/baseline.json"
    if [ -n "$(field "$OUT/baseline.json" level)" ]; then
      rm -f "$OUT/baseline.json"
      echo "пороги сторожа достигнуты ещё до синтетики (строка «порог» выше), грузить нельзя"; exit 1
    fi
  fi
  prepare_m4

  last=""
  for n in $STAGES; do
    if [ -f "$OUT/stage-$n.json" ] && [ -f "$OUT/qps-$n.json" ]; then
      outcome=$(field "$OUT/stage-$n.json" outcome)
      mark "ступень $n уже есть в прогоне ($outcome)"
      if [ "$outcome" = ok ]; then continue; fi
      break
    fi
    mark "ступень $n: загрузка до $n точек"
    code=0
    # shellcheck disable=SC2086  # DISK и GUARD_OPTS это списки флагов
    synth api-key load --collection "$COLL" --target "$n" --shards "$SHARDS" --replicas "$REPLICAS" \
      --quantization "$QUANT" $DISK --baseline baseline.json --out "stage-$n.json" $GUARD_OPTS || code=$?
    # исход берём из итога ступени, а не из кода выхода: так не важно, как его передаст limactl
    outcome=""
    if limactl copy "machine-1:$REMOTE/stage-$n.json" "$OUT/stage-$n.json" >/dev/null 2>&1; then
      outcome=$(field "$OUT/stage-$n.json" outcome)
    fi
    case "$outcome" in
      ok) ;;
      soft) last=1; mark "мягкий порог: ступень $n последняя" ;;
      hard) mark "жёсткий порог: синтетика удалена, нагрузку не меряем"; break ;;
      index_timeout) mark "индекс не достроился: на недостроенном индексе нагрузку не меряем"; break ;;
      *) mark "загрузка упала (код $code)"; exit 1 ;;
    esac
    if [ -z "$SWITCHED" ]; then
      mark "сервис поиска переключается на $COLL"
      SWITCHED=1
      use_collection "$COLL"
    fi
    mark "ступень $n: лестница нагрузки"
    stage_qps "$n"
    if [ -n "$last" ]; then break; fi
  done

  if [ -n "$SWITCHED" ]; then
    mark "поиск обратно на $PREV"
    use_collection "$PREV"
    SWITCHED=""
  fi
  "$PY" -I -B "$ROOT/eval/limit_report.py" "$OUT" --out "$ROOT/eval/results/limit.md"
  DONE=1
  mark "готово: eval/results/limit.md, сырые данные в ${OUT#"$ROOT"/}"
}

case "${1:-}" in
  run) run_all ;;
  status)
    prepare_m1
    trap 'limactl shell machine-1 sudo rm -rf "$REMOTE" >/dev/null 2>&1' EXIT
    synth read-only-api-key snapshot
    echo "  поиск ходит в коллекцию: $(current_collection)" ;;
  clean)
    cur=$(current_collection)
    case "$cur" in
      synth_*)
        prev=$(latest_prev)
        case "$prev" in ""|synth_*) prev=contracts_rf2 ;; esac
        echo "поиск ходит в $cur, возвращаю на $prev"
        use_collection "$prev" ;;
    esac
    m4_stop
    m1_stop
    prepare_m1
    trap 'limactl shell machine-1 sudo rm -rf "$REMOTE" >/dev/null 2>&1' EXIT
    synth api-key clean ;;
  report)
    shift
    if [ $# -eq 0 ]; then
      d=$(ls -td "$ROOT"/runs/limit/*/ 2>/dev/null | head -1 || true)
      [ -n "$d" ] || { echo "прогонов нет: runs/limit/ пуст"; exit 1; }
      set -- "$d"
    fi
    "$PY" -I -B "$ROOT/eval/limit_report.py" "$@" --out "$ROOT/eval/results/limit.md" ;;
  *) echo "run | status | clean | report [RUN…]"; exit 1 ;;
esac
