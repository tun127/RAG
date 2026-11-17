"""确定性中文字符二元组 + 英文词；索引与查询使用相同规则。"""

import re

from rank_bm25 import BM25Okapi


def tokenize(text: str) -> list[str]:
    tokens = []
    for word in re.findall(r"[a-z0-9_./-]+|[\u4e00-\u9fff]+", text.lower()):
        if "\u4e00" <= word[0] <= "\u9fff":
            tokens.extend(word)
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            tokens.append(word)
    return tokens


class LexicalIndex:
    def __init__(self, chunks):
        self.chunks = chunks
        self.model = (
            BM25Okapi([tokenize(c.text) or ["__empty__"] for c in chunks]) if chunks else None
        )

    def search(self, query: str, limit: int):
        if self.model is None:
            return []
        scores = self.model.get_scores(tokenize(query))
        rows = [(c.chunk_id, float(s)) for c, s in zip(self.chunks, scores, strict=True) if s > 0]
        return sorted(rows, key=lambda pair: (-pair[1], pair[0]))[:limit]
