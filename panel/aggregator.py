#!/usr/bin/env python3
"""Агрегатор панели стенда «Kubernetes + Qdrant»: HTTP-сервер на стандартной библиотеке.

Раздаёт статику панели (static/index.html) и единый JSON для неё — GET /api/state (контракт —
в panel/README.md). Источники подключаемые, каждый — отдельный класс (panel/sources.py):
load (статистика генератора), kube (узлы и поды), qdrant (карта «шард × машина»),
observer (службы machine-4), events (лента и POST /api/event для внешних событий, например
алертов Falco); mock (panel/mockstand.py) — сценарий «machine-2 выключена» без кластера.

Поверх источников агрегатор раз в 0,5 с собирает состояние: карточки машин, события из
переходов (узел NotReady, под переехал, реплика Dead…) и хронологию сбоя с временем
восстановления.

    python3 -I -B panel/aggregator.py --mock            # на Маке, без кластера
    open http://127.0.0.1:8090/

На стенде — служба systemd на machine-4 (deploy/panel/install.sh): страница только на
127.0.0.1, события Falco принимает второй адрес (--event-bind), только POST /api/event с токеном.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import signal
import socketserver
import sys
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    # Соседние модули панели. В конец списка: при запуске с -I стандартная библиотека остаётся
    # главнее, подменить json.py файлом из этой папки не выйдет.
    sys.path.append(HERE)

from sources import (EventsSource, KubeAPI, KubeSource, LoadSource, QdrantSource,  # noqa: E402
                     RateLimited, ServiceChecks, build_qdrant)

SCHEMA = "panel/1"
# Цвета машин: первые слоты категориальной палитры (синий, оранжевый, бирюзовый проверены на
# различимость при дальтонизме); наблюдатель вне кластера — нейтральный серый.
CLUSTER_COLORS = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9", "#e66767", "#0ca30c")
OBSERVER_COLOR = "#8a93a5"
EXTRA_COLOR = "#6b7280"
STEPS = (("lost", "Machine lost"), ("notready", "Node NotReady"), ("moved", "Pods rescheduled"),
         ("back", "Machine back"), ("recovered", "Replicas in sync"))
FAILED_POD = {"CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff", "ErrImagePull",
              "CreateContainerConfigError", "Evicted", "Failed", "NodeLost"}


@dataclass
class Config:
    machines: list[str] = field(default_factory=lambda: ["machine-1", "machine-2", "machine-3", "machine-4"])
    observer: str | None = "machine-4"
    mode: str = "live"
    title: str = "Kubernetes + Qdrant"
    subtitle: str = "Search service on a 3-node k3s HA cluster"
    caption: str = "3 VMs + observer on one laptop · numbers for this hardware"
    peer_map: dict = field(default_factory=dict)
    lease_stale_s: float = 15.0
    lease_renew_s: float = 10.0
    spike_ms: float = 250.0
    cycle_s: float | None = None


def fmt_ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def machine_colors(names: list[str], observer: str | None) -> dict[str, str]:
    out, i = {}, 0
    for n in names:
        if n == observer:
            out[n] = OBSERVER_COLOR
        else:
            out[n] = CLUSTER_COLORS[i] if i < len(CLUSTER_COLORS) else EXTRA_COLOR  # без повтора по кругу
            i += 1
    return out


def pod_state(p: dict) -> str:
    """Состояние пода для панели: ready, starting, notready, terminating, failed."""
    if p.get("deleting"):
        return "terminating"
    if p.get("ready"):
        return "ready"
    s = p.get("status") or ""
    if s in FAILED_POD or p.get("phase") == "Failed":
        return "failed"
    if p.get("phase") == "Pending" or s in ("ContainerCreating", "PodInitializing") or s.startswith("Init"):
        return "starting"
    return "notready"


def pod_view(p: dict, now: float) -> dict:
    short = p["name"] or "?"
    if p.get("owner_kind") in ("ReplicaSet", "DaemonSet") and "-" in short:
        short = short.rsplit("-", 1)[1]  # search-7f9c6d8b5-x2k9p -> x2k9p
    return {"namespace": p["namespace"], "name": p["name"], "short": short, "app": p["app"],
            "status": p["status"], "state": pod_state(p), "ready": p["ready"], "restarts": p["restarts"],
            "owner_kind": p.get("owner_kind"),
            "age_s": None if p.get("created") is None else round(max(0.0, now - p["created"]), 1)}


def observer_services(checks, load: dict | None) -> list[dict]:
    out = [{"name": c["name"], "ok": c["ok"], "detail": c["target"]} for c in checks or []]
    if load is not None:
        running = bool(load.get("running", True)) and not load.get("stale")
        cfg = load.get("config") or {}
        retry = "on" if cfg.get("retry") == "other" else "off"
        detail = f"{cfg.get('rate', '?'):g} req/s · retry {retry}" if isinstance(cfg.get("rate"), (int, float)) \
            else f"retry {retry}"
        out.append({"name": "Load generator", "ok": running, "detail": detail if running else "stopped"})
    else:
        out.append({"name": "Load generator", "ok": None, "detail": "not connected"})
    out.append({"name": "Panel", "ok": True, "detail": "this page"})
    return out


def build_machines(cfg: Config, kube: dict | None, kube_ok: bool, qdrant: dict | None, load: dict | None,
                   checks, now: float) -> list[dict]:
    """Карточки машин: статус узла (Ready / нет пульса / NotReady), поды, пир Qdrant, цвет.
    Пульс — по аренде kubelet. Если API не отвечает, возраст пульса считается на момент
    последнего удачного ответа: иначе устаревшие данные «уронили» бы все машины разом."""
    nodes = {n["name"]: n for n in (kube or {}).get("nodes") or []}
    leases = (kube or {}).get("leases") or {}
    ref = now if kube_ok else ((kube or {}).get("fetched_at") or now)
    by_node = defaultdict(list)
    for p in (kube or {}).get("pods") or []:
        if p.get("node") and p.get("phase") != "Succeeded":
            by_node[p["node"]].append(p)
    names = list(cfg.machines) + sorted(n for n in nodes if n not in cfg.machines)
    colors = machine_colors(names, cfg.observer)
    peers, reps = {}, defaultdict(Counter)
    if qdrant and qdrant.get("ok"):
        for pr in qdrant["peers"]:
            if pr.get("machine"):
                peers[pr["machine"]] = pr
        for sh in qdrant["shards"]:
            for r in sh["replicas"]:
                if r.get("machine"):
                    reps[r["machine"]][r["display"]] += 1
    out = []
    for name in names:
        node = nodes.get(name)
        m = {"name": name, "color": colors[name],
             "role": "observer" if name == cfg.observer else ("server" if node else "machine"),
             "status": "unknown", "status_since": None, "heartbeat_at": None, "heartbeat_age_s": None,
             "ip": None, "kube": None, "pods": [], "system_pods": 0, "qdrant": None, "services": None}
        if name == cfg.observer:
            m["status"] = "observer"
            m["services"] = observer_services(checks, load)
        elif node:
            m["ip"] = node["ip"]
            m["kube"] = {k: node[k] for k in ("ready", "reason", "message", "roles", "kubelet",
                                               "unschedulable", "taints")}
            lease = leases.get(name)
            age = None
            if lease:
                age = max(0.0, ref - lease["renew"])
                m["heartbeat_at"] = lease["renew"]
                m["heartbeat_age_s"] = round(age, 1)
            if node["ready"] != "True":
                m["status"], m["status_since"] = "notready", node["ready_since"]
            elif age is not None and age > cfg.lease_stale_s:
                m["status"], m["status_since"] = "unreachable", lease["renew"]
            else:
                m["status"], m["status_since"] = "ready", node["ready_since"]
        pods = by_node.get(name, [])
        m["pods"] = [pod_view(p, now) for p in pods if not p.get("system")]
        m["system_pods"] = sum(1 for p in pods if p.get("system"))
        pr = peers.get(name)
        if pr:
            m["qdrant"] = {"peer": pr["id"], "pod": pr.get("pod"), "reachable": pr["reachable"],
                           "leader": pr["leader"], "replicas": dict(reps[name])}
        out.append(m)
    return out


class IncidentTracker:
    """Хронология сбоя одной машины: потеряна -> NotReady -> поды переехали -> вернулась ->
    реплики догнали. Время восстановления — от возвращения машины до «все реплики Active».

    Момент потери оценивается по самому раннему надёжному признаку: пир Qdrant на этой машине
    перестал отвечать (точно, ±1 с); иначе — последнее продление аренды kubelet плюс
    полинтервала (±5 с); иначе — переход узла в NotReady."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cur: dict | None = None

    @staticmethod
    def _desired(pods, machine: str) -> dict[str, int]:
        """Приложения с подами Deployment на машине и сколько у них подов (не удаляемых) всего."""
        apps = {p["app"] for p in pods if p.get("node") == machine and p.get("owner_kind") == "ReplicaSet"
                and not p.get("system")}
        return {a: sum(1 for p in pods if p["app"] == a and p.get("owner_kind") == "ReplicaSet"
                       and not p.get("deleting") and not p.get("system")) for a in sorted(apps)}

    def _moved(self, pods, inc: dict) -> bool:
        for app, want in inc["desired"].items():
            have = sum(1 for p in pods if p["app"] == app and p.get("owner_kind") == "ReplicaSet"
                       and p.get("ready") and not p.get("deleting") and p.get("node") != inc["machine"])
            if have < want:
                return False
        return True

    def _lost_at(self, m: dict, peer_down: dict, now: float) -> float:
        q = peer_down.get(m["name"])
        hb = m.get("heartbeat_at")
        if q is not None and now - q <= 60 and (hb is None or q >= hb):
            return q
        if hb is not None and m["status"] == "unreachable":
            return min(now, hb + self.cfg.lease_renew_s / 2)
        if m["status"] == "notready" and m.get("status_since"):
            return min(now, m["status_since"])
        return now

    def update(self, now: float, machines: list[dict], pods, qdrant: dict | None, load: dict | None,
               peer_down: dict) -> None:
        cluster = {m["name"]: m for m in machines if m["role"] != "observer"}
        inc = self.cur
        if inc is not None and not inc["active"]:
            self._accumulate(inc, load)  # дочитать последние секунды ряда до момента восстановления
        if inc is None or not inc["active"]:
            bad = [m for m in cluster.values() if m["status"] in ("unreachable", "notready")]
            if not bad:
                return
            m = bad[0]
            self.cur = inc = {"active": True, "machine": m["name"], "color": m["color"],
                              "t": {"lost": self._lost_at(m, peer_down, now)},
                              "desired": self._desired(pods, m["name"]),
                              "acc": {"requests": 0, "without_retry": 0, "with_retry": 0,
                                      "p99_max": None, "last_t": None}}
        m = cluster.get(inc["machine"])
        t = inc["t"]
        if m is not None:
            st = m["status"]
            if "notready" not in t and st == "notready":
                t["notready"] = max(t["lost"], min(now, m["status_since"] or now))
            if "moved" not in t and inc["desired"] and "notready" in t and self._moved(pods, inc):
                t["moved"] = now
            if "back" in t and "recovered" not in t and st in ("unreachable", "notready"):
                del t["back"]  # машина снова пропала, не успев восстановиться
            if "back" not in t and st == "ready":
                since = m.get("status_since")
                t["back"] = since if since and t["lost"] < since <= now else now
            if "back" in t and "recovered" not in t:
                if qdrant is None or (qdrant.get("ok") and qdrant["summary"]["healthy"]):
                    t["recovered"] = now
        self._accumulate(inc, load)
        if "recovered" in t:
            inc["active"] = False

    @staticmethod
    def _accumulate(inc: dict, load: dict | None) -> None:
        """Запросы и ошибки за время сбоя — по секундам ряда генератора (каждая секунда один раз)."""
        if not load:
            return
        acc, t = inc["acc"], inc["t"]
        first = math.floor(t["lost"])
        last = math.floor(t["recovered"]) if "recovered" in t else None
        for pt in load.get("series") or []:
            s = pt["t"]
            if s < first or (acc["last_t"] is not None and s <= acc["last_t"]) or (last is not None and s > last):
                continue
            acc["requests"] += pt.get("qps") or 0
            acc["without_retry"] += pt.get("errors_without_retry") or 0
            acc["with_retry"] += pt.get("errors_with_retry") or 0
            if pt.get("p99") is not None:
                acc["p99_max"] = pt["p99"] if acc["p99_max"] is None else max(acc["p99_max"], pt["p99"])
            acc["last_t"] = s

    def offset(self, machine: str | None, ts: float | None) -> float | None:
        inc = self.cur
        if inc is None or ts is None or (machine is not None and machine != inc["machine"]):
            return None
        return ts - inc["t"]["lost"]

    def recovery_s(self) -> float | None:
        t = (self.cur or {}).get("t") or {}
        if "back" in t and "recovered" in t:
            return t["recovered"] - t["back"]
        return None

    def snapshot(self, now: float) -> dict | None:
        inc = self.cur
        if inc is None:
            return None
        t = inc["t"]
        steps = []
        for key, label in STEPS:
            if key == "moved" and not inc["desired"]:
                continue  # на машине не было подов Deployment — переезжать нечему
            ts = t.get(key)
            steps.append({"key": key, "label": label, "ts": ts, "done": ts is not None,
                          "offset_s": None if ts is None else round(ts - t["lost"], 1)})
        phase = next((k for k, _ in reversed(STEPS) if k in t), "lost")
        end = t.get("recovered", now)
        acc = inc["acc"]
        return {
            "active": inc["active"], "machine": inc["machine"], "color": inc["color"], "phase": phase,
            "started": t["lost"], "elapsed_s": round(end - t["lost"], 1),
            "downtime_s": round(t["back"] - t["lost"], 1) if "back" in t else None,
            "recovery_s": round(t["recovered"] - t["back"], 1) if "back" in t and "recovered" in t else None,
            "total_s": round(t["recovered"] - t["lost"], 1) if "recovered" in t else None,
            "steps": steps,
            "apps": inc["desired"],
            "load": {k: acc[k] for k in ("requests", "without_retry", "with_retry", "p99_max")},
        }


class Panel:
    """Сборка /api/state из источников и события из переходов между тактами."""

    def __init__(self, cfg: Config, events: EventsSource, load: LoadSource | None = None,
                 kube: KubeSource | None = None, qdrant: QdrantSource | None = None,
                 observer: ServiceChecks | None = None, mock=None) -> None:
        self.cfg = cfg
        self.events = events
        self.load = load
        self.kube = kube
        self.qdrant = qdrant
        self.observer = observer
        self.mock = mock
        self.incident = IncidentTracker(cfg)
        self._prev: dict | None = None
        self._peer_down: dict[str, float] = {}
        self._src_prev: dict[str, bool] = {}
        self._lf = {"fail": False, "user": False, "spike": False, "base": None, "calm_since": None,
                    "spike_max": None, "ep_start": None}
        self._lock = threading.Lock()
        self._state: dict = {}
        self._state_bytes = b"{}"
        self._stop = threading.Event()

    def sources(self) -> list:
        return [s for s in (self.load, self.kube, self.qdrant, self.observer) if s is not None]

    # --- такт -----------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            if self.mock is not None:
                self.mock.on_tick(now, self.events)
            statuses: dict[str, dict] = {}
            load = None
            if self.load is not None:
                load = self.load.snapshot(now)
                st = self.load.status(now)
                # генератор остановлен (последний снимок с running: false) или молчит дольше 3 с
                st["stale"] = bool(load and (load.get("stale") or load.get("running") is False))
                statuses["load"] = st
            kube = kst = None
            if self.kube is not None:
                kube = self.kube.data()
                kst = statuses["kube"] = self.kube.status(now)
            pods = (kube or {}).get("pods") or []
            qdrant = None
            if self.qdrant is not None:
                statuses["qdrant"] = self.qdrant.status(now)
                # источник есть, а данных ещё нет — это «не отвечает», а не «Qdrant не подключён»
                qdrant = build_qdrant(self.qdrant.data() or {"views": [], "collection": self.qdrant.collection},
                                      pods, self.cfg.peer_map, now)
            checks = None
            if self.observer is not None:
                checks = self.observer.data()
                statuses["observer"] = self.observer.status(now)
            machines = build_machines(self.cfg, kube, bool(kst and kst["ok"]), qdrant, load, checks, now)
            self._track_peers(now, qdrant)
            self.incident.update(now, machines, pods, qdrant, load, self._peer_down)
            self._derive(now, machines, kube, qdrant, load, statuses)
            state = {
                "schema": SCHEMA,
                "ts": round(now, 3),
                "mode": self.cfg.mode,
                "ui": {"title": self.cfg.title, "subtitle": self.cfg.subtitle, "caption": self.cfg.caption},
                "sources": statuses,
                "load": load,
                "machines": machines,
                "kube": self._kube_section(kube, kst, now),
                "qdrant": qdrant,
                "incident": self.incident.snapshot(now),
                "events": self.events.recent(50),
            }
            self._state = state
            self._state_bytes = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            return state

    def state_bytes(self) -> bytes:
        with self._lock:
            return self._state_bytes

    @staticmethod
    def _kube_section(kube: dict | None, kst: dict | None, now: float) -> dict | None:
        if kst is None:
            return None
        out = {"ok": kst["ok"], "age_s": kst["age_s"], "error": kst["error"], "server": None,
               "heartbeats": False, "nodes": [], "unscheduled": []}
        if kube:
            out["server"] = kube.get("server")
            out["heartbeats"] = kube.get("leases") is not None
            out["nodes"] = [{"name": n["name"], "ready": n["ready"], "since": n["ready_since"],
                             "reason": n["reason"], "unschedulable": n["unschedulable"],
                             "taints": n["taints"]} for n in kube["nodes"]]
            out["unscheduled"] = [pod_view(p, now) for p in kube["pods"]
                                  if not p.get("node") and not p.get("system")
                                  and p["phase"] not in ("Succeeded", "Failed")]
        return out

    def _track_peers(self, now: float, qdrant: dict | None) -> None:
        if not qdrant or not qdrant.get("ok"):
            return
        for pr in qdrant["peers"]:
            key = pr.get("machine") or pr["id"]
            if pr["reachable"] is False:
                self._peer_down.setdefault(key, now)
            else:
                self._peer_down.pop(key, None)

    def start(self, interval: float = 0.5) -> None:
        for s in self.sources():
            s.start()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # такт не должен останавливать панель
                    print(f"[panel] ошибка такта: {type(e).__name__}: {e}", file=sys.stderr)
                self._stop.wait(interval)
        threading.Thread(target=loop, name="panel-tick", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        for s in self.sources():
            s.stop()

    # --- события из переходов -------------------------------------------------------------
    def _add(self, title: str, level: str, kind: str, now: float, **kw) -> None:
        kw.setdefault("ts", now)
        kw["ts"] = min(now, kw["ts"] if kw["ts"] is not None else now)
        self.events.add(title, level=level, kind=kind, **kw)

    def _derive(self, now: float, machines, kube, qdrant, load, statuses) -> None:
        qok = bool(qdrant and qdrant.get("ok"))
        cur = {
            "status": {m["name"]: m["status"] for m in machines if m["role"] != "observer"},
            "pods": None if kube is None else {
                (p["namespace"], p["name"]): {k: p.get(k) for k in ("app", "name", "node", "ready", "deleting",
                                                                      "restarts")}
                for p in kube["pods"] if not p.get("system") and p["phase"] != "Succeeded"},
            "peers": {pr["id"]: pr["reachable"] for pr in qdrant["peers"]} if qok else None,
            "cells": {(sh["id"], r["peer"]): r["display"] for sh in qdrant["shards"] for r in sh["replicas"]}
            if qok else None,
            "healthy": qdrant["summary"]["healthy"] if qok else None,
            "leader": qdrant["raft"]["leader"] if qok else None,
            "machine_of": {pr["id"]: pr.get("machine") for pr in qdrant["peers"]} if qok else {},
        }
        prev, self._prev = self._prev, cur
        if prev is None:
            self._hello(now, machines)
            self._src_prev = {k: bool(v["ok"]) and not v.get("stale") for k, v in statuses.items()}
            return
        self._derive_sources(now, statuses)
        self._derive_nodes(now, machines, prev)
        if prev["pods"] is not None and cur["pods"] is not None:
            self._derive_pods(now, machines, prev["pods"], cur["pods"])
        if prev["cells"] is not None and cur["cells"] is not None:
            self._derive_qdrant(now, qdrant, prev, cur)
        if load and not load.get("stale"):
            self._derive_load(now, load)

    def _hello(self, now: float, machines) -> None:
        if self.cfg.mode == "mock":
            cyc = f" · repeats every {self.cfg.cycle_s:.0f} s" if self.cfg.cycle_s else ""
            self._add("Simulation running", "info", "info", now, detail=f"scenario: machine-2 power loss{cyc}")
        else:
            names = ", ".join(s.name for s in self.sources()) or "none"
            self._add("Panel started", "info", "info", now, detail=f"sources: {names}")

    def _derive_sources(self, now: float, statuses: dict) -> None:
        labels = {"kube": "Kubernetes API", "qdrant": "Qdrant API"}
        for k, st in statuses.items():
            ok = bool(st["ok"]) and not st.get("stale")
            old = self._src_prev.get(k)
            self._src_prev[k] = ok
            if old is None or old == ok:
                continue
            if k == "load":
                if ok:
                    self._add("Load generator running", "ok", "source", now)
                else:
                    self._add("Load generator stopped", "warn", "source", now,
                              detail=st.get("error") or "no fresh stats for over 3 s")
            elif k in labels:
                if ok:
                    self._add(f"{labels[k]} responding again", "ok", "source", now)
                else:
                    self._add(f"{labels[k]} not responding", "warn", "source", now,
                              detail=f"{st.get('error') or 'error'} · showing last known data")

    def _derive_nodes(self, now: float, machines, prev: dict) -> None:
        for m in machines:
            if m["role"] == "observer":
                continue
            name, new = m["name"], m["status"]
            old = prev["status"].get(name)
            if old in (None, "unknown") or new == "unknown" or old == new:
                continue
            if new == "unreachable":
                self._add(f"{name}: no kubelet heartbeat", "warn", "node", now, machine=name,
                          detail=f"last heartbeat {m['heartbeat_age_s']:.0f} s ago · Kubernetes still shows Ready")
            elif new == "notready":
                detail = (m.get("kube") or {}).get("message") or "node controller marked the node NotReady"
                off = self.incident.offset(name, m["status_since"])
                if off is not None and off > 0:
                    detail = f"{off:.0f} s after the machine was lost · {detail}"
                self._add(f"{name} NotReady", "error", "node", now, machine=name, detail=detail,
                          ts=m["status_since"] if old == "unreachable" else now)
            elif new == "ready":
                off = self.incident.offset(name, m["status_since"])
                detail = f"node is back · {off:.0f} s after it was lost" if off is not None and off > 0 \
                    else "kubelet heartbeat is back"
                self._add(f"{name} Ready again", "ok", "node", now, machine=name, detail=detail,
                          ts=m["status_since"] if old == "notready" else now)

    def _derive_pods(self, now: float, machines, prev: dict, cur: dict) -> None:
        status_of = {m["name"]: m["status"] for m in machines}
        inc = self.incident.cur
        notready = defaultdict(list)
        for key, p in cur.items():
            o = prev.get(key)
            node, app, name = p["node"], p["app"], p["name"]
            if o is None or (node and not o["node"]):
                if node:
                    self._add(f"{app} pod scheduled on {node}", "info", "pod", now, machine=node, pod=name,
                              detail=name)
                else:
                    self._add(f"{app} pod pending", "info", "pod", now, pod=name, detail=f"{name} · waiting for a node")
                if o is None:
                    continue
            if p["deleting"] and not o["deleting"]:
                lost = status_of.get(node) in ("notready", "unreachable")
                title = f"{app} pod evicted from {node}" if lost else f"{app} pod terminating on {node}"
                self._add(title, "warn", "pod", now, machine=node, pod=name,
                          detail=f"{name} · node not responding" if lost else name)
            elif p["ready"] and not o["ready"]:
                detail = name
                if inc and inc["active"] and node != inc["machine"]:
                    detail += f" · {now - inc['t']['lost']:.0f} s after {inc['machine']} was lost"
                self._add(f"{app} pod Ready on {node}", "ok", "pod", now, machine=node, pod=name, detail=detail)
            elif o["ready"] and not p["ready"] and not p["deleting"]:
                notready[node].append(p)
            if (p["restarts"] or 0) > (o["restarts"] or 0):
                self._add(f"{app} restarted on {node}", "warn", "pod", now, machine=node, pod=name,
                          detail=f"{name} · restart #{p['restarts']}")
        for node, ps in notready.items():
            if len(ps) == 1:
                self._add(f"{ps[0]['app']} pod not ready on {node}", "warn", "pod", now, machine=node,
                          pod=ps[0]["name"], detail=ps[0]["name"])
            else:
                self._add(f"{len(ps)} pods not ready on {node}", "warn", "pod", now, machine=node,
                          detail=", ".join(p["name"] for p in ps))
        for key, o in prev.items():
            if key not in cur:
                self._add(f"{o['app']} pod removed", "info", "pod", now, machine=o["node"], pod=key[1],
                          detail=f"{key[1]} · was on {o['node'] or 'no node'}")

    def _derive_qdrant(self, now: float, qdrant: dict, prev: dict, cur: dict) -> None:
        mof = cur["machine_of"]

        def mname(pid: str) -> str:
            return mof.get(pid) or f"peer {pid[:6]}…"
        for pid, reach in cur["peers"].items():
            old = (prev["peers"] or {}).get(pid)
            if old is True and reach is False:
                self._add(f"Qdrant peer on {mname(pid)} down", "error", "replica", now,
                          machine=mof.get(pid), detail="reads go to the other replica of each shard")
            elif old is False and reach is True:
                self._add(f"Qdrant peer on {mname(pid)} back", "ok", "replica", now, machine=mof.get(pid),
                          detail="peer rejoined the cluster")
        groups = defaultdict(list)
        for key, disp in cur["cells"].items():
            old = prev["cells"].get(key)
            if old is not None and old != disp:
                groups[(key[1], disp)].append(key[0])
        source = {}
        for sh in qdrant["shards"]:
            for r in sh["replicas"]:
                if r.get("from_machine"):
                    source[(sh["id"], r["peer"])] = r["from_machine"]
        for (pid, disp), sids in sorted(groups.items()):
            sids.sort()
            m, n = mname(pid), len(sids)
            what = f"shard {sids[0]}" if n == 1 else "shards " + ", ".join(map(str, sids))
            kw = {"machine": mof.get(pid)}
            if disp == "dead":
                self._add(f"{m}: {_plural(n, 'replica')} Dead", "error", "replica", now,
                          detail=f"{what} · marked Dead by the cluster", **kw)
            elif disp == "offline":
                self._add(f"{m}: {_plural(n, 'replica')} offline", "error", "replica", now,
                          detail=f"{what} · peer not responding", **kw)
            elif disp == "recovering":
                src = source.get((sids[0], pid))
                self._add(f"{m}: {what} recovering", "info", "replica", now,
                          detail=f"streaming from {src}" if src else "shard transfer in progress", **kw)
            elif disp == "partial":
                self._add(f"{m}: {what} partial", "warn", "replica", now, detail="replica is not complete", **kw)
            elif disp == "active":
                self._add(f"{m}: {what} Active", "ok", "replica", now, detail="replica in sync", **kw)
        if prev["healthy"] is False and cur["healthy"]:
            detail = f"collection {qdrant.get('collection')} fully replicated"
            rec = self.incident.recovery_s()
            if rec is not None:
                detail = f"{rec:.0f} s after {self.incident.cur['machine']} came back · {detail}"
            self._add(f"All {qdrant['summary']['total']} replicas Active", "ok", "replica", now, detail=detail)
        if prev["leader"] and cur["leader"] and prev["leader"] != cur["leader"]:
            self._add(f"Raft leader moved to {mname(cur['leader'])}", "info", "replica", now,
                      machine=mof.get(cur["leader"]))

    def _derive_load(self, now: float, load: dict) -> None:
        f = self._lf
        w1, w10 = load["windows"]["1s"], load["windows"]["10s"]
        fail1, user1 = w1["without_retry"]["errors"], w1["with_retry"]["errors"]
        retry_on = (load.get("config") or {}).get("retry") == "other"
        if fail1 and not f["fail"]:
            f["fail"], f["ep_start"] = True, now
            kinds = ", ".join(f"{v} {k}" for k, v in w1["without_retry"]["by_kind"].items() if v)
            if not retry_on:
                title, level = "Requests failing (retry is off)", "error"
            elif user1:
                title, level = "Users getting errors", "error"
                f["user"] = True
            else:
                title, level = "Retry absorbing failed attempts", "warn"
            self._add(title, level, "load", now,
                      detail=f"{fail1} failed in 1 s ({kinds}) · {user1} errors after retry")
        elif f["fail"] and w10["without_retry"]["errors"] == 0:
            f["fail"] = False
            wo = wr = 0
            for pt in load.get("series") or []:
                if pt["t"] >= math.floor(f["ep_start"] or now) - 2:
                    wo += pt.get("errors_without_retry") or 0
                    wr += pt.get("errors_with_retry") or 0
            self._add("No failed attempts for 10 s", "ok", "load", now,
                      detail=f"episode: {wo} failed first attempts · {wr} errors after retry")
        if retry_on and user1 and not f["user"]:
            f["user"] = True
            self._add("Users getting errors", "error", "load", now,
                      detail=f"{user1} requests failed even after retry in 1 s")
        elif f["user"] and w10["with_retry"]["errors"] == 0:
            f["user"] = False
            self._add("No user-facing errors for 10 s", "ok", "load", now)
        p99 = w10["p99_ms"]
        if p99 is None:
            return
        if not f["spike"]:
            base = f["base"] if f["base"] is not None else p99
            if p99 > max(self.cfg.spike_ms, 3 * base):
                f["spike"], f["spike_max"], f["calm_since"] = True, p99, None
                self._add(f"Latency spike: p99 {fmt_ms(p99)}", "warn", "load", now,
                          detail=f"usual p99 ≈ {fmt_ms(base)} · 10 s window")
            elif w10["without_retry"]["errors"] == 0:
                f["base"] = p99 if f["base"] is None else 0.9 * f["base"] + 0.1 * p99
        else:
            f["spike_max"] = max(f["spike_max"] or p99, p99)
            base = f["base"] if f["base"] is not None else p99
            if p99 < max(self.cfg.spike_ms / 2, 2 * base):
                f["calm_since"] = f["calm_since"] or now
                if now - f["calm_since"] >= 5:
                    f["spike"] = False
                    self._add(f"Latency back to normal: p99 {fmt_ms(p99)}", "ok", "load", now,
                              detail=f"peak p99 {fmt_ms(f['spike_max'])}")
            else:
                f["calm_since"] = None


# --- HTTP ---------------------------------------------------------------------------------
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".json": "application/json"}
_INLINE = re.compile(rb"<(script|style)>(.*?)</\1>", re.S)
_TOKEN_IN_URL = re.compile(r"(token=)[^&\s\"]+")
MAX_EVENT_BODY = 64 * 1024


class PanelHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer без обратного DNS при запуске: машинам стенда DNS закрыт, и
    getfqdn(адрес lima0) ждал бы таймаута резолвера."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


def csp_for(html: bytes) -> str:
    """CSP с хэшами встроенных <script> и <style>: чужой код в странице не исполнится."""
    hashes = {b"script": [], b"style": []}
    for tag, body in _INLINE.findall(html):
        hashes[tag].append("'sha256-" + base64.b64encode(hashlib.sha256(body).digest()).decode() + "'")
    script = " ".join(hashes[b"script"]) or "'none'"
    style = " ".join(hashes[b"style"]) or "'none'"
    return (f"default-src 'none'; script-src {script}; style-src {style}; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def make_handler(panel: Panel, static_dir: str, token: str | None = None, verbose: bool = False,
                 events_only: bool = False):
    """Обработчик HTTP. events_only — для адреса, открытого машинам кластера (--event-bind):
    только POST /api/event и только с токеном; страница, /api/state и /healthz там 404."""
    root = os.path.realpath(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "stand-panel/1"
        protocol_version = "HTTP/1.1"
        # TCP_NODELAY: ответ уходит двумя записями (заголовки, тело), и с Nagle тело ждало отложенного
        # подтверждения клиента. Falco шлёт тревоги по одному keep-alive соединению, и каждая после
        # первой стояла 41 мс (замер 19.11: 15 тревог выходили из Falco 0,6 с вместо миллисекунд).
        disable_nagle_algorithm = True

        def version_string(self) -> str:  # без версии Python в заголовке Server
            return self.server_version

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if self.close_connection:
                self.send_header("Connection", "close")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, doc) -> None:
            self._send(code, json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if events_only:
                self._json(404, {"error": "not found"})
            elif path == "/api/state":
                self._send(200, panel.state_bytes(), "application/json; charset=utf-8")
            elif path == "/healthz":
                self._json(200, {"ok": True})
            else:
                self._static(path)

        do_HEAD = do_GET

        def _static(self, path: str) -> None:
            rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
            full = os.path.realpath(os.path.join(root, rel))
            ext = os.path.splitext(full)[1].lower()
            if not full.startswith(root + os.sep) or not os.path.isfile(full) or ext not in STATIC_TYPES:
                self._json(404, {"error": "not found"})
                return
            with open(full, "rb") as f:
                body = f.read()
            extra = {"Content-Security-Policy": csp_for(body)} if ext == ".html" else None
            self._send(200, body, STATIC_TYPES[ext], extra)

        def _authorized(self) -> bool:
            if token:
                got = self.headers.get("X-Panel-Token") or ""
                auth = self.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    got = auth[7:].strip()
                if not got:
                    got = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("token") or [""])[0]
                return hmac.compare_digest(got.encode(), token.encode())
            if events_only:
                return False  # без токена с внешнего адреса не принимаем ничего
            try:
                return ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                return False

        def _drain(self) -> None:
            """Перед отказом дочитать небольшое тело. Иначе закрытие с непрочитанными данными даёт
            клиенту сброс соединения вместо ответа, а при keep-alive остаток тела читается как
            следующий запрос. Большое или непонятное тело не читаем: соединение закрывается."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if self.headers.get("Transfer-Encoding") or not 0 <= n <= MAX_EVENT_BODY:
                self.close_connection = True
            elif n:
                self.rfile.read(n)

        def do_POST(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path != "/api/event":
                self._drain()
                self._json(404, {"error": "not found"})
                return
            if not self._authorized():
                self._drain()
                self._json(403, {"error": "forbidden"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_EVENT_BODY:
                self.close_connection = True
                if MAX_EVENT_BODY < n <= 16 * MAX_EVENT_BODY:
                    self.rfile.read(n)  # дочитать тело: иначе клиент вместо 413 увидит сброс соединения
                self._json(413 if n > MAX_EVENT_BODY else 400, {"error": "body required, at most 64 KiB"})
                return
            try:
                payload = json.loads(self.rfile.read(n))
            except ValueError:
                self._json(400, {"error": "invalid JSON"})
                return
            try:
                items = payload if isinstance(payload, list) else [payload]
                ids = [panel.events.ingest(p)["id"] for p in items[:50]]
            except RateLimited:
                self._json(429, {"error": "too many events"})
                return
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(201, {"ok": True, "ids": ids})

        def log_message(self, fmt: str, *args) -> None:
            if verbose:  # токен из ?token= (Falco) в журнал не пишем
                msg = _TOKEN_IN_URL.sub(r"\1…", fmt % args)
                sys.stderr.write(f"{self.address_string()} - - [{self.log_date_time_string()}] {msg}\n")

    return Handler


# --- запуск -------------------------------------------------------------------------------
def _split_target(s: str) -> tuple[str | None, str]:
    """«machine-1=http://…» -> ("machine-1", "http://…"); «http://…» -> (None, "http://…")."""
    head, sep, tail = s.partition("=")
    if sep and "://" not in head:
        return head.strip() or None, tail.strip()
    return None, s.strip()


def _host_port(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"нужно хост:порт, получено {s!r}")
    return host.strip("[]"), int(port)


def build(args: argparse.Namespace) -> Panel:
    cfg = Config(machines=[m.strip() for m in args.machines.split(",") if m.strip()],
                 observer=args.observer or None, title=args.title, subtitle=args.subtitle,
                 caption=args.caption, lease_stale_s=args.lease_stale, spike_ms=args.spike_ms)
    namespaces = tuple(n.strip() for n in args.kube_namespaces.split(",") if n.strip())
    events = EventsSource()
    if args.mock:
        import mockstand
        mock = mockstand.MockSource(start_offset=args.mock_start, seed=args.mock_seed)
        cfg.mode, cfg.cycle_s = "mock", mockstand.CYCLE
        panel = Panel(cfg, events,
                      load=LoadSource(provider=mock.load_provider),
                      kube=KubeSource(runner=mock.kubectl, namespaces=namespaces),
                      qdrant=QdrantSource(mock.qdrant_targets(), None, mock.collection, fetch=mock.qdrant_fetch),
                      observer=ServiceChecks(mock.observer_checks(), connect=mock.connect),
                      mock=mock)
        mock.replay(panel)
        return panel
    load = LoadSource(path=args.load_stats) if args.load_stats else None
    kube = None
    kubectl = bool(args.kubectl or args.kubeconfig or args.kube_server)
    if args.kube_api:
        if kubectl or args.kube_context:
            raise ValueError("--kube-api ходит в API без kubectl: --kubectl, --kubeconfig, --kube-server "
                             "и --kube-context с ним не задаются")
        if not args.kube_ca or not args.kube_token_file:
            raise ValueError("для --kube-api нужны --kube-ca (CA кластера) и --kube-token-file")
        api = KubeAPI(args.kube_api, args.kube_ca, args.kube_token_file, timeout=args.kube_timeout)
        kube = KubeSource(api=api, timeout=args.kube_timeout, namespaces=namespaces,
                          leases=not args.no_kube_leases)
    elif args.kube_ca or args.kube_token_file:
        raise ValueError("--kube-ca и --kube-token-file нужны только вместе с --kube-api")
    elif kubectl:
        kube = KubeSource(kubectl=args.kubectl or "kubectl", kubeconfig=args.kubeconfig,
                          servers=args.kube_server, context=args.kube_context, timeout=args.kube_timeout,
                          namespaces=namespaces, leases=not args.no_kube_leases)
    qdrant = None
    if args.qdrant:
        if not args.qdrant_collection:
            raise ValueError("для --qdrant нужна --qdrant-collection")
        if not args.qdrant_key_file:
            print("[panel] Qdrant без ключа: так и задумано для шлюза :30633 (deploy/qdrant/gate.py)",
                  file=sys.stderr)
        qdrant = QdrantSource([_split_target(t) for t in args.qdrant], args.qdrant_key_file,
                              args.qdrant_collection, timeout=args.qdrant_timeout,
                              cafile=args.qdrant_cafile, insecure=args.qdrant_insecure)
        for item in args.qdrant_peer:
            pid, _, machine = item.partition("=")
            if not machine:
                raise ValueError(f"--qdrant-peer: нужно peer_id=машина, получено {item!r}")
            cfg.peer_map[pid.strip()] = machine.strip()
    observer = None
    if args.observer_check:
        checks = []
        for item in args.observer_check:
            name, _, hp = item.partition("=")
            host, port = _host_port(hp)
            checks.append((name.strip(), host, port))
        observer = ServiceChecks(checks)
    if not any((load, kube, qdrant, observer)):
        print("[panel] ни одного источника: задайте --load-stats, --kube-api, --kubeconfig, --qdrant или --mock",
              file=sys.stderr)
    return Panel(cfg, events, load=load, kube=kube, qdrant=qdrant, observer=observer)


def listeners(args: argparse.Namespace, panel: Panel, token: str | None) -> list[PanelHTTPServer]:
    """Адреса HTTP: --bind — страница, /api/state и события; --event-bind (если задан) — только
    POST /api/event с токеном. Так страница остаётся на 127.0.0.1, а события Falco принимает
    адрес сети машин. Не открылся какой-то адрес — OSError, открытые закрываются."""
    if args.event_bind and not token:
        raise ValueError("для --event-bind нужен --event-token-file: без токена события с чужого адреса "
                         "не принимаются")
    static = os.path.join(HERE, "static")
    plan = [((args.bind, args.port), make_handler(panel, static, token, args.verbose))]
    if args.event_bind:
        port = args.port if args.event_port is None else args.event_port
        plan.append(((args.event_bind, port), make_handler(panel, static, token, args.verbose, events_only=True)))
    out: list[PanelHTTPServer] = []
    try:
        for addr, handler in plan:
            out.append(PanelHTTPServer(addr, handler))
    except OSError as e:
        for s in out:
            s.server_close()
        raise OSError(f"не открыть {addr[0]}:{addr[1]}: {e.strerror or e}") from None
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Панель стенда «Kubernetes + Qdrant»: агрегатор и статика")
    ap.add_argument("--bind", default="127.0.0.1", help="адрес (по умолчанию 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8090, help="порт (8090)")
    ap.add_argument("--mock", action="store_true", help="сценарий «machine-2 выключена» без кластера")
    ap.add_argument("--mock-start", type=float, default=0.0,
                    help="начать сценарий с N-й секунды цикла; история графика дорисуется")
    ap.add_argument("--mock-seed", type=int, default=7)
    g = ap.add_argument_group("нагрузка")
    g.add_argument("--load-stats", help="JSON генератора нагрузки (loadgen.py --stats-file)")
    g = ap.add_argument_group("Kubernetes")
    g.add_argument("--kube-api", action="append", default=[],
                   help="https://адрес API без kubectl (на стенде https://127.0.0.1:6443, вход HAProxy); "
                        "можно несколько раз — запасные")
    g.add_argument("--kube-ca", help="CA кластера для проверки сертификата API (с --kube-api)")
    g.add_argument("--kube-token-file", help="токен учётной записи только на чтение (с --kube-api)")
    g.add_argument("--kubectl", help="путь к kubectl (по умолчанию ищется в PATH)")
    g.add_argument("--kubeconfig", help="kubeconfig с правами на чтение узлов, подов и leases")
    g.add_argument("--kube-server", action="append", default=[],
                   help="адрес API-сервера; можно несколько раз — запасные на случай отказа")
    g.add_argument("--kube-context")
    g.add_argument("--kube-timeout", type=float, default=4.0, help="жёсткий таймаут kubectl или запроса к API, с (4)")
    g.add_argument("--kube-namespaces", default="search,qdrant",
                   help="пространства имён с подами приложения; остальные — «системные»")
    g.add_argument("--no-kube-leases", action="store_true", help="не читать аренды kubelet (пульс узлов)")
    g = ap.add_argument_group("Qdrant")
    g.add_argument("--qdrant", action="append", default=[],
                   help="[машина=]URL пира Qdrant; по одному на пир, можно несколько раз")
    g.add_argument("--qdrant-key-file", help="файл с ключом Qdrant ТОЛЬКО на чтение")
    g.add_argument("--qdrant-collection", help="коллекция или алиас")
    g.add_argument("--qdrant-peer", action="append", default=[], help="peer_id=машина — явное сопоставление")
    g.add_argument("--qdrant-timeout", type=float, default=1.5)
    g.add_argument("--qdrant-cafile")
    g.add_argument("--qdrant-insecure", action="store_true")
    g = ap.add_argument_group("машины и наблюдатель")
    g.add_argument("--machines", default="machine-1,machine-2,machine-3,machine-4",
                   help="порядок карточек; имена = имена узлов Kubernetes")
    g.add_argument("--observer", default="machine-4", help="машина-наблюдатель (не узел кластера)")
    g.add_argument("--observer-check", action="append", default=[],
                   help="ИМЯ=хост:порт — TCP-проверка службы наблюдателя, например 'HAProxy · TLS 443=127.0.0.1:443'")
    g.add_argument("--lease-stale", type=float, default=15.0, help="пульс старше N с — «нет пульса» (15)")
    g.add_argument("--spike-ms", type=float, default=250.0, help="порог события «всплеск p99», мс (250)")
    g = ap.add_argument_group("события и оформление")
    g.add_argument("--event-token-file", help="токен для POST /api/event; без него — только с localhost")
    g.add_argument("--event-bind", help="второй адрес только для POST /api/event с токеном (например, lima0 "
                                        "machine-4 для Falco); страница там не отдаётся")
    g.add_argument("--event-port", type=int, help="порт для --event-bind (по умолчанию как --port)")
    g.add_argument("--title", default="Kubernetes + Qdrant")
    g.add_argument("--subtitle", default="Search service on a 3-node k3s HA cluster")
    g.add_argument("--caption", default="3 VMs + observer on one laptop · numbers for this hardware")
    g.add_argument("--verbose", action="store_true", help="журнал HTTP-запросов")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        token = None
        if args.event_token_file:
            with open(args.event_token_file, encoding="utf-8") as f:
                token = f.read().strip() or None
        panel = build(args)
        servers = listeners(args, panel, token)
    except (ValueError, OSError) as e:
        print(f"[panel] {e}", file=sys.stderr)
        return 2
    server, extra = servers[0], servers[1:]
    panel.tick()
    panel.start()
    for s in extra:
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.5}, name="panel-events",
                         daemon=True).start()

    def on_term(signum, frame) -> None:  # SIGTERM — так же, как Ctrl+C: закрыть сокет и потоки
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    def url(s: PanelHTTPServer) -> str:
        host = str(s.server_address[0])
        return f"http://{f'[{host}]' if ':' in host else host}:{s.server_address[1]}"
    events = f" · события: {url(extra[0])}/api/event (только с токеном)" if extra else ""
    print(f"[panel] {url(server)}/ · режим {panel.cfg.mode} · "
          f"источники: {', '.join(s.name for s in panel.sources()) or 'нет'}{events}", file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        panel.stop()
        for s in extra:
            s.shutdown()
        for s in servers:
            s.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
