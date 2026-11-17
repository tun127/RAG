from production_rag.domain.errors import DependencyError
from production_rag.domain.models import SearchHit
from production_rag.retrieval.bm25 import LexicalIndex
from production_rag.retrieval.fusion import reciprocal_rank_fusion
from production_rag.retrieval.reranker import rerank


class Retriever:
    """绑定不可变快照，一次 Agent 运行不会跨知识库版本。"""

    def __init__(self, settings, snapshot, embedder, index):
        self.settings, self.snapshot = settings, snapshot
        self.embedder, self.index = embedder, index
        self.lexical = LexicalIndex(snapshot.chunks)
        self.chunks = {c.chunk_id: c for c in snapshot.chunks}
        self.sources = {d.document_id: d.source for d in snapshot.documents}
        for key in ("embedding_model", "embedding_dim", "vector_backend"):
            if snapshot.config[key] != getattr(settings, key):
                raise DependencyError(f"快照 {key} 与当前配置不同，请重新导入")

    def search(self, query: str, budget, *, mode="hybrid", top_k=None, reranking=False):
        budget.remaining_seconds()
        top_k = top_k or self.settings.top_k
        rankings = []
        if mode in {"vector", "hybrid"} and self.snapshot.chunks:
            identity = self.snapshot.config["embedding_digest"]
            timeout = budget.remaining_seconds()
            vector = (
                self.embedder.query(query, identity, timeout)
                if hasattr(self.embedder, "query")
                else self.embedder.embed([query], timeout=timeout)[0]
            )
            budget.remaining_seconds()
            rankings.append(
                self.index.search(
                    self.snapshot,
                    vector,
                    self.settings.candidate_k,
                    timeout=budget.remaining_seconds(),
                )
            )
        if mode in {"keyword", "hybrid"}:
            rankings.append(self.lexical.search(query, self.settings.candidate_k))
        ranking = (
            reciprocal_rank_fusion(rankings)
            if mode == "hybrid"
            else (rankings[0] if rankings else [])
        )
        hits = [
            self.hit(chunk_id, score, mode)
            for chunk_id, score in ranking
            if chunk_id in self.chunks
        ]
        if reranking:
            hits = rerank(query, hits)
        budget.remaining_seconds()
        return hits[:top_k]

    def hit(self, chunk_id, score=0.0, retriever="read_chunk"):
        chunk = self.chunks[chunk_id]
        return SearchHit(
            chunk=chunk,
            score=float(score),
            retriever=retriever,
            source=self.sources[chunk.document_id],
        )
