#!/bin/bash
# Выкатывает сервис поиска. Сервису достаётся только ключ Qdrant на чтение: его копия
# переносится из Secret qdrant внутри машины, на диск Мака и в git ключ не попадает.
# Адреса Qdrant берутся из кластера (ClusterIP сервисов qdrant и qdrant-local) и подставляются в манифест.
# Правила сети (netpol.yaml) пускают внутрь только вход HAProxy: его адрес берётся с machine-4.
#   deploy/search/deploy.sh [machine-1] [machine-4]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VM="${1:-machine-1}"
ENTRY_VM="${2:-machine-4}"
ENTRY="$(limactl shell "$ENTRY_VM" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1")"
[ -n "$ENTRY" ] || { echo "у $ENTRY_VM нет адреса на lima0"; exit 1; }

limactl copy "$HERE/k8s.yaml" "$VM:/tmp/search.yaml"
limactl copy "$HERE/netpol.yaml" "$VM:/tmp/search-netpol.yaml"
limactl shell --workdir /tmp "$VM" sudo bash -s -- "$ENTRY" <<'EOS'
set -euo pipefail
ENTRY=$1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
IP=$(kubectl -n qdrant get svc qdrant -o jsonpath='{.spec.clusterIP}')
[ -n "$IP" ] || { echo "нет сервиса qdrant"; exit 1; }
LOCAL_IP=$(kubectl -n qdrant get svc qdrant-local -o jsonpath='{.spec.clusterIP}')
[ -n "$LOCAL_IP" ] || { echo "нет сервиса qdrant-local: повтори deploy/qdrant/install.sh"; exit 1; }
sed -i "s/QDRANT_LOCAL_CLUSTER_IP/$LOCAL_IP/; s/QDRANT_CLUSTER_IP/$IP/" /tmp/search.yaml
existed=$(kubectl -n search get deploy search -o name 2>/dev/null || true)
kubectl apply -f /tmp/search.yaml >/dev/null
sed -i "s/ENTRY_IP/$ENTRY/" /tmp/search-netpol.yaml
kubectl apply -f /tmp/search-netpol.yaml >/dev/null
if ! kubectl -n search get secret qdrant-read-only-key >/dev/null 2>&1; then
  d=$(mktemp -d)
  ( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "read-only-api-key"}}' | base64 -d > "$d/read-only-api-key" )
  kubectl -n search create secret generic qdrant-read-only-key --from-file="$d/read-only-api-key" >/dev/null
  rm -rf "$d"
fi
# тег образа тот же (stage2), поэтому новый образ подхватываем перезапуском; свежему Deployment он не нужен
[ -n "$existed" ] && kubectl -n search rollout restart deploy/search >/dev/null
kubectl -n search rollout status deploy/search --timeout=180s
kubectl -n search get pods -o wide
rm -f /tmp/search.yaml /tmp/search-netpol.yaml
EOS
