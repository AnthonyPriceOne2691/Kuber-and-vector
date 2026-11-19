"""Ответ модели (app/answer.py): промпт, разбор JSON, проверка цитат, статусы."""
from __future__ import annotations

import json
import unittest

from app import answer as A

HITS = [
    {"document": "Acme License", "section": "12. Governing Law.", "file": "cuad/Acme_License.txt", "span": [0, 500],
     "text": "12. Governing Law. This Agreement shall be governed by the laws of the State of Delaware, "
             "without regard to its conflict of laws principles."},
    {"document": "Acme License", "section": None, "file": "cuad/Acme_License.txt", "span": [500, 1000],
     "text": "Either party may terminate this Agreement upon thirty (30) days’ prior written notice."},
]


def reply(found=True, answer="Delaware law governs [1].", citations=None) -> str:
    if citations is None:
        citations = [{"n": 1, "quote": "governed by the laws of the State of Delaware"}]
    return json.dumps({"found": found, "answer": answer, "citations": citations})


class PromptTest(unittest.TestCase):
    def test_prompt_numbers_fragments(self):
        p = A.prompt("Which law governs?", HITS)
        self.assertTrue(p.startswith("Question: Which law governs?"))
        self.assertIn("[1] Document: Acme License | Section: 12. Governing Law.\n12. Governing Law.", p)
        self.assertIn("[2] Document: Acme License\nEither party", p)  # без раздела нет хвоста «Section»

    def test_chat_body(self):
        b = A.chat_body("qwen3:8b", "Which law governs?", HITS)
        self.assertEqual((b["model"], b["stream"], b["think"]), ("qwen3:8b", False, False))
        self.assertEqual(b["format"], A.SCHEMA)
        self.assertEqual([m["role"] for m in b["messages"]], ["system", "user"])
        self.assertEqual(b["options"], {"temperature": 0, "seed": 7, "num_ctx": A.NUM_CTX, "num_predict": A.MAX_TOKENS})


class ParseTest(unittest.TestCase):
    def test_plain_and_wrapped(self):
        for raw in (reply(), "```json\n" + reply() + "\n```", "<think>\nhmm {x}\n</think>\n" + reply()):
            r = A.parse(raw)
            self.assertEqual(r["found"], True, raw[:30])
            self.assertEqual(r["citations"], [{"n": 1, "quote": "governed by the laws of the State of Delaware"}])

    def test_broken(self):
        for raw in ("", "no json here", "{broken", '{"answer": "x"}', '{"found": "yes"}', "[1, 2]", None):
            self.assertIsNone(A.parse(raw), raw)

    def test_bad_citations_dropped(self):
        r = A.parse(json.dumps({"found": True, "answer": "x", "citations": [
            {"n": "1", "quote": "q"}, {"n": True, "quote": "q"}, {"n": 2}, "junk", {"n": 2, "quote": " ok "}]}))
        self.assertEqual(r["citations"], [{"n": 2, "quote": "ok"}])
        self.assertEqual(A.parse('{"found": true, "answer": 5, "citations": {"n": 1}}'),
                         {"found": True, "answer": "", "citations": []})


class QuoteTest(unittest.TestCase):
    text = HITS[1]["text"]

    def test_exact_and_normalized(self):
        self.assertTrue(A.quote_in("terminate this Agreement upon thirty (30) days", self.text))
        self.assertTrue(A.quote_in("  TERMINATE   this agreement\nupon thirty ", self.text))
        self.assertTrue(A.quote_in("thirty (30) days' prior written notice.", self.text))  # кавычка и точка
        self.assertTrue(A.quote_in("“Either party may terminate”", self.text))

    def test_ellipsis_parts_in_order(self):
        self.assertTrue(A.quote_in("Either party may terminate ... prior written notice", self.text))
        self.assertTrue(A.quote_in("Either party may terminate… prior written notice", self.text))
        self.assertFalse(A.quote_in("prior written notice ... Either party may terminate", self.text))

    def test_rejects_short_or_invented(self):
        self.assertFalse(A.quote_in("notice", self.text))  # слишком коротко, чтобы что-то доказать
        self.assertFalse(A.quote_in("Either party may terminate ... the ... notice", self.text))
        self.assertFalse(A.quote_in("Either party may terminate at any time without notice", self.text))
        self.assertFalse(A.quote_in("", self.text))


class CheckTest(unittest.TestCase):
    def test_answered(self):
        r = A.check(A.parse(reply()), HITS, "Which law governs the Acme license?")
        self.assertEqual(r["status"], "answered")
        self.assertEqual(r["answer"], "Delaware law governs [1].")
        c = r["citations"][0]
        self.assertEqual((c["valid"], c["verified"], c["file"], c["span"]), (True, True, "cuad/Acme_License.txt", [0, 500]))

    def test_refused_in_question_language(self):
        r = A.check(A.parse(reply(found=False, answer="", citations=[])), HITS, "Which law governs?")
        self.assertEqual((r["status"], r["answer"]), ("refused", "The documents do not contain this."))
        r = A.check(A.parse(reply(found=False, answer="", citations=[])), HITS, "Какое право применимо к Acme?")
        self.assertEqual(r["answer"], "В документах этого нет.")

    def test_unsupported_when_no_quote_verifies(self):
        r = A.check(A.parse(reply(citations=[{"n": 1, "quote": "governed by the laws of New York State"},
                                             {"n": 7, "quote": "governed by the laws of the State of Delaware"}])),
                    HITS, "Which law governs?")
        self.assertEqual(r["status"], "unsupported")
        self.assertEqual(r["answer"], "The documents do not contain this.")
        self.assertEqual(r["model_answer"], "Delaware law governs [1].")
        self.assertEqual([(c["valid"], c["verified"]) for c in r["citations"]], [(True, False), (False, False)])
        self.assertNotIn("file", r["citations"][1])  # номер вне списка кусков: адреса нет

    def test_one_verified_is_enough(self):
        r = A.check(A.parse(reply(citations=[{"n": 2, "quote": "made up words that are not there"},
                                             {"n": 1, "quote": "laws of the State of Delaware"}])), HITS, "q?")
        self.assertEqual(r["status"], "answered")

    def test_invalid(self):
        self.assertEqual(A.check(None, HITS, "q?")["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
