#!/bin/bash
# Нагрузка для сцен: генератор panel/loadgen.py на machine-4 идёт во вход https, как пользователь,
# и проверяет сертификат по УЦ стенда.
#   deploy/scenes/load.sh start [RATE]      запустить в фоне (по умолчанию 20 запросов в секунду)
#   deploy/scenes/load.sh stop FILE.json    остановить, забрать снимок, напечатать итог
# Снимок статистики (раз в 0,5 с) генератор пишет в STATS: его же читает панель на machine-4
# (deploy/panel, --load-stats). У службы панели свой /tmp, поэтому снимок лежит в /run/stand-load.
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VM=machine-4
DIR=/tmp/scene-load
STATS_DIR=/run/stand-load
STATS=$STATS_DIR/load.json

case "${1:-}" in
  start)
    RATE="${2:-20}"
    ENTRY="$(limactl shell "$VM" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1")"
    [ -f "$ROOT/runs/stand-ca.pem" ] && [ -f "$ROOT/runs/entry-api-key" ] \
      || { echo "нет runs/stand-ca.pem или runs/entry-api-key: сначала deploy/observer/install.sh"; exit 1; }
    limactl shell "$VM" bash -c "pkill -f '[l]oadgen.py' || true; rm -rf $DIR; mkdir -p $DIR"
    # папка снимка: пишет пользователь машины (генератор), читают все (панель); /run пустеет при
    # перезагрузке, поэтому создаётся здесь, на каждом старте; старый снимок убирается
    limactl shell "$VM" sudo bash -c "install -d -m 0755 -o \"\$SUDO_USER\" -g \"\$(id -gn \"\$SUDO_USER\")\" $STATS_DIR && rm -f $STATS"
    limactl copy "$ROOT/panel/loadgen.py" "$VM:$DIR/loadgen.py"
    limactl copy "$ROOT/panel/questions.txt" "$VM:$DIR/questions.txt"
    limactl copy "$ROOT/runs/stand-ca.pem" "$VM:$DIR/ca.pem"
    limactl copy "$ROOT/runs/entry-api-key" "$VM:$DIR/key"
    limactl shell "$VM" chmod 600 "$DIR/key"
    limactl shell --workdir "$DIR" "$VM" bash -c "setsid nohup python3 -I -B loadgen.py --url https://$ENTRY/search \
      --cafile ca.pem --api-key-file key --rate $RATE --timeout 2 --retry other --questions questions.txt \
      --stats-file $STATS --quiet > $DIR/loadgen.log 2>&1 < /dev/null &"
    for _ in $(seq 1 20); do limactl shell "$VM" test -s "$STATS" && break; sleep 1; done
    limactl shell "$VM" test -s "$STATS" || { echo "генератор не стартовал:"; limactl shell "$VM" cat "$DIR/loadgen.log"; exit 1; }
    echo "нагрузка: $RATE запросов в секунду на https://$ENTRY/search (снимок для панели: $STATS)" ;;
  stop)
    FILE="${2:?куда сохранить снимок}"
    limactl shell "$VM" bash -c "pkill -TERM -f '[l]oadgen.py' || true"
    sleep 2
    limactl copy "$VM:$STATS" "$FILE"
    limactl shell "$VM" rm -f "$DIR/key"
    /opt/homebrew/bin/python3 -I -B "$HERE/load_summary.py" "$FILE" ;;
  *) echo "start [RATE] | stop FILE.json"; exit 1 ;;
esac
