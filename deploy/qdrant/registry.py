"""Реестр «коллекция → модель» и алиас поиска в Qdrant. Запускается внутри кластера (на машине стенда)
админским ключом из файла, только стандартная библиотека. Обёртка: deploy/qdrant/registry.sh.

Реестр это маленькая коллекция embedding_registry: 1 шард, 3 копии, у каждого пира своя, поэтому
сервис поиска читает её со своего пира. Точка на коллекцию, id из имени коллекции (uuid5, то же
считает app/binding.py). Payload: collection, model, dim, probe_id, probe_text, source, registered_at;
вектор fingerprint это вектор пробы на момент записи. Проба это настоящий кусок коллекции: сервис
сверяет его вектор из самой коллекции с вектором того же текста, посчитанным моделью из реестра,
и только тогда берёт коллекцию (app/api.py, _verify).

    registry.py --url URL --key-file KEY show [--json]
    registry.py --url URL --key-file KEY register --entry entry.json --collection C
    registry.py --url URL --key-file KEY alias --name contracts --to C
    registry.py --url URL --key-file KEY drop --collection C

register сначала сверяет пробу набора (eval/fingerprint.py) с той же точкой в коллекции: коллекцию,
загруженную не из этого набора, записать нельзя. alias переключает одним запросом
POST /collections/aliases: delete и create в одном теле Qdrant применяет вместе. drop удаляет только
подложенную коллекцию (запись с пометкой planted) и только если на неё не указывает алиас.
Вывод по-английски: он идёт в кадр.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
import uuid

REGISTRY = "embedding_registry"
SAME_VECTOR = 0.999  # вектор пробы в коллекции и в наборе один и тот же, расхождение только от округления


def registry_id(collection: str) -> str:
    """Как app/binding.registry_id: совпадение сверяет tests/test_registry.py."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "embedding-registry/" + collection))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Failed(Exception):
    pass


class Qdrant:
    def __init__(self, url: str, key: str, timeout: float = 60) -> None:
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"api-key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                text = e.read(500).decode("utf-8", "replace")
            raise Failed(f"Qdrant {method} {path.split('?')[0]}: HTTP {e.code} {text}") from None

    def collections(self) -> set[str]:
        return {c["name"] for c in self.call("GET", "/collections")["result"]["collections"]}

    def aliases(self) -> dict[str, str]:
        return {a["alias_name"]: a["collection_name"] for a in self.call("GET", "/aliases")["result"]["aliases"]}

    def entries(self) -> dict[str, dict]:
        if REGISTRY not in self.collections():
            return {}
        res = self.call("POST", f"/collections/{REGISTRY}/points/scroll",
                        {"limit": 1000, "with_payload": True, "with_vector": False})["result"]
        return {p["payload"]["collection"]: p["payload"] for p in res["points"] if (p.get("payload") or {}).get("collection")}

    def entry(self, collection: str) -> dict | None:
        if REGISTRY not in self.collections():
            return None
        found = self.call("POST", f"/collections/{REGISTRY}/points",
                          {"ids": [registry_id(collection)], "with_payload": True, "with_vector": False})["result"]
        return found[0]["payload"] if found else None


def label(collection: str | None, entries: dict[str, dict]) -> str:
    """«contracts_rf2 (bge-m3)» или «c (not registered)»."""
    if collection is None:
        return "(none)"
    e = entries.get(collection)
    return f"{collection} ({e['model']})" if e else f"{collection} (not registered)"


def show(q: Qdrant, as_json: bool) -> None:
    cols, aliases, entries = q.collections(), q.aliases(), q.entries()
    if as_json:
        print(json.dumps({"aliases": aliases, "registry": entries, "collections": sorted(cols)}, ensure_ascii=False))
        return
    for name, target in sorted(aliases.items()):
        print(f"alias {name} -> {label(target, entries)}")
    if not aliases:
        print("no aliases")
    print(f"registry {REGISTRY}:" if REGISTRY in cols else f"registry {REGISTRY}: not created yet")
    for c in sorted(cols - {REGISTRY}):
        e = entries.get(c)
        if not e:
            print(f"  {c:24s} not registered")
            continue
        note = f"  planted: vectors made by {e.get('meta_model')}" if e.get("planted") else ""
        print(f"  {c:24s} {e['model']:26s} from {e.get('source', '?')}, registered {e.get('registered_at', '?')}{note}")
    for c in sorted(set(entries) - cols):
        print(f"  {c:24s} {entries[c]['model']:26s} collection is gone")


def register(q: Qdrant, entry: dict, collection: str) -> None:
    for field in ("model", "dim", "probe_id", "probe_text", "vector", "source"):
        if field not in entry:
            raise Failed(f"entry has no field {field}")
    cols = q.collections()
    if collection not in cols:
        raise Failed(f"collection {collection} does not exist")
    if collection == REGISTRY:
        raise Failed("the registry does not register itself")
    found = q.call("POST", f"/collections/{collection}/points",
                   {"ids": [entry["probe_id"]], "with_payload": False, "with_vector": ["dense"]})["result"]
    stored = ((found[0].get("vector") or {}).get("dense")) if found else None
    if not stored:
        raise Failed(f"{collection} has no point {entry['probe_id']}: it was not loaded from {entry['source']}")
    same = cosine(stored, entry["vector"])
    if same < SAME_VECTOR:
        raise Failed(f"{collection} holds other vectors than {entry['source']} (cosine {same:.4f})")
    dim = int(entry["dim"])
    if REGISTRY not in cols:
        q.call("PUT", f"/collections/{REGISTRY}", {"vectors": {"fingerprint": {"size": dim, "distance": "Cosine"}},
                                                  "shard_number": 1, "replication_factor": 3})
    else:
        size = q.call("GET", f"/collections/{REGISTRY}")["result"]["config"]["params"]["vectors"]["fingerprint"]["size"]
        if size != dim:
            raise Failed(f"registry stores {size}-dimensional fingerprints, this set has {dim}")
    payload = {"collection": collection, "model": entry["model"], "dim": dim, "probe_id": entry["probe_id"],
               "probe_text": entry["probe_text"], "source": entry["source"],
               "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if entry.get("planted"):
        payload.update(planted=True, meta_model=entry.get("meta_model"))
    q.call("PUT", f"/collections/{REGISTRY}/points?wait=true",
           {"points": [{"id": registry_id(collection), "vector": {"fingerprint": entry["vector"]}, "payload": payload}]})
    note = f"; planted: the vectors were made by {entry.get('meta_model')}" if entry.get("planted") else ""
    print(f"registered {collection}: {entry['model']} (fingerprint point {str(entry['probe_id'])[:8]}, "
          f"from {entry['source']}){note}")


def switch_alias(q: Qdrant, name: str, target: str) -> None:
    cols = q.collections()
    if target not in cols:
        raise Failed(f"collection {target} does not exist")
    if target == REGISTRY:
        raise Failed("the alias cannot point to the registry")
    old = q.aliases().get(name)
    entries = q.entries()
    if old == target:
        print(f"alias {name} already points to {label(target, entries)}")
        return
    if target not in entries:
        print(f"warning: {target} is not in the registry, the search service will refuse it")
    actions = ([{"delete_alias": {"alias_name": name}}] if old else []) + \
        [{"create_alias": {"collection_name": target, "alias_name": name}}]
    q.call("POST", "/collections/aliases", {"actions": actions})
    print(f"alias {name}: {label(old, entries)} -> {label(target, entries)} (one request to Qdrant)")


def drop(q: Qdrant, collection: str) -> None:
    users = sorted(a for a, c in q.aliases().items() if c == collection)
    if users:
        raise Failed(f"{collection} is used by alias {users[0]}: move the alias first")
    e = q.entry(collection)
    if not (e and e.get("planted")):
        raise Failed(f"{collection} is not a planted collection: drop removes only those, delete others by hand")
    if collection in q.collections():
        q.call("DELETE", f"/collections/{collection}")
    q.call("POST", f"/collections/{REGISTRY}/points/delete?wait=true", {"points": [registry_id(collection)]})
    print(f"dropped {collection} and its registry entry")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("register")
    r.add_argument("--entry", required=True)
    r.add_argument("--collection", required=True)
    a = sub.add_parser("alias")
    a.add_argument("--name", required=True)
    a.add_argument("--to", required=True)
    d = sub.add_parser("drop")
    d.add_argument("--collection", required=True)
    args = ap.parse_args(argv)
    with open(args.key_file) as f:
        q = Qdrant(args.url, f.read().strip())
    try:
        if args.cmd == "show":
            show(q, args.json)
        elif args.cmd == "register":
            with open(args.entry, encoding="utf-8") as f:
                register(q, json.load(f), args.collection)
        elif args.cmd == "alias":
            switch_alias(q, args.name, args.to)
        else:
            drop(q, args.collection)
    except Failed as e:
        print(f"registry: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
