#!/bin/bash
# Проверка k3s по CIS: kube-bench 0.16.0 из комплекта (утверждён владельцем 18.11) на сервере k3s.
# Ему нужен root на самой машине: он читает файлы k3s, флаги процесса и журнал службы. Поэтому
# запуск разовый, без сети (песочница машины), и бинарник удаляется сразу после прогона.
# Профиль k3s-cis-1.9 самый новый из k3s у этой версии; он писался под Kubernetes 1.27–1.29,
# у нас 1.37, поэтому часть проверок может не совпасть с флагами новой версии: такие строки
# разбираем руками по самооценке k3s (CIS Self-Assessment Guide), а не считаем провалом молча.
#   deploy/security/kube-bench.sh [machine-1]
#   BENCHMARK=k3s-cis-1.9 по умолчанию
# Итог: runs/security/<время>/kube-bench.json и сводка summary.md.
# Пишется под системный bash 3.2 на macOS. Прогнан на стенде 19.11.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
VM="${1:-machine-1}"
BENCHMARK="${BENCHMARK:-k3s-cis-1.9}"
TGZ="$ROOT/kit/vm-security/bin/kube-bench_0.16.0_linux_arm64.tar.gz"
OUT="$ROOT/runs/security/$(date +%Y%m%d-%H%M%S)"
[ -f "$TGZ" ] || { echo "kit missing: deploy/vm/kit-build.sh security"; exit 1; }
mkdir -p "$OUT"

limactl copy "$TGZ" "$VM:/tmp/kube-bench.tar.gz"
limactl shell --workdir /tmp "$VM" sudo bash -s -- "$BENCHMARK" <<'EOS'
set -euo pipefail
B=/root/kube-bench
trap 'rm -rf "$B" /tmp/kube-bench.tar.gz' EXIT
rm -rf "$B"; mkdir -m 0700 "$B"
tar -xzf /tmp/kube-bench.tar.gz -C "$B" --no-same-owner
cd "$B"
# код выхода kube-bench ненулевой, если есть FAIL: это результат, а не поломка
./kube-bench run --config-dir cfg --config cfg/config.yaml --benchmark "$1" --json --outputfile /tmp/kube-bench.json >/dev/null || true
test -s /tmp/kube-bench.json
chmod 0644 /tmp/kube-bench.json
EOS
limactl copy "$VM:/tmp/kube-bench.json" "$OUT/kube-bench.json"
limactl shell "$VM" sudo rm -f /tmp/kube-bench.json
status=0
"$PY" -I -B "$HERE/summary.py" bench "$OUT" > "$OUT/summary.md" || status=$?
cat "$OUT/summary.md"
echo "files: $OUT"
exit "$status"
