#!/bin/bash
# Ставит Falco из комплекта kit/vm-falco: образ в containerd каждой машины кластера,
# чарт с настройками стенда (deploy/falco/values.yaml). Пространство имён falco помечено
# privileged явно: сторожу нужен доступ к ядру (eBPF) и к сокету containerd, и это единственное
# такое исключение на стенде.
#   deploy/falco/install.sh [machine-1]
#   NODES="machine-1 machine-2 machine-3" по умолчанию
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm-falco"
VM="${1:-machine-1}"
NODES="${NODES:-machine-1 machine-2 machine-3}"
[ -f "$KIT/images/falco-0.45.2-arm64.oci.tar" ] || { echo "нет комплекта: deploy/vm/kit-build.sh falco"; exit 1; }

for n in $NODES; do
  limactl copy "$KIT/images/falco-0.45.2-arm64.oci.tar" "$n:/tmp/falco.oci.tar"
  limactl shell --workdir /tmp "$n" sudo bash -s -- "$n" <<'EOS'
set -euo pipefail
k3s ctr -n k8s.io images import /tmp/falco.oci.tar >/dev/null
rm -f /tmp/falco.oci.tar
echo "$1: образ $(k3s ctr -n k8s.io images ls -q | grep 'falcosecurity/falco:0.45.2')"
EOS
done

limactl copy "$KIT/charts/falco-9.2.2.tgz" "$VM:/tmp/falco-chart.tgz"
limactl copy "$HERE/values.yaml" "$VM:/tmp/falco-values.yaml"
limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOS'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl create namespace falco --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace falco --overwrite pod-security.kubernetes.io/enforce=privileged >/dev/null
helm upgrade --install falco /tmp/falco-chart.tgz -n falco -f /tmp/falco-values.yaml --wait --timeout 10m >/dev/null
rm -f /tmp/falco-chart.tgz /tmp/falco-values.yaml
kubectl -n falco get pods -o wide
EOS
