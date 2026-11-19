#!/bin/bash
# Стенд с нуля одной командой: четыре машины из одной схемы, k3s HA на machine-1…3,
# Qdrant из трёх пиров, данные двумя схемами, сервис поиска на трёх машинах, вход на machine-4,
# сторож Falco на машинах кластера, мониторинг и панель на machine-4.
#   deploy/stand-up.sh            подробный вывод по-русски, как раньше
#   QUIET=1 deploy/stand-up.sh    для кадра: в терминал только английские заголовки шагов с секундами
#                                 от старта и в конце «stand ready in N s» с короткой сводкой; подробный
#                                 вывод шагов в runs/stand-up-<время>.log; при ошибке шага последние
#                                 строки журнала и ненулевой код выхода
# Повторный запуск безопасен: готовые шаги пропускаются или повторяются без вреда.
# Нужно заранее: Lima и socket_vmnet (deploy/vm/README.md), комплект kit/vm, Ollama с bge-m3
# на Маке, подготовленные куски (python3 -I eval/prepare.py), комплекты Falco и мониторинга
# (kit-build.sh falco, kit-build.sh monitoring).
# Пишется под системный bash 3.2 на macOS.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY=/opt/homebrew/bin/python3
CLUSTER="machine-1 machine-2 machine-3"
QUIET="${QUIET:-0}"
t0=$(date +%s)
k3s_ready() { limactl shell "$1" bash -c 'systemctl is-active -q k3s 2>/dev/null'; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }

# --- тихий режим: tests/test_stand_up_quiet.py вынимает этот кусок и гоняет его без стенда ---
# С QUIET=1 весь вывод скрипта и шагов уходит в журнал, терминал остаётся на дескрипторе 3, туда
# пишутся только строки для кадра. Съёмка витрины (capture.py) гоняет команду в псевдотерминале
# и пишет вывод кусками с отметкой времени, поэтому в терминал идут только целые строки, без цвета,
# без \r и других управляющих символов; хвост журнала при ошибке от них очищается.
LOG=""; CUR_STEP=""
since() { echo $(( $(date +%s) - t0 )); }
say() { if [ "$QUIET" = 1 ]; then printf '%s\n' "$*" >&3; fi; }
quiet_start() {
  [ "$QUIET" = 1 ] || return 0
  mkdir -p "$ROOT/runs"
  LOG="$ROOT/runs/stand-up-$(date +%Y%m%d-%H%M%S).log"
  exec 3>&1 >>"$LOG" 2>&1 </dev/null
  trap 'quiet_end $?' EXIT
}
# шаг: заголовок по-русски в подробный вывод (терминал или журнал), по-английски в кадр
step() {
  CUR_STEP="$2"
  echo; echo "== $(since) с: $1"
  if [ "$QUIET" = 1 ]; then printf '  %-7s %s\n' "+$(since) s" "$2" >&3; fi
}
quiet_end() {
  local rc="$1"
  trap - EXIT
  set +e
  if [ "$rc" -ne 0 ]; then
    printf '  %-7s %s\n' "+$(since) s" "FAILED with exit code $rc at: ${CUR_STEP:-start}" >&3
    printf '%s\n' "last lines of ${LOG#"$ROOT"/}" >&3
    tail -n "${QUIET_TAIL:-20}" "$LOG" | sed $'s/\033\\[[0-9;?]*[A-Za-z]//g' \
      | LC_ALL=C tr -d '\000-\010\013-\037\177' | sed 's/^/  | /' >&3
  fi
  exit "$rc"
}
# --- конец тихого режима ---

# Сводка в кадр после проверки: узлы, пиры Qdrant, поиск, вход, итог check.sh (deploy/stand_summary.py).
# Чего-то нет, в сводке unknown: стенд проверку уже прошёл, поэтому сводка подъём не роняет.
quiet_summary() {
  local d entry m1
  d="$(mktemp -d)"
  kc get nodes,pods -A -o json > "$d/kube.json" || true
  entry="$(ip_of machine-4)" || entry=""
  m1="$(ip_of machine-1)" || m1=""
  # состояние кластера Qdrant глазами его пиров: шлюз machine-1 (только чтение, пускает только machine-4)
  if [ -n "$m1" ]; then limactl shell machine-4 curl -sf -m 3 "http://$m1:30633/cluster" > "$d/gate.json" || true; fi
  "$PY" -I -B "$HERE/stand_summary.py" --kube "$d/kube.json" --gate "$d/gate.json" --entry "$entry" \
    --check-log "$LOG" >&3 || say "  summary unavailable, see ${LOG#"$ROOT"/}"
  rm -rf "$d"
}

quiet_start

step "машины" "four VMs from one template"
"$HERE/vm/up.sh"

step "фильтр Ollama на Маке" "Ollama filter on the Mac"
"$HERE/host/filter.sh" start

step "файрвол на входе машин: соседям и входу свои порты, остальное в отбой" "inbound firewall on all four VMs"
"$HERE/vm/ingress.sh"

step "k3s: machine-1 поднимает etcd, остальные присоединяются по одной" "k3s HA, etcd on machine-1, then machine-2 and machine-3 join"
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

step "k3s: быстрее признавать выключенную машину мёртвой" "k3s marks a dead machine sooner, grace period 25 s"
"$HERE/k3s/failover-tuning.sh"

step "DNS кластера без выхода наружу" "cluster DNS without forwarding outside"
"$HERE/k3s/coredns-local.sh" machine-1

step "Qdrant" "Qdrant, three peers, one per machine"
"$HERE/qdrant/install.sh" machine-1

step "данные" "data, contract chunks into Qdrant, RF2 and RF3 collections"
"$HERE/qdrant/load.sh" machine-1

step "сервис поиска" "search service, image built offline, three replicas"
"$HERE/search/build.sh" machine-1 stage2
"$HERE/search/deploy.sh" machine-1

step "вход на machine-4" "entry on machine-4, HAProxy with TLS and an API key"
"$HERE/observer/install.sh" machine-4

step "сторож Falco" "Falco runtime alerts on every cluster machine"
"$HERE/falco/install.sh" machine-1

step "шлюз Qdrant для мониторинга и панели" "read-only Qdrant gate for monitoring and the panel"
"$HERE/qdrant/gate-install.sh" machine-1 machine-4

step "мониторинг на machine-4" "monitoring on machine-4, Prometheus and Grafana"
"$HERE/monitoring/install.sh" machine-4

step "панель на machine-4: Kubernetes только на чтение, Qdrant через шлюз, тревоги Falco" "panel on machine-4, read-only access to Kubernetes"
"$HERE/panel/install.sh" machine-4

step "учётки default без токена API во всех пространствах, кроме kube-system (CIS 5.1.5)" "default service accounts get no API token"
"$HERE/k3s/default-sa.sh" machine-1

step "образы только из комплекта (политика допуска, последней: все поды уже стоят)" "admission policy, only images from the kit"
"$HERE/k3s/image-policy.sh" machine-1

step "проверка" "checks"
"$HERE/check.sh"
ready=$(since)
echo; echo "стенд готов за $ready с"
if [ "$QUIET" = 1 ]; then
  say ""
  say "stand ready in $ready s"
  quiet_summary
fi
