#!/bin/bash
# Собирает комплект стенда одной командой, на Маке:
#   ./kit-build.sh            основной комплект  -> kit/vm
#   ./kit-build.sh security   nmap, trivy с базой, kube-bench -> kit/vm-security
#   ./kit-build.sh falco      образ и чарт Falco -> kit/vm-falco
#   ./kit-build.sh monitoring Prometheus, node_exporter, Grafana -> kit/vm-monitoring
#                             (только после «да» владельца на список в deploy/monitoring/README.md)
# Шаги: одноразовая сборочная ВМ из machine.yaml (роль builder) -> внутри неё скрипт
# качает всё по утверждённому списку и сверяет суммы -> комплект копируется на Мак,
# суммы проверяются ещё раз -> сборочная ВМ удаляется.
# На Маке ничего чужого не запускается: только limactl, shasum и копирование файлов.
# У сборочной ВМ есть интернет, поэтому в сеть машин стенда (socket_vmnet) она не входит:
# networks = [], остаётся только встроенная сеть Lima, где localhost Мака закрыт.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PART="${1:-base}"
case "$PART" in
  base)     FETCH=kit-fetch.sh;          DEST="$ROOT/kit/vm" ;;
  security) FETCH=kit-fetch-security.sh; DEST="$ROOT/kit/vm-security" ;;
  falco)    FETCH=kit-fetch-falco.sh;    DEST="$ROOT/kit/vm-falco" ;;
  monitoring) FETCH=kit-fetch-monitoring.sh; DEST="$ROOT/kit/vm-monitoring" ;;
  *) echo "часть комплекта: base, security, falco или monitoring"; exit 1 ;;
esac
VM="builder-$PART"

if [ -e "$DEST" ]; then echo "комплект уже есть: $DEST (удали папку, чтобы собрать заново)"; exit 1; fi
limactl create --tty=false --name "$VM" --set '.param.ROLE = "builder" | .networks = []' "$HERE/machine.yaml"
trap 'limactl delete -f "$VM" >/dev/null 2>&1 || true' EXIT
limactl start --tty=false "$VM"
limactl copy "$HERE/$FETCH" "$VM:/tmp/$FETCH"
limactl shell --workdir /tmp "$VM" bash "/tmp/$FETCH" /home/ops.guest/kit
mkdir -p "$(dirname "$DEST")"
limactl copy -r "$VM:/home/ops.guest/kit" "$DEST"
( cd "$DEST" && shasum -a 256 -c SHA256SUMS --quiet ) && echo "суммы на Маке сошлись"
echo "комплект: $DEST"
