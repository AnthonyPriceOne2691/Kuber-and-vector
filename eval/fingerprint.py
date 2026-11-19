"""Отпечаток набора для реестра «коллекция → модель» (deploy/qdrant/registry.py).

На Маке, без сети. Модель и размерность берутся из meta.json набора (папка над SRC). Проба это
первый кусок набора: его id, текст, по которому prepare.py считал вектор (prepare.index_text),
и сам вектор из vectors.f32. Сервис поиска потом читает вектор пробы из самой коллекции и сравнивает
его с вектором того же текста, посчитанным моделью из реестра (app/api.py, _verify).

    python3 -I -B eval/fingerprint.py cache/stage1_arctic/naive500_title > entry.json
    python3 -I -B eval/fingerprint.py cache/stage1_arctic/naive500_title --model bge-m3 > entry.json

--model подписывает набор не той моделью, что в meta.json: только для подложенной ошибки в сцене
deploy/scenes/model-switch.sh. Такая запись помечена planted и хранит модель из meta.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from eval.prepare import index_text  # noqa: E402


def entry(src: str, model: str | None = None) -> dict:
    """Запись реестра для набора SRC (папка ветки с points.jsonl и vectors.f32)."""
    path = Path(src)
    meta_path = path.parent / "meta.json"
    if not meta_path.exists():
        raise ValueError(f"нет {meta_path}: модель набора неизвестна")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    strategy = path.name
    if strategy not in (meta.get("strategies") or {}):
        raise ValueError(f"в {meta_path} нет ветки {strategy}: meta.json от другого прогона")
    with open(path / "points.jsonl", encoding="utf-8") as f:
        first = json.loads(f.readline())
        rows = 1 + sum(1 for line in f if line.strip())
    size = os.path.getsize(path / "vectors.f32")
    dim = int(meta.get("dim") or size // 4 // rows)
    if size != rows * dim * 4:
        raise ValueError(f"vectors.f32 не сходится с points.jsonl: {size} байт на {rows} кусков по {dim}")
    vec = array("f")
    with open(path / "vectors.f32", "rb") as f:
        vec.fromfile(f, dim)
    out = {"model": model or meta["model"], "dim": dim, "probe_id": first["id"],
           "probe_text": index_text(first, strategy.partition("_")[2]),
           "vector": vec.tolist(), "source": f"{path.parent.name}/{strategy}"}
    if model and model != meta["model"]:
        out.update(planted=True, meta_model=meta["model"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="папка ветки набора, например cache/stage1/naive500_title")
    ap.add_argument("--model", default=None, help="подпись модели вместо meta.json (подложенная ошибка)")
    args = ap.parse_args()
    try:
        doc = entry(args.src, args.model or None)
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"fingerprint: {e}")
    json.dump(doc, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
