#!/bin/bash
# Ставит Qdrant в кластер из комплекта: образ в containerd k3s, helm из комплекта,
# ключи генерируются внутри машины и живут только в Secret кластера.
#   ./install.sh [machine-1]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm"
VM="${1:-machine-1}"

limactl copy "$KIT/images/qdrant-v1.20.2-arm64.oci.tar" "$VM:/tmp/qdrant.oci.tar"
limactl copy "$KIT/charts/qdrant-1.20.2.tgz" "$VM:/tmp/qdrant-chart.tgz"
limactl copy "$KIT/bin/helm-v4.3.2-linux-arm64.tar.gz" "$VM:/tmp/helm.tar.gz"
limactl copy "$HERE/values.yaml" "$VM:/tmp/qdrant-values.yaml"

limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOF'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
k3s ctr -n k8s.io images import /tmp/qdrant.oci.tar >/dev/null
echo "образ: $(k3s ctr -n k8s.io images ls -q | grep 'qdrant/qdrant:v1.20.2')"
if ! command -v helm >/dev/null; then
  tar -xzf /tmp/helm.tar.gz -C /tmp linux-arm64/helm
  install -m 0755 /tmp/linux-arm64/helm /usr/local/bin/helm
fi
kubectl create namespace qdrant --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if ! kubectl -n qdrant get secret qdrant-keys >/dev/null 2>&1; then
  d=$(mktemp -d); chmod 700 "$d"
  openssl rand -hex 24 > "$d/api-key"
  openssl rand -hex 24 > "$d/read-only-api-key"
  kubectl -n qdrant create secret generic qdrant-keys --from-file="$d/api-key" --from-file="$d/read-only-api-key" >/dev/null
  rm -rf "$d"
  echo "ключи созданы в Secret qdrant/qdrant-keys"
fi
helm upgrade --install qdrant /tmp/qdrant-chart.tgz -n qdrant -f /tmp/qdrant-values.yaml --wait --timeout 5m >/dev/null
rm -rf /tmp/qdrant.oci.tar /tmp/qdrant-chart.tgz /tmp/helm.tar.gz /tmp/linux-arm64
kubectl -n qdrant get pods,svc -o wide
EOF
