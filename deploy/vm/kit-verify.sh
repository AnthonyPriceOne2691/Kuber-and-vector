#!/bin/bash
# Проверяет подписи издателей для уже собранного комплекта, одной командой на Маке.
# Одноразовая ВМ (роль builder) получает копии нужных файлов, запускает
# kit-verify-signatures.sh и удаляется. Отчёт пишется в deploy/vm/signatures.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VM=builder-verify

STAGE="$(mktemp -d)"
mkdir -p "$STAGE"/{lima,helm,trivy}
cp "$ROOT/kit/host/lima-v2.3.0/SHA256SUMS" "$STAGE/lima/"
cp "$ROOT/kit/vm/bin/helm-v4.3.2-linux-arm64.tar.gz" "$STAGE/helm/"
cp "$ROOT/kit/vm-security/bin/trivy_0.76.2_checksums.txt" "$STAGE/trivy/"
# Образы: имя без тега и digest индекса из SOURCES.tsv (подписи многоархитектурных образов
# обычно висят на индексе)
awk -F'\t' '/^images\//{ref=$2; sub("^docker://","",ref); sub(/:[^:\/]+$/,"",ref);
  if (match($3,/index sha256:[0-9a-f]+/)) print ref, substr($3,RSTART+6,RLENGTH-6)}' \
  "$ROOT/kit/vm/SOURCES.tsv" > "$STAGE/image-digests.txt"

limactl create --tty=false --name "$VM" --set '.param.ROLE = "builder"' "$HERE/machine.yaml" >/dev/null
trap 'limactl delete -f "$VM" >/dev/null 2>&1 || true; rm -rf "$STAGE"' EXIT
limactl start --tty=false "$VM" >/dev/null
limactl copy -r "$STAGE" "$VM:/tmp/sigin"
limactl copy "$HERE/kit-verify-signatures.sh" "$VM:/tmp/kit-verify-signatures.sh"
{
  echo "# Подписи издателей для комплекта стенда"
  echo
  echo "Проверено $(date '+%d.%m.%Y %H:%M') в одноразовой ВМ (\`deploy/vm/kit-verify.sh\`)."
  echo
  limactl shell --workdir /tmp "$VM" bash /tmp/kit-verify-signatures.sh /tmp/sigin
} > "$HERE/signatures.md"
cat "$HERE/signatures.md"
