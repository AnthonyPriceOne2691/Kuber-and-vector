#!/bin/bash
# Проверка стенда после подъёма: кластер, база, поиск через вход, песочница машин.
# Каждая проверка печатает ✓ или ✗; код выхода 1, если провалилась хоть одна.
#   deploy/check.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY=/opt/homebrew/bin/python3
CA="$ROOT/runs/stand-ca.pem"
KEYH="$ROOT/runs/entry-api-key.header"  # «X-API-Key: …», пишет deploy/observer/install.sh
pass=0; fail=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "  ✓ $name"; pass=$((pass+1)); else echo "  ✗ $name"; fail=$((fail+1)); fi; }
not() { ! "$@"; }
kc() { limactl shell machine-1 sudo k3s kubectl "$@"; }
ip_of() { limactl shell "$1" bash -c "ip -4 -o addr show lima0 | awk '{print \$4}' | cut -d/ -f1"; }
ENTRY="$(ip_of machine-4)"
M1="$(ip_of machine-1)"
M2="$(ip_of machine-2)"
Q="What%20is%20the%20governing%20law%20of%20the%20agreement%3F"
reach() { limactl shell "$1" timeout 3 bash -c "</dev/tcp/$2/$3"; }  # с машины $1 до $2:$3
mac_reach() { nc -z -G 3 -w 3 "$1" "$2"; }                           # с Мака до $1:$2

echo "кластер"
check "три узла Ready" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl get nodes --no-headers | awk '\$2==\"Ready\"' | wc -l) -eq 3 ]"
check "etcd из трёх членов" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl get nodes -l node-role.kubernetes.io/etcd=true --no-headers | wc -l) -eq 3 ]"
check "три пира Qdrant, по одному на машину" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl -n qdrant get pods -o jsonpath='{range .items[?(@.status.phase==\"Running\")]}{.spec.nodeName}{\"\\n\"}{end}' | sort -u | wc -l) -eq 3 ]"
check "три пода поиска на трёх машинах" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl -n search get pods -o jsonpath='{range .items[?(@.status.phase==\"Running\")]}{.spec.nodeName}{\"\\n\"}{end}' | sort -u | wc -l) -eq 3 ]"
psa_rejects() {  # привилегированный под в search сервер отклоняет ещё до запуска (dry-run)
  kc -n search run psa-probe --image=localhost/search:stage2 --restart=Never --dry-run=server \
    --overrides='{"apiVersion":"v1","spec":{"containers":[{"name":"psa-probe","image":"localhost/search:stage2","securityContext":{"privileged":true}}]}}' \
    2>&1 | grep -q "violates PodSecurity"
}
check "секреты в etcd зашифрованы" bash -c "limactl shell machine-1 sudo k3s secrets-encrypt status | grep -q 'Encryption Status: Enabled'"
check "журнал аудита API пишется" limactl shell machine-1 sudo test -s /var/lib/rancher/k3s/server/logs/audit.log
check "привилегированный под отклоняется (Pod Security restricted)" psa_rejects
image_probe() {  # $1 образ: под по правилам restricted, чтобы отказать могла только политика образов
  kc -n search run image-probe --image="$1" --restart=Never --dry-run=server --overrides="{\"apiVersion\":\"v1\",\"spec\":{\"securityContext\":{\"runAsNonRoot\":true,\"runAsUser\":10001,\"seccompProfile\":{\"type\":\"RuntimeDefault\"}},\"containers\":[{\"name\":\"image-probe\",\"image\":\"$1\",\"imagePullPolicy\":\"Never\",\"securityContext\":{\"allowPrivilegeEscalation\":false,\"capabilities\":{\"drop\":[\"ALL\"]}}}]}}" 2>&1
}
foreign_rejected() { image_probe docker.io/library/busybox:1.37 | grep -q "не из комплекта"; }
own_admitted() { image_probe localhost/search:stage2 | grep -q "server dry run"; }
check "политика образов в режиме Deny" bash -c "limactl shell machine-1 sudo k3s kubectl get validatingadmissionpolicybinding stand-images -o jsonpath='{.spec.validationActions}' | grep -q Deny"
check "чужой образ отклоняется (только образы из комплекта)" foreign_rejected
check "образ из комплекта проходит" own_admitted
limactl copy "$HERE/k3s/dns_probe.py" machine-1:/tmp/dns_probe.py >/dev/null 2>&1
check "DNS кластера: в Corefile нет forward наружу" bash -c "! limactl shell machine-1 sudo k3s kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' | grep -q forward"
check "DNS кластера: имя кластера отвечает" limactl shell machine-1 python3 -I -B /tmp/dns_probe.py kubernetes.default.svc.cluster.local
check "DNS кластера: чужое имя сразу получает отказ" limactl shell machine-1 python3 -I -B /tmp/dns_probe.py --refused example.com
check "Falco на трёх машинах" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl -n falco get pods -o jsonpath='{range .items[?(@.status.phase==\"Running\")]}{.spec.nodeName}{\"\\n\"}{end}' | sort -u | wc -l) -eq 3 ]"

echo "поиск через вход https://$ENTRY"
check "TLS проверяется по УЦ стенда, ответ с адресами" bash -c "curl -sf --cacert '$CA' -H @'$KEYH' 'https://$ENTRY/search?q=$Q&k=3' | $PY -I -c 'import json,sys; r=json.load(sys.stdin)[\"results\"]; assert r and all(\"span\" in x for x in r)'"
check "без УЦ стенда curl отказывается (сертификат не публичный)" not curl -sf "https://$ENTRY/healthz"
check "POST запрещён (405)" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' -X POST 'https://$ENTRY/search') = 405 ]"
check "чужой путь закрыт (404)" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' 'https://$ENTRY/docs') = 404 ]"
check "порт 80 на входе закрыт" not curl -s -m 3 "http://$ENTRY/"
if [ "${CHECK_ANSWER:-0}" = 1 ]; then  # грузит qwen3:8b на Маке (около 6 ГБ, оценка), поэтому по запросу
  check "ответ модели через вход: статус и ссылки" bash -c "curl -sf -m 150 --cacert '$CA' -H @'$KEYH' 'https://$ENTRY/answer?q=$Q&k=5' | $PY -I -c 'import json,sys; r=json.load(sys.stdin); assert r[\"status\"] in (\"answered\",\"refused\",\"unsupported\"); assert r[\"status\"] != \"answered\" or any(c[\"verified\"] for c in r[\"citations\"])'"
fi
served=$(for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  curl -s --cacert "$CA" -H @"$KEYH" "https://$ENTRY/search?q=$Q&k=1" | $PY -I -c 'import json,sys; print(json.load(sys.stdin)["served_by"]["node"])' 2>/dev/null
done | sort | uniq -c | tr '\n' ' ')
echo "    кто ответил на 12 запросов: $served"
code() { curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" "$@"; }

echo "мониторинг (machine-4) и шлюз Qdrant"
check "Prometheus: все цели отвечают" bash -c "limactl shell machine-4 curl -sf 'http://127.0.0.1:9090/api/v1/query?query=min(up)' | $PY -I -c 'import json,sys; r=json.load(sys.stdin)[\"data\"][\"result\"]; assert r and r[0][\"value\"][1]==\"1\"'"
check "Grafana отвечает на localhost machine-4" limactl shell machine-4 curl -sf -o /dev/null http://127.0.0.1:3000/api/health
check "Grafana не видна из сети машин" not limactl shell machine-1 curl -s -m 3 -o /dev/null "http://$ENTRY:3000/"
check "node_exporter machine-1 закрыт для machine-2" not limactl shell machine-2 curl -s -m 3 -o /dev/null "http://$M1:9100/metrics"
check "метрики через вход не видны (404)" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' 'https://$ENTRY/metrics') = 404 ]"
check "шлюз Qdrant отдаёт machine-4 состояние кластера" bash -c "limactl shell machine-4 curl -sf -m 3 http://$M1:30633/cluster | grep -q peers"
check "шлюз Qdrant не отдаёт точки (404)" bash -c "[ \$(limactl shell machine-4 curl -s -m 3 -o /dev/null -w '%{http_code}' http://$M1:30633/collections/contracts_rf2/points/scroll) = 404 ]"
check "шлюз Qdrant закрыт для machine-2" not limactl shell machine-2 curl -s -m 3 -o /dev/null "http://$M1:30633/cluster"
check "без ключа API 401" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' 'https://$ENTRY/search?q=$Q') = 401 ]"
check "чужой ключ API 401" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' -H 'X-API-Key: guess' 'https://$ENTRY/search?q=$Q') = 401 ]"
# последняя проверка входа: после неё адрес Мака около 10 с получает 429 на всё
last=""; for i in $(seq 1 15); do last=$(code -H "X-API-Key: guess-$i" "https://$ENTRY/search?q=$Q"); done
check "перебор ключей получает 429" [ "$last" = 429 ]

echo "файрвол на входе машин (deploy/vm/ingress.sh)"
check "фильтр стоит на всех четырёх машинах" bash -c 'for m in machine-1 machine-2 machine-3 machine-4; do limactl shell $m sudo nft list counter inet stand_ingress dropped >/dev/null || exit 1; done'
check "сосед по кластеру достаёт до etcd (machine-2 → machine-1:2379)" reach machine-2 "$M1" 2379
check "вход достаёт до API k3s (machine-4 → machine-1:6443)" reach machine-4 "$M1" 6443
check "вход не достаёт до etcd (machine-4 → machine-1:2379)" not reach machine-4 "$M1" 2379
check "вход не достаёт до kubelet (machine-4 → machine-1:10250)" not reach machine-4 "$M1" 10250
check "ssh между машинами по сети машин закрыт (machine-1 → machine-2:22)" not reach machine-1 "$M2" 22
check "Мак не достаёт до API k3s (machine-1:6443)" not mac_reach "$M1" 6443
check "Мак не достаёт до ssh входа по сети машин (machine-4:22)" not mac_reach "$ENTRY" 22

echo "песочница машин (на machine-1)"
check "интернета нет" not limactl shell machine-1 curl -s -m 5 -o /dev/null https://example.com
check "localhost Мака закрыт (шлюз Lima 192.168.5.2; гипервизор держит и без файрвола ВМ, замер 18.11)" not limactl shell machine-1 curl -s -m 3 -o /dev/null http://192.168.5.2:11434/
check "Мак по сети машин закрыт, кроме фильтра (AirPlay 7000)" not limactl shell machine-1 curl -s -m 3 -o /dev/null http://192.168.105.1:7000/
check "фильтр пускает только векторизацию (/api/tags → 403)" bash -c "[ \$(limactl shell machine-1 curl -s -m 5 -o /dev/null -w '%{http_code}' http://192.168.105.1:11434/api/tags) = 403 ]"
check "фильтр отдаёт векторы bge-m3" bash -c "limactl shell machine-1 curl -sf -m 10 http://192.168.105.1:11434/api/embed -d '{\"model\":\"bge-m3\",\"input\":\"test\"}' | grep -q embeddings"
check "фильтр не даёт чат с чужой моделью (403)" bash -c "[ \$(limactl shell machine-1 curl -s -m 5 -o /dev/null -w '%{http_code}' http://192.168.105.1:11434/api/chat -d '{\"model\":\"qwen3:14b\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}') = 403 ]"
check "фильтр не даёт держать модель в памяти (keep_alive → 400)" bash -c "[ \$(limactl shell machine-1 curl -s -m 5 -o /dev/null -w '%{http_code}' http://192.168.105.1:11434/api/chat -d '{\"model\":\"qwen3:8b\",\"keep_alive\":-1,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}') = 400 ]"
check "IPv6 выключен" bash -c "[ \$(limactl shell machine-1 cat /proc/sys/net/ipv6/conf/all/disable_ipv6) = 1 ]"
check "папок Мака в машине нет" not limactl shell machine-1 test -d /Users

echo; echo "итого: $pass ✓, $fail ✗"
[ "$fail" -eq 0 ]
