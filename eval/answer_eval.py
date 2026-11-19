"""Оценка ответа модели на LegalBench-RAG: ссылки, цитаты и честное «в документах этого нет».

Эталонных ответов в LegalBench-RAG нет, есть эталонные фрагменты (файл и диапазон знаков).
Поэтому меряем то, что проверяется без модели-судьи:
- вопросы «в базе» (мини-выборка, их документы в индексе): доля ответов, отказов,
  неподтверждённых ответов и ошибок формата; у ссылок доля указывающих на существующий кусок,
  доля цитат, найденных в куске дословно, и доля кусков, пересекающих эталонный фрагмент;
- вопросы «вне базы» (полный набор, их документов в индексе нет): правильное поведение это
  отказ; ответ здесь ошибка, это ответ по чужому договору.
Поиск точный по замороженным векторам (cache/stage1/naive500_title), как в оценке этапа 1.
Промпт и проверка ответа те же, что в сервисе (app/answer.py). Модель в Ollama на Маке.
Прогон докачиваемый: готовые вопросы лежат в --out и при повторном запуске пропускаются.

    python3 -I -B eval/answer_eval.py                      по 25 вопросов на набор в базе и по 15 вне
    python3 -I -B eval/answer_eval.py --report-only        только пересчитать отчёт по готовым ответам
Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import operator
import random
import statistics
import sys
import time
import urllib.request
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from app import answer as A  # noqa: E402
from app.embedding import OllamaEmbedder  # noqa: E402
from eval import legalbench as lb  # noqa: E402

B = 2000  # повторов бутстрепа


# ---------- индекс и поиск ----------

def load_index(d: Path, dim: int) -> tuple[list[dict], list[array]]:
    """Куски и их векторы, нормированные для косинусной близости."""
    points = []
    with open(d / "points.jsonl", encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            p.pop("sparse", None)  # BM25 здесь не нужен, бережём память
            points.append(p)
    raw = array("f")
    with open(d / "vectors.f32", "rb") as f:
        raw.fromfile(f, len(points) * dim)
    return points, [unit(raw[i * dim:(i + 1) * dim]) for i in range(len(points))]


def unit(v) -> array:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array("f", (x / n for x in v))


def search(qv: array, vecs: list[array], k: int) -> list[tuple[float, int]]:
    q = unit(qv)
    return heapq.nlargest(k, ((sum(map(operator.mul, q, v)), i) for i, v in enumerate(vecs)))


def as_hit(p: dict, score: float) -> dict:
    """Кусок в том же виде, что в ответе /search сервиса."""
    return {"score": round(score, 4), "document": p["title"], "section": p.get("section") or None,
            "file": p["file"], "span": [p["start"], p["end"]], "text": p["text"]}


def overlaps(file: str, span: list[int], gold: list[list]) -> bool:
    return any(gf == file and min(span[1], ge) > max(span[0], gs) for gf, gs, ge in gold)


# ---------- вопросы ----------

def in_base(stage: Path, dim: int, per_dataset: int, seed: int) -> list[dict]:
    with open(stage / "queries.jsonl", encoding="utf-8") as f:
        queries = [json.loads(line) for line in f]
    qv = array("f")
    with open(stage / "query_vectors.f32", "rb") as f:
        qv.fromfile(f, len(queries) * dim)
    by_ds: dict[str, list[int]] = {}
    for i, q in enumerate(queries):
        by_ds.setdefault(q["dataset"], []).append(i)
    rng = random.Random(seed)
    out = []
    for ds in lb.DATASETS:
        idx = by_ds.get(ds, [])
        for i in sorted(rng.sample(idx, min(per_dataset, len(idx)))):
            out.append({"kind": "in", "dataset": ds, "query": queries[i]["query"],
                        "gold": queries[i]["snippets"], "vector": qv[i * dim:(i + 1) * dim]})
    return out


def out_of_base(data: Path, indexed: set[str], per_dataset: int, seed: int) -> list[dict]:
    """Вопросы полного набора, чьих документов нет в индексе: правильный ответ это отказ."""
    rng = random.Random(seed)
    out = []
    for ds in lb.DATASETS:
        if not (data / "benchmarks" / f"{ds}.json").exists():
            continue
        pool = [t for t in lb.load_tests(str(data), ds)
                if not {s["file_path"] for s in t["snippets"]} & indexed]
        for t in rng.sample(pool, min(per_dataset, len(pool))):
            out.append({"kind": "out", "dataset": ds, "query": t["query"],
                        "gold": [[s["file_path"], s["span"][0], s["span"][1]] for s in t["snippets"]]})
    return out


# ---------- модель ----------

def ask(url: str, model: str, question: str, hits: list[dict], timeout: int = 300) -> tuple[dict, float]:
    body = json.dumps(A.chat_body(model, question, hits)).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data, time.perf_counter() - t0


def evaluate(q: dict, hits: list[dict], reply: dict, seconds: float) -> dict:
    checked = A.check(A.parse((reply.get("message") or {}).get("content", "")), hits, q["query"])
    for c in checked["citations"]:
        c["gold"] = bool(c["valid"]) and overlaps(c["file"], c["span"], q["gold"])
    out_tokens, out_ns = reply.get("eval_count"), reply.get("eval_duration")
    return {
        "kind": q["kind"], "dataset": q["dataset"], "query": q["query"], "gold": q["gold"],
        "retrieved": [[h["file"], *h["span"]] for h in hits],
        "gold_retrieved": any(overlaps(h["file"], h["span"], q["gold"]) for h in hits),
        "status": checked["status"], "answer": checked["answer"], "model_answer": checked.get("model_answer"),
        "citations": [{k: c.get(k) for k in ("n", "quote", "valid", "verified", "gold", "file", "span")}
                      for c in checked["citations"]],
        "seconds": round(seconds, 2), "prompt_tokens": reply.get("prompt_eval_count"), "output_tokens": out_tokens,
        "tokens_per_s": round(out_tokens / (out_ns / 1e9), 1) if out_tokens and out_ns else None,
    }


# ---------- итоги ----------

def boot(xs: list[float], seed: int = 0) -> tuple[float, float, float] | None:
    """Доля (или среднее) и 95 % интервал бутстрепом по вопросам, в процентах."""
    if not xs:
        return None
    rng = random.Random(seed)
    sims = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(B))
    return 100 * statistics.fmean(xs), 100 * sims[int(0.025 * B)], 100 * sims[int(0.975 * B) - 1]


def pct(xs: list[float], q: float) -> float | None:
    s = sorted(x for x in xs if x is not None)
    return s[min(len(s) - 1, int(q * len(s)))] if s else None


def summarize(records: list[dict]) -> dict:
    inn = [r for r in records if r["kind"] == "in"]
    out = [r for r in records if r["kind"] == "out"]
    st = lambda rs, s: [1.0 if r["status"] == s else 0.0 for r in rs]  # noqa: E731
    cits = [c for r in inn if r["status"] in ("answered", "unsupported") for c in r["citations"]]
    valid = [c for c in cits if c["valid"]]
    found = [r for r in inn if r["gold_retrieved"]]
    return {
        "n_in": len(inn), "n_out": len(out),
        "in": {s: boot(st(inn, s)) for s in ("answered", "refused", "unsupported", "invalid")},
        "in_gold_retrieved": boot([1.0 if r["gold_retrieved"] else 0.0 for r in inn]),
        "in_answered_when_found": boot(st(found, "answered")),
        "in_answered_with_gold_quote": boot([1.0 if r["status"] == "answered" and any(c["verified"] and c["gold"]
                                             for c in r["citations"]) else 0.0 for r in found]),
        "n_citations": len(cits),
        "cit_valid": boot([1.0 if c["valid"] else 0.0 for c in cits]),
        "cit_verified": boot([1.0 if c["verified"] else 0.0 for c in valid]),
        "cit_gold": boot([1.0 if c["gold"] else 0.0 for c in valid]),
        "out_declined": boot([1.0 if r["status"] in ("refused", "unsupported") else 0.0 for r in out]),
        "out_answered": boot(st(out, "answered")),
        "out_invalid": boot(st(out, "invalid")),
        "seconds_p50": pct([r["seconds"] for r in records], 0.5),
        "seconds_p95": pct([r["seconds"] for r in records], 0.95),
        "tokens_per_s_p50": pct([r["tokens_per_s"] for r in records], 0.5),
        "prompt_tokens_p50": pct([r["prompt_tokens"] for r in records], 0.5),
    }


def fmt(t: tuple[float, float, float] | None) -> str:
    return "нет данных" if t is None else f"{t[0]:.1f} [{t[1]:.1f}; {t[2]:.1f}]"


def report(s: dict, args: argparse.Namespace) -> str:
    L = ["# Ответ модели: ссылки, цитаты, честные отказы", "",
         f"Модель {args.model} в Ollama на Маке, температура 0. Поиск точный по `{Path(args.index).name}`, "
         f"k = {args.k}. Промпт и проверка как в сервисе (`app/answer.py`).",
         f"Вопросов в базе {s['n_in']}, вне базы {s['n_out']}. Проценты с 95 % интервалом (бутстреп по вопросам).",
         "Эталонных ответов в LegalBench-RAG нет: правильность смысла здесь не оценивается, только опора на текст.", "",
         "## Вопросы в базе", "", "| Исход | % |", "|---|---|",
         f"| ответ с подтверждённой цитатой | {fmt(s['in']['answered'])} |",
         f"| отказ «в документах этого нет» | {fmt(s['in']['refused'])} |",
         f"| ответ без подтверждения, скрыт | {fmt(s['in']['unsupported'])} |",
         f"| ответ не по формату | {fmt(s['in']['invalid'])} |",
         f"| поиск нашёл эталонный фрагмент | {fmt(s['in_gold_retrieved'])} |",
         f"| ответ, когда эталон найден | {fmt(s['in_answered_when_found'])} |",
         f"| ответ с цитатой из эталона, когда эталон найден | {fmt(s['in_answered_with_gold_quote'])} |", "",
         f"## Ссылки в ответах ({s['n_citations']})", "", "| Ссылка | % |", "|---|---|",
         f"| на существующий кусок | {fmt(s['cit_valid'])} |",
         f"| цитата найдена в куске дословно | {fmt(s['cit_verified'])} |",
         f"| кусок пересекается с эталонным фрагментом | {fmt(s['cit_gold'])} |", "",
         "## Вопросы вне базы", "", "| Исход | % |", "|---|---|",
         f"| отказ (правильно) | {fmt(s['out_declined'])} |",
         f"| ответ по чужому документу (ошибка) | {fmt(s['out_answered'])} |",
         f"| ответ не по формату | {fmt(s['out_invalid'])} |", "",
         "## Скорость", "", "| Мера | Значение |", "|---|---|",
         f"| время ответа p50 / p95, с | {s['seconds_p50']} / {s['seconds_p95']} |",
         f"| токенов в секунду, медиана | {s['tokens_per_s_p50']} |",
         f"| токенов промпта, медиана | {s['prompt_tokens_p50']} |", ""]
    return "\n".join(L)


# ---------- прогон ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/legalbenchrag"))
    ap.add_argument("--stage", default=str(ROOT / "cache/stage1"))
    ap.add_argument("--index", default=str(ROOT / "cache/stage1/naive500_title"))
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--embed-cache", default=str(ROOT / "cache/embeddings"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--per-dataset", type=int, default=25)
    ap.add_argument("--out-of-base", type=int, default=15)  # privacy_qa вне базы пуст: весь набор в мини
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "runs/answer/answers.jsonl"))
    ap.add_argument("--report", default=str(ROOT / "eval/results/answer.md"))
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if out.exists():
        with open(out, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                done[(r["kind"], r["dataset"], r["query"])] = r

    if not args.report_only:
        dim = json.loads((Path(args.stage) / "meta.json").read_text())["dim"]
        print("индекс…", flush=True)
        points, vecs = load_index(Path(args.index), dim)
        questions = in_base(Path(args.stage), dim, args.per_dataset, args.seed)
        questions += out_of_base(Path(args.data), {p["file"] for p in points}, args.out_of_base, args.seed)
        todo = [q for q in questions if (q["kind"], q["dataset"], q["query"]) not in done]
        need = [q["query"] for q in todo if "vector" not in q]
        if need:
            emb = OllamaEmbedder(model=args.embed_model, url=args.ollama, cache_dir=args.embed_cache)
            emb.ensure(need, progress=False)
            for q in todo:
                if "vector" not in q:
                    q["vector"] = emb.vector(q["query"])
        print(f"вопросов {len(questions)}, готово раньше {len(questions) - len(todo)}, осталось {len(todo)}", flush=True)
        with open(out, "a", encoding="utf-8") as f:
            for i, q in enumerate(todo, 1):
                hits = [as_hit(points[j], score) for score, j in search(q["vector"], vecs, args.k)]
                reply, seconds = ask(args.ollama, args.model, q["query"], hits)
                rec = evaluate(q, hits, reply, seconds)
                done[(rec["kind"], rec["dataset"], rec["query"])] = rec
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                print(f"  {i}/{len(todo)} {rec['kind']:3s} {rec['dataset']:11s} {rec['status']:11s} {seconds:5.1f} с",
                      flush=True)
        keep = {(q["kind"], q["dataset"], q["query"]) for q in questions}
        records = [r for key, r in done.items() if key in keep]
    else:
        records = list(done.values())

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(report(summarize(records), args), encoding="utf-8")
    print(f"отчёт: {args.report}")


if __name__ == "__main__":
    main()
