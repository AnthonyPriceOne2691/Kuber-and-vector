"""Ответ модели по найденным кускам: промпт, разбор ответа, проверка ссылок и цитат.

Без FastAPI и без сети: этот же модуль использует оценка ответа (eval/answer_eval.py),
поэтому в ролике и в замере работает один и тот же код.

Модель отвечает JSON по схеме (structured outputs Ollama):
    {"found": bool, "answer": str, "citations": [{"n": int, "quote": str}]}
n это номер куска в промпте, quote дословная цитата из него. Цитату проверяем сами: она должна
найтись в тексте куска без учёта регистра, пробелов и типографских кавычек. Ответ показываем,
только если подтвердилась хотя бы одна цитата. Иначе честное «в документах этого нет».

Статусы:
    answered     ответ есть, хотя бы одна цитата найдена в своём куске
    refused      модель сама сказала, что в кусках ответа нет
    unsupported  модель ответила, но ни одна цитата не подтвердилась: ответ не показываем
    invalid      ответ модели не разобрать (не JSON или не та схема)
"""
from __future__ import annotations

import json
import re

NUM_CTX = 4096      # контекст модели; фильтр на Маке выставляет то же самое и больше не даст
MAX_TOKENS = 400    # предел длины ответа модели в токенах
MIN_QUOTE = 15      # цитата короче этого числа знаков ничего не доказывает
MIN_PART = 8        # часть цитаты между многоточиями не короче этого

SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"n": {"type": "integer"}, "quote": {"type": "string"}},
                      "required": ["n", "quote"]},
        },
    },
    "required": ["found", "answer", "citations"],
}

SYSTEM = (
    "You answer questions about legal documents using only the numbered fragments the user provides.\n"
    "Rules:\n"
    "1. Use only the fragments. Do not use outside knowledge.\n"
    "2. If the question names a specific document, company or party and no fragment comes from that "
    "document, the fragments do not contain the answer.\n"
    "3. If the fragments do not contain the answer, return found=false, an empty answer and no citations.\n"
    "4. Otherwise return found=true and a short answer of at most three sentences in the language of the "
    "question. Put the numbers of the fragments you rely on in square brackets, for example [2].\n"
    "5. For every fragment you rely on, add a citation with its number n and a quote of 5 to 30 words "
    "copied exactly from that fragment.\n"
)


def prompt(question: str, hits: list[dict]) -> str:
    """Текст для модели: вопрос и пронумерованные куски с названием документа и раздела."""
    parts = [f"Question: {question}", "", "Fragments:"]
    for n, h in enumerate(hits, 1):
        head = f"[{n}] Document: {h.get('document') or h['file']}"
        if h.get("section"):
            head += f" | Section: {h['section']}"
        parts += [head, h["text"].strip(), ""]
    return "\n".join(parts).rstrip()


def chat_body(model: str, question: str, hits: list[dict]) -> dict:
    """Тело запроса к Ollama /api/chat. Детерминированно: температура 0 и фиксированное зерно."""
    return {"model": model, "stream": False, "think": False, "format": SCHEMA,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt(question, hits)}],
            "options": {"temperature": 0, "seed": 7, "num_ctx": NUM_CTX, "num_predict": MAX_TOKENS}}


def parse(content: str) -> dict | None:
    """Ответ модели в виде {"found", "answer", "citations"}; None, если разобрать нельзя."""
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("found"), bool):
        return None
    answer = data.get("answer") if isinstance(data.get("answer"), str) else ""
    citations = []
    for c in data.get("citations") if isinstance(data.get("citations"), list) else []:
        n = c.get("n") if isinstance(c, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and isinstance(c.get("quote"), str):
            citations.append({"n": n, "quote": c["quote"].strip()})
    return {"found": data["found"], "answer": answer.strip(), "citations": citations}


_TYPO = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.translate(_TYPO).lower()).strip()


def quote_in(quote: str, text: str) -> bool:
    """Цитата дословно есть в тексте. Многоточие в цитате означает пропуск: части ищем по порядку."""
    parts = [p.strip(" \"'.,;:") for p in re.split(r"\.\.\.|…", norm(quote))]
    parts = [p for p in parts if p]
    if not parts or sum(map(len, parts)) < MIN_QUOTE or min(map(len, parts)) < MIN_PART:
        return False
    hay, pos = norm(text), 0
    for p in parts:
        i = hay.find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return True


def _russian(question: str) -> bool:
    low = question.lower()
    return len(re.findall(r"[а-яё]", low)) > len(re.findall(r"[a-z]", low))


def refusal(question: str) -> str:
    """«В документах этого нет» на языке вопроса."""
    return "В документах этого нет." if _russian(question) else "The documents do not contain this."


def not_in_base(question: str) -> str:
    """Отказ гейта (app/routing.py): договора из вопроса в базе нет, модель не вызывалась."""
    return "Этого договора нет в базе." if _russian(question) else "This contract is not in the database."


def check(reply: dict | None, hits: list[dict], question: str) -> dict:
    """Сверяет разобранный ответ модели с найденными кусками и выносит статус.

    hits в том виде, как их отдаёт /search: document, section, file, span, text.
    """
    if reply is None:
        return {"status": "invalid", "answer": refusal(question), "citations": []}
    if not reply["found"]:
        return {"status": "refused", "answer": refusal(question), "citations": []}
    citations = []
    for c in reply["citations"]:
        n = c["n"]
        hit = hits[n - 1] if 1 <= n <= len(hits) else None
        item = {"n": n, "quote": c["quote"], "valid": hit is not None,
                "verified": hit is not None and quote_in(c["quote"], hit["text"])}
        if hit is not None:
            item.update(document=hit.get("document"), section=hit.get("section"),
                        file=hit["file"], span=hit["span"])
        citations.append(item)
    if not any(c["verified"] for c in citations):
        return {"status": "unsupported", "answer": refusal(question), "citations": citations,
                "model_answer": reply["answer"]}
    return {"status": "answered", "answer": reply["answer"], "citations": citations}
