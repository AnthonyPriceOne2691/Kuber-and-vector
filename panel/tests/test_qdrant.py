"""Разбор ответов Qdrant /cluster и /collections/{c}/cluster, карта «шард × машина», источник qdrant."""
import copy
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sources import QdrantSource, build_qdrant, display_state, parse_kube, parse_time, transfer_progress

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
M1, M2, M3 = "2738916503459183011", "11635047302187392211", "6029477265530419423"
NOW = parse_time("2026-11-18T10:06:31Z")


def fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def ok_view(machine, cluster, coll):
    cl, cc = fixture(cluster)["result"], fixture(coll)["result"]
    return {"url": f"http://{machine}:6333", "machine": machine, "ok": True, "cluster": cl, "collection": cc,
            "peer_id": str(cl["peer_id"])}


def down_view(machine, last_peer):
    return {"url": f"http://{machine}:6333", "machine": machine, "ok": False, "peer_id": last_peer, "error": "timed out"}


PODS = parse_kube(fixture("kubectl_nodes_pods.json"))["pods"]


def cells(q):
    return {(sh["id"], r["machine"]): r["display"] for sh in q["shards"] for r in sh["replicas"]}


class BuildQdrantTest(unittest.TestCase):
    def test_peer_down_map(self):
        data = {"collection": "lbr", "views": [ok_view("machine-1", "qdrant_cluster_m1.json", "qdrant_collection_m1_down.json"),
                                              down_view("machine-2", M2),
                                              ok_view("machine-3", "qdrant_cluster_m3.json", "qdrant_collection_m3_down.json")]}
        q = build_qdrant(data, PODS, now=NOW)
        self.assertTrue(q["ok"])
        peers = {p["id"]: p for p in q["peers"]}
        self.assertEqual(set(peers), {M1, M2, M3})  # номера — строки, u64 не теряется
        self.assertEqual({p["machine"] for p in q["peers"]}, {"machine-1", "machine-2", "machine-3"})
        self.assertEqual(peers[M2]["pod"], "qdrant-1")
        self.assertFalse(peers[M2]["reachable"])
        self.assertTrue(peers[M1]["leader"])
        self.assertEqual(q["raft"]["leader_machine"], "machine-1")
        c = cells(q)
        self.assertEqual(c[(0, "machine-2")], "dead")
        self.assertEqual(c[(1, "machine-2")], "offline")  # консенсус ещё считает Active, но пир молчит
        self.assertEqual(c[(4, "machine-2")], "offline")
        self.assertEqual(c[(0, "machine-1")], "active")
        self.assertNotIn((2, "machine-2"), c)
        s = q["summary"]
        self.assertEqual((s["active"], s["dead"], s["offline"], s["total"]), (8, 2, 2, 12))
        self.assertFalse(s["healthy"])
        self.assertEqual(q["shard_count"], 6)
        self.assertEqual(q["replication_factor"], 2)

    def test_consensus_failures_mark_peer_down_without_direct_probe(self):
        data = {"views": [ok_view("machine-1", "qdrant_cluster_m1.json", "qdrant_collection_m1_down.json")]}
        q = build_qdrant(data, PODS, now=NOW)
        self.assertFalse({p["id"]: p for p in q["peers"]}[M2]["reachable"])
        # минуту спустя та же старая ошибка уже не считается признаком
        q = build_qdrant(data, PODS, now=NOW + 60)
        self.assertTrue({p["id"]: p for p in q["peers"]}[M2]["reachable"])
        self.assertEqual(cells(q)[(1, "machine-2")], "active")

    def test_recovering_partial_and_progress(self):
        v = ok_view("machine-1", "qdrant_cluster_m1.json", "qdrant_collection_m1_recovering.json")
        v["cluster"]["message_send_failures"] = {}
        q = build_qdrant({"views": [v]}, PODS, now=NOW)
        c = cells(q)
        self.assertEqual(c[(1, "machine-2")], "recovering")
        self.assertEqual(c[(4, "machine-2")], "partial")  # Partial без переноса
        self.assertEqual(c[(3, "machine-2")], "dead")
        self.assertEqual(c[(0, "machine-2")], "active")
        rep = next(r for sh in q["shards"] if sh["id"] == 1 for r in sh["replicas"] if r["machine"] == "machine-2")
        self.assertEqual(rep["progress"], 0.414)
        self.assertEqual(rep["from_machine"], "machine-3")
        t = q["transfers"][0]
        self.assertEqual((t["shard"], t["from_machine"], t["to_machine"], t["method"]), (1, "machine-3", "machine-2", "stream_records"))

    def test_healthy_when_all_active(self):
        v = ok_view("machine-1", "qdrant_cluster_m1.json", "qdrant_collection_m1_recovering.json")
        v["cluster"]["message_send_failures"] = {}
        for s in v["collection"]["remote_shards"]:
            s["state"] = "Active"
        v["collection"]["shard_transfers"] = []
        q = build_qdrant({"views": [v]}, PODS, now=NOW)
        self.assertTrue(q["summary"]["healthy"])
        self.assertEqual(q["summary"]["active"], 12)

    def test_mapping_priority(self):
        data = {"views": [ok_view("machine-1", "qdrant_cluster_m1.json", "qdrant_collection_m1_down.json")]}
        q = build_qdrant(data, PODS, peer_map={M2: "machine-9"}, now=NOW)
        self.assertEqual({p["id"]: p["machine"] for p in q["peers"]}[M2], "machine-9")
        # без подов Kubernetes машина берётся из подсказки у адреса опроса
        q = build_qdrant(data, [], now=NOW)
        machines = {p["id"]: p["machine"] for p in q["peers"]}
        self.assertEqual(machines[M1], "machine-1")
        self.assertIsNone(machines[M3])

    def test_mapping_by_pod_ip(self):
        v = ok_view("machine-1", "qdrant_cluster_m1.json", "qdrant_collection_m1_down.json")
        v["cluster"]["peers"][M3]["uri"] = "http://10.42.2.12:6335/"
        q = build_qdrant({"views": [v]}, PODS, now=NOW)
        self.assertEqual({p["id"]: p["machine"] for p in q["peers"]}[M3], "machine-3")

    def test_no_answers(self):
        q = build_qdrant({"views": [down_view("machine-1", M1)], "collection": "lbr"}, PODS, now=NOW)
        self.assertFalse(q["ok"])
        self.assertEqual(q["shards"], [])
        self.assertFalse(q["summary"]["healthy"])
        good = build_qdrant({"views": [ok_view("machine-1", "qdrant_cluster_m1.json",
                                               "qdrant_collection_m1_down.json")]}, PODS, now=NOW)
        self.assertEqual(set(q), set(good))  # тот же набор ключей: контракт не зависит от ok
        self.assertIsNone(build_qdrant(None))

    def test_display_state_and_progress(self):
        self.assertEqual(display_state("Active", True, False), "active")
        self.assertEqual(display_state("ActiveRead", True, False), "active")
        self.assertEqual(display_state("Dead", False, False), "dead")
        self.assertEqual(display_state("Active", False, False), "offline")
        self.assertEqual(display_state("Partial", True, True), "recovering")
        self.assertEqual(display_state("Recovery", True, False), "recovering")
        self.assertEqual(display_state("Initializing", True, False), "partial")
        self.assertEqual(display_state("Listener", True, False), "listener")
        self.assertEqual(display_state("Whatever", True, False), "unknown")
        self.assertEqual(transfer_progress("Transferring records (1200/2900), started 3s ago"), 0.414)
        self.assertIsNone(transfer_progress("Transferring"))
        self.assertIsNone(transfer_progress(None))


class QdrantSourceTest(unittest.TestCase):
    def fetcher(self, down=(), aliases=None, alias_error=None):
        docs = {
            "m1": (fixture("qdrant_cluster_m1.json"), fixture("qdrant_collection_m1_down.json")),
            "m3": (fixture("qdrant_cluster_m3.json"), fixture("qdrant_collection_m3_down.json")),
        }
        m2_cluster = copy.deepcopy(docs["m3"][0])
        m2_cluster["result"]["peer_id"] = int(M2)
        docs["m2"] = (m2_cluster, fixture("qdrant_collection_m3_down.json"))
        paths = []

        def fetch(url, path):
            paths.append((url, path))
            host = url.split("//")[1].split(":")[0]
            if host in down:
                raise TimeoutError("timed out")
            if path == "/aliases":
                if alias_error:
                    raise alias_error
                return {"result": {"aliases": aliases or []}}
            if path == "/cluster":
                return docs[host][0]
            if path.endswith("/cluster"):
                return docs[host][1]
            return {"result": {"status": "yellow", "points_count": 17403,
                               "config": {"params": {"shard_number": 6, "replication_factor": 2}}}}
        return fetch, paths

    def source(self, fetch):
        return QdrantSource([("machine-1", "http://m1:6333"), ("machine-2", "http://m2:6333"),
                             ("machine-3", "http://m3:6333")], None, "lbr", fetch=fetch)

    def test_remembers_who_answered_at_a_failing_address(self):
        down = set()
        fetch, _ = self.fetcher(down=down)
        src = self.source(fetch)
        src.poll(now=NOW - 5)
        self.assertEqual(src.data()["views"][1]["peer_id"], M2)
        down.add("m2")
        src.poll(now=NOW)
        v2 = src.data()["views"][1]
        self.assertFalse(v2["ok"])
        self.assertEqual(v2["peer_id"], M2)  # кто отвечал по этому адресу в прошлый раз
        self.assertEqual(v2["error"], "timed out")
        q = build_qdrant(src.data(), PODS, now=NOW)
        self.assertFalse({p["id"]: p for p in q["peers"]}[M2]["reachable"])
        self.assertEqual(q["status"], "yellow")
        self.assertEqual(q["points"], 17403)

    def test_alias_is_resolved_to_collection(self):
        fetch, paths = self.fetcher(aliases=[{"alias_name": "lbr", "collection_name": "lbr_e5_v2"}])
        src = self.source(fetch)
        src.poll(now=NOW)
        self.assertEqual((src.data()["collection"], src.data()["alias"]), ("lbr_e5_v2", "lbr"))
        self.assertIn(("http://m1:6333", "/collections/lbr_e5_v2/cluster"), paths)

    def test_alias_forbidden_keeps_name(self):
        fetch, paths = self.fetcher(alias_error=PermissionError("HTTP 403"))
        src = self.source(fetch)
        src.poll(now=NOW)
        self.assertEqual((src.data()["collection"], src.data()["alias"]), ("lbr", None))

    def test_all_down_keeps_last_data(self):
        down = set()
        fetch, _ = self.fetcher(down=down)
        src = self.source(fetch)
        src.poll(now=NOW)
        down.update({"m1", "m2", "m3"})
        src.poll(now=NOW + 3)
        st = src.status(NOW + 3)
        self.assertFalse(st["ok"])
        self.assertIn("not responding", st["error"])
        self.assertTrue(src.data()["views"][0]["ok"])  # кэш последнего удачного ответа


class _Qdrant(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.keys.append(self.headers.get("api-key"))
        if self.server.deny:
            body, code = b'{"status": {"error": "Unauthorized"}}', 401
        elif self.path == "/cluster":
            body, code = json.dumps(fixture("qdrant_cluster_m1.json")).encode(), 200
        elif self.path == "/collections/lbr/cluster":
            body, code = json.dumps(fixture("qdrant_collection_m1_down.json")).encode(), 200
        else:
            body, code = b'{"result": {"aliases": []}}', 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ReadOnlyKeyTest(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Qdrant)
        self.srv.keys, self.srv.deny = [], False
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = os.path.join(self.tmp.name, "read-only-api-key")
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write("ro-key-123\n")
        self.src = QdrantSource([(None, f"http://127.0.0.1:{self.srv.server_address[1]}")], self.key_file, "lbr")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def test_key_from_file_goes_only_to_header(self):
        self.src.poll(now=NOW)
        self.assertTrue(self.src.status(NOW)["ok"])
        self.assertEqual(set(self.srv.keys), {"ro-key-123"})
        self.assertNotIn("ro-key-123", json.dumps(self.src.data()))

    def test_key_file_is_reread_after_rotation(self):
        self.src.poll(now=NOW)
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write("ro-key-456")
        st = os.stat(self.key_file)
        os.utime(self.key_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.src.poll(now=NOW + 1)
        self.assertEqual(self.srv.keys[-1], "ro-key-456")

    def test_unauthorized_is_reported_without_the_key(self):
        self.srv.deny = True
        self.src.poll(now=NOW)
        st = self.src.status(NOW)
        self.assertFalse(st["ok"])
        self.assertIn("read-only API key", st["error"])
        self.assertNotIn("ro-key-123", st["error"])


if __name__ == "__main__":
    unittest.main()
