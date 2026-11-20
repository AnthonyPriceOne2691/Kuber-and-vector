#!/bin/bash
# Учётка default без токена API во всех пространствах, кроме kube-system (CIS 5.1.5, kube-bench
# k3s-cis-1.9): automountServiceAccountToken: false. kube-system не трогаем, как и самооценка k3s:
# там системные поды k3s со своими учётками. Нашим подам API Kubernetes не нужен: поиск и шлюз Qdrant
# выключают токен в описании пода, Qdrant и Falco ходят под default (deploy/qdrant/values.yaml,
# deploy/falco/values.yaml), и их install.sh выключают токен у default ещё до установки чарта, потому
# что токен монтируется при создании пода. Здесь, шагом deploy/stand-up.sh после всех установок,
# догоняются остальные: default, kube-public, kube-node-lease, search, stand-panel и любые новые.
# Если поду понадобится API, ему заводят свою учётку с нужными правами, а не включают токен у default.
#   deploy/k3s/default-sa.sh [machine-1]
# Повторный запуск безопасен: где токен уже выключен, ничего не меняется. Пишется под системный
# bash 3.2 на macOS. Прогнан на стенде 20.11 при подъёме с нуля (QUIET=1 deploy/stand-up.sh, проверки
# 66 из 66); tests/test_cis.py гоняет удалённую часть с поддельным kubectl.
set -euo pipefail
VM="${1:-machine-1}"

limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOS'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
changed=""; kept=""
for ns in $(kubectl get namespaces -o jsonpath='{range .items[?(@.status.phase=="Active")]}{.metadata.name}{" "}{end}'); do
  [ "$ns" = kube-system ] && continue
  if [ "$(kubectl -n "$ns" get serviceaccount default -o jsonpath='{.automountServiceAccountToken}' 2>/dev/null || true)" = false ]; then
    kept="$kept $ns"; continue
  fi
  # server-side apply: учётку, которую контроллер ещё не успел создать, создаёт сам, а у готовой
  # меняет одно поле, без предупреждения kubectl apply о чужом объекте
  kubectl -n "$ns" apply --server-side --field-manager=stand-default-sa -f - >/dev/null <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata: {name: default}
automountServiceAccountToken: false
EOF
  changed="$changed $ns"
done
echo "учётка default без токена API; выключен сейчас:${changed:- нигде}; уже был выключен:${kept:- нигде}"
EOS
