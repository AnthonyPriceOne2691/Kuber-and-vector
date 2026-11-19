#!/bin/bash
# Состояние пода поиска на каждой машине кластера: его /readyz через NodePort 30080 с machine-4.
# У сервиса externalTrafficPolicy Local, поэтому машина отвечает только своим подом; machine-4 пускают
# на 30080 и файрвол, и правило сети (тем же путём HAProxy проверяет здоровье подов). В под ничего
# не запускается, поэтому Falco на это не тревожится.
# Печатает строку на машину: «machine-N {тело /readyz}» (коллекция, модель, model_check).
#   deploy/scenes/pod_states.sh
#   NODE_IPS="machine-1=IP machine-2=IP machine-3=IP" — адреса заранее (сцена считает их один раз),
#   без него адреса узнаются здесь же, это ещё около секунды.
# Нужен deploy/scenes/model_switch.py (--pods-cmd). Пишется под системный bash 3.2 на macOS.
set -euo pipefail
pairs="${NODE_IPS:-}"
if [ -z "$pairs" ]; then
  for m in machine-1 machine-2 machine-3; do
    ip=$(limactl shell "$m" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1" 2>/dev/null || true)
    if [ -n "$ip" ]; then pairs="$pairs $m=$ip"; fi
  done
fi
# shellcheck disable=SC2086  # список пар «машина=адрес»
limactl shell machine-4 bash -s -- $pairs <<'EOS'
for p in "$@"; do
  echo "${p%%=*} $(curl -s -m 2 "http://${p#*=}:30080/readyz" || true)"
done
EOS
