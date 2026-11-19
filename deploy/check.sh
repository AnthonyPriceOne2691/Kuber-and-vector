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
Q="What%20is%20the%20governing%20law%20of%20the%20agreement%3F"

echo "кластер"
check "три узла Ready" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl get nodes --no-headers | awk '\$2==\"Ready\"' | wc -l) -eq 3 ]"
check "etcd из трёх членов" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl get nodes -l node-role.kubernetes.io/etcd=true --no-headers | wc -l) -eq 3 ]"
check "три пира Qdrant, по одному на машину" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl -n qdrant get pods -o jsonpath='{range .items[?(@.status.phase==\"Running\")]}{.spec.nodeName}{\"\\n\"}{end}' | sort -u | wc -l) -eq 3 ]"
check "три пода поиска на трёх машинах" bash -c "[ \$(limactl shell machine-1 sudo k3s kubectl -n search get pods -o jsonpath='{range .items[?(@.status.phase==\"Running\")]}{.spec.nodeName}{\"\\n\"}{end}' | sort -u | wc -l) -eq 3 ]"
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
check "без ключа API 401" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' 'https://$ENTRY/search?q=$Q') = 401 ]"
check "чужой ключ API 401" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' -H 'X-API-Key: guess' 'https://$ENTRY/search?q=$Q') = 401 ]"
# последняя проверка входа: после неё адрес Мака около 10 с получает 429 на всё
last=""; for i in $(seq 1 15); do last=$(code -H "X-API-Key: guess-$i" "https://$ENTRY/search?q=$Q"); done
check "перебор ключей получает 429" [ "$last" = 429 ]

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
