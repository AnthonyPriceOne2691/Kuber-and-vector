"""У каждого скрипта .sh стоит бит запуска: stand-up.sh и сцены зовут их по пути, без bash впереди.
Найдено на пересборке 19.11: девять скриптов, написанных без стенда, лежали в git как 100644,
и подъём встал на третьем шаге (ingress.sh: Permission denied).
"""
from __future__ import annotations

import os
import stat
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDERS = ("deploy", "panel")


class ExecutableTest(unittest.TestCase):
    def test_shell_scripts_are_executable(self):
        missing = []
        for folder in FOLDERS:
            for dirpath, _, filenames in os.walk(os.path.join(ROOT, folder)):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    if name.endswith(".sh") and not os.stat(path).st_mode & stat.S_IXUSR:
                        missing.append(os.path.relpath(path, ROOT))
        self.assertEqual(sorted(missing), [], "нужен chmod +x и git add")


if __name__ == "__main__":
    unittest.main()
