#!/bin/bash
# Мониторинг стенда из комплекта kit/vm-monitoring. Prometheus и Grafana живут на machine-4
# службами systemd, без контейнеров: наблюдатель вне кластера, и выключенная машина не уносит
# с собой измеритель. Оба слушают только 127.0.0.1, Grafana на Маке через проброс порта (README).
# node_exporter (процессор, память, нагрузка) ставится на все четыре машины; на машинах
# кластера его порт 9100 открыт только machine-4 (фильтр nftables в самой службе).
# В кластер идёт только k8s.yaml: NodePort и правило сети для метрик Falco. Мониторингу ключи
# и токены кластера не нужны (токен только на чтение на machine-4 есть у панели, deploy/panel).
#   deploy/monitoring/install.sh [machine-4]
#   NODES="machine-1 machine-2 machine-3" по умолчанию; KC=machine-1: где запускать kubectl
# Повторный запуск безопасен: адреса машин и цели Prometheus собираются заново.
# Пишется под системный bash 3.2 на macOS.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm-monitoring/bin"
VM="${1:-machine-4}"
NODES="${NODES:-machine-1 machine-2 machine-3}"
KC="${KC:-machine-1}"
PROM=3.16.0
NODEX=1.13.0
GRAFANA=13.3.1
PROM_TGZ="prometheus-$PROM.linux-arm64.tar.gz"
NODEX_TGZ="node_exporter-$NODEX.linux-arm64.tar.gz"
GRAFANA_TGZ="grafana_${GRAFANA}_42849527790_linux_arm64.tar.gz"
for f in "$PROM_TGZ" "$NODEX_TGZ" "$GRAFANA_TGZ"; do
  [ -f "$KIT/$f" ] || { echo "нет в комплекте: $KIT/$f (deploy/vm/kit-build.sh monitoring)"; exit 1; }
done
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }
ENTRY="$(ip_of "$VM")"
[ -n "$ENTRY" ] || { echo "у $VM нет адреса на lima0"; exit 1; }

# Цели Prometheus (file_sd): адрес и имя машины. 9100 node_exporter, 30080 поиск (NodePort,
# Local: каждая машина отвечает своим подом), 30765 Falco, 30633 шлюз Qdrant (развилка в README).
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
add() {  # файл, адрес, порт, машина
  [ -s "$1" ] && printf ',\n' >> "$1"
  printf '  {"targets": ["%s:%s"], "labels": {"machine": "%s"}}' "$2" "$3" "$4" >> "$1"
}
for j in node search falco qdrant; do : > "$T/$j"; done
for n in $NODES; do
  ip="$(ip_of "$n")"; [ -n "$ip" ] || { echo "у $n нет адреса на lima0"; exit 1; }
  add "$T/node" "$ip" 9100 "$n"
  add "$T/search" "$ip" 30080 "$n"
  add "$T/falco" "$ip" 30765 "$n"
  add "$T/qdrant" "$ip" 30633 "$n"
done
add "$T/node" 127.0.0.1 9100 "$VM"
if ! limactl shell "$KC" sudo k3s kubectl -n qdrant get svc qdrant-gate >/dev/null 2>&1; then
  : > "$T/qdrant"
  echo "Qdrant: шлюза в кластере нет (развилка в README), цели qdrant пока пустые"
fi
for j in node search falco qdrant; do { echo "["; cat "$T/$j"; echo; echo "]"; } > "$T/$j.json"; done

echo "== node_exporter $NODEX: $NODES $VM"
for n in $NODES $VM; do
  if [ "$n" = "$VM" ]; then listen=127.0.0.1; else listen="$(ip_of "$n")"; fi
  limactl copy "$KIT/$NODEX_TGZ" "$n:/tmp/node_exporter.tar.gz"
  limactl copy "$HERE/systemd/node-exporter.service" "$n:/tmp/node-exporter.service"
  limactl shell --workdir /tmp "$n" sudo bash -s -- "$n" "$listen" "$ENTRY" "$NODEX" <<'EOS'
set -euo pipefail
NAME=$1; LISTEN=$2; OBSERVER=$3; V=$4
tar -xzf /tmp/node_exporter.tar.gz --no-same-owner -C /tmp "node_exporter-$V.linux-arm64/node_exporter"
install -m 0755 "/tmp/node_exporter-$V.linux-arm64/node_exporter" /usr/local/bin/node_exporter
rm -rf /tmp/node_exporter.tar.gz "/tmp/node_exporter-$V.linux-arm64"
install -d -m 0755 /etc/node_exporter
# 9100 только для machine-4 и самой машины (lo); поды и соседние машины сюда не достанут
cat > /etc/node_exporter/guard.nft <<NFT
table inet node_exporter_guard
delete table inet node_exporter_guard
table inet node_exporter_guard {
  chain input {
    type filter hook input priority filter; policy accept;
    iifname "lo" accept
    tcp dport 9100 ip saddr != $OBSERVER counter drop
  }
}
NFT
sed "s/@LISTEN@/$LISTEN/" /tmp/node-exporter.service > /etc/systemd/system/node-exporter.service
rm -f /tmp/node-exporter.service
systemctl daemon-reload
systemctl enable -q node-exporter
systemctl restart node-exporter
sleep 1
n=$(curl -sf "http://$LISTEN:9100/metrics" | grep -c '^node_cpu_seconds_total' || true)
echo "$NAME: node_exporter на $LISTEN:9100, серий процессора: $n"
EOS
done

echo "== кластер: NodePort и правило сети для метрик Falco (k8s.yaml)"
limactl copy "$HERE/k8s.yaml" "$KC:/tmp/monitoring-k8s.yaml"
limactl shell --workdir /tmp "$KC" sudo bash -s -- "$ENTRY" <<'EOS'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
sed -i "s/ENTRY_IP/$1/" /tmp/monitoring-k8s.yaml
if kubectl get namespace falco >/dev/null 2>&1; then
  kubectl apply -f /tmp/monitoring-k8s.yaml >/dev/null
  kubectl -n falco get svc falco-metrics-node
else
  echo "Falco не установлен (deploy/falco/install.sh), метрики Falco пропускаем"
fi
rm -f /tmp/monitoring-k8s.yaml
EOS

echo "== Prometheus $PROM на $VM"
limactl shell "$VM" sudo rm -rf /tmp/prom-in
limactl shell "$VM" mkdir -p /tmp/prom-in/targets
limactl copy "$KIT/$PROM_TGZ" "$VM:/tmp/prom-in/prometheus.tar.gz"
limactl copy "$HERE/prometheus/prometheus.yml" "$VM:/tmp/prom-in/prometheus.yml"
limactl copy "$HERE/systemd/prometheus.service" "$VM:/tmp/prom-in/prometheus.service"
for j in node search falco qdrant; do limactl copy "$T/$j.json" "$VM:/tmp/prom-in/targets/$j.json"; done
limactl shell --workdir /tmp "$VM" sudo bash -s -- "$PROM" <<'EOS'
set -euo pipefail
V=$1; I=/tmp/prom-in
id prometheus >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin prometheus
if [ ! -x "/opt/prometheus-$V/prometheus" ]; then
  tar -xzf "$I/prometheus.tar.gz" --no-same-owner -C /opt
  rm -rf "/opt/prometheus-$V" && mv "/opt/prometheus-$V.linux-arm64" "/opt/prometheus-$V"
fi
ln -sfn "/opt/prometheus-$V/prometheus" /usr/local/bin/prometheus
ln -sfn "/opt/prometheus-$V/promtool" /usr/local/bin/promtool
install -d -m 0755 /etc/prometheus /etc/prometheus/targets
install -d -m 0750 -o prometheus -g prometheus /var/lib/prometheus
install -m 0644 "$I/prometheus.yml" /etc/prometheus/prometheus.yml
install -m 0644 "$I"/targets/*.json /etc/prometheus/targets/
promtool check config /etc/prometheus/prometheus.yml >/dev/null
install -m 0644 "$I/prometheus.service" /etc/systemd/system/prometheus.service
rm -rf "$I"
systemctl daemon-reload
systemctl enable -q prometheus
systemctl restart prometheus
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:9090/-/ready >/dev/null && break; sleep 1; done
echo "Prometheus: $(curl -sf http://127.0.0.1:9090/-/ready || echo 'не готов')"
EOS

echo "== Grafana $GRAFANA на $VM"
limactl shell "$VM" sudo rm -rf /tmp/grafana-in
limactl shell "$VM" mkdir -p /tmp/grafana-in
limactl copy "$KIT/$GRAFANA_TGZ" "$VM:/tmp/grafana-in/grafana.tar.gz"
limactl copy "$HERE/grafana/grafana.ini" "$VM:/tmp/grafana-in/grafana.ini"
limactl copy "$HERE/grafana/provisioning/datasources/prometheus.yaml" "$VM:/tmp/grafana-in/datasource.yaml"
limactl copy "$HERE/grafana/provisioning/dashboards/stand.yaml" "$VM:/tmp/grafana-in/dashboards.yaml"
limactl copy "$HERE/grafana/dashboards/stand.json" "$VM:/tmp/grafana-in/stand.json"
limactl copy "$HERE/systemd/grafana.service" "$VM:/tmp/grafana-in/grafana.service"
limactl shell --workdir /tmp "$VM" sudo bash -s -- "$GRAFANA" <<'EOS'
set -euo pipefail
V=$1; I=/tmp/grafana-in
id grafana >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin grafana
if [ ! -x "/opt/grafana-$V/bin/grafana" ]; then
  P="/opt/grafana-$V.part"; rm -rf "$P" && mkdir -p "$P"
  tar -xzf "$I/grafana.tar.gz" --no-same-owner -C "$P"
  # в архиве одна верхняя папка (её имя пишет SOURCES.tsv) или сразу bin/, conf/, public/
  H=$(dirname "$(dirname "$(find "$P" -maxdepth 3 -path '*/bin/grafana' -type f | head -1)")")
  [ -x "$H/bin/grafana" ] || { echo "в архиве Grafana нет bin/grafana"; exit 1; }
  rm -rf "/opt/grafana-$V" && mv "$H" "/opt/grafana-$V"
  rm -rf "$P"
fi
ln -sfn "/opt/grafana-$V" /opt/grafana
install -d -m 0755 /etc/grafana /etc/grafana/provisioning /etc/grafana/provisioning/datasources \
  /etc/grafana/provisioning/dashboards /etc/grafana/provisioning/plugins \
  /etc/grafana/provisioning/alerting /etc/grafana/dashboards
install -d -m 0750 -o grafana -g grafana /var/lib/grafana /var/log/grafana
# источник Prometheus (с 13.2 отдельный плагин) лежит в архиве в plugins-bundled; интернета нет,
# поэтому путь к этой папке задаём явно. Нет папки: Prometheus встроен (до 13.2), строку убираем
B=$(find "/opt/grafana-$V" -maxdepth 3 -type d -name plugins-bundled | head -1)
if [ -n "$B" ]; then sed "s|@BUNDLED_PLUGINS@|$B|" "$I/grafana.ini"; else sed '/@BUNDLED_PLUGINS@/d' "$I/grafana.ini"; fi > /etc/grafana/grafana.ini
if [ ! -s /etc/grafana/admin-password ]; then
  ( umask 027; openssl rand -hex 16 > /etc/grafana/admin-password )
fi
if [ ! -s /etc/grafana/secret-key ]; then
  ( umask 027; openssl rand -hex 32 > /etc/grafana/secret-key )
fi
chgrp grafana /etc/grafana/admin-password /etc/grafana/secret-key /etc/grafana/grafana.ini
chmod 0640 /etc/grafana/admin-password /etc/grafana/secret-key /etc/grafana/grafana.ini
install -m 0644 "$I/datasource.yaml" /etc/grafana/provisioning/datasources/prometheus.yaml
install -m 0644 "$I/dashboards.yaml" /etc/grafana/provisioning/dashboards/stand.yaml
install -m 0644 "$I/stand.json" /etc/grafana/dashboards/stand.json
install -m 0644 "$I/grafana.service" /etc/systemd/system/grafana.service
rm -rf "$I"
systemctl daemon-reload
systemctl enable -q grafana
systemctl restart grafana
for _ in $(seq 1 60); do curl -sf http://127.0.0.1:3000/api/health >/dev/null && break; sleep 2; done
curl -sf http://127.0.0.1:3000/api/health >/dev/null || { echo "Grafana не ответила"; journalctl -u grafana -n 30 --no-pager; exit 1; }
echo "Grafana: plugins-bundled ${B:-нет (Prometheus встроен)}"
# источник данных отвечает: спрашиваем от имени admin, пароль идёт файлом netrc, не аргументом
N=$(mktemp); trap 'rm -f "$N"' EXIT
( umask 077; printf 'machine 127.0.0.1 login admin password %s\n' "$(cat /etc/grafana/admin-password)" > "$N" )
curl -s --netrc-file "$N" http://127.0.0.1:3000/api/datasources/uid/prometheus/health > /tmp/grafana-ds.json || true
python3 -I - /tmp/grafana-ds.json <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(f"источник Prometheus в Grafana: {d.get('status')} {d.get('message', '')}")
except Exception as e:
    print(f"источник Prometheus в Grafana: нет ответа ({e})")
PY
rm -f /tmp/grafana-ds.json
EOS

echo "== цели Prometheus (через 10 с, два круга скрейпа)"
sleep 10
limactl shell --workdir /tmp "$VM" bash -s <<'EOS'
set -euo pipefail
curl -sf http://127.0.0.1:9090/api/v1/targets > /tmp/prom-targets.json
python3 -I - /tmp/prom-targets.json <<'PY'
import json, sys
hint = {
    "search": "в сервисе поиска ещё нет /metrics (контракт в README мониторинга)",
    "haproxy": "в haproxy.cfg.in нет строки prometheus-exporter (README мониторинга)",
    "falco": "метрики не включены в deploy/falco/values.yaml (README мониторинга)",
    "qdrant": "шлюз Qdrant не отвечает",
}
targets = json.load(open(sys.argv[1]))["data"]["activeTargets"]
for t in sorted(targets, key=lambda t: (t["labels"]["job"], t["labels"].get("machine", ""))):
    job, machine = t["labels"]["job"], t["labels"].get("machine", "")
    ok = t["health"] == "up"
    line = f"  {'✓' if ok else '✗'} {job:10} {machine:10} {t['scrapeUrl']}"
    if not ok:
        line += f"  ({t.get('lastError', '')[:70]}) {hint.get(job, '')}"
    print(line)
PY
rm -f /tmp/prom-targets.json
EOS
echo "Grafana: на Маке http://127.0.0.1:13000 после проброса порта (README); смотреть можно без входа,"
echo "  admin: пароль limactl shell $VM sudo cat /etc/grafana/admin-password"
