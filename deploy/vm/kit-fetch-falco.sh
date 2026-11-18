#!/bin/bash
# Falco для сцены «заражённый под»: сторож, который видит подозрительные действия в контейнерах.
# Утверждено владельцем 18.11.2026 («Falco качаем»).
# Запускается ТОЛЬКО внутри одноразовой сборочной ВМ (роль builder), без сети стенда.
#
# Состав: образ falcosecurity/falco (arm64, по digest) и чарт falco. Модуль modern eBPF встроен
# в программу, стандартные правила лежат в образе, поэтому falcoctl (качает правила и плагины
# при старте из интернета) не нужен и в комплект не идёт. Есть ли в образе плагин container
# (имена подов и пространств имён), скрипт проверяет по слоям образа и пишет в опись.
#
# Проверки: cosign сверяется по sha256 своего выпуска; образ обязан быть подписан выпускным
# workflow falcosecurity/falco (Sigstore, keyless, издатель GitHub Actions), иначе остановка:
# Falco работает в кластере с правами ядра. Чарт сверяется с digest из официального index.yaml.
#   bash kit-fetch-falco.sh /home/ops.guest/kit-falco
set -euo pipefail

FALCO_VERSION="0.45.2"
CHART_VERSION="9.2.2"
COSIGN_VERSION="v3.1.4"

OUT="${1:?укажи путь для комплекта}"
if [ -e "$OUT" ]; then echo "уже существует: $OUT"; exit 1; fi
mkdir -p "$OUT"/{images,charts}
SRC="$OUT/SOURCES.tsv"
printf 'файл\tисточник\tверсия, digest, проверка\n' > "$SRC"
src() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$SRC"; }
fetch() { curl -4 -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
export DEBIAN_FRONTEND=noninteractive

echo "[1/5] инструменты сборочной ВМ (в комплект не идут): skopeo из Ubuntu, cosign из выпуска"
sudo apt-get -o Acquire::ForceIPv4=true -qq update
sudo apt-get -o Acquire::ForceIPv4=true -qq install -y skopeo >/dev/null
cb="https://github.com/sigstore/cosign/releases/download/$COSIGN_VERSION"
fetch "$cb/cosign-linux-arm64" "$W/cosign"
fetch "$cb/cosign_checksums.txt" "$W/cosign_checksums.txt"
( cd "$W" && grep -E ' cosign-linux-arm64$' cosign_checksums.txt | sed 's/cosign-linux-arm64$/cosign/' | sha256sum -c - )
chmod +x "$W/cosign"; COSIGN="$W/cosign"

echo "[2/5] образ falco $FALCO_VERSION: digest индекса и подпись"
repo="docker.io/falcosecurity/falco"
skopeo inspect --raw "docker://$repo:$FALCO_VERSION" > "$W/index.json"
idx="sha256:$(sha256sum "$W/index.json" | awk '{print $1}')"
echo "  индекс $idx"
# подпись именно этого digest, сделанная workflow репозитория falcosecurity/falco на GitHub Actions;
# сначала строго с привязкой к тегу выпуска, потом по репозиторию (так и запишем в опись)
tag_re="^https://github\\.com/falcosecurity/falco/\\.github/workflows/[^@]+@refs/tags/${FALCO_VERSION//./\\.}\$"
repo_re='^https://github\.com/falcosecurity/falco/\.github/workflows/'
bound=""
for re in "$tag_re" "$repo_re"; do
  if "$COSIGN" verify --certificate-identity-regexp "$re" \
       --certificate-oidc-issuer https://token.actions.githubusercontent.com \
       "$repo@$idx" > "$W/sig.json" 2> "$W/sig.err"; then bound="$re"; break; fi
done
if [ -z "$bound" ]; then
  echo "  подпись не прошла:"; tail -3 "$W/sig.err"; echo "СТОП: образ без подписи falcosecurity/falco в комплект не берём"; exit 1
fi
# кто подписал: ищем в выводе cosign поля subject/issuer/workflow, где бы они ни лежали
signed_by=$(python3 -I - "$W/sig.json" <<'PY'
import json, re, sys
found = {}
def walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, str) and re.search(r"(?i)subject|issuer|workflowref|57264\.1\.(9|14)", k):
                found.setdefault(k, v)
            walk(v)
    elif isinstance(x, list):
        for v in x:
            walk(v)
walk(json.load(open(sys.argv[1])))
print("; ".join(f"{k}={v}" for k, v in list(found.items())[:4]) or "поля не найдены")
PY
)
[ "$bound" = "$tag_re" ] && how="привязана к тегу $FALCO_VERSION" || how="workflow репозитория falcosecurity/falco (без привязки к тегу)"
echo "  подпись верна: $how"
echo "  подписал: $signed_by"
signed_by="$how; $signed_by"

echo "[3/5] образ arm64 в архив, digest сохраняется"
file="falco-$FALCO_VERSION-arm64.oci.tar"
skopeo copy --quiet --override-os linux --override-arch arm64 --preserve-digests \
  "docker://$repo@$idx" "oci-archive:$OUT/images/$file:$repo:$FALCO_VERSION"
arm=$(tar -xOf "$OUT/images/$file" index.json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["manifests"][0]["digest"])')
python3 -I -c 'import json,sys; m=json.load(open(sys.argv[1])).get("manifests",[]); sys.exit(0 if any(x["digest"]==sys.argv[2] for x in m) else 1)' \
  "$W/index.json" "$arm" || { echo "манифест $arm не найден в индексе"; exit 1; }
# что внутри: правила, плагины (слои читаем как данные, ничего не запускаем)
mkdir -p "$W/img" && tar -xf "$OUT/images/$file" -C "$W/img"
: > "$W/files.txt"
# сжатие слоёв tar определяет сам (gzip или zstd); манифесты и конфиги не архивы, их пропускаем
for b in "$W"/img/blobs/sha256/*; do tar -tf "$b" 2>/dev/null >> "$W/files.txt" || true; done
echo "  файлов в слоях: $(wc -l < "$W/files.txt")"
rules=$(grep -cE '(^|/)etc/falco/falco_rules\.yaml$' "$W/files.txt" || true)
plugins=$( { grep -E '(^|/)usr/share/falco/plugins/[^/]+\.so$' "$W/files.txt" || true; } | sed 's|.*/||' | sort -u | tr '\n' ' ')
echo "  стандартные правила в образе: $([ "$rules" -gt 0 ] && echo есть || echo НЕТ)"
echo "  плагины в образе: ${plugins:-нет}"
src "images/$file" "docker://$repo:$FALCO_VERSION" "index $idx; arm64 $arm; подпись Sigstore: $signed_by; правила в образе: $([ "$rules" -gt 0 ] && echo да || echo нет); плагины: ${plugins:-нет}"

echo "[4/5] чарт falco $CHART_VERSION, сверка с digest из index.yaml"
fetch "https://falcosecurity.github.io/charts/index.yaml" "$W/charts-index.yaml"
want=$(python3 -I - "$W/charts-index.yaml" "$CHART_VERSION" <<'PY'
import sys, yaml  # PyYAML есть в образе Ubuntu (на нём работает cloud-init)
idx = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for e in idx["entries"].get("falco", []):
    if e.get("name") == "falco" and str(e.get("version")) == sys.argv[2]:
        print(e["digest"]); break
PY
)
cfile="falco-$CHART_VERSION.tgz"
fetch "https://github.com/falcosecurity/charts/releases/download/falco-$CHART_VERSION/$cfile" "$OUT/charts/$cfile"
got=$(sha256sum "$OUT/charts/$cfile" | awk '{print $1}')
if [ -z "$want" ] || [ "$want" != "$got" ]; then echo "digest чарта не совпал: index=$want файл=$got"; exit 1; fi
deps=$(tar -tzf "$OUT/charts/$cfile" | grep -E '^falco/charts/[^/]+/Chart\.yaml$' | sed 's|falco/charts/||; s|/Chart.yaml||' | tr '\n' ' ')
echo "  sha256 совпал с index.yaml; вложенные чарты: ${deps:-нет}"
src "charts/$cfile" "github.com/falcosecurity/charts, выпуск falco-$CHART_VERSION" "sha256 $got совпал с index.yaml; вложенные чарты: ${deps:-нет}"

echo "[5/5] суммы"
( cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
du -sh "$OUT"/* | sort -h
echo "готово: $OUT"
