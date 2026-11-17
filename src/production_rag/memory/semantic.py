"""按相关性选择用户显式保存的事实；当前使用词项相关性，非向量语义索引。"""

from production_rag.retrieval.bm25 import tokenize


def relevant_memories(question, items, limit=4):
    query = set(tokenize(question))
    ranked = sorted(
        items,
        key=lambda m: (
            m.kind == "preference",
            len(query & set(tokenize(m.key + m.value))),
            m.updated_at,
        ),
        reverse=True,
    )
    return [m for m in ranked if m.kind == "preference" or query & set(tokenize(m.key + m.value))][
        :limit
    ]
