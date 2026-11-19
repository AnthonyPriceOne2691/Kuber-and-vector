#!/bin/bash
# Кластер быстрее признаёт выключенную машину мёртвой. Пока узел не признан мёртвым, адреса его
# подов остаются в сервисах, и каждое третье новое соединение к Qdrant уходит в пустоту
# (замер 18.11: деградация около 45 с при стандартных 50 с).
#   - node-monitor-grace-period 25 с вместо 50 с (kube-controller-manager);
#   - kubelet обновляет пульс (Lease) раз в 10 с, поэтому 25 с переживают один пропуск пульса;
#   - документация Kubernetes требует, чтобы этот срок был больше суммы таймаутов HTTP/2 у kubelet
#     (по умолчанию 30 + 15 с), поэтому их сокращаем до 10 + 5 с через окружение службы k3s.
# Цена: на перегруженной машине выше риск ложного «мёртв». Проверяется сценой «выключаем машину».
# Применяется по одной машине (кворум etcd не теряется); поды при перезапуске k3s не падают
# (у службы k3s KillMode=process). Повторный запуск без изменений ничего не перезапускает.
#   deploy/k3s/failover-tuning.sh                 GRACE=25s по умолчанию
#   GRACE=50s deploy/k3s/failover-tuning.sh       вернуть стандартный срок
set -euo pipefail
NODES="${NODES:-machine-1 machine-2 machine-3}"
GRACE="${GRACE:-25s}"
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }

# пишет настройки на машине; печатает yes, если что-то поменялось
apply_conf() {
  limactl shell "$1" sudo bash -s -- "$GRACE" <<'EOS'
set -euo pipefail
GRACE=$1
CONF=/etc/rancher/k3s/config.yaml.d/50-failover.yaml
ENVF=/etc/systemd/system/k3s.service.env
# «+» дописывает к аргументам из 40-cis.yaml, а не заменяет их (файлы читаются по алфавиту)
want_conf=$(printf '%s\n' "# deploy/k3s/failover-tuning.sh" "kube-controller-manager-arg+:" "  - \"node-monitor-grace-period=$GRACE\"")
want_env=$(printf '%s\n' "HTTP2_READ_IDLE_TIMEOUT_SECONDS=10" "HTTP2_PING_TIMEOUT_SECONDS=5")
have_conf=$(cat "$CONF" 2>/dev/null || true)
have_env=$(grep -E '^HTTP2_(READ_IDLE|PING)_TIMEOUT_SECONDS=' "$ENVF" 2>/dev/null || true)
if [ "$have_conf" = "$want_conf" ] && [ "$have_env" = "$want_env" ]; then echo no; exit 0; fi
mkdir -p "$(dirname "$CONF")"
printf '%s\n' "$want_conf" > "$CONF"
touch "$ENVF"
sed -i -E '/^HTTP2_(READ_IDLE|PING)_TIMEOUT_SECONDS=/d' "$ENVF"
printf '%s\n' "$want_env" >> "$ENVF"
chmod 600 "$ENVF"
echo yes
EOS
}

for m in $NODES; do
  changed=$(apply_conf "$m")
  if [ "$changed" != yes ]; then echo "$m: уже настроено (grace $GRACE)"; continue; fi
  echo "$m: перезапуск k3s с grace $GRACE"
  limactl shell "$m" sudo systemctl restart k3s
  # дальше только когда API этой машины снова готов (значит, её etcd вернулся в кворум) и все три
  # узла Ready: иначе следующий перезапуск рискует кворумом etcd
  sleep 5
  ok=""
  for _ in $(seq 1 90); do
    if limactl shell "$m" sudo k3s kubectl get --raw=/readyz >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  [ -n "$ok" ] || { echo "API на $m не поднялся"; exit 1; }
  n=0
  for _ in $(seq 1 90); do
    n=$(kc get nodes --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l | tr -d ' ')
    [ "$n" = 3 ] && break; sleep 2
  done
  [ "$n" = 3 ] || { echo "после перезапуска $m не все узлы Ready"; exit 1; }
done
kc get nodes
