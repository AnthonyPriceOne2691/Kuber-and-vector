#!/bin/bash
# Выкатывает сервис поиска. Сервису достаётся только ключ Qdrant на чтение: его копия
# переносится из Secret qdrant внутри машины, на диск Мака и в git ключ не попадает.
#   deploy/search/deploy.sh [machine-1]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VM="${1:-machine-1}"

limactl copy "$HERE/k8s.yaml" "$VM:/tmp/search.yaml"
limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOF'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl apply -f /tmp/search.yaml >/dev/null
if ! kubectl -n search get secret qdrant-read-only-key >/dev/null 2>&1; then
  d=$(mktemp -d)
  ( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "read-only-api-key"}}' | base64 -d > "$d/read-only-api-key" )
  kubectl -n search create secret generic qdrant-read-only-key --from-file="$d/read-only-api-key" >/dev/null
  rm -rf "$d"
fi
kubectl -n search rollout restart deploy/search >/dev/null
kubectl -n search rollout status deploy/search --timeout=180s
kubectl -n search get pods -o wide
rm -f /tmp/search.yaml
EOF
