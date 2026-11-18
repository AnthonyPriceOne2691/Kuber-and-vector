"""Mock стенда: сценарий «machine-2 выключена» по кругу, без кластера.

Mock подменяет только транспорт: отдаёт JSON как `kubectl get nodes,pods -A -o json` и как
GET /cluster, /collections/{c}/cluster у Qdrant, а синтетические запросы пишет в тот же Stats,
что и настоящий генератор. Дальше работает обычный код панели: разбор ответов, карта шардов,
события, хронология сбоя. Так mock проверяет панель целиком, а не рисует картинку.

Сценарий, секунды цикла (цикл 165 с, потом повтор):
    0–20    норма: 3 узла Ready, 12/12 реплик Active, 25 req/s
    20      machine-2 выключена (как limactl stop --force): попытки к ней висят до таймаута 2 с,
            повтор на другую цель спасает — ошибок у пользователя нет, p99 ~2 с
    23      реплики на machine-2 — Dead (первая неудачная запись)
    27      балансировщик убрал machine-2; до NotReady p99 ~0,3 с: часть трафика ещё идёт
            на её поды через Service
    64      узел NotReady (через 44 с после потери), поды на нём — not ready
    74      выселение пода search (tolerationSeconds 10): новый под на machine-3, Ready к 81
    100     machine-2 включена (limactl start)
    116     узел Ready, под qdrant-1 перезапускается; 120 — пир отвечает
    121–135 реплики догоняют: 4 переноса шардов по 3,5 с — и снова 12/12 Active
    140     только в mock: под search возвращается на machine-2, чтобы круг начинался так же

Числа правдоподобны для этого железа, но выдуманы: на панели в mock-режиме всегда плашка
SIMULATED DATA.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import time
from datetime import datetime, timezone

import loadgen

CYCLE = 165.0
T_LOSS = 20.0
T_DEAD = 23.0
T_LB = 27.0
T_NOTREADY = 64.0
T_EVICT = 74.0
T_SCHED = 74.6
T_RUN = 77.0
T_READY = 81.0
T_VM_START = 100.0
T_NODE_READY = 116.0
T_OLD_GONE = 117.0
T_QPOD_RUN = 118.0
T_PEER_UP = 120.0
T_QPOD_READY = 123.0
T_RECOVER0 = 121.0
RECOVER_EACH = 3.5
T_REBAL = 140.0

MACHINES = ("machine-1", "machine-2", "machine-3")
OBSERVER = "machine-4"
VICTIM = "machine-2"
IPS = {"machine-1": "192.168.104.3", "machine-2": "192.168.104.4", "machine-3": "192.168.104.5",
       "machine-4": "192.168.104.6"}
POD_NET = {"machine-1": "10.42.0", "machine-2": "10.42.1", "machine-3": "10.42.2"}
# номера пиров Qdrant — случайные u64, как настоящие (два больше 2^53: проверка для JS)
PEERS = {"machine-1": 2738916503459183011, "machine-2": 11635047302187392211,
         "machine-3": 6029477265530419423}
QPODS = {"machine-1": "qdrant-0", "machine-2": "qdrant-1", "machine-3": "qdrant-2"}
LEASE_OFFSET = {"machine-1": 2.0, "machine-2": 5.0, "machine-3": 8.0}
COLLECTION = "lbr_mini_naive500_bge_m3"
MODEL = "bge-m3"
SHARDS = 6
POINTS = 17403
RATE = 25.0
TIMEOUT_MS = 2000.0
SEARCH_RS = "search-7f9c6d8b5"
# шард s лежит на двух машинах подряд: 0 -> m1,m2; 1 -> m2,m3; 2 -> m3,m1; …
PLACEMENT = {s: (MACHINES[s % 3], MACHINES[(s + 1) % 3]) for s in range(SHARDS)}
VICTIM_SHARDS = [s for s in range(SHARDS) if VICTIM in PLACEMENT[s]]
T_RECOVERED = T_RECOVER0 + RECOVER_EACH * len(VICTIM_SHARDS)


def iso(ts: float, frac: bool = False) -> str:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ" if frac else "%Y-%m-%dT%H:%M:%SZ")


def suffix(seed: str) -> str:
    """Хвост имени пода как у Kubernetes (5 знаков из его алфавита), детерминированный."""
    alphabet = "bcdfghjklmnpqrstvwxz2456789"
    h = hashlib.sha256(seed.encode()).digest()
    return "".join(alphabet[b % len(alphabet)] for b in h[:5])


def load_profile(ct: float) -> dict:
    """Параметры синтетической нагрузки в секунду цикла ct: темп, медиана и разброс задержки,
    доля упавших первых попыток (fail) и медленных ответов (slow)."""
    p = {"rate": RATE, "median": 23.0, "sigma": 0.26, "fail": 0.0, "slow": 0.0, "slow_ms": 320.0}
    if T_LOSS <= ct < T_LOSS + 3:
        p.update(fail=0.33, rate=RATE * 0.8)  # треть запросов шла на machine-2 и повисла
    elif T_LOSS + 3 <= ct < T_LB:
        k = (ct - T_LOSS - 3) / (T_LB - T_LOSS - 3)
        p.update(fail=0.33 + (0.02 - 0.33) * k, rate=RATE * 1.1)  # проверки HAProxy отсекают её
    elif T_LB <= ct < T_NOTREADY + 2:
        p.update(fail=0.004, slow=0.025, median=25.0)  # Service ещё ведёт часть трафика на её поды
    elif T_NOTREADY + 2 <= ct < T_NODE_READY:
        p.update(median=25.0, sigma=0.28)  # работают две машины
    elif T_NODE_READY <= ct < T_RECOVERED + 2:
        p.update(median=27.0, sigma=0.36)  # переносы шардов делят процессор
    return p


class MockScenario:
    """Мир стенда как функция времени: узлы, поды, аренды, пиры и реплики Qdrant."""

    def __init__(self, origin: float) -> None:
        self.origin = origin

    def at(self, now: float) -> tuple[int, float]:
        rel = now - self.origin
        cycle = math.floor(rel / CYCLE)
        return cycle, rel - cycle * CYCLE

    def abs(self, cycle: int, ct: float) -> float:
        return self.origin + cycle * CYCLE + ct

    # --- состояние machine-2 ---------------------------------------------------------------
    @staticmethod
    def vm_up(ct: float) -> bool:
        return not (T_LOSS <= ct < T_VM_START)

    @staticmethod
    def kubelet_up(ct: float) -> bool:
        return not (T_LOSS <= ct < T_NODE_READY - 1)

    @staticmethod
    def peer_up(ct: float) -> bool:
        return not (T_LOSS <= ct < T_PEER_UP)

    def replica_state(self, shard: int, machine: str, ct: float) -> str:
        if machine != VICTIM or ct < T_DEAD:
            return "Active"
        start = T_RECOVER0 + VICTIM_SHARDS.index(shard) * RECOVER_EACH
        if ct < start:
            return "Dead"
        if ct < start + RECOVER_EACH:
            return "Partial"
        return "Active"

    def transfers(self, ct: float) -> list[dict]:
        out = []
        for i, s in enumerate(VICTIM_SHARDS):
            start = T_RECOVER0 + i * RECOVER_EACH
            if start <= ct < start + RECOVER_EACH:
                src = next(m for m in PLACEMENT[s] if m != VICTIM)
                total = POINTS // SHARDS
                done = int(total * (ct - start) / RECOVER_EACH)
                out.append({"shard_id": s, "from": PEERS[src], "to": PEERS[VICTIM], "sync": True,
                            "method": "stream_records",
                            "comment": f"Transferring records ({done}/{total}), started {ct - start:.0f}s ago, "
                                       f"ETA: {start + RECOVER_EACH - ct:.2f}s"})
        return out

    # --- Kubernetes ------------------------------------------------------------------------
    def node(self, name: str, cycle: int, ct: float) -> dict:
        ready, since = "True", self.origin - 86400
        if name == VICTIM:
            if ct < T_NOTREADY:
                since = self.abs(cycle - 1, T_NODE_READY)
            elif ct < T_NODE_READY:
                ready, since = "Unknown", self.abs(cycle, T_NOTREADY)
            else:
                since = self.abs(cycle, T_NODE_READY)
        cond = {"type": "Ready", "status": ready, "lastTransitionTime": iso(since),
                "lastHeartbeatTime": iso(since), "reason": "KubeletReady",
                "message": "kubelet is posting ready status"}
        taints = []
        if ready != "True":
            cond.update(reason="NodeStatusUnknown", message="Kubelet stopped posting node status.")
            taints = [{"key": "node.kubernetes.io/unreachable", "effect": "NoSchedule"},
                      {"key": "node.kubernetes.io/unreachable", "effect": "NoExecute"}]
        return {"apiVersion": "v1", "kind": "Node",
                "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name,
                                                      "node-role.kubernetes.io/control-plane": "true",
                                                      "node-role.kubernetes.io/etcd": "true",
                                                      "node-role.kubernetes.io/master": "true"}},
                "spec": {"taints": taints} if taints else {},
                "status": {"conditions": [cond],
                           "addresses": [{"type": "InternalIP", "address": IPS[name]},
                                         {"type": "Hostname", "address": name}],
                           "nodeInfo": {"kubeletVersion": "v1.37.2+k3s1"}}}

    @staticmethod
    def pod(ns: str, name: str, node: str | None, labels: dict, owner: tuple[str, str], created: float,
            phase: str = "Running", ready: bool = True, restarts: int = 0, waiting: str | None = None,
            deleting: float | None = None, ip: str | None = None) -> dict:
        md = {"name": name, "namespace": ns, "labels": labels, "creationTimestamp": iso(created),
              "ownerReferences": [{"apiVersion": "apps/v1", "kind": owner[0], "name": owner[1],
                                   "controller": True}]}
        if deleting is not None:
            md["deletionTimestamp"] = iso(deleting)
        state = {"waiting": {"reason": waiting}} if waiting else {"running": {"startedAt": iso(created)}}
        return {"apiVersion": "v1", "kind": "Pod", "metadata": md,
                "spec": {"nodeName": node} if node else {},
                "status": {"phase": phase, "podIP": ip if node else None,
                           "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                           "containerStatuses": [{"name": labels.get("app") or labels.get(
                               "app.kubernetes.io/name") or "main", "ready": ready,
                               "restartCount": restarts, "state": state}]}}

    def _search_pod(self, name: str, node: str | None, created: float, stage: str, ip: str,
                    deleting: float | None = None) -> dict:
        kw = {"Pending": dict(phase="Pending", ready=False),
              "Creating": dict(phase="Pending", ready=False, waiting="ContainerCreating"),
              "Starting": dict(phase="Running", ready=False),
              "Ready": dict(phase="Running", ready=True),
              "NotReady": dict(phase="Running", ready=False),
              "Terminating": dict(phase="Running", ready=False)}[stage]
        return self.pod("search", name, node, {"app": "search", "pod-template-hash": SEARCH_RS[7:]},
                        ("ReplicaSet", SEARCH_RS), created, ip=ip, deleting=deleting, **kw)

    def pods(self, cycle: int, ct: float) -> list[dict]:
        long_ago = self.origin - 7200
        out = [
            self._search_pod(f"{SEARCH_RS}-{suffix('search-a')}", "machine-1", long_ago, "Ready", "10.42.0.11"),
            self.pod("qdrant", "qdrant-0", "machine-1", {"app.kubernetes.io/name": "qdrant"},
                     ("StatefulSet", "qdrant"), long_ago, ip="10.42.0.12"),
            self.pod("qdrant", "qdrant-2", "machine-3", {"app.kubernetes.io/name": "qdrant"},
                     ("StatefulSet", "qdrant"), long_ago, ip="10.42.2.12"),
            self.pod("kube-system", f"coredns-6799fbcd5-{suffix('dns')}", "machine-1", {"k8s-app": "kube-dns"},
                     ("ReplicaSet", "coredns-6799fbcd5"), long_ago, ip="10.42.0.2"),
            self.pod("kube-system", f"traefik-7d5c9b9b6-{suffix('tr1')}", "machine-1",
                     {"app.kubernetes.io/name": "traefik"}, ("ReplicaSet", "traefik-7d5c9b9b6"), long_ago,
                     ip="10.42.0.3"),
            self.pod("kube-system", f"traefik-7d5c9b9b6-{suffix('tr2')}", "machine-3",
                     {"app.kubernetes.io/name": "traefik"}, ("ReplicaSet", "traefik-7d5c9b9b6"), long_ago,
                     ip="10.42.2.3"),
            self.pod("kube-system", f"metrics-server-54fd9b65b-{suffix('ms')}", "machine-3",
                     {"k8s-app": "metrics-server"}, ("ReplicaSet", "metrics-server-54fd9b65b"), long_ago,
                     ip="10.42.2.4"),
            self.pod("kube-system", f"local-path-provisioner-6795b5f9d8-{suffix('lp')}", "machine-3",
                     {"app": "local-path-provisioner"}, ("ReplicaSet", "local-path-provisioner-6795b5f9d8"),
                     long_ago, ip="10.42.2.5"),
        ]
        # под search на machine-2: создан «ребалансировкой» прошлого круга
        b_name = f"{SEARCH_RS}-{suffix(f'search-b{cycle}')}"
        b_created = self.abs(cycle - 1, T_REBAL)
        if ct < T_NOTREADY:
            out.append(self._search_pod(b_name, VICTIM, b_created, "Ready", "10.42.1.21"))
        elif ct < T_EVICT:
            out.append(self._search_pod(b_name, VICTIM, b_created, "NotReady", "10.42.1.21"))
        elif ct < T_OLD_GONE:
            out.append(self._search_pod(b_name, VICTIM, b_created, "Terminating", "10.42.1.21",
                                        deleting=self.abs(cycle, T_EVICT)))
        # замена на machine-3 после выселения
        c_name = f"{SEARCH_RS}-{suffix(f'search-c{cycle}')}"
        c_created = self.abs(cycle, T_EVICT)
        if T_EVICT <= ct < T_REBAL + 9:
            if ct < T_SCHED:
                st, node = "Pending", None
            elif ct < T_RUN:
                st, node = "Creating", "machine-3"
            elif ct < T_READY:
                st, node = "Starting", "machine-3"
            elif ct < T_REBAL + 7:
                st, node = "Ready", "machine-3"
            else:
                st, node = "Terminating", "machine-3"
            out.append(self._search_pod(c_name, node, c_created, st, "10.42.2.31",
                                        deleting=self.abs(cycle, T_REBAL + 7) if st == "Terminating" else None))
        # «ребалансировка»: новый под на machine-2 для следующего круга
        if ct >= T_REBAL:
            n_name = f"{SEARCH_RS}-{suffix(f'search-b{cycle + 1}')}"
            d = ct - T_REBAL
            st, node = (("Pending", None) if d < 0.5 else ("Creating", VICTIM) if d < 3 else
                        ("Starting", VICTIM) if d < 6 else ("Ready", VICTIM))
            out.append(self._search_pod(n_name, node, self.abs(cycle, T_REBAL), st, "10.42.1.21"))
        # qdrant-1 на machine-2: StatefulSet с локальным томом — переехать не может, ждёт машину
        restarts = cycle + 3 + (1 if ct >= T_QPOD_RUN else 0)
        if ct < T_NOTREADY or ct >= T_QPOD_READY:
            kw = dict(ready=True)
        elif ct < T_NODE_READY or ct >= T_QPOD_RUN:
            kw = dict(ready=False)
        else:
            kw = dict(ready=False, waiting="ContainerCreating")
        out.append(self.pod("qdrant", "qdrant-1", VICTIM, {"app.kubernetes.io/name": "qdrant"},
                            ("StatefulSet", "qdrant"), long_ago, restarts=restarts, ip="10.42.1.12", **kw))
        return out

    def last_renew(self, name: str, now: float) -> float:
        """Последнее продление аренды kubelet: раз в 10 с, у выключенной машины — замирает."""
        off = LEASE_OFFSET[name]
        t = self.origin + off + 10 * math.floor((now - self.origin - off) / 10)
        for _ in range(40):
            if name != VICTIM or self.kubelet_up(self.at(t)[1]):
                return t
            t -= 10
        return t

    def kubectl_doc(self, now: float) -> dict:
        cycle, ct = self.at(now)
        items = [self.node(m, cycle, ct) for m in MACHINES] + self.pods(cycle, ct)
        return {"apiVersion": "v1", "kind": "List", "items": items, "metadata": {"resourceVersion": ""}}

    def leases_doc(self, now: float) -> dict:
        return {"apiVersion": "v1", "kind": "List", "items": [
            {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
             "metadata": {"name": m, "namespace": "kube-node-lease"},
             "spec": {"holderIdentity": m, "leaseDurationSeconds": 40,
                      "renewTime": iso(self.last_renew(m, now), frac=True)}} for m in MACHINES]}

    # --- Qdrant ----------------------------------------------------------------------------
    def cluster_doc(self, machine: str, now: float) -> dict:
        _, ct = self.at(now)
        failures = {}
        if not self.peer_up(ct):
            failures[f"http://{QPODS[VICTIM]}.qdrant-headless:6335/"] = {
                "count": int((ct - T_LOSS) * 10) + 1,
                "latest_error": "Failed to send message: deadline has elapsed",
                "latest_error_timestamp": iso(now - 0.3, frac=True)}
        return {"result": {
            "status": "enabled", "peer_id": PEERS[machine],
            "peers": {str(PEERS[m]): {"uri": f"http://{QPODS[m]}.qdrant-headless:6335/"} for m in MACHINES},
            "raft_info": {"term": 7, "commit": 4000 + int(now - self.origin), "pending_operations": 0,
                          "leader": PEERS["machine-1"],
                          "role": "Leader" if machine == "machine-1" else "Follower", "is_voter": True},
            "consensus_thread_status": {"consensus_thread_status": "working", "last_update": iso(now, frac=True)},
            "message_send_failures": failures}, "status": "ok", "time": 0.000012}

    def collection_cluster_doc(self, machine: str, now: float) -> dict:
        _, ct = self.at(now)
        local, remote = [], []
        for s in range(SHARDS):
            for m in PLACEMENT[s]:
                state = self.replica_state(s, m, ct)
                if m == machine:
                    local.append({"shard_id": s, "points_count": POINTS // SHARDS + s, "state": state})
                else:
                    remote.append({"shard_id": s, "peer_id": PEERS[m], "state": state})
        return {"result": {"peer_id": PEERS[machine], "shard_count": SHARDS, "local_shards": local,
                           "remote_shards": remote, "shard_transfers": self.transfers(ct)},
                "status": "ok", "time": 0.00021}

    def collection_info_doc(self, now: float) -> dict:
        _, ct = self.at(now)
        degraded = T_DEAD <= ct < T_RECOVERED
        return {"result": {"status": "yellow" if degraded else "green", "points_count": POINTS,
                           "config": {"params": {"shard_number": SHARDS, "replication_factor": 2}}},
                "status": "ok", "time": 0.0001}


class MockSource:
    """Источник mock: владеет сценарием и выдаёт подменённый транспорт остальным источникам.
    replay() прогоняет панель по прошлым секундам, чтобы при старте уже были график и лента."""

    name = "mock"
    collection = COLLECTION
    cycle = CYCLE

    def __init__(self, start_offset: float = 0.0, seed: int = 7, clock=time.time) -> None:
        self.clock = clock
        t0 = clock()
        self.start_offset = start_offset
        self.scenario = MockScenario(origin=t0 - start_offset)
        # история до старта: не больше 3 минут и не глубже «ребалансировки» прошлого круга
        self.backfill_s = min(180.0, start_offset + CYCLE - T_REBAL - 10.0)
        self.stats = loadgen.Stats(seed=seed)
        self._rng = random.Random(seed)
        self._now: float | None = None
        self._gen_t = t0
        self._acc_fail = self._acc_slow = self._acc_conn = 0.0
        self._last_tick: float | None = None
        self._lat_sum = 0.0
        self._lat_n = 0

    def now(self) -> float:
        return self._now if self._now is not None else self.clock()

    # --- транспорт для KubeSource ----------------------------------------------------------
    def kubectl(self, cmd: list[str], timeout: float) -> str:
        now = self.now()
        doc = self.scenario.leases_doc(now) if "leases" in cmd else self.scenario.kubectl_doc(now)
        return json.dumps(doc)

    # --- транспорт для QdrantSource --------------------------------------------------------
    @staticmethod
    def qdrant_targets() -> list[tuple[str, str]]:
        return [(m, f"http://{m}.mock:6333") for m in MACHINES]

    def qdrant_fetch(self, url: str, path: str) -> dict:
        now = self.now()
        machine = next((m for m in MACHINES if m in url), None)
        if machine is None:
            raise ConnectionRefusedError("unknown mock peer")
        _, ct = self.scenario.at(now)
        if machine == VICTIM and not self.scenario.peer_up(ct):
            if not self.scenario.vm_up(ct):
                raise TimeoutError("timed out")  # машина выключена: пакеты в никуда
            raise ConnectionRefusedError("connection refused")  # ВМ загружается, Qdrant ещё нет
        if path == "/cluster":
            return self.scenario.cluster_doc(machine, now)
        if path == "/aliases":
            return {"result": {"aliases": []}, "status": "ok", "time": 0.00001}
        if path.endswith("/cluster"):
            return self.scenario.collection_cluster_doc(machine, now)
        if path.startswith("/collections/"):
            return self.scenario.collection_info_doc(now)
        raise RuntimeError("HTTP 404")

    # --- службы наблюдателя ----------------------------------------------------------------
    @staticmethod
    def observer_checks() -> list[tuple[str, str, int]]:
        return [("HAProxy · TLS", "127.0.0.1", 443)]

    @staticmethod
    def connect(host: str, port: int, timeout: float) -> None:
        return None

    # --- нагрузка --------------------------------------------------------------------------
    def config(self) -> dict:
        return {"targets": ["https://machine-4/search"], "rate": RATE, "concurrency": 64,
                "timeout_s": TIMEOUT_MS / 1000, "retry": "other", "k": 5, "arrival": "uniform"}

    def _emit(self, ts: float, p: dict) -> None:
        # Доли сбоев и медленных ответов раскладываются равномерно (накопитель с переносом),
        # а не жребием: линии на графике ровные, как у настоящего ровного потока.
        first = None
        self._acc_fail += p["fail"]
        if self._acc_fail >= 1.0:
            self._acc_fail -= 1.0
            self._acc_conn += 0.12
            if self._acc_conn >= 1.0:
                self._acc_conn -= 1.0
                first = "conn"
            else:
                first = "timeout"
        slow = False
        if first is None:
            self._acc_slow += p["slow"]
            if self._acc_slow >= 1.0:
                self._acc_slow -= 1.0
                slow = True
        base = self._rng.lognormvariate(math.log(p["median"]), p["sigma"])
        if first == "timeout":
            lat = TIMEOUT_MS + base
        elif first == "conn":
            lat = self._rng.uniform(1.5, 6.0) + base
        elif slow:
            lat = self._rng.lognormvariate(math.log(p["slow_ms"]), 0.15)
        else:
            lat = base
        embed = self._rng.lognormvariate(math.log(17.0), 0.18)
        search = self._rng.lognormvariate(math.log(1.3), 0.2)
        self.stats.record(ts, lat, without_retry=first, with_retry=None, retried=first is not None,
                          serving=(MODEL, COLLECTION), timing=(embed, search))
        self._lat_sum += lat
        self._lat_n += 1

    def _generate(self, until: float) -> None:
        while True:
            _, ct = self.scenario.at(self._gen_t)
            p = load_profile(ct)
            nxt = self._gen_t + 1.0 / p["rate"]
            if nxt > until:
                return
            self._gen_t = nxt
            self._emit(nxt, p)

    def load_provider(self, now: float) -> dict:
        self._generate(now)
        snap = self.stats.snapshot(now)
        mean = self._lat_sum / self._lat_n if self._lat_n else 0.0
        self._lat_sum, self._lat_n = 0.0, 0
        snap.update({"source": "mock", "running": True,
                     "inflight": max(0, round(RATE * mean / 1000.0)), "config": self.config()})
        return snap

    # --- события сценария ------------------------------------------------------------------
    MARKS = (
        (T_LOSS, "machine-2 powered off", "simulated: limactl stop --force machine-2"),
        (T_VM_START, "machine-2 powered on", "simulated: limactl start machine-2"),
        (T_REBAL, "search pod rebalanced to machine-2", "simulated, so the next round starts the same"),
    )

    def on_tick(self, now: float, events) -> None:
        """Вызывается панелью на каждом такте: отмечает в ленте действия «оператора»."""
        last = self._last_tick if self._last_tick is not None else now - 0.001
        self._last_tick = now
        if now <= last:
            return
        c0, _ = self.scenario.at(last)
        for cycle in range(c0, self.scenario.at(now)[0] + 1):
            for ct, title, detail in self.MARKS:
                ts = self.scenario.abs(cycle, ct)
                if last < ts <= now:
                    events.add(title, level="info", kind="scenario", detail=detail, machine=VICTIM,
                               ts=ts, source="mock")

    def replay(self, panel, now: float | None = None) -> None:
        """Прогон панели по прошлым секундам: история графика, лента и хронология сбоя."""
        now = self.clock() if now is None else now
        t = now - self.backfill_s
        self._gen_t = t
        self._last_tick = None
        try:
            while t < now:
                self._now = t
                for src in panel.sources():
                    src.poll(now=t)
                panel.tick(now=t)
                t += 1.0
        finally:
            self._now = None
