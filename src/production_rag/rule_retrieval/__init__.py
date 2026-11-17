"""Whole enterprise-rule retrieval; no contract judgement or model calls."""

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

from production_rag.retrieval.bm25 import tokenize


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class Scope:
    enterprise_role: str
    contract_type: str
    version: str
    authority: str = "synthetic_enterprise_policy_not_law"

    def __post_init__(self):
        for name, value in vars(self).items():
            _text(value, name)


class RuleRetriever:
    """Inject a validated file snapshot. Results are independent dictionary copies."""

    def __init__(self, source: str | Path):
        self.source = str(Path(source).resolve())
        payload = Path(source).read_bytes()
        self.sha256 = hashlib.sha256(payload).hexdigest()
        document = json.loads(payload, object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or document.get("schema_version") != "0.1":
            raise ValueError("unsupported schema_version (expected 0.1)")
        rules = document.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ValueError("rules must be a nonempty list")
        seen = set()
        required = (
            "rule_id", "title", "version", "authority", "enterprise_role", "contract_type",
            "requirement", "exceptions", "note", "source_id", "suggested_action",
        )
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("each rule must be an object")
            for field in required:
                _text(rule.get(field), field)
            if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", rule["version"]):
                raise ValueError("invalid rule version")
            if type(rule.get("professional_review")) is not bool:
                raise ValueError("professional_review must be boolean")
            identity = (rule["source_id"], rule["rule_id"], rule["version"])
            if identity in seen:
                raise ValueError(f"duplicate rule identity: {identity}")
            seen.add(identity)
        self._document = document

    def search(self, query: str, *, scope: Scope, top_k: int | None = None,
               method: str = "bm25") -> list[dict]:
        """all ignores relevance; top_k=None returns every scoped candidate.

        BM25 retains even zero-score candidates, never asserting relevance.
        Scores are ranking signals, not risk probabilities.
        """
        _text(query, "query")
        if not isinstance(scope, Scope):
            raise ValueError("explicit Scope required")
        if top_k is not None and (type(top_k) is not int or top_k < 1):
            raise ValueError("top_k must be a positive integer or None")
        if method not in {"all", "bm25"}:
            raise ValueError("method must be all or bm25")
        rows = [(i, r) for i, r in enumerate(self._document["rules"])
                if all(r[key] == value for key, value in vars(scope).items())]
        scores = [0.0] * len(rows)
        if rows and method == "bm25":
            corpus = [tokenize("\n".join(r[k] for k in
                      ("title", "requirement", "exceptions", "note"))) for _, r in rows]
            scores = BM25Okapi(corpus).get_scores(tokenize(query))
        ranked = sorted(zip(rows, scores, strict=True),
                        key=lambda item: (-float(item[1]), item[0][1]["source_id"],
                                          item[0][1]["rule_id"], item[0][1]["version"]))
        results = []
        for (index, rule), score in ranked[:top_k]:
            results.append({
                **copy.deepcopy(rule), "source_file": self.source,
                "source_sha256": self.sha256, "source_pointer": f"/rules/{index}",
                "schema_version": self._document["schema_version"],
                "score": float(score), "method": method, "relevance": "unconfirmed",
            })
        return results


__all__ = ["RuleRetriever", "Scope"]
