#!/bin/bash
# Настройки k3s по профилю CIS (руководство по усилению k3s, docs.k3s.io/security/hardening-guide)
# для новой установки. install-server.sh вызывает этот скрипт до установки k3s на машину.
#   - секреты в etcd зашифрованы (secrets-encryption);
#   - журнал аудита API: кто, что и когда, без тел запросов, с ротацией;
#   - Pod Security по умолчанию baseline, отчёт и предупреждения по restricted; пространства
#     search и qdrant сами поднимают себе restricted, falco помечен privileged (ему нужен eBPF);
#   - kubelet проверяет параметры ядра (protect-kernel-defaults), их ставим здесь же;
#   - потоковые соединения kubelet (exec, logs) закрываются через 5 минут простоя.
# На уже работающем кластере шифрование секретов так не включается (нужен k3s secrets-encrypt),
# поэтому стенд с этими настройками собирается заново: deploy/stand-up.sh на новых машинах.
#   deploy/k3s/cis.sh machine-1
set -euo pipefail
VM="${1:?машина}"

limactl shell "$VM" sudo bash -s <<'EOS'
set -euo pipefail
# параметры ядра, которые kubelet с protect-kernel-defaults требует, а не меняет сам
# (первые четыре из руководства k3s, ключи ядра kubelet тоже сверяет; у Ubuntu они такие же)
cat > /etc/sysctl.d/90-kubelet-cis.conf <<'EOF'
vm.panic_on_oom=0
vm.overcommit_memory=1
kernel.panic=10
kernel.panic_on_oops=1
kernel.keys.root_maxkeys=1000000
kernel.keys.root_maxbytes=25000000
EOF
sysctl -q -p /etc/sysctl.d/90-kubelet-cis.conf

S=/var/lib/rancher/k3s/server
install -d -m 0700 "$S" "$S/logs"

cat > "$S/audit.yaml" <<'EOF'
# Журнал аудита: метаданные всех запросов к API, без тел (секреты в журнал не попадают).
# Пульс узлов и проверки здоровья не пишем: это шум раз в секунды.
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: ["RequestReceived"]
rules:
  - level: None
    nonResourceURLs: ["/healthz*", "/readyz*", "/livez*", "/version"]
  - level: None
    userGroups: ["system:nodes"]
    verbs: ["get", "update", "patch"]
    resources:
      - group: coordination.k8s.io
        resources: ["leases"]
  # Чтения панели (узлы, поды, аренды kubelet раз в 2 с, deploy/panel) тоже шум. Только эти
  # ресурсы: если её токен попросит что-то ещё, например secrets, запрос и отказ в журнал попадут.
  - level: None
    users: ["system:serviceaccount:stand-panel:panel-reader"]
    verbs: ["get", "list", "watch"]
    resources:
      - group: ""
        resources: ["nodes", "pods"]
      - group: coordination.k8s.io
        resources: ["leases"]
  - level: Metadata
EOF

cat > "$S/psa.yaml" <<'EOF'
apiVersion: apiserver.config.k8s.io/v1
kind: AdmissionConfiguration
plugins:
  - name: PodSecurity
    configuration:
      apiVersion: pod-security.admission.config.k8s.io/v1
      kind: PodSecurityConfiguration
      defaults:
        enforce: "baseline"
        enforce-version: "latest"
        audit: "restricted"
        audit-version: "latest"
        warn: "restricted"
        warn-version: "latest"
      exemptions:
        usernames: []
        runtimeClasses: []
        namespaces: [kube-system]
EOF

install -d -m 0755 /etc/rancher/k3s/config.yaml.d
cat > /etc/rancher/k3s/config.yaml.d/40-cis.yaml <<'EOF'
# deploy/k3s/cis.sh
secrets-encryption: true
protect-kernel-defaults: true
kube-apiserver-arg:
  - "admission-control-config-file=/var/lib/rancher/k3s/server/psa.yaml"
  - "audit-policy-file=/var/lib/rancher/k3s/server/audit.yaml"
  - "audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log"
  - "audit-log-maxage=30"
  - "audit-log-maxbackup=4"
  - "audit-log-maxsize=50"
kube-controller-manager-arg:
  - "terminated-pod-gc-threshold=10"
kubelet-arg:
  - "streaming-connection-idle-timeout=5m"
EOF
echo "CIS: $(hostname)"
EOS
