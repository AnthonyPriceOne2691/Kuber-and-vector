"""Образ поиска (deploy/search/build.sh) получает все модули app, которые нужны api.py.
Найдено на пересборке 19.11: answer.py и metrics.py дописали без стенда, в образ они не попали,
и все три пода поиска падали на импорте (CrashLoopBackOff).
"""
from __future__ import annotations

import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")


def app_imports(name: str) -> set[str]:
    """Файлы модулей app, которые импортирует app/<name>."""
    with open(os.path.join(APP, name), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app" or (node.level == 1 and node.module is None):
                found |= {a.name + ".py" for a in node.names}
            elif node.module and (node.module.startswith("app.") or node.level == 1):
                found.add(node.module.split(".")[0 if node.level else 1] + ".py")
        elif isinstance(node, ast.Import):
            found |= {a.name.split(".")[1] + ".py" for a in node.names if a.name.startswith("app.")}
    return found


def needed(start: str = "api.py") -> set[str]:
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += sorted(app_imports(name))
    return seen | {"__init__.py"}


class SearchBuildTest(unittest.TestCase):
    def test_image_gets_every_module_api_imports(self):
        with open(os.path.join(ROOT, "deploy", "search", "build.sh"), encoding="utf-8") as f:
            m = re.search(r'^APP_FILES="([^"]*)"', f.read(), re.M)
        self.assertIsNotNone(m, "в build.sh нет строки APP_FILES=")
        listed = set(m.group(1).split())
        self.assertEqual(sorted(needed() - listed), [], "дописать в APP_FILES в deploy/search/build.sh")
        self.assertEqual(sorted(f for f in listed if not os.path.exists(os.path.join(APP, f))), [])

    def test_closure_follows_imports(self):
        self.assertTrue({"api.py", "answer.py", "bm25.py", "metrics.py"} <= needed())


if __name__ == "__main__":
    unittest.main()
