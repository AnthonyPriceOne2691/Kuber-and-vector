#!/bin/bash
# Атака снаружи, шаг 1: скан портов стенда с машины «снаружи» (deploy/security/outside.sh).
# Все 65 535 портов TCP четырёх машин и несколько UDP, потом «стук» руками: Qdrant 6333 и API k3s
# 6443 напрямую, вход без ключа и перебор ключей. Ждём: открыт только 443 на machine-4, всё
# остальное молчит (фильтр входящих deploy/vm/ingress.sh), без ключа 401, перебор получает 429.
#   deploy/security/scan-ports.sh
#   RATE=3000 пакетов в секунду (по умолчанию); KEEP=1 оставить машину «снаружи» после скана
# Итог: runs/security/<время>/nmap-tcp.xml, nmap-udp.xml, knock.txt и сводка summary.md.
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
VM="${OUTSIDE_VM:-outside}"
RATE="${RATE:-3000}"
OUT="$ROOT/runs/security/$(date +%Y%m%d-%H%M%S)"
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }

targets=""; names=""
for m in machine-1 machine-2 machine-3 machine-4; do
  ip="$(ip_of "$m")"; [ -n "$ip" ] || { echo "$m has no address on lima0"; exit 1; }
  targets="$targets $ip"; names="$names $m=$ip"
done
M1="$(ip_of machine-1)"; ENTRY="$(ip_of machine-4)"
mkdir -p "$OUT"
echo "$names" | tr ' ' '\n' | sed '/^$/d' > "$OUT/machines.txt"

OUTSIDE_VM="$VM" "$HERE/outside.sh" up
[ "${KEEP:-0}" = 1 ] || trap 'OUTSIDE_VM="$VM" "$HERE/outside.sh" down' EXIT
echo "outside machine address: $(ip_of "$VM")"

t0=$(date +%s)
# shellcheck disable=SC2086  # targets это список адресов
limactl shell "$VM" sudo nmap -Pn -n -sS -p- --max-retries 1 --min-rate "$RATE" -oX /tmp/nmap-tcp.xml $targets >/dev/null
# shellcheck disable=SC2086
limactl shell "$VM" sudo nmap -Pn -n -sU -p 53,67,123,2379,4789,6443,8472,51820 --max-retries 1 -oX /tmp/nmap-udp.xml $targets >/dev/null
echo "scan took $(( $(date +%s) - t0 )) s"
for f in nmap-tcp.xml nmap-udp.xml; do limactl copy "$VM:/tmp/$f" "$OUT/$f"; done

# «Стук» руками: код ответа curl (000 значит, что соединения не было вовсе)
knock() {  # $1 описание (по-английски: идёт в кадр), $2… аргументы curl
  local what="$1"; shift
  printf '%s\t%s\n' "$what" "$(limactl shell "$VM" curl -s -o /dev/null -w '%{http_code}' -m 4 "$@" || true)"
}
{
  knock "Qdrant machine-1:6333 direct" "http://$M1:6333/collections"
  knock "k3s API machine-1:6443 direct" -k "https://$M1:6443/version"
  knock "kubelet machine-1:10250 direct" -k "https://$M1:10250/pods"
  knock "search through the entry without a key" -k "https://$ENTRY/search?q=test"
  for i in $(seq 1 15); do knock "key guessing, attempt $i" -k -H "X-API-Key: guess-$i" "https://$ENTRY/search?q=test"; done
} > "$OUT/knock.txt"

status=0
"$PY" -I -B "$HERE/summary.py" ports "$OUT" > "$OUT/summary.md" || status=$?
cat "$OUT/summary.md"
echo "files: $OUT"
exit "$status"
