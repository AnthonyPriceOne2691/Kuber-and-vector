#!/bin/bash
# Настройки k3s по профилю CIS (руководство по усилению k3s, docs.k3s.io/security/hardening-guide)
# для новой установки. install-server.sh вызывает этот скрипт до установки k3s на машину.
#   - секреты в etcd зашифрованы (secrets-encryption);
#   - журнал аудита API: кто, что и когда, без тел запросов, с ротацией;
#   - Pod Security по умолчанию baseline, отчёт и предупреждения по restricted; пространства
#     search и qdrant сами поднимают себе restricted, falco помечен privileged (ему нужен eBPF);
#   - kubelet проверяет параметры ядра (protect-kernel-defaults), их ставим здесь же;
#   - потоковые соединения kubelet (exec, logs) закрываются через 5 минут простоя;
#   - публичные сертификаты k3s только для root, 600 (CIS 1.1.20): правило systemd после
#     каждого старта k3s, потому что k3s пишет их заново с 644.
# На уже работающем кластере шифрование секретов так не включается (нужен k3s secrets-encrypt),
# поэтому стенд с этими настройками собирается заново: deploy/stand-up.sh на новых машинах.
# Повторный запуск на работающем сервере безопасен: файлы те же, k3s не перезапускается, а права
# сертификатов ставятся сразу. Так права 600 доходят до уже поднятого стенда (по одному разу на
# machine-1…3); остальное из этого файла k3s читает только при старте.
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

# CIS 1.1.20: публичные сертификаты k3s (server/tls и вложенные папки) только для root. k3s пишет
# их с 644 (прогон kube-bench 19.11): при установке, при перевыпуске на старте, когда срок подходит
# к концу, и после k3s certificate rotate. Поэтому права ставит правило systemd после каждого старта:
# у службы k3s Type=notify, и ExecStartPost идёт, когда k3s сообщил о готовности, то есть файлы уже
# на месте. «-» впереди: если find не сработает, k3s из-за этого не остановится. Читает эти файлы
# только root: сам k3s, папка server 0700, а панель берёт CA кластера через sudo (deploy/panel/install.sh).
install -d -m 0755 /etc/systemd/system/k3s.service.d
cat > /etc/systemd/system/k3s.service.d/40-cis-tls.conf <<'EOF'
# deploy/k3s/cis.sh, CIS 1.1.20
[Service]
ExecStartPost=-/usr/bin/find /var/lib/rancher/k3s/server/tls -type f -name "*.crt" -exec chmod 0600 {} +
EOF
systemctl daemon-reload
# на работающем сервере сразу, без перезапуска k3s; на новой машине папки ещё нет
if [ -d "$S/tls" ]; then find "$S/tls" -type f -name '*.crt' -exec chmod 0600 {} +; fi
echo "CIS: $(hostname)"
EOS
