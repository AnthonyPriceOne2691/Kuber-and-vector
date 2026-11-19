#!/bin/bash
# Ставит Qdrant в кластер из комплекта: образ в containerd каждой машины кластера (реестра нет),
# helm из комплекта, ключи генерируются внутри машины и живут только в Secret кластера.
#   deploy/qdrant/install.sh [machine-1]        helm и ключи на этой машине
#   NODES="machine-1 machine-2 machine-3" по умолчанию: куда загрузить образ
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm"
VM="${1:-machine-1}"
NODES="${NODES:-machine-1 machine-2 machine-3}"

for n in $NODES; do
  limactl copy "$KIT/images/qdrant-v1.20.2-arm64.oci.tar" "$n:/tmp/qdrant.oci.tar"
  limactl shell --workdir /tmp "$n" sudo bash -s -- "$n" <<'EOS'
set -euo pipefail
k3s ctr -n k8s.io images import /tmp/qdrant.oci.tar >/dev/null
rm -f /tmp/qdrant.oci.tar
echo "$1: образ $(k3s ctr -n k8s.io images ls -q | grep 'qdrant/qdrant:v1.20.2')"
EOS
done

limactl copy "$KIT/charts/qdrant-1.20.2.tgz" "$VM:/tmp/qdrant-chart.tgz"
limactl copy "$KIT/bin/helm-v4.3.2-linux-arm64.tar.gz" "$VM:/tmp/helm.tar.gz"
limactl copy "$HERE/values.yaml" "$VM:/tmp/qdrant-values.yaml"
limactl copy "$HERE/netpol.yaml" "$VM:/tmp/qdrant-netpol.yaml"
limactl copy "$HERE/local-svc.yaml" "$VM:/tmp/qdrant-local-svc.yaml"

limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOS'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
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
helm upgrade --install qdrant /tmp/qdrant-chart.tgz -n qdrant -f /tmp/qdrant-values.yaml --wait --timeout 10m >/dev/null
# сеть: Qdrant принимает только поиск и своих пиров (deploy/qdrant/netpol.yaml)
kubectl apply -f /tmp/qdrant-netpol.yaml >/dev/null
# свой пир на машине для сервиса поиска (deploy/qdrant/local-svc.yaml)
kubectl apply -f /tmp/qdrant-local-svc.yaml >/dev/null
rm -rf /tmp/qdrant-chart.tgz /tmp/helm.tar.gz /tmp/linux-arm64 /tmp/qdrant-netpol.yaml /tmp/qdrant-local-svc.yaml
kubectl -n qdrant get pods -o wide
# пиры видят друг друга: число пиров в консенсусе с точки зрения пира 0 (через туннель API,
# напрямую с узла правило сети уже не пускает)
K=$(mktemp); PF=""; trap 'rm -f "$K"; kill $PF 2>/dev/null' EXIT
kubectl -n qdrant port-forward pod/qdrant-0 16330:6333 >/dev/null 2>&1 & PF=$!
sleep 3
# ключ идёт заголовком из файла (curl -H @файл), в аргументы процессов он не попадает
( umask 077; printf 'api-key: %s\n' "$(kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "api-key"}}' | base64 -d)" > "$K" )
for i in $(seq 1 60); do
  n=$(curl -s -H @"$K" "http://127.0.0.1:16330/cluster" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["result"]["peers"]))' 2>/dev/null || echo 0)
  [ "$n" = 3 ] && break
  sleep 2
done
echo "пиров в консенсусе: $n"
EOS
