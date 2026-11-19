#!/bin/bash
# Мониторинг стенда: Prometheus, node_exporter и Grafana. Это готовые программы для systemd
# на машинах стенда, образов контейнеров в этой части нет.
# Список ждёт утверждения владельца (deploy/monitoring/README.md, «Комплект на утверждение»):
# до его «да» скрипт не запускать.
# Запускается ТОЛЬКО внутри одноразовой сборочной ВМ (роль builder), без сети стенда.
#
# Проверки. Издатели эти архивы не подписывают (ни GPG, ни cosign). У Prometheus и node_exporter
# в выпуске только sha256sums.txt (у выпуска Prometheus есть ещё аттестация GitHub, её проверяет
# только gh, а его здесь нет), у Grafana sha256 на странице загрузки и файл .sha256 рядом с архивом.
# Поэтому ожидаемые sha256 закреплены в этом файле по официальным страницам на 19.11.2026: архив
# должен совпасть и с закреплённой суммой, и со списком сумм выпуска. Не совпало, остановка.
# Внутри архивов ничего не запускается: состав читается как данные (список tar, заголовок ELF,
# plugin.json). С Grafana 13.2 источник Prometheus в ней это отдельный плагин; если его нет
# в архиве, без интернета Grafana его не получит, поэтому это тоже остановка.
#   bash kit-fetch-monitoring.sh /home/ops.guest/kit-monitoring
set -euo pipefail

PROM_VERSION="3.16.0"
PROM_SHA256="fd8571b8162f4020232d886f17340bcf92e1722985ae3b4c1d096157826c0fbe"
NODEX_VERSION="1.13.0"
NODEX_SHA256="01f65e7ab3e1f7c04e167fbe097968609eebc9d54cbe523b71d101b830f5d54a"
GRAFANA_VERSION="13.3.1"
GRAFANA_BUILD="42849527790"
GRAFANA_SHA256="a96fa3dfe94eebb036e53bb4c9c19a9b35bb19fb42f788baf17000f8b6972b33"

OUT="${1:?укажи путь для комплекта}"
if [ -e "$OUT" ]; then echo "уже существует: $OUT"; exit 1; fi
mkdir -p "$OUT/bin"
SRC="$OUT/SOURCES.tsv"
printf 'файл\tисточник\tверсия, проверка, состав\n' > "$SRC"
src() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$SRC"; }
fetch() { curl -4 -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

# sha256 файла: и закреплённая здесь сумма, и сумма из списка выпуска
same() {
  local file="$1" pinned="$2" listed="$3" got
  got=$(sha256sum "$file" | awk '{print $1}')
  echo "  sha256 $got"
  if [ "$got" != "$pinned" ] || [ "$got" != "$listed" ]; then
    echo "СТОП: sha256 не совпал (закреплён $pinned, в списке выпуска ${listed:-нет})"; exit 1
  fi
}

# ELF читаем как данные: архитектура и динамический загрузчик (PT_INTERP), ничего не запускаем
elf() {
  python3 -I - "$1" <<'PY'
import struct, sys
with open(sys.argv[1], "rb") as f:
    d = f.read(1 << 16)
if d[:4] != b"\x7fELF" or d[4] != 2:
    sys.exit("не ELF64")
e = "<" if d[5] == 1 else ">"
machine = struct.unpack_from(e + "H", d, 18)[0]
phoff = struct.unpack_from(e + "Q", d, 32)[0]
phentsize, phnum = struct.unpack_from(e + "HH", d, 54)
interp = "нет (статический)"
for i in range(phnum):
    o = phoff + i * phentsize
    if struct.unpack_from(e + "I", d, o)[0] == 3:  # PT_INTERP
        off = struct.unpack_from(e + "Q", d, o + 8)[0]
        size = struct.unpack_from(e + "Q", d, o + 32)[0]
        interp = d[off:off + size].rstrip(b"\0").decode()
arch = "arm64" if machine == 183 else f"машина {machine}"
print(f"{arch}, загрузчик {interp}")
PY
}

echo "[1/4] Prometheus $PROM_VERSION"
pbase="https://github.com/prometheus/prometheus/releases/download/v$PROM_VERSION"
pfile="prometheus-$PROM_VERSION.linux-arm64.tar.gz"
pdir="prometheus-$PROM_VERSION.linux-arm64"
fetch "$pbase/$pfile" "$OUT/bin/$pfile"
fetch "$pbase/sha256sums.txt" "$W/prometheus-sha256sums.txt"
same "$OUT/bin/$pfile" "$PROM_SHA256" "$(awk -v f="$pfile" '$2==f {print $1}' "$W/prometheus-sha256sums.txt")"
mkdir -p "$W/prom"
tar -xzf "$OUT/bin/$pfile" --no-same-owner -C "$W/prom" "$pdir/prometheus" "$pdir/promtool"
pelf="prometheus: $(elf "$W/prom/$pdir/prometheus"); promtool: $(elf "$W/prom/$pdir/promtool")"
echo "  $pelf"
src "bin/$pfile" "$pbase/$pfile" "v$PROM_VERSION; sha256 закреплён и совпал с sha256sums.txt выпуска; $pelf"

echo "[2/4] node_exporter $NODEX_VERSION"
nbase="https://github.com/prometheus/node_exporter/releases/download/v$NODEX_VERSION"
nfile="node_exporter-$NODEX_VERSION.linux-arm64.tar.gz"
ndir="node_exporter-$NODEX_VERSION.linux-arm64"
fetch "$nbase/$nfile" "$OUT/bin/$nfile"
fetch "$nbase/sha256sums.txt" "$W/node-sha256sums.txt"
same "$OUT/bin/$nfile" "$NODEX_SHA256" "$(awk -v f="$nfile" '$2==f {print $1}' "$W/node-sha256sums.txt")"
mkdir -p "$W/node"
tar -xzf "$OUT/bin/$nfile" --no-same-owner -C "$W/node" "$ndir/node_exporter"
nelf="node_exporter: $(elf "$W/node/$ndir/node_exporter")"
echo "  $nelf"
src "bin/$nfile" "$nbase/$nfile" "v$NODEX_VERSION; sha256 закреплён и совпал с sha256sums.txt выпуска; $nelf"

echo "[3/4] Grafana OSS $GRAFANA_VERSION"
gbase="https://dl.grafana.com/grafana/release/$GRAFANA_VERSION"
gfile="grafana_${GRAFANA_VERSION}_${GRAFANA_BUILD}_linux_arm64.tar.gz"
fetch "$gbase/$gfile" "$OUT/bin/$gfile"
fetch "$gbase/$gfile.sha256" "$W/grafana.sha256"
same "$OUT/bin/$gfile" "$GRAFANA_SHA256" "$(awk '{print $1; exit}' "$W/grafana.sha256")"
tar -tzf "$OUT/bin/$gfile" > "$W/grafana-files.txt"
gbin=$(grep -E '^(\./)?([^/]+/)?bin/grafana$' "$W/grafana-files.txt" | head -1 || true)
[ -n "$gbin" ] || { echo "СТОП: в архиве нет bin/grafana"; exit 1; }
gtop="${gbin%bin/grafana}"
echo "  папка в архиве: ${gtop:-корень}, файлов: $(wc -l < "$W/grafana-files.txt")"
mkdir -p "$W/graf" "$W/gp"
tar -xzf "$OUT/bin/$gfile" --no-same-owner -C "$W/graf" "$gbin"
gelf=$(elf "$W/graf/$gbin")
echo "  bin/grafana: $gelf"
case "$gelf" in
  *musl*) echo "СТОП: Grafana собрана под musl, на Ubuntu ей нужен пакет musl, его нет в утверждённом списке"; exit 1 ;;
esac
# источник Prometheus: с 13.2 отдельный плагин в plugins-bundled, раньше встроен в public/app/plugins
grep -E '(^|/)plugin\.json$' "$W/grafana-files.txt" \
  | grep -E '(^|/)(plugins-bundled/|public/app/plugins/datasource/prometheus/)' > "$W/grafana-plugins.txt" || true
if [ -s "$W/grafana-plugins.txt" ]; then
  tar -xzf "$OUT/bin/$gfile" --no-same-owner -C "$W/gp" -T "$W/grafana-plugins.txt"
fi
gplug=$(python3 -I - "$W/gp" <<'PY'
import json, os, sys
top, found, bundled = sys.argv[1], None, set()
for root, _, files in os.walk(top):
    if "plugin.json" not in files:
        continue
    path = os.path.join(root, "plugin.json")
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        continue
    rel = os.path.relpath(root, top)
    if "plugins-bundled" in rel.split(os.sep):
        bundled.add(f"{m.get('id')}@{(m.get('info') or {}).get('version', '?')}")
    if m.get("id") == "prometheus" and m.get("type") == "datasource" and found is None:
        found = rel
print(f"источник prometheus: {found or 'НЕТ'}; plugins-bundled: {' '.join(sorted(bundled)) or 'нет'}")
PY
)
echo "  $gplug"
case "$gplug" in
  "источник prometheus: НЕТ"*) echo "СТОП: в архиве нет источника Prometheus, без интернета Grafana его не получит (развилка в README: Grafana 13.1.10)"; exit 1 ;;
esac
src "bin/$gfile" "$gbase/$gfile" "v$GRAFANA_VERSION OSS; sha256 закреплён по grafana.com/grafana/download и совпал с .sha256 на dl.grafana.com; папка ${gtop:-корень}; bin/grafana: $gelf; $gplug"

echo "[4/4] суммы"
( cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
du -sh "$OUT"/* | sort -h
echo "готово: $OUT"
