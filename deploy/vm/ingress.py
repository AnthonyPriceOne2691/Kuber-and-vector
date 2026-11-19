"""Фильтр входящих на сети машин (lima0): кто кому и на какой порт может постучаться.

Песочница (sandbox-boot.sh) закрывает выход машины, этот фильтр закрывает вход. Сеть машин
считаем открытым интернетом. Машине кластера пишут только соседи по кластеру и вход (machine-4),
каждый на свои порты. Входу пишут все, но только на 443; машины кластера ещё шлют события Falco
в панель. Всё остальное с lima0 отбрасывается молча, на счётчик «dropped».

Встроенную сеть Lima (eth0) фильтр не трогает: её видит только Lima на Маке (ssh и копирование
файлов), а машины стенда друг до друга по ней не достают.

Правила собираются по адресам машин; ставит их deploy/vm/ingress.sh (служба stand-ingress).
  python3 -I -B deploy/vm/ingress.py --role cluster --cluster 192.168.105.5,192.168.105.3,192.168.105.4 \
    --entry 192.168.105.2
"""
from __future__ import annotations

import argparse
import ipaddress
import sys

LAN = ipaddress.ip_network("192.168.105.0/24")  # socket_vmnet shared
HOST = ipaddress.ip_address("192.168.105.1")     # Мак в сети машин: «внешний мир», своих портов у него нет
IFACE = "lima0"
TABLE = "stand_ingress"

# Порты машины кластера. Трафик подов между машинами идёт внутри WireGuard (51820), поэтому
# Qdrant (6333, 6335) и DNS кластера на lima0 не видны вовсе.
FROM_PEERS_TCP = (2379, 2380, 6443, 10250)  # etcd (клиенты и пиры), API k3s, kubelet
FROM_PEERS_UDP = (51820,)                   # flannel wireguard-native
# вход: API k3s (frontend kube-api HAProxy для панели), node_exporter, поиск, шлюз Qdrant, метрики Falco
FROM_ENTRY_TCP = (6443, 9100, 30080, 30633, 30765)
# Порты входа.
PUBLIC_TCP = (443,)          # HAProxy с TLS, единственное, что открыто всем
FROM_CLUSTER_TCP = (8090,)   # события Falco (http_output) в панель

ROLES = ("cluster", "observer")


def address(text: str) -> str:
    """Адрес машины стенда: IPv4 из сети машин, не Мак и не служебный адрес сети."""
    try:
        ip = ipaddress.IPv4Address(text.strip())
    except ValueError:
        raise ValueError(f"не адрес IPv4: {text!r}") from None
    if ip not in LAN or ip in (LAN.network_address, LAN.broadcast_address):
        raise ValueError(f"{ip} не из сети машин {LAN}")
    if ip == HOST:
        raise ValueError(f"{ip} это Мак, а не машина стенда")
    return str(ip)


def addresses(cluster: list[str], entry: str) -> tuple[list[str], str]:
    nodes = [address(a) for a in cluster]
    gate = address(entry)
    if not nodes:
        raise ValueError("нет адресов машин кластера")
    if len(set(nodes)) != len(nodes):
        raise ValueError(f"адреса машин кластера повторяются: {nodes}")
    if gate in nodes:
        raise ValueError(f"адрес входа {gate} совпадает с машиной кластера")
    return nodes, gate


def _ports(ports: tuple[int, ...]) -> str:
    return "{ " + ", ".join(str(p) for p in ports) + " }"


def render(role: str, cluster: list[str], entry: str) -> str:
    """Набор правил nftables для машины с ролью role. Файл целиком атомарный: nft -f сначала
    удаляет старую таблицу и ставит новую одной транзакцией."""
    if role not in ROLES:
        raise ValueError(f"роль {role!r}: ждём одну из {', '.join(ROLES)}")
    nodes, gate = addresses(cluster, entry)
    if role == "cluster":
        allow = [
            "# соседи по кластеру: etcd, API k3s, kubelet, WireGuard",
            f"ip saddr @cluster tcp dport {_ports(FROM_PEERS_TCP)} accept",
            f"ip saddr @cluster udp dport {_ports(FROM_PEERS_UDP)} accept",
            "# вход (machine-4): API для панели, node_exporter, NodePort поиска, шлюза Qdrant и метрик Falco",
            f"ip saddr @entry tcp dport {_ports(FROM_ENTRY_TCP)} accept",
        ]
    else:
        allow = [
            "# вход стенда открыт всем, но только на 443 (HAProxy, TLS, ключ API)",
            f"tcp dport {_ports(PUBLIC_TCP)} accept",
            "# машины кластера: события Falco в панель",
            f"ip saddr @cluster tcp dport {_ports(FROM_CLUSTER_TCP)} accept",
        ]
    body = "\n".join(f"    {line}" for line in allow)
    return f"""# Фильтр входящих на {IFACE}, роль {role}. Собран deploy/vm/ingress.py, ставит deploy/vm/ingress.sh.
table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
  set cluster {{ type ipv4_addr; elements = {{ {", ".join(nodes)} }}; }}
  set entry {{ type ipv4_addr; elements = {{ {gate} }}; }}
  counter dropped {{ packets 0 bytes 0 }}
  chain prerouting {{
    # раньше DNAT kube-proxy (priority -100): NodePort здесь виден под своим номером
    type filter hook prerouting priority -150; policy accept;
    iifname != "{IFACE}" accept
    ct state established,related accept
    # адрес по DHCP раздаёт Мак (bootpd socket_vmnet)
    ip saddr {HOST} udp sport 67 udp dport 68 accept
{body}
    ip saddr @cluster icmp type echo-request accept
    ip saddr @entry icmp type echo-request accept
    counter name "dropped" drop
  }}
}}
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Правила nftables для входа машины стенда")
    ap.add_argument("--role", required=True, choices=ROLES)
    ap.add_argument("--cluster", required=True, help="адреса machine-1…3 через запятую")
    ap.add_argument("--entry", required=True, help="адрес machine-4")
    args = ap.parse_args(argv)
    try:
        sys.stdout.write(render(args.role, args.cluster.split(","), args.entry))
    except ValueError as e:
        print(f"ingress: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
