"""Deterministic retrieval providers sharing one document-level hydration path."""

import json
import re
from typing import Any, Protocol, Sequence

from production_rag.knowledge_baseline.snapshot import Snapshot
from production_rag.retrieval.bm25 import LexicalIndex

from .vector import HybridRanker, LocalVectorIndex, VectorRanker

SOURCE_KINDS = {
    "policies": {"synthetic_demo_policy"},
    "references": {"versioned_public_reference"},
    "both": {"synthetic_demo_policy", "versioned_public_reference"},
}
METHODS = {"all", "bm25", "vector", "hybrid"}
ALGORITHMS = {"all": "bm25-existing-tokenizer-v1", "bm25": "bm25-existing-tokenizer-v1",
              "vector": "local-cosine-v1", "hybrid": "rrf-bm25-local-vector-v1"}


class Ranker(Protocol):
    method: str

    def rank(self, units, query: str, top_k: int | None) -> list[tuple[str, float | None]]: ...


class AllRanker:
    method = "all"

    def rank(self, units, query, top_k):
        return [(unit.unit_id, None) for unit in units]


class Bm25Ranker:
    method = "bm25"

    def rank(self, units, query, top_k):
        return LexicalIndex(units).search(query, top_k or 5)


class RetrievalProvider:
    def __init__(self, ranker: Ranker):
        self.ranker = ranker
        self.method = ranker.method

    def retrieve(self, snapshot: Snapshot, query: str, *, sources: str,
                 top_k: int | None = None,
                 document_ids: Sequence[str] = ()) -> dict[str, Any]:
        snapshot = Snapshot.model_validate_json(snapshot.model_dump_json())
        if sources not in SOURCE_KINDS or self.method not in METHODS:
            raise ValueError("explicit supported scope/method required")
        if self.method == "all" and top_k is not None:
            raise ValueError("all returns the entire scope; top_k is not applicable")
        if self.method != "all" and (top_k is not None and (type(top_k) is not int or top_k < 1)):
            raise ValueError("positive top_k required")
        if self.method != "all" and top_k is None:
            top_k = 5
        ranker_snapshot = getattr(self.ranker, "snapshot_id", None)
        if ranker_snapshot is not None and ranker_snapshot != snapshot.snapshot_id:
            raise ValueError("bound index snapshot mismatch")
        all_docs = {d.document_id: d for d in snapshot.documents}
        if set(document_ids) - all_docs.keys():
            raise ValueError("requested document not in snapshot")
        docs = {k: d for k, d in all_docs.items()
                if d.source_kind in SOURCE_KINDS[sources]
                and (not document_ids or k in document_ids)}
        if set(document_ids) - docs.keys():
            raise ValueError("requested document outside source scope")
        units = [u for u in snapshot.units if u.document_id in docs]
        ranked = self.ranker.rank(units, query, top_k)
        result = hydrate(snapshot, all_docs, docs, units, ranked, method=self.method,
                         sources=sources, query=query, top_k=top_k,
                         algorithm=ALGORITHMS[self.method])
        metadata = getattr(self.ranker, "retrieval_metadata", None)
        if metadata is not None:
            result["vector"] = metadata()
            result["embedding_called"] = True
            result["score_semantics"] = ("cosine similarity / RRF rank score; "
                                         "neither is relevance probability")
        return result


PROVIDERS = {
    "all": RetrievalProvider(AllRanker()),
    "bm25": RetrievalProvider(Bm25Ranker()),
}


def provider_for(mode: str) -> RetrievalProvider:
    if mode == "all":
        return PROVIDERS["all"]
    if mode == "rag":
        return PROVIDERS["bm25"]
    raise ValueError("invalid_mode")


def vector_provider(snapshot, embedder, *, index=None, candidate_k=50):
    index = index or LocalVectorIndex.build(snapshot, embedder)
    return RetrievalProvider(VectorRanker(embedder, index, candidate_k=candidate_k))


def hybrid_provider(snapshot, embedder, *, index=None, candidate_k=50, rrf_k=60):
    index = index or LocalVectorIndex.build(snapshot, embedder)
    return RetrievalProvider(HybridRanker(embedder, index, candidate_k=candidate_k,
                                          rrf_k=rrf_k))


def hydrate(snapshot: Snapshot, all_docs, docs, units, ranked, *,
            method: str, sources: str, query: str,
            top_k: int | None,
            algorithm: str = "bm25-existing-tokenizer-v1") -> dict[str, Any]:
    if top_k is not None:
        ranked = ranked[:top_k]
    by_id = {u.unit_id: u for u in units}
    hits = []
    returned = []
    for uid, score in ranked:
        u = by_id[uid]
        d = docs[u.document_id]
        hits.append({**u.model_dump(mode="json"), "score": score,
                     "source_kind": d.source_kind, "source_sha256": d.sha256,
                     "text_sha256": d.text_sha256,
                     "origin_start": d.origin_text_start + u.start,
                     "origin_end": d.origin_text_start + u.end,
                     "origin_offset_convention": d.origin_offset_convention})
        if d.document_id not in returned:
            returned.append(d.document_id)
    contexts = []
    for did in returned:
        d = docs[did]
        references = sorted(set(d.dependencies)
                            | (set(re.findall(r"\b(?:DP-[A-Z]+|CK\d{2})\b", d.text)) - {did}))
        contexts.append({"document_id": did, "version": d.version, "source_kind": d.source_kind,
                         "title": d.title, "applicability": d.applicability,
                         "path": d.path,
                         "source_metadata": json.loads(d.metadata_json)["source_provenance"],
                         "source_sha256": d.sha256, "text_sha256": d.text_sha256,
                         "original_sha256": d.original_sha256,
                         "origin_text_sha256": d.origin_text_sha256,
                         "origin_offset_convention": d.origin_offset_convention,
                         "start": 0, "end": len(d.text), "origin_start": d.origin_text_start,
                         "origin_end": d.origin_text_start + len(d.text), "text": d.text,
                         "dependencies": [{"document_id": dep,
                                           "relation": "declared_policy_dependency"
                                           if dep in d.dependencies else "explicit_text_reference",
                                           "in_snapshot": dep in all_docs,
                                           "in_scope": dep in docs,
                                           "returned": dep in returned} for dep in references]})
    return {"snapshot_id": snapshot.snapshot_id, "algorithm": algorithm,
            "method": method, "sources": sources, "query": query,
            "top_k": None if method == "all" else (top_k or 5),
            "eligible_units": len(units), "returned_units": len(hits),
            "returned_documents": len(contexts),
            "context_characters": sum(len(c["text"]) for c in contexts),
            "hits": hits, "contexts": contexts, "model_called": False,
            "limitations": ["候选与分数不是相关性裁定或风险概率；无命中不表示无要求",
                            "父文档完整返回；依赖仅列出，不自动补查，不能代替完整检查清单",
                            "公开参考不是演示企业要求，适用主体和事实须另核对"]}
