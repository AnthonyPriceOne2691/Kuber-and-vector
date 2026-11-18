#!/bin/bash
# Ставит сервер k3s из комплекта на машину стенда. Интернет машине не нужен.
#   ./install-server.sh machine-1 init              первый сервер, поднимает встроенный etcd
#   ./install-server.sh machine-2 join machine-1    следующий сервер присоединяется к кластеру
# Флаги безопасности (профиль CIS, шифрование секретов, аудит) добавит дорожка безопасности.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$ROOT/kit/vm/k3s"
VM="${1:?машина}"; MODE="${2:?init или join}"; PEER="${3:-}"

for f in k3s-arm64 k3s-airgap-images-arm64.tar.zst install.sh; do
  [ -f "$KIT/$f" ] || { echo "нет в комплекте: $KIT/$f"; exit 1; }
done

# servicelb не нужен: вход держит HAProxy на machine-4
FLAGS="--node-name $VM --tls-san lima-$VM.internal --disable servicelb --write-kubeconfig-mode 0600"
case "$MODE" in
  init)
    FLAGS="--cluster-init $FLAGS" ;;
  join)
    [ -n "$PEER" ] || { echo "для join укажи машину, к которой присоединяться"; exit 1; }
    # токен идёт трубой из машины в машину, в аргументы команд и на диск Мака не попадает
    limactl shell "$PEER" sudo cat /var/lib/rancher/k3s/server/token \
      | limactl shell "$VM" sudo sh -c 'umask 077; mkdir -p /etc/rancher/k3s; cat > /etc/rancher/k3s/join-token'
    FLAGS="--server https://lima-$PEER.internal:6443 --token-file /etc/rancher/k3s/join-token $FLAGS" ;;
  *) echo "режим: init или join"; exit 1 ;;
esac

limactl copy "$KIT/k3s-arm64" "$VM:/tmp/k3s"
limactl copy "$KIT/k3s-airgap-images-arm64.tar.zst" "$VM:/tmp/k3s-airgap-images-arm64.tar.zst"
limactl copy "$KIT/install.sh" "$VM:/tmp/k3s-install.sh"
limactl shell --workdir /tmp "$VM" sudo env INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_EXEC="server $FLAGS" bash -c '
  set -euo pipefail
  install -m 0755 /tmp/k3s /usr/local/bin/k3s
  mkdir -p /var/lib/rancher/k3s/agent/images
  mv /tmp/k3s-airgap-images-arm64.tar.zst /var/lib/rancher/k3s/agent/images/
  sh /tmp/k3s-install.sh
  rm -f /tmp/k3s /tmp/k3s-install.sh
'
echo "k3s на $VM: $MODE"
