#!/bin/bash
# Фильтр входящих на сети машин (lima0) на всех четырёх машинах. Правила собирает
# deploy/vm/ingress.py по текущим адресам машин, служба stand-ingress ставит их на каждом
# старте ещё до подъёма сети, поэтому выключенная и включённая машина возвращается под фильтром.
#   deploy/vm/ingress.sh           поставить или обновить (адреса берутся заново)
#   deploy/vm/ingress.sh status    сколько пакетов отбито на каждой машине
#   deploy/vm/ingress.sh off       снять со всех машин, если на стенде что-то сломалось
# Встроенную сеть Lima (eth0) фильтр не трогает, поэтому ssh и limactl работают и при ошибке в правилах.
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
CLUSTER_VMS="${CLUSTER_VMS:-machine-1 machine-2 machine-3}"
ENTRY_VM="${ENTRY_VM:-machine-4}"
MODE="${1:-apply}"
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }

case "$MODE" in
  status)
    for vm in $CLUSTER_VMS $ENTRY_VM; do
      n=$({ limactl shell "$vm" sudo nft list counter inet stand_ingress dropped 2>/dev/null || true; } \
        | awk '/packets/ {for (i = 1; i < NF; i++) if ($i == "packets") print $(i + 1)}')
      echo "$vm: ${n:-фильтра нет} отбито пакетов"
    done
    exit 0 ;;
  off)
    for vm in $CLUSTER_VMS $ENTRY_VM; do
      limactl shell "$vm" sudo bash -c 'systemctl disable -q --now stand-ingress 2>/dev/null || true
        rm -f /etc/stand/ingress.nft /etc/systemd/system/stand-ingress.service; systemctl daemon-reload
        nft delete table inet stand_ingress 2>/dev/null || true'
      echo "$vm: фильтр входящих снят"
    done
    exit 0 ;;
  apply) ;;
  *) echo "режим: apply (по умолчанию), status или off"; exit 1 ;;
esac

cluster=""
for vm in $CLUSTER_VMS; do
  ip="$(ip_of "$vm")"; [ -n "$ip" ] || { echo "у $vm нет адреса на lima0"; exit 1; }
  cluster="${cluster:+$cluster,}$ip"
done
entry="$(ip_of "$ENTRY_VM")"; [ -n "$entry" ] || { echo "у $ENTRY_VM нет адреса на lima0"; exit 1; }
echo "кластер $cluster, вход $entry"

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
"$PY" -I -B "$HERE/ingress.py" --role cluster --cluster "$cluster" --entry "$entry" > "$T/cluster.nft"
"$PY" -I -B "$HERE/ingress.py" --role observer --cluster "$cluster" --entry "$entry" > "$T/observer.nft"

for vm in $CLUSTER_VMS $ENTRY_VM; do
  if [ "$vm" = "$ENTRY_VM" ]; then rules="$T/observer.nft"; else rules="$T/cluster.nft"; fi
  limactl copy "$rules" "$vm:/tmp/stand-ingress.nft"
  limactl copy "$HERE/stand-ingress.service" "$vm:/tmp/stand-ingress.service"
  limactl shell --workdir /tmp "$vm" sudo bash -s -- "$vm" <<'EOS'
set -euo pipefail
nft -c -f /tmp/stand-ingress.nft  # синтаксис до установки: с ошибкой старый фильтр остаётся как был
install -d -m 0755 /etc/stand
install -m 0644 /tmp/stand-ingress.nft /etc/stand/ingress.nft
install -m 0644 /tmp/stand-ingress.service /etc/systemd/system/stand-ingress.service
rm -f /tmp/stand-ingress.nft /tmp/stand-ingress.service
systemctl daemon-reload
systemctl enable -q stand-ingress
systemctl reload-or-restart stand-ingress  # nft -f заменяет таблицу одной транзакцией
nft list counter inet stand_ingress dropped >/dev/null
echo "$1: фильтр входящих стоит"
EOS
done
