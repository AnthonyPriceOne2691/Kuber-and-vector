#!/bin/bash
# Политика «образы только из комплекта» (deploy/k3s/image-policy.yaml) для всех пространств,
# кроме kube-system. Сначала сверяет работающие поды: если все по правилам, политика сразу
# отклоняет нарушителей (Deny); если нет, ставится в режиме предупреждений (Warn и Audit)
# и печатает, кто мешает, как проверка на сухую у Pod Security в deploy/qdrant/install.sh.
#   deploy/k3s/image-policy.sh [machine-1]
#   ALLOWED="образ образ ..." другой список (по умолчанию образы комплекта и поиска stage2)
# Повторный запуск безопасен. Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VM="${1:-machine-1}"
ALLOWED="${ALLOWED:-localhost/search:stage2 docker.io/qdrant/qdrant:v1.20.2 docker.io/falcosecurity/falco:0.45.2}"
for img in $ALLOWED; do
  case "$img" in *[!A-Za-z0-9._/:@-]*) echo "странное имя образа: $img"; exit 1 ;; esac
done

limactl copy "$HERE/image-policy.yaml" "$VM:/tmp/image-policy.yaml"
limactl shell --workdir /tmp "$VM" sudo bash -s -- "$ALLOWED" <<'EOS'
set -euo pipefail
ALLOWED=$1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
# поды вне kube-system: «пространство/под образ=политика ...»
kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{range .spec.initContainers[*]} {.image}={.imagePullPolicy}{end}{range .spec.containers[*]} {.image}={.imagePullPolicy}{end}{"\n"}{end}' \
  > /tmp/image-policy-pods
bad=$(awk -v allowed="$ALLOWED" '
  BEGIN { n = split(allowed, a, " "); for (i = 1; i <= n; i++) ok[a[i]] = 1 }
  $1 ~ /^kube-system\// { next }
  { for (i = 2; i <= NF; i++) { split($i, p, "="); if (!(p[1] in ok) || p[2] != "Never") print $1, $i } }
' /tmp/image-policy-pods)
rm -f /tmp/image-policy-pods
if [ -z "$bad" ]; then actions="[Deny]"; else actions="[Warn, Audit]"; fi
list=""
for img in $ALLOWED; do list="${list:+$list, }'$img'"; done
sed -e "s|@ALLOWED@|[$list]|" -e "s|@ACTIONS@|$actions|" /tmp/image-policy.yaml | kubectl apply -f - >/dev/null
rm -f /tmp/image-policy.yaml
echo "политика stand-images: $actions, разрешены $ALLOWED"
if [ -n "$bad" ]; then
  echo "не по правилам (поэтому пока только предупреждения):"
  echo "$bad" | sed 's/^/  /'
fi
EOS
