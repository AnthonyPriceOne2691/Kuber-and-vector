#!/bin/bash
# Инструменты безопасности для сцены «атака снаружи» и проверки по CIS.
# Запускается ТОЛЬКО внутри одноразовой сборочной ВМ (роль builder) и только на чистой
# машине: пакеты Ubuntu качаются первыми, чтобы зависимости совпали с чистой машиной стенда.
# Утверждено владельцем 18.11.2026: nmap, trivy, kube-bench.
#
# trivy: в марте 2026 цепочка поставок Trivy была скомпрометирована (GHSA-69fq-xp46-6x23,
# вредоносные v0.69.4 и образы v0.69.5/0.69.6). Берём v0.76.2: после инцидента, выпуск
# неизменяемый (immutable release), исправление GHSA-mcj4-mphf-j9ff (с 0.71.1) внутри.
# Подписи sigstore не проверяем (нужен cosign, его нет в списке); сверяем sha256 из выпуска.
#
#   bash kit-fetch-security.sh /home/ops.guest/kit-security
set -euo pipefail

TRIVY_VERSION="0.76.2"
KUBEBENCH_VERSION="0.16.0"
DEBS=(nmap)

OUT="${1:?укажи путь для комплекта}"
if [ -e "$OUT" ]; then echo "уже существует: $OUT"; exit 1; fi
mkdir -p "$OUT"/{debs/partial,bin,trivy-cache}
SRC="$OUT/SOURCES.tsv"
printf 'файл\tисточник\tверсия, проверка\n' > "$SRC"
src() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$SRC"; }
fetch() { curl -4 -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"; }
export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -o Acquire::ForceIPv4=true -o Dpkg::Use-Pty=0 -qq)

echo "[1/4] nmap (пакеты Ubuntu, первым делом, на чистой машине)"
sudo "${APT[@]}" update
sudo "${APT[@]}" install --download-only -y -o Dir::Cache::archives="$OUT/debs" "${DEBS[@]}"
sudo rm -rf "$OUT/debs/partial" "$OUT/debs/lock"
sudo chown -R "$(id -u):$(id -g)" "$OUT/debs"
for f in "$OUT"/debs/*.deb; do
  src "debs/$(basename "$f")" "ports.ubuntu.com (apt, noble; подписи репозитория проверил apt)" "$(dpkg-deb -f "$f" Version)"
done

echo "[2/4] trivy v${TRIVY_VERSION}"
tbase="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}"
tfile="trivy_${TRIVY_VERSION}_Linux-ARM64.tar.gz"
fetch "$tbase/$tfile" "$OUT/bin/$tfile"
fetch "$tbase/trivy_${TRIVY_VERSION}_checksums.txt" "$OUT/bin/trivy_${TRIVY_VERSION}_checksums.txt"
( cd "$OUT/bin" && grep -E " ${tfile}\$" "trivy_${TRIVY_VERSION}_checksums.txt" | sha256sum -c - )
src "bin/$tfile" "$tbase/$tfile" "v$TRIVY_VERSION; sha256 сверен с checksums.txt неизменяемого выпуска; sigstore не проверен"

echo "[3/4] база уязвимостей trivy (снимок на дату сборки, для работы без сети)"
mkdir -p /tmp/trivy-bin
tar -xzf "$OUT/bin/$tfile" -C /tmp/trivy-bin trivy
# через VPN база идёт медленно: стандартных 5 минут не хватило; второй источник на случай сбоя первого
/tmp/trivy-bin/trivy --cache-dir "$OUT/trivy-cache" image --download-db-only --no-progress --quiet \
  --timeout 30m --db-repository "mirror.gcr.io/aquasec/trivy-db:2,ghcr.io/aquasecurity/trivy-db:2"
db_meta="$(cat "$OUT/trivy-cache/db/metadata.json" 2>/dev/null || echo '{}')"
src "trivy-cache/db" "ghcr.io/aquasecurity/trivy-db (скачал сам trivy)" "metadata: $db_meta"
rm -rf /tmp/trivy-bin

echo "[4/4] kube-bench v${KUBEBENCH_VERSION}"
kbase="https://github.com/aquasecurity/kube-bench/releases/download/v${KUBEBENCH_VERSION}"
kfile="kube-bench_${KUBEBENCH_VERSION}_linux_arm64.tar.gz"
fetch "$kbase/$kfile" "$OUT/bin/$kfile"
if fetch "$kbase/kube-bench_${KUBEBENCH_VERSION}_checksums.txt" "$OUT/bin/kube-bench_${KUBEBENCH_VERSION}_checksums.txt"; then
  ( cd "$OUT/bin" && grep -E " ${kfile}\$" "kube-bench_${KUBEBENCH_VERSION}_checksums.txt" | sha256sum -c - )
  src "bin/$kfile" "$kbase/$kfile" "v$KUBEBENCH_VERSION; sha256 сверен с checksums.txt выпуска"
else
  src "bin/$kfile" "$kbase/$kfile" "v$KUBEBENCH_VERSION; выпуск не публикует суммы, sha256 зафиксирован здесь"
fi

( cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
echo "== размеры:"; du -sh "$OUT"/* | sort -h
echo "готово: $OUT"
