"""Гейт «этого договора в базе нет»: уверенность выбора документа как порог отказа (LegalBench-RAG).

Приём «гейт отвечаемости» (answerability gate): если ответа в базе нет, сервис отказывает и модель не
вызывает. Балл вопроса здесь это лучший косинус ссылки на договор (часть вопроса до первой «;») с
представлением документа базы, как у маршрутизатора `eval/routing_eval.py`. Ниже порога ответ
«этого договора в базе нет».

База это мини-набор (cache/stage1/naive500_title, 72 документа), как у оценки ответа
(`eval/answer_eval.py`). В базе: 776 вопросов мини. Вне базы: вопросы полного набора, чьих документов в
мини нет. Порог подбирается на половине A, отчёт на B. В базе половины те же, что у routing_eval
(зерно 2026); вне базы половины делятся по документам, чтобы порог не видел документов проверки.

    python3 -I -B eval/gate_eval.py --cache cache --embed-cache <кэш коротких текстов> --md eval/results/gate.md
Векторы ссылок bge-m3 через Ollama на Маке с кэшем по тексту; стенд не нужен.
"""
from __future__ import annotations

import argparse
import bisect
import datetime
import hashlib
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from eval import routing_eval as RE  # noqa: E402

GATE_REPS = ("centroid", "first", "title", "title+first")
MAX_FALSE = (0.01, 0.02, 0.05)  # допустимая доля ложных отказов в базе на A
BOOT = 2000


def doc_half(file: str) -> str:
    """Половина вопроса вне базы по его документу: все вопросы одного договора в одной половине."""
    return "A" if hashlib.sha256(file.encode()).digest()[0] % 2 == 0 else "B"


def auc(pos: list[float], neg: list[float]) -> float:
    """Вероятность, что вопрос из базы получит балл выше вопроса вне базы (ничьи пополам)."""
    s = sorted(neg)
    total = 0.0
    for x in pos:
        lo, hi = bisect.bisect_left(s, x), bisect.bisect_right(s, x)
        total += lo + 0.5 * (hi - lo)
    return total / (len(pos) * len(neg))


def threshold_for(in_scores: list[float], max_false: float) -> float:
    """Наибольший порог, при котором отказ (балл < порога) получают не больше max_false вопросов из базы."""
    s = sorted(in_scores)
    return s[int(max_false * len(s))]


def youden(in_scores: list[float], out_scores: list[float]) -> float:
    """Порог с наибольшей разницей «верные отказы вне базы − ложные отказы в базе»."""
    best, best_t = -2.0, 0.0
    si, so = sorted(in_scores), sorted(out_scores)
    for t in sorted(set(in_scores) | set(out_scores)):
        j = bisect.bisect_left(so, t) / len(so) - bisect.bisect_left(si, t) / len(si)
        if j > best:
            best, best_t = j, t
    return best_t


def refused(scores: list[float], t: float) -> list[float]:
    return [1.0 if x < t else 0.0 for x in scores]


def ci(xs: list[float], seed: int = 0) -> tuple[float, float, float]:
    """Доля в процентах и 95 % интервал бутстрепом по вопросам."""
    rng = random.Random(seed)
    n = len(xs)
    sims = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(BOOT))
    return 100 * statistics.mean(xs), 100 * sims[int(0.025 * BOOT)], 100 * sims[int(0.975 * BOOT)]


def fmt(t: tuple[float, float, float]) -> str:
    return f"{t[0]:.1f} [{t[1]:.1f}; {t[2]:.1f}]"


def load(stage: Path, dim: int) -> list[dict]:
    queries, _ = RE.load_queries(stage, dim)
    return queries


def evaluate(router: RE.Router, inside: list[dict], outside: list[dict], in_half: dict[int, str]) -> dict:
    """Баллы, AUC и пороги для каждого представления документа."""
    out = {}
    for rep in GATE_REPS:
        name = f"{rep}|ref"
        s_in = {q["i"]: max(router.scores(q, name)) for q in inside}
        s_out = [(q, max(router.scores(q, name))) for q in outside]
        inA = [s for i, s in s_in.items() if in_half[i] == "A"]
        inB = [s for i, s in s_in.items() if in_half[i] == "B"]
        outA = [s for q, s in s_out if q["half"] == "A"]
        outB = [s for q, s in s_out if q["half"] == "B"]
        refsB = {}
        for q, s in s_out:
            if q["half"] == "B":
                refsB[(q["dataset"], q["ref"])] = s
        r = {"auc_A": auc(inA, outA), "auc_B": auc(inB, outB), "n": {"inA": len(inA), "inB": len(inB),
             "outA": len(outA), "outB": len(outB), "refsB": len(refsB)}, "thresholds": []}
        for name_t, t in [(f"≤ {int(m * 100)} % ложных отказов на A", threshold_for(inA, m)) for m in MAX_FALSE] + \
                         [("лучший баланс на A", youden(inA, outA))]:
            per_ds = {}
            for d in RE.DS:
                xs = [s for q, s in s_out if q["half"] == "B" and q["dataset"] == d]
                if xs:
                    per_ds[d] = 100 * statistics.mean(refused(xs, t))
            r["thresholds"].append({
                "rule": name_t, "t": t,
                "false_B": ci(refused(inB, t)), "true_B": ci(refused(outB, t)),
                "true_refs_B": 100 * statistics.mean(refused(list(refsB.values()), t)),
                "true_B_by_ds": per_ds})
        out[rep] = r
    return out


def report(res: dict, n_docs: int) -> list[str]:
    any_rep = next(iter(res.values()))["n"]
    L = [f"# Гейт «этого договора в базе нет» (LegalBench-RAG, {datetime.date.today():%d.%m.%Y})", "",
         f"База: мини-набор, документов в ней {n_docs} (naive500_title, bge-m3). Балл вопроса это лучший косинус ссылки "
         "на договор (часть до первой «;») с представлением документа базы. Балл ниже порога даёт отказ "
         "«этого договора в базе нет», модель не вызывается. Замер без стенда (`eval/gate_eval.py`).", "",
         f"- В базе: вопросы мини, половина A {any_rep['inA']}, B {any_rep['inB']} (половины как у "
         "`eval/routing_eval.py`).",
         f"- Вне базы: вопросы полного набора, чьих документов в мини нет; половины по документам: A "
         f"{any_rep['outA']}, B {any_rep['outB']} вопросов ({any_rep['refsB']} разных ссылок в B). "
         "PrivacyQA вне базы нет: весь набор в мини.",
         "- Порог подбирается на A, проценты на B с 95 % интервалом (бутстреп по вопросам). AUC это "
         "вероятность, что вопрос из базы получит балл выше вопроса вне базы.", ""]
    L += ["| Представление документа | AUC на A | AUC на B |", "|---|---|---|"]
    for rep, r in res.items():
        L.append(f"| {RE.REP_RU[rep]} | {r['auc_A']:.3f} | {r['auc_B']:.3f} |")
    best = max(res, key=lambda r: res[r]["auc_B"])
    t1 = res[best]["thresholds"][0]
    L += ["", "## Вывод", "",
          f"- Лучше всего различает {RE.REP_RU[best]} (AUC на B {res[best]['auc_B']:.3f}). При пороге с A "
          f"({t1['rule']}, порог {t1['t']:.4f}) на B ложных отказов в базе {fmt(t1['false_B'])} %, верных отказов "
          f"вне базы {fmt(t1['true_B'])} %.",
          "- В сервисе (app/routing.py) порог с A округлён вниз до трёх знаков. Вверх нельзя: порог с A стоит "
          "ровно на балле одного договора базы, и округление вверх отсекало его вопросы (на стенде 20.11: 11 "
          "вопросов мини при 0,586 вместо 0,5858).",
          "- Для сравнения: без гейта модель на вопросах вне базы отказывала в 68,9 % случаев, а в 31,1 % "
          "отвечала по чужому договору (`answer.md`, 45 вопросов).",
          "- Оговорки. Вопросы вне базы из того же бенчмарка и того же шаблона «Consider …; …». База маленькая "
          "(мини); на большой базе порог надо проверить заново. Живые вопросы без названия договора гейт не "
          "отличит: им нужен обычный путь без маршрута."]
    for rep, r in res.items():
        L += ["", f"## {RE.REP_RU[rep]}", "",
              "| Правило порога | порог | ложные отказы в базе, B % | верные отказы вне базы, B % | "
              "по разным ссылкам, B % | CUAD / ContractNLI / MAUD, B % |", "|---|---|---|---|---|---|"]
        for t in r["thresholds"]:
            by = t["true_B_by_ds"]
            ds = " / ".join(f"{by[d]:.1f}" for d in ("cuad", "contractnli", "maud") if d in by)
            L.append(f"| {t['rule']} | {t['t']:.4f} | {fmt(t['false_B'])} | {fmt(t['true_B'])} | "
                     f"{t['true_refs_B']:.1f} | {ds} |")
    return L


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--cache", default=str(ROOT / "cache"), help="папка с stage1/ и full/ от eval/prepare.py")
    ap.add_argument("--embed-cache", default=str(ROOT / "cache/embeddings"))
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-pause", type=float, default=2.0)
    ap.add_argument("--md", help="записать отчёт в этот файл")
    ap.add_argument("--json", help="сырые итоги")
    args = ap.parse_args(argv)
    cache = Path(args.cache)
    meta = json.loads((cache / "stage1" / "meta.json").read_text())
    dim = meta["dim"]
    corpus = RE.Corpus(cache / "stage1" / "naive500_title", dim)
    inside = load(cache / "stage1", dim)
    files = set(corpus.files)
    outside = [q for q in load(cache / "full", dim) if not ({s[0] for s in q["gt"]} & files)]
    for q in outside:
        q["half"] = doc_half(q["gt"][0][0])
    titles = [RE.TITLE_PREFIX + t for t in corpus.title]
    vec, stats = RE.embed_texts(titles + sorted({q["ref"] for q in inside + outside}), args)
    print(f"документов в базе {len(corpus.files)}, вопросов в базе {len(inside)}, вне базы {len(outside)}; "
          f"векторы коротких текстов: {stats['texts']}, новых {stats['computed']}", flush=True)
    reps = {"title": [vec[t] for t in titles], "first": corpus.first_vectors(), "centroid": corpus.centroids()}
    router = RE.Router(reps, lambda q: vec[q["ref"]], lambda q: None)
    res = evaluate(router, inside, outside, RE.halves(inside))
    lines = report(res, len(corpus.files))
    print("\n".join(lines))
    if args.md:
        Path(args.md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
