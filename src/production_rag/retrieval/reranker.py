"""可替换的词项覆盖重排，不把它包装为 Cross-Encoder。"""

from production_rag.retrieval.bm25 import tokenize


def rerank(query: str, hits):
    terms = set(tokenize(query))
    return sorted(
        hits,
        key=lambda h: (
            -len(terms & set(tokenize(h.chunk.text))) / max(1, len(terms)),
            -h.score,
            h.chunk.chunk_id,
        ),
    )
