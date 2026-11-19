"""Подготовка манифестов для проверок в CI (deploy/ci/render.py): все шаблоны подставлены, новые не теряются."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ci_render", os.path.join(ROOT, "deploy", "ci", "render.py"))
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)


class RenderTest(unittest.TestCase):
    def test_manifests_found_values_skipped(self):
        rel = {str(p.relative_to(pathlib.Path(ROOT) / "deploy")) for p in render.manifests()}
        for must in ("search/k8s.yaml", "search/netpol.yaml", "qdrant/gate.yaml", "qdrant/netpol.yaml",
                     "qdrant/local-svc.yaml", "monitoring/k8s.yaml", "k3s/image-policy.yaml"):
            self.assertIn(must, rel)
        for never in ("qdrant/values.yaml", "falco/values.yaml"):
            self.assertNotIn(never, rel, "values чарта проверяются через helm template")

    def test_all_placeholders_replaced(self):
        for path in render.manifests():
            text = render.render(path.read_text(encoding="utf-8"))
            for key in render.VALUES:
                self.assertNotIn(key, text.replace("QDRANT_LOCAL_CLUSTER_IP", ""), f"{path.name}: {key}")

    def test_unknown_placeholder_fails(self):
        with self.assertRaises(ValueError):
            render.render("kind: X\nvalue: NEW_SERVICE_IP\n")
        with self.assertRaises(ValueError):
            render.render("kind: X\nvalue: @NEW@\n")
        # неизвестное имя в комментарии не ошибка: комментарий в манифест не идёт
        self.assertEqual(render.render("kind: X  # про OTHER_IP\nip: ENTRY_IP\n"), "kind: X  # про OTHER_IP\nip: 192.0.2.10\n")

    def test_main_writes_tree(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(render.main([d]), 0)
            self.assertTrue((pathlib.Path(d) / "search" / "k8s.yaml").is_file())
        self.assertIn("манифестов:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
