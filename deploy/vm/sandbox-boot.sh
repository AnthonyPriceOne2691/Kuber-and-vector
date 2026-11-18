#!/bin/sh
# Сетевая песочница машины стенда.
# Lima запускает этот скрипт в режиме boot на каждом старте, до поднятия сети.
# Роль приходит параметром ROLE (переменная PARAM_ROLE): cluster | observer | builder.
# Пустая или неизвестная роль даёт самый строгий режим: без Ollama и без интернета.
#
# Что разрешено:
#   все роли   DHCP; DNS на шлюз; соседние ВМ (user-v2) и сети подов/сервисов k3s
#   cluster    плюс Ollama на Маке (tcp 11434), это векторизатор вопросов
#   builder    плюс интернет: только одноразовая сборочная ВМ качает комплект
# Всё остальное к Маку (localhost хоста: Claude Code, Cursor, Postgres и прочее) закрыто.
set -eu

HOST=192.168.104.2        # шлюз user-v2: это сам Мак, его DNS и выход наружу
LAN=192.168.104.0/24      # машины стенда между собой
PODS=10.42.0.0/16         # поды k3s (значение по умолчанию)
SERVICES=10.43.0.0/16     # сервисы k3s (значение по умолчанию)
OLLAMA_PORT=11434

# Стенд работает только на IPv4. IPv6 выключаем: меньше поверхность атаки, и DNS
# с AAAA-записями не заставляет программы ждать таймаута по несуществующему маршруту.
sysctl -qw net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1

ROLE="${PARAM_ROLE:-strict}"
host_extra=""
tail_rule="counter drop"
case "$ROLE" in
  cluster)  host_extra="ip daddr $HOST tcp dport $OLLAMA_PORT accept" ;;
  observer) ;;
  builder)  tail_rule="accept" ;;
  *)        ROLE="strict" ;;
esac

nft -f - <<EOF
table inet sandbox
delete table inet sandbox
table inet sandbox {
  chain egress {
    udp sport 68 udp dport 67 accept
    ip daddr $HOST udp dport 53 accept
    ip daddr $HOST tcp dport 53 accept
    $host_extra
    ip daddr $HOST counter drop
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
