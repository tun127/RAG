"""小语料精确余弦检索；轻量部署仍使用真实 embedding。"""

from math import sqrt


def cosine(a, b) -> float:
    denominator = sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / denominator if denominator else 0.0


class LocalVectorIndex:
    def build(self, snapshot):
        pass  # 向量已在 stage() 中持久化，无需额外构建。

    def search(self, snapshot, vector, limit, timeout=30):
        scored = [
            (c.chunk_id, cosine(v, vector))
            for c, v in zip(snapshot.chunks, snapshot.vectors, strict=True)
        ]
        return sorted(scored, key=lambda pair: (-pair[1], pair[0]))[:limit]
