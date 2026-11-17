"""Offline vector components adapted from knowledge_vector at 9ca9e53.

The Milvus/Ollama persistence adapters are intentionally not ported: this
module keeps the identity, metadata, validation and ranking boundaries and
uses an in-memory deterministic index for offline integration.
"""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any, Protocol, Sequence

from production_rag.knowledge_baseline.snapshot import Snapshot
from production_rag.retrieval.bm25 import LexicalIndex, tokenize
from production_rag.review_inputs.models import digest


@dataclass(frozen=True)
class Identity:
    model: str
    revision: str
    dimension: int
    index_type: str = "FLAT"
    metric: str = "COSINE"

    def __post_init__(self):
        if (not self.model or not self.revision or type(self.dimension) is not int
                or self.dimension < 1 or self.index_type != "FLAT" or self.metric != "COSINE"):
            raise ValueError("explicit model revision, dimension and supported index required")

    @property
    def key(self):
        return digest(json.dumps(asdict(self), sort_keys=True).encode())


def metadata(snapshot, identity):
    snapshot = Snapshot.model_validate_json(snapshot.model_dump_json())
    docs = {d.document_id: d for d in snapshot.documents}
    return {u.unit_id: {"snapshot_id": snapshot.snapshot_id, "unit_id": u.unit_id,
                       "document_id": u.document_id, "version": u.version,
                       "source_kind": docs[u.document_id].source_kind,
                       "unit_text_sha256": digest(u.text.encode()),
                       "identity": asdict(identity), "identity_key": identity.key}
            for u in snapshot.units}


def vectors_ok(rows, count, dimension):
    if len(rows) != count or any(len(v) != dimension or not all(math.isfinite(x) for x in v)
                                  or not any(v) for v in rows):
        raise ValueError("vector count/dimension/finite/nonzero mismatch")


class Embedder(Protocol):
    settings: Any

    def digest(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic offline double; it is not a semantic embedding model."""

    def __init__(self, *, model="offline-hash-v1", dimension=32):
        if not model or type(dimension) is not int or dimension < 1:
            raise ValueError("explicit offline model and positive dimension required")
        self.settings = SimpleNamespace(embedding_model=model, embedding_dim=dimension)
        self._revision = digest(f"{model}\0{dimension}\0pilot-offline-hash-v1".encode())

    def digest(self):
        return self._revision

    def embed(self, texts, timeout=0):
        return [self._vector(text) for text in texts]

    def _vector(self, text):
        values = [0.0] * self.settings.embedding_dim
        for token in tokenize(text):
            entry = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(entry[:4], "big") % len(values)
            values[index] += 1.0 if entry[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:
            values[0] = 1.0
            norm = 1.0
        return [value / norm for value in values]


def identity_from_embedder(embedder):
    return Identity(model=embedder.settings.embedding_model,
                    revision=embedder.digest(), dimension=embedder.settings.embedding_dim)


class LocalVectorIndex:
    backend = "local-memory"

    def __init__(self, *, snapshot_id, identity, expected, vectors):
        if set(expected) != set(vectors) or not expected:
            raise ValueError("incomplete vector inventory")
        self.snapshot_id = snapshot_id
        self.identity = identity
        self.expected = expected
        self.vectors = vectors

    @classmethod
    def build(cls, snapshot, embedder, *, identity=None):
        snapshot = Snapshot.model_validate_json(snapshot.model_dump_json())
        identity = identity or identity_from_embedder(embedder)
        if (embedder.settings.embedding_model != identity.model
                or embedder.settings.embedding_dim != identity.dimension
                or embedder.digest() != identity.revision):
            raise ValueError("embedding model/revision/dimension mismatch")
        expected = metadata(snapshot, identity)
        rows = embedder.embed([u.text for u in snapshot.units])
        vectors_ok(rows, len(expected), identity.dimension)
        if embedder.digest() != identity.revision:
            raise ValueError("embedding revision drift")
        vectors = {u.unit_id: vector for u, vector in zip(snapshot.units, rows, strict=True)}
        return cls(snapshot_id=snapshot.snapshot_id, identity=identity,
                   expected=expected, vectors=vectors)

    def validate_units(self, units):
        for unit in units:
            entry = self.expected.get(unit.unit_id)
            if (entry is None or entry["snapshot_id"] != self.snapshot_id
                    or entry["identity_key"] != self.identity.key
                    or entry["document_id"] != unit.document_id
                    or entry["version"] != unit.version
                    or entry["unit_text_sha256"] != digest(unit.text.encode())):
                raise ValueError("bound index snapshot mismatch")

    def search_bound(self, vector, allowed, limit):
        vectors_ok([vector], 1, self.identity.dimension)
        if type(limit) is not int or limit < 1:
            raise ValueError("positive limit required")
        allowed = set(allowed)
        if allowed - set(self.expected):
            raise ValueError("allowed unit outside bound index")
        if not allowed:
            return []
        ranked = []
        for unit_id in allowed:
            score = _cosine(vector, self.vectors[unit_id])
            if not math.isfinite(score):
                raise ValueError("non-finite vector score")
            ranked.append((unit_id, score))
        return sorted(ranked, key=lambda pair: (-pair[1], pair[0]))[:limit]


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("zero vector")
    score = dot / (left_norm * right_norm)
    if not math.isfinite(score):
        raise ValueError("non-finite cosine")
    return score


def rrf_fuse(rankings, *, rrf_k=60, top_k=5):
    if type(rrf_k) is not int or rrf_k < 1:
        raise ValueError("positive rrf_k required")
    if type(top_k) is not int or top_k < 1:
        raise ValueError("positive top_k required")
    scores = {}
    for ranking in rankings:
        seen = set()
        for rank, (unit_id, _) in enumerate(ranking, 1):
            if unit_id in seen:
                continue
            seen.add(unit_id)
            scores[unit_id] = scores.get(unit_id, 0.0) + 1.0 / (rrf_k + rank)
    ordered = sorted(scores, key=lambda unit_id: (-scores[unit_id], unit_id))[:top_k]
    return [(unit_id, scores[unit_id]) for unit_id in ordered]


class VectorRanker:
    method = "vector"

    def __init__(self, embedder, index, *, candidate_k=50):
        if type(candidate_k) is not int or candidate_k < 1:
            raise ValueError("positive candidate_k required")
        if (embedder.settings.embedding_model != index.identity.model
                or embedder.settings.embedding_dim != index.identity.dimension
                or embedder.digest() != index.identity.revision):
            raise ValueError("embedding model/revision/dimension mismatch")
        self.embedder = embedder
        self.index = index
        self.candidate_k = candidate_k

    @property
    def snapshot_id(self):
        return self.index.snapshot_id

    def retrieval_metadata(self):
        return {"backend": self.index.backend, "snapshot_id": self.index.snapshot_id,
                "identity_key": self.index.identity.key, "candidate_k": self.candidate_k}

    def _query_vector(self, query):
        if self.embedder.digest() != self.index.identity.revision:
            raise ValueError("embedding revision mismatch")
        vector = self.embedder.embed([query])[0]
        vectors_ok([vector], 1, self.index.identity.dimension)
        if self.embedder.digest() != self.index.identity.revision:
            raise ValueError("embedding revision drift")
        return vector

    def rank_candidates(self, units, query):
        units = list(units)
        if not units:
            return []
        self.index.validate_units(units)
        allowed = {unit.unit_id for unit in units}
        ranked = self.index.search_bound(self._query_vector(query), allowed, self.candidate_k)
        if (len({unit_id for unit_id, _ in ranked}) != len(ranked)
                or any(unit_id not in allowed or not math.isfinite(score)
                       for unit_id, score in ranked)):
            raise ValueError("invalid returned records")
        return ranked

    def rank(self, units, query, top_k):
        effective = _effective_top_k(top_k)
        if effective > self.candidate_k:
            raise ValueError("candidate_k must be >= top_k")
        return self.rank_candidates(units, query)[:effective]


class HybridRanker:
    method = "hybrid"

    def __init__(self, embedder, index, *, candidate_k=50, rrf_k=60):
        if type(rrf_k) is not int or rrf_k < 1:
            raise ValueError("positive rrf_k required")
        self.vector = VectorRanker(embedder, index, candidate_k=candidate_k)
        self.candidate_k = candidate_k
        self.rrf_k = rrf_k

    @property
    def snapshot_id(self):
        return self.vector.snapshot_id

    def retrieval_metadata(self):
        return {**self.vector.retrieval_metadata(), "rrf_k": self.rrf_k}

    def rank(self, units, query, top_k):
        effective = _effective_top_k(top_k)
        if effective > self.candidate_k:
            raise ValueError("candidate_k must be >= top_k")
        units = list(units)
        vector_ranked = self.vector.rank_candidates(units, query)
        lexical_ranked = LexicalIndex(units).search(query, self.candidate_k)
        return rrf_fuse([vector_ranked, lexical_ranked], rrf_k=self.rrf_k, top_k=effective)


def _effective_top_k(top_k):
    if top_k is None:
        return 5
    if type(top_k) is not int or top_k < 1:
        raise ValueError("positive top_k required")
    return top_k
