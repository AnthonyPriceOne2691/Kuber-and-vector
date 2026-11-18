#!/bin/bash
# Вход стенда на machine-4: HAProxy из комплекта, TLS от своего УЦ стенда, серверы по адресам
# узлов кластера. Ключ УЦ и ключ сертификата не покидают machine-4; на Мак уходит только
# открытый сертификат УЦ (runs/stand-ca.pem), чтобы curl и браузер проверяли TLS по-честному.
#   deploy/observer/install.sh [machine-4]
#   NODES="machine-1 machine-2 machine-3" по умолчанию
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm"
VM="${1:-machine-4}"
NODES="${NODES:-machine-1 machine-2 machine-3}"
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }

ENTRY="$(ip_of "$VM")"
[ -n "$ENTRY" ] || { echo "у $VM нет адреса на lima0"; exit 1; }
search="" ; api=""
for n in $NODES; do
  ip="$(ip_of "$n")"; [ -n "$ip" ] || { echo "у $n нет адреса на lima0"; exit 1; }
  search="$search    server $n $ip:30080"$'\n'
  api="$api    server $n $ip:6443"$'\n'
done
mkdir -p "$ROOT/runs"
CFG="$ROOT/runs/haproxy.cfg"
# через окружение: awk на macOS не принимает перевод строки в -v
SERVERS_SEARCH="$search" SERVERS_API="$api" awk '
  /^@SEARCH_SERVERS@$/ { printf "%s", ENVIRON["SERVERS_SEARCH"]; next }
  /^@API_SERVERS@$/    { printf "%s", ENVIRON["SERVERS_API"]; next }
  { print }' "$HERE/haproxy.cfg.in" > "$CFG"

limactl shell "$VM" sudo rm -rf /tmp/kit-debs
limactl shell "$VM" mkdir -p /tmp/kit-debs-in
for f in "$KIT"/debs/haproxy_*.deb "$KIT"/debs/liblua5.4-0_*.deb; do limactl copy "$f" "$VM:/tmp/kit-debs-in/"; done
limactl copy "$CFG" "$VM:/tmp/haproxy.cfg"

limactl shell --workdir /tmp "$VM" sudo bash -s -- "$ENTRY" <<'EOS'
set -euo pipefail
ENTRY=$1
if ! command -v haproxy >/dev/null; then
  # интернета у машины нет: только пакеты комплекта; /tmp, чтобы их читал пользователь _apt
  mv /tmp/kit-debs-in /tmp/kit-debs && chmod -R a+rX /tmp/kit-debs
  # репозитории из интернета отключены: DNS машинам стенда закрыт, apt берёт только файлы комплекта
  mkdir -p /tmp/apt-no-sources
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts=/tmp/apt-no-sources /tmp/kit-debs/*.deb >/dev/null
fi
rm -rf /tmp/kit-debs /tmp/kit-debs-in
haproxy -v | head -1

# УЦ стенда и сертификат входа: ECDSA P-256, сертификат на 30 дней, адрес входа в SAN
C=/etc/haproxy/certs; install -d -m 0700 "$C"
if [ ! -f "$C/ca.key" ]; then
  ( umask 077
    openssl ecparam -name prime256v1 -genkey -noout -out "$C/ca.key"
    openssl req -x509 -new -key "$C/ca.key" -sha256 -days 90 -subj "/CN=stand CA" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -out "$C/ca.pem" )
fi
if [ ! -f "$C/stand.pem" ] || ! openssl x509 -in "$C/stand.pem" -noout -ext subjectAltName 2>/dev/null | grep -q "IP Address:$ENTRY"; then
  ( umask 077
    openssl ecparam -name prime256v1 -genkey -noout -out "$C/server.key"
    openssl req -new -key "$C/server.key" -subj "/CN=stand.local" -out "$C/server.csr"
    printf 'subjectAltName=DNS:stand.local,IP:%s\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature\nbasicConstraints=CA:FALSE\n' "$ENTRY" > "$C/server.ext"
    openssl x509 -req -in "$C/server.csr" -CA "$C/ca.pem" -CAkey "$C/ca.key" -CAcreateserial -days 30 -sha256 \
      -extfile "$C/server.ext" -out "$C/server.crt" 2>/dev/null
    cat "$C/server.crt" "$C/server.key" > "$C/stand.pem"
    rm -f "$C/server.csr" "$C/server.ext" "$C/server.key" )
fi
install -m 0644 "$C/ca.pem" /tmp/stand-ca.pem

install -m 0644 /tmp/haproxy.cfg /etc/haproxy/haproxy.cfg && rm -f /tmp/haproxy.cfg
haproxy -c -q -f /etc/haproxy/haproxy.cfg
systemctl enable -q haproxy
systemctl restart haproxy
sleep 3
echo "слушает:"; ss -ltnp | awk 'NR>1 && /haproxy/ {print "  " $4}'
echo "серверы:"; curl -s 'http://127.0.0.1:8404/stats;csv' | awk -F, 'NR>1 && $2!="FRONTEND" && $2!="BACKEND" && $1!="stats" {print "  " $1 "/" $2 ": " $18}' || true
EOS
limactl copy "$VM:/tmp/stand-ca.pem" "$ROOT/runs/stand-ca.pem"
limactl shell "$VM" sudo rm -f /tmp/stand-ca.pem
echo "вход: https://$ENTRY  (УЦ стенда: runs/stand-ca.pem)"
