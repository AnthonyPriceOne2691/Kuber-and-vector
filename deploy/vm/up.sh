#!/bin/bash
# Поднимает стенд из одной схемы (machine.yaml):
#   machine-1…3  роль cluster   серверы k3s и пиры Qdrant
#   machine-4    роль observer  вход, нагрузка, панель
# Повторный запуск безопасен: существующие машины только запускаются.
#   ./up.sh                    все четыре
#   ./up.sh machine-1          только указанные
# Пишется под системный bash 3.2 на macOS: без пустых массивов при set -u.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$HERE/machine.yaml"

role_of() { case "$1" in machine-4) echo observer ;; machine-*) echo cluster ;; *) echo strict ;; esac; }

ensure() {
  local name="$1" role
  role="$(role_of "$name")"
  if limactl list -q 2>/dev/null | grep -qx "$name"; then return 0; fi
  if [ "$role" = observer ]; then
    # Вход стенда: 443 на machine-4 виден на Маке как https://127.0.0.1:8443 («интернет»);
    # Grafana: только localhost machine-4, на Маке http://127.0.0.1:13000
    limactl create --tty=false --name "$name" --set ".param.ROLE = \"$role\"" \
      --set '.portForwards = [{"guestPort": 443, "hostPort": 8443}, {"guestIP": "127.0.0.1", "guestPort": 3000, "hostPort": 13000}] + .portForwards' "$TEMPLATE"
  else
    limactl create --tty=false --name "$name" --set ".param.ROLE = \"$role\"" "$TEMPLATE"
  fi
}

if [ $# -gt 0 ]; then set -- "$@"; else set -- machine-1 machine-2 machine-3 machine-4; fi
for m in "$@"; do ensure "$m"; done
for m in "$@"; do limactl start --tty=false "$m" & done
wait
limactl list
