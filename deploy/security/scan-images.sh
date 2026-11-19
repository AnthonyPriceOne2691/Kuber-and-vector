#!/bin/bash
# Уязвимости в образах, которые реально работают на стенде: trivy 0.76.2 с базой из комплекта,
# без сети, на машине «снаружи» (deploy/security/outside.sh), где нет ключей кластера.
# Образы выгружаются из containerd machine-1 архивами в runs/security/<время>/images (в git не идёт).
# База уязвимостей это снимок на дату сборки комплекта: дата стоит в сводке.
#   deploy/security/scan-images.sh [machine-1]
#   KEEP=1 оставить машину «снаружи» после скана
# Итог: runs/security/<время>/trivy/*.json и сводка summary.md.
# Пишется под системный bash 3.2 на macOS. Прогнан на стенде 19.11.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
VM="${1:-machine-1}"
OUTSIDE="${OUTSIDE_VM:-outside}"
OUT="$ROOT/runs/security/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT/images" "$OUT/trivy"

# образы работающих подов, по одному архиву на образ
images=$(limactl shell "$VM" sudo k3s kubectl get pods -A -o jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' | sort -u)
[ -n "$images" ] || { echo "no pods"; exit 1; }
: > "$OUT/images.txt"
for img in $images; do
  case "$img" in *[!A-Za-z0-9._/:@-]*) echo "unexpected image name: $img"; exit 1 ;; esac
  file="$(echo "$img" | tr '/:@' '___').tar"
  # Имя без реестра (rancher/...) containerd хранит с docker.io/
  ref="$img"; case "$ref" in */*/*|localhost/*) ;; */*) ref="docker.io/$ref" ;; *) ref="docker.io/library/$ref" ;; esac
  limactl shell "$VM" sudo k3s ctr -n k8s.io images export --platform linux/arm64 "/tmp/$file" "$ref"
  limactl shell "$VM" sudo chmod 0644 "/tmp/$file"
  limactl copy "$VM:/tmp/$file" "$OUT/images/$file"
  limactl shell "$VM" sudo rm -f "/tmp/$file"
  printf '%s\t%s\n' "$file" "$img" >> "$OUT/images.txt"
done

OUTSIDE_VM="$OUTSIDE" "$HERE/outside.sh" trivy
[ "${KEEP:-0}" = 1 ] || trap 'OUTSIDE_VM="$OUTSIDE" "$HERE/outside.sh" down' EXIT
limactl shell "$OUTSIDE" bash -c 'rm -rf /tmp/scan && mkdir -p /tmp/scan'
# limactl внутри цикла читает stdin, поэтому у каждого вызова свой </dev/null
while IFS="$(printf '\t')" read -r file img; do
  limactl copy "$OUT/images/$file" "$OUTSIDE:/tmp/scan/$file" </dev/null
  # без сети: база и проверки только из комплекта, обновлений и телеметрии нет
  limactl shell "$OUTSIDE" env TRIVY_SKIP_VERSION_CHECK=true TRIVY_DISABLE_TELEMETRY=true \
    trivy image --input "/tmp/scan/$file" --offline-scan --skip-db-update --skip-java-db-update \
    --cache-dir /home/ops.guest/trivy-cache --scanners vuln --format json --quiet --output "/tmp/scan/$file.json" </dev/null
  limactl copy "$OUTSIDE:/tmp/scan/$file.json" "$OUT/trivy/$file.json" </dev/null
  limactl shell "$OUTSIDE" rm -f "/tmp/scan/$file" </dev/null
  echo "$img: scanned"
done < "$OUT/images.txt"
limactl shell "$OUTSIDE" cat /home/ops.guest/trivy-cache/db/metadata.json > "$OUT/trivy-db.json"

status=0
"$PY" -I -B "$HERE/summary.py" images "$OUT" > "$OUT/summary.md" || status=$?
cat "$OUT/summary.md"
echo "files: $OUT"
exit "$status"
