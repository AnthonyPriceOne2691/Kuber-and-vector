"""BM25 разреженными векторами для Qdrant.

Часть BM25 с частотой термина и длиной документа (k1, b) считается здесь, при индексации;
IDF применяет сам Qdrant (sparse-вектор с modifier "idf"). Так устроен BM25 в Qdrant.
Токенизация простая: нижний регистр, буквы и цифры, без короткого мусора и частых слов
английского. Термин превращается в индекс через crc32.
"""
from __future__ import annotations

import re
import zlib
from collections import Counter

TOKEN = re.compile(r"[a-z0-9]+")
STOP = frozenset("""a an and are as at be by for from has have in is it its of on or that the this to was were
will with shall any such other than which may not no all each upon under into these those their there""".split())


def tokens(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if len(t) > 1 and t not in STOP]


def term_id(term: str) -> int:
    return zlib.crc32(term.encode("utf-8")) & 0x7FFFFFFF


def doc_sparse(text: str, avgdl: float, k1: float = 1.2, b: float = 0.75) -> dict:
    toks = tokens(text)
    dl = len(toks) or 1
    weights: dict[int, float] = {}
    for term, tf in Counter(toks).items():
        w = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
        i = term_id(term)
        weights[i] = weights.get(i, 0.0) + w
    return {"indices": list(weights), "values": [round(v, 4) for v in weights.values()]}


def query_sparse(text: str) -> dict:
    ids = sorted({term_id(t) for t in tokens(text)})
    return {"indices": ids, "values": [1.0] * len(ids)}
