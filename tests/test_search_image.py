"""Образ поиска (deploy/search/Containerfile) без pip: trivy 19.11 нашёл в нём 5 из 6 исправимых HIGH,
все во вшитых в pip msgpack, urllib3 и setuptools. Порядок в RUN важен: колёса ставит pip без сети,
потом pip снимает setuptools, wheel и себя последним, убирается его копия в ensurepip, и тут же
проверяется, что pip не импортируется, а сервис и шлюз Qdrant импортируются. Сборка образа идёт
только на стенде (deploy/search/build.sh), здесь разбирается текст Containerfile.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_instructions() -> list[str]:
    with open(os.path.join(ROOT, "deploy", "search", "Containerfile"), encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"\\\n\s*", " ", text)  # продолжения строк
    return [line[4:] for line in text.splitlines() if line.startswith("RUN ")]


class SearchImageTest(unittest.TestCase):
    def test_pip_removed_right_after_install(self):
        runs = run_instructions()
        self.assertEqual(len(runs), 1, "установка колёс, удаление pip и проверки идут одной командой RUN")
        steps = [s.strip() for s in runs[0].split("&&")]
        install = next(i for i, s in enumerate(steps) if "-m pip install" in s)
        uninstall = next(i for i, s in enumerate(steps) if "-m pip uninstall" in s)
        bundled = next(i for i, s in enumerate(steps) if "ensurepip/_bundled" in s)
        no_pip = steps.index("! python -c 'import pip' 2>/dev/null")
        works = next(i for i, s in enumerate(steps) if s.startswith("python -c 'import fastapi"))
        self.assertLess(install, uninstall)
        self.assertLess(uninstall, bundled)
        self.assertLess(bundled, no_pip)
        self.assertLess(no_pip, works)
        self.assertIn("--no-index", steps[install], "сборка без сети")
        args = steps[uninstall].split()
        self.assertIn("-y", args)
        self.assertEqual(args[-3:], ["setuptools", "wheel", "pip"], "pip снимает себя последним")
        self.assertIn("-delete", steps[bundled])
        for module in ("fastapi", "httpx", "uvicorn", "http.server"):
            self.assertIn(module, steps[works])
        self.assertTrue(steps[-1].startswith("useradd --uid 10001"))


if __name__ == "__main__":
    unittest.main()
