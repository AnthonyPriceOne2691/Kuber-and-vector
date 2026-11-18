#!/bin/sh
# Сетевая песочница машины стенда, второй слой после гипервизора.
# Lima запускает этот скрипт в режиме boot на каждом старте, до поднятия сети.
# Роль приходит параметром ROLE (переменная PARAM_ROLE): cluster | observer | builder.
# Пустая или неизвестная роль даёт самый строгий режим: без Ollama и без интернета.
#
# Сети машины:
#   lima0  socket_vmnet shared, 192.168.105.0/24: машины между собой; Мак = 192.168.105.1
#   eth0   встроенная сеть Lima, 192.168.5.0/24: шлюз и DNS 192.168.5.2 (замер 18.11: .3 не отвечает);
#          доступ к localhost Мака здесь закрыт гипервизором (networkOpts.hostLoopback: false)
#
# Что разрешено:
#   все роли   DHCP; соседние машины и сети подов/сервисов k3s
#   cluster    плюс фильтр Ollama на Маке (192.168.105.1:11434, только векторизация bge-m3)
#   builder    плюс DNS и интернет: только одноразовая сборочная ВМ качает комплект
# Всё остальное к Маку и наружу закрыто. DNS машинам стенда не нужен (k3s, Qdrant и поиск ходят
# по IP), а открытый DNS это канал утечки (данные прячут в DNS-запросы), поэтому он закрыт.
set -eu

# Стенд работает только на IPv4. IPv6 выключаем: меньше поверхность атаки, и DNS
# с AAAA-записями не заставляет программы ждать таймаута по несуществующему маршруту.
sysctl -qw net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1

HOST=192.168.105.1        # Мак в сети машин (мост socket_vmnet)
LAN=192.168.105.0/24      # машины стенда между собой
USERNET=192.168.5.0/24    # встроенная сеть Lima
DNS=192.168.5.2           # DNS Lima на встроенной сети
PODS=10.42.0.0/16         # поды k3s (значение по умолчанию)
SERVICES=10.43.0.0/16     # сервисы k3s (значение по умолчанию)
OLLAMA_PORT=11434

ROLE="${PARAM_ROLE:-strict}"
host_extra=""
dns_rule=""
tail_rule="counter drop"
case "$ROLE" in
  cluster)  host_extra="ip daddr $HOST tcp dport $OLLAMA_PORT accept" ;;
  observer) ;;
  builder)  dns_rule="ip daddr $DNS meta l4proto { udp, tcp } th dport 53 accept"; tail_rule="accept" ;;
  *)        ROLE="strict" ;;
esac

nft -f - <<EOF
table inet sandbox
delete table inet sandbox
table inet sandbox {
  chain egress {
    udp sport 68 udp dport 67 accept
    $dns_rule
    $host_extra
    ip daddr $HOST counter drop
    ip daddr $USERNET counter drop
    ip daddr { $LAN, $PODS, $SERVICES } accept
    $tail_rule
  }
  chain output {
    type filter hook output priority filter; policy accept;
    oifname "lo" accept
    ct state established,related accept
    jump egress
  }
  chain forward {
    type filter hook forward priority filter; policy accept;
    ct state established,related accept
    jump egress
  }
}
EOF

echo "$ROLE" > /run/sandbox-role
echo "sandbox: role=$ROLE"
