#!/bin/bash
# Нагрузка для сцен: генератор panel/loadgen.py на machine-4 идёт во вход https, как пользователь,
# и проверяет сертификат по УЦ стенда.
#   deploy/scenes/load.sh start [RATE]      запустить в фоне (по умолчанию 20 запросов в секунду)
#   deploy/scenes/load.sh stop FILE.json    остановить, забрать снимок, напечатать итог
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VM=machine-4
DIR=/tmp/scene-load

case "${1:-}" in
  start)
    RATE="${2:-20}"
    ENTRY="$(limactl shell "$VM" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1")"
    [ -f "$ROOT/runs/stand-ca.pem" ] && [ -f "$ROOT/runs/entry-api-key" ] \
      || { echo "нет runs/stand-ca.pem или runs/entry-api-key: сначала deploy/observer/install.sh"; exit 1; }
    limactl shell "$VM" bash -c "pkill -f '[l]oadgen.py' || true; rm -rf $DIR; mkdir -p $DIR"
    limactl copy "$ROOT/panel/loadgen.py" "$VM:$DIR/loadgen.py"
    limactl copy "$ROOT/panel/questions.txt" "$VM:$DIR/questions.txt"
    limactl copy "$ROOT/runs/stand-ca.pem" "$VM:$DIR/ca.pem"
    limactl copy "$ROOT/runs/entry-api-key" "$VM:$DIR/key"
    limactl shell "$VM" chmod 600 "$DIR/key"
    limactl shell --workdir "$DIR" "$VM" bash -c "setsid nohup python3 -I -B loadgen.py --url https://$ENTRY/search \
      --cafile ca.pem --api-key-file key --rate $RATE --timeout 2 --retry other --questions questions.txt \
      --stats-file $DIR/load.json --quiet > $DIR/loadgen.log 2>&1 < /dev/null &"
    for _ in $(seq 1 20); do limactl shell "$VM" test -s "$DIR/load.json" && break; sleep 1; done
    limactl shell "$VM" test -s "$DIR/load.json" || { echo "генератор не стартовал:"; limactl shell "$VM" cat "$DIR/loadgen.log"; exit 1; }
    echo "нагрузка: $RATE запросов в секунду на https://$ENTRY/search" ;;
  stop)
    FILE="${2:?куда сохранить снимок}"
    limactl shell "$VM" bash -c "pkill -TERM -f '[l]oadgen.py' || true"
    sleep 2
    limactl copy "$VM:$DIR/load.json" "$FILE"
    limactl shell "$VM" rm -f "$DIR/key"
    /opt/homebrew/bin/python3 -I -B "$HERE/load_summary.py" "$FILE" ;;
  *) echo "start [RATE] | stop FILE.json"; exit 1 ;;
esac
