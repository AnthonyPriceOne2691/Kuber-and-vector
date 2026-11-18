#!/bin/bash
# Сборка комплекта стенда. Запускается ТОЛЬКО внутри одноразовой сборочной ВМ
# (роль builder: интернет есть, Мак закрыт файрволом). Список загрузок утвердил
# владелец 18.11.2026 (план, «Загрузки v2»). У каждого файла есть строка источника
# в SOURCES.tsv и sha256 в SHA256SUMS; где источник публикует сумму, она сверяется.
#
#   bash kit-fetch.sh /home/ops.guest/kit
set -euo pipefail

K3S_VERSION="v1.37.2+k3s1"
HELM_VERSION="v4.3.2"
QDRANT_VERSION="v1.20.2"
QDRANT_CHART_VERSION="1.20.2"
PYTHON_IMAGE_TAG="3.13-slim"
PY_PACKAGES=(fastapi uvicorn httpx)
DEBS=(haproxy buildah)
LEGALBENCH_URL='https://www.dropbox.com/scl/fo/r7xfa5i3hdsbxex1w6amw/AID389Olvtm-ZLTKAPrw6k4?rlkey=5n8zrbk4c08lbit3iiexofmwg&st=0hu354cq&dl=1'

OUT="${1:?укажи путь для комплекта}"
if [ -e "$OUT" ]; then echo "уже существует: $OUT"; exit 1; fi
mkdir -p "$OUT"/{k3s,images,charts,bin,wheels,debs/partial,data}
SRC="$OUT/SOURCES.tsv"
printf 'файл\tисточник\tверсия, digest, проверка\n' > "$SRC"
src() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$SRC"; }
fetch() { curl -4 -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"; }
export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -o Acquire::ForceIPv4=true -o Dpkg::Use-Pty=0 -qq)

echo "[1/8] пакеты Ubuntu для стенда: ${DEBS[*]} (до любой установки, чтобы зависимости совпали с чистой машиной)"
sudo "${APT[@]}" update
sudo "${APT[@]}" install --download-only -y -o Dir::Cache::archives="$OUT/debs" "${DEBS[@]}"
sudo rm -rf "$OUT/debs/partial" "$OUT/debs/lock"
sudo chown -R "$(id -u):$(id -g)" "$OUT/debs"
for f in "$OUT"/debs/*.deb; do
  src "debs/$(basename "$f")" "ports.ubuntu.com (apt, noble; подписи репозитория проверил apt)" "$(dpkg-deb -f "$f" Version)"
done

echo "[2/8] инструменты самой сборочной ВМ (в комплект не идут): skopeo, pip"
sudo "${APT[@]}" install -y skopeo python3-pip >/dev/null

echo "[3/8] k3s ${K3S_VERSION} для установки без сети"
enc="${K3S_VERSION//+/%2B}"
base="https://github.com/k3s-io/k3s/releases/download/${enc}"
fetch "$base/k3s-arm64" "$OUT/k3s/k3s-arm64"
fetch "$base/k3s-airgap-images-arm64.tar.zst" "$OUT/k3s/k3s-airgap-images-arm64.tar.zst"
fetch "$base/sha256sum-arm64.txt" "$OUT/k3s/sha256sum-arm64.txt"
( cd "$OUT/k3s" && grep -E ' (k3s-arm64|k3s-airgap-images-arm64\.tar\.zst)$' sha256sum-arm64.txt | sha256sum -c - )
src "k3s/k3s-arm64" "$base/k3s-arm64" "$K3S_VERSION; sha256 сверен с sha256sum-arm64.txt; внутри и kubectl"
src "k3s/k3s-airgap-images-arm64.tar.zst" "$base/k3s-airgap-images-arm64.tar.zst" "$K3S_VERSION; sha256 сверен"
if fetch "https://raw.githubusercontent.com/k3s-io/k3s/${enc}/install.sh" "$OUT/k3s/install.sh"; then
  src "k3s/install.sh" "raw.githubusercontent.com/k3s-io/k3s/${enc}/install.sh" "из тега $K3S_VERSION"
else
  fetch "https://get.k3s.io" "$OUT/k3s/install.sh"
  src "k3s/install.sh" "https://get.k3s.io" "не закреплён тегом: по тегу не отдался"
fi

echo "[4/8] helm ${HELM_VERSION}"
hfile="helm-${HELM_VERSION}-linux-arm64.tar.gz"
fetch "https://get.helm.sh/$hfile" "$OUT/bin/$hfile"
fetch "https://get.helm.sh/$hfile.sha256sum" "$OUT/bin/$hfile.sha256sum"
( cd "$OUT/bin" && sha256sum -c "$hfile.sha256sum" )
src "bin/$hfile" "https://get.helm.sh/$hfile" "$HELM_VERSION; sha256 сверен с .sha256sum"

echo "[5/8] образы контейнеров arm64, digest сохраняется"
img() {
  local ref="$1" file="$2" idx arm
  skopeo copy --quiet --override-os linux --override-arch arm64 --preserve-digests \
    "docker://$ref" "oci-archive:$OUT/images/$file:$ref"
  skopeo inspect --raw "docker://$ref" > "$OUT/images/.index.json"
  idx=$(sha256sum "$OUT/images/.index.json" | awk '{print $1}')
  # digest манифеста arm64 берём из самого архива (skopeo inspect --format отдаёт digest индекса)
  arm=$(tar -xOf "$OUT/images/$file" index.json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["manifests"][0]["digest"])')
  # и требуем, чтобы этот манифест стоял в индексе выпуска в реестре
  python3 -I -c 'import json,sys; m=json.load(open(sys.argv[1])).get("manifests",[]); sys.exit(0 if any(x["digest"]==sys.argv[2] for x in m) else 1)' \
    "$OUT/images/.index.json" "$arm" || { echo "манифест $arm не найден в индексе $ref"; exit 1; }
  rm -f "$OUT/images/.index.json"
  src "images/$file" "docker://$ref" "index sha256:$idx; arm64 $arm (есть в индексе реестра)"
}
img "docker.io/qdrant/qdrant:${QDRANT_VERSION}" "qdrant-${QDRANT_VERSION}-arm64.oci.tar"
img "docker.io/library/python:${PYTHON_IMAGE_TAG}" "python-${PYTHON_IMAGE_TAG}-arm64.oci.tar"

echo "[6/8] Helm-чарт Qdrant ${QDRANT_CHART_VERSION}, сверка с digest из index.yaml"
cfile="qdrant-${QDRANT_CHART_VERSION}.tgz"
curl_chart="https://github.com/qdrant/qdrant-helm/releases/download/qdrant-${QDRANT_CHART_VERSION}/$cfile"
fetch "$curl_chart" "$OUT/charts/$cfile"
fetch "https://qdrant.github.io/qdrant-helm/index.yaml" "$OUT/charts/qdrant-index.yaml"
want=$(awk -v v="$QDRANT_CHART_VERSION" '/^[[:space:]]+digest:/{d=$2} /^[[:space:]]+version:/{gsub(/"/,"",$2); if($2==v){print d; exit}}' "$OUT/charts/qdrant-index.yaml")
got=$(sha256sum "$OUT/charts/$cfile" | awk '{print $1}')
if [ -z "$want" ] || [ "$want" != "$got" ]; then echo "digest чарта не совпал: index=$want файл=$got"; exit 1; fi
src "charts/$cfile" "$curl_chart" "chart $QDRANT_CHART_VERSION, app $QDRANT_VERSION; sha256 сверен с index.yaml"

echo "[7/8] пакеты Python с хэшами под ${PYTHON_IMAGE_TAG} (cp313, aarch64)"
python3 -m pip download -q --only-binary=:all: \
  --platform manylinux_2_28_aarch64 --platform manylinux_2_17_aarch64 --platform manylinux2014_aarch64 \
  --python-version 3.13 --implementation cp --abi cp313 --abi abi3 --abi none \
  -d "$OUT/wheels" "${PY_PACKAGES[@]}"
python3 -I - "$OUT/wheels" > "$OUT/wheels/requirements.lock" <<'PY'
import hashlib, os, sys
d = sys.argv[1]
for f in sorted(os.listdir(d)):
    if f.endswith(".whl"):
        name, ver = f.split("-")[:2]
        h = hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest()
        print(f"{name.replace('_', '-').lower()}=={ver} --hash=sha256:{h}")
PY
src "wheels/*.whl" "pypi.org (pip download; pip сверил sha256 из индекса)" "$(paste -sd' ' <(cut -d' ' -f1 "$OUT/wheels/requirements.lock"))"

echo "[8/8] данные LegalBench-RAG"
fetch "$LEGALBENCH_URL" "$OUT/data/legalbenchrag.zip"
python3 -I - "$OUT/data/legalbenchrag.zip" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
tops = sorted({"/".join(n.split("/")[:2]) for n in names if "/" in n})
print(f"в архиве {len(names)} записей; верхние папки: {', '.join(tops[:8])}")
PY
src "data/legalbenchrag.zip" "dropbox, ссылка из README github.com/zeroentropy-cc/legalbenchrag" "источник суммы не публикует; sha256 зафиксирован здесь"

( cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
echo "== размеры:"; du -sh "$OUT"/* | sort -h
echo "готово: $OUT"
