"""Модели векторов: какой текст вопроса видит модель.

Одно место для сервиса поиска (app/api.py) и подготовки данных (eval/prepare.py). Вопрос должен
прийти к модели в том виде, на котором она училась, иначе его вектор ляжет не туда, где лежат
куски, и поиск молча станет хуже, без единой ошибки.
- bge-m3: без префикса;
- snowflake-arctic-embed2: у вопроса префикс «query: » (карточка модели).
Куски документов у обеих моделей идут как есть, без префикса. Модели без записи здесь получают
вопрос как есть. Имя сравнивается без тега Ollama («snowflake-arctic-embed2:latest» и «:568m»
это та же модель, префикс у неё тот же). Только стандартная библиотека.
"""
from __future__ import annotations

QUERY_PREFIX = {
    "bge-m3": "",
    "snowflake-arctic-embed2": "query: ",
}


def base_name(model: str) -> str:
    return model.split(":", 1)[0]


def query_prefix(model: str) -> str:
    return QUERY_PREFIX.get(base_name(model), "")


def query_text(model: str, question: str) -> str:
    """Текст вопроса, который уходит в модель; по нему же считается ключ кэша векторов."""
    return query_prefix(model) + question
