import unittest

from ex_persona.retrieval import BM25, tokenize


class RetrievalTest(unittest.TestCase):
    def test_tokenize_returns_tokens(self) -> None:
        tokens = tokenize("今晚一起吃饭吗")
        self.assertTrue(tokens)

    def test_bm25_ranks_relevant_doc_first(self) -> None:
        docs = ["今晚一起吃饭吗", "项目需求又改了很烦", "早点睡别熬夜"]
        index = BM25(docs)
        top = index.top_k("需求又改了", 1)
        self.assertEqual(top[0][0], 1)

    def test_bm25_empty_query(self) -> None:
        index = BM25(["a", "b"])
        self.assertEqual(index.top_k("", 3), [])


if __name__ == "__main__":
    unittest.main()
