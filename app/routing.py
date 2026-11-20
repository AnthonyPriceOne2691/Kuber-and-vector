"""Выбор договора по ссылке в вопросе, деление вопроса и гейт «этого договора в базе нет».

Вопрос LegalBench-RAG называет договор до первой «;» («Consider the Supply Agreement between A and
B; What is the expiration date?»). Сервис делит вопрос на ссылку и сам вопрос. Ссылка выбирает
договор: косинус ссылки со средним вектором кусков документа (вектор «route» коллекции документов
<коллекция>_docs). Куски внутри договора ранжирует смесь a·весь вопрос + (1 − a)·сам вопрос, к баллу
куска прибавляется μ·лучший балл соседнего куска того же документа. Договоры идут по порядку
маршрута: сначала куски первого, потом следующего, если кусков не хватило. Замер без стенда в
eval/results/routing.md: на мини средний recall по k 45,7 → 61,6, recall@64 81,8 → 90,7.

Гейт: лучший балл ссылки по вектору «gate» документа, это (вектор «Document: <название>» +
вектор первого куска) / 2, расстояние Dot, то есть среднее двух косинусов. Ниже порога ответ
«этого договора в базе нет», куски не ищутся, модель не зовётся. Порог для bge-m3 подобран на
половине A мини (0,58581, не больше 1 % ложных отказов на A) и округлён вниз до 0,585: округление
вверх до 0,586 отсекало договор с баллом ровно 0,5858 (11 вопросов мини, замер на стенде 20.11).
На половине B верных отказов вне базы 97,2 %, ложных в базе 0,3 % (eval/results/gate.md). Для других
моделей порог не мерили, гейт у них выключен.

Только стандартная библиотека.
"""
from __future__ import annotations

import math
import os

from app import embed_models

DOCS_SUFFIX = "_docs"
# порог гейта по модели: только замеренные (eval/results/gate.md); GATE_MIN в окружении перекрывает
GATE_MIN = {"bge-m3": 0.585}


def split_question(q: str) -> tuple[str, str] | None:
    """Ссылка на договор и сам вопрос по первой «;»; None, если делить нечего."""
    ref, sep, ask = q.partition(";")
    ref, ask = ref.strip(), ask.strip()
    if not sep or not ref or not ask:
        return None
    return ref, ask


def docs_collection(collection: str) -> str:
    return collection + DOCS_SUFFIX


def gate_min(model: str) -> float | None:
    raw = os.environ.get("GATE_MIN", "")
    if raw:
        return None if raw.lower() == "off" else float(raw)
    return GATE_MIN.get(embed_models.base_name(model))


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def mix(full: list[float], ask: list[float], a: float) -> list[float]:
    """Вектор запроса кусков: a·весь вопрос + (1 − a)·сам вопрос. Qdrant с косинусом нормирует его сам,
    балл куска выходит смесью двух косинусов, умноженной на одно для всех кусков число: порядок тот же."""
    return [a * x + (1 - a) * y for x, y in zip(full, ask)]


def smooth(scores: list[float], mu: float) -> list[float]:
    """Балл куска + μ·лучший балл соседа (предыдущий или следующий кусок того же документа)."""
    out = []
    for i, x in enumerate(scores):
        nb = [scores[j] for j in (i - 1, i + 1) if 0 <= j < len(scores)]
        out.append(x + mu * (max(nb) if nb else x))
    return out


def rank_in_doc(points: list[dict], mu: float) -> list[tuple[float, object]]:
    """Куски одного документа (id, score, payload.start) → [(балл со соседями, id)] по убыванию балла."""
    ordered = sorted(points, key=lambda p: p["payload"]["start"])
    scores = smooth([p["score"] for p in ordered], mu)
    return sorted(zip(scores, (p["id"] for p in ordered)), key=lambda t: -t[0])
