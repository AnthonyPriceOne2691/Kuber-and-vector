#!/bin/bash
# DNS кластера без выхода наружу: из Corefile CoreDNS убирается `forward . /etc/resolv.conf`.
# Имена кластера (cluster.local) отвечают как раньше, чужое имя сразу получает отказ (SERVFAIL),
# а не уходит к DNS машины. Наружу этот запрос и так не прошёл бы (песочница машины закрывает DNS),
# но теперь его нет вовсе: через DNS кластера нельзя вынести данные даже со снятым файрволом машины.
# Чтобы k3s не вернул свой Corefile при перезапуске, на каждом сервере кладётся coredns.yaml.skip
# (штатный способ k3s остановить обновление встроенного манифеста); NodeHosts k3s обновляет по-прежнему.
#   deploy/k3s/coredns-local.sh [machine-1]
#   SERVERS="machine-1 machine-2 machine-3" по умолчанию
# Повторный запуск безопасен. Пишется под системный bash 3.2 на macOS. На живом стенде ещё не проверялся.
set -euo pipefail
VM="${1:-machine-1}"
SERVERS="${SERVERS:-machine-1 machine-2 machine-3}"
for s in $SERVERS; do
  limactl shell "$s" sudo touch /var/lib/rancher/k3s/server/manifests/coredns.yaml.skip
done
limactl shell --workdir /tmp "$VM" sudo bash -s <<'EOS'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' > /tmp/Corefile
if grep -q 'forward' /tmp/Corefile; then
  line='^[[:space:]]*forward \. /etc/resolv\.conf[[:space:]]*$'
  grep -Eq "$line" /tmp/Corefile || { echo "в Corefile другой forward, правлю только известный вид:"; cat /tmp/Corefile; exit 1; }
  grep -Ev "$line" /tmp/Corefile > /tmp/Corefile.local
  if grep -q 'forward' /tmp/Corefile.local; then echo "forward остался, стоп"; cat /tmp/Corefile.local; exit 1; fi
  python3 -I -c 'import json, sys; print(json.dumps({"data": {"Corefile": open(sys.argv[1]).read()}}))' /tmp/Corefile.local \
    > /tmp/coredns-patch.json
  kubectl -n kube-system patch configmap coredns --type merge --patch-file /tmp/coredns-patch.json >/dev/null
  # reload в Corefile подхватил бы правку сам через минуту-две, перезапуск быстрее и виден
  kubectl -n kube-system rollout restart deploy/coredns >/dev/null
  kubectl -n kube-system rollout status deploy/coredns --timeout=120s
  echo "CoreDNS: forward убран"
else
  echo "CoreDNS: forward уже убран"
fi
rm -f /tmp/Corefile /tmp/Corefile.local /tmp/coredns-patch.json
EOS
