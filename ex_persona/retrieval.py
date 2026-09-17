import logging
import math
import re
from collections import Counter

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

CJK_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    if _HAS_JIEBA:
        return [token for token in jieba.lcut(text) if token.strip()]
    chars = CJK_RE.findall(text or "")
    tokens = list(chars)
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens


class BM25:
    def __init__(
        self,
        docs: list[str],
        k1: float = 1.5,
        b: float = 0.75,
        tokenizer=None,
    ) -> None:
        self.docs = docs
        self.k1 = k1
        self.b = b
        self._tokenize = tokenizer or tokenize
        self.tokens = [self._tokenize(doc) for doc in docs]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.avg_len = sum(self.lengths) / len(self.lengths) if self.lengths else 0.0
        self.freqs = [Counter(tokens) for tokens in self.tokens]

        doc_freq: Counter[str] = Counter()
        for freq in self.freqs:
            doc_freq.update(freq.keys())
        total = len(docs)
        self.idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in doc_freq.items()
        }

    def score(self, query: str) -> list[float]:
        scores = [0.0] * len(self.docs)
        for term in self._tokenize(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for index, freq in enumerate(self.freqs):
                tf = freq.get(term, 0)
                if not tf:
                    continue
                length = self.lengths[index] or 1
                norm = 1 - self.b + self.b * length / (self.avg_len or 1)
                scores[index] += idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        return scores

    def top_k(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        scores = self.score(query)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [(i, scores[i]) for i in order[:k] if scores[i] > 0]
