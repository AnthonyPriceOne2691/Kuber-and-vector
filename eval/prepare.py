"""На Маке: выборка LegalBench-RAG, нарезка, векторы bge-m3 через Ollama.

    python3 -I eval/prepare.py --check-only        только сверить мини-выборку со статьёй
    python3 -I eval/prepare.py                     мини: нарезать, посчитать векторы, записать файлы
    python3 -I eval/prepare.py --full --check-only полный набор: только посчитать документы и знаки
    python3 -I eval/prepare.py --full              полный корпус (714 документов) и полный тест

Мини (по умолчанию) пишет в cache/stage1/, полный в cache/full/ и только ветку naive500_title,
без разреженных векторов BM25 у кусков (в кластер идёт режим dense, а BM25 у полного корпуса это
сотни мегабайт в памяти и на диске). Векторы считаются докачиваемо: прерванный запуск продолжит
с того же места (кэш cache/embeddings). Результат:
    <стратегия>/points.jsonl   id, dataset, file, start, end, text (порядок = порядок векторов)
    <стратегия>/vectors.f32    float32 подряд
    queries.jsonl, query_vectors.f32, meta.json (замеры скорости)
Данные набора читаются как текст и JSON, код из них не исполняется.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
import os
import sys
import time
import uuid
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from app import bm25  # noqa: E402
from app.chunking import STRATEGIES  # noqa: E402
from app.embedding import OllamaEmbedder  # noqa: E402
from eval import legalbench as lb  # noqa: E402


def point_id(strategy: str, file: str, start: int, end: int) -> str:
    h = hashlib.sha256(f"{strategy}|{file}|{start}|{end}".encode()).digest()
    return str(uuid.UUID(bytes=h[:16]))


# Адрес куска: ближайший сверху заголовок раздела («Section 5.2 …», «ARTICLE V …», «5.2 Termination»).
# Эвристика; если заголовка нет, адресом остаются документ и позиция в знаках.
HEADING = re.compile(r"^[ \t]*(?:(?:section|article)\s+[0-9IVXLC]+(?:\.\d+)*\.?|\d+(?:\.\d+){0,3}\.?)[ \t]+[A-Z][^\n]{2,80}",
                     re.MULTILINE | re.IGNORECASE)


NUMBER = re.compile(r"(?:(?:section|article)\s+[0-9IVXLC]+(?:\.\d+)*\.?|\d+(?:\.\d+){0,3}\.?)\s+", re.IGNORECASE)


def section_index(text: str) -> tuple[list[int], list[str]]:
    starts, titles = [], []
    for m in HEADING.finditer(text):
        head = " ".join(m.group(0).split())
        num = NUMBER.match(head)
        rest = head[num.end():] if num else head
        cut = rest.find(". ")
        title = (head[:num.end()] if num else "") + (rest[:cut + 1] if cut > 0 else rest)
        starts.append(m.start())
        titles.append(title[:80])
    return starts, titles


def section_for(starts: list[int], titles: list[str], s: int, e: int) -> str:
    i = bisect.bisect_right(starts, s) - 1
    if i >= 0:
        return titles[i]
    j = bisect.bisect_left(starts, s)
    return titles[j] if j < len(starts) and starts[j] < e else ""


def title_of(file: str) -> str:
    return Path(file).stem.replace("_", " ").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/legalbenchrag"))
    ap.add_argument("--full", action="store_true", help="полный корпус и полный тест вместо мини-выборки")
    ap.add_argument("--out", default=None)
    ap.add_argument("--model", default="bge-m3")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-cache", default=str(ROOT / "cache/embeddings"))
    ap.add_argument("--strategies", default=None)
    ap.add_argument("--sparse", choices=("auto", "yes", "no"), default="auto",
                    help="BM25 у кусков: auto значит да для мини и нет для полного")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    out_default = "cache/full" if args.full else "cache/stage1"
    args.out = args.out or str(ROOT / out_default)
    strategies = args.strategies or ("naive500_title" if args.full else "naive500,naive500_title")
    with_sparse = args.sparse == "yes" or (args.sparse == "auto" and not args.full)

    selected: dict[str, list[dict]] = {}
    for name in lb.DATASETS:
        tests = lb.load_tests(args.data, name)
        selected[name] = tests if args.full else lb.select_mini(tests)

    print("Полный набор (документов, знаков):" if args.full else "Мини-выборка против статьи (документов, знаков):")
    docs: dict[str, str] = {}
    all_ok = True
    for name in lb.DATASETS:
        files = sorted(lb.corpus_files(selected[name]))
        chars = 0
        for f in files:
            docs[f] = lb.read_doc(args.data, f)
            chars += len(docs[f])
        if args.full:
            print(f"  {name:12s} вопросов {len(selected[name]):5d}  документов {len(files):3d}  знаков {chars:>11,}")
            continue
        want = lb.PAPER_MINI_CORPUS[name]
        ok = (len(files), chars) == want
        all_ok &= ok
        print(f"  {name:12s} вопросов {len(selected[name]):4d}  документов {len(files):3d} (статья {want[0]})"
              f"  знаков {chars:>10,} (статья {want[1]:>10,})  {'совпало' if ok else 'РАСХОЖДЕНИЕ'}")
    total_docs, total_chars = len(docs), sum(len(t) for t in docs.values())
    if args.full:
        print(f"  всего документов {total_docs}, знаков {total_chars:,}, вопросов {sum(map(len, selected.values()))}")
    else:
        print(f"  всего документов {total_docs} (статья 72), знаков {total_chars:,} (статья 8,682,104)")
    if args.check_only:
        sys.exit(0 if all_ok else 1)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    emb = OllamaEmbedder(model=args.model, url=args.ollama, cache_dir=args.embed_cache)
    meta: dict = {"model": args.model, "selection": "full" if args.full else "mini",
                  "datasets": {n: len(selected[n]) for n in lb.DATASETS},
                  "docs": total_docs, "chars": total_chars, "sparse": with_sparse, "strategies": {}}

    for strategy in strategies.split(","):
        # «naive500_title»: та же нарезка, но к тексту для поиска (вектор и BM25) приписан префикс
        # «Document: … / Section: …» (contextual embeddings без LLM). Адрес и смещения куска те же.
        base, _, variant = strategy.partition("_")
        chunker = STRATEGIES[base]
        points = []
        for f in sorted(docs):
            text = docs[f]
            starts, titles = section_index(text)
            for s, e in chunker(text):
                if text[s:e].strip():
                    points.append({"id": point_id(strategy, f, s, e), "dataset": f.split("/")[0],
                                   "file": f, "title": title_of(f), "section": section_for(starts, titles, s, e),
                                   "start": s, "end": e, "text": text[s:e]})
        for p in points:
            prefix = ""
            if variant == "title":
                prefix = f"Document: {p['title']}\n" + (f"Section: {p['section']}\n" if p["section"] else "") + "\n"
            p["index_text"] = prefix + p["text"]
        avgdl = sum(len(bm25.tokens(p["index_text"])) for p in points) / max(len(points), 1) if with_sparse else 0.0
        print(f"{strategy}: кусков {len(points)}")
        before_n, before_s = emb.computed, emb.seconds
        t0 = time.perf_counter()
        emb.ensure([p["index_text"] for p in points])
        wall = time.perf_counter() - t0
        new_n, new_s = emb.computed - before_n, emb.seconds - before_s
        d = out / strategy
        d.mkdir(exist_ok=True)
        with open(d / "points.jsonl", "w", encoding="utf-8") as fp, open(d / "vectors.f32", "wb") as fv:
            for p in points:
                index_text = p.pop("index_text")
                if with_sparse:
                    p["sparse"] = bm25.doc_sparse(index_text, avgdl)
                fp.write(json.dumps(p, ensure_ascii=False) + "\n")
                emb.vector(index_text).tofile(fv)
        meta["strategies"][strategy] = {
            "chunks": len(points), "new_vectors": new_n, "embed_seconds": round(new_s, 1),
            "wall_seconds": round(wall, 1), "chunks_per_second": round(new_n / new_s, 1) if new_s else None}
        print(f"  посчитано новых векторов {new_n} за {new_s:.0f} с"
              + (f", {new_n / new_s:.1f} кусков/с" if new_s else ""))

    queries = [{"dataset": n, "query": t["query"], "sparse": bm25.query_sparse(t["query"]),
                "snippets": [[s["file_path"], s["span"][0], s["span"][1]] for s in t["snippets"]]}
               for n in lb.DATASETS for t in selected[n]]
    emb.ensure([q["query"] for q in queries], progress=False)
    with open(out / "queries.jsonl", "w", encoding="utf-8") as fq, open(out / "query_vectors.f32", "wb") as fv:
        for q in queries:
            fq.write(json.dumps(q, ensure_ascii=False) + "\n")
            emb.vector(q["query"]).tofile(fv)
    meta["queries"] = len(queries)
    meta["dim"] = emb.dim
    emb.close()
    with open(out / "meta.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"готово: {out}  (вопросов {len(queries)}, размерность {emb.dim})")


if __name__ == "__main__":
    main()
