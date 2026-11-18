"""Векторы через Ollama на Маке (GPU, Metal) с кэшем на диске.

Ключ кэша: sha256(модель + «\\0» + текст). Кэш дописывается: keys.txt (ключ на строку)
и vectors.f32 (float32 подряд, строка на ключ). Повторный запуск не пересчитывает готовое.
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
        self.vectors = array("f")
        self.computed = 0
        self.seconds = 0.0
        if os.path.exists(self.meta_path):
            self.dim = json.load(open(self.meta_path))["dim"]
            with open(self.keys_path) as f:
                for i, line in enumerate(f):
                    self.index[line.strip()] = i
            with open(self.vec_path, "rb") as f:
                self.vectors.fromfile(f, len(self.index) * self.dim)

    def key(self, text: str) -> str:
        return hashlib.sha256((self.model + "\0" + text).encode("utf-8")).hexdigest()

    def vector(self, text: str) -> array:
        row = self.index[self.key(text)]
        return self.vectors[row * self.dim:(row + 1) * self.dim]

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
        with open(self.keys_path, "a") as fk, open(self.vec_path, "ab") as fv:
            for t, e in zip(texts, embs):
                k = self.key(t)
                self.index[k] = len(self.index)
                fk.write(k + "\n")
                a = array("f", e)
                a.tofile(fv)
                self.vectors.extend(a)
