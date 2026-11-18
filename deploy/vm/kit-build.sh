#!/bin/bash
# Собирает комплект стенда одной командой, на Маке:
#   ./kit-build.sh            основной комплект  -> kit/vm
#   ./kit-build.sh security   nmap, trivy с базой, kube-bench -> kit/vm-security
# Шаги: одноразовая сборочная ВМ из machine.yaml (роль builder) -> внутри неё скрипт
# качает всё по утверждённому списку и сверяет суммы -> комплект копируется на Мак,
# суммы проверяются ещё раз -> сборочная ВМ удаляется.
# На Маке ничего чужого не запускается: только limactl, shasum и копирование файлов.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PART="${1:-base}"
case "$PART" in
  base)     FETCH=kit-fetch.sh;          DEST="$ROOT/kit/vm" ;;
  security) FETCH=kit-fetch-security.sh; DEST="$ROOT/kit/vm-security" ;;
  *) echo "часть комплекта: base или security"; exit 1 ;;
esac
VM="builder-$PART"

if [ -e "$DEST" ]; then echo "комплект уже есть: $DEST (удали папку, чтобы собрать заново)"; exit 1; fi
limactl create --tty=false --name "$VM" --set '.param.ROLE = "builder"' "$HERE/machine.yaml"
trap 'limactl delete -f "$VM" >/dev/null 2>&1 || true' EXIT
limactl start --tty=false "$VM"
limactl copy "$HERE/$FETCH" "$VM:/tmp/$FETCH"
limactl shell --workdir /tmp "$VM" bash "/tmp/$FETCH" /home/ops.guest/kit
mkdir -p "$(dirname "$DEST")"
limactl copy -r "$VM:/home/ops.guest/kit" "$DEST"
( cd "$DEST" && shasum -a 256 -c SHA256SUMS --quiet ) && echo "суммы на Маке сошлись"
echo "комплект: $DEST"
