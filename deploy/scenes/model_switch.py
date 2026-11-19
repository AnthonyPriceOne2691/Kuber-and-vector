"""Проверочная часть сцены «смена модели векторов на ходу» (deploy/scenes/model-switch.sh).

На Маке, только стандартная библиотека. Все запросы идут через вход стенда (HAProxy на machine-4,
https с проверкой по УЦ стенда, ключ API), как у пользователя, не чаще --rate в секунду: вход
пускает с одного адреса не больше 60 запросов в секунду.
    pair     кто сейчас отвечает: коллекция, модель, проверка отпечатка, поды
    wait     ждать, пока каждый под поиска ответит заданной парой
    hold     N секунд следить: все ответы идут прежней парой, цель алиаса каждый под отверг
    quality  recall@k на фиксированной выборке вопросов LegalBench-RAG-mini: первые --per-dataset
             вопросов каждого набора мини-выборки (eval/legalbench.py), эталон из data/legalbenchrag,
             метрика посимвольная, как у авторов; живьём через вход, тем же путём, что у пользователя
    compare  разница двух замеров качества на тех же вопросах с 95 % бутстреп-интервалом
    summary  итог сцены по файлам папки прогона; код выхода 1, если сцена не прошла
Каждая команда печатает одну-две строки по-английски (они идут в кадр) и пишет JSON в --out.

    python3 -I -B model_switch.py --url https://ENTRY --cafile ca.pem --api-key-file key wait \\
        --collection contracts_rf2_arctic --pods 3 --out wait.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)  # eval/legalbench.py; с -I папка скрипта сама в путь не попадает

from eval import legalbench as lb  # noqa: E402

PROBE_QUESTION = "Which law governs the agreement?"


class Entry:
    """Вход стенда: GET /search с ключом API, новое соединение на каждый запрос (балансировка по подам)."""

    def __init__(self, url: str, cafile: str | None = None, key_file: str | None = None,
                 timeout: float = 5.0, rate: float = 10.0) -> None:
        self.base = url.rstrip("/")
        self.key = None
        if key_file:
            with open(key_file, encoding="utf-8") as f:
                self.key = f.read().strip()
        handlers: list = [urllib.request.ProxyHandler({})]
        if self.base.startswith("https:"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = timeout
        self.gap = 1.0 / rate if rate > 0 else 0.0
        self._next = 0.0

    def search(self, q: str, k: int = 1) -> tuple[int, dict | None]:
        """(код ответа, тело) — код 0, если соединения не было или ответ не JSON."""
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(now, self._next) + self.gap
        headers = {"Accept": "application/json"}
        if self.key:
            headers["X-API-Key"] = self.key
        req = urllib.request.Request(f"{self.base}/search?" + urllib.parse.urlencode({"q": q, "k": k}), headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, None
        except (OSError, ValueError):
            return 0, None


def pair_of(doc: dict) -> str:
    return f"{doc.get('collection')} ({doc.get('model')})"


def check_of(doc: dict) -> dict:
    return doc.get("model_check") or {}


def pod_of(doc: dict) -> str:
    return (doc.get("served_by") or {}).get("pod") or "?"


def describe_check(c: dict) -> str:
    if not c:
        return "alias mode off"
    status = c.get("status")
    if status == "verified":
        cos = c.get("cosine")
        return "fingerprint verified" + (f", cosine {cos:.3f}" if isinstance(cos, (int, float)) else "")
    if status == "refused":
        return f"refused {c.get('target')} (registered as {c.get('target_model')}): {c.get('reason')}"
    return f"{status}" + (f": {c['reason']}" if c.get("reason") else "")


def write(path: str | None, doc: dict) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)


# --- pair, wait, hold ----------------------------------------------------------------------------
def cmd_pair(e: Entry, args) -> int:
    seen: dict[str, dict] = {}
    errors = 0
    for _ in range(args.requests):
        code, doc = e.search(PROBE_QUESTION)
        if code == 200 and doc:
            seen[pod_of(doc)] = doc
        else:
            errors += 1
    if not seen:
        print(f"no answer through the entry ({errors} errors)")
        write(args.out, {"ok": False, "errors": errors})
        return 1
    pairs = Counter(pair_of(d) for d in seen.values())
    one = next(iter(seen.values()))
    c = check_of(one)
    alias = f"alias {c.get('alias')} -> {c.get('target')} · " if c.get("alias") else ""
    print(f"users are served by {' + '.join(pairs)} · {alias}{describe_check(c)} · pods {len(seen)}"
          + (f" · errors {errors}" if errors else ""))
    write(args.out, {"ok": True, "pairs": dict(pairs), "pods": {p: {"collection": d.get("collection"),
                     "model": d.get("model"), "model_check": check_of(d)} for p, d in seen.items()},
                     "errors": errors})
    return 0


def matches(doc: dict, collection: str, model: str | None, status: str | None) -> bool:
    return (doc.get("collection") == collection and (model is None or doc.get("model") == model)
            and (status is None or check_of(doc).get("status") == status))


def cmd_wait(e: Entry, args) -> int:
    t0 = time.monotonic()
    last: dict[str, dict] = {}
    first_ok: dict[str, float] = {}
    sent = errors = 0
    done_at = None
    while time.monotonic() - t0 < args.timeout:
        code, doc = e.search(PROBE_QUESTION)
        sent += 1
        if code != 200 or not doc:
            errors += 1
            continue
        pod = pod_of(doc)
        last[pod] = doc
        ok = matches(doc, args.collection, args.model, args.status)
        if ok and pod not in first_ok:
            first_ok[pod] = time.monotonic() - t0
        if not ok:
            first_ok.pop(pod, None)  # под ещё на старой паре: ждём заново
        good = [p for p, d in last.items() if matches(d, args.collection, args.model, args.status)]
        if len(good) >= args.pods and len(good) == len(last):
            done_at = time.monotonic() - t0
            break
    target = f"{args.collection}" + (f" ({args.model})" if args.model else "")
    some = next((d for d in last.values() if matches(d, args.collection, args.model, args.status)), None)
    if some:
        target = pair_of(some)
    result = {"ok": done_at is not None, "collection": args.collection,
              "seconds": round(done_at, 1) if done_at is not None else None,
              "first_pod_seconds": round(min(first_ok.values()), 1) if first_ok else None,
              "pods": {p: {"collection": d.get("collection"), "model": d.get("model"),
                           "status": check_of(d).get("status")} for p, d in last.items()},
              "requests": sent, "errors": errors}
    write(args.out, result)
    if done_at is None:
        reasons = sorted({check_of(d)["reason"] for d in last.values() if check_of(d).get("reason")})
        print(f"timeout after {args.timeout:.0f} s: pods answer "
              + ", ".join(f"{p} {pair_of(d)} {check_of(d).get('status', '')}".rstrip() for p, d in sorted(last.items()))
              + f"; errors {errors}" + (f"; reason: {'; '.join(reasons)}" if reasons else ""))
        return 1
    print(f"all {len(last)} pods answer {target} {done_at:.1f} s after the switch command returned"
          + f" · {sent} probe requests, errors {errors}")
    return 0


def cmd_hold(e: Entry, args) -> int:
    t0 = time.monotonic()
    pairs: Counter = Counter()
    collections: Counter = Counter()
    refused: dict[str, dict] = {}
    first_refusal = None
    sent = errors = 0
    while time.monotonic() - t0 < args.seconds:
        code, doc = e.search(PROBE_QUESTION)
        sent += 1
        if code != 200 or not doc:
            errors += 1
            continue
        pairs[pair_of(doc)] += 1
        collections[doc.get("collection")] += 1
        c = check_of(doc)
        if c.get("status") == "refused" and c.get("target") == args.refused:
            refused[pod_of(doc)] = c
            if first_refusal is None:
                first_refusal = time.monotonic() - t0
    answers = sum(pairs.values())
    from_old, from_target = collections[args.collection], collections[args.refused]
    some = next(iter(refused.values()), {})
    reason = some.get("reason")
    ok = answers > 0 and from_old == answers and errors == 0 and len(refused) >= args.pods
    write(args.out, {"ok": ok, "seconds": args.seconds, "requests": sent, "errors": errors, "answers": answers,
                     "pairs": dict(pairs), "from_target": from_target, "pods_refused": sorted(refused),
                     "reason": reason, "target_model": some.get("target_model"),
                     "first_refusal_seconds": round(first_refusal, 1) if first_refusal is not None else None})
    print(f"{args.seconds:.0f} s, {answers} answers: {from_old} from {' + '.join(pairs) or '-'}, "
          f"{from_target} from {args.refused}, errors {errors}")
    if refused:
        print(f"{len(refused)} pods refused {args.refused}: {reason}")
    else:
        print(f"no pod reported a refusal of {args.refused}")
    return 0 if ok else 1


# --- качество ----------------------------------------------------------------------------------
def sample(data: str, per_dataset: int) -> list[dict]:
    """Фиксированная выборка: первые per_dataset вопросов каждого набора мини-выборки авторов."""
    out = []
    for name in lb.DATASETS:
        for i, t in enumerate(lb.select_mini(lb.load_tests(data, name))[:per_dataset]):
            out.append({"id": f"{name}/{i}", "dataset": name, "query": t["query"],
                        "gt": [(s["file_path"], s["span"][0], s["span"][1]) for s in t["snippets"]]})
    return out


def cmd_quality(e: Entry, args) -> int:
    questions = sample(args.data, args.per_dataset)
    per_q, pairs = [], Counter()
    errors = 0
    for q in questions:
        code, doc = e.search(q["query"][:500], args.k)
        if code != 200 or not doc:
            errors += 1
            per_q.append({"id": q["id"], "dataset": q["dataset"], "recall": None, "precision": None, "pair": None})
            continue
        found = [(r["file"], r["span"][0], r["span"][1]) for r in doc.get("results") or []]
        precision, recall = lb.precision_recall(found[:args.k], q["gt"])
        pairs[pair_of(doc)] += 1
        per_q.append({"id": q["id"], "dataset": q["dataset"], "recall": recall, "precision": precision,
                      "pair": pair_of(doc), "collection": doc.get("collection")})
    by_ds = {}
    for name in lb.DATASETS:
        rs = [x["recall"] for x in per_q if x["dataset"] == name and x["recall"] is not None]
        by_ds[name] = 100 * sum(rs) / len(rs) if rs else None
    vals = [v for v in by_ds.values() if v is not None]
    overall = sum(vals) / len(vals) if vals else None  # как у авторов: среднее по наборам с равными весами
    result = {"k": args.k, "questions": len(questions), "per_dataset": args.per_dataset, "errors": errors,
              "recall": overall, "recall_by_dataset": by_ds, "pairs": dict(pairs), "per_question": per_q,
              "method": "live through the entry; first questions of each LegalBench-RAG-mini dataset; "
                        "character-level recall as in the paper"}
    write(args.out, result)
    if overall is None:
        print(f"quality: no answers ({errors} errors)")
        return 1
    print(f"{args.label}recall@{args.k} {overall:.1f} % on {len(questions)} LegalBench-RAG-mini questions "
          f"({args.per_dataset} per dataset), live through the entry · answered by {' + '.join(pairs)}"
          + (f" · errors {errors}" if errors else ""))
    stray = sum(1 for x in per_q if x["recall"] is not None and x["collection"] != args.expect) if args.expect else 0
    if stray:
        print(f"warning: {stray} answers did not come from {args.expect}")
        return 1
    return 0


def paired_ci(a: dict, b: dict, rounds: int = 2000, seed: int = 7) -> tuple[float, float, float, int]:
    """Разница b − a в пунктах recall и 95 % интервал: бутстреп по вопросам внутри каждого набора,
    итог как у авторов, среднее по наборам."""
    ra = {x["id"]: x for x in a["per_question"] if x["recall"] is not None}
    rb = {x["id"]: x for x in b["per_question"] if x["recall"] is not None}
    by_ds: dict[str, list[float]] = {}
    for qid in sorted(set(ra) & set(rb)):
        by_ds.setdefault(ra[qid]["dataset"], []).append(100 * (rb[qid]["recall"] - ra[qid]["recall"]))
    if not by_ds:
        raise ValueError("no common questions")
    point = sum(sum(v) / len(v) for v in by_ds.values()) / len(by_ds)
    rng = random.Random(seed)
    stats = []
    for _ in range(rounds):
        means = [sum(rng.choice(v) for _ in v) / len(v) for v in by_ds.values()]
        stats.append(sum(means) / len(means))
    stats.sort()
    return point, stats[int(0.025 * rounds)], stats[int(0.975 * rounds) - 1], sum(len(v) for v in by_ds.values())


def cmd_compare(args) -> int:
    with open(args.before, encoding="utf-8") as f:
        a = json.load(f)
    with open(args.after, encoding="utf-8") as f:
        b = json.load(f)
    point, lo, hi, n = paired_ci(a, b)
    write(args.out, {"before": a["recall"], "after": b["recall"], "delta": point, "ci95": [lo, hi], "questions": n,
                     "k": a["k"]})
    print(f"recall@{a['k']}: {a['recall']:.1f} % -> {b['recall']:.1f} % ({point:+.1f} points, 95 % CI {lo:+.1f} … "
          f"{hi:+.1f}, paired bootstrap over the same {n} questions)")
    return 0


# --- итог ----------------------------------------------------------------------------------------
def load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cmd_summary(args) -> int:
    d = args.dir
    snap = load_json(os.path.join(d, "load.json"))
    switch, rollback = load_json(os.path.join(d, "wait-switch.json")), load_json(os.path.join(d, "wait-rollback.json"))
    trap, back = load_json(os.path.join(d, "hold-trap.json")), load_json(os.path.join(d, "wait-cleanup.json"))
    cmp_ = load_json(os.path.join(d, "quality-compare.json"))
    lines, ok = [], True
    if snap:
        t = snap["totals"]
        wr = t["with_retry"]["errors"]
        ok &= wr == 0
        lines.append(f"users: {t['count']} requests under load, errors seen {wr}, retries {t['retries']}")
    else:
        ok = False
        lines.append("users: no load snapshot")
    for name, w in (("switch", switch), ("rollback", rollback), ("trap cleanup", back)):
        if w and w.get("ok"):
            lines.append(f"{name}: all pods on {w['collection']} in {w['seconds']} s, no restart")
        else:
            ok = False
            lines.append(f"{name}: did not complete")
    if cmp_:
        lines.append(f"quality recall@{cmp_['k']}: {cmp_['before']:.1f} % -> {cmp_['after']:.1f} % "
                     f"({cmp_['delta']:+.1f} points, 95 % CI {cmp_['ci95'][0]:+.1f} … {cmp_['ci95'][1]:+.1f}, "
                     f"{cmp_['questions']} questions, live)")
    if trap:
        ok &= bool(trap.get("ok"))
        lines.append(f"trap: {len(trap.get('pods_refused') or [])} pods refused the mislabeled collection "
                     f"({trap.get('reason')}); {trap.get('from_target', 0)} answers from it, errors {trap.get('errors')}")
    else:
        ok = False
        lines.append("trap: not run")
    lines.append("result: " + ("passed" if ok else "FAILED"))
    print("\n".join(lines))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="вход стенда, https://ENTRY")
    ap.add_argument("--cafile")
    ap.add_argument("--api-key-file")
    ap.add_argument("--rate", type=float, default=10.0, help="запросов в секунду, не больше")
    ap.add_argument("--request-timeout", type=float, default=5.0, help="таймаут запроса, с")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair")
    p.add_argument("--requests", type=int, default=9)
    p.add_argument("--out")
    w = sub.add_parser("wait")
    w.add_argument("--collection", required=True)
    w.add_argument("--model")
    w.add_argument("--status", help="ещё и состояние проверки, например verified")
    w.add_argument("--pods", type=int, default=3)
    w.add_argument("--timeout", type=float, default=90.0, help="сколько ждать, с")
    w.add_argument("--out")
    h = sub.add_parser("hold")
    h.add_argument("--seconds", type=float, default=20.0)
    h.add_argument("--collection", required=True, help="прежняя пара: все ответы должны идти из неё")
    h.add_argument("--refused", required=True, help="цель алиаса, которую поды должны отвергнуть")
    h.add_argument("--pods", type=int, default=3)
    h.add_argument("--out")
    q = sub.add_parser("quality")
    q.add_argument("--data", default=os.path.join(ROOT, "data", "legalbenchrag"))
    q.add_argument("--k", type=int, default=8)
    q.add_argument("--per-dataset", type=int, default=25)
    q.add_argument("--expect", help="коллекция, из которой должны прийти все ответы")
    q.add_argument("--label", default="")
    q.add_argument("--out")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--out")
    s = sub.add_parser("summary")
    s.add_argument("dir")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        return cmd_compare(args)
    if args.cmd == "summary":
        return cmd_summary(args)
    if not args.url:
        ap.error("--url is required")
    e = Entry(args.url, args.cafile, args.api_key_file, timeout=args.request_timeout, rate=args.rate)
    return {"pair": cmd_pair, "wait": cmd_wait, "hold": cmd_hold, "quality": cmd_quality}[args.cmd](e, args)


if __name__ == "__main__":
    sys.exit(main())
