"""Коллекция договоров для выбора договора и гейта (app/routing.py): по точке на документ.

    build  на Маке: из набора кусков (points.jsonl, vectors.f32) считает векторы документов
           python3 -I -B deploy/qdrant/docs.py build --src cache/stage1/naive500_title
           → cache/stage1/naive500_title_docs/{points.jsonl, route.f32, gate.f32, meta.json}
    load   на машине кластера (как eval/qdrant_load.py): коллекция <куски>_docs и индекс поля file
           python3 -I -B docs.py load --url http://127.0.0.1:16333 --key-file KEY --dir docs --collection contracts_rf2

Векторы документа, как в замере (eval/routing_eval.py, eval/gate_eval.py):
- route: средний вектор кусков документа, нормированный; расстояние Cosine.
- gate: (вектор «Document: <название>» + вектор первого куска) / 2, оба нормированы; расстояние Dot,
  поэтому балл это среднее двух косинусов с нормированной ссылкой вопроса.
Название векторизует модель набора (meta.json над SRC) через Ollama на Маке, с кэшем по тексту.
Коллекция документов маленькая (72 точки у мини, 714 у полного корпуса): один шард с копией на каждой
машине, чтобы выбор договора жил и при двух выключенных машинах. load только на стандартной библиотеке.
"""
from __future__ import annotations

import argparse
import json
import math
import operator
import sys
import time
import urllib.error
import urllib.request
import uuid
from array import array
from pathlib import Path

TITLE_PREFIX = "Document: "  # как в начале куска ветки naive500_title и в замере гейта


def unit(v) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def doc_id(file: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"doc/{file}"))


def group(points: list[dict]) -> list[dict]:
    """Документы в порядке появления: файл, название, набор, номера его кусков и номер первого куска."""
    docs: dict[str, dict] = {}
    for i, p in enumerate(points):
        d = docs.get(p["file"])
        if d is None:
            d = docs[p["file"]] = {"file": p["file"], "title": p.get("title") or "", "dataset": p.get("dataset"),
                                   "rows": [], "first": i, "first_start": p["start"]}
        d["rows"].append(i)
        if p["start"] < d["first_start"]:
            d["first"], d["first_start"] = i, p["start"]
    return list(docs.values())


def doc_vectors(doc: dict, vec, title_vec: list[float]) -> tuple[list[float], list[float]]:
    """(route, gate) документа; vec(i) отдаёт вектор куска i."""
    acc = [0.0] * len(title_vec)
    for i in doc["rows"]:
        acc = list(map(operator.add, acc, vec(i)))
    first = unit(vec(doc["first"]))
    t = unit(title_vec)
    return unit(acc), [(x + y) / 2 for x, y in zip(t, first)]


def build(args) -> int:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))  # app/embedding.py; с -I папка скрипта сама в путь не попадает
    from app.embedding import OllamaEmbedder
    src = Path(args.src)
    meta = json.loads((src.parent / "meta.json").read_text())
    model, dim = args.model or meta["model"], int(meta.get("dim") or 1024)
    out = Path(args.out or f"{src}_docs")
    with open(src / "points.jsonl", encoding="utf-8") as f:
        points = [json.loads(line) for line in f]
    vecs = array("f")
    with open(src / "vectors.f32", "rb") as f:
        vecs.fromfile(f, len(points) * dim)
    docs = group(points)
    titles = [TITLE_PREFIX + d["title"] for d in docs]
    emb = OllamaEmbedder(model=model, url=args.ollama, cache_dir=args.embed_cache)
    try:
        emb.ensure(titles, progress=False)
        tvecs = [list(emb.vector(t)) for t in titles]
        computed = emb.computed
    finally:
        emb.close()
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "points.jsonl", "w", encoding="utf-8") as fp, \
            open(out / "route.f32", "wb") as fr, open(out / "gate.f32", "wb") as fg:
        for d, tv in zip(docs, tvecs):
            route, gate = doc_vectors(d, lambda i: vecs[i * dim:(i + 1) * dim], tv)
            fp.write(json.dumps({"id": doc_id(d["file"]), "file": d["file"], "title": d["title"],
                                 "dataset": d["dataset"], "chunks": len(d["rows"])}, ensure_ascii=False) + "\n")
            array("f", route).tofile(fr)
            array("f", gate).tofile(fg)
    (out / "meta.json").write_text(json.dumps({"model": model, "dim": dim, "docs": len(docs), "source": src.name,
                                               "title_prefix": TITLE_PREFIX}, indent=2) + "\n")
    print(f"{out}: документов {len(docs)} из {len(points)} кусков, модель {model}, новых векторов названий {computed}")
    return 0


def call(method: str, url: str, key: str, body: dict | None = None, timeout: float = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load(args) -> int:
    key = Path(args.key_file).read_text().strip()
    root, src = args.url.rstrip("/"), Path(args.dir)
    meta = json.loads((src / "meta.json").read_text())
    dim = int(meta["dim"])
    with open(src / "points.jsonl", encoding="utf-8") as f:
        docs = [json.loads(line) for line in f]
    route, gate = array("f"), array("f")
    with open(src / "route.f32", "rb") as f:
        route.fromfile(f, len(docs) * dim)
    with open(src / "gate.f32", "rb") as f:
        gate.fromfile(f, len(docs) * dim)
    name = args.collection + "_docs"
    base = f"{root}/collections/{name}"
    existing = {c["name"] for c in call("GET", f"{root}/collections", key)["result"]["collections"]}
    if name not in existing:
        call("PUT", base, key, {"vectors": {"route": {"size": dim, "distance": "Cosine"},
                                            "gate": {"size": dim, "distance": "Dot"}},
                                "shard_number": 1, "replication_factor": args.replicas})
    for j in range(0, len(docs), 256):
        batch = [{"id": d["id"], "vector": {"route": route[i * dim:(i + 1) * dim].tolist(),
                                            "gate": gate[i * dim:(i + 1) * dim].tolist()},
                  "payload": {k: d[k] for k in ("file", "title", "dataset", "chunks")}}
                 for i, d in enumerate(docs[j:j + 256], start=j)]
        call("PUT", f"{base}/points?wait=true", key, {"points": batch})
    # индекс поля file у кусков: поиск внутри договора идёт с фильтром по нему
    call("PUT", f"{root}/collections/{args.collection}/index?wait=true", key,
         {"field_name": "file", "field_schema": "keyword"})
    t0 = time.perf_counter()
    while call("GET", base, key)["result"].get("status") != "green" and time.perf_counter() - t0 < 120:
        time.sleep(1)
    info = call("GET", base, key)["result"]
    print(f"{name}: документов {info.get('points_count')}, статус {info.get('status')}, "
          f"копий {args.replicas}; индекс file у {args.collection}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--src", required=True, help="набор кусков: папка с points.jsonl и vectors.f32")
    b.add_argument("--out", help="по умолчанию <src>_docs")
    b.add_argument("--model", help="по умолчанию модель из meta.json набора")
    b.add_argument("--ollama", default="http://127.0.0.1:11434")
    b.add_argument("--embed-cache", default=str(Path(__file__).resolve().parents[2] / "cache/embeddings"))
    ld = sub.add_parser("load")
    ld.add_argument("--url", required=True)
    ld.add_argument("--key-file", required=True)
    ld.add_argument("--dir", required=True)
    ld.add_argument("--collection", required=True, help="коллекция кусков; документы лягут в <она>_docs")
    ld.add_argument("--replicas", type=int, default=3)
    args = ap.parse_args(argv)
    try:
        return build(args) if args.cmd == "build" else load(args)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"docs.py {args.cmd}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
