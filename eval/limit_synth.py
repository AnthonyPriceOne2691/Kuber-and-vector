"""Синтетика для замера предела стенда: случайные векторы ступенями и сторож памяти и диска.
Запускается внутри machine-1, как eval/qdrant_load.py: к Qdrant через kubectl port-forward ключом
из файла, к Kubernetes через kubectl под root. Только стандартная библиотека.

Векторы случайные и воспроизводимые: компоненты вектора точки i это байты SHAKE-256 от зерна
и номера точки, целые от -128 до 127. Qdrant с косинусом сам нормирует вектор при записи, поэтому
целых чисел хватает, а JSON с целыми почти в шесть раз короче, чем с дробными (3,9 КБ на точку
против 21,8 КБ при 1024 измерениях и том же payload), и собирается быстрее: у машины 2 vCPU, и их делят
пир Qdrant, etcd и API Kubernetes. Векторы считаются на лету порциями, файлов на диске машины нет.
Payload маленький, но с полями, которые читает сервис поиска (app/api.py): title, section, file,
start, end, text. Имена коллекций только synth_*: contracts_* этот скрипт не трогает даже при очистке.

Сторож смотрит между порциями и пока строится индекс:
  - анонимная память пира Qdrant (её не вытеснить на диск) против предела пода;
  - свободная память и диск машин глазами kubelet (stats/summary), условия узлов (MemoryPressure…);
  - готовность и перезапуски пиров Qdrant, статус коллекции и оптимизатора.
Мягкий порог: точек больше не добавляем, ждём индекс, ступень последняя (код выхода 10).
Жёсткий порог: синтетическая коллекция удаляется сразу, машину до OOM не доводим (код 11).
Индекс не достроился за --wait-green: код 12. Ошибка: код 1.

    python3 -I -B limit_synth.py snapshot --url U --key-file K --out baseline.json [--strict]
    python3 -I -B limit_synth.py load --url U --key-file K --collection synth_rf2 --target 100000 \\
        --shards 6 --replicas 2 --quantization int8 --on-disk --baseline baseline.json --out stage-100000.json
    python3 -I -B limit_synth.py clean --url U --key-file K [--collection synth_rf2]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from array import array
from dataclasses import asdict, dataclass

NAME_RE = re.compile(r"synth_[a-z0-9_]{1,48}")
STAGE_SCHEMA = "limit-stage/1"
SNAPSHOT_SCHEMA = "limit-snapshot/1"
SOFT, HARD = "soft", "hard"
EXIT_SOFT, EXIT_HARD, EXIT_INDEX = 10, 11, 12
MIB = 1024 * 1024
PRESSURE = ("MemoryPressure", "DiskPressure", "PIDPressure")
QDRANT_SELECTOR = "app.kubernetes.io/name=qdrant"  # в пространстве qdrant живёт и шлюз, его не считаем


class SetupError(Exception):
    """Коллекция или ответы Qdrant не те, что ждём: грузить нельзя."""


class QdrantError(Exception):
    pass


class GuardError(Exception):
    pass


# --- синтетические точки --------------------------------------------------------------------
def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"синтетические коллекции называются synth_…, получено {name!r}")
    return name


def vector(i: int, seed: int, dim: int) -> list[int]:
    """Компоненты от -128 до 127: байты SHAKE-256 от зерна и номера, одинаковые на любой машине."""
    return array("b", hashlib.shake_256(b"synth:%d:%d" % (seed, i)).digest(dim)).tolist()


def payload(i: int) -> dict:
    return {"title": "synthetic", "section": "", "file": f"synth/{i:07d}", "start": 0, "end": 0, "text": ""}


def batch_body(start: int, end: int, seed: int, dim: int) -> bytes:
    points = [{"id": i, "vector": {"dense": vector(i, seed, dim)}, "payload": payload(i)} for i in range(start, end)]
    return json.dumps({"points": points}, separators=(",", ":")).encode()


def collection_config(dim: int, shards: int, replicas: int, quantization: str | None, on_disk: bool,
                      hnsw_m: int | None = None, ef_construct: int | None = None) -> dict:
    """Схема как у contracts_* (вектор dense, косинус), но исходные векторы на диске, а в памяти int8."""
    dense: dict = {"size": dim, "distance": "Cosine"}
    if on_disk:
        dense["on_disk"] = True
    cfg: dict = {"vectors": {"dense": dense}, "shard_number": shards, "replication_factor": replicas,
                 "on_disk_payload": True}
    if quantization == "int8":
        cfg["quantization_config"] = {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}}
    hnsw = {k: v for k, v in (("m", hnsw_m), ("ef_construct", ef_construct)) if v is not None}
    if hnsw:
        cfg["hnsw_config"] = hnsw
    return cfg


def config_mismatch(info: dict, want: dict) -> list[str]:
    """Чем уже созданная коллекция отличается от нужной схемы (пусто, если ничем)."""
    params = (info.get("config") or {}).get("params") or {}
    have_dense = (params.get("vectors") or {}).get("dense")
    if not isinstance(have_dense, dict):
        return ["нет вектора dense"]
    want_dense = want["vectors"]["dense"]
    out = []
    for key, have, need in (("размер", have_dense.get("size"), want_dense["size"]),
                            ("расстояние", have_dense.get("distance"), want_dense["distance"]),
                            ("векторы на диске", bool(have_dense.get("on_disk")), bool(want_dense.get("on_disk"))),
                            ("шардов", params.get("shard_number"), want["shard_number"]),
                            ("копий", params.get("replication_factor"), want["replication_factor"])):
        if have != need:
            out.append(f"{key} {have}, нужно {need}")
    have_q = (((info.get("config") or {}).get("quantization_config") or {}).get("scalar") or {}).get("type")
    need_q = ((want.get("quantization_config") or {}).get("scalar") or {}).get("type")
    if have_q != need_q:
        out.append(f"квантование {have_q}, нужно {need_q}")
    return out


# --- Qdrant -----------------------------------------------------------------------------------
class Qdrant:
    """REST Qdrant с повтором временных сбоев: туннель kubectl рвётся, повтор порции безопасен
    (id и векторы те же, upsert перезапишет точку той же точкой)."""

    def __init__(self, url: str, key: str, timeout: float = 120, attempts: int = 6, sleep=time.sleep) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.retries = 0

    def call(self, method: str, path: str, body: dict | bytes | None = None, timeout: float | None = None,
             retry: bool = True) -> dict:
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        for attempt in range(1, self.attempts + 1):
            req = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"api-key": self.key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                detail = e.read(300).decode("utf-8", "replace")
                e.close()
                if e.code < 500 or not retry or attempt == self.attempts:
                    raise QdrantError(f"{method} {path.split('?')[0]}: HTTP {e.code} {detail}") from None
                err = f"HTTP {e.code}"
            except (urllib.error.URLError, OSError, ValueError) as e:  # туннель оборвался, таймаут, битый ответ
                if not retry or attempt == self.attempts:
                    raise QdrantError(f"{method} {path.split('?')[0]}: {e}") from None
                err = type(e).__name__
            self.retries += 1
            wait = min(2 ** attempt, 20)
            print(f"  повтор через {wait} с: {method} {path.split('?')[0]} ({err})", flush=True)
            self.sleep(wait)
        raise QdrantError("недостижимо")

    def names(self) -> list[str]:
        return sorted(c["name"] for c in self.call("GET", "/collections")["result"]["collections"])

    def info(self, name: str) -> dict:
        return self.call("GET", f"/collections/{name}")["result"]

    def count(self, name: str) -> int:
        return int(self.call("POST", f"/collections/{name}/points/count", {"exact": True})["result"]["count"])


# --- сторож -----------------------------------------------------------------------------------
_SUFFIX = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60,
           "k": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "E": 10 ** 18, "m": 1e-3, "": 1}


def parse_quantity(q: str) -> int:
    """Размер в записи Kubernetes («2Gi», «4013236Ki», «1500M») в байтах."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?", str(q).strip())
    if not m:
        raise ValueError(f"не размер Kubernetes: {q!r}")
    return int(float(m.group(1)) * _SUFFIX[m.group(2) or ""])


@dataclass
class Thresholds:
    soft_qdrant_mem: float = 0.75   # доля предела пода, анонимная память пира
    hard_qdrant_mem: float = 0.90
    soft_node_avail_mib: int = 300  # свободно памяти машины по kubelet; он вытесняет поды ниже 100 Ми
    hard_node_avail_mib: int = 150
    soft_disk_free: float = 0.25    # доля диска машины; kubelet чистит образы ниже 15 %, а образы у нас без реестра
    hard_disk_free: float = 0.18


def kubectl(args: list[str], timeout: float = 30) -> dict:
    try:
        r = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GuardError(f"kubectl {' '.join(args[:2])}: {e}") from None
    if r.returncode != 0:
        raise GuardError(f"kubectl {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise GuardError(f"kubectl {' '.join(args[:2])}: ответ не JSON") from None


def node_view(nodes: dict, pods: dict, summaries: dict[str, dict]) -> dict[str, dict]:
    """По машине: память и диск глазами kubelet, пир Qdrant на ней, его память и предел пода."""
    view: dict[str, dict] = {}
    for n in nodes.get("items", []):
        st = n.get("status") or {}
        conds = {c.get("type"): c.get("status") for c in st.get("conditions") or []}
        cap = (st.get("capacity") or {}).get("memory")
        view[n["metadata"]["name"]] = {
            "ready": conds.get("Ready") == "True",
            "pressure": [c for c in PRESSURE if conds.get(c) == "True"],
            "mem_capacity": parse_quantity(cap) if cap else None,
            "mem_available": None, "mem_working_set": None, "fs_capacity": None, "fs_available": None,
            "qdrant_pod": None, "qdrant_ready": False, "qdrant_restarts": 0, "qdrant_oom": False,
            "qdrant_limit": None, "qdrant_rss": None, "qdrant_working_set": None,
        }
    for p in pods.get("items", []):
        v = view.get((p.get("spec") or {}).get("nodeName"))
        if v is None:
            continue
        containers = p["spec"].get("containers") or [{}]
        c = next((c for c in containers if c.get("name") == "qdrant"), containers[0])
        lim = ((c.get("resources") or {}).get("limits") or {}).get("memory")
        cs = next((s for s in (p.get("status") or {}).get("containerStatuses") or [] if s.get("name") == c.get("name")), {})
        v.update(qdrant_pod=p["metadata"]["name"], qdrant_container=c.get("name"),
                 qdrant_limit=parse_quantity(lim) if lim else None,
                 qdrant_ready=bool(cs.get("ready")), qdrant_restarts=int(cs.get("restartCount") or 0),
                 qdrant_oom=((cs.get("lastState") or {}).get("terminated") or {}).get("reason") == "OOMKilled")
    for name, s in summaries.items():
        v = view.get(name)
        if v is None:
            continue
        node = s.get("node") or {}
        mem, fs = node.get("memory") or {}, node.get("fs") or {}
        v.update(mem_available=mem.get("availableBytes"), mem_working_set=mem.get("workingSetBytes"),
                 fs_capacity=fs.get("capacityBytes"), fs_available=fs.get("availableBytes"))
        for pod in s.get("pods") or []:
            ref = pod.get("podRef") or {}
            if ref.get("namespace") != "qdrant" or ref.get("name") != v["qdrant_pod"]:
                continue
            for c in pod.get("containers") or []:
                if c.get("name") == v.get("qdrant_container"):
                    m = c.get("memory") or {}
                    v["qdrant_rss"] = m.get("rssBytes", m.get("workingSetBytes"))
                    v["qdrant_working_set"] = m.get("workingSetBytes")
    for v in view.values():
        v.pop("qdrant_container", None)
    return view


def _pct(x: float) -> str:
    return f"{100 * x:.0f} %"


def _gb(x: float | None) -> str:
    return "?" if x is None else f"{x / 1e9:.2f} ГБ".replace(".", ",")


def verdict(view: dict[str, dict], info: dict | None, t: Thresholds,
            restarts0: dict[str, int] | None = None) -> tuple[str | None, list[str]]:
    """Уровень (None, soft, hard) и причины. Нет данных о памяти или диске: мягкий порог, вслепую не грузим."""
    soft: list[str] = []
    hard: list[str] = []
    if not view:
        return SOFT, ["сторож не видит ни одной машины"]
    for name, v in sorted(view.items()):
        if not v["ready"]:
            hard.append(f"{name}: узел не Ready")
        hard.extend(f"{name}: {c}" for c in v["pressure"])
        if v["qdrant_pod"] is None:
            hard.append(f"{name}: нет пира Qdrant")
        else:
            if not v["qdrant_ready"]:
                hard.append(f"{name}: пир {v['qdrant_pod']} не готов")
            before = (restarts0 or {}).get(name)
            if before is not None and v["qdrant_restarts"] > before:
                hard.append(f"{name}: пир перезапускался" + (" (OOMKilled)" if v["qdrant_oom"] else ""))
            limit = v["qdrant_limit"] or v["mem_capacity"]
            if v["qdrant_rss"] is None or not limit:
                soft.append(f"{name}: нет данных о памяти Qdrant")
            else:
                share = v["qdrant_rss"] / limit
                msg = f"{name}: память Qdrant {_gb(v['qdrant_rss'])}, {_pct(share)} предела пода"
                if share >= t.hard_qdrant_mem:
                    hard.append(msg)
                elif share >= t.soft_qdrant_mem:
                    soft.append(msg)
        if v["mem_available"] is None:
            soft.append(f"{name}: нет данных о памяти машины")
        elif v["mem_available"] < t.hard_node_avail_mib * MIB:
            hard.append(f"{name}: свободно памяти {v['mem_available'] // MIB} МиБ")
        elif v["mem_available"] < t.soft_node_avail_mib * MIB:
            soft.append(f"{name}: свободно памяти {v['mem_available'] // MIB} МиБ")
        if v["fs_available"] is None or not v["fs_capacity"]:
            soft.append(f"{name}: нет данных о диске")
        else:
            free = v["fs_available"] / v["fs_capacity"]
            if free < t.hard_disk_free:
                hard.append(f"{name}: свободно диска {_pct(free)}")
            elif free < t.soft_disk_free:
                soft.append(f"{name}: свободно диска {_pct(free)}")
    if info is not None:
        if info.get("status") == "red":
            hard.append("коллекция в статусе red")
        opt = info.get("optimizer_status")
        if isinstance(opt, dict) and opt.get("error"):
            hard.append(f"ошибка оптимизатора: {opt['error']}")
    if hard:
        return HARD, hard + soft
    if soft:
        return SOFT, soft
    return None, []


def brief(view: dict[str, dict]) -> str:
    """Одна строка для журнала: худшая машина по каждому ресурсу."""
    mem = [(v["qdrant_rss"] / (v["qdrant_limit"] or v["mem_capacity"]), n) for n, v in view.items()
           if v["qdrant_rss"] is not None and (v["qdrant_limit"] or v["mem_capacity"])]
    avail = [(v["mem_available"], n) for n, v in view.items() if v["mem_available"] is not None]
    disk = [(v["fs_available"] / v["fs_capacity"], n) for n, v in view.items() if v["fs_available"] is not None and v["fs_capacity"]]
    parts = []
    if mem:
        share, n = max(mem)
        parts.append(f"память Qdrant до {_pct(share)} предела пода ({n})")
    if avail:
        a, n = min(avail)
        parts.append(f"свободно памяти от {_gb(a)} ({n})")
    if disk:
        d, n = min(disk)
        parts.append(f"диска от {_pct(d)} ({n})")
    return ", ".join(parts) or "сторож без данных"


class Guard:
    """Снимки машин между порциями. Пики за ступень копятся в peaks, перезапуски сравниваются
    с началом прогона (restarts0 из baseline.json) или с первым снимком."""

    def __init__(self, kube=None, thresholds: Thresholds | None = None, restarts0: dict[str, int] | None = None,
                 max_failures: int = 3) -> None:
        self.kube = kube or kubectl
        self.t = thresholds or Thresholds()
        self.restarts0 = restarts0
        self.max_failures = max_failures
        self.failures = 0
        self.samples = 0
        self.peaks: dict[str, dict] = {}
        self.last: dict[str, dict] = {}

    def snapshot(self) -> dict[str, dict]:
        nodes = self.kube(["get", "nodes", "-o", "json"])
        pods = self.kube(["-n", "qdrant", "get", "pods", "-l", QDRANT_SELECTOR, "-o", "json"])
        names = [n["metadata"]["name"] for n in nodes.get("items", [])]
        summaries = {n: self.kube(["get", "--raw", f"/api/v1/nodes/{n}/proxy/stats/summary"]) for n in names}
        return node_view(nodes, pods, summaries)

    def check(self, info: dict | None = None) -> tuple[str | None, list[str], dict]:
        try:
            view = self.snapshot()
        except (GuardError, ValueError, KeyError, TypeError) as e:
            self.failures += 1
            if self.failures >= self.max_failures:
                return SOFT, [f"сторож не видит машины {self.failures} раза подряд: {e}"], {}
            return None, [], {}
        self.failures = 0
        self.samples += 1
        if self.restarts0 is None:
            self.restarts0 = {n: v["qdrant_restarts"] for n, v in view.items()}
        for name, v in view.items():
            p = self.peaks.setdefault(name, {})
            for key, pick in (("qdrant_rss", max), ("qdrant_working_set", max), ("mem_available", min),
                              ("fs_available", min)):
                if v[key] is not None:
                    p[key] = v[key] if p.get(key) is None else pick(p[key], v[key])
        self.last = view
        level, reasons = verdict(view, info, self.t, self.restarts0)
        return level, reasons, view


def forward(pod: str, timeout: float = 15) -> tuple[subprocess.Popen, str]:
    """kubectl port-forward к поду Qdrant на свободный порт: (процесс, адрес)."""
    try:
        p = subprocess.Popen(["kubectl", "-n", "qdrant", "port-forward", f"pod/{pod}", ":6333"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as e:
        raise GuardError(f"kubectl port-forward: {e}") from None
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    try:
        line = p.stdout.readline()  # «Forwarding from 127.0.0.1:41234 -> 6333»
    finally:
        timer.cancel()
    m = re.search(r"127\.0\.0\.1:(\d+)", line)
    if not m:
        _stop(p)
        raise GuardError(f"туннель к {pod} не поднялся")
    return p, f"http://127.0.0.1:{m.group(1)}"


def _stop(p: subprocess.Popen) -> None:
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    if p.stdout:
        p.stdout.close()


def peer_statuses(pods: list[str], key: str, name: str) -> dict[str, str]:
    """Статус коллекции глазами каждого пира. Общий статус Qdrant собирает по одной копии каждого
    шарда (свою в первую очередь), поэтому green у одного пира не значит, что соседи достроили индекс."""
    out = {}
    for pod in pods:
        try:
            p, url = forward(pod)
        except GuardError:
            out[pod] = "unreachable"
            continue
        try:
            out[pod] = str(Qdrant(url, key, timeout=10, attempts=2).info(name).get("status"))
        except QdrantError:
            out[pod] = "unreachable"
        finally:
            _stop(p)
    return out


# --- загрузка ступени -------------------------------------------------------------------------
def write_json(path: str, doc: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sanity(q: Qdrant, name: str, i: int, seed: int, dim: int) -> None:
    """Точка вернулась той же: Qdrant принял целые компоненты и нормировал вектор (косинус)."""
    got = q.call("POST", f"/collections/{name}/points", {"ids": [i], "with_vector": True, "with_payload": True})["result"]
    if not got:
        raise SetupError(f"точка {i} не нашлась сразу после записи")
    vec = (got[0].get("vector") or {}).get("dense") or []
    sent = vector(i, seed, dim)
    norm_v = math.sqrt(sum(x * x for x in vec))
    norm_s = math.sqrt(sum(x * x for x in sent))
    cos = sum(a * b for a, b in zip(vec, sent)) / (norm_v * norm_s) if norm_v and norm_s else 0.0
    if len(vec) != dim or cos < 0.999 or abs(norm_v - 1) > 1e-3:
        raise SetupError(f"точка {i} вернулась не той: измерений {len(vec)}, косинус {cos:.4f}, длина {norm_v:.4f}")
    if (got[0].get("payload") or {}).get("file") != payload(i)["file"]:
        raise SetupError(f"у точки {i} не тот payload")


def run_load(args: argparse.Namespace, q: Qdrant, guard: Guard, clock=time.monotonic, sleep=time.sleep,
             peers=None) -> int:
    """Догрузить коллекцию до args.target точек, дождаться индекса, записать итог ступени в args.out.
    peers() -> {под: статус}: статус коллекции у каждого пира; без него ждём только общий green."""
    name = check_name(args.collection)
    want = collection_config(args.dim, args.shards, args.replicas, args.quantization, args.on_disk,
                             args.hnsw_m, args.ef_construct)
    rec: dict = {"schema": STAGE_SCHEMA, "collection": name, "target": args.target, "config": want,
                 "seed": args.seed, "dim": args.dim, "batch": args.batch, "thresholds": asdict(guard.t),
                 "check_every": args.check_every, "guard_interval": args.guard_interval,
                 "wait_green": args.wait_green, "started_at": round(time.time(), 1),
                 "outcome": None, "stopped": None, "deleted": False}

    def save() -> None:
        rec["guard_samples"] = guard.samples
        rec["peaks"] = guard.peaks
        rec["retries"] = q.retries
        write_json(args.out, rec)

    def emergency(reasons: list[str], at: int) -> int:
        rec["stopped"] = {"level": HARD, "reasons": reasons, "at_points": at}
        rec["outcome"] = HARD
        print(f"  ЖЁСТКИЙ ПОРОГ на {at} точках: {'; '.join(reasons)}. Удаляю {name}.", flush=True)
        try:
            q.call("DELETE", f"/collections/{name}", timeout=120)
            rec["deleted"] = True
        except QdrantError as e:
            rec["delete_error"] = str(e)
            print(f"  удалить {name} не вышло: {e}", flush=True)
        rec["after"] = guard.last
        save()
        return EXIT_HARD

    if name in q.names():
        diff = config_mismatch(q.info(name), want)
        if diff:
            raise SetupError(f"{name} уже есть с другой схемой ({'; '.join(diff)}): сначала limit.sh clean")
        rec["created"] = False
    else:
        try:
            q.call("PUT", f"/collections/{name}", want, timeout=60)
        except QdrantError:
            if name not in q.names():  # ответ мог потеряться в туннеле, а коллекция создалась
                raise
        rec["created"] = True
    before = q.count(name)
    rec["points_before"] = before
    # последняя порция прошлого прогона могла лечь не во все шарды: её повторяем
    start = max(0, before - args.batch) if before < args.target else before
    for _ in range(guard.max_failures):  # без первого снимка не начинаем: вслепую не грузим
        level, reasons, view = guard.check(q.info(name))
        if view or level:
            break
        sleep(2)
    rec["before"] = view
    print(f"{name}: в коллекции {before} точек, цель {args.target}; {brief(view)}", flush=True)
    if level == HARD:
        return emergency(reasons, before)
    if level == SOFT:
        rec["stopped"] = {"level": SOFT, "reasons": reasons, "at_points": before}
        print(f"  мягкий порог до загрузки: {'; '.join(reasons)}", flush=True)

    t0 = clock()
    i = start
    sent = 0
    next_check = i + args.check_every
    checked_first = False
    while rec["stopped"] is None and i < args.target:
        end = min(i + args.batch, args.target)
        q.call("PUT", f"/collections/{name}/points?wait=true", batch_body(i, end, args.seed, args.dim))
        if not checked_first:
            sanity(q, name, i, args.seed, args.dim)
            checked_first = True
        sent += end - i
        i = end
        if i >= next_check or i >= args.target:
            next_check = i + args.check_every
            level, reasons, view = guard.check(q.info(name))
            took = max(clock() - t0, 1e-9)
            print(f"  +{took:.0f} с  {i} из {args.target} ({sent / took:.0f} точек в секунду); {brief(view)}", flush=True)
            if level == HARD:
                return emergency(reasons, i)
            if level == SOFT:
                rec["stopped"] = {"level": SOFT, "reasons": reasons, "at_points": i}
                print(f"  мягкий порог на {i} точках: {'; '.join(reasons)}. Дальше не грузим, ждём индекс.", flush=True)
    rec["uploaded"] = sent
    rec["upload_s"] = round(clock() - t0, 1)
    save()

    # Индекс HNSW строится в фоне; мерить до green нельзя. Grey значит «оптимизация ждёт толчка».
    # Green нужен у каждого пира: иначе сосед ещё строит индекс, и лестница померит его, а не поиск.
    t1 = clock()
    last_guard = last_print = grey_since = None
    status = None
    lost_streak = 0
    peers_off = peers is None
    while True:
        info = q.info(name)
        status = info.get("status")
        detail = ""
        if status == "green" and not peers_off:
            st = peers()
            rec["peers"] = st
            bad = sorted(p for p, s in st.items() if s != "green")
            lost = [p for p in bad if st[p] == "unreachable"]
            lost_streak = lost_streak + 1 if lost else 0
            if lost and lost_streak >= 5:
                peers_off = True
                rec["peers_note"] = f"не ответили через туннель: {', '.join(lost)}; проверен только общий статус"
                print(f"  {rec['peers_note']}", flush=True)
            elif bad:
                status = "yellow"
                detail = "; у пиров " + ", ".join(f"{p} {st[p]}" for p in bad)
        now = clock()
        if last_guard is None or now - last_guard >= args.guard_interval:
            last_guard = now
            level, reasons, view = guard.check(info)
            if level == HARD:
                return emergency(reasons, i)
            if level == SOFT and rec["stopped"] is None:
                rec["stopped"] = {"level": SOFT, "reasons": reasons, "at_points": i, "during": "index"}
                print(f"  мягкий порог, пока строится индекс: {'; '.join(reasons)}", flush=True)
        if status == "green":
            break
        if status == "grey":
            grey_since = grey_since if grey_since is not None else now
            if now - grey_since >= args.grey_kick:
                print("  статус grey: подталкиваю оптимизатор", flush=True)
                q.call("PATCH", f"/collections/{name}", {"optimizers_config": {}}, timeout=60)
                grey_since = None
        else:
            grey_since = None
        if now - t1 >= args.wait_green:
            rec["index_timeout"] = True
            break
        if last_print is None or now - last_print >= 60:
            last_print = now
            print(f"  индекс: +{now - t1:.0f} с, статус {status}, проиндексировано {info.get('indexed_vectors_count')} "
                  f"из {info.get('points_count')}, сегментов {info.get('segments_count')}{detail}", flush=True)
        sleep(args.poll)
    rec["index_s"] = round(clock() - t1, 1)
    rec["status"] = status
    rec["indexed_vectors"] = info.get("indexed_vectors_count")
    rec["segments"] = info.get("segments_count")
    rec["points"] = q.count(name)
    level, reasons, view = guard.check(info)
    rec["after"] = view or guard.last
    if level == HARD:
        return emergency(reasons, rec["points"])
    if level == SOFT and rec["stopped"] is None:
        rec["stopped"] = {"level": SOFT, "reasons": reasons, "at_points": rec["points"], "during": "index"}
    if rec.get("index_timeout"):
        rec["outcome"] = "index_timeout"
        code = EXIT_INDEX
    elif rec["stopped"] is not None:
        rec["outcome"] = SOFT
        code = EXIT_SOFT
    else:
        rec["outcome"] = "ok"
        code = 0
    save()
    print(f"{name}: {rec['points']} точек; загрузка {rec['upload_s']:.0f} с, индекс {rec['index_s']:.0f} с, "
          f"статус {status}; {brief(rec['after'])}", flush=True)
    return code


# --- снимок и очистка -------------------------------------------------------------------------
def collection_brief(q: Qdrant, name: str) -> dict:
    info = q.info(name)
    params = (info.get("config") or {}).get("params") or {}
    dense = (params.get("vectors") or {}).get("dense") if isinstance(params.get("vectors"), dict) else None
    quant = (info.get("config") or {}).get("quantization_config") or {}
    return {"name": name, "points": info.get("points_count"), "status": info.get("status"),
            "shards": params.get("shard_number"), "replicas": params.get("replication_factor"),
            "on_disk": bool((dense or {}).get("on_disk")), "quantization": (quant.get("scalar") or {}).get("type")}


def snapshot(q: Qdrant, guard: Guard, sleep=time.sleep) -> dict:
    collections = [collection_brief(q, n) for n in q.names()]
    for _ in range(guard.max_failures):  # пустой снимок не годится в базу: после трёх сбоев мягкий порог
        level, reasons, view = guard.check()
        if view or level:
            break
        sleep(2)
    return {"schema": SNAPSHOT_SCHEMA, "ts": round(time.time(), 1), "collections": collections,
            "nodes": view, "level": level, "reasons": reasons}


def clean(q: Qdrant, only: str | None = None) -> list[str]:
    """Удаляет synth_* (или одну названную synth_-коллекцию); остальные имена не трогает никогда."""
    if only is not None:
        check_name(only)
    names = q.names()
    targets = [n for n in names if NAME_RE.fullmatch(n)] if only is None else [n for n in names if n == only]
    for n in targets:
        q.call("DELETE", f"/collections/{n}", timeout=120)
    return targets


# --- командная строка -------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", required=True)
    common.add_argument("--key-file", required=True)
    ap = argparse.ArgumentParser(description="Синтетика для замера предела стенда")
    sub = ap.add_subparsers(dest="cmd", required=True)
    guard_opts = argparse.ArgumentParser(add_help=False)
    d = Thresholds()
    for f, v in asdict(d).items():
        guard_opts.add_argument("--" + f.replace("_", "-"), type=type(v), default=v)

    s = sub.add_parser("snapshot", parents=[common, guard_opts], help="что сейчас: коллекции, память и диск машин")
    s.add_argument("--out")
    s.add_argument("--strict", action="store_true", help="код 10 или 11, если порог уже достигнут")

    ld = sub.add_parser("load", parents=[common, guard_opts], help="догрузить коллекцию до --target точек")
    ld.add_argument("--collection", required=True)
    ld.add_argument("--target", type=int, required=True)
    ld.add_argument("--shards", type=int, default=6)
    ld.add_argument("--replicas", type=int, default=2)
    ld.add_argument("--quantization", choices=("int8", "none"), default="int8")
    ld.add_argument("--on-disk", action="store_true", help="исходные векторы на диске")
    ld.add_argument("--hnsw-m", type=int)
    ld.add_argument("--ef-construct", type=int)
    ld.add_argument("--dim", type=int, default=1024)
    ld.add_argument("--seed", type=int, default=1)
    ld.add_argument("--batch", type=int, default=256)
    ld.add_argument("--check-every", type=int, default=20000, help="сторож каждые N точек")
    ld.add_argument("--guard-interval", type=float, default=15, help="сторож, пока строится индекс, раз в N с")
    ld.add_argument("--poll", type=float, default=5)
    ld.add_argument("--grey-kick", type=float, default=30)
    ld.add_argument("--wait-green", type=float, default=7200, help="сколько ждать индекса, с")
    ld.add_argument("--baseline", help="снимок до синтетики: перезапуски пиров сравниваются с ним")
    ld.add_argument("--out", required=True)

    c = sub.add_parser("clean", parents=[common], help="удалить synth_*")
    c.add_argument("--collection")
    args = ap.parse_args(argv)
    if getattr(args, "quantization", None) == "none":
        args.quantization = None
    try:
        if getattr(args, "collection", None) is not None:
            check_name(args.collection)
    except ValueError as e:
        ap.error(str(e))
    if args.cmd == "load" and (args.target < 0 or args.batch < 1 or args.check_every < 1):
        ap.error("--target от 0, --batch и --check-every от 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with open(args.key_file) as f:
        key = f.read().strip()
    q = Qdrant(args.url, key)
    if args.cmd == "clean":
        gone = clean(q, args.collection)
        print("удалены: " + (", ".join(gone) if gone else "нечего удалять"), flush=True)
        return 0
    t = Thresholds(**{f: getattr(args, f) for f in asdict(Thresholds())})
    if args.cmd == "snapshot":
        snap = snapshot(q, Guard(kubectl, t))
        if args.out:
            write_json(args.out, snap)
        for c in snap["collections"]:
            print(f"  {c['name']}: {c['points']} точек, {c['status']}, шардов {c['shards']} × {c['replicas']}"
                  + (", на диске" if c["on_disk"] else "") + (f", {c['quantization']}" if c["quantization"] else ""))
        print(f"  машины: {brief(snap['nodes'])}")
        if snap["level"]:
            print(f"  порог {snap['level']}: {'; '.join(snap['reasons'])}")
        return {None: 0, SOFT: EXIT_SOFT, HARD: EXIT_HARD}[snap["level"]] if args.strict else 0
    restarts0 = None
    if args.baseline and os.path.exists(args.baseline):
        with open(args.baseline, encoding="utf-8") as f:
            # пустой снимок не база: тогда перезапуски считаем от первого снимка этой ступени
            restarts0 = {n: v.get("qdrant_restarts", 0) for n, v in (json.load(f).get("nodes") or {}).items()} or None
    guard = Guard(kubectl, t, restarts0)

    def peers() -> dict[str, str]:
        pods = sorted(v["qdrant_pod"] for v in guard.last.values() if v.get("qdrant_pod"))
        return peer_statuses(pods, key, args.collection) if pods else {}
    try:
        return run_load(args, q, guard, peers=peers)
    except (SetupError, QdrantError) as e:
        print(f"стоп: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
