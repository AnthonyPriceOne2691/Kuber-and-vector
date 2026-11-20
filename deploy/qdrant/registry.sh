#!/bin/bash
# Реестр «коллекция → модель» и алиас поиска в кластерном Qdrant (deploy/qdrant/registry.py).
# Доступ как у deploy/qdrant/load.sh: изнутри машины кластера админским ключом из Secret, ключ живёт
# в файле 600 и удаляется при выходе, к Qdrant через kubectl port-forward (туннель под RBAC).
#   deploy/qdrant/registry.sh show [--json]                         алиасы и реестр
#   deploy/qdrant/registry.sh alias contracts contracts_rf2_arctic  переключить алиас одним запросом
#   deploy/qdrant/registry.sh register contracts_rf2 [cache/stage1/naive500_title]
#                                   записать в реестр уже загруженную коллекцию по набору, из которого
#                                   она загружена (новые коллекции load.sh записывает сам)
#   deploy/qdrant/registry.sh drop contracts_mislabeled            удалить подложенную коллекцию и запись
# REGISTER_AS=bge-m3 подписывает набор не той моделью, что в meta.json: только для подложенной ошибки
# (deploy/scenes/model-switch.sh). Вывод по-английски: он идёт в кадр.
# Пишется под системный bash 3.2 на macOS. Прогнан на стенде 20.11 в сцене смены модели (deploy/scenes/model-switch.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
VM="${VM:-machine-1}"
REGISTER_AS="${REGISTER_AS:-}"
REMOTE=/home/ops.guest/registry
PORT=16336
usage() {
  echo "usage: registry.sh show [--json] | alias NAME COLLECTION | register COLLECTION [SRC] | drop COLLECTION"
  exit 1
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
case "$cmd" in
  show)
    set -- show "$@" ;;
  alias)
    [ $# -eq 2 ] || usage
    set -- alias --name "$1" --to "$2" ;;
  register)
    [ $# -ge 1 ] && [ $# -le 2 ] || usage
    src="${2:-cache/stage1/naive500_title}"
    case "$src" in /*) ;; *) src="$ROOT/$src" ;; esac
    if [ -n "$REGISTER_AS" ]; then
      "$PY" -I -B "$ROOT/eval/fingerprint.py" "$src" --model "$REGISTER_AS" > "$T/entry.json"
    else
      "$PY" -I -B "$ROOT/eval/fingerprint.py" "$src" > "$T/entry.json"
    fi
    set -- register --entry entry.json --collection "$1" ;;
  drop)
    [ $# -eq 1 ] || usage
    set -- drop --collection "$1" ;;
  *) usage ;;
esac

limactl shell "$VM" sudo rm -rf "$REMOTE"
limactl shell "$VM" mkdir -p "$REMOTE"
limactl copy "$HERE/registry.py" "$VM:$REMOTE/registry.py"
if [ -f "$T/entry.json" ]; then limactl copy "$T/entry.json" "$VM:$REMOTE/entry.json"; fi
status=0
limactl shell --workdir "$REMOTE" "$VM" sudo bash -s -- "$REMOTE" "$PORT" "$@" <<'EOS' || status=$?
set -euo pipefail
cd "$1"; PORT=$2; shift 2
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K=$(mktemp -d); PF=""
trap 'kill $PF 2>/dev/null; rm -rf "$K"' EXIT
( umask 077; kubectl -n qdrant get secret qdrant-keys -o go-template='{{index .data "api-key"}}' | base64 -d > "$K/rw" )
kubectl -n qdrant port-forward svc/qdrant "$PORT:6333" >/dev/null 2>&1 & PF=$!
for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 1; done
python3 -I -B registry.py --url "http://127.0.0.1:$PORT" --key-file "$K/rw" "$@"
EOS
limactl shell "$VM" sudo rm -rf "$REMOTE"
exit "$status"
