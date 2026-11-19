#!/bin/bash
# Панель стенда на machine-4: агрегатор panel/aggregator.py службой systemd stand-panel под своим
# пользователем (stand-panel.service). Страница только на 127.0.0.1:8090, на Маке
# http://127.0.0.1:18090 (проброс Lima из deploy/vm/up.sh); адрес lima0:8090 принимает только
# POST /api/event с токеном, для http_output Falco.
#   - Kubernetes только на чтение: учётная запись stand-panel/panel-reader (rbac.yaml), токен
#     TokenRequest на TOKEN_TTL, API через вход HAProxy 127.0.0.1:6443 с проверкой по CA кластера;
#   - Qdrant через шлюз каждой машины (:30633), ключа Qdrant на machine-4 нет;
#   - нагрузка: снимок генератора /run/stand-load/load.json (пишет deploy/scenes/load.sh);
#   - события Falco: токен генерируется здесь один раз, адрес с ним уходит в Secret
#     falco/falco-panel-output (deploy/falco/values.yaml монтирует его в Falco).
# Нужны вход (deploy/observer/install.sh), шлюз Qdrant (deploy/qdrant/gate-install.sh), Falco.
#   deploy/panel/install.sh [machine-4]
#   NODES="machine-1 machine-2 machine-3", KC=machine-1 (где kubectl), TOKEN_TTL=24h, COLLECTION=contracts_rf2
# Повторный запуск безопасен: код и адреса обновляются, токен Kubernetes выпускается новый, а прежний
# сразу перестаёт действовать (учётная запись пересоздаётся с новым UID). Токен событий остаётся.
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VM="${1:-machine-4}"
NODES="${NODES:-machine-1 machine-2 machine-3}"
KC="${KC:-machine-1}"
TOKEN_TTL="${TOKEN_TTL:-24h}"
COLLECTION="${COLLECTION:-contracts_rf2}"
PORT=8090
case "$COLLECTION" in ""|*[!A-Za-z0-9_-]*) echo "COLLECTION: только латиница, цифры, _ и -"; exit 1 ;; esac
case "$TOKEN_TTL" in ""|*[!0-9hms]*) echo "TOKEN_TTL: срок вида 24h или 90m"; exit 1 ;; esac
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }
kc() { limactl shell "$KC" sudo k3s kubectl "$@"; }

ENTRY="$(ip_of "$VM")"
[ -n "$ENTRY" ] || { echo "у $VM нет адреса на lima0"; exit 1; }
qdrant=""; ips=""
for n in $NODES; do
  ip="$(ip_of "$n")"; [ -n "$ip" ] || { echo "у $n нет адреса на lima0"; exit 1; }
  qdrant="$qdrant  --qdrant $n=http://$ip:30633 \\"$'\n'
  ips="${ips:+$ips }$ip"
done
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
# через окружение: awk на macOS не принимает перевод строки в -v
QDRANT_ARGS="$qdrant" ENTRY="$ENTRY" COLLECTION="$COLLECTION" CLUSTER_IPS="$ips" awk '
  /^@QDRANT_ARGS@$/ { printf "%s", ENVIRON["QDRANT_ARGS"]; next }
  { gsub(/@ENTRY@/, ENVIRON["ENTRY"]); gsub(/@COLLECTION@/, ENVIRON["COLLECTION"])
    gsub(/@CLUSTER_IPS@/, ENVIRON["CLUSTER_IPS"]); print }' "$HERE/stand-panel.service" > "$T/stand-panel.service"
if grep -q '@[A-Z_][A-Z_]*@' "$T/stand-panel.service"; then echo "в юните остались подстановки"; exit 1; fi

echo "== учётная запись панели только на чтение ($KC)"
limactl copy "$HERE/rbac.yaml" "$KC:/tmp/panel-rbac.yaml"
limactl shell --workdir /tmp "$KC" sudo bash -s <<'EOS'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
# новая учётная запись получает новый UID: токены прошлых установок сразу перестают действовать
if kubectl get namespace stand-panel >/dev/null 2>&1; then
  kubectl -n stand-panel delete serviceaccount panel-reader --ignore-not-found >/dev/null
fi
kubectl apply -f /tmp/panel-rbac.yaml >/dev/null
rm -f /tmp/panel-rbac.yaml
kubectl -n stand-panel get serviceaccount panel-reader -o jsonpath='stand-panel/panel-reader, uid {.metadata.uid}{"\n"}'
EOS

echo "== $VM: пользователь stand-panel, код панели, токен событий"
limactl shell "$VM" sudo rm -rf /tmp/panel-in
limactl shell "$VM" mkdir -p /tmp/panel-in/static
# только то, что нужно живой панели: генератор и mock на эту машину не идут
for f in aggregator.py sources.py; do limactl copy "$ROOT/panel/$f" "$VM:/tmp/panel-in/$f"; done
limactl copy "$ROOT/panel/static/index.html" "$VM:/tmp/panel-in/static/index.html"
limactl copy "$T/stand-panel.service" "$VM:/tmp/panel-in/stand-panel.service"
limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOS'
set -euo pipefail
I=/tmp/panel-in
python3 -I -c 'import sys; print("Python", sys.version.split()[0]); sys.exit(sys.version_info < (3, 12))'
id stand-panel >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin stand-panel
# код принадлежит root: служба его только читает
rm -rf /opt/stand-panel.new
install -d -m 0755 /opt/stand-panel.new /opt/stand-panel.new/static
install -m 0644 "$I/aggregator.py" "$I/sources.py" /opt/stand-panel.new/
install -m 0644 "$I/static/index.html" /opt/stand-panel.new/static/index.html
rm -rf /opt/stand-panel.old
if [ -d /opt/stand-panel ]; then mv /opt/stand-panel /opt/stand-panel.old; fi
mv /opt/stand-panel.new /opt/stand-panel && rm -rf /opt/stand-panel.old
# секреты панели: папку видят root и группа панели, файлы токенов только пользователь панели
install -d -m 0750 -o root -g stand-panel /etc/stand-panel
if [ ! -s /etc/stand-panel/event-token ]; then
  ( umask 077; openssl rand -hex 32 > /etc/stand-panel/event-token )
fi
chown stand-panel:stand-panel /etc/stand-panel/event-token
chmod 0600 /etc/stand-panel/event-token
install -m 0644 "$I/stand-panel.service" /etc/systemd/system/stand-panel.service
rm -rf "$I"
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/stand-panel.service 2>&1 | grep -v '^$' || true
EOS

echo "== CA кластера и токен Kubernetes на $TOKEN_TTL ($KC → $VM)"
# сертификат не секрет, но проверяется: это PEM, иначе старый файл остаётся
limactl shell "$KC" sudo cat /var/lib/rancher/k3s/server/tls/server-ca.crt \
  | limactl shell "$VM" sudo sh -c 'f=/etc/stand-panel/kube-ca.crt; cat > "$f.new" && openssl x509 -in "$f.new" -noout && chmod 0644 "$f.new" && mv -f "$f.new" "$f"'
# токен идёт трубой из машины в машину: в аргументы команд и на диск Мака не попадает
kc -n stand-panel create token panel-reader --duration "$TOKEN_TTL" \
  | limactl shell "$VM" sudo sh -c 'umask 077; f=/etc/stand-panel/kube-token; cat > "$f.new"; if ! grep -Eqx "[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+" "$f.new"; then rm -f "$f.new"; echo "токен не получен"; exit 1; fi; chown stand-panel:stand-panel "$f.new" && mv -f "$f.new" "$f"'

echo "== служба stand-panel"
limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOS'
set -euo pipefail
systemctl enable -q stand-panel
systemctl restart stand-panel
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
cat > "$D/state.py" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
src = s["sources"]
if sys.argv[2] == "wait":
    sys.exit(0 if all(src.get(k, {}).get("ok") for k in ("kube", "qdrant", "observer")) else 1)
for k in ("kube", "qdrant", "observer", "load"):
    st = src.get(k)
    line = "не подключён" if st is None else ("отвечает" if st["ok"] else f"нет: {st.get('error')}")
    if k == "load" and st is not None and not st["ok"]:
        line += " (так и есть, пока сцена не запустила deploy/scenes/load.sh start)"
    print(f"  {k:9} {line}")
k, q = s.get("kube") or {}, s.get("qdrant") or {}
print(f"  узлы: {', '.join(n['name'] + ' ' + str(n['ready']) for n in k.get('nodes', [])) or 'нет данных'}"
      f" · пульс kubelet: {'да' if k.get('heartbeats') else 'нет'}")
print(f"  пиры Qdrant: {', '.join(str(p.get('machine')) + (' ✓' if p['reachable'] else ' ✗') for p in q.get('peers', [])) or 'нет данных'}")
PY
ok=""
for _ in $(seq 1 30); do
  if curl -sf -m 2 -o "$D/state.json" http://127.0.0.1:8090/api/state \
      && python3 -I "$D/state.py" "$D/state.json" wait; then ok=1; break; fi
  sleep 1
done
if [ -s "$D/state.json" ]; then python3 -I "$D/state.py" "$D/state.json" show; fi
if [ -z "$ok" ]; then
  echo "панель не увидела все источники за 30 с:"
  systemctl --no-pager --lines=0 status stand-panel || true
  journalctl -u stand-panel -n 20 --no-pager
  exit 1
fi
echo "слушает:"; ss -ltn | awk 'NR>1 && $4 ~ /:8090$/ {print "  " $4}'
EOS

echo "== события Falco: адрес с токеном в Secret falco/falco-panel-output ($KC)"
cat > "$T/falco-secret.sh" <<'EOS'
set -euo pipefail
ENTRY=$1; PORT=$2
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
IFS= read -r TOKEN || true
case "$TOKEN" in ""|*[!0-9a-f]*) echo "токен событий не прочитан"; exit 1 ;; esac
if ! kubectl get namespace falco >/dev/null 2>&1; then
  echo "Falco не установлен (deploy/falco/install.sh): тревоги в панель не пойдут"; exit 0
fi
# адрес с токеном только в Secret; printf встроен в bash, поэтому токен не виден в списке процессов
out=$(printf 'http_output:\n  enabled: true\n  url: "http://%s:%s/api/event?token=%s"\n  user_agent: "falcosecurity/falco"\n  insecure: false\n  mtls: false\n  echo: false\n  compress_uploads: false\n  keep_alive: false\n  max_consecutive_timeouts: 5\n' \
    "$ENTRY" "$PORT" "$TOKEN" \
  | kubectl -n falco create secret generic falco-panel-output --from-file=panel-output.yaml=/dev/stdin \
      --dry-run=client -o yaml \
  | kubectl apply -f -)
echo "$out"
vols=$(kubectl -n falco get ds falco -o jsonpath='{.spec.template.spec.volumes[*].name}')
case " $vols " in
  *" panel-output "*) ;;
  *) echo "у Falco нет тома panel-output: повтори deploy/falco/install.sh (новый deploy/falco/values.yaml)"; exit 0 ;;
esac
case "$out" in
  *unchanged*) echo "адрес не менялся, Falco не перезапускается" ;;
  *) kubectl -n falco rollout restart ds/falco >/dev/null
     kubectl -n falco rollout status ds/falco --timeout=300s ;;
esac
EOS
limactl copy "$T/falco-secret.sh" "$KC:/tmp/panel-falco-secret.sh"
limactl shell "$VM" sudo cat /etc/stand-panel/event-token \
  | limactl shell --workdir /tmp "$KC" sudo bash /tmp/panel-falco-secret.sh "$ENTRY" "$PORT"
limactl shell "$KC" rm -f /tmp/panel-falco-secret.sh

echo "панель: на Маке http://127.0.0.1:18090 (проброс Lima ставит deploy/vm/up.sh при создании $VM;"
echo "  для уже созданной: ssh -F ~/.lima/$VM/ssh.config -N -L 127.0.0.1:18090:127.0.0.1:$PORT lima-$VM)"
echo "  события: http://$ENTRY:$PORT/api/event, только с токеном; журнал: limactl shell $VM journalctl -u stand-panel"
