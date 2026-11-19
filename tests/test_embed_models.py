"""Префикс вопроса по модели (app/embed_models.py): одно место для сервиса и eval/prepare.py."""
from __future__ import annotations

import unittest

from app import embed_models


class QueryTextTest(unittest.TestCase):
    def test_prefix_by_model(self):
        self.assertEqual(embed_models.query_text("bge-m3", "Which law?"), "Which law?")
        for name in ("snowflake-arctic-embed2", "snowflake-arctic-embed2:latest", "snowflake-arctic-embed2:568m"):
            self.assertEqual(embed_models.query_text(name, "Which law?"), "query: Which law?", name)
        self.assertEqual(embed_models.query_text("nomic-embed-text", "Which law?"), "Which law?")  # без записи как есть

    def test_prefix_table(self):
        self.assertEqual(embed_models.query_prefix("bge-m3:latest"), "")
        self.assertEqual(embed_models.base_name("snowflake-arctic-embed2:568m"), "snowflake-arctic-embed2")


if __name__ == "__main__":
    unittest.main()
