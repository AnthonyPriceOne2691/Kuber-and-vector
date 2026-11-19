"""Маршрутизация по документу и деление вопроса на части: замер на замороженных векторах LegalBench-RAG.

Стенд и новые векторы кусков не нужны: куски, их векторы (нарезка naive500_title, bge-m3) и векторы
вопросов берутся из кэша подготовки (eval/prepare.py), поиск точный, полным перебором в чистом Python.
Новые векторы только у коротких текстов, через Ollama (bge-m3), пакетами и с кэшем на диске:
- название документа в том же виде, что в начале куска: «Document: <название>»;
- части вопроса. Деление детерминированное, по первой «;»: ссылка на документ («Consider the Supply
  Agreement between A and B») и сам вопрос («What is the expiration date of this contract?»). Вопрос
  без «;» идёт как есть, обе части равны ему (сколько таких по наборам, пишется в итог).

Варианты:
  база       вектор всего вопроса, top-k кусков по всему корпусу (как сервис сейчас)
  жёстко     маршрутизатор выбирает n документов (1, 2, 3), куски только из них, по вектору всего вопроса
  мягко      балл куска + λ·балл документа по M лучшим документам (λ = ∞: документ за документом)
  деление а  поиск по каждой части вопроса по всему корпусу и слияние: взвешенная сумма баллов, RRF, максимум
  деление б  ссылка выбирает документы (маршрутизатор), куски внутри них ранжирует сам вопрос
             (или смесь баллов «весь вопрос» и «сам вопрос»), жёстко или мягко
  агрегация  top-200 кусков по всему вопросу, балл документа из баллов его кусков (max, сумма и среднее
             top-3, доля), куски переупорядочиваются с учётом документа
  соседи     поверх лучшего маршрута: балл куска + μ·лучший балл соседнего куска того же документа
Если в выбранных документах меньше k кусков, список добивается следующими документами по порядку
маршрутизатора: у всех вариантов ровно k кусков, и precision сравнима с базой.
Маршрутизатор: представление документа (вектор названия, вектор первого куска с преамбулой и сторонами,
их среднее, средний вектор всех кусков) × сторона вопроса (ссылка, весь вопрос, обе).

Честность:
- вопросы каждого набора делятся пополам с фиксированным зерном: A для подбора, B для проверки;
  маршрутизатор выбирается по точности top-1 на A, параметры каждого семейства по среднему recall
  по k (k = 1…64, как в статье SAC) на A; итог показан на B, все вопросы как справка;
- полный набор: параметры с мини A без нового подбора, вопросы мини A из полного исключены; варианты
  с поиском по всему корпусу (деление а, агрегация, сверка базы) на подвыборке, по 200 вопросов на набор;
- справка на полном: на мини маршрутизатор не ошибается, и мини не различает ширину маршрута (M, λ).
  Поэтому отдельно маршрутизатор, M и λ подбираются на половине A полного, итог на его половине B;
- разница с базой: парный бутстреп на тех же вопросах (2000 повторов, выборка внутри наборов).

    python3 -I -B eval/routing_eval.py mini --cache CACHE --embed-cache EMB
    python3 -I -B eval/routing_eval.py full --cache CACHE --embed-cache EMB --procs 4
    python3 -I -B eval/routing_eval.py report
CACHE это папка cache с подготовленными stage1/ и full/ (читается, не пишется). Итоги прогонов в
runs/routing (в git не идут), отчёт в eval/results/routing.md. Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import operator
import random
import re
import statistics
import sys
import time
from array import array
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from eval import legalbench as lb  # noqa: E402

K = lb.K_VALUES
KMAX = max(K)
DS = lb.DATASETS
NAMES = {"privacy_qa": "PrivacyQA", "contractnli": "ContractNLI", "maud": "MAUD", "cuad": "CUAD"}
INF = float("inf")
SEED = 2026          # деление на половины A и B
BOOT = 2000          # повторов бутстрепа
SAMPLE = 200         # вопросов на набор для вариантов с полным перебором на полном наборе
AGG_N = 200          # кандидатов у агрегации по документам
RRF_N, RRF_K0 = 200, 60
FUSE_N = 1000        # кандидатов из каждой части у слияния на полном наборе (точность проверяется)
TITLE_PREFIX = "Document: "  # как в начале куска ветки naive500_title (eval/prepare.py, index_text)

# math.sumprod (Python 3.12+) считает скалярное произведение в C, в 3 раза быстрее sum(map(mul))
dot = getattr(math, "sumprod", None) or (lambda a, b: sum(map(operator.mul, a, b)))


# ---------- вопросы и корпус ----------

def split_query(q: str) -> tuple[str, str]:
    """Ссылка на документ и сам вопрос: деление по первой «;». Без «;» обе части равны вопросу."""
    ref, sep, ask = q.partition(";")
    ref, ask = ref.strip(), ask.strip()
    if not sep or not ref or not ask:
        return q, q
    return ref, ask


def qkey(q: dict) -> str:
    return json.dumps([q["dataset"], q["query"], q["snippets"]], ensure_ascii=False)


def load_queries(stage: Path, dim: int) -> tuple[list[dict], array]:
    with open(stage / "queries.jsonl", encoding="utf-8") as f:
        queries = [json.loads(line) for line in f]
    qv = array("f")
    with open(stage / "query_vectors.f32", "rb") as f:
        qv.fromfile(f, len(queries) * dim)
    for i, q in enumerate(queries):
        q.pop("sparse", None)
        q["i"] = i
        q["ref"], q["ask"] = split_query(q["query"])
        q["gt"] = [tuple(s) for s in q["snippets"]]
        q["key"] = qkey(q)
    return queries, qv


def halves(queries: list[dict], seed: int = SEED) -> dict[int, str]:
    """Половины A (подбор) и B (проверка) внутри каждого набора; зерно фиксировано."""
    out = {}
    for d in DS:
        idx = [q["i"] for q in queries if q["dataset"] == d]
        random.Random(f"{seed}:{d}").shuffle(idx)
        for j, i in enumerate(idx):
            out[i] = "A" if j < len(idx) // 2 else "B"
    return out


def read_vectors(path: Path, dim: int, lo: int, hi: int) -> list[array]:
    out = []
    with open(path, "rb") as f:
        f.seek(lo * dim * 4)
        for _ in range(lo, hi):
            v = array("f")
            v.fromfile(f, dim)
            out.append(v)
    return out


class Corpus:
    """Куски (адрес и документ) и их векторы; документы в порядке появления, куски документа по смещению."""

    def __init__(self, index: Path, dim: int, with_vectors: bool = True):
        self.dim, self.vec_path = dim, index / "vectors.f32"
        self.files: list[str] = []
        self.title: list[str] = []
        self.doc_id: dict[str, int] = {}
        self.doc_of, self.start, self.end = array("i"), array("i"), array("i")
        with open(index / "points.jsonl", encoding="utf-8") as f:
            for line in f:
                p = json.loads(line)
                d = self.doc_id.get(p["file"])
                if d is None:
                    d = self.doc_id[p["file"]] = len(self.files)
                    self.files.append(p["file"])
                    self.title.append(p["title"])
                self.doc_of.append(d)
                self.start.append(p["start"])
                self.end.append(p["end"])
        self.n = len(self.doc_of)
        self.chunks_of: list[list[int]] = [[] for _ in self.files]
        for c in range(self.n):
            self.chunks_of[self.doc_of[c]].append(c)
        for cs in self.chunks_of:
            cs.sort(key=self.start.__getitem__)
        self.vecs = read_vectors(self.vec_path, dim, 0, self.n) if with_vectors else None

    def first_vectors(self) -> list[list[float]]:
        """Вектор первого куска документа: название, раздел и преамбула со сторонами."""
        return [self.vecs[cs[0]].tolist() for cs in self.chunks_of]

    def centroids(self) -> list[list[float]]:
        out = []
        for cs in self.chunks_of:
            acc = [0.0] * self.dim
            for c in cs:
                acc = list(map(operator.add, acc, self.vecs[c]))
            out.append(unit(acc))
        return out


def unit(v) -> list[float]:
    n = math.sqrt(dot(v, v)) or 1.0
    return [x / n for x in v]


# ---------- метрика ----------

def pr_at_k(corpus: Corpus, ranked: list[int], gt: list[tuple]) -> tuple[list[float], list[float]]:
    """recall и precision при k = 1…64 по первым k кускам, посимвольно, как lb.precision_recall
    (куски не перекрываются, поэтому пересечения можно копить по одному куску)."""
    total_rel = sum(e - s for _, s, e in gt)
    files = {f for f, _, _ in gt}
    rec, prec = [], []
    ov = ln = 0
    ki = 0
    for i, c in enumerate(ranked[:KMAX], 1):
        s, e = corpus.start[c], corpus.end[c]
        f = corpus.files[corpus.doc_of[c]]
        ln += e - s
        if f in files:
            ov += sum(max(0, min(e, ge) - max(s, gs)) for gf, gs, ge in gt if gf == f)
        while ki < len(K) and K[ki] == i:
            rec.append(ov / total_rel if total_rel else 0.0)
            prec.append(ov / ln if ln else 0.0)
            ki += 1
    while ki < len(K):  # кусков меньше k: top-k это все найденные, как в lb.precision_recall
        rec.append(ov / total_rel if total_rel else 0.0)
        prec.append(ov / ln if ln else 0.0)
        ki += 1
    return rec, prec


def mean_over_k(xs: list[float]) -> float:
    return statistics.fmean(xs)


def objective(res: dict[int, tuple], queries: list[dict], subset: set[int]) -> float:
    """Средний recall по k (1…64), среднее по наборам с равными весами, в процентах."""
    per = defaultdict(list)
    for q in queries:
        if q["i"] in subset and q["i"] in res:
            per[q["dataset"]].append(mean_over_k(res[q["i"]][0]))
    return 100 * statistics.fmean(statistics.fmean(v) for v in per.values())


# ---------- баллы ----------

def score_matrix(qlists: list[list[float]], vecs: list[array], block: int = 256) -> list[array]:
    """Баллы всех кусков для каждого вектора (мини: держим целиком, это 70 КБ на вектор)."""
    out = [array("f") for _ in qlists]
    for b in range(0, len(vecs), block):
        blk = [v.tolist() for v in vecs[b:b + block]]
        for o, q in zip(out, qlists):
            o.extend(map(dot, itertools.repeat(q, len(blk)), blk))
    return out


def scan_top(task: tuple) -> list[list[tuple[float, int]]]:
    """Полный перебор куска корпуса [lo, hi): top-N кусков для каждого вектора. Читает векторы
    с диска блоками, поэтому памяти почти не берёт; для нескольких процессов."""
    path, dim, lo, hi, qlists, top_n, block = task
    heaps: list[list[tuple[float, int]]] = [[] for _ in qlists]
    with open(path, "rb") as f:
        f.seek(lo * dim * 4)
        for b in range(lo, hi, block):
            m = min(block, hi - b)
            raw = array("f")
            raw.fromfile(f, m * dim)
            blk = [raw[i * dim:(i + 1) * dim].tolist() for i in range(m)]
            for h, q in zip(heaps, qlists):
                thr = h[0][0] if len(h) >= top_n else -INF
                for j, s in enumerate(map(dot, itertools.repeat(q, m), blk)):
                    if s > thr:
                        if len(h) < top_n:
                            heapq.heappush(h, (s, b + j))
                        else:
                            heapq.heapreplace(h, (s, b + j))
                        if len(h) >= top_n:
                            thr = h[0][0]
    return [sorted(h, reverse=True) for h in heaps]


def scan(path: Path, dim: int, n: int, qlists: list[list[float]], top_n: int, procs: int) -> list[list]:
    tasks = [(str(path), dim, n * i // procs, n * (i + 1) // procs, qlists, top_n, 256) for i in range(procs)]
    if procs == 1:
        parts = [scan_top(tasks[0])]
    else:
        import multiprocessing as mp
        with mp.get_context("spawn").Pool(procs) as pool:
            parts = pool.map(scan_top, tasks)
    return [heapq.nlargest(top_n, itertools.chain.from_iterable(p[i] for p in parts)) for i in range(len(qlists))]


class Scores:
    """Баллы кусков по вектору (весь вопрос "f", сам вопрос "s", ссылка "p"): из полной матрицы (мини),
    из top-N полного перебора или по требованию по документу (полный набор)."""

    def __init__(self, corpus: Corpus, vec_of, arrays: dict | None = None, tops: dict | None = None):
        self.corpus, self.vec_of = corpus, vec_of
        self.arrays, self.tops = arrays or {}, tops or {}
        self._doc: dict = {}
        self._top: dict = {}
        self._topd: dict = {}
        self.inexact = 0  # слияний, где кандидатов могло не хватить (на полном наборе)

    def doc(self, key, d: int):
        S = self.arrays.get(key)
        if S is not None:
            return [S[c] for c in self.corpus.chunks_of[d]]
        r = self._doc.get((key, d))
        if r is None:
            if len(self._doc) > 50_000:
                self._doc.clear()
            q, vecs = self.vec_of(key), self.corpus.vecs
            r = self._doc[(key, d)] = array("f", (dot(q, vecs[c]) for c in self.corpus.chunks_of[d]))
        return r

    def top(self, key, n: int) -> list[tuple[float, int]]:
        S = self.arrays.get(key)
        if S is not None:
            t = self._top.get(key)
            if t is None or len(t) < n:
                t = self._top[key] = [(S[c], c) for c in heapq.nlargest(max(n, FUSE_N), range(len(S)), key=S.__getitem__)]
            return t[:n]
        t = self.tops[key]
        if len(t) < n and len(t) < self.corpus.n:
            raise ValueError(f"top-{n} не посчитан для {key}")
        return t[:n]

    def score(self, key, c: int) -> float:
        S = self.arrays.get(key)
        if S is not None:
            return S[c]
        d = self._topd.get(key)
        if d is None:
            d = self._topd[key] = {cc: s for s, cc in self.tops.get(key, ())}
        s = d.get(c)
        return s if s is not None else dot(self.vec_of(key), self.corpus.vecs[c])

    def full(self, key) -> bool:
        return key in self.arrays


# ---------- маршрутизатор ----------

REPS = ("title", "first", "title+first", "centroid")
SIDES = ("ref", "full", "ref+full")
ROUTERS = tuple(f"{r}|{s}" for r in REPS for s in SIDES)
REP_RU = {"title": "название", "first": "первый кусок", "title+first": "название + первый кусок",
          "centroid": "средний вектор кусков"}
SIDE_RU = {"ref": "ссылка", "full": "весь вопрос", "ref+full": "ссылка + весь вопрос"}


class Router:
    """Баллы документов: косинус стороны вопроса с представлением документа; составные — среднее баллов."""

    def __init__(self, reps: dict[str, list[list[float]]], ref_vec, full_vec):
        self.reps, self.ref_vec, self.full_vec = reps, ref_vec, full_vec
        self._ref: dict = {}   # по тексту ссылки: у многих вопросов она общая
        self._full: dict = {}  # только для текущего вопроса
        self._full_i = None

    def scores(self, q: dict, router: str) -> list[float]:
        rep, side = router.split("|")
        lists = [self._base(q, r, s) for r in rep.split("+") for s in side.split("+")]
        if len(lists) == 1:
            return lists[0]
        return [sum(t) / len(t) for t in zip(*lists)]

    def _base(self, q: dict, rep: str, side: str) -> list[float]:
        if side == "ref":
            cache, key = self._ref, (rep, q["ref"])
        else:
            if q["i"] != self._full_i:
                self._full, self._full_i = {}, q["i"]
            cache, key = self._full, rep
        r = cache.get(key)
        if r is None:
            v = self.ref_vec(q) if side == "ref" else self.full_vec(q)
            r = cache[key] = [dot(v, x) for x in self.reps[rep]]
        return r


def gold_doc(corpus: Corpus, q: dict) -> int | None:
    return corpus.doc_id.get(q["gt"][0][0])


def router_errors(corpus: Corpus, queries: list[dict], router: "Router", name: str, everyone: list[dict]) -> dict:
    """Ошибки top-1 маршрутизатора по наборам и сколько из них у ссылок, общих для нескольких документов
    (такой вопрос сам не отличает документы; общие ссылки ищутся по всем вопросам набора)."""
    docs_of_ref: dict = defaultdict(set)
    for q in everyone:
        docs_of_ref[(q["dataset"], q["ref"])].add(q["gt"][0][0])
    out = {d: {"n": 0, "wrong": 0, "shared_ref": 0} for d in DS}
    for q in queries:
        o = out[q["dataset"]]
        o["n"] += 1
        if route_rank_of_gold(router.scores(q, name), gold_doc(corpus, q)) > 0:
            o["wrong"] += 1
            o["shared_ref"] += len(docs_of_ref[(q["dataset"], q["ref"])]) > 1
    return {d: v for d, v in out.items() if v["n"]}


def route_rank_of_gold(scores: list[float], g: int | None) -> int:
    if g is None:
        return 10 ** 9
    s = scores[g]
    return sum(1 for x in scores if x > s)


# ---------- варианты ----------

def cfg_name(c: dict) -> str:
    f = c["family"]
    if f == "base":
        return "база"
    if f == "route":
        lam = "∞" if c["lam"] == INF else f"{c['lam']:g}"
        s = f"маршрут M={c['M']} λ={lam} a={c['a']:g}"
        return s + (f" μ={c['mu']:g}" if c.get("mu") else "")
    if f == "fuse":
        return f"слияние {c['how']}" + (f" w={c['w']:g}" if c["how"] == "wsum" else "")
    if f == "agg":
        lam = "∞" if c["lam"] == INF else f"{c['lam']:g}"
        return f"агрегация {c['how']} λ={lam}"
    raise ValueError(f)


def route_cfg(M: int, lam: float, a: float, mu: float = 0.0) -> dict:
    return {"family": "route", "M": M, "lam": lam, "a": a, "mu": mu}


LAMS = (0.25, 0.5, 1.0, 2.0, 4.0, INF)
MS = (2, 3, 5, 10)
MUS = (0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5)


def families() -> dict[str, list[dict]]:
    """Сетки семейств. a: доля балла «весь вопрос» в балле куска (1 весь вопрос, 0 сам вопрос)."""
    split_b = [route_cfg(1, 0.0, a) for a in (0.0, 0.25, 0.5, 0.75)]
    split_b += [route_cfg(m, lam, a) for a in (0.0, 0.25, 0.5, 0.75) for m in MS for lam in (0.0, *LAMS)]
    return {
        "hard": [route_cfg(n, 0.0, 1.0) for n in (1, 2, 3)],
        "soft": [route_cfg(m, lam, 1.0) for m in MS for lam in LAMS],
        "split_a": [{"family": "fuse", "how": "wsum", "w": w / 10} for w in range(11)]
                   + [{"family": "fuse", "how": "rrf"}, {"family": "fuse", "how": "max"}],
        "split_b": split_b,
        "agg": [{"family": "agg", "how": h, "lam": lam} for h in ("max", "sum3", "mean3", "frac") for lam in LAMS],
    }


FAMILY_RU = {"base": "база", "hard": "жёстко", "soft": "мягко", "split_a": "деление а", "split_b": "деление б",
             "agg": "агрегация", "neighbors": "соседи"}


def smooth(s, mu: float) -> list[float]:
    """Балл куска + μ·лучший балл соседа (предыдущий или следующий кусок того же документа)."""
    n = len(s)
    out = []
    for i, x in enumerate(s):
        nb = max(s[i - 1] if i > 0 else -INF, s[i + 1] if i + 1 < n else -INF)
        out.append(x + mu * (nb if nb > -INF else x))
    return out


class QueryCtx:
    """Всё, что вариантам нужно про один вопрос: порядок документов маршрутизатора и баллы кусков."""

    def __init__(self, corpus: Corpus, scores: Scores, router: Router, router_name: str):
        self.corpus, self.sc, self.router, self.router_name = corpus, scores, router, router_name

    def set(self, q: dict) -> "QueryCtx":
        self.q = q
        self.dsc = self.router.scores(q, self.router_name)
        self.order = sorted(range(len(self.dsc)), key=self.dsc.__getitem__, reverse=True)
        self.keys = {"f": ("f", q["i"]), "s": ("s", q["ask"]), "p": ("p", q["ref"])}
        self._mix: dict = {}
        return self

    def mixed(self, d: int, a: float, mu: float):
        key = (d, a, mu)
        r = self._mix.get(key)
        if r is None:
            if a >= 1:
                r = self.sc.doc(self.keys["f"], d)
            elif a <= 0:
                r = self.sc.doc(self.keys["s"], d)
            else:
                r = [a * x + (1 - a) * y for x, y in zip(self.sc.doc(self.keys["f"], d), self.sc.doc(self.keys["s"], d))]
            if mu:
                r = smooth(r, mu)
            self._mix[key] = r
        return r


def rank(cfg: dict, ctx: QueryCtx, k: int = KMAX) -> list[int]:
    f = cfg["family"]
    if f == "base":
        return [c for _, c in ctx.sc.top(ctx.keys["f"], k)]
    if f == "route":
        return rank_route(cfg, ctx, k)
    if f == "fuse":
        return rank_fuse(cfg, ctx, k)
    if f == "agg":
        return rank_agg(cfg, ctx, k)
    raise ValueError(f)


def rank_route(cfg: dict, ctx: QueryCtx, k: int = KMAX) -> list[int]:
    M, lam, a, mu = cfg["M"], cfg["lam"], cfg["a"], cfg.get("mu", 0.0)
    chunks_of = ctx.corpus.chunks_of
    items = []
    for r, d in enumerate(ctx.order[:M]):
        s = ctx.mixed(d, a, mu)
        if lam == INF:  # документ за документом: сначала порядок документа, потом балл куска
            items.extend(zip(itertools.repeat(-r), s, chunks_of[d]))
        else:
            bonus = lam * ctx.dsc[d]
            items.extend(zip((x + bonus for x in s), chunks_of[d]))
    out = [t[-1] for t in heapq.nlargest(k, items)]
    if len(out) < k:  # добивка следующими документами маршрутизатора
        for d in ctx.order[M:]:
            s = ctx.mixed(d, a, mu)
            out.extend(c for _, c in sorted(zip(s, chunks_of[d]), reverse=True))
            if len(out) >= k:
                break
    return out[:k]


def rank_fuse(cfg: dict, ctx: QueryCtx, k: int = KMAX) -> list[int]:
    sc, kp, ks = ctx.sc, ctx.keys["p"], ctx.keys["s"]
    how = cfg["how"]
    if how == "rrf":
        fused: dict[int, float] = defaultdict(float)
        for lst in (sc.top(kp, RRF_N), sc.top(ks, RRF_N)):
            for r, (_, c) in enumerate(lst, 1):
                fused[c] += 1.0 / (RRF_K0 + r)
        return [c for c, _ in heapq.nlargest(k, fused.items(), key=operator.itemgetter(1))]
    w = cfg.get("w", 0.5)
    comb = (lambda p, s: w * p + (1 - w) * s) if how == "wsum" else max
    if sc.full(kp) and sc.full(ks):  # мини: точно по всем кускам
        P, S = sc.arrays[kp], sc.arrays[ks]
        return heapq.nlargest(k, range(len(P)), key=lambda c: comb(P[c], S[c]))
    tp, ts = sc.top(kp, FUSE_N), sc.top(ks, FUSE_N)
    cand = {c for _, c in tp} | {c for _, c in ts}
    top = heapq.nlargest(k, ((comb(sc.score(kp, c), sc.score(ks, c)), c) for c in cand))
    # кусок вне обоих top-N не выше comb(N-й балл ссылки, N-й балл вопроса): иначе результат не точный
    if len(top) == k and len(tp) == FUSE_N and len(ts) == FUSE_N and top[-1][0] < comb(tp[-1][0], ts[-1][0]):
        sc.inexact += 1
    return [c for _, c in top]


AGG = {
    "max": lambda v: v[0],
    "sum3": lambda v: sum(v[:3]),
    "mean3": lambda v: statistics.fmean(v[:3]),
    "frac": lambda v: len(v) / AGG_N,
}


def rank_agg(cfg: dict, ctx: QueryCtx, k: int = KMAX) -> list[int]:
    cand = ctx.sc.top(ctx.keys["f"], AGG_N)
    doc_of = ctx.corpus.doc_of
    by: dict[int, list[float]] = defaultdict(list)
    for s, c in cand:  # кандидаты по убыванию балла, списки документов тоже
        by[doc_of[c]].append(s)
    agg = {d: AGG[cfg["how"]](v) for d, v in by.items()}
    lam = cfg["lam"]
    if lam == INF:
        items = ((agg[doc_of[c]], s, c) for s, c in cand)
    else:
        items = ((s + lam * agg[doc_of[c]], c) for s, c in cand)
    return [t[-1] for t in heapq.nlargest(k, items)]


def run_configs(cfgs: list[dict], queries: list[dict], ctx: QueryCtx) -> dict[str, dict[int, tuple]]:
    res: dict[str, dict[int, tuple]] = {cfg_name(c): {} for c in cfgs}
    for q in queries:
        ctx.set(q)
        for c in cfgs:
            res[cfg_name(c)][q["i"]] = pr_at_k(ctx.corpus, rank(c, ctx), q["gt"])
    return res


# ---------- бутстреп ----------

def boot(series: dict[str, dict[str, list[float]]], b: int = BOOT, seed: int = 0) -> dict[str, tuple[float, float, float]]:
    """Среднее по наборам (наборы с равными весами) и 95 % интервал бутстрепом по вопросам внутри
    наборов. Все ряды считаются на одних и тех же выборках, поэтому ряд разностей двух вариантов
    по тем же вопросам даёт парный бутстреп (как eval/report.py)."""
    names = list(series)
    dss = [d for d in DS if series[names[0]].get(d)]
    point = {n: statistics.fmean(statistics.fmean(series[n][d]) for d in dss) for n in names}
    rng = random.Random(seed)
    sims: dict[str, list[float]] = {n: [] for n in names}
    for _ in range(b):
        acc = dict.fromkeys(names, 0.0)
        for d in dss:
            m = len(series[names[0]][d])
            idx = rng.choices(range(m), k=m)
            for n in names:
                acc[n] += sum(map(series[n][d].__getitem__, idx)) / m
        for n in names:
            sims[n].append(acc[n] / len(dss))
    out = {}
    for n in names:
        s = sorted(sims[n])
        out[n] = (point[n], s[int(0.025 * b)], s[min(b - 1, int(0.975 * b))])
    return out


# ---------- части вопроса через Ollama ----------

def ensure_in_portions(emb, texts: list[str], pause: float, portion: int = 160, progress: bool = False) -> None:
    """Векторы, которых ещё нет в кэше, порциями с паузой: через ту же Ollama ходит живой стенд."""
    todo = [t for t in dict.fromkeys(texts) if emb.key(t) not in emb.index]
    for j in range(0, len(todo), portion):
        if j:
            time.sleep(pause)
        emb.ensure(todo[j:j + portion], progress=False)
        if progress and (j // portion) % 10 == 9:
            rate = emb.computed / emb.seconds if emb.seconds else 0.0
            print(f"  векторы: {min(j + portion, len(todo))}/{len(todo)}, {rate:.1f} текстов/с", flush=True)


def embed_texts(texts: list[str], args) -> tuple[dict[str, list[float]], dict]:
    """Векторы коротких текстов (пакетами по 32, кэш на диске). Возвращает нормированные списки."""
    from app.embedding import OllamaEmbedder
    emb = OllamaEmbedder(model=args.embed_model, url=args.ollama, cache_dir=args.embed_cache)
    try:
        before = emb.computed
        ensure_in_portions(emb, texts, args.embed_pause)
        out = {t: unit(emb.vector(t)) for t in set(texts)}
        stats = {"texts": len(out), "computed": emb.computed - before, "seconds": round(emb.seconds, 2)}
    finally:
        emb.close()
    return out, stats


def parts_vectors(corpus: Corpus, queries: list[dict], qv: array, args) -> tuple[dict, dict]:
    """Векторы названий, ссылок и самих вопросов; сверка: вектор одного вопроса заново против кэша.
    Префиксы модели (args.query_prefix у ссылки и вопроса, args.doc_prefix у названия) по умолчанию
    пустые, как у bge-m3; другие модели передают свои (eval/models_eval.py)."""
    qp, dp = getattr(args, "query_prefix", ""), getattr(args, "doc_prefix", "")
    titles = [dp + TITLE_PREFIX + t for t in corpus.title]
    refs = sorted({q["ref"] for q in queries})
    asks = sorted({q["ask"] for q in queries})
    probe = qp + queries[0]["query"]
    vec, stats = embed_texts(titles + [qp + r for r in refs] + [qp + a for a in asks] + [probe], args)
    dim = corpus.dim
    stats["probe_cos"] = round(dot(vec[probe], unit(qv[0:dim])), 6)
    return {"title": [vec[t] for t in titles], "ref": {r: vec[qp + r] for r in refs},
            "ask": {a: vec[qp + a] for a in asks}}, stats


def no_semicolon(queries: list[dict]) -> dict[str, int]:
    return {d: sum(1 for q in queries if q["dataset"] == d and ";" not in q["query"]) for d in DS}


# ---------- замер времени ----------

def pipeline_base(corpus: Corpus, qf: list[float], k: int = KMAX) -> list[int]:
    n = corpus.n
    return [c for _, c in heapq.nlargest(k, zip(map(dot, itertools.repeat(qf, n), corpus.vecs), range(n)))]


def pipeline_route(corpus: Corpus, reps: dict, router_name: str, cfg: dict,
                   qf: list[float], qref: list[float], qask: list[float], k: int = KMAX) -> list[int]:
    """Маршрут с нуля, без кэшей: так он работал бы на запрос (векторы частей уже посчитаны)."""
    rep, side = router_name.split("|")
    sides = {"ref": qref, "full": qf}
    lists = [[dot(sides[s], x) for x in reps[r]] for r in rep.split("+") for s in side.split("+")]
    dsc = lists[0] if len(lists) == 1 else [sum(t) / len(t) for t in zip(*lists)]
    order = sorted(range(len(dsc)), key=dsc.__getitem__, reverse=True)
    M, lam, a, mu = cfg["M"], cfg["lam"], cfg["a"], cfg.get("mu", 0.0)

    def mixed(d):
        cs = corpus.chunks_of[d]
        sf = [dot(qf, corpus.vecs[c]) for c in cs] if a > 0 else None
        ss = [dot(qask, corpus.vecs[c]) for c in cs] if a < 1 else None
        s = sf if ss is None else ss if sf is None else [a * x + (1 - a) * y for x, y in zip(sf, ss)]
        return smooth(s, mu) if mu else s

    items = []
    for r, d in enumerate(order[:M]):
        s = mixed(d)
        if lam == INF:
            items.extend(zip(itertools.repeat(-r), s, corpus.chunks_of[d]))
        else:
            items.extend(zip((x + lam * dsc[d] for x in s), corpus.chunks_of[d]))
    out = [t[-1] for t in heapq.nlargest(k, items)]
    for d in order[M:]:
        if len(out) >= k:
            break
        out.extend(c for _, c in sorted(zip(mixed(d), corpus.chunks_of[d]), reverse=True))
    return out[:k]


# ---------- мини ----------

def setup(stage: Path, args) -> tuple:
    meta = json.loads((stage / "meta.json").read_text())
    dim = meta["dim"]
    t0 = time.perf_counter()
    corpus = Corpus(stage / "naive500_title", dim)
    queries, qv = load_queries(stage, dim)
    print(f"{stage.name}: кусков {corpus.n}, документов {len(corpus.files)}, вопросов {len(queries)}, "
          f"загрузка {time.perf_counter() - t0:.0f} с", flush=True)
    parts, estats = parts_vectors(corpus, queries, qv, args)
    print(f"  векторы частей: текстов {estats['texts']}, новых {estats['computed']}, "
          f"сверка вектора вопроса cos = {estats['probe_cos']}", flush=True)
    t0 = time.perf_counter()
    reps = {"title": parts["title"], "first": corpus.first_vectors(), "centroid": corpus.centroids()}
    print(f"  представления документов {time.perf_counter() - t0:.0f} с", flush=True)
    full_vec = lambda q: qv[q["i"] * dim:(q["i"] + 1) * dim].tolist()  # noqa: E731
    router = Router(reps, lambda q: parts["ref"][q["ref"]], full_vec)

    def vec_of(key):
        kind, x = key
        return full_vec({"i": x}) if kind == "f" else parts["ask"][x] if kind == "s" else parts["ref"][x]
    return meta, dim, corpus, queries, qv, parts, estats, reps, router, vec_of, full_vec


def routing_table(corpus: Corpus, queries: list[dict], router: Router, groups: dict[str, set[int]]) -> dict:
    """Доля вопросов, у которых эталонный документ в top-1/2/3 маршрутизатора (среднее по наборам и по наборам)."""
    ranks = {r: {} for r in ROUTERS}
    for q in queries:
        g = gold_doc(corpus, q)
        for r in ROUTERS:
            ranks[r][q["i"]] = route_rank_of_gold(router.scores(q, r), g)
    out = {}
    for r in ROUTERS:
        out[r] = {}
        for gname, subset in groups.items():
            per = defaultdict(list)
            for q in queries:
                if q["i"] in subset:
                    per[q["dataset"]].append(ranks[r][q["i"]])
            by_ds = {d: [100 * statistics.fmean(1.0 if x < n else 0.0 for x in v) for n in (1, 2, 3)]
                     for d, v in per.items()}
            out[r][gname] = {"mean": [statistics.fmean(v[j] for v in by_ds.values()) for j in range(3)],
                             "by_dataset": by_ds}
    return out


def pack(res: dict[int, tuple], ids: list[int]) -> dict:
    return {"R": [[round(x, 6) for x in res[i][0]] for i in ids], "P": [[round(x, 6) for x in res[i][1]] for i in ids]}


def run_mini(args) -> None:
    stage = Path(args.cache) / "stage1"
    meta, dim, corpus, queries, qv, parts, estats, reps, router, vec_of, full_vec = setup(stage, args)
    half = halves(queries)
    A = {i for i, h in half.items() if h == "A"}
    B = {i for i, h in half.items() if h == "B"}
    ALL = set(half)

    t0 = time.perf_counter()
    keys = [("f", q["i"]) for q in queries] + [("s", a) for a in sorted(parts["ask"])] + [("p", r) for r in sorted(parts["ref"])]
    mats = score_matrix([vec_of(k) for k in keys], corpus.vecs)
    scores = Scores(corpus, vec_of, arrays=dict(zip(keys, mats)))
    print(f"  баллы всех кусков для {len(keys)} векторов: {time.perf_counter() - t0:.0f} с", flush=True)

    t0 = time.perf_counter()
    rtab = routing_table(corpus, queries, router, {"A": A, "B": B, "all": ALL})
    best_router = max(ROUTERS, key=lambda r: (rtab[r]["A"]["mean"][0], statistics.fmean(rtab[r]["A"]["mean"]), r))
    print(f"  маршрутизатор по top-1 на A: {best_router} "
          f"({rtab[best_router]['A']['mean'][0]:.1f} %), {time.perf_counter() - t0:.0f} с", flush=True)

    ctx = QueryCtx(corpus, scores, router, best_router)
    qa = [q for q in queries if q["i"] in A]
    fams = families()
    grid, selected = {}, {}
    for fam, cfgs in fams.items():
        t0 = time.perf_counter()
        res = run_configs(cfgs, qa, ctx)
        ranked = sorted(((objective(r, queries, A), name) for name, r in res.items()), reverse=True)
        best = next(c for c in cfgs if cfg_name(c) == ranked[0][1])
        grid[fam] = {"tried": len(cfgs), "top": [[n, round(o, 3)] for o, n in ranked]}
        selected[fam] = best
        print(f"  {fam}: перебрано {len(cfgs)} за {time.perf_counter() - t0:.0f} с, лучший на A "
              f"{ranked[0][1]} ({ranked[0][0]:.2f})", flush=True)
    # соседи поверх лучшего маршрута (жёстко, мягко или деление б)
    route_fams = ("hard", "soft", "split_b")
    best_route_fam = max(route_fams, key=lambda f: next(o for n, o in grid[f]["top"] if n == cfg_name(selected[f])))
    parent = selected[best_route_fam]
    nb_cfgs = [dict(parent, mu=mu) for mu in MUS]
    res = run_configs(nb_cfgs, qa, ctx)
    ranked = sorted(((objective(r, queries, A), name) for name, r in res.items()), reverse=True)
    selected["neighbors"] = next(c for c in nb_cfgs if cfg_name(c) == ranked[0][1])
    grid["neighbors"] = {"tried": len(nb_cfgs), "parent": cfg_name(parent), "top": [[n, round(o, 3)] for o, n in ranked]}
    print(f"  соседи поверх {cfg_name(parent)}: лучший {ranked[0][1]} ({ranked[0][0]:.2f})", flush=True)

    # варианты для отчёта: база, жёстко top-1/2/3, победители семейств; на всех вопросах
    show = {"база": {"family": "base"}}
    for n in (1, 2, 3):
        show[f"жёстко top-{n}"] = route_cfg(n, 0.0, 1.0)
    for fam in ("soft", "split_a", "split_b", "agg"):
        show[FAMILY_RU[fam]] = selected[fam]
    show[f"{FAMILY_RU[best_route_fam]} + соседи"] = selected["neighbors"]
    a_score = {}
    t0 = time.perf_counter()
    allres = run_configs(list({cfg_name(c): c for c in show.values()}.values()), queries, ctx)
    for label, c in show.items():
        a_score[label] = objective(allres[cfg_name(c)], queries, A)
    candidates = [lbl for lbl in show if lbl != "база"]
    best_label = max(candidates, key=lambda lbl: a_score[lbl])
    print(f"  варианты на всех вопросах {time.perf_counter() - t0:.0f} с; лучший на A: {best_label} "
          f"({cfg_name(show[best_label])}, {a_score[best_label]:.2f})", flush=True)

    # сверка: свой точный поиск против Qdrant (exact) по тем же вопросам
    check = base_check(allres["база"], queries, Path(args.cache) / "stage1/results/result_naive500_title_dense.json")
    # сверка: маршрут с нуля (как на запрос) против маршрута из готовых баллов
    timing = time_pipelines(corpus, queries, reps, best_router, show[best_label] if show[best_label]["family"] == "route"
                            else selected[best_route_fam], parts, full_vec, ctx, n=args.time_n)
    ids = [q["i"] for q in queries]
    out = {
        "stage": "mini", "meta": {"chunks": corpus.n, "docs": len(corpus.files), "queries": len(queries),
                                  "model": meta["model"], "seed": SEED, "embed": estats,
                                  "no_semicolon": no_semicolon(queries)},
        "queries": [{"i": q["i"], "dataset": q["dataset"], "half": half[q["i"]], "key": q["key"]} for q in queries],
        "routing": rtab, "router": best_router, "grid": grid,
        "selected": {f: c for f, c in selected.items()}, "best_route_family": best_route_fam,
        "show": show, "a_score": a_score, "best": best_label, "base_check": check, "timing": timing,
        "variants": {label: pack(allres[cfg_name(c)], ids) for label, c in show.items()},
    }
    save(Path(args.work) / "mini.json", out)


def base_check(res: dict[int, tuple], queries: list[dict], path: Path) -> dict | None:
    """Свой точный поиск против Qdrant exact: доля вопросов с тем же recall@k при всех k."""
    if not path.exists():
        return None
    r = load_json(path)
    pos = defaultdict(int)
    same, diff = 0, []
    for q in queries:
        d = q["dataset"]
        j = pos[d]
        pos[d] += 1
        qd = [r["per_query"][d][str(k)][j][1] for k in K]
        mine = res[q["i"]][0]
        if all(abs(a - b) < 1e-6 for a, b in zip(qd, mine)):
            same += 1
        diff.append(statistics.fmean(mine) - statistics.fmean(qd))
    return {"queries": len(queries), "same_recall_all_k": same,
            "mean_diff_mean_recall_pp": round(100 * statistics.fmean(diff), 4)}


def time_pipelines(corpus: Corpus, queries: list[dict], reps: dict, router_name: str, cfg: dict,
                   parts: dict, full_vec, ctx: QueryCtx, n: int = 50, base_n: int | None = None) -> dict:
    """Время на вопрос в чистом Python, один процесс: база (перебор всех кусков) и маршрут с нуля.
    Заодно сверка: маршрут с нуля даёт тот же список, что маршрут из готовых баллов."""
    rng = random.Random(1)
    sample = rng.sample(queries, min(n, len(queries)))
    t_route, same = [], 0
    for q in sample:
        qf, qref, qask = full_vec(q), parts["ref"][q["ref"]], parts["ask"][q["ask"]]
        t0 = time.perf_counter()
        got = pipeline_route(corpus, reps, router_name, cfg, qf, qref, qask)
        t_route.append(time.perf_counter() - t0)
        same += got == rank(cfg, ctx.set(q))
    t_base = []
    for q in sample[:base_n or n]:
        qf = full_vec(q)
        t0 = time.perf_counter()
        pipeline_base(corpus, qf)
        t_base.append(time.perf_counter() - t0)
    return {"route_cfg": cfg_name(cfg), "router": router_name, "n_route": len(t_route), "n_base": len(t_base),
            "route_ms_p50": round(1000 * statistics.median(t_route), 2),
            "route_ms_mean": round(1000 * statistics.fmean(t_route), 2),
            "base_ms_p50": round(1000 * statistics.median(t_base), 2),
            "base_ms_mean": round(1000 * statistics.fmean(t_base), 2),
            "route_same_as_cached": same}


def save(path: Path, obj: dict) -> None:
    """Итог прогона в JSON; λ = ∞ модуль json пишет как Infinity и сам же читает обратно."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    print(f"итог: {path}", flush=True)


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------- полный набор ----------

def run_full(args) -> None:
    mini = load_json(Path(args.work) / "mini.json")
    stage = Path(args.cache) / "full"
    meta, dim, corpus, queries, qv, parts, estats, reps, router, vec_of, full_vec = setup(stage, args)
    tuned = {q["key"] for q in mini["queries"] if q["half"] == "A"}
    elig = [q for q in queries if q["key"] not in tuned]
    found_tuned = sum(1 for q in queries if q["key"] in tuned)
    print(f"  вопросов мини A в полном {found_tuned} из {len(tuned)}; без них {len(elig)}", flush=True)
    rng = random.Random(SEED)
    sub = []
    for d in DS:
        pool = [q for q in elig if q["dataset"] == d]
        sub += sorted(rng.sample(pool, min(args.sample, len(pool))), key=lambda q: q["i"])
    sub_ids = {q["i"] for q in sub}

    t0 = time.perf_counter()
    groups = {"all": {q["i"] for q in elig}}
    rtab = routing_table(corpus, elig, router, groups)
    rerr = router_errors(corpus, elig, router, mini["router"], queries)
    print(f"  точность маршрутизаторов {time.perf_counter() - t0:.0f} с; выбранный {mini['router']}: "
          f"top-1 {rtab[mini['router']]['all']['mean'][0]:.1f} %, ошибки {rerr}", flush=True)

    # варианты с полным перебором: база для сверки, слияние частей, агрегация — на подвыборке
    show = mini["show"]
    need_f = any(c["family"] in ("base", "agg") for c in show.values())
    need_ps = any(c["family"] == "fuse" for c in show.values())
    vecs_keys = [("f", q["i"]) for q in sub] if need_f else []
    if need_ps:
        vecs_keys += [("p", r) for r in sorted({q["ref"] for q in sub})] + [("s", a) for a in sorted({q["ask"] for q in sub})]
    t0 = time.perf_counter()
    tops_list = scan(corpus.vec_path, dim, corpus.n, [vec_of(k) for k in vecs_keys], FUSE_N, args.procs)
    tops = dict(zip(vecs_keys, tops_list))
    scan_s = time.perf_counter() - t0
    print(f"  полный перебор: {len(vecs_keys)} векторов, {args.procs} процесса, {scan_s:.0f} с", flush=True)
    scores = Scores(corpus, vec_of, tops=tops)
    ctx = QueryCtx(corpus, scores, router, mini["router"])

    route_like = {lbl: c for lbl, c in show.items() if c["family"] == "route"}
    global_like = {lbl: c for lbl, c in show.items() if c["family"] != "route"}
    t0 = time.perf_counter()
    res_route = run_configs(list(route_like.values()), elig, ctx)
    route_s = time.perf_counter() - t0
    print(f"  маршруты на {len(elig)} вопросах: {route_s:.0f} с", flush=True)
    t0 = time.perf_counter()
    res_global = run_configs(list(global_like.values()), sub, ctx)
    print(f"  варианты с перебором на подвыборке {len(sub)}: {time.perf_counter() - t0:.0f} с; "
          f"неточных слияний {scores.inexact}", flush=True)

    qdrant = qdrant_base(Path(args.cache) / "full/results/result_full.json", queries)
    check = None
    if "база" in global_like:
        mine = res_global[cfg_name(global_like["база"])]
        same = sum(1 for i in sub_ids if all(abs(a - b) < 1e-6 for a, b in zip(mine[i][0], qdrant[i][0])))
        check = {"queries": len(sub_ids), "same_recall_all_k": same,
                 "mean_diff_mean_recall_pp": round(100 * statistics.fmean(
                     statistics.fmean(mine[i][0]) - statistics.fmean(qdrant[i][0]) for i in sub_ids), 4)}
        print(f"  сверка базы с Qdrant: одинаковый recall при всех k у {same} из {len(sub_ids)}", flush=True)
    best_cfg = show[mini["best"]]
    tcfg = best_cfg if best_cfg["family"] == "route" else mini["selected"][mini["best_route_family"]]
    timing = time_pipelines(corpus, elig, reps, mini["router"], tcfg, parts, full_vec, ctx,
                            n=args.time_n, base_n=args.time_base_n)
    ids_e = [q["i"] for q in elig]
    ids_s = [q["i"] for q in sub]
    variants = {"база": {"ids": "elig", **pack(qdrant, ids_e)}}
    for lbl, c in route_like.items():
        variants[lbl] = {"ids": "elig", **pack(res_route[cfg_name(c)], ids_e)}
    for lbl, c in global_like.items():
        name = "база (свой перебор)" if lbl == "база" else lbl
        variants[name] = {"ids": "sub", **pack(res_global[cfg_name(c)], ids_s)}

    # Справка: ширина маршрута. На мини маршрутизатор не ошибается, и мини не может выбрать между «только лучший
    # документ» и «несколько документов с весом λ». Здесь маршрутизатор, M и λ подбираются на половине A полного
    # (без мини A), итог на половине B; ранжирование кусков внутри документа (a, μ) остаётся с мини.
    tune = None
    if args.tune:
        fh = halves(elig)
        fa = [q for q in elig if fh[q["i"]] == "A"]
        fb = [q for q in elig if fh[q["i"]] == "B"]
        t0 = time.perf_counter()
        rt_half = routing_table(corpus, elig, router, {"A": {q["i"] for q in fa}, "B": {q["i"] for q in fb}})
        r_full = max(ROUTERS, key=lambda r: (rt_half[r]["A"]["mean"][0], statistics.fmean(rt_half[r]["A"]["mean"]), r))
        grid_t = [dict(tcfg, M=1, lam=0.0)] + [dict(tcfg, M=m, lam=lam) for m in MS for lam in (0.0, *LAMS)]
        ctx_t = QueryCtx(corpus, scores, router, r_full)
        res_t = run_configs(grid_t, fa, ctx_t)
        fa_ids = {q["i"] for q in fa}
        ranked_t = sorted(((objective(r, elig, fa_ids), n) for n, r in res_t.items()), reverse=True)
        cfg_t = next(c for c in grid_t if cfg_name(c) == ranked_t[0][1])
        res_tb = run_configs([cfg_t], fb, ctx_t)[cfg_name(cfg_t)]
        ids_b = [q["i"] for q in fb]
        variants["подобран на полном A"] = {"ids": "fullB", **pack(res_tb, ids_b)}
        tune = {"router": r_full, "cfg": cfg_t, "grid": len(grid_t), "fullB": ids_b, "routing": rt_half,
                "top": [[n, round(o, 3)] for o, n in ranked_t], "seconds": round(time.perf_counter() - t0, 1)}
        print(f"  подбор ширины на полном A: {r_full}, {cfg_name(cfg_t)} ({ranked_t[0][0]:.2f}), "
              f"{tune['seconds']:.0f} с", flush=True)
    out = {
        "stage": "full", "meta": {"chunks": corpus.n, "docs": len(corpus.files), "queries": len(queries),
                                  "eligible": len(elig), "tuned_found": found_tuned, "sample": len(sub),
                                  "sample_per_dataset": args.sample, "embed": estats,
                                  "sample_by_dataset": {d: sum(1 for q in sub if q["dataset"] == d) for d in DS},
                                  "no_semicolon": no_semicolon(queries), "scan_seconds": round(scan_s, 1),
                                  "scan_vectors": len(vecs_keys), "procs": args.procs,
                                  "inexact_fusions": scores.inexact},
        "queries": [{"i": q["i"], "dataset": q["dataset"]} for q in queries],
        "elig": ids_e, "sub": ids_s, "routing": rtab, "router": mini["router"], "router_errors": rerr,
        "base_check": check, "timing": timing, "variants": variants, "tune": tune,
    }
    save(Path(args.work) / "full.json", out)


def qdrant_base(path: Path, queries: list[dict]) -> dict[int, tuple]:
    """База со стенда (Qdrant exact, eval/run_full.sh): recall и precision по вопросам в порядке queries.jsonl."""
    r = load_json(path)
    pos = defaultdict(int)
    out = {}
    for q in queries:
        d = q["dataset"]
        j = pos[d]
        pos[d] += 1
        out[q["i"]] = ([r["per_query"][d][str(k)][j][1] for k in K], [r["per_query"][d][str(k)][j][0] for k in K])
    return out


# ---------- отчёт ----------

AGG_RU = {"max": "лучший кусок", "sum3": "сумма трёх лучших", "mean3": "среднее трёх лучших", "frac": "доля кусков"}


def cfg_ru(c: dict) -> str:
    """Параметры варианта словами для отчёта."""
    f = c["family"]
    if f == "base":
        return "весь вопрос по всему корпусу"
    if f == "route":
        a = c["a"]
        q = "весь вопрос" if a >= 1 else "сам вопрос" if a <= 0 else f"{a:g}·весь вопрос + {1 - a:g}·сам вопрос"
        if c["lam"] == INF:
            how = "документ за документом"
        elif c["lam"] == 0:
            how = f"top-{c['M']} " + ("документ" if c["M"] == 1 else "документа" if c["M"] < 5 else "документов")
        else:
            how = f"top-{c['M']} документов, λ = {c['lam']:g}"
        return f"{how}; куски ранжирует {q}" + (f"; соседи μ = {c['mu']:g}" if c.get("mu") else "")
    if f == "fuse":
        if c["how"] == "wsum":
            return f"{c['w']:g}·ссылка + {1 - c['w']:g}·сам вопрос, по всему корпусу"
        return {"rrf": "RRF списков ссылки и самого вопроса (top-200)", "max": "максимум баллов ссылки и самого вопроса"}[c["how"]]
    if f == "agg":
        lam = "∞" if c["lam"] == INF else f"{c['lam']:g}"
        return f"top-200, балл документа: {AGG_RU[c['how']]}, λ = {lam}"
    raise ValueError(f)


def fmt(t, sign: bool = False) -> str:
    p, lo, hi = t
    s = "+" if sign else ""
    return f"{p:{s}.1f} [{lo:{s}.1f}; {hi:{s}.1f}]"


def stats(v: dict, base: dict, qinfo: dict[int, str], v_ids: list[int], b_ids: list[int], subset: set[int]) -> dict:
    """recall@1/8/64, precision@1, средние по k и парные разности с базой: точка и 95 % интервал, в процентах."""
    bpos = {i: p for p, i in enumerate(b_ids)}
    ser: dict = defaultdict(lambda: defaultdict(list))
    for p, i in enumerate(v_ids):
        if i not in subset:
            continue
        d, R, P = qinfo[i], v["R"][p], v["P"][p]
        rm, pm = statistics.fmean(R), statistics.fmean(P)
        for nm, x in (("r1", R[0]), ("r8", R[3]), ("r64", R[6]), ("p1", P[0]), ("rm", rm), ("pm", pm)):
            ser[nm][d].append(100 * x)
        if v is not base:
            bp = bpos[i]
            ser["dr"][d].append(100 * (rm - statistics.fmean(base["R"][bp])))
            ser["dp"][d].append(100 * (pm - statistics.fmean(base["P"][bp])))
    return boot({nm: dict(x) for nm, x in ser.items()})


HEAD = ["| Вариант | Параметры | R@1 | R@8 | R@64 | P@1 | средний R по k | Δ R к базе | средний P по k | Δ P к базе |",
        "|---|---|---|---|---|---|---|---|---|---|"]


def row(label: str, params: str, st: dict) -> str:
    dr = fmt(st["dr"], True) if "dr" in st else "база"
    dp = fmt(st["dp"], True) if "dp" in st else "база"
    return (f"| {label} | {params} | {st['r1'][0]:.1f} | {st['r8'][0]:.1f} | {st['r64'][0]:.1f} | {st['p1'][0]:.1f} | "
            f"{fmt(st['rm'])} | {dr} | {fmt(st['pm'])} | {dp} |")


def ds_table(variants: dict, labels: list[str], qinfo: dict[int, str], ids_of, subset: set[int]) -> list[str]:
    """По наборам: средний recall и precision по k, recall@8 и @64 (без интервалов)."""
    L = ["| Набор | Вариант | средний R по k | R@8 | R@64 | средний P по k | P@1 |", "|---|---|---|---|---|---|---|"]
    for d in DS:
        for lbl in labels:
            v = variants[lbl]
            rows = [p for p, i in enumerate(ids_of(v)) if i in subset and qinfo[i] == d]
            if not rows:
                continue
            m = lambda f: 100 * statistics.fmean(f(p) for p in rows)  # noqa: E731
            L.append(f"| {NAMES[d]} | {lbl} | {m(lambda p: statistics.fmean(v['R'][p])):.1f} | {m(lambda p: v['R'][p][3]):.1f} | "
                     f"{m(lambda p: v['R'][p][6]):.1f} | {m(lambda p: statistics.fmean(v['P'][p])):.1f} | {m(lambda p: v['P'][p][0]):.1f} |")
    return L


def k_table(v: dict, what: str, qinfo: dict[int, str], ids: list[int], subset: set[int]) -> list[str]:
    L = ["| Набор | " + " | ".join(f"k={k}" for k in K) + " |", "|---|" + "---|" * len(K)]
    per = {}
    for d in DS:
        rows = [v[what][p] for p, i in enumerate(ids) if i in subset and qinfo[i] == d]
        if rows:
            per[d] = [100 * statistics.fmean(x[j] for x in rows) for j in range(len(K))]
            L.append(f"| {NAMES[d]} | " + " | ".join(f"{x:.1f}" for x in per[d]) + " |")
    L.append("| Среднее | " + " | ".join(f"{statistics.fmean(v[j] for v in per.values()):.1f}" for j in range(len(K))) + " |")
    return L


def sensitivity(grid: dict) -> list[str]:
    """Чувствительность на A: лучший средний recall по k при каждом значении параметра (по остальным максимум)."""
    def best_by(fam: str, pattern: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, o in grid.get(fam, {}).get("top", []):
            m = re.search(pattern, name)
            if m:
                out[m.group(1)] = max(out.get(m.group(1), -INF), o)
        return out

    def line(title: str, d: dict[str, float]) -> str:
        return f"- {title}: " + ", ".join(f"{k} → {v:.1f}" for k, v in d.items()) + "."
    L = ["Чувствительность на A (средний recall по k, лучший по остальным параметрам):", ""]
    for title, fam, pat in (("деление б, доля всего вопроса a в балле куска", "split_b", r"a=([\d.]+)"),
                            ("деление б, вес документа λ", "split_b", r"λ=(\S+)"),
                            ("соседи, μ", "neighbors", r"μ=([\d.]+)"),
                            ("деление а, слияние", "split_a", r"слияние (.+)$"),
                            ("агрегация, балл документа", "agg", r"агрегация (\w+)"),
                            ("мягко, вес документа λ", "soft", r"λ=(\S+)")):
        d = best_by(fam, pat)
        if d:
            L.append(line(title, d))
    return L + [""]


def run_report(args) -> None:
    work = Path(args.work)
    mini = load_json(work / "mini.json")
    full = load_json(work / "full.json") if (work / "full.json").exists() else None
    mq = {q["i"]: q["dataset"] for q in mini["queries"]}
    mids = [q["i"] for q in mini["queries"]]
    Bh = {q["i"] for q in mini["queries"] if q["half"] == "B"}
    mv, show, best, router = mini["variants"], mini["show"], mini["best"], mini["router"]
    labels = list(mv)
    stB = {lbl: stats(mv[lbl], mv["база"], mq, mids, mids, Bh) for lbl in labels}
    stAll = {lbl: stats(mv[lbl], mv["база"], mq, mids, mids, set(mids)) for lbl in labels}
    rep, side = router.split("|")
    rt = mini["routing"]
    no_semi = sum(mini["meta"]["no_semicolon"].values()) + (sum(full["meta"]["no_semicolon"].values()) if full else 0)
    if full:
        fq = {q["i"]: q["dataset"] for q in full["queries"]}
        elig, sub, fv = full["elig"], full["sub"], full["variants"]
        id_lists = {"elig": elig, "sub": sub, "fullB": (full.get("tune") or {}).get("fullB", [])}
        ids_f = lambda v: id_lists[v["ids"]]  # noqa: E731
        flab_e = [lbl for lbl in fv if fv[lbl]["ids"] == "elig"]
        flab_s = [lbl for lbl in fv if fv[lbl]["ids"] in ("elig", "sub")]
        stE = {lbl: stats(fv[lbl], fv["база"], fq, elig, elig, set(elig)) for lbl in flab_e}
        stS = {lbl: stats(fv[lbl], fv["база"], fq, ids_f(fv[lbl]), elig, set(sub)) for lbl in flab_s}

    b, bb = stB["база"], stB[best]
    L = ["# Маршрутизация по документу и деление вопроса на части (LegalBench-RAG)", "",
         "Замер без стенда, на замороженных векторах: нарезка naive500_title, bge-m3, точный поиск полным перебором в "
         "чистом Python (`eval/routing_eval.py`). Векторы кусков те же; новые векторы только у коротких текстов: названия "
         "документов («Document: <название>») и две части вопроса. Деление детерминированное, по первой «;»: ссылка на "
         "документ («Consider the Supply Agreement between A and B») и сам вопрос («What is the expiration date of this "
         f"contract?»). Вопросов без «;» в наборах {no_semi}. Метрика посимвольная, как у авторов бенчмарка; средний R и P "
         "по k это среднее по k = 1, 2, 4, …, 64, как в статье SAC (arXiv 2510.06999). В скобках 95 % интервал "
         "(бутстреп по вопросам), Δ к базе считается парно на тех же вопросах. У всех вариантов ровно k кусков: если в "
         "выбранных документах кусков меньше, список добивается следующими документами маршрутизатора.", "",
         "## Коротко", "",
         f"- Лучший вариант (выбран на половине A): **{best}**, {cfg_ru(show[best])}. Маршрутизатор: "
         f"{REP_RU[rep]} × {SIDE_RU[side]} (балл документа это косинус их векторов). "
         f"На половине B мини средний recall по k {b['rm'][0]:.1f} → {bb['rm'][0]:.1f} "
         f"(Δ {fmt(bb['dr'], True)}), средняя precision по k {b['pm'][0]:.1f} → {bb['pm'][0]:.1f} (Δ {fmt(bb['dp'], True)}); "
         f"recall@1 {b['r1'][0]:.1f} → {bb['r1'][0]:.1f}, recall@8 {b['r8'][0]:.1f} → {bb['r8'][0]:.1f}, "
         f"recall@64 {b['r64'][0]:.1f} → {bb['r64'][0]:.1f}."]
    if full and best in stE:
        fb, fbb = stE["база"], stE[best]
        L.append(f"- Полный набор ({len(elig)} вопросов, параметры с мини без нового подбора): средний recall по k "
                 f"{fb['rm'][0]:.1f} → {fbb['rm'][0]:.1f} (Δ {fmt(fbb['dr'], True)}), precision {fb['pm'][0]:.1f} → "
                 f"{fbb['pm'][0]:.1f} (Δ {fmt(fbb['dp'], True)}); recall@64 {fb['r64'][0]:.1f} → {fbb['r64'][0]:.1f}. "
                 "Для сравнения статья SAC на полном наборе (gte-large, краткое содержание документа от LLM у каждого "
                 "куска): 41.8 и 11.0; это числа статьи, не наш замер.")
    sb = stB.get(FAMILY_RU["split_b"])
    nb = next((lbl for lbl in labels if lbl == f"{FAMILY_RU['split_b']} + соседи"), None)
    nb_vs = stats(mv[nb], mv[FAMILY_RU["split_b"]], mq, mids, mids, Bh) if nb and sb else None
    tail = []
    for lbl in ("жёстко top-1", FAMILY_RU["split_a"], FAMILY_RU["agg"]):
        if lbl in stB:
            tail.append(f"{lbl} {fmt(stB[lbl]['dr'], True)}")
    L.append("- Главное дало деление вопроса: ссылка выбирает документ, а куски внутри него ранжирует сам вопрос"
             + (f" (деление б: Δ {fmt(sb['dr'], True)} на B" if sb else "")
             + (f"; соседи поверх него ещё {fmt(nb_vs['dr'], True)})" if nb_vs else ")" if sb else "")
             + ". Δ среднего recall по k на B у остальных: " + "; ".join(tail) + ".")
    L.append(f"- Маршрутизатор ставит эталонный документ первым у {rt[router]['B']['mean'][0]:.1f} % вопросов мини B"
             + (f" и у {full['routing'][router]['all']['mean'][0]:.1f} % вопросов полного набора" if full else "") + ".")
    tune = full.get("tune") if full else None
    if tune and best in stE:
        fbh = set(tune["fullB"])
        vs = stats(fv["подобран на полном A"], fv[best], fq, tune["fullB"], elig, fbh)
        L.append("- Ширину маршрута мини выбрать не может: на мини маршрутизатор не ошибается. Подбор на половине A "
                 f"полного дал {cfg_ru(tune['cfg'])}, маршрутизатор {REP_RU[tune['router'].split('|')[0]]} × "
                 f"{SIDE_RU[tune['router'].split('|')[1]]}: на половине B к параметрам с мини Δ среднего recall по k "
                 f"{fmt(vs['dr'], True)}, precision {fmt(vs['dp'], True)}.")
    L.append("- Приём опирается на устройство данных: вопрос LegalBench-RAG называет документ. Для юридического поиска это "
             "честно (вопрос про конкретный договор), но это выигрыш от знания, о каком документе вопрос, а не «лучше модель».")
    L += ["", "## Как выбирались параметры", "",
          f"Вопросы каждого набора поделены пополам с зерном {mini['meta']['seed']}: половина A для подбора, B для "
          "проверки. Маршрутизатор выбран по доле вопросов с эталонным документом на первом месте (A), параметры каждого "
          f"семейства по среднему recall по k на A; всего перебрано {sum(g['tried'] for g in mini['grid'].values())} "
          "сочетаний. Соседи подобраны поверх лучшего маршрута. Лучший вариант это лучший на A среди победителей "
          "семейств. На мини маршрутизатор не ошибается, поэтому «документ за документом» (λ = ∞) и «top-1 с добивкой» "
          "дают один и тот же список.", "",
          "Семейства: жёстко (куски только из top-n документов, ранжирует весь вопрос); мягко (балл куска + λ·балл "
          "документа); деление а (поиск по каждой части вопроса по всему корпусу и слияние: взвешенная сумма баллов, RRF, "
          "максимум); деление б (ссылка выбирает документы, куски ранжирует сам вопрос или смесь с баллом всего вопроса); "
          "агрегация (top-200 кусков по всему вопросу, балл документа из баллов его кусков); соседи (балл куска + μ·лучший "
          "балл соседнего куска того же документа).", "",
          *sensitivity(mini["grid"]),
          "## Мини-набор, половина B (проверка)", "", *HEAD]
    L += [row(lbl, cfg_ru(show[lbl]), stB[lbl]) for lbl in labels]
    if mv.get("мягко") and mv.get("жёстко top-1") and mv["мягко"]["R"] == mv["жёстко top-1"]["R"]:
        L += ["", "«Мягко» на A выбрало λ = ∞: это тот же список, что «жёстко top-1» (числа совпадают по каждому вопросу)."]
    L += ["", "По наборам (половина B):", ""]
    L += ds_table(mv, ["база", best], mq, lambda v: mids, Bh)
    L += ["", "## Мини-набор, все 776 вопросов (справка: половина A участвовала в подборе)", "", *HEAD]
    L += [row(lbl, cfg_ru(show[lbl]), stAll[lbl]) for lbl in labels]
    L += ["", "## Точность маршрутизации", "",
          "Доля вопросов, у которых эталонный документ (файл из эталонных фрагментов) на 1-м и в первых трёх местах, "
          "среднее по наборам, %. Полный набор: все вопросы без мини A.", "",
          "| Документ × вопрос | мини A | мини B | мини все | полный |", "|---|---|---|---|---|"]
    for r in ROUTERS:
        rr, ss = r.split("|")
        cells = [f"{rt[r][g]['mean'][0]:.1f} / {rt[r][g]['mean'][2]:.1f}" for g in ("A", "B", "all")]
        cells.append(f"{full['routing'][r]['all']['mean'][0]:.1f} / {full['routing'][r]['all']['mean'][2]:.1f}" if full else "")
        L.append(f"| {REP_RU[rr]} × {SIDE_RU[ss]}{' (выбран)' if r == router else ''} | " + " | ".join(cells) + " |")
    if full:
        L += ["", "У выбранного маршрутизатора на полном по наборам, top-1 / top-3: " + ", ".join(
            f"{NAMES[d]} {v[0]:.1f} / {v[2]:.1f}" for d, v in full["routing"][router]["all"]["by_dataset"].items()) + "."]
        re_ = full.get("router_errors")
        if re_:
            L.append("Промахи top-1 на полном: " + ", ".join(
                f"{NAMES[d]} {v['wrong']} из {v['n']}" for d, v in re_.items()) + f". Из них {sum(v['shared_ref'] for v in re_.values())} "
                "у вопросов, чья ссылка общая для нескольких документов (вопрос сам их не различает); остальные, судя по "
                "разобранным примерам, путают договоры одного вида с похожими названиями или поправку с основным договором.")
    if full:
        fm = full["meta"]
        L += ["", "## Полный набор", "",
              f"{fm['docs']} документов, {fm['chunks']} кусков. Параметры взяты с мини A без нового подбора; {fm['tuned_found']} "
              f"вопросов мини A исключены, осталось {fm['eligible']}. База это прогон стенда (Qdrant exact, `eval/run_full.sh`) "
              "по тем же вопросам.", "", f"### Все {len(elig)} вопросов, варианты с маршрутизатором", "", *HEAD]
        L += [row(lbl, cfg_ru(show[lbl]) if lbl in show else "", stE[lbl]) for lbl in flab_e]
        by_ds = fm.get("sample_by_dataset") or {}
        counts = ", ".join(f"{NAMES[d]} {n}" for d, n in by_ds.items() if n)
        L += ["", f"### Подвыборка {len(sub)} вопросов, все варианты, включая перебор всего корпуса", "",
              f"До {fm['sample_per_dataset']} вопросов на набор, зерно {SEED}" + (f" ({counts}; у PrivacyQA все его вопросы "
              "есть и в мини, половина ушла в подбор)" if counts else "") + ".", "", *HEAD]
        for lbl in flab_s:
            params = cfg_ru(show[lbl]) if lbl in show else "свой точный перебор, сверка со стендом"
            L.append(row(lbl, params, stS[lbl]))
        bc, mc = full.get("base_check"), mini.get("base_check")
        if bc:
            L += ["", f"Сверка базы: свой точный перебор и Qdrant exact дали одинаковый recall при всех k у "
                  f"{bc['same_recall_all_k']} из {bc['queries']} вопросов подвыборки (разница среднего recall по k "
                  f"{bc['mean_diff_mean_recall_pp']:+.3f} п.п.)"
                  + (f"; на мини у {mc['same_recall_all_k']} из {mc['queries']}." if mc else ".")]
        if fm.get("inexact_fusions"):
            L.append(f"Деление а на полном считается по top-{FUSE_N} каждой части. У {fm['inexact_fusions']} из {len(sub)} "
                     "вопросов граница не доказала, что список совпадает с полным перебором (совпасть он мог); у остальных "
                     "список точный.")
        if best in fv:
            where = set(ids_f(fv[best]))  # маршрут: все вопросы без мини A; перебор: подвыборка
            L += ["", "По наборам (" + ("все вопросы полного без мини A" if fv[best]["ids"] == "elig" else "подвыборка")
                  + "):", ""]
            L += ds_table(fv, ["база", best], fq, ids_f, where)
            for what, name in (("R", "recall"), ("P", "precision")):
                for lbl in ("база", best):
                    L += ["", f"{name} по k, {lbl}, %:", ""]
                    L += k_table(fv[lbl], what, fq, ids_f(fv[lbl]), where)
        if tune and best in flab_e:
            fbh = set(tune["fullB"])
            rr, ss = tune["router"].split("|")
            tl = {"база": cfg_ru(show["база"]), best: cfg_ru(show[best]) + " (параметры с мини)",
                  "подобран на полном A": cfg_ru(tune["cfg"]) + f"; маршрутизатор {REP_RU[rr]} × {SIDE_RU[ss]}"}
            L += ["", "### Справка: ширина маршрута, подобранная на самом полном наборе", "",
                  "На мини маршрутизатор не ошибается, поэтому мини не может выбрать между «только лучший документ» и "
                  "«несколько документов с весом λ». Здесь маршрутизатор, M и λ подобраны на половине A полного набора "
                  f"(вопросы без мини A, деление пополам с тем же зерном, перебрано {tune['grid']} сочетаний), итог на "
                  f"половине B (вопросов: {len(fbh)}); ранжирование кусков внутри документа (a, μ) взято с мини.", "", *HEAD]
            for lbl, params in tl.items():
                L.append(row(lbl, params, stats(fv[lbl], fv["база"], fq, ids_f(fv[lbl]), elig, fbh)))
    tm = mini["timing"]
    L += ["", "## Время на вопрос", "",
          "Чистый Python, один процесс, Mac (медиана). База перебирает все куски; маршрут считается с нуля: баллы "
          "документов, баллы кусков выбранных документов, ранжирование. Векторы частей вопроса сюда не входят: это ещё "
          "два коротких текста для Ollama. Список маршрута с нуля сверен со списком из готовых баллов.", "",
          "| Набор | база | маршрут | вопросов (база / маршрут) | маршрут с нуля совпал |", "|---|---|---|---|---|",
          f"| мини | {tm['base_ms_p50']:.0f} мс | {tm['route_ms_p50']:.1f} мс | {tm['n_base']} / {tm['n_route']} | "
          f"{tm['route_same_as_cached']} из {tm['n_route']} |"]
    if full:
        ft = full["timing"]
        L.append(f"| полный | {ft['base_ms_p50']:.0f} мс | {ft['route_ms_p50']:.1f} мс | {ft['n_base']} / {ft['n_route']} | "
                 f"{ft['route_same_as_cached']} из {ft['n_route']} |")
    L += ["", f"Маршрут: `{tm['route_cfg']}`. В Qdrant база это HNSW по всей коллекции (на стенде p50 15.9 мс на полном), "
          "маршрут это поиск по маленькой коллекции документов и точный поиск по кускам одного документа с фильтром; "
          "на стенде это не мерили.", ""]
    L += service_notes(show[best], router, tune)
    L += caveats(full is not None)
    Path(args.md).parent.mkdir(parents=True, exist_ok=True)
    Path(args.md).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def service_notes(cfg: dict, router: str, tuned: dict | None = None) -> list[str]:
    rep = router.split("|")[0]
    rep_how = {"centroid": "это среднее векторов его кусков (считается из уже загруженных векторов, без модели)",
               "first": "это вектор первого куска (он уже есть в коллекции кусков)",
               "title": "это вектор «Document: <название>» (один короткий текст на документ)",
               "title+first": "это векторы названия и первого куска (балл документа как среднее двух)"}[rep]
    a = cfg.get("a", 1.0)
    L = ["## Что понадобится сервису", "",
         "- Деление вопроса по первой «;» на ссылку и сам вопрос. Вопрос без ссылки идёт как сейчас: весь вопрос по всему "
         "корпусу. Для живых вопросов нужен свой разбор ссылки (здесь не проверялся).",
         "- Ещё два коротких вектора на запрос (ссылка и сам вопрос), одним запросом к Ollama вместе с вопросом.",
         f"- Коллекция документов в Qdrant: точка на документ, вектор документа {rep_how}, payload `file` и `title`. "
         "На полном наборе 714 точек.",
         "- В коллекции кусков индекс payload по `file` (keyword): поиск кусков с фильтром `file` = выбранный документ."]
    if cfg["family"] == "route":
        if 0 < a < 1:
            L.append(f"- Смесь баллов линейна: {a:g}·cos(весь вопрос, кусок) + {1 - a:g}·cos(сам вопрос, кусок) это скалярное "
                     f"произведение куска с вектором {a:g}·v(весь вопрос) + {1 - a:g}·v(сам вопрос), поэтому в Qdrant это "
                     "один поиск с этим вектором.")
        L.append("- На запрос два вызова Qdrant: поиск по коллекции документов по вектору ссылки и точный поиск кусков "
                 "лучшего документа с фильтром по `file`; если в документе меньше k кусков, такой же поиск в следующем.")
        if cfg.get("mu"):
            L.append(f"- Соседи (μ = {cfg['mu']:g}): нужны баллы всех кусков документа, то есть поиск в документе с limit = "
                     "число его кусков (без текста в payload, только `start`), сглаживание и отбор top-k в сервисе, потом "
                     "текст выбранных кусков по id.")
    if tuned and tuned["cfg"].get("lam", 0) not in (0, INF):
        t = tuned["cfg"]
        L.append(f"- Мягкий маршрут, подобранный на полном (top-{t['M']} документов, λ = {t['lam']:g}): фильтр `file` по "
                 f"{t['M']} документам маршрутизатора и прибавка λ·балл документа к баллу куска в сервисе.")
    L.append("")
    return L


def caveats(with_full: bool) -> list[str]:
    L = ["## Оговорки и что не проверено", "",
         "- Приём работает потому, что вопрос называет документ. Ошибка маршрутизатора уводит выдачу в чужой договор "
         "целиком; на вопросах без ссылки выигрыша нет.",
         "- Деление по «;» опирается на шаблон вопросов бенчмарка. Живым вопросам нужен другой разбор (правило или "
         "модель), это не мерили.",
         "- Половины A и B делят вопросы, а не документы: документы у половин общие. Подбирается несколько глобальных "
         "чисел (маршрутизатор, M, λ, a, μ), поэтому подгонка под документы мала, но не нулевая.",
         "- Время в чистом Python не равно времени в Qdrant; на стенде маршрут не мерили.",
         "- Векторы кусков не менялись (та же нарезка naive500_title). Статья SAC сравнивается по её числам, у неё другая "
         "модель (gte-large) и краткое содержание документа от LLM у каждого куска."]
    if with_full:
        L.append("- Полный набор: варианты с перебором всего корпуса (база для сверки, деление а, агрегация) посчитаны на "
                 f"подвыборке по {SAMPLE} вопросов на набор, маршруты на всех вопросах без мини A.")
    L.append("")
    return L


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=("mini", "full", "report"))
    ap.add_argument("--cache", default=str(ROOT / "cache"), help="папка с stage1/ и full/ от eval/prepare.py")
    ap.add_argument("--embed-cache", default=str(ROOT / "cache/embeddings"))
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-pause", type=float, default=2.0, help="пауза между порциями по 160 текстов, с")
    ap.add_argument("--work", default=str(ROOT / "runs/routing"))
    ap.add_argument("--md", default=str(ROOT / "eval/results/routing.md"))
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--sample", type=int, default=SAMPLE)
    ap.add_argument("--time-n", type=int, default=100)
    ap.add_argument("--time-base-n", type=int, default=20)
    ap.add_argument("--no-tune", dest="tune", action="store_false",
                    help="полный: без справочного подбора ширины маршрута на половине A полного")
    args = ap.parse_args(argv)
    {"mini": run_mini, "full": run_full, "report": run_report}[args.step](args)


if __name__ == "__main__":
    main()
