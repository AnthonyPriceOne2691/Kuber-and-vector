#!/bin/bash
# Собирает образ сервиса поиска внутри машины стенда, без интернета: buildah из пакетов
# комплекта, база python:3.13-slim и колёса Python из комплекта. Готовый образ уходит
# в containerd k3s, в реестр ничего не пушится. Реестра в кластере нет, поэтому тот же архив
# образа загружается в containerd каждой машины кластера (NODES).
#   deploy/search/build.sh [machine-1] [тег]
#   NODES="machine-1 machine-2 machine-3" по умолчанию
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm"
VM="${1:-machine-1}"
TAG="${2:-stage2}"
NODES="${NODES:-machine-1 machine-2 machine-3}"
OUT="$ROOT/runs/images"
W=/home/ops.guest/build-search
# Модули сервиса в образе: api.py и всё, что он берёт из app. Список сверяет с импортами
# tests/test_search_build.py: 19.11 answer.py и metrics.py в образ не попали, поды падали на импорте.
APP_FILES="__init__.py api.py answer.py binding.py bm25.py embed_models.py metrics.py"

limactl shell "$VM" sudo rm -rf "$W"
limactl shell "$VM" mkdir -p "$W/app"
limactl copy "$HERE/Containerfile" "$VM:$W/Containerfile"
for f in $APP_FILES; do limactl copy "$ROOT/app/$f" "$VM:$W/app/$f"; done
limactl copy -r "$KIT/wheels" "$VM:$W/wheels"
limactl copy "$KIT/images/python-3.13-slim-arm64.oci.tar" "$VM:$W/python.oci.tar"

# buildah ставится из комплекта один раз. Интернета у машины кластера нет (песочница), так что
# скачать недостающее apt не сможет. Флаг --no-download не годится: он запрещает и копирование
# локальных .deb. Пакеты кладём в /tmp, чтобы их читал служебный пользователь apt (_apt).
# Репозитории из интернета отключаем на время установки: иначе apt пытается разрешить их имена,
# а DNS машинам стенда закрыт, и установка висит на таймаутах.
if ! limactl shell "$VM" bash -c 'command -v buildah' >/dev/null 2>&1; then
  limactl copy -r "$KIT/debs" "$VM:$W/debs"
  limactl shell --workdir /tmp "$VM" sudo bash -c "rm -rf /tmp/kit-debs && cp -r '$W/debs' /tmp/kit-debs \
    && rm -f /tmp/kit-debs/haproxy_*.deb && chmod -R a+rX /tmp/kit-debs \
    && mkdir -p /tmp/apt-no-sources \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts=/tmp/apt-no-sources /tmp/kit-debs/*.deb >/dev/null \
    && rm -rf /tmp/kit-debs && buildah --version"
fi

limactl shell --workdir "$W" "$VM" sudo bash -s -- "$W" "$TAG" <<'EOF'
set -euo pipefail
W=$1; TAG=$2
buildah pull -q oci-archive:"$W/python.oci.tar" >/dev/null
buildah bud -q --network none -t "localhost/search:$TAG" -f "$W/Containerfile" "$W" >/dev/null
buildah push -q "localhost/search:$TAG" oci-archive:"$W/search.oci.tar":"localhost/search:$TAG"
chmod 0644 "$W/search.oci.tar"
EOF
mkdir -p "$OUT"
limactl copy "$VM:$W/search.oci.tar" "$OUT/search-$TAG.oci.tar"
limactl shell "$VM" sudo rm -rf "$W"
shasum -a 256 "$OUT/search-$TAG.oci.tar" | awk '{print "архив образа sha256:", $1}'
for n in $NODES; do
  limactl copy "$OUT/search-$TAG.oci.tar" "$n:/tmp/search.oci.tar"
  limactl shell --workdir /tmp "$n" sudo bash -s -- "$TAG" "$n" <<'EOS'
set -euo pipefail
k3s ctr -n k8s.io images import /tmp/search.oci.tar >/dev/null
rm -f /tmp/search.oci.tar
k3s ctr -n k8s.io images ls | awk -v t="localhost/search:$1" -v n="$2" '$1==t{print n ": образ", $1, "digest", $3}'
EOS
done
