"""Векторы через Ollama на Маке (GPU, Metal) с кэшем на диске.

Ключ кэша: sha256(модель + «\\0» + текст). Кэш дописывается: keys.txt (ключ на строку)
и vectors.f32 (float32 подряд, строка на ключ). Повторный запуск не пересчитывает готовое.
Векторы в памяти не держим: в памяти только номер строки по ключу, сам вектор читается
с диска. Полный корпус (около 159 тыс. кусков, 0,65 ГБ векторов) так не раздувает память Мака.
Если прошлая запись оборвалась на середине, при открытии кэш обрезается до последней целой строки.
Только стандартная библиотека.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from array import array


class OllamaEmbedder:
    def __init__(self, model: str = "bge-m3", url: str = "http://127.0.0.1:11434",
                 cache_dir: str = "cache/embeddings", batch: int = 32, timeout: int = 600):
        self.model, self.url, self.batch, self.timeout = model, url.rstrip("/"), batch, timeout
        self.dir = os.path.join(cache_dir, model.replace(":", "_").replace("/", "_"))
        os.makedirs(self.dir, exist_ok=True)
        self.keys_path = os.path.join(self.dir, "keys.txt")
        self.vec_path = os.path.join(self.dir, "vectors.f32")
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.dim: int | None = None
        self.index: dict[str, int] = {}
        self.computed = 0
        self.seconds = 0.0
        self._reader = None
        if os.path.exists(self.meta_path):
            with open(self.meta_path) as f:
                self.dim = json.load(f)["dim"]
            self._open_existing()

    def _open_existing(self) -> None:
        keys = []
        if os.path.exists(self.keys_path):
            with open(self.keys_path) as f:
                keys = [line.strip() for line in f if line.strip()]
        row_bytes = 4 * self.dim
        size = os.path.getsize(self.vec_path) if os.path.exists(self.vec_path) else 0
        n = min(len(keys), size // row_bytes)
        if n != len(keys) or size != n * row_bytes:  # обрыв прошлой записи: обрезаем до целой строки
            with open(self.keys_path, "w") as f:
                f.writelines(k + "\n" for k in keys[:n])
            if os.path.exists(self.vec_path):
                os.truncate(self.vec_path, n * row_bytes)
        self.index = {k: i for i, k in enumerate(keys[:n])}

    def key(self, text: str) -> str:
        return hashlib.sha256((self.model + "\0" + text).encode("utf-8")).hexdigest()

    def vector(self, text: str) -> array:
        row = self.index[self.key(text)]
        if self._reader is None:
            self._reader = open(self.vec_path, "rb")
        self._reader.seek(row * 4 * self.dim)
        v = array("f")
        v.fromfile(self._reader, self.dim)
        return v

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def ensure(self, texts: list[str], progress: bool = True) -> None:
        todo, seen = [], set()
        for t in texts:
            k = self.key(t)
            if k not in self.index and k not in seen:
                seen.add(k)
                todo.append(t)
        for i in range(0, len(todo), self.batch):
            part = todo[i:i + self.batch]
            t0 = time.perf_counter()
            embs = self._call(part)
            self.seconds += time.perf_counter() - t0
            self._append(part, embs)
            self.computed += len(part)
            if progress and (i // self.batch) % 20 == 0:
                rate = self.computed / self.seconds if self.seconds else 0.0
                print(f"  векторы: {self.computed}/{len(todo)}, {rate:.1f} кусков/с", flush=True)

    def _call(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts, "truncate": True}).encode()
        req = urllib.request.Request(self.url + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)["embeddings"]

    def _append(self, texts: list[str], embs: list[list[float]]) -> None:
        if self.dim is None:
            self.dim = len(embs[0])
            with open(self.meta_path, "w") as f:
                json.dump({"model": self.model, "dim": self.dim}, f)
        if any(len(e) != self.dim for e in embs):
            raise ValueError(f"размерность вектора не {self.dim}")
        # сначала векторы, потом ключи: при обрыве лишний вектор без ключа срежется при открытии
        with open(self.vec_path, "ab") as fv:
            for e in embs:
                array("f", e).tofile(fv)
        with open(self.keys_path, "a") as fk:
            for t in texts:
                k = self.key(t)
                self.index[k] = len(self.index)
                fk.write(k + "\n")
