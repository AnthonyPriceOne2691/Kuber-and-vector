#!/bin/bash
# Стенд с нуля одной командой: четыре машины из одной схемы, k3s HA на machine-1…3,
# Qdrant из трёх пиров, данные двумя схемами, сервис поиска на трёх машинах, вход на machine-4,
# сторож Falco на машинах кластера, мониторинг и панель на machine-4.
#   deploy/stand-up.sh
# Повторный запуск безопасен: готовые шаги пропускаются или повторяются без вреда.
# Нужно заранее: Lima и socket_vmnet (deploy/vm/README.md), комплект kit/vm, Ollama с bge-m3
# на Маке, подготовленные куски (python3 -I eval/prepare.py), комплекты Falco и мониторинга
# (kit-build.sh falco, kit-build.sh monitoring).
# Пишется под системный bash 3.2 на macOS.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CLUSTER="machine-1 machine-2 machine-3"
t0=$(date +%s)
step() { echo; echo "== $(( $(date +%s) - t0 )) с: $*"; }
k3s_ready() { limactl shell "$1" bash -c 'systemctl is-active -q k3s 2>/dev/null'; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }

step "машины"
"$HERE/vm/up.sh"

step "фильтр Ollama на Маке"
"$HERE/host/filter.sh" start

step "файрвол на входе машин: соседям и входу свои порты, остальное в отбой"
"$HERE/vm/ingress.sh"

step "k3s: machine-1 поднимает etcd, остальные присоединяются по одной"
k3s_ready machine-1 || "$HERE/k3s/install-server.sh" machine-1 init
for m in machine-2 machine-3; do k3s_ready "$m" || "$HERE/k3s/install-server.sh" "$m" join machine-1; done
for _ in $(seq 1 90); do
  n=$(kc get nodes --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l | tr -d ' ')
  [ "$n" = 3 ] && break; sleep 2
done
kc get nodes -o wide
# DNS кластера в трёх копиях, по одной на машину: иначе выключение machine-1 на минуту оставит
# пиры Qdrant без имён друг друга. В манифесте k3s число копий не задано, поэтому не откатится.
kc -n kube-system scale deploy/coredns --replicas=3 >/dev/null
kc -n kube-system rollout status deploy/coredns --timeout=120s

step "k3s: быстрее признавать выключенную машину мёртвой"
"$HERE/k3s/failover-tuning.sh"

step "DNS кластера без выхода наружу"
"$HERE/k3s/coredns-local.sh" machine-1

step "Qdrant"
"$HERE/qdrant/install.sh" machine-1

step "данные"
"$HERE/qdrant/load.sh" machine-1

step "сервис поиска"
"$HERE/search/build.sh" machine-1 stage2
"$HERE/search/deploy.sh" machine-1

step "вход на machine-4"
"$HERE/observer/install.sh" machine-4

step "сторож Falco"
"$HERE/falco/install.sh" machine-1

step "шлюз Qdrant для мониторинга и панели"
"$HERE/qdrant/gate-install.sh" machine-1 machine-4

step "мониторинг на machine-4"
"$HERE/monitoring/install.sh" machine-4

step "панель на machine-4: Kubernetes только на чтение, Qdrant через шлюз, тревоги Falco"
"$HERE/panel/install.sh" machine-4

step "образы только из комплекта (политика допуска, последней: все поды уже стоят)"
"$HERE/k3s/image-policy.sh" machine-1

step "проверка"
"$HERE/check.sh"
echo; echo "стенд готов за $(( $(date +%s) - t0 )) с"
