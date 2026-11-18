"""Источники данных панели стенда «Kubernetes + Qdrant».

Каждый источник — отдельный класс: poll(now) опрашивает свой источник, запоминает последний
удачный ответ и никогда не падает (ошибка уходит в status()). Опрос идёт в своём потоке,
чтобы зависший kubectl или недоступный пир Qdrant не тормозили остальное.

Разбор ответов — чистые функции (parse_kube, parse_leases, build_qdrant): их проверяют тесты
на примерах JSON, и тот же код работает и с живым кластером, и с mock.

Строки, которые видны на панели (ошибки источников, события), — по-английски: ролик на английском.
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

LEVELS = ("info", "ok", "warn", "error")
APP_LABELS = ("app.kubernetes.io/name", "app", "k8s-app", "app.kubernetes.io/instance", "name")
ACTIVE_STATES = {"Active", "ActiveRead"}
PARTIAL_STATES = {"Partial", "Initializing", "PartialSnapshot", "Recovery", "Resharding",
                  "ReshardingScaleDown"}
_FRAC = re.compile(r"\.(\d+)")
_PROGRESS = re.compile(r"\((\d+)\s*/\s*(\d+)\)")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def parse_time(value) -> float | None:
    """RFC 3339 (Kubernetes, Qdrant, Falco) -> unix-время. Наносекунды обрезаются до микро."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def short_error(e: BaseException) -> str:
    """Короткое описание ошибки для панели: без трассировки и без лишних подробностей."""
    if isinstance(e, subprocess.TimeoutExpired):
        return "timed out"
    if isinstance(e, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        r = e.reason
        return "timed out" if isinstance(r, (TimeoutError, socket.timeout)) else _clean(str(r), 120)
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    msg = str(e).strip() or type(e).__name__
    return _clean(msg, 160)


def _clean(value, limit: int) -> str | None:
    """Текст из чужого источника: без управляющих символов, с пределом длины."""
    if value is None:
        return None
    s = _CTRL.sub(" ", str(value)).strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s if len(s) <= limit else s[:limit - 1] + "…"


class Source:
    """Общая часть: последний удачный ответ, ошибка последнего опроса, поток опроса."""

    name = "source"
    interval = 1.0

    def __init__(self, interval: float | None = None) -> None:
        if interval is not None:
            self.interval = interval
        self._lock = threading.Lock()
        self._data = None
        self._ok_at: float | None = None
        self._err: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll(self, now: float | None = None) -> None:
        raise NotImplementedError

    def _ok(self, data, now: float) -> None:
        with self._lock:
            self._data = data
            self._ok_at = now
            self._err = None

    def _fail(self, msg: str, now: float) -> None:
        with self._lock:
            self._err = msg

    def data(self):
        with self._lock:
            return self._data

    def status(self, now: float) -> dict:
        with self._lock:
            age = None if self._ok_at is None else max(0.0, now - self._ok_at)
            return {"ok": self._err is None and self._ok_at is not None,
                    "age_s": None if age is None else round(age, 1),
                    "error": self._err}

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    self.poll()
                except Exception as e:  # опрос не должен останавливать поток
                    self._fail(f"internal error: {type(e).__name__}", time.time())
                self._stop.wait(max(0.05, self.interval - (time.monotonic() - t0)))
        self._thread = threading.Thread(target=loop, name=f"source-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# --- генератор нагрузки ------------------------------------------------------------------------
class LoadSource(Source):
    """Статистика генератора: JSON-файл, который пишет loadgen.py (или функция mock)."""

    name = "load"
    interval = 0.5

    def __init__(self, path: str | None = None, provider=None, stale_after: float = 3.0,
                 interval: float | None = None) -> None:
        super().__init__(interval)
        if not path and provider is None:
            raise ValueError("нужен путь к файлу статистики или provider")
        self.path = path
        self.provider = provider
        self.stale_after = stale_after

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            if self.provider is not None:
                snap = self.provider(now)
            else:
                with open(self.path, encoding="utf-8") as f:
                    snap = json.load(f)
        except FileNotFoundError:
            self._fail("no stats file — load generator not running", now)
            return
        except (OSError, ValueError) as e:
            self._fail(f"cannot read load stats: {type(e).__name__}", now)
            return
        if not isinstance(snap, dict) or not isinstance(snap.get("windows"), dict):
            self._fail("unexpected load stats format", now)
            return
        self._ok(snap, now)

    def snapshot(self, now: float) -> dict | None:
        snap = self.data()
        if snap is None:
            return None
        out = dict(snap)
        age = max(0.0, now - float(snap.get("ts") or 0.0))
        out["age_s"] = round(age, 1)
        out["stale"] = age > self.stale_after
        out.setdefault("running", True)
        return out


# --- Kubernetes -------------------------------------------------------------------------------
def pod_app(labels: dict, name: str | None, owner: dict) -> str:
    for k in APP_LABELS:
        v = labels.get(k)
        if v:
            return str(v)
    base = owner.get("name") or name or "?"
    if owner.get("kind") == "ReplicaSet":
        base = base.rsplit("-", 1)[0]  # search-7f9c6d8b5 -> search
    return base


def pod_status(item: dict) -> str:
    """Статус пода примерно как колонка STATUS у kubectl."""
    md = item.get("metadata") or {}
    st = item.get("status") or {}
    if md.get("deletionTimestamp"):
        return "Terminating"
    if st.get("reason"):
        return str(st["reason"])  # Evicted, NodeLost, Shutdown…
    phase = st.get("phase") or "Unknown"
    for c in st.get("initContainerStatuses") or []:
        s = c.get("state") or {}
        t = s.get("terminated")
        if t and int(t.get("exitCode") or 0) == 0:
            continue
        if t:
            return "Init:" + str(t.get("reason") or "Error")
        w = s.get("waiting")
        if w and w.get("reason") and w["reason"] != "PodInitializing":
            return "Init:" + str(w["reason"])
        return "Init"
    for c in st.get("containerStatuses") or []:
        s = c.get("state") or {}
        w = s.get("waiting")
        if w and w.get("reason"):
            return str(w["reason"])
        t = s.get("terminated")
        if t and phase != "Succeeded" and t.get("reason"):
            return str(t["reason"])
    return phase


def parse_node(item: dict) -> dict:
    md = item.get("metadata") or {}
    st = item.get("status") or {}
    sp = item.get("spec") or {}
    ready = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready"), None) or {}
    labels = md.get("labels") or {}
    roles = sorted(k.split("/", 1)[1] for k in labels
                   if k.startswith("node-role.kubernetes.io/") and k.split("/", 1)[1])
    ip = next((a.get("address") for a in st.get("addresses") or [] if a.get("type") == "InternalIP"), None)
    return {
        "name": md.get("name"),
        "ready": ready.get("status"),  # "True" / "False" / "Unknown" / None
        "ready_since": parse_time(ready.get("lastTransitionTime")),
        "reason": ready.get("reason"),
        "message": _clean(ready.get("message"), 200),
        "roles": roles,
        "ip": ip,
        "kubelet": (st.get("nodeInfo") or {}).get("kubeletVersion"),
        "unschedulable": bool(sp.get("unschedulable")),
        "taints": [f"{t.get('key')}:{t.get('effect')}" for t in sp.get("taints") or []],
    }


def parse_pod(item: dict) -> dict:
    md = item.get("metadata") or {}
    sp = item.get("spec") or {}
    st = item.get("status") or {}
    owners = md.get("ownerReferences") or []
    owner = owners[0] if owners else {}
    ready_cond = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready"), None)
    return {
        "namespace": md.get("namespace"),
        "name": md.get("name"),
        "app": pod_app(md.get("labels") or {}, md.get("name"), owner),
        "node": sp.get("nodeName"),
        "phase": st.get("phase") or "Unknown",
        "status": pod_status(item),
        "ready": bool(ready_cond and ready_cond.get("status") == "True"),
        "restarts": sum(int(c.get("restartCount") or 0) for c in st.get("containerStatuses") or []),
        "ip": st.get("podIP"),
        "owner_kind": owner.get("kind"),
        "owner": owner.get("name"),
        "created": parse_time(md.get("creationTimestamp")),
        "deleting": md.get("deletionTimestamp") is not None,
    }


def parse_kube(doc: dict, namespaces=("search", "qdrant")) -> dict:
    """Ответ `kubectl get nodes,pods -A -o json` -> узлы и поды. Поды вне namespaces — системные."""
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise ValueError("unexpected kubectl output")
    nodes, pods = [], []
    for item in doc["items"]:
        kind = item.get("kind")
        if kind == "Node":
            nodes.append(parse_node(item))
        elif kind == "Pod":
            p = parse_pod(item)
            p["system"] = p["namespace"] not in namespaces
            pods.append(p)
    nodes.sort(key=lambda n: n["name"] or "")
    pods.sort(key=lambda p: (p["namespace"] or "", p["name"] or ""))
    return {"nodes": nodes, "pods": pods}


def parse_leases(doc: dict) -> dict:
    """Аренды kubelet из kube-node-lease: узел -> время последнего продления (пульс узла)."""
    out = {}
    for item in (doc or {}).get("items") or []:
        md = item.get("metadata") or {}
        if md.get("namespace") not in (None, "kube-node-lease"):
            continue
        sp = item.get("spec") or {}
        renew = parse_time(sp.get("renewTime"))
        if md.get("name") and renew is not None:
            out[md["name"]] = {"renew": renew, "duration": sp.get("leaseDurationSeconds")}
    return out


class KubeSource(Source):
    """Узлы и поды через kubectl. Вызов может висеть, когда API недоступен: у kubectl свой
    --request-timeout, у процесса — жёсткий таймаут, данные — последние удачные. Если задано
    несколько --server, при отказе текущего пробуем следующий (у k3s HA их три)."""

    name = "kube"
    interval = 2.0

    def __init__(self, kubectl: str = "kubectl", kubeconfig: str | None = None, servers=(),
                 context: str | None = None, timeout: float = 4.0, namespaces=("search", "qdrant"),
                 leases: bool = True, runner=None, interval: float | None = None) -> None:
        super().__init__(interval)
        self.kubectl = kubectl
        self.kubeconfig = kubeconfig
        self.context = context
        self.timeout = timeout
        self.namespaces = tuple(namespaces)
        self.leases = leases
        self.runner = runner or self._subprocess
        self._servers = list(servers) or [None]
        self._cur = 0

    def command(self, server: str | None, args: list[str]) -> list[str]:
        cmd = [self.kubectl, f"--request-timeout={max(1, int(self.timeout) - 1)}s"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        if server:
            cmd += ["--server", server]
        return cmd + args

    @staticmethod
    def _subprocess(cmd: list[str], timeout: float) -> str:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            lines = (p.stderr or "").strip().splitlines()
            raise RuntimeError(lines[-1] if lines else f"kubectl exited with {p.returncode}")
        return p.stdout

    def _run(self, args: list[str]) -> tuple[str, str | None]:
        last: BaseException | None = None
        for k in range(len(self._servers)):
            idx = (self._cur + k) % len(self._servers)
            try:
                out = self.runner(self.command(self._servers[idx], args), self.timeout)
                self._cur = idx
                return out, self._servers[idx]
            except FileNotFoundError:
                raise
            except Exception as e:
                last = e
        raise last if last else RuntimeError("no API server")

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            raw, server = self._run(["get", "nodes,pods", "-A", "-o", "json"])
            parsed = parse_kube(json.loads(raw), self.namespaces)
        except FileNotFoundError:
            self._fail("kubectl not found", now)
            return
        except subprocess.TimeoutExpired:
            self._fail("Kubernetes API not responding (kubectl timed out)", now)
            return
        except Exception as e:
            self._fail(f"kubectl: {short_error(e)}", now)
            return
        leases = None
        if self.leases:
            try:
                raw_l, _ = self._run(["get", "leases", "-n", "kube-node-lease", "-o", "json"])
                leases = parse_leases(json.loads(raw_l))
            except Exception:
                leases = None  # нет прав на leases — без пульса, по одному Ready
        parsed.update(leases=leases, server=server, fetched_at=now)
        self._ok(parsed, now)


# --- Qdrant -----------------------------------------------------------------------------------
def display_state(raw: str | None, reachable: bool | None, transferring: bool) -> str:
    """Состояние реплики для панели: active, partial, recovering, dead, offline, listener, unknown.
    offline — пир не отвечает, хотя консенсус ещё числит реплику живой (без записей Qdrant
    помечает реплику Dead не сразу)."""
    if raw == "Dead":
        return "dead"
    if reachable is False:
        return "offline"
    if raw in ACTIVE_STATES:
        return "active"
    if raw in ("Recovery", "PartialSnapshot") or (raw in PARTIAL_STATES and transferring):
        return "recovering"
    if raw in PARTIAL_STATES:
        return "partial"
    if raw == "Listener":
        return "listener"
    return "unknown"


def transfer_progress(comment: str | None) -> float | None:
    """Из комментария переноса «Transferring records (1200/2900), …» -> 0.41."""
    m = _PROGRESS.search(comment or "")
    if not m or int(m.group(2)) == 0:
        return None
    return round(min(1.0, int(m.group(1)) / int(m.group(2))), 3)


def build_qdrant(data: dict | None, pods=(), peer_map: dict | None = None, now: float | None = None,
                 failure_window_s: float = 15.0) -> dict | None:
    """Взгляды пиров (ответы /cluster и /collections/{c}/cluster) -> карта «шард × пир».

    Машина пира: явное сопоставление (--qdrant-peer) > под Kubernetes по адресу пира
    (qdrant-1.qdrant-headless -> под qdrant-1 -> его узел; или IP пода) > машина у URL опроса.
    Доступность пира: ответил нам сам — да; адрес, где он отвечал раньше, молчит — нет;
    иначе — по message_send_failures консенсуса за последние failure_window_s секунд.
    Номера пиров — строками: u64 не помещается в число JavaScript.
    """
    if not data:
        return None
    now = time.time() if now is None else now
    views = data.get("views") or []
    ok = [v for v in views if v.get("ok")]
    base_out = {"collection": data.get("collection"), "alias": data.get("alias")}
    if not ok:
        return {"ok": False, **base_out, "status": None, "points": None, "shard_count": 0,
                "replication_factor": None, "peers": [], "shards": [], "transfers": [],
                "summary": {"total": 0, "healthy": False},
                "raft": {"leader": None, "leader_machine": None, "term": None, "commit": None}}
    peer_map = {str(k): v for k, v in (peer_map or {}).items()}

    peers: dict[str, str | None] = {}
    for v in ok:
        for pid, info in (v["cluster"].get("peers") or {}).items():
            peers[str(pid)] = (info or {}).get("uri")
    leaders = [str(v["cluster"]["raft_info"]["leader"]) for v in ok
               if (v["cluster"].get("raft_info") or {}).get("leader") is not None]
    own_leader = [v["peer_id"] for v in ok if (v["cluster"].get("raft_info") or {}).get("role") == "Leader"]
    leader = own_leader[0] if own_leader else (Counter(leaders).most_common(1)[0][0] if leaders else None)

    direct_ok = {v["peer_id"] for v in ok}
    direct_fail = {v["peer_id"] for v in views if not v.get("ok") and v.get("peer_id")} - direct_ok
    failing = set()
    for v in ok:
        for uri, f in (v["cluster"].get("message_send_failures") or {}).items():
            ts = parse_time((f or {}).get("latest_error_timestamp"))
            if ts is not None and now - ts <= failure_window_s:
                failing.add(uri.rstrip("/"))

    def reachable(pid: str) -> bool:
        if pid in direct_ok:
            return True
        if pid in direct_fail:
            return False
        return (peers.get(pid) or "").rstrip("/") not in failing

    by_name = {p["name"]: p for p in pods if p.get("node") and p.get("name")}
    by_ip = {p["ip"]: p for p in pods if p.get("node") and p.get("ip")}
    hints = {v["peer_id"]: v.get("machine") for v in views if v.get("peer_id") and v.get("machine")}

    def locate(pid: str) -> tuple[str | None, str | None]:
        host = urllib.parse.urlsplit(peers.get(pid) or "").hostname or ""
        pod = by_name.get(host.split(".")[0]) or by_ip.get(host)
        if pid in peer_map:
            return peer_map[pid], pod["name"] if pod else None
        if pod:
            return pod["node"], pod["name"]
        return hints.get(pid), None

    base = next((v for v in ok if v["peer_id"] == leader), ok[0])
    cells: dict[tuple[int, str], str | None] = {}
    for v in [base] + [x for x in ok if x is not base]:
        cc = v["collection"]
        for s in cc.get("local_shards") or []:
            cells.setdefault((int(s["shard_id"]), v["peer_id"]), s.get("state"))
        for s in cc.get("remote_shards") or []:
            cells.setdefault((int(s["shard_id"]), str(s["peer_id"])), s.get("state"))

    transfers, seen = [], set()
    for v in ok:
        for t in v["collection"].get("shard_transfers") or []:
            key = (int(t["shard_id"]), str(t.get("from")), str(t.get("to")))
            if key in seen:
                continue
            seen.add(key)
            transfers.append({"shard": key[0], "from": key[1], "to": key[2],
                              "from_machine": locate(key[1])[0], "to_machine": locate(key[2])[0],
                              "method": t.get("method"), "sync": t.get("sync"),
                              "progress": transfer_progress(t.get("comment")),
                              "comment": _clean(t.get("comment"), 160)})
    xfer = {(t["shard"], t["to"]): t for t in transfers}

    shard_count = int(base["collection"].get("shard_count") or 0)
    shard_ids = sorted(set(range(shard_count)) | {s for s, _ in cells})
    shards, counts = [], Counter()
    for sid in shard_ids:
        reps = []
        for (s, pid), raw in sorted(cells.items()):
            if s != sid:
                continue
            machine, _ = locate(pid)
            t = xfer.get((sid, pid))
            disp = display_state(raw, reachable(pid), t is not None)
            counts[disp] += 1
            reps.append({"peer": pid, "machine": machine, "state": raw, "display": disp,
                         "progress": t["progress"] if t and disp == "recovering" else None,
                         "from_machine": t["from_machine"] if t else None})
        shards.append({"id": sid, "replicas": reps})

    peer_rows = []
    for pid in sorted(set(peers) | {p for _, p in cells}):
        machine, pod = locate(pid)
        peer_rows.append({"id": pid, "machine": machine, "pod": pod, "uri": peers.get(pid),
                          "reachable": reachable(pid), "leader": pid == leader})
    info = data.get("info") or {}
    params = ((info.get("config") or {}).get("params") or {})
    rf = params.get("replication_factor") or max((len(s["replicas"]) for s in shards), default=0)
    total = sum(counts.values())
    summary = {k: counts.get(k, 0) for k in ("active", "partial", "recovering", "dead", "offline",
                                             "listener", "unknown")}
    summary["total"] = total
    summary["healthy"] = (total > 0 and counts.get("active", 0) == total and not transfers
                          and all(p["reachable"] for p in peer_rows))
    raft = (base["cluster"].get("raft_info") or {})
    return {
        "ok": True, **base_out,
        "status": info.get("status"),
        "points": info.get("points_count"),
        "shard_count": shard_count or len(shard_ids),
        "replication_factor": rf,
        "peers": peer_rows,
        "shards": shards,
        "transfers": transfers,
        "summary": summary,
        "raft": {"leader": leader, "leader_machine": locate(leader)[0] if leader else None,
                 "term": raft.get("term"), "commit": raft.get("commit")},
    }


class QdrantSource(Source):
    """Каждый пир Qdrant по своему адресу: GET /cluster и GET /collections/{c}/cluster.
    Ключ — только на чтение и только из файла (перечитывается, если файл сменился); в журналы
    и в состояние панели он не попадает. Пиры опрашиваются параллельно: мёртвый пир держит
    опрос не дольше таймаута."""

    name = "qdrant"
    interval = 1.0

    def __init__(self, targets, key_file: str | None, collection: str, timeout: float = 1.5,
                 fetch=None, cafile: str | None = None, insecure: bool = False,
                 interval: float | None = None) -> None:
        super().__init__(interval)
        self.targets = [(m, u.rstrip("/")) for m, u in targets]
        if not self.targets:
            raise ValueError("нужен хотя бы один адрес Qdrant")
        self.key_file = key_file
        self.collection = collection
        self.timeout = timeout
        self.fetch = fetch or self._http_get
        self._key_cache: tuple[int | None, str | None] = (None, None)
        self._url_peer: dict[str, str] = {}
        self._good_url: str | None = None
        self._resolved: tuple[str, str | None] | None = None
        ctx = ssl.create_default_context(cafile=cafile)
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        # прокси из окружения не нужны: Qdrant внутри стенда
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                   urllib.request.HTTPSHandler(context=ctx))
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(self.targets)), thread_name_prefix="qdrant")

    def _key(self) -> str | None:
        if not self.key_file:
            return None
        mtime = os.stat(self.key_file).st_mtime_ns
        if self._key_cache[0] != mtime:
            with open(self.key_file, encoding="utf-8") as f:
                self._key_cache = (mtime, f.read().strip())
        return self._key_cache[1]

    def _http_get(self, base: str, path: str) -> dict:
        headers = {"Accept": "application/json"}
        key = self._key()
        if key:
            headers["api-key"] = key
        req = urllib.request.Request(base + path, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            e.close()  # у ошибки HTTP тоже открытый ответ: закрыть, а не ждать сборщика мусора
            if e.code in (401, 403):
                raise PermissionError(f"HTTP {e.code} — check the read-only API key") from None
            raise RuntimeError(f"HTTP {e.code}") from None

    def _resolve(self, url: str) -> tuple[str, str | None]:
        """Если коллекция задана алиасом — найти настоящую (для сцены со сменой модели).
        Спрашиваем пира, который отвечал в прошлый раз; не ответил — берём прошлый итог."""
        try:
            doc = self.fetch(url, "/aliases")
            found = (self.collection, None)
            for a in ((doc.get("result") or {}).get("aliases") or []):
                if a.get("alias_name") == self.collection and a.get("collection_name"):
                    found = (a["collection_name"], self.collection)
            self._resolved = found
        except PermissionError:
            self._resolved = (self.collection, None)  # ключу не положено видеть алиасы: имя как есть
        except Exception:
            pass
        return self._resolved or (self.collection, None)

    def _view(self, machine: str | None, url: str, collection: str) -> dict:
        v = {"url": url, "machine": machine, "ok": False, "peer_id": self._url_peer.get(url)}
        try:
            cl = self.fetch(url, "/cluster")["result"]
            cc = self.fetch(url, f"/collections/{urllib.parse.quote(collection, safe='')}/cluster")["result"]
            pid = str(cl.get("peer_id") if cl.get("peer_id") is not None else cc.get("peer_id"))
            v.update(ok=True, cluster=cl, collection=cc, peer_id=pid)
        except Exception as e:
            v["error"] = short_error(e)
        return v

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            self._key()
        except OSError:
            self._fail("cannot read Qdrant API key file", now)
            return
        collection, alias = self._resolve(self._good_url or self.targets[0][1])
        views = list(self._pool.map(lambda t: self._view(t[0], t[1], collection), self.targets))
        for v in views:
            if v["ok"]:
                self._url_peer[v["url"]] = v["peer_id"]
        ok = [v for v in views if v["ok"]]
        if not ok:
            self._fail(f"Qdrant not responding: {views[0].get('error', 'error')}", now)
            return
        self._good_url = ok[0]["url"]
        info = None
        try:
            info = self.fetch(ok[0]["url"], f"/collections/{urllib.parse.quote(collection, safe='')}")["result"]
        except Exception:
            pass
        self._ok({"views": views, "collection": collection, "alias": alias, "info": info,
                  "fetched_at": now}, now)


# --- наблюдатель: службы на machine-4 ----------------------------------------------------------
class ServiceChecks(Source):
    """TCP-проверки служб наблюдателя (например, HAProxy на 443): соединение открылось — жива."""

    name = "observer"
    interval = 2.0

    def __init__(self, checks, timeout: float = 0.8, connect=None, interval: float | None = None) -> None:
        super().__init__(interval)
        self.checks = list(checks)  # [(имя, хост, порт)]
        self.timeout = timeout
        self.connect = connect or (lambda host, port, t: socket.create_connection((host, port), timeout=t).close())

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        out = []
        for name, host, port in self.checks:
            t0 = time.perf_counter()
            try:
                self.connect(host, port, self.timeout)
                ok = True
            except OSError:
                ok = False
            out.append({"name": name, "ok": ok, "target": f"{host}:{port}",
                        "ms": round((time.perf_counter() - t0) * 1000, 1)})
        self._ok(out, now)


# --- события ----------------------------------------------------------------------------------
FALCO_LEVELS = {"emergency": "error", "alert": "error", "critical": "error", "error": "error",
                "warning": "warn", "notice": "warn", "informational": "info", "info": "info",
                "debug": "info"}


class RateLimited(Exception):
    pass


class EventsSource:
    """Лента событий: выведенные панелью (переходы узлов, поды, реплики, нагрузка) и внешние
    (POST /api/event: алерты Falco, «ответчик» с карантином пода). Одинаковые события за
    dedupe_s секунд склеиваются в одно со счётчиком, чтобы поток алертов не смыл ленту."""

    name = "events"

    def __init__(self, maxlen: int = 300, dedupe_s: float = 10.0, rate: float = 20.0,
                 burst: float = 40.0) -> None:
        self._q: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._next = 1
        self.dedupe_s = dedupe_s
        self._rate = rate
        self._burst = burst
        self._tokens = burst
        self._t_tokens = time.monotonic()

    def add(self, title: str, level: str = "info", kind: str = "info", detail: str | None = None,
            machine: str | None = None, pod: str | None = None, ts: float | None = None,
            source: str = "panel") -> dict:
        ts = time.time() if ts is None else float(ts)
        ev = {"title": _clean(title, 140) or "event", "detail": _clean(detail, 400),
              "level": level if level in LEVELS else "info", "kind": _clean(kind, 24) or "info",
              "machine": _clean(machine, 64), "pod": _clean(pod, 96), "source": _clean(source, 24),
              "ts": round(ts, 3), "count": 1}
        key = (ev["kind"], ev["title"], ev["machine"], ev["pod"])
        with self._lock:
            for old in list(self._q)[-30:]:
                # сравниваем с последним повтором: непрерывный поток алертов — одна строка со счётчиком
                if (old["kind"], old["title"], old["machine"], old["pod"]) == key \
                        and 0 <= ts - old.get("last_ts", old["ts"]) <= self.dedupe_s:
                    old["count"] += 1
                    old["last_ts"] = ev["ts"]
                    if ev["detail"]:
                        old["detail"] = ev["detail"]
                    return dict(old)
            ev["id"] = self._next
            self._next += 1
            self._q.append(ev)
            return dict(ev)

    def recent(self, limit: int = 60) -> list[dict]:
        with self._lock:
            items = [dict(e) for e in self._q]
        items.sort(key=lambda e: (e.get("last_ts", e["ts"]), e["id"]), reverse=True)
        return items[:limit]

    def ingest(self, payload, now: float | None = None) -> dict:
        """Внешнее событие. Формат Falco (http_output) узнаётся по полям rule/output/priority."""
        now = time.time() if now is None else now
        with self._lock:
            t = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (t - self._t_tokens) * self._rate)
            self._t_tokens = t
            if self._tokens < 1:
                raise RateLimited("too many events")
            self._tokens -= 1
        if not isinstance(payload, dict):
            raise ValueError("JSON object expected")
        if "rule" in payload or "output" in payload:
            fields = payload.get("output_fields") if isinstance(payload.get("output_fields"), dict) else {}
            title = payload.get("rule") or "Falco alert"
            detail = payload.get("output")
            level = FALCO_LEVELS.get(str(payload.get("priority") or "").lower(), "warn")
            machine = payload.get("hostname")
            pod = fields.get("k8s.pod.name")
            kind, source = "security", "falco"
        else:
            title = payload.get("title")
            if not title:
                raise ValueError("field 'title' is required")
            detail = payload.get("detail")
            level = payload.get("level") if payload.get("level") in LEVELS else "info"
            machine = payload.get("machine")
            pod = payload.get("pod")
            kind = payload.get("kind") or "security"
            source = payload.get("source") or "external"
        ts = parse_time(payload.get("time", payload.get("ts")))
        if ts is None or abs(ts - now) > 86400:
            ts = now
        return self.add(title, level=level, kind=kind, detail=detail, machine=machine, pod=pod,
                        ts=ts, source=source)
