"""Эталон LegalBench-RAG для сцены поиска: вопрос -> фрагменты, размеченные юристами; куски
сервиса против разметки; адрес куска против исходного файла.

Координаты одни и те же. Сервис отдаёт у куска file (путь в корпусе) и span [start, end] из
нарезки eval/prepare.py: это индексы символов Python str в тексте документа, прочитанном
lb.read_doc (UTF-8, текстовый режим). Эталон задаёт фрагмент тем же путём (file_path) и span
[start, end] в тех же единицах: у мини-набора doc[span] совпадает с полем answer у всех 1307
фрагментов (сверено 19.11). Поэтому пересечение считается прямо по числам, как у авторов
(lb.precision_recall), а подсветка внутри куска режется здесь, в Python: в JS индексы строк
идут в UTF-16 и на символах вне BMP разошлись бы с нашими.

Данные набора читаются как JSON и текст, код из них не исполняется.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

from eval import legalbench as lb

DATASET_NAMES = {"privacy_qa": "PrivacyQA", "contractnli": "ContractNLI", "maud": "MAUD", "cuad": "CUAD"}


def norm_query(q: str) -> str:
    """Ключ вопроса: пробелы и переводы строк схлопнуты, регистр не трогаем."""
    return " ".join((q or "").split())


def title_of(file: str) -> str:
    """Название документа из имени файла, как в eval/prepare.py (у кусков сервиса то же поле)."""
    return Path(file).stem.replace("_", " ").strip()


def merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Склеить пересекающиеся и соседние отрезки [a, b)."""
    out: list[tuple[int, int]] = []
    for a, b in sorted(r for r in ranges if r[1] > r[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def segments(text: str, ranges: list[tuple[int, int]]) -> list[dict]:
    """Текст куска кусочками: truth=True там, где он пересекается с разметкой. Отрезки в
    координатах самого куска (0 = его start), уже склеены. Склейка кусочков даёт исходный текст."""
    out, pos = [], 0
    for a, b in ranges:
        a, b = max(a, pos), min(b, len(text))
        if a >= b:
            continue
        if a > pos:
            out.append({"text": text[pos:a], "truth": False})
        out.append({"text": text[a:b], "truth": True})
        pos = b
    if pos < len(text) or not out:
        out.append({"text": text[pos:], "truth": False})
    return out


def _hit_address(hit) -> tuple[str, int, int, str] | None:
    """(file, start, end, text) из куска ответа сервиса; None, если форма не та."""
    if not isinstance(hit, dict):
        return None
    file, span, text = hit.get("file"), hit.get("span"), hit.get("text")
    if not isinstance(file, str) or not isinstance(text, str) or not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    start, end = span
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)) or not 0 <= start <= end:
        return None
    return file, start, end, text


class Benchmark:
    """Вопросы LegalBench-RAG всех четырёх наборов, индекс по тексту вопроса, корпус по запросу.

    root: папка с benchmarks/ и corpus/ (data/legalbenchrag). Нет папки — available() False,
    сцена поиска работает без подсветки и честно пишет, что разметки нет."""

    def __init__(self, root: str | None, doc_cache: int = 32) -> None:
        self.root = os.path.realpath(root) if root else None
        self.error: str | None = None
        self._index: dict[str, list[tuple[str, dict, bool]]] | None = None
        self._lock = threading.Lock()
        self._docs: OrderedDict[str, str] = OrderedDict()
        self._doc_cache = doc_cache

    # --- вопросы ---------------------------------------------------------------------------
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> dict | None:
        with self._lock:
            if self._index is not None or self.error is not None:
                return self._index
            if not self.root or not os.path.isdir(os.path.join(self.root, "benchmarks")):
                self.error = "нет папки benchmarks"
                return None
            index: dict[str, list[tuple[str, dict, bool]]] = {}
            try:
                for name in lb.DATASETS:
                    tests = lb.load_tests(self.root, name)
                    mini = {id(t) for t in lb.select_mini(tests)}
                    for t in tests:
                        index.setdefault(norm_query(t["query"]), []).append((name, t, id(t) in mini))
            except (OSError, ValueError, KeyError, TypeError) as e:
                self.error = f"набор не прочитан: {type(e).__name__}"
                return None
            self._index = index
            return index

    def lookup(self, q: str) -> dict | None:
        """Эталон вопроса: размеченные фрагменты с текстом; None, если вопроса нет в наборе.
        Каждый вызов собирает новый словарь: вызывающий может его дополнять."""
        index = self._load()
        entries = (index or {}).get(norm_query(q))
        if not entries:
            return None
        seen, snippets = set(), []
        for _, t, _ in entries:
            for s in t.get("snippets") or []:
                file, span = s.get("file_path"), s.get("span")
                if not isinstance(file, str) or not isinstance(span, list) or len(span) != 2:
                    continue
                key = (file, span[0], span[1])
                if key in seen:
                    continue
                seen.add(key)
                text = s.get("answer")
                if not isinstance(text, str):
                    doc = self.doc(file)
                    text = doc[span[0]:span[1]] if doc is not None else ""
                snippets.append({"file": file, "document": title_of(file), "span": [span[0], span[1]],
                                 "chars": span[1] - span[0], "text": text})
        datasets = sorted({name for name, _, _ in entries})
        return {"dataset": datasets[0], "dataset_name": DATASET_NAMES.get(datasets[0], datasets[0]),
                "datasets": datasets, "in_mini": any(m for _, _, m in entries),
                "snippets": snippets, "total_chars": sum(s["chars"] for s in snippets)}

    # --- корпус ----------------------------------------------------------------------------
    def doc(self, file: str) -> str | None:
        """Текст документа корпуса (как lb.read_doc) или None. Путь приходит со стенда, поэтому
        проверяется: файл только внутри corpus/, без выхода наверх по «..» и ссылкам."""
        if not self.root or not isinstance(file, str) or not file or "\x00" in file:
            return None
        with self._lock:
            if file in self._docs:
                self._docs.move_to_end(file)
                return self._docs[file]
        corpus = os.path.realpath(os.path.join(self.root, "corpus"))
        full = os.path.realpath(os.path.join(corpus, file))
        if not full.startswith(corpus + os.sep) or not os.path.isfile(full):
            return None
        try:
            text = lb.read_doc(self.root, os.path.relpath(full, corpus))
        except (OSError, UnicodeDecodeError):
            return None
        with self._lock:
            self._docs[file] = text
            while len(self._docs) > self._doc_cache:
                self._docs.popitem(last=False)
        return text

    def check_span(self, file: str, start: int, end: int, text: str) -> bool | None:
        """Адрес куска против исходного файла: doc[start:end] == text. None — файла здесь нет."""
        doc = self.doc(file)
        if doc is None:
            return None
        return end <= len(doc) and doc[start:end] == text

    # --- сопоставление ---------------------------------------------------------------------
    def annotate(self, hits: list, truth: dict | None) -> dict:
        """Куски сервиса против эталона. Для каждого куска: пересекается ли с разметкой (match;
        None — эталона у вопроса нет), сколько знаков общих, текст кусочками для подсветки и
        проверка адреса по файлу. Для вопроса: recall и precision по формуле авторов на этих k
        кусках; для каждого фрагмента разметки — номера кусков (с 1), которые его задели."""
        snippets = (truth or {}).get("snippets") or []
        found_in: list[list[int]] = [[] for _ in snippets]
        out, retrieved = [], []
        for rank, hit in enumerate(hits or [], 1):
            addr = _hit_address(hit)
            if addr is None:
                text = hit.get("text") if isinstance(hit, dict) else None
                out.append({"match": None, "overlap": 0, "span_ok": None,
                            "segments": [{"text": text if isinstance(text, str) else "", "truth": False}]})
                continue
            file, start, end, text = addr
            retrieved.append((file, start, end))
            ranges = []
            for i, sn in enumerate(snippets):
                if sn["file"] != file:
                    continue
                a, b = max(start, sn["span"][0]), min(end, sn["span"][1])
                if a < b:
                    ranges.append((a - start, b - start))
                    found_in[i].append(rank)
            ranges = merge(ranges)
            # длина текста не та, что у адреса: координаты и текст разошлись, подсветку не режем
            consistent = len(text) == end - start
            out.append({
                "match": None if truth is None else bool(ranges),
                "overlap": sum(b - a for a, b in ranges),
                "segments": segments(text, ranges if consistent else []),
                "span_ok": self.check_span(file, start, end, text),
            })
        coverage = None
        if truth is not None:
            gt = [(sn["file"], sn["span"][0], sn["span"][1]) for sn in snippets]
            precision, recall = lb.precision_recall(retrieved, gt)
            coverage = {"k": len(hits or []), "recall": round(min(recall, 1.0), 4), "precision": round(precision, 4)}
        return {"hits": out, "coverage": coverage, "found_in": found_in}
