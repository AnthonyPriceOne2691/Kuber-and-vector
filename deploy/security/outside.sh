#!/bin/bash
# Одноразовая машина «снаружи»: смотрит на стенд глазами постороннего в той же сети машин.
# Та же схема machine.yaml, роль outside. Такой роли песочница не знает и даёт самый строгий режим:
# только сеть машин, без Мака и без интернета. Ключей и адресов стенда, кроме публичных, у неё нет.
# Здесь же работают чужие сканеры (nmap и trivy из комплекта kit/vm-security, утверждены владельцем
# 18.11): они не видят ни ключей кластера, ни файлов Мака и уходят вместе с машиной.
#   deploy/security/outside.sh up            создать и поставить nmap
#   deploy/security/outside.sh trivy         поставить trivy и его базу (1,8 ГБ, для scan-images.sh)
#   deploy/security/outside.sh down          удалить
# 1 vCPU и 2 ГБ: trivy на образах берёт около гигабайта (оценка, мерить на прогоне).
# Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm-security"
VM="${OUTSIDE_VM:-outside}"
TRIVY=0.76.2
exists() { limactl list -q 2>/dev/null | grep -qx "$VM"; }

case "${1:-up}" in
  up)
    [ -d "$KIT/debs" ] || { echo "kit missing: deploy/vm/kit-build.sh security"; exit 1; }
    if ! exists; then
      limactl create --tty=false --name "$VM" \
        --set '.param.ROLE = "outside" | .cpus = 1 | .memory = "2GiB"' "$ROOT/deploy/vm/machine.yaml"
    fi
    limactl start --tty=false "$VM" >/dev/null
    if ! limactl shell "$VM" bash -c 'command -v nmap' >/dev/null 2>&1; then
      limactl shell "$VM" bash -c 'rm -rf /tmp/kit-debs && mkdir -p /tmp/kit-debs /tmp/apt-no-sources'
      for f in "$KIT"/debs/*.deb; do limactl copy "$f" "$VM:/tmp/kit-debs/"; done
      # пакеты только из комплекта: репозитории отключены, интернета у машины и так нет
      limactl shell "$VM" sudo bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts=/tmp/apt-no-sources /tmp/kit-debs/*.deb >/dev/null
        rm -rf /tmp/kit-debs'
    fi
    echo "$VM: $(limactl shell "$VM" bash -c 'nmap --version | head -1'), sandbox role $(limactl shell "$VM" cat /run/sandbox-role)"
    ;;
  trivy)
    exists || "$0" up
    if ! limactl shell "$VM" bash -c 'command -v trivy' >/dev/null 2>&1; then
      limactl copy "$KIT/bin/trivy_${TRIVY}_Linux-ARM64.tar.gz" "$VM:/tmp/trivy.tar.gz"
      limactl shell --workdir /tmp "$VM" sudo bash -c 'tar -xzf /tmp/trivy.tar.gz -C /tmp trivy \
        && install -m 0755 /tmp/trivy /usr/local/bin/trivy && rm -f /tmp/trivy /tmp/trivy.tar.gz'
    fi
    if ! limactl shell "$VM" test -s /home/ops.guest/trivy-cache/db/trivy.db; then
      limactl shell "$VM" mkdir -p /home/ops.guest/trivy-cache
      limactl copy -r "$KIT/trivy-cache/db" "$VM:/home/ops.guest/trivy-cache/db"
    fi
    echo "$VM: $(limactl shell "$VM" trivy --version 2>/dev/null | head -1), database $(limactl shell "$VM" cat /home/ops.guest/trivy-cache/db/metadata.json)"
    ;;
  down)
    if exists; then limactl delete --force "$VM" >/dev/null; echo "$VM deleted"; else echo "no $VM"; fi
    ;;
  *) echo "mode: up, trivy or down"; exit 1 ;;
esac
