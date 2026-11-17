import json
from typing import Literal

from pydantic import Field

from production_rag.knowledge_baseline.snapshot import unique
from production_rag.review_inputs.models import Frozen

from .context import blocks, source_maps
from .inputs import Source


class Citation(Frozen):
    source_type: Literal["contract", "material", "demo_policy"]
    document_id: str
    version: str
    block_id: str
    quote: str = Field(min_length=1, max_length=240)


class Coverage(Frozen):
    document_id: str
    version: str
    text_sha256: str
    block_ids: list[str]


class Reply(Frozen):
    conclusion: Literal["compliant", "deviation", "needs_review", "not_applicable"]
    reason_code: Literal["text_consistent", "text_deviation", "condition_unknown",
                         "condition_not_triggered", "material_unavailable", "absence_candidate"]
    evidence: list[Citation] = Field(max_length=4)
    material_ids: list[str] = Field(max_length=16)
    coverage: Coverage | None


REASONS = {"text_consistent": "模型提出演示政策符合候选，尚待人工核对语义支持",
           "text_deviation": "模型提出演示政策偏差候选，尚待人工核对语义支持",
           "condition_unknown": "适用条件或事实仍待确认",
           "condition_not_triggered": "模型提出条件未触发候选，已知条件与依据仍待人工核对",
           "material_unavailable": "存在声明未提供或未核实的材料",
           "absence_candidate": "已提供合同全文的疑似未覆盖项，不能直接确认为条款缺失"}


def locate(run, maps, declaration):
    mapped = next((m for m in maps if m["source"]["document_id"] == declaration.document_id), None)
    if mapped is None:
        raise ValueError("citation_unknown_document")
    doc = Source.model_validate(mapped["source"])
    if doc.source_type != declaration.source_type or doc.version != declaration.version:
        raise ValueError("citation_wrong_source_or_version")
    if mapped["blocks"] != blocks(doc, run.run_id):
        raise ValueError("block_map_drift")
    block = next((b for b in mapped["blocks"] if b["block_id"] == declaration.block_id), None)
    if block is None:
        raise ValueError("citation_unknown_block")
    quote = declaration.quote
    if not quote.strip():
        raise ValueError("citation_empty")
    offset = block["text"].find(quote)
    if offset < 0:
        raise ValueError("citation_not_found")
    if block["text"].find(quote, offset + 1) >= 0:
        raise ValueError("citation_ambiguous")
    start = block["start"] + offset
    end = start + len(quote)
    if doc.text[start:end] != quote:
        raise ValueError("citation_fixed_text_mismatch")
    return {**declaration.model_dump(), "start": start, "end": end,
            "text_sha256": doc.text_sha256, "raw_sha256": doc.raw_sha256,
            "locator_method": "unique_exact_in_declared_block-v1", "semantic_support": "pending_human_review"}


def validate(run, task, prepared, text):
    reply = Reply.model_validate(json.loads(text, object_pairs_hook=unique), strict=True)
    if prepared["maps"] != source_maps(run, prepared["retrieval"]):
        raise ValueError("source_map_mismatch")
    locations = []
    diagnostics = []
    for item in reply.evidence:
        try:
            locations.append(locate(run, prepared["maps"], item))
            diagnostics.append({"declaration": item.model_dump(), "status": "located"})
        except ValueError as exc:
            diagnostics.append({"declaration": item.model_dump(), "status": str(exc)})
    if any(d["status"] != "located" for d in diagnostics):
        return {"valid": False, "failure": "citation_failed", "reply": reply.model_dump(),
                "locations": locations, "citation_diagnostics": diagnostics}
    materials = {m.material_id: m for m in run.materials}
    if len(set(reply.material_ids)) != len(reply.material_ids) or set(reply.material_ids) - materials.keys():
        raise ValueError("unknown_or_duplicate_material")
    pair = {"compliant": {"text_consistent"}, "deviation": {"text_deviation"},
            "not_applicable": {"condition_not_triggered"},
            "needs_review": {"condition_unknown", "material_unavailable", "absence_candidate"}}
    if reply.reason_code not in pair[reply.conclusion]:
        raise ValueError("conclusion_reason_mismatch")
    policy_ids = {e["document_id"] for e in locations if e["source_type"] == "demo_policy"}
    task_policies = {s.document_id for s in task.sources}
    if not task_policies <= policy_ids:
        raise ValueError("task_policy_evidence_required")
    for did in task_policies:
        allowed = [u for u in run.knowledge.units if any(s.unit_id == u.unit_id for s in task.sources)
                   and u.document_id == did]
        if not any(e["document_id"] == did and any(u.start <= e["start"] < e["end"] <= u.end for u in allowed)
                   for e in locations):
            raise ValueError("task_section_evidence_required")
    if reply.conclusion != "needs_review":
        if not any(e["source_type"] == "contract" for e in locations):
            raise ValueError("contract_fact_evidence_required")
        if prepared["retrieval"]["missing_task_dependencies"]:
            raise ValueError("required_policy_dependency_unreturned")
        if not set(task.required_policy_dependencies) <= policy_ids:
            raise ValueError("dependency_policy_evidence_required")
        if reply.conclusion != "not_applicable" and any(
                mid not in materials or materials[mid].status != "provided" for mid in task.required_materials):
            raise ValueError("required_material_facts_incomplete")
    if reply.reason_code == "material_unavailable":
        if not reply.material_ids or any(materials[mid].status not in {"missing", "unknown"} for mid in reply.material_ids):
            raise ValueError("material_basis_unverified")
    coverage_verified = False
    if reply.reason_code == "absence_candidate":
        contract_map = next(m for m in prepared["maps"] if m["source"]["source_type"] == "contract")
        expected = Coverage(document_id=run.contract.document_id, version=run.contract.version,
                            text_sha256=run.contract.text_sha256,
                            block_ids=[b["block_id"] for b in contract_map["blocks"]])
        if reply.coverage != expected:
            raise ValueError("incomplete_contract_coverage_claim")
        coverage_verified = True
    elif reply.coverage is not None:
        raise ValueError("unexpected_coverage")
    return {"valid": True, "failure": None, "reply": reply.model_dump(),
            "locations": locations, "citation_diagnostics": diagnostics,
            "reason": REASONS[reply.reason_code], "coverage_map_verified": coverage_verified,
            "decision_basis": {"task_applicability": task.applicability, "task_exceptions": task.exceptions,
                               "fact_citation_indexes": [i for i, e in enumerate(locations) if e["source_type"] in {"contract", "material"}],
                               "policy_citation_indexes": [i for i, e in enumerate(locations) if e["source_type"] == "demo_policy"]},
            "semantic_support": "pending_human_review", "manual_review_required": True}
