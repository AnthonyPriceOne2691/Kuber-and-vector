"""Поиск «от малого к большому» (parent-document retrieval): дети для поиска, родитель в выдаче.

Родитель это готовый кусок naive500_title (500 знаков, адрес: файл, раздел, знаки). Его текст режется
на детей по --size знаков с шагом --step; к каждому ребёнку приписан тот же заголовок, что у родителя
(«Document: … / Section: …», eval/prepare.py index_text), и по этому тексту считается вектор. В точке
ребёнка адрес родителя (file, start, end) и поле parent: поиск группирует детей по родителю и отдаёт
родителя, поэтому единица выдачи та же, что у базы, и recall@k сравним с ней один к одному.
Приём из журнала RAG владельца (шаг 15: мелкие куски с заголовком для поиска, родитель для ответа).

    python3 -I -B eval/children.py --parents cache/stage1/naive500_title --out cache/stage1_children/children200
Только стандартная библиотека; векторы bge-m3 через Ollama на Маке с кэшем по тексту (app/embedding.py).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from app.embedding import OllamaEmbedder  # noqa: E402
from eval.prepare import index_text  # noqa: E402


def children_of(parent: dict, size: int, step: int) -> list[dict]:
    """Дети родителя: куски его текста по size знаков с шагом step, хвост короче size тоже ребёнок.
    Пустые (одни пробелы) пропускаются. Адрес в точке родительский, своё место у ребёнка в child_start."""
    text = parent["text"]
    out = []
    for i in range(0, len(text), step):
        piece = text[i:i + size]
        if piece.strip():
            out.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"child/{parent['id']}/{i}/{size}")),
                        "dataset": parent["dataset"], "file": parent["file"], "title": parent["title"],
                        "section": parent["section"], "start": parent["start"], "end": parent["end"],
                        "parent": parent["id"], "child_start": parent["start"] + i, "text": piece})
        if i + size >= len(text):
            break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--parents", default=str(ROOT / "cache/stage1/naive500_title"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=200)
    ap.add_argument("--step", type=int, default=200)
    ap.add_argument("--model", default="bge-m3")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-cache", default=str(ROOT / "cache/embeddings"))
    args = ap.parse_args(argv)

    with open(Path(args.parents) / "points.jsonl", encoding="utf-8") as f:
        parents = [json.loads(line) for line in f]
    kids = [c for p in parents for c in children_of(p, args.size, args.step)]
    texts = [index_text(c, "title") for c in kids]
    print(f"родителей {len(parents)}, детей {len(kids)} (по {args.size} знаков, шаг {args.step})")

    emb = OllamaEmbedder(model=args.model, url=args.ollama, cache_dir=args.embed_cache)
    t0 = time.perf_counter()
    emb.ensure(texts)
    print(f"векторы: новых {emb.computed} за {time.perf_counter() - t0:.0f} с")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "points.jsonl", "w", encoding="utf-8") as fp, open(out / "vectors.f32", "wb") as fv:
        for c, t in zip(kids, texts):
            fp.write(json.dumps(c, ensure_ascii=False) + "\n")
            emb.vector(t).tofile(fv)
    (out / "meta.json").write_text(json.dumps({"model": args.model, "size": args.size, "step": args.step,
                                               "parents": len(parents), "children": len(kids)}, indent=2))
    print(f"готово: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
