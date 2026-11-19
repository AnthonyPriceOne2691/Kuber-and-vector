"""Упирается ли качество поиска по договорам в модель: три модели векторов, LLM-реранкер и HyDE.

Замер без стенда, на замороженных векторах LegalBench-RAG (мини: 776 вопросов, 17 403 куска naive500_title),
тем же кодом, что маршрутизация (eval/routing_eval.py): точный поиск полным перебором в чистом Python,
те же половины A и B, та же посимвольная метрика и тот же парный бутстреп.

Шаги:
    stage   папка этапа для модели в том же виде, что cache/stage1 (eval/prepare.py): те же куски, их
            векторы этой моделью, вопросы и их векторы с префиксом модели. Готовые векторы кусков
            (--chunks) проверяются пробой: первый кусок заново через Ollama должен дать тот же вектор.
    models  каждая модель: база (весь вопрос по всему корпусу) и лучший маршрут с параметрами bge-m3
            (средний вектор кусков × ссылка, документ за документом, 0.25·весь вопрос + 0.75·сам вопрос,
            соседи μ = 0.5); справкой маршрут, перенастроенный на A (маршрутизатор по top-1 на A, a и μ
            по среднему recall по k на A). Вектор документа и короткие тексты считает та же модель.
    rerank  LLM пересортировывает первые N кусков лучшего маршрута (bge-m3) на половине B: поточечно,
            балл 0–10 на кусок, think выключен, температура 0, ответ JSON по схеме. Куски дальше N на
            своих местах. Порядок вопросов случайный с фиксированным зерном, наборы по очереди, поэтому
            первые --questions вопросов это случайная подвыборка B почти поровну из каждого набора.
    hyde    LLM пишет короткий гипотетический пункт документа на сам вопрос (часть после «;»); его вектор
            (bge-m3) смешивается с вектором самого вопроса: балл куска (1 − β)·cos(вопрос) + β·cos(черновик).
            Маршрут, a и μ как у лучшего маршрута, β подбирается на A, итог на B. Черновики в кэше по тексту вопроса.
    report  отчёт eval/results/models.md из итогов шагов.

    python3 -I -B eval/models_eval.py stage --model nomic-embed-text --cache CACHE --out OUT/stage1_nomic --embed-cache EMB
    python3 -I -B eval/models_eval.py models --stage bge-m3=CACHE/stage1 --stage nomic=OUT/stage1_nomic --embed-cache EMB --work WORK
    python3 -I -B eval/models_eval.py rerank --cache CACHE --embed-cache EMB --work WORK --questions 200
    python3 -I -B eval/models_eval.py hyde --cache CACHE --embed-cache EMB --work WORK
    python3 -I -B eval/models_eval.py report --work WORK
CACHE это папка cache с подготовленным stage1/ (читается, не пишется). Ответы LLM в WORK/llm_cache.jsonl:
повторный запуск не спрашивает модель заново. Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.request
from array import array
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from app.embed_models import base_name, query_prefix  # noqa: E402
from app.embedding import OllamaEmbedder  # noqa: E402
from eval import legalbench as lb  # noqa: E402
from eval import routing_eval as R  # noqa: E402
from eval.prepare import index_text  # noqa: E402

# Модели только для замера. В app/embed_models.py их нет: тот список заодно пропуск фильтра Ollama
# на стенде (tests/test_filter.py), а сервису nomic не нужен. Карточка nomic-embed-text требует
# префикс задачи: «search_document: » у документов и «search_query: » у вопросов; Ollama сам его
# не ставит (шаблон модели это голый текст).
MEASURE_PREFIX = {"nomic-embed-text": ("search_query: ", "search_document: ")}

REF_ROUTER = "centroid|ref"               # маршрутизатор лучшего маршрута (eval/results/routing.md)
BEST = R.route_cfg(5, R.INF, 0.25, 0.5)   # лучший маршрут bge-m3, подобран на мини A
TUNE_A = (0.0, 0.25, 0.5, 0.75)           # перенастройка на A: доля балла всего вопроса
TUNE_MU = (0.0, *R.MUS)                   # и вес соседей
VARIANTS = ("база", "маршрут", "маршрут, подбор на A")

NUM_CTX = 2048       # контекст LLM: вопрос и кусок в 500 знаков занимают меньше 600 токенов
RERANK_N = (10, 20)
BETAS = tuple(x / 10 for x in range(11))  # HyDE: вес черновика в балле самого вопроса


def prefixes(model: str) -> tuple[str, str]:
    """Префиксы вопроса и документа: модели сервиса из app/embed_models.py (у документа без префикса),
    модели только для замера из MEASURE_PREFIX."""
    return MEASURE_PREFIX.get(base_name(model), (query_prefix(model), ""))


# ---------- stage: векторы кусков и вопросов моделью ----------

def link(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    os.symlink(os.path.abspath(src), dst)


def read_row(path: Path, dim: int, row: int) -> array:
    v = array("f")
    with open(path, "rb") as f:
        f.seek(row * dim * 4)
        v.fromfile(f, dim)
    return v


def write_vectors(emb: OllamaEmbedder, texts: list[str], path: Path) -> None:
    with open(path, "wb") as f:
        for t in texts:
            emb.vector(t).tofile(f)


def build_stage(args) -> None:
    """Папка этапа для модели: куски и вопросы из CACHE/stage1, векторы этой моделью с её префиксами."""
    src, out = Path(args.cache) / "stage1", Path(args.out)
    qp, dp = prefixes(args.model)
    if args.no_query_prefix:
        qp = ""
    idx = out / "naive500_title"
    idx.mkdir(parents=True, exist_ok=True)
    chunk_src = Path(args.chunks) / "naive500_title" if args.chunks else src / "naive500_title"
    with open(chunk_src / "points.jsonl", encoding="utf-8") as f:
        texts = [dp + index_text(json.loads(line), "title") for line in f]
    with open(src / "queries.jsonl", encoding="utf-8") as f:
        qtexts = [qp + json.loads(line)["query"] for line in f]
    link(chunk_src / "points.jsonl", idx / "points.jsonl")
    link(src / "queries.jsonl", out / "queries.jsonl")
    meta = {"model": args.model, "selection": "mini", "query_prefix": qp, "doc_prefix": dp,
            "chunks": len(texts), "queries": len(qtexts)}
    emb = OllamaEmbedder(model=args.model, url=args.ollama, cache_dir=args.embed_cache)
    try:
        if args.chunks:  # готовые векторы кусков: первый кусок заново через Ollama должен совпасть
            R.ensure_in_portions(emb, texts[:1], args.embed_pause)
            probe = R.unit(emb.vector(texts[0]))
            have = R.unit(read_row(chunk_src / "vectors.f32", emb.dim, 0))
            meta["chunk_probe_cos"] = round(R.dot(probe, have), 6)
            prep = json.loads((Path(args.chunks) / "meta.json").read_text(encoding="utf-8"))
            meta["prep"] = (prep.get("strategies") or {}).get("naive500_title")
            print(f"{args.model}: проба первого куска cos = {meta['chunk_probe_cos']}", flush=True)
            if meta["chunk_probe_cos"] < 0.999:
                raise SystemExit("векторы кусков посчитаны не этой моделью или не по этому тексту")
            link(chunk_src / "vectors.f32", idx / "vectors.f32")
        else:
            print(f"{args.model}: кусков {len(texts)}, префикс документа {dp!r}", flush=True)
            n0, s0, t0 = emb.computed, emb.seconds, time.perf_counter()
            R.ensure_in_portions(emb, texts, args.embed_pause, portion=args.portion, progress=True)
            new, secs = emb.computed - n0, emb.seconds - s0
            meta.update(new_vectors=new, embed_seconds=round(secs, 1),
                        wall_seconds=round(time.perf_counter() - t0, 1),
                        chunks_per_second=round(new / secs, 1) if secs else None)
            write_vectors(emb, texts, idx / "vectors.f32")
            print(f"  новых векторов {new} за {secs:.0f} с", flush=True)
        R.ensure_in_portions(emb, qtexts, args.embed_pause)
        write_vectors(emb, qtexts, out / "query_vectors.f32")
        meta["dim"] = emb.dim
        ready = Path(args.chunks) / "query_vectors.f32" if args.chunks else None
        if ready is not None and ready.exists():  # те же ли векторы вопросов лежат в готовом этапе
            mine = R.read_vectors(out / "query_vectors.f32", emb.dim, 0, len(qtexts))
            have = R.read_vectors(ready, emb.dim, 0, len(qtexts))
            meta["ready_queries_same"] = sum(1 for a, b in zip(mine, have) if R.dot(R.unit(a), R.unit(b)) > 0.9999)
            print(f"  векторы вопросов совпали с готовым этапом: {meta['ready_queries_same']} из {len(qtexts)}", flush=True)
    finally:
        emb.close()
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"готово: {out} (размерность {meta['dim']}, префикс вопроса {qp!r})", flush=True)


# ---------- models: база и маршрут у каждой модели ----------

def model_args(args, stage: Path) -> argparse.Namespace:
    """Модель и её префиксы по meta.json этапа (у этапов от eval/prepare.py префиксов в meta нет)."""
    meta = json.loads((stage / "meta.json").read_text(encoding="utf-8"))
    qp, dp = prefixes(meta["model"])
    return argparse.Namespace(embed_model=meta["model"], ollama=args.ollama, embed_cache=args.embed_cache,
                              embed_pause=args.embed_pause, query_prefix=meta.get("query_prefix", qp),
                              doc_prefix=meta.get("doc_prefix", dp))


def load(stage: Path, margs) -> argparse.Namespace:
    """Корпус, вопросы, векторы частей вопроса (той же моделью) и маршрутизатор; сверка вектора вопроса."""
    meta, dim, corpus, queries, qv, parts, estats, reps, router, vec_of, full_vec = R.setup(stage, margs)
    if estats["probe_cos"] < 0.999:
        raise SystemExit(f"{stage}: вектор вопроса заново не совпал с кэшем (cos {estats['probe_cos']}), не тот префикс?")
    return argparse.Namespace(meta=meta, dim=dim, corpus=corpus, queries=queries, parts=parts, estats=estats,
                              router=router, vec_of=vec_of)


def score_all(m) -> R.Scores:
    """Баллы всех кусков по всему вопросу и по самому вопросу (мини держим целиком)."""
    t0 = time.perf_counter()
    keys = [("f", q["i"]) for q in m.queries] + [("s", a) for a in sorted(m.parts["ask"])]
    mats = R.score_matrix([m.vec_of(k) for k in keys], m.corpus.vecs)
    print(f"  баллы всех кусков для {len(keys)} векторов: {time.perf_counter() - t0:.0f} с", flush=True)
    return R.Scores(m.corpus, m.vec_of, arrays=dict(zip(keys, mats)))


def chunk_speed(meta: dict) -> dict | None:
    """Скорость векторизации кусков: свой замер шага stage или замер подготовки (eval/prepare.py)."""
    if meta.get("chunks_per_second"):
        return {"chunks_per_second": meta["chunks_per_second"], "embed_seconds": meta["embed_seconds"],
                "new_vectors": meta["new_vectors"], "source": "stage"}
    prep = meta.get("prep") or (meta.get("strategies") or {}).get("naive500_title")
    if prep and prep.get("chunks_per_second"):
        return {"chunks_per_second": prep["chunks_per_second"], "embed_seconds": prep["embed_seconds"],
                "new_vectors": prep["new_vectors"], "source": "prepare"}
    return None


def best_router(rtab: dict) -> str:
    """Как в eval/routing_eval.py: по доле top-1 на A, при равенстве по среднему top-1/2/3."""
    return max(R.ROUTERS, key=lambda r: (rtab[r]["A"]["mean"][0], statistics.fmean(rtab[r]["A"]["mean"]), r))


def eval_stage(stage: Path, args) -> dict:
    t0 = time.perf_counter()
    margs = model_args(args, stage)
    m = load(stage, margs)
    half = R.halves(m.queries)
    groups = {h: {i for i, x in half.items() if x == h} for h in ("A", "B")}
    groups["all"] = set(half)
    scores = score_all(m)
    rtab = R.routing_table(m.corpus, m.queries, m.router, groups)
    tuned_router = best_router(rtab)
    ctx = R.QueryCtx(m.corpus, scores, m.router, REF_ROUTER)
    res = R.run_configs([{"family": "base"}, BEST], m.queries, ctx)
    qa = [q for q in m.queries if half[q["i"]] == "A"]
    grid = [R.route_cfg(BEST["M"], BEST["lam"], a, mu) for a in TUNE_A for mu in TUNE_MU]
    ctx_t = R.QueryCtx(m.corpus, scores, m.router, tuned_router)
    gres = R.run_configs(grid, qa, ctx_t)
    ranked = sorted(((R.objective(r, m.queries, groups["A"]), n) for n, r in gres.items()), reverse=True)
    tuned = next(c for c in grid if R.cfg_name(c) == ranked[0][1])
    tres = R.run_configs([tuned], m.queries, ctx_t)[R.cfg_name(tuned)]
    ids = [q["i"] for q in m.queries]
    print(f"  {m.meta['model']}: маршрутизатор на A {tuned_router}, подбор на A {R.cfg_name(tuned)} "
          f"({ranked[0][0]:.2f}), {time.perf_counter() - t0:.0f} с", flush=True)
    return {"model": m.meta["model"], "query_prefix": margs.query_prefix, "doc_prefix": margs.doc_prefix,
            "dim": m.dim, "embed_short": m.estats, "chunk_speed": chunk_speed(m.meta),
            "chunk_probe_cos": m.meta.get("chunk_probe_cos"), "ready_queries_same": m.meta.get("ready_queries_same"),
            "queries_n": len(m.queries),
            "routing": {r: {g: rtab[r][g]["mean"] for g in groups} for r in dict.fromkeys((REF_ROUTER, tuned_router))},
            "tuned_router": tuned_router, "tuned": tuned, "grid_top": [[n, round(o, 3)] for o, n in ranked[:8]],
            "keys": [q["key"] for q in m.queries], "datasets": [q["dataset"] for q in m.queries],
            "half": [half[i] for i in ids],
            "variants": {"база": R.pack(res["база"], ids), "маршрут": R.pack(res[R.cfg_name(BEST)], ids),
                         "маршрут, подбор на A": R.pack(tres, ids)},
            "seconds": round(time.perf_counter() - t0, 1)}


def run_models(args) -> None:
    out: dict = {"order": [], "stages": {}, "best": BEST, "router": REF_ROUTER}
    for spec in args.stage:
        label, _, path = spec.partition("=")
        print(f"== {label}: {path}", flush=True)
        out["order"].append(label)
        out["stages"][label] = eval_stage(Path(path), args)
    ref = out["stages"][out["order"][0]]
    for label in out["order"][1:]:
        if out["stages"][label]["keys"] != ref["keys"]:
            raise SystemExit(f"{label}: другие вопросы или другой их порядок, парное сравнение невозможно")
    out["queries"] = [{"i": j, "dataset": d, "half": h} for j, (d, h) in enumerate(zip(ref["datasets"], ref["half"]))]
    for st in out["stages"].values():
        for k in ("keys", "datasets", "half"):
            del st[k]
    R.save(Path(args.work) / "models.json", out)


# ---------- LLM через Ollama ----------

def ns_ms(x) -> float | None:
    return round(x / 1e6, 1) if isinstance(x, (int, float)) else None


class LLM:
    """Ollama /api/chat строго по одному запросу; ответы в кэше на диске (JSONL, ключ: тело запроса без
    keep_alive). Оборванная последняя строка кэша при чтении пропускается."""

    def __init__(self, url: str, model: str, cache_path: Path, timeout: int = 600):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout
        self.path = Path(cache_path)
        self.cache: dict[str, dict] = {}
        self.calls = 0
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.cache[r["key"]] = r

    @staticmethod
    def key(body: dict) -> str:
        core = {k: v for k, v in body.items() if k != "keep_alive"}
        return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def ask(self, body: dict) -> dict:
        k = self.key(body)
        r = self.cache.get(k)
        if r is None:
            t0 = time.perf_counter()
            data = self._post("/api/chat", body)
            r = {"key": k, "content": (data.get("message") or {}).get("content", ""),
                 "seconds": round(time.perf_counter() - t0, 3), "prompt_tokens": data.get("prompt_eval_count"),
                 "prompt_ms": ns_ms(data.get("prompt_eval_duration")), "out_tokens": data.get("eval_count"),
                 "out_ms": ns_ms(data.get("eval_duration")), "load_ms": ns_ms(data.get("load_duration"))}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.cache[k] = r
            self.calls += 1
        return r

    def unload(self) -> None:
        """Выгрузить модель из памяти Мака (keep_alive 0): рядом работают виртуальные машины стенда."""
        try:
            self._post("/api/generate", {"model": self.model, "keep_alive": 0})
        except OSError as e:
            print(f"  выгрузить {self.model} не вышло: {e}", flush=True)


def chat_body(llm: str, system: str, user: str, fmt: dict | None = None, num_predict: int = 16) -> dict:
    """Детерминированно: think выключен, температура 0, фиксированное зерно, один и тот же контекст
    (смена num_ctx перезагружает модель)."""
    body = {"model": llm, "stream": False, "think": False, "keep_alive": "15m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0, "seed": 7, "num_ctx": NUM_CTX, "num_predict": num_predict}}
    if fmt is not None:
        body["format"] = fmt
    return body


def strip_think(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()


# ---------- rerank: поточечный реранкер ----------

RERANK_SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
RERANK_SYSTEM = (
    "You grade search results for questions about legal documents: contracts, agreements and privacy policies.\n"
    "You get a question and one excerpt from a document. Rate how relevant the excerpt is to the question, "
    "as an integer from 0 to 10:\n"
    "10 - the excerpt contains the provision that answers the question or that the question asks about;\n"
    "7 - the excerpt contains part of that provision;\n"
    "3 - the excerpt is on a related topic but does not contain the answer;\n"
    "0 - the excerpt is unrelated to the question.\n"
    'Return JSON: {"score": <integer from 0 to 10>}.'
)


def rerank_body(llm: str, question: str, chunk: str) -> dict:
    """Вопрос перед куском: у вызовов одного вопроса общее начало промпта, Ollama его не пересчитывает."""
    return chat_body(llm, RERANK_SYSTEM, f"Question: {question}\n\nExcerpt:\n{chunk}", RERANK_SCHEMA, 16)


def parse_score(content: str) -> int | None:
    """Балл 0–10 из ответа {"score": n}; None, если разобрать нельзя."""
    text = strip_think(content)
    try:
        s = json.loads(text[text.find("{"):text.rfind("}") + 1])["score"]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(s, bool) or not isinstance(s, (int, float)):
        return None
    return max(0, min(10, round(s)))


def rerank(ranked: list[int], score: dict[int, int | None], n: int) -> list[int]:
    """Первые n кусков по баллу LLM, при равном балле порядок маршрута; кусок без балла остаётся на своём
    месте; куски дальше n как были."""
    top = ranked[:n]
    pos = {c: j for j, c in enumerate(top)}
    scored = iter(sorted((c for c in top if score.get(c) is not None), key=lambda c: (-score[c], pos[c])))
    return [next(scored) if score.get(c) is not None else c for c in top] + ranked[n:]


def gold_chars(corpus: R.Corpus, c: int, gt: list[tuple]) -> int:
    f, s, e = corpus.files[corpus.doc_of[c]], corpus.start[c], corpus.end[c]
    return sum(max(0, min(e, ge) - max(s, gs)) for gf, gs, ge in gt if gf == f)


def oracle(corpus: R.Corpus, ranked: list[int], gt: list[tuple], n: int) -> list[int]:
    """Потолок реранкера: первые n кусков по числу эталонных знаков в них (лучший возможный порядок)."""
    return sorted(ranked[:n], key=lambda c: -gold_chars(corpus, c, gt)) + ranked[n:]


def discriminate(acc: dict, corpus: R.Corpus, top: list[int], score: dict[int, int | None], gt: list[tuple]) -> None:
    """Пары (кусок с эталонными знаками, кусок без них) из первых N одного вопроса: доля пар, где эталонный
    выше, у балла LLM (равный балл это половина) и у порядка маршрута; заодно баллы обеих групп."""
    gold = [c for c in top if gold_chars(corpus, c, gt) > 0]
    other = [c for c in top if gold_chars(corpus, c, gt) == 0]
    pos = {c: j for j, c in enumerate(top)}
    acc["gold"] += [score[c] for c in gold if score.get(c) is not None]
    acc["other"] += [score[c] for c in other if score.get(c) is not None]
    for g in gold:
        for o in other:
            sg, so = score.get(g), score.get(o)
            if sg is not None and so is not None:
                acc["llm"][0] += 1.0 if sg > so else 0.5 if sg == so else 0.0
                acc["llm"][1] += 1
            acc["route"][0] += pos[g] < pos[o]
            acc["route"][1] += 1


def discrimination_summary(acc: dict) -> dict:
    def share(x: list[int]) -> float | None:
        return round(100 * sum(1 for s in x if s >= 7) / len(x), 1) if x else None
    return {"gold_n": len(acc["gold"]), "other_n": len(acc["other"]),
            "gold_mean": round(statistics.fmean(acc["gold"]), 2) if acc["gold"] else None,
            "other_mean": round(statistics.fmean(acc["other"]), 2) if acc["other"] else None,
            "gold_ge7": share(acc["gold"]), "other_ge7": share(acc["other"]),
            "llm_pairs": round(100 * acc["llm"][0] / acc["llm"][1], 1) if acc["llm"][1] else None,
            "route_pairs": round(100 * acc["route"][0] / acc["route"][1], 1) if acc["route"][1] else None,
            "pairs": acc["route"][1]}


def sample_order(queries: list[dict], half: dict[int, str], h: str, seed: int = R.SEED) -> list[dict]:
    """Вопросы половины h в случайном порядке с фиксированным зерном, наборы по очереди: первые K вопросов
    это случайная подвыборка, из каждого набора почти поровну."""
    per = []
    for d in lb.DATASETS:
        qs = [q for q in queries if q["dataset"] == d and half[q["i"]] == h]
        random.Random(f"{seed}:order:{d}").shuffle(qs)
        per.append(qs)
    return [q for group in itertools.zip_longest(*per) for q in group if q is not None]


def chunk_texts(stage: Path, wanted: set[int]) -> dict[int, str]:
    """Текст куска для LLM тот же, что видела модель векторов: «Document / Section» и текст."""
    out = {}
    with open(stage / "naive500_title/points.jsonl", encoding="utf-8") as f:
        for c, line in enumerate(f):
            if c in wanted:
                out[c] = index_text(json.loads(line), "title")
    return out


def run_rerank(args) -> None:
    stage = Path(args.cache) / "stage1"
    m = load(stage, model_args(args, stage))
    half = R.halves(m.queries)
    order = sample_order(m.queries, half, "B")
    qs = order[:args.questions] if args.questions else order
    ns = sorted({int(x) for x in str(args.n).split(",")})
    nmax = max(ns)
    ctx = R.QueryCtx(m.corpus, R.Scores(m.corpus, m.vec_of), m.router, REF_ROUTER)  # баллы по документу
    ranked = {q["i"]: R.rank(BEST, ctx.set(q)) for q in qs}
    texts = chunk_texts(stage, {c for r in ranked.values() for c in r[:nmax]})
    llm = LLM(args.ollama, args.llm, Path(args.work) / "llm_cache.jsonl", timeout=args.timeout)
    calls: dict[int, list[dict]] = {}
    t0, new0 = time.perf_counter(), 0
    print(f"реранкер {args.llm}: вопросов {len(qs)} из {len(order)} половины B, N до {nmax}", flush=True)
    try:
        for j, q in enumerate(qs, 1):
            calls[q["i"]] = [llm.ask(rerank_body(args.llm, q["query"], texts[c])) for c in ranked[q["i"]][:nmax]]
            if j % 5 == 0 or j == len(qs):
                spent = time.perf_counter() - t0
                done_new = llm.calls - new0
                per_call = spent / done_new if done_new else 0.0
                left = sum(1 for qq in qs[j:] for c in ranked[qq["i"]][:nmax]
                           if llm.key(rerank_body(args.llm, qq["query"], texts[c])) not in llm.cache)
                print(f"  {j}/{len(qs)}: новых вызовов {done_new}, {spent:.0f} с; оценка на вопрос "
                      f"{per_call * nmax:.1f} с, осталось около {left * per_call / 60:.0f} мин", flush=True)
    finally:
        if args.unload:
            llm.unload()
    res: dict[str, dict[int, tuple]] = defaultdict(dict)
    hist: Counter = Counter()
    disc = {"gold": [], "other": [], "llm": [0.0, 0], "route": [0.0, 0]}
    for q in qs:
        i, r = q["i"], ranked[q["i"]]
        sc = {c: parse_score(x["content"]) for c, x in zip(r[:nmax], calls[i])}
        hist.update("нет" if s is None else str(s) for s in sc.values())
        discriminate(disc, m.corpus, r[:nmax], sc, q["gt"])
        res["маршрут"][i] = R.pr_at_k(m.corpus, r, q["gt"])
        for n in ns:
            res[f"реранкер N={n}"][i] = R.pr_at_k(m.corpus, rerank(r, sc, n), q["gt"])
        for n in ns:
            res[f"потолок N={n}"][i] = R.pr_at_k(m.corpus, oracle(m.corpus, r, q["gt"], n), q["gt"])
    ids = [q["i"] for q in qs]
    timing = {}
    for n in ns:
        per_q = [sum(x["seconds"] for x in calls[i][:n]) for i in ids]
        timing[str(n)] = {"q_p50": round(statistics.median(per_q), 2), "q_mean": round(statistics.fmean(per_q), 2),
                          "q_p90": round(sorted(per_q)[int(0.9 * (len(per_q) - 1))], 2)}
    flat = [x for i in ids for x in calls[i]]
    timing["call_p50"] = round(statistics.median(x["seconds"] for x in flat), 3)
    for k in ("prompt_tokens", "out_tokens"):
        vals = [x[k] for x in flat if x.get(k)]
        timing[k + "_p50"] = statistics.median(vals) if vals else None
    out = {"llm": args.llm, "n": ns, "router": REF_ROUTER, "cfg": BEST, "num_ctx": NUM_CTX,
           "sample": len(qs), "b_total": len(order),
           "sample_by_dataset": {d: sum(1 for q in qs if q["dataset"] == d) for d in lb.DATASETS},
           "queries": [{"i": q["i"], "dataset": q["dataset"]} for q in qs], "ids": ids,
           "variants": {k: R.pack(v, ids) for k, v in res.items()}, "timing": timing,
           "discrimination": discrimination_summary(disc),
           "scores": dict(sorted(hist.items())), "new_calls": llm.calls, "wall_seconds": round(time.perf_counter() - t0, 1)}
    R.save(Path(args.work) / f"rerank_{args.llm.replace(':', '_')}.json", out)


# ---------- hyde: гипотетический пункт документа ----------

GENRE = {"privacy_qa": "a privacy policy", "contractnli": "a non-disclosure agreement",
         "maud": "a merger agreement", "cuad": "a commercial contract"}
HYDE_SYSTEM = "You draft passages of legal documents."


def hyde_body(llm: str, dataset: str, ask: str) -> dict:
    user = (f"Write a short passage of {GENRE[dataset]}, two to four sentences, that answers the question below. "
            "Write only the passage text in the style of such a document, without a title or explanations.\n\n"
            f"Question: {ask}")
    return chat_body(llm, HYDE_SYSTEM, user, None, 220)


class MixScores(R.Scores):
    """Баллы самого вопроса с примесью черновика: (1 − β)·cos(сам вопрос, кусок) + β·cos(черновик, кусок).
    Это скалярное произведение куска со смесью векторов (без нормировки), то есть один поиск в Qdrant."""

    def __init__(self, base: R.Scores, draft: dict[str, array], beta: float):
        super().__init__(base.corpus, base.vec_of, arrays=base.arrays)
        self.draft, self.beta = draft, beta

    def doc(self, key, d: int):
        s = super().doc(key, d)
        if key[0] != "s" or not self.beta:
            return s
        h, b = self.draft[key[1]], self.beta
        return [(1 - b) * x + b * h[c] for x, c in zip(s, self.corpus.chunks_of[d])]


def run_hyde(args) -> None:
    stage = Path(args.cache) / "stage1"
    margs = model_args(args, stage)
    m = load(stage, margs)
    ds_of: dict[str, set] = defaultdict(set)
    for q in m.queries:
        ds_of[q["ask"]].add(q["dataset"])
    shared = sorted(a for a, d in ds_of.items() if len(d) > 1)
    asks = sorted(ds_of)
    llm = LLM(args.ollama, args.llm, Path(args.work) / "llm_cache.jsonl", timeout=args.timeout)
    print(f"HyDE {args.llm}: уникальных вопросов {len(asks)} (у {len(shared)} больше одного набора)", flush=True)
    drafts, gen = {}, []
    t0 = time.perf_counter()
    try:
        for j, a in enumerate(asks, 1):
            r = llm.ask(hyde_body(args.llm, sorted(ds_of[a])[0], a))
            drafts[a] = strip_think(r["content"]) or a
            gen.append(r)
            if j % 20 == 0 or j == len(asks):
                print(f"  черновиков {j}/{len(asks)}, новых {llm.calls}, {time.perf_counter() - t0:.0f} с", flush=True)
    finally:
        if args.unload:
            llm.unload()
    dvec, estats = R.embed_texts([margs.doc_prefix + d for d in drafts.values()], margs)
    mats = R.score_matrix([dvec[margs.doc_prefix + drafts[a]] for a in asks], m.corpus.vecs)
    draft_scores = dict(zip(asks, mats))
    scores = score_all(m)
    half = R.halves(m.queries)
    A = {i for i, h in half.items() if h == "A"}
    qa = [q for q in m.queries if q["i"] in A]
    grid = []  # меняется только вес черновика β; маршрут, a и μ как у лучшего маршрута
    for b in BETAS:
        ctx = R.QueryCtx(m.corpus, MixScores(scores, draft_scores, b), m.router, REF_ROUTER)
        res = R.run_configs([BEST], qa, ctx)[R.cfg_name(BEST)]
        grid.append([b, round(R.objective(res, m.queries, A), 3)])
    b_best = max(grid, key=lambda g: (g[1], -g[0]))[0]  # при равенстве меньший вес черновика
    print(f"  подбор на A: β = {b_best:g}", flush=True)
    shown = {"маршрут": 0.0, "HyDE, подбор на A": b_best, "HyDE вместо вопроса": 1.0}
    ids = [q["i"] for q in m.queries]
    variants = {}
    for label, b in shown.items():
        ctx = R.QueryCtx(m.corpus, MixScores(scores, draft_scores, b), m.router, REF_ROUTER)
        variants[label] = R.pack(R.run_configs([BEST], m.queries, ctx)[R.cfg_name(BEST)], ids)
    secs = [r["seconds"] for r in gen]
    out = {"llm": args.llm, "drafts": len(asks), "shared_asks": len(shared), "embed": estats,
           "gen": {"p50": round(statistics.median(secs), 2), "mean": round(statistics.fmean(secs), 2),
                   "total": round(sum(secs), 1),
                   "out_tokens_p50": statistics.median([r["out_tokens"] for r in gen if r.get("out_tokens")] or [0])},
           "grid": grid, "beta": b_best, "shown": shown,
           "queries": [{"i": q["i"], "dataset": q["dataset"], "half": half[q["i"]]} for q in m.queries],
           "variants": variants,
           "examples": [{"ask": a, "draft": drafts[a]} for a in asks[:: max(1, len(asks) // 3)][:3]]}
    R.save(Path(args.work) / f"hyde_{args.llm.replace(':', '_')}.json", out)


# ---------- отчёт ----------

def measures(rk: list[float], pk: list[float]) -> dict[str, float]:
    return {"r1": rk[0], "r4": rk[2], "r8": rk[3], "r64": rk[6], "rm": statistics.fmean(rk), "pm": statistics.fmean(pk)}


def tstats(v: dict, ref: dict | None, qinfo: dict[int, str], ids: list[int], subset: set[int]) -> dict:
    """Точки и 95 % интервалы в процентах (бутстреп по вопросам внутри наборов, наборы с равными весами);
    с ref ещё парные разности на тех же вопросах («d» + имя меры). v и ref выровнены по ids."""
    ser: dict = defaultdict(lambda: defaultdict(list))
    for p, i in enumerate(ids):
        if i not in subset:
            continue
        d, x = qinfo[i], measures(v["R"][p], v["P"][p])
        y = measures(ref["R"][p], ref["P"][p]) if ref is not None else None
        for nm, val in x.items():
            ser[nm][d].append(100 * val)
            if y is not None:
                ser["d" + nm][d].append(100 * (val - y[nm]))
    return R.boot({nm: dict(s) for nm, s in ser.items()})


def ds_means(v: dict, ids: list[int], qinfo: dict[int, str], subset: set[int]) -> dict[str, float]:
    """Средний recall по k по наборам, в процентах (без интервалов)."""
    per: dict = defaultdict(list)
    for p, i in enumerate(ids):
        if i in subset:
            per[qinfo[i]].append(statistics.fmean(v["R"][p]))
    return {d: 100 * statistics.fmean(x) for d, x in per.items()}


def take(v: dict, ids_from: list[int], ids_to: list[int]) -> dict:
    """Строки упакованного варианта для другого списка вопросов (ids_to ⊆ ids_from)."""
    pos = {i: p for p, i in enumerate(ids_from)}
    return {"R": [v["R"][pos[i]] for i in ids_to], "P": [v["P"][pos[i]] for i in ids_to]}


def verdict(t: tuple) -> str:
    if t[1] <= 0 <= t[2]:
        return "в пределах шума (интервал накрывает ноль)"
    return "лучше" if t[1] > 0 else "хуже"


def cfg_short(c: dict, router: str) -> str:
    rep, side = router.split("|")
    return (f"маршрутизатор {R.REP_RU[rep]} × {R.SIDE_RU[side]}; a = {c['a']:g}, μ = {c.get('mu', 0):g}")


MODEL_HEAD = ["| Модель | Вариант | R@1 | R@8 | R@64 | средний R по k | Δ R к {ref} | средний P по k | Δ P к {ref} |",
              "|---|---|---|---|---|---|---|---|---|"]


def models_section(mo: dict) -> tuple[list[str], list[str]]:
    qinfo = {q["i"]: q["dataset"] for q in mo["queries"]}
    ids = [q["i"] for q in mo["queries"]]
    subsets = {"B": {q["i"] for q in mo["queries"] if q["half"] == "B"}, "all": set(ids)}
    order, stg = mo["order"], mo["stages"]
    ref = order[0]
    st = {(lb_, var, sn): tstats(stg[lb_]["variants"][var], None if lb_ == ref else stg[ref]["variants"][var],
                                 qinfo, ids, sub)
          for lb_ in order for var in VARIANTS for sn, sub in subsets.items()}
    own = {lb_: tstats(stg[lb_]["variants"]["маршрут"], stg[lb_]["variants"]["база"], qinfo, ids, subsets["B"])
           for lb_ in order}

    def table(sn: str) -> list[str]:
        L = [h.format(ref=ref) for h in MODEL_HEAD]
        for lb_ in order:
            for var in VARIANTS:
                s = st[lb_, var, sn]
                dr = R.fmt(s["drm"], True) if "drm" in s else "база"
                dp = R.fmt(s["dpm"], True) if "dpm" in s else "база"
                L.append(f"| {lb_} | {var} | {s['r1'][0]:.1f} | {s['r8'][0]:.1f} | {s['r64'][0]:.1f} | "
                         f"{R.fmt(s['rm'])} | {dr} | {R.fmt(s['pm'])} | {dp} |")
        return L

    def canonical(lb_: str) -> bool:  # префиксы те, что ждёт модель; иначе это справка
        return (stg[lb_]["query_prefix"], stg[lb_]["doc_prefix"]) == prefixes(stg[lb_]["model"])

    short = []
    for lb_ in order[1:]:
        b, r = st[lb_, "база", "B"]["drm"], st[lb_, "маршрут", "B"]["drm"]
        short.append(f"- {lb_} против {ref}, средний recall по k на B: база Δ {R.fmt(b, True)}, {verdict(b)}; "
                     f"маршрут Δ {R.fmt(r, True)}, {verdict(r)}.")
    gains = ", ".join(f"{lb_} {R.fmt(own[lb_]['drm'], True)}" for lb_ in order)
    short.append(f"- Для сравнения маршрут к своей базе (та же модель, B): {gains}.")
    rivals = [lb_ for lb_ in order[1:] if canonical(lb_)]
    if rivals:
        ds = [st[lb_, "маршрут", "B"]["drm"][0] for lb_ in rivals]
        own_pts = [own[lb_]["drm"][0] for lb_ in [ref, *rivals]]
        short.insert(0, f"- Смена модели векторов сдвигает средний recall по k маршрута на B от {min(ds):+.1f} до "
                        f"{max(ds):+.1f} п.п. к {ref}; сам маршрут к своей базе даёт от {min(own_pts):+.1f} до "
                        f"{max(own_pts):+.1f} п.п. у тех же моделей.")
    for lb_ in order[1:]:
        s = stg[lb_]
        if not canonical(lb_) and s.get("ready_queries_same") is not None:
            short.append(f"- {lb_}: так лежат векторы вопросов в готовом этапе cache/stage1_arctic. Вектор вопроса без "
                         f"префикса модели совпал с ними у {s['ready_queries_same']} из {s['queries_n']} вопросов, "
                         "то есть префикс «query: » там не ставился; сравнение по этим векторам занижает модель.")
    L = ["## Модели векторов", "",
         "Каждая модель считает всё сама: векторы кусков, средний вектор кусков документа для маршрутизатора, ссылку, "
         "сам вопрос и весь вопрос, со своими префиксами. Нарезка, вопросы, половины A и B, метрика и код те же. "
         "«Маршрут» это параметры, подобранные для bge-m3 (" + cfg_short(mo["best"], mo["router"]) + ", документ за "
         "документом). «Маршрут, подбор на A» это справка: маршрутизатор и a, μ заново подобраны на A для каждой модели. "
         f"Δ к {ref} считается парно на тех же вопросах для того же варианта.", "",
         "### Половина B (проверка)", "", *table("B"), "",
         "### Все 776 вопросов (справка: на A подбирались параметры)", "", *table("all"), "",
         "### Маршрут к своей базе, половина B", "",
         "| Модель | Δ средний R по k | Δ R@8 | Δ средний P по k |", "|---|---|---|---|"]
    for lb_ in order:
        o = own[lb_]
        L.append(f"| {lb_} | {R.fmt(o['drm'], True)} | {R.fmt(o['dr8'], True)} | {R.fmt(o['dpm'], True)} |")
    L += ["", "### По наборам, средний recall по k, половина B", "",
          "| Набор | Вариант | " + " | ".join(order) + " |", "|---|---|" + "---|" * len(order)]
    for var in VARIANTS[:2]:
        per = {lb_: ds_means(stg[lb_]["variants"][var], ids, qinfo, subsets["B"]) for lb_ in order}
        for d in lb.DATASETS:
            L.append(f"| {R.NAMES[d]} | {var} | " + " | ".join(f"{per[lb_].get(d, 0):.1f}" for lb_ in order) + " |")
    def pfx(s: str) -> str:
        return f"«{s}»" if s else "нет"

    L += ["", "### Модели, префиксы, маршрутизатор, подбор на A", "",
          "| Модель | Ollama | размерность | префикс вопроса | префикс документа | эталонный документ первым у маршрутизатора маршрута, % (A / B) | подбор на A | средний R по k на A |",
          "|---|---|---|---|---|---|---|---|"]
    for lb_ in order:
        s = stg[lb_]
        top1 = s["routing"][mo["router"]]
        tuned = s["tuned"]
        L.append(f"| {lb_} | {s['model']} | {s['dim']} | {pfx(s['query_prefix'])} | {pfx(s['doc_prefix'])} | "
                 f"{top1['A'][0]:.1f} / {top1['B'][0]:.1f} | {cfg_short(tuned, s['tuned_router'])} | {s['grid_top'][0][1]:.1f} |")
    L += ["", "Векторизация 17 403 кусков мини на Маке (Ollama, пакеты по 32):", "",
          "| Модель | кусков в секунду | время | откуда число |", "|---|---|---|---|"]
    seen = set()
    for lb_ in order:
        s = stg[lb_]
        sp = s.get("chunk_speed")
        if not sp or s["model"] in seen:
            continue
        seen.add(s["model"])
        src = "этот замер (шаг stage)" if sp["source"] == "stage" else "подготовка этапа (eval/prepare.py, meta.json)"
        L.append(f"| {s['model']} | {sp['chunks_per_second']:.1f} | {sp['embed_seconds'] / 60:.1f} мин | {src} |")
    return short, L + [""]


RERANK_HEAD = ["| Вариант | R@1 | R@4 | R@8 | средний R по k | Δ R@1 | Δ R@4 | Δ R@8 | Δ средний R по k | Δ средний P по k |",
               "|---|---|---|---|---|---|---|---|---|---|"]


def rerank_rows(rr: dict, base: dict, ids: list[int], qinfo: dict, labels: list[str]) -> list[str]:
    L = []
    for lbl in labels:
        v = take(rr["variants"][lbl], rr["ids"], ids)
        s = tstats(v, None if lbl == "маршрут" else base, qinfo, ids, set(ids))
        d = (lambda k: R.fmt(s[k], True) if k in s else "база")  # noqa: E731
        L.append(f"| {lbl} | {s['r1'][0]:.1f} | {s['r4'][0]:.1f} | {s['r8'][0]:.1f} | {R.fmt(s['rm'])} | "
                 f"{d('dr1')} | {d('dr4')} | {d('dr8')} | {d('drm')} | {d('dpm')} |")
    return L


def rerank_section(rrs: list[dict]) -> tuple[list[str], list[str]]:
    short, L = [], ["## LLM как реранкер", "",
                    "Поточечно: на каждый из первых N кусков лучшего маршрута (bge-m3) отдельный запрос, вопрос целиком "
                    "и кусок с заголовком «Document / Section», ответ JSON {\"score\": 0–10} по схеме Ollama, think "
                    "выключен, температура 0. Первые N кусков пересортированы по баллу, при равном балле порядок маршрута; "
                    "куски дальше N на своих местах. Поточечно, а не списком: каждый кусок оценивается независимо от "
                    "соседей (списку важен порядок кусков в промпте, и модель может потерять или повторить номер), ответ "
                    "это одно число, а сбой одного ответа портит один кусок, а не весь вопрос; списком не мерили. "
                    "N = 10 и N = 20 берут "
                    "одни и те же вызовы: первые 10 кусков это начало первых 20. «Потолок» это лучший возможный порядок "
                    "первых N кусков (по числу эталонных знаков), выше него реранкер этих N не поднимется.", ""]
    for rr in rrs:
        ids = rr["ids"]
        qinfo = {q["i"]: q["dataset"] for q in rr["queries"]}
        base = rr["variants"]["маршрут"]
        labels = ["маршрут"] + [f"реранкер N={n}" for n in rr["n"]] + [f"потолок N={n}" for n in rr["n"]]
        by = ", ".join(f"{R.NAMES[d]} {n}" for d, n in rr["sample_by_dataset"].items())
        L += [f"### {rr['llm']}: {rr['sample']} вопросов из {rr['b_total']} половины B", "",
              f"Подвыборка: {by}; зерно {R.SEED}." if rr["sample"] < rr["b_total"] else "Вся половина B.", "",
              *RERANK_HEAD, *rerank_rows(rr, base, ids, qinfo, labels), ""]
        per = {lbl: ds_means(rr["variants"][lbl], ids, qinfo, set(ids)) for lbl in labels}
        L += ["По наборам, средний recall по k:", "", "| Набор | " + " | ".join(labels) + " |",
              "|---|" + "---|" * len(labels)]
        for d in lb.DATASETS:
            if d in per["маршрут"]:
                L.append(f"| {R.NAMES[d]} | " + " | ".join(f"{per[lbl][d]:.1f}" for lbl in labels) + " |")
        tm = rr["timing"]
        L += ["", "| N | время LLM на вопрос, медиана | среднее | 90-й процентиль |", "|---|---|---|---|"]
        for n in rr["n"]:
            t = tm[str(n)]
            L.append(f"| {n} | {t['q_p50']:.1f} с | {t['q_mean']:.1f} с | {t['q_p90']:.1f} с |")
        hist = rr["scores"]
        shown = sorted((k for k in hist if k.isdigit()), key=int)
        L += ["", f"Один вызов: медиана {tm['call_p50']:.2f} с, токенов промпта {tm['prompt_tokens_p50'] or 0:.0f}, ответа "
              f"{tm['out_tokens_p50'] or 0:.0f} (медианы). Сколько кусков получили балл: "
              + ", ".join(f"{k} у {hist[k]}" for k in shown)
              + "; без балла (ответ не разобран) " + str(hist.get("нет", 0)) + ".", ""]
        dc = rr.get("discrimination")
        if dc and dc.get("pairs"):
            L += [f"Различает ли LLM куски: среди первых {max(rr['n'])} кусков у кусков с эталонными знаками средний балл "
                  f"{dc['gold_mean']}, балл 7 и выше у {dc['gold_ge7']} %; у остальных {dc['other_mean']} и {dc['other_ge7']} %. "
                  f"В парах «кусок с эталоном, кусок без» из одного вопроса (пар всего {dc['pairs']}) эталонный выше у балла "
                  f"LLM в {dc['llm_pairs']} % пар (равный балл считается половиной), у порядка маршрута в {dc['route_pairs']} %.", ""]
        for n in rr["n"]:
            s = tstats(rr["variants"][f"реранкер N={n}"], base, qinfo, ids, set(ids))
            o = tstats(rr["variants"][f"потолок N={n}"], base, qinfo, ids, set(ids))
            short.append(f"- Реранкер {rr['llm']}, N = {n}, {rr['sample']} вопросов B: Δ среднего recall по k "
                         f"{R.fmt(s['drm'], True)} ({verdict(s['drm'])}), Δ recall@1 {R.fmt(s['dr1'], True)}, "
                         f"Δ recall@8 {R.fmt(s['dr8'], True)}; потолок того же N {o['drm'][0]:+.1f}; LLM на вопрос "
                         f"{tm[str(n)]['q_p50']:.1f} с (медиана).")
    if len(rrs) > 1:  # сравнение моделей на общих вопросах
        a, b = rrs[0], rrs[1]
        common = [i for i in b["ids"] if i in set(a["ids"])]
        if common:
            qinfo = {q["i"]: q["dataset"] for q in b["queries"]}
            L += [f"### {b['llm']} против {a['llm']} на тех же {len(common)} вопросах", "",
                  f"Точки это {b['llm']}, Δ это {b['llm']} минус {a['llm']} на тех же вопросах.", "", *RERANK_HEAD]
            for n in b["n"]:
                lbl = f"реранкер N={n}"
                if lbl not in a["variants"]:
                    continue
                va, vb = take(a["variants"][lbl], a["ids"], common), take(b["variants"][lbl], b["ids"], common)
                s = tstats(vb, va, qinfo, common, set(common))
                L.append(f"| {b['llm']} − {a['llm']}, N={n} | {s['r1'][0]:.1f} | {s['r4'][0]:.1f} | {s['r8'][0]:.1f} | "
                         f"{R.fmt(s['rm'])} | {R.fmt(s['dr1'], True)} | {R.fmt(s['dr4'], True)} | {R.fmt(s['dr8'], True)} | "
                         f"{R.fmt(s['drm'], True)} | {R.fmt(s['dpm'], True)} |")
                if n == max(b["n"]):
                    short.append(f"- {b['llm']} против {a['llm']} на тех же {len(common)} вопросах B, N = {n}: Δ среднего "
                                 f"recall по k {R.fmt(s['drm'], True)}, {verdict(s['drm'])}; LLM на вопрос "
                                 f"{b['timing'][str(n)]['q_p50']:.1f} с против {a['timing'][str(n)]['q_p50']:.1f} с "
                                 "(медианы своих прогонов).")
            L.append("")
    return short, L


def hyde_section(hy: dict) -> tuple[list[str], list[str]]:
    qinfo = {q["i"]: q["dataset"] for q in hy["queries"]}
    ids = [q["i"] for q in hy["queries"]]
    B = {q["i"] for q in hy["queries"] if q["half"] == "B"}
    base = hy["variants"]["маршрут"]
    L = ["## HyDE", "",
         f"{hy['llm']} пишет на сам вопрос (часть после «;») короткий пункт документа того вида, что в наборе (политика "
         "конфиденциальности, NDA, договор слияния, коммерческий договор). Вектор черновика bge-m3; балл куска внутри "
         "документа 0.25·cos(весь вопрос) + 0.75·[(1 − β)·cos(сам вопрос) + β·cos(черновик)], соседи μ = 0.5, "
         f"маршрутизатор тот же: меняется только вес черновика β. Уникальных вопросов {hy['drafts']}, черновик на каждый "
         f"один раз. β подобран на A по среднему recall по k: β = {hy['beta']:g}.", "",
         "Половина B:", "", *RERANK_HEAD]
    for lbl in hy["variants"]:
        s = tstats(hy["variants"][lbl], None if lbl == "маршрут" else base, qinfo, ids, B)
        d = (lambda k: R.fmt(s[k], True) if k in s else "база")  # noqa: E731
        L.append(f"| {lbl} (β = {hy['shown'][lbl]:g}) | {s['r1'][0]:.1f} | {s['r4'][0]:.1f} | {s['r8'][0]:.1f} | "
                 f"{R.fmt(s['rm'])} | {d('dr1')} | {d('dr4')} | {d('dr8')} | {d('drm')} | {d('dpm')} |")
    g = hy["gen"]
    L += ["", "Подбор на A, средний recall по k при каждом β: "
          + ", ".join(f"{b:g} → {o:.1f}" for b, o in hy["grid"]) + ".", "",
          f"Черновик: медиана {g['p50']:.1f} с, всего {g['total'] / 60:.1f} мин на {hy['drafts']} черновиков, "
          f"токенов ответа {g['out_tokens_p50']:.0f} (медиана). Пример:", ""]
    for ex in hy["examples"][:1]:
        L += [f"> Вопрос: {ex['ask']}", ">", f"> Черновик: {ex['draft']}", ""]
    s = tstats(hy["variants"]["HyDE, подбор на A"], base, qinfo, ids, B)
    r = tstats(hy["variants"]["HyDE вместо вопроса"], base, qinfo, ids, B)
    short = [f"- HyDE ({hy['llm']}, вес черновика β = {hy['beta']:g} подобран на A) к маршруту на B: Δ среднего recall "
             f"по k {R.fmt(s['drm'], True)}, {verdict(s['drm'])}; черновик вместо самого вопроса (β = 1) "
             f"{R.fmt(r['drm'], True)}, {verdict(r['drm'])}."]
    return short, L


CAVEATS = [
    "## Оговорки", "",
    "- Только мини-набор (776 вопросов, 72 документа). На мини маршрутизатор ставит эталонный документ первым у всех "
    "моделей, поэтому разница маршрутов это разница в порядке кусков внутри документа; на полном наборе (714 "
    "документов) модели не сравнивали.",
    "- Половины A и B делят вопросы, а не документы. Перенастройка на A перебирает только маршрутизатор, a и μ; ширина "
    "маршрута (M, λ) та же, что у bge-m3.",
    "- Префиксы nomic-embed-text взяты из карточки модели (search_document / search_query); вариант без префиксов не мерили.",
    "- Скорость векторизации мерилась в разное время: bge-m3 и arctic при подготовке этапов, nomic в этом замере; "
    "на Маке при этом работали виртуальные машины стенда.",
    "- Реранкер и HyDE: одна формулировка промпта, она не подбиралась; qwen3 в Ollama (Q4_K_M), think выключен. Время "
    "LLM это время на Маке рядом с работающим стендом, а не на выделенном сервере.",
    "- HyDE берёт вид документа из набора (политика, NDA, слияние, коммерческий договор): в самом вопросе его нет, это "
    "подсказка от набора; живому сервису вид пришлось бы брать из документа, выбранного маршрутизатором.",
    "",
]


def answer_lines(mo: dict | None, rrs: list[dict], hys: list[dict]) -> list[str]:
    """Короткий ответ из чисел разделов: где теряется качество маршрута и что дают модель векторов и LLM."""
    if not mo:
        return []
    qinfo = {q["i"]: q["dataset"] for q in mo["queries"]}
    ids = [q["i"] for q in mo["queries"]]
    B = {q["i"] for q in mo["queries"] if q["half"] == "B"}
    stg, ref = mo["stages"], mo["order"][0]
    route = tstats(stg[ref]["variants"]["маршрут"], None, qinfo, ids, B)
    parts = []
    rivals = [x for x in mo["order"][1:] if (stg[x]["query_prefix"], stg[x]["doc_prefix"]) == prefixes(stg[x]["model"])]
    if rivals:
        d = {x: tstats(stg[x]["variants"]["маршрут"], stg[ref]["variants"]["маршрут"], qinfo, ids, B)["drm"] for x in rivals}
        best = max(rivals, key=lambda x: d[x][0])
        parts.append(f"лучшая из других моделей векторов ({best}) {R.fmt(d[best], True)}")
    for rr in rrs[:1]:
        n, rq = max(rr["n"]), {q["i"]: q["dataset"] for q in rr["queries"]}
        s = tstats(rr["variants"][f"реранкер N={n}"], rr["variants"]["маршрут"], rq, rr["ids"], set(rr["ids"]))
        o = tstats(rr["variants"][f"потолок N={n}"], rr["variants"]["маршрут"], rq, rr["ids"], set(rr["ids"]))
        parts.append(f"LLM-реранкер {rr['llm']} первых {n} кусков {R.fmt(s['drm'], True)} на {rr['sample']} вопросах B "
                     f"(потолок {o['drm'][0]:+.1f}, LLM {rr['timing'][str(n)]['q_p50']:.0f} с на вопрос)")
    for hy in hys[:1]:
        hq = {q["i"]: q["dataset"] for q in hy["queries"]}
        hids = [q["i"] for q in hy["queries"]]
        s = tstats(hy["variants"]["HyDE, подбор на A"], hy["variants"]["маршрут"], hq, hids, B)
        parts.append(f"HyDE {hy['llm']} {R.fmt(s['drm'], True)}")
    top1 = stg[ref]["routing"][mo["router"]]["B"][0]
    return [f"- Ответ. Маршрутизатор ставит эталонный документ первым у {top1:.1f} % вопросов B; у маршрута {ref} recall@64 "
            f"{route['r64'][0]:.1f}, recall@8 {route['r8'][0]:.1f}, recall@1 {route['r1'][0]:.1f}: эталон в выбранном "
            "документе, теряется порядок кусков внутри него. Поправки к среднему recall по k маршрута на B: "
            + "; ".join(parts) + "."]


def run_report(args) -> None:
    work = Path(args.work)
    short, body = [], []
    mo = R.load_json(work / "models.json") if (work / "models.json").exists() else None
    if mo:
        s, b = models_section(mo)
        short, body = short + s, body + b
    rrs = [R.load_json(p) for p in sorted(work.glob("rerank_*.json"), key=lambda p: (len(p.name), p.name))]
    if rrs:
        s, b = rerank_section(rrs)
        short, body = short + s, body + b
    hys = [R.load_json(p) for p in sorted(work.glob("hyde_*.json"))]
    for hy in hys:
        s, b = hyde_section(hy)
        short, body = short + s, body + b
    short = answer_lines(mo, rrs, hys) + short
    L = ["# Упирается ли качество в модель: модели векторов, LLM-реранкер, HyDE (LegalBench-RAG, мини)", "",
         "Замер без стенда, на замороженных векторах, тем же кодом, что маршрутизация (`eval/routing_eval.py`, итоги "
         "в `eval/results/routing.md`): нарезка naive500_title, точный поиск полным перебором, посимвольные recall и "
         "precision при k = 1, 2, 4, …, 64 и их среднее по k, как в статье SAC. Половины A и B те же (зерно "
         f"{R.SEED}): A для подбора, B для проверки. В скобках 95 % интервал, бутстреп по вопросам внутри наборов, "
         "наборы с равными весами; Δ считается парно на тех же вопросах. Разница, интервал которой накрывает ноль, "
         "не улучшение. Код `eval/models_eval.py`.", "", "## Коротко", "", *short, "", *body, *CAVEATS]
    Path(args.md).parent.mkdir(parents=True, exist_ok=True)
    Path(args.md).write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")
    print("\n".join(L))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=("stage", "models", "rerank", "hyde", "report"))
    ap.add_argument("--cache", default=str(ROOT / "cache"), help="папка с stage1/ от eval/prepare.py")
    ap.add_argument("--embed-cache", default=str(ROOT / "cache/embeddings"))
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-pause", type=float, default=1.0, help="пауза между порциями текстов, с")
    ap.add_argument("--work", default=str(ROOT / "runs/models"))
    ap.add_argument("--md", default=str(ROOT / "eval/results/models.md"))
    ap.add_argument("--portion", type=int, default=256, help="stage: текстов в порции")
    ap.add_argument("--model", help="stage: модель векторов")
    ap.add_argument("--out", help="stage: папка этапа")
    ap.add_argument("--chunks", help="stage: папка этапа с готовыми векторами кусков этой модели")
    ap.add_argument("--no-query-prefix", action="store_true", help="stage: вопросы без префикса (справка)")
    ap.add_argument("--stage", action="append", default=[], help="models: МЕТКА=папка этапа; первая это эталон")
    ap.add_argument("--llm", default="qwen3:8b")
    ap.add_argument("--n", default=",".join(map(str, RERANK_N)), help="rerank: сколько первых кусков пересортировать")
    ap.add_argument("--questions", type=int, default=0, help="rerank: первые K вопросов порядка B (0 — все)")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--no-unload", dest="unload", action="store_false", help="не выгружать LLM в конце шага")
    args = ap.parse_args(argv)
    {"stage": build_stage, "models": run_models, "rerank": run_rerank, "hyde": run_hyde,
     "report": run_report}[args.step](args)


if __name__ == "__main__":
    main()
