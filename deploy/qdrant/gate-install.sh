#!/bin/bash
# Шлюз к Qdrant (deploy/qdrant/gate.py и gate.yaml): по поду на машину, наружу только machine-4.
# Нужны сервис qdrant-local (deploy/qdrant/install.sh) и образ поиска localhost/search:stage2
# на машинах (deploy/search/build.sh): шлюз работает на его Python.
#   deploy/qdrant/gate-install.sh [machine-1] [machine-4]
# Повторный запуск безопасен: код обновляется, поды перезапускаются по одному.
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VM="${1:-machine-1}"
ENTRY_VM="${2:-machine-4}"
ENTRY="$(limactl shell "$ENTRY_VM" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1")"
[ -n "$ENTRY" ] || { echo "у $ENTRY_VM нет адреса на lima0"; exit 1; }

limactl copy "$HERE/gate.py" "$VM:/tmp/qdrant-gate.py"
limactl copy "$HERE/gate.yaml" "$VM:/tmp/qdrant-gate.yaml"
limactl shell --workdir /tmp "$VM" sudo bash -s -- "$ENTRY" <<'EOS'
set -euo pipefail
ENTRY=$1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
LOCAL_IP=$(kubectl -n qdrant get svc qdrant-local -o jsonpath='{.spec.clusterIP}')
[ -n "$LOCAL_IP" ] || { echo "нет сервиса qdrant-local: повтори deploy/qdrant/install.sh"; exit 1; }
sed -i "s/ENTRY_IP/$ENTRY/; s/QDRANT_LOCAL_CLUSTER_IP/$LOCAL_IP/" /tmp/qdrant-gate.yaml
kubectl -n qdrant create configmap qdrant-gate-code --from-file=gate.py=/tmp/qdrant-gate.py \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f /tmp/qdrant-gate.yaml >/dev/null
kubectl -n qdrant rollout restart ds/qdrant-gate >/dev/null  # новый код из ConfigMap
kubectl -n qdrant rollout status ds/qdrant-gate --timeout=180s
kubectl -n qdrant get pods -l app=qdrant-gate -o wide
rm -f /tmp/qdrant-gate.py /tmp/qdrant-gate.yaml
EOS
echo "шлюз Qdrant: http://<адрес машины>:30633 (только с $ENTRY_VM)"
