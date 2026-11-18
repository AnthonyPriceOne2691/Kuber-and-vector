#!/bin/bash
# Собирает образ сервиса поиска внутри машины стенда, без интернета: buildah из пакетов
# комплекта, база python:3.13-slim и колёса Python из комплекта. Готовый образ уходит
# в containerd k3s, в реестр ничего не пушится.
#   deploy/search/build.sh [machine-1] [тег]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm"
VM="${1:-machine-1}"
TAG="${2:-stage1}"
W=/home/ops.guest/build-search

limactl shell "$VM" sudo rm -rf "$W"
limactl shell "$VM" mkdir -p "$W/app"
limactl copy "$HERE/Containerfile" "$VM:$W/Containerfile"
for f in __init__.py api.py bm25.py; do limactl copy "$ROOT/app/$f" "$VM:$W/app/$f"; done
limactl copy -r "$KIT/wheels" "$VM:$W/wheels"
limactl copy "$KIT/images/python-3.13-slim-arm64.oci.tar" "$VM:$W/python.oci.tar"

# buildah ставится из комплекта один раз. Интернета у машины кластера нет (песочница), так что
# скачать недостающее apt не сможет. Флаг --no-download не годится: он запрещает и копирование
# локальных .deb. Пакеты кладём в /tmp, чтобы их читал служебный пользователь apt (_apt).
if ! limactl shell "$VM" bash -c 'command -v buildah' >/dev/null 2>&1; then
  limactl copy -r "$KIT/debs" "$VM:$W/debs"
  limactl shell --workdir /tmp "$VM" sudo bash -c "rm -rf /tmp/kit-debs && cp -r '$W/debs' /tmp/kit-debs \
    && rm -f /tmp/kit-debs/haproxy_*.deb && chmod -R a+rX /tmp/kit-debs \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /tmp/kit-debs/*.deb >/dev/null \
    && rm -rf /tmp/kit-debs && buildah --version"
fi

limactl shell --workdir "$W" "$VM" sudo bash -s -- "$W" "$TAG" <<'EOF'
set -euo pipefail
W=$1; TAG=$2
buildah pull -q oci-archive:"$W/python.oci.tar" >/dev/null
buildah bud -q --network none -t "localhost/search:$TAG" -f "$W/Containerfile" "$W" >/dev/null
buildah push -q "localhost/search:$TAG" oci-archive:"$W/search.oci.tar":"localhost/search:$TAG"
k3s ctr -n k8s.io images import "$W/search.oci.tar" >/dev/null
k3s ctr -n k8s.io images ls | awk -v t="localhost/search:$TAG" '$1==t{print "образ:", $1, "digest:", $3, "размер:", $4 $5}'
EOF
limactl shell "$VM" sudo rm -rf "$W"
