import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ReplyError(ValueError):
    def __init__(self, kind, details=None):
        self.kind = kind
        self.details = details
        super().__init__(kind)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_id: str
    start: int
    end: int
    quote: str = Field(min_length=1, max_length=400)


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conclusion: Literal["compliant", "deviation", "needs_review", "not_applicable"]
    reason: str = Field(min_length=1, max_length=400)
    evidence: list[Citation] = Field(max_length=4)
    basis: Literal["cited_text", "material_inventory", "full_supplier_text"]
    material_ids: list[str] = Field(max_length=20)


class BlockCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_id: str
    block_id: str
    quote: str = Field(max_length=400)


class BlockReply(Reply):
    evidence: list[BlockCitation] = Field(max_length=4)


def _pairs(items):
    result = {}
    for k, v in items:
        if k in result:
            raise ValueError("duplicate key")
        result[k] = v
    return result


def validate_reply(text, run, task, *, protocol="review-result-v1", block_maps=None):
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise ReplyError("parse_error") from None
    if protocol not in ("review-result-v1", "review-result-v2"):
        raise ReplyError("unsupported_output_protocol")
    try:
        reply = (Reply if protocol == "review-result-v1" else BlockReply).model_validate(value)
    except ValidationError:
        raise ReplyError("invalid_output") from None
    if protocol == "review-result-v1":
        return _validated_result(reply, run, task)
    from production_rag.review_execution.locator import LOCATOR_METHOD, LocationError, locate

    details = {
        "model_evidence": [c.model_dump() for c in reply.evidence],
        "locator_method": LOCATOR_METHOD, "locations": [],
    }
    located = []
    for claim in reply.evidence:
        try:
            found = locate(run, claim, block_maps)
        except LocationError as exc:
            details["locations"].append({"model_evidence": claim.model_dump(), "error": str(exc)})
            raise ReplyError(str(exc), details) from None
        located.append(found)
        details["locations"].append(found)
    # Reuse the existing strict range/role/material checks after exact location.
    offset_reply = Reply.model_validate({
        **reply.model_dump(), "evidence": [
            {k: ev[k] for k in ("document_id", "start", "end", "quote")} for ev in located
        ],
    })
    try:
        result = _validated_result(offset_reply, run, task)
    except ReplyError as exc:
        raise ReplyError(exc.kind, details) from None
    result["evidence"] = [{**ev, **found} for ev, found in zip(result["evidence"], located, strict=True)]
    result["citation_diagnostics"] = details
    return result


def _validated_result(reply, run, task):
    docs = {d.document_id: d for d in run.documents}
    evidence = []
    for c in reply.evidence:
        try:
            source = docs[c.document_id].cite(c.start, c.end)
        except (KeyError, ValueError):
            raise ReplyError("invalid_citation") from None
        if source["quote"] != c.quote:
            raise ReplyError("invalid_citation")
        evidence.append({**source, "evidence_kind": {
            "supplier": "supplier_evidence", "standard": "standard_reference",
            "material": "material_evidence",
        }[source["role"]]})
    inventory = {m.material_id: m.model_dump() for m in run.materials}
    if len(set(reply.material_ids)) != len(reply.material_ids) or any(
        mid not in inventory for mid in reply.material_ids
    ):
        raise ReplyError("invalid_material_basis")
    if reply.basis == "cited_text" and not evidence:
        raise ReplyError("invalid_citation")
    if reply.basis == "material_inventory" and not reply.material_ids:
        raise ReplyError("invalid_material_basis")
    if reply.conclusion != "needs_review" and reply.basis == "cited_text" and not any(
        e["role"] in ("supplier", "material") for e in evidence
    ):
        raise ReplyError("standard_reference_only")
    return {
        "runtime_status": "success", "conclusion": reply.conclusion,
        "reason": reply.reason, "failure_type": None, "evidence": evidence,
        "basis": reply.basis,
        "material_basis": [inventory[mid] for mid in reply.material_ids],
        "coverage_basis": {"document_id": d.document_id, "text_sha256": d.text_sha256,
                           "scope": "entire_fixed_supplier_text_not_proof_of_absence"}
        if (d := next(d for d in run.documents if d.role == "supplier"))
        and reply.basis == "full_supplier_text" else None,
        "rule_basis": {"source_id": task.source_id, "rule_id": task.rule_id,
                       "version": task.rule_version, "sha256": run.rule_sha256,
                       "source_pointer": task.source_pointer, "rule_json": task.rule_json},
        "citation_validity": "exact_source_match_only_not_semantic_support",
    }
