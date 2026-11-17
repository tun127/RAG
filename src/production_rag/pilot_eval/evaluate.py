"""Offline evaluation adapter for M3.2 pilot artifacts.

All inputs are JSON artifacts produced by the pilot CLI.  This module never
imports the pilot package, never calls a model, and never rewrites pilot output.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    DETERMINATE_CONCLUSIONS,
    EXECUTION_STATUSES,
    EXPECTED_POLICY_SCOPE,
    EXPECTED_TASK_IDS,
    PROTOCOL,
    SCHEMA,
    SUPPORTED_PROTOCOLS,
    ContractError,
    artifact_record,
    discover_execution_dirs,
    display_path,
    find_final_answer,
    find_prepared_input,
    is_hex64,
    load_cases_manifest,
    load_execution_input,
    load_final_answer,
    load_prepared_input,
    load_request,
    load_result,
    occurrences,
    parse_json_text,
    parse_reply,
    request_metrics,
    sha256_text,
    verify_maps,
)

PILOT_VALIDATION_TO_CITATION = {
    "located": "valid",
    "citation_not_found": "invalid",
    "citation_empty": "invalid",
    "citation_wrong_source_or_version": "invalid",
    "citation_fixed_text_mismatch": "invalid",
    "citation_ambiguous": "ambiguous",
    "citation_unknown_document": "missing",
    "citation_unknown_block": "missing",
}

BUSINESS_VALIDATION_FAILURES = {
    "citation_failed", "unknown_or_duplicate_material", "conclusion_reason_mismatch",
    "task_policy_evidence_required", "task_section_evidence_required",
    "contract_fact_evidence_required", "required_policy_dependency_unreturned",
    "dependency_policy_evidence_required", "required_material_facts_incomplete",
    "requested_material_not_referenced", "unexpected_material_request", "review_issue_required",
    "invalid_issue_evidence", "unexpected_review_issue", "task_section_body_evidence_required",
    "issue_body_evidence_required", "reply_schema_invalid",
    "presign_material_request_out_of_scope", "presign_trigger_unknown", "presign_condition_not_triggered",
    "presign_condition_triggered", "presign_base_task_applicable",
    "presign_conditional_requirement_unknown", "presign_required_material_incomplete",
    "material_basis_unverified", "incomplete_contract_coverage_claim", "unexpected_coverage",
}


TRANSPORT_LIKE_FAILURES = {
    "transport",
    "timeout",
    "authentication",
    "rate_limit",
    "server",
    "http_error",
    "business_error",
    "usage_unknown",
    "deadline",
    "budget_stop",
    "stopped_after_api_or_usage_failure",
}


def evaluate_pilot(
    *,
    run_root: Path,
    cases_manifest: Path,
    inputs_root: Path | None = None,
    final_answers_root: Path | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    run_root = Path(run_root)
    cases_manifest = Path(cases_manifest)
    cases = load_cases_manifest(cases_manifest)
    execution_dirs = discover_execution_dirs(run_root)

    executions = [
        _evaluate_execution(
            execution_dir=execution_dir,
            run_root=run_root,
            cases=cases,
            inputs_root=Path(inputs_root) if inputs_root else None,
            final_answers_root=Path(final_answers_root) if final_answers_root else None,
        )
        for execution_dir in execution_dirs
    ]
    executions.sort(
        key=lambda item: (
            item["case_id"] or "",
            item["mode"],
            item["execution_id"],
        )
    )
    aggregate = _aggregate(executions)
    environment = _overall_environment(executions)
    warnings = _warnings(executions, aggregate, environment)
    return {
        "schema": "pilot-evaluation-v2",
        "legacy_schema": SCHEMA,
        "title": title or "M3.2 pilot review evaluation",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "business_accuracy_measured": False,
        "semantic_support_measured": False,
        "run_root": str(run_root),
        "cases_manifest": str(cases_manifest),
        "scope": {
            "protocol": sorted({e["identity"]["config"].get("output_protocol", PROTOCOL) for e in executions}),
            "expected_task_ids": list(EXPECTED_TASK_IDS),
            "expected_policy_scope": list(EXPECTED_POLICY_SCOPE),
            "annotation_role": (
                "single-author development draft; not an independent test set, "
                "business accuracy baseline, or legal opinion"
            ),
            "classification_metric_label": (
                "agreement_with_single_author_draft_not_business_accuracy"
            ),
        },
        "inputs": {
            "cases_manifest": artifact_record(
                cases_manifest, "cases_manifest", cases_manifest.parent
            ),
            "annotations": [case["annotation_record"] for case in cases],
        },
        "executions": executions,
        "aggregate": aggregate,
        "warnings": warnings,
    }


def _evaluate_execution(
    *,
    execution_dir: Path,
    run_root: Path,
    cases: list[dict[str, Any]],
    inputs_root: Path | None,
    final_answers_root: Path | None,
) -> dict[str, Any]:
    execution_input_path = execution_dir / "execution-input.json"
    execution_input = load_execution_input(execution_input_path)
    run_id = execution_input["run_id"]
    artifacts: list[dict[str, Any]] = [
        artifact_record(execution_input_path, "execution_input", run_root)
    ]
    identity_checks: list[dict[str, Any]] = []

    prepared_input_path = find_prepared_input(execution_dir, run_id, inputs_root)
    prepared_input = None
    if prepared_input_path is not None:
        prepared_input = load_prepared_input(prepared_input_path)
        artifacts.append(artifact_record(prepared_input_path, "prepared_input", run_root))
        payload = prepared_input["payload"]
        _bind(
            identity_checks,
            "prepared_input_sha256",
            prepared_input["input_sha256"],
            execution_input["input_sha256"],
        )
        _bind(identity_checks, "prepared_run_id", payload["run_id"], run_id)
        _bind(
            identity_checks,
            "prepared_code_revision",
            payload["code_revision"],
            execution_input["code_revision"],
        )
        _bind(
            identity_checks,
            "prepared_knowledge_id",
            payload["knowledge_id"],
            execution_input["knowledge_id"],
        )

    slot_ids = [slot["task_id"] for slot in execution_input["slots"]]
    task_ids = list(EXPECTED_TASK_IDS) + sorted(set(slot_ids) - set(EXPECTED_TASK_IDS))

    requests: dict[str, dict[str, Any] | None] = {}
    results: dict[str, dict[str, Any] | None] = {}
    fallbacks: dict[str, dict[str, Any] | None] = {}
    request_paths: dict[str, Path] = {}
    result_paths: dict[str, Path] = {}
    fallback_paths: dict[str, Path] = {}
    for task_id in task_ids:
        request_path = execution_dir / f"{task_id}-request.json"
        result_path = execution_dir / f"{task_id}-result.json"
        fallback_path = find_final_answer(execution_dir, task_id, final_answers_root)
        request_paths[task_id] = request_path
        result_paths[task_id] = result_path
        if fallback_path is not None:
            fallback_paths[task_id] = fallback_path
        request = load_request(request_path) if request_path.is_file() else None
        result = load_result(result_path) if result_path.is_file() else None
        fallback = load_final_answer(fallback_path) if fallback_path is not None else None
        artifacts.append(artifact_record(request_path, f"request:{task_id}", run_root))
        artifacts.append(artifact_record(result_path, f"result:{task_id}", run_root))
        if fallback_path is not None:
            artifacts.append(
                artifact_record(fallback_path, f"final_answer:{task_id}", run_root)
            )
        if result is not None and result["task_id"] != task_id:
            raise ContractError(
                "result_task_identity_mismatch",
                path=result_path,
                detail=f"expected {task_id}, found {result['task_id']}",
            )
        if fallback is not None and fallback["task_id"] != task_id:
            raise ContractError(
                "final_answer_task_identity_mismatch",
                path=fallback_path,
                detail=f"expected {task_id}, found {fallback['task_id']}",
            )
        if request is not None:
            _check_request_identity(
                request=request,
                expected_task_id=task_id,
                knowledge_id=execution_input["knowledge_id"],
                path=request_path,
            )
        if request is not None:
            req_payload = parse_json_text(request["messages"][1]["content"])
            version = req_payload["protocol"]
            config = execution_input["config"]
            if config.get("output_protocol", PROTOCOL) != version:
                raise ContractError("execution_request_protocol_mismatch", path=request_path)
            if version == "pilot-review-block-v2":
                expected_prompt = "pilot-pay-acc-exc-protocol-v3"
                if req_payload.get("input_version") == "pilot-presign-input-v1":
                    if (not prepared_input or prepared_input["payload"].get("protocol") != "pilot-presign-input-v1"
                            or req_payload.get("checklist_version") != "pay-acc-exc-presign-1.0"):
                        raise ContractError("presign_input_binding_missing", path=request_path)
                    expected_prompt = "pilot-presign-v1"
                if (config.get("prompt_version") != expected_prompt
                        or request.get("prompt_version") != expected_prompt
                        or request.get("protocol") != version
                        or req_payload.get("prompt_version") != expected_prompt
                        or sha256_text(request["messages"][0]["content"]) != execution_input["prompt_sha256"]):
                    raise ContractError("execution_request_prompt_mismatch", path=request_path)
                if result and (result.get("output_protocol") != version or result.get("prompt_version") != expected_prompt):
                    raise ContractError("result_protocol_prompt_mismatch", path=result_path)
        requests[task_id] = request
        results[task_id] = result
        fallbacks[task_id] = fallback

    contract_sha = _resolve_contract_sha(
        prepared_input=prepared_input,
        requests=requests,
        request_paths=request_paths,
    )
    case = next(
        (item for item in cases if item["annotation"]["contract_sha256"] == contract_sha),
        None,
    )

    rows = []
    for task_id in task_ids:
        rows.append(
            _evaluate_task(
                task_id=task_id,
                slot_ids=slot_ids,
                request=requests[task_id],
                result=results[task_id],
                fallback=fallbacks[task_id],
                request_path=request_paths[task_id],
                result_path=result_paths[task_id],
                fallback_path=fallback_paths.get(task_id),
                knowledge_id=execution_input["knowledge_id"],
                case=case,
            )
        )

    metrics = _execution_metrics(rows)
    coverage = _execution_coverage(
        execution_input=execution_input,
        slot_ids=slot_ids,
        task_ids=task_ids,
        rows=rows,
        case=case,
        prepared_input_path=prepared_input_path,
        request_paths=request_paths,
        result_paths=result_paths,
        fallback_paths=fallback_paths,
    )
    return {
        "execution_id": execution_dir.name,
        "execution_path": display_path(execution_dir, run_root),
        "run_id": run_id,
        "mode": execution_input["config"]["mode"],
        "simulation": execution_input["simulation"],
        "environment": "SIMULATED" if execution_input["simulation"] else "LIVE",
        "case_id": case["case_id"] if case else None,
        "annotation": (
            {
                "case_id": case["case_id"],
                "status": case["annotation"]["status"],
                "contract_sha256": case["annotation"]["contract_sha256"],
                "scope": case["annotation"].get("scope"),
            }
            if case
            else None
        ),
        "contract_sha256": contract_sha,
        "identity": {
            "input_sha256": execution_input["input_sha256"],
            "knowledge_id": execution_input["knowledge_id"],
            "code_revision": execution_input["code_revision"],
            "execution_revision": execution_input["execution_revision"],
            "implementation_sha256": execution_input["implementation_sha256"],
            "prompt_sha256": execution_input["prompt_sha256"],
            "config": execution_input["config"],
            "checks": identity_checks,
        },
        "artifacts": artifacts,
        "tasks": rows,
        "metrics": metrics,
        "coverage": coverage,
    }


def _bind(checks: list[dict[str, Any]], name: str, actual: Any, expected: Any) -> None:
    match = actual == expected
    checks.append({"name": name, "actual": actual, "expected": expected, "match": match})
    if not match:
        raise ContractError(
            "artifact_identity_mismatch",
            detail=f"{name}: expected {expected!r}, found {actual!r}",
        )


def _check_request_identity(
    *, request: dict[str, Any], expected_task_id: str, knowledge_id: str, path: Path
) -> None:
    retrieval = request["retrieval"]
    if retrieval["snapshot_id"] != knowledge_id:
        raise ContractError(
            "request_knowledge_identity_mismatch",
            path=path,
            detail=f"expected {knowledge_id}, found {retrieval['snapshot_id']}",
        )
    messages = request["messages"]
    if not isinstance(messages[0], dict) or messages[0].get("role") != "system":
        raise ContractError("request_first_message_not_system", path=path)
    if not isinstance(messages[1], dict) or messages[1].get("role") != "user":
        raise ContractError("request_second_message_not_user", path=path)
    if not isinstance(messages[0].get("content"), str) or not isinstance(
        messages[1].get("content"), str
    ):
        raise ContractError("request_message_content_not_text", path=path)
    try:
        payload = parse_json_text(messages[1]["content"])
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        raise ContractError("request_user_payload_not_json_object", path=path)
    if payload.get("protocol") not in SUPPORTED_PROTOCOLS:
        raise ContractError(
            "request_protocol_mismatch",
            path=path,
            detail=str(payload.get("protocol")),
        )
    task = payload.get("task")
    if not isinstance(task, dict) or task.get("task_id") != expected_task_id:
        found = task.get("task_id") if isinstance(task, dict) else None
        raise ContractError(
            "request_task_identity_mismatch",
            path=path,
            detail=f"expected {expected_task_id}, found {found}",
        )


def _resolve_contract_sha(
    *,
    prepared_input: dict[str, Any] | None,
    requests: dict[str, dict[str, Any] | None],
    request_paths: dict[str, Path],
) -> str | None:
    prepared_sha = (
        prepared_input["payload"]["contract"]["raw_sha256"] if prepared_input else None
    )
    request_shas: set[str] = set()
    for task_id, request in requests.items():
        if request is None:
            continue
        contract_sources = [
            mapping["source"]
            for mapping in request["maps"]
            if isinstance(mapping, dict)
            and isinstance(mapping.get("source"), dict)
            and mapping["source"].get("source_type") == "contract"
        ]
        if len(contract_sources) != 1:
            raise ContractError(
                "request_contract_source_count_invalid",
                path=request_paths[task_id],
                detail=f"found {len(contract_sources)} contract sources",
            )
        request_shas.add(contract_sources[0].get("raw_sha256"))
    if not request_shas and prepared_sha is None:
        return None
    if len(request_shas) > 1:
        raise ContractError(
            "multiple_contract_hashes_in_requests", detail=", ".join(sorted(request_shas))
        )
    request_sha = next(iter(request_shas)) if request_shas else None
    if prepared_sha is not None and request_sha is not None and prepared_sha != request_sha:
        raise ContractError(
            "prepared_and_request_contract_identity_conflict",
            detail=f"prepared {prepared_sha}, request {request_sha}",
        )
    result = prepared_sha or request_sha
    if result is not None and not is_hex64(result):
        raise ContractError("invalid_contract_sha256", detail=str(result))
    return result


def _evaluate_task(
    *,
    task_id: str,
    slot_ids: list[str],
    request: dict[str, Any] | None,
    result: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
    request_path: Path,
    result_path: Path,
    fallback_path: Path | None,
    knowledge_id: str,
    case: dict[str, Any] | None,
) -> dict[str, Any]:
    request_check = (
        _check_request(request, task_id, knowledge_id) if request is not None else None
    )
    result_conclusion = result["business_conclusion"] if result is not None else None
    result_binding = _result_binding(request, result)
    request_ok = bool(
        request_check
        and request_check["ok"]
        and (result_binding is None or result_binding["matches"])
    )
    if not request_check or not request_check["ok"]:
        request_reason = "request_construction_failed"
    elif result_binding is not None and not result_binding["matches"]:
        request_reason = "result_request_binding_mismatch"
    else:
        request_reason = None
    attempts = (
        result["attempts"]
        if result is not None
        else (fallback["attempts"] if fallback is not None else [])
    )
    result_final_answer = result["final_answer"] if result is not None else None
    fallback_final_answer = fallback["final_text"] if fallback is not None else None
    fallback_used = result_final_answer is None and fallback_final_answer is not None
    final_answer_text = (
        result_final_answer if result_final_answer is not None else fallback_final_answer
    )
    final_answer_source = (
        "result.final_answer"
        if result_final_answer is not None
        else ("final-answers" if fallback_used else None)
    )

    if result is not None:
        execution_status = result["execution_status"]
        failure = result["failure"]
    elif fallback is not None:
        execution_status = "failed" if attempts else "not_executed"
        failure = fallback.get("error_kind") or (
            "result_missing_but_final_answer_present" if fallback_final_answer else "no_final_answer_unknown_cause"
        )
    else:
        execution_status = "not_executed"
        failure = "result_missing"

    attempted = bool(attempts) or execution_status in {"success", "failed"}
    attempt_summary = [_attempt_summary(item) for item in attempts]
    http_ok, http_reason, attempt_counts = _http_result(
        attempts=attempts,
        execution_status=execution_status,
        failure=failure,
        attempted=attempted,
    )

    reply, schema_errors = (None, [])
    if final_answer_text is not None:
        reply, schema_errors = parse_reply(final_answer_text,
            parse_json_text(request["messages"][1]["content"])["protocol"] if request else "unsupported")
    schema_ok = None if final_answer_text is None else not schema_errors
    validation = result["validation"] if result is not None else None
    pilot_validation_valid = validation.get("valid") if isinstance(validation, dict) else None
    recorded_reply = validation.get("reply") if isinstance(validation, dict) else None
    recorded_reply_matches = None
    if reply is not None and isinstance(recorded_reply, dict):
        recorded_reply_matches = reply == recorded_reply
    conclusion_matches = None
    if (
        reply is not None
        and validation is not None
        and pilot_validation_valid is True
        and result_conclusion is not None
    ):
        conclusion_matches = result_conclusion == reply.get("conclusion")
    structure_ok = schema_ok
    structure_reason = None
    if schema_ok is False:
        structure_reason = "reply_schema_invalid"
    if recorded_reply_matches is False:
        structure_ok = False
        structure_reason = "validation_reply_mismatch"
    if conclusion_matches is False:
        structure_ok = False
        structure_reason = "business_conclusion_mismatch"

    citation = _locate_citations(
        reply=reply,
        request=request,
        request_check=request_check,
        validation=validation,
    )

    program = _program_result(
        request_ok=request_ok,
        request_reason=request_reason,
        http_ok=http_ok,
        http_reason=http_reason,
        structure_ok=structure_ok,
        structure_reason=structure_reason,
        citation_ok=citation["ok"],
        citation_reason=citation["reason"],
        validation_valid=pilot_validation_valid,
        status_ok=(result["execution_status"] == "success" if result is not None else None),
        attempted=attempted,
        failure=failure,
    )

    annotation_draft = None
    annotation_task_missing = False
    if case is not None:
        drafts = case["annotation"]["task_drafts"]
        if task_id in drafts:
            annotation_draft = drafts[task_id]["candidate_for_review"]
        else:
            annotation_task_missing = True

    classification = _classify_task(
        task_id=task_id,
        result_present=result is not None,
        final_answer_text=final_answer_text,
        failure=failure,
        result_conclusion=result["business_conclusion"] if result is not None else None,
        reply_conclusion=reply.get("conclusion") if isinstance(reply, dict) else None,
        validation_valid=pilot_validation_valid,
        program_success=program["success"],
        program_reason=program["reason"],
        annotation_draft=annotation_draft,
        annotation_matched=case is not None,
        annotation_task_missing=annotation_task_missing,
    )
    review_outcome = _review_outcome(
        result_present=result is not None,
        final_answer_text=final_answer_text,
        attempts=attempts,
        execution_status=execution_status,
        failure=failure,
    )
    returned_policy_ids = (
        result["returned_policy_ids"]
        if result is not None
        else (request_check["returned_policy_ids"] if request_check else [])
    )
    request_policy_ids = (
        request_check["returned_policy_ids"] if request_check else []
    )
    missing_dependencies = sorted(
        set(result["missing_task_dependencies"] if result is not None else [])
        | set(request["retrieval"]["missing_task_dependencies"] if request is not None else [])
    )
    undeclared_materials = (
        result["undeclared_required_materials"] if result is not None else []
    )
    material_state = result.get("material_state") if result is not None else None

    return {
        "task_id": task_id,
        "slot_present": task_id in slot_ids,
        "request_present": request is not None,
        "result_present": result is not None,
        "final_answer_source": final_answer_source,
        "final_answer_fallback_used": fallback_used,
        "final_answer_fallback_path": (
            display_path(fallback_path, request_path.parent) if fallback_path is not None else None
        ),
        "execution_status": execution_status,
        "evaluation_v2": _task_facets(
            result, fallback, attempts, failure, schema_ok, schema_errors,
            pilot_validation_valid, request_ok, recorded_reply_matches, conclusion_matches,
            reply, request, classification, case, citation, fallback_used,
        ),
        "review_outcome": review_outcome,
        "failure": failure,
        "business_conclusion": result["business_conclusion"] if result is not None else None,
        "material_inventory_state": material_state,
        "reply_fields": ({k: reply.get(k) for k in
                          ("protocol", "material_refs", "requested_material_ids", "review_issue")}
                         if isinstance(reply, dict) and reply.get("protocol") == "pilot-review-block-v2"
                         else {"material_ids": reply.get("material_ids")} if isinstance(reply, dict) else None),
        "reply_conclusion": reply.get("conclusion") if isinstance(reply, dict) else None,
        "manual_review_required": (
            result["manual_review_required"] if result is not None else None
        ),
        "program": {
            "request": {
                "ok": request_ok,
                "reason": request_reason,
                "errors": request_check["errors"] if request_check else ["request_missing"],
                "recomputed": request_check["recomputed"] if request_check else None,
                "matches": request_check["matches"] if request_check else None,
                "result_binding": result_binding,
            },
            "http": {
                "ok": http_ok,
                "reason": http_reason,
                "attempted": attempted,
                "attempts": attempt_summary,
                "counts": attempt_counts,
            },
            "structure": {
                "ok": structure_ok,
                "reason": structure_reason,
                "errors": schema_errors,
                "pilot_validation_present": isinstance(validation, dict),
                "pilot_validation_valid": pilot_validation_valid,
                "pilot_validation_failure": (
                    validation.get("failure") if isinstance(validation, dict) else None
                ),
                "recorded_reply_matches_parsed": recorded_reply_matches,
                "business_conclusion_matches_reply": conclusion_matches,
                "execution_status_success": (
                    result["execution_status"] == "success" if result is not None else None
                ),
            },
            "citations": citation,
            "success": program["success"],
            "reason": program["reason"],
            "attempted": attempted,
        },
        "classification": classification,
        "evidence_support": {
            "manual_review_required": (
                result["manual_review_required"] if result is not None else None
            ),
            "semantic_support": (
                validation.get("semantic_support") if isinstance(validation, dict) else None
            ),
            "human_review_status": (
                "pending"
                if result is not None and result["manual_review_required"] is True
                else ("not_required_reported" if result is not None else "unknown")
            ),
            "automated_semantic_judgement": "not_performed",
            "located_citations_do_not_imply_support": True,
        },
        "coverage": {
            "slot_present": task_id in slot_ids,
            "request_present": request is not None,
            "result_present": result is not None,
            "final_answer_fallback_present": fallback is not None,
            "request_bytes": request["request_bytes"] if request is not None else None,
            "input_characters": (
                request["input_characters"] if request is not None else None
            ),
            "returned_policy_ids": returned_policy_ids,
            "request_policy_ids": request_policy_ids,
            "missing_task_dependencies": missing_dependencies,
            "missing_primary_policies": (
                request["retrieval"]["missing_primary_policies"]
                if request is not None
                else []
            ),
            "undeclared_required_materials": undeclared_materials,
            "material_state_counts": _count_material_states(material_state),
            "review_outcome": review_outcome,
            "failure": failure,
            "annotation_task_missing": annotation_task_missing,
        },
    }


def _result_binding(
    request: dict[str, Any] | None, result: dict[str, Any] | None
) -> dict[str, Any] | None:
    if result is None:
        return None
    if request is None:
        return {
            "matches": False,
            "reason": "request_missing",
            "fields": {
                "request_sha256": None,
                "request_bytes": None,
                "input_characters": None,
            },
        }
    fields = {
        "request_sha256": result["request_sha256"] == request["request_sha256"],
        "request_bytes": result["request_bytes"] == request["request_bytes"],
        "input_characters": result["input_characters"] == request["input_characters"],
    }
    return {
        "matches": all(fields.values()),
        "reason": None if all(fields.values()) else "result_request_binding_mismatch",
        "fields": fields,
    }


def _check_request(
    request: dict[str, Any], expected_task_id: str, knowledge_id: str
) -> dict[str, Any]:
    errors: list[str] = []
    messages = request["messages"]
    if not isinstance(messages[0], dict) or messages[0].get("role") != "system":
        errors.append("first_message_not_system")
    if not isinstance(messages[1], dict) or messages[1].get("role") != "user":
        errors.append("second_message_not_user")
    payload = None
    if not errors:
        try:
            payload = parse_json_text(messages[1]["content"])
        except (ValueError, TypeError) as exc:
            errors.append(f"user_payload_invalid:{exc}")
        if not isinstance(payload, dict):
            errors.append("user_payload_not_object")
    if isinstance(payload, dict):
        if payload.get("protocol") not in SUPPORTED_PROTOCOLS:
            errors.append("protocol_mismatch")
        task = payload.get("task")
        if not isinstance(task, dict) or task.get("task_id") != expected_task_id:
            errors.append("task_identity_mismatch")

    maps_index, map_errors = verify_maps(request["maps"])
    errors.extend(map_errors)

    retrieval = request["retrieval"]
    if retrieval["snapshot_id"] != knowledge_id:
        errors.append("knowledge_identity_mismatch")
    for index, context in enumerate(retrieval["contexts"]):
        if not isinstance(context, dict):
            errors.append(f"retrieval_contexts[{index}]_not_object")
            continue
        document_id = context.get("document_id")
        if document_id not in maps_index:
            errors.append(f"retrieval_contexts[{index}]_document_not_in_maps")
            continue
        if context.get("text_sha256") != maps_index[document_id]["source"]["text_sha256"]:
            errors.append(f"retrieval_contexts[{index}]_text_sha256_mismatch")

    try:
        recomputed_sha, recomputed_bytes, recomputed_characters = request_metrics(messages)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        errors.append(f"request_metrics_failed:{type(exc).__name__}")
        recomputed_sha, recomputed_bytes, recomputed_characters = None, None, None
    matches = {
        "request_sha256": request["request_sha256"] == recomputed_sha,
        "request_bytes": request["request_bytes"] == recomputed_bytes,
        "input_characters": request["input_characters"] == recomputed_characters,
    }
    if not matches["request_sha256"]:
        errors.append("request_sha256_mismatch")
    if not matches["request_bytes"]:
        errors.append("request_bytes_mismatch")
    if not matches["input_characters"]:
        errors.append("input_characters_mismatch")

    returned_policy_ids = []
    if isinstance(payload, dict):
        documents = payload.get("demo_policy_documents")
        if isinstance(documents, list):
            returned_policy_ids = [
                item.get("document_id")
                for item in documents
                if isinstance(item, dict) and isinstance(item.get("document_id"), str)
            ]
    return {
        "ok": not errors,
        "errors": errors,
        "maps_index": maps_index,
        "maps_errors": map_errors,
        "payload": payload,
        "recomputed": {
            "request_sha256": recomputed_sha,
            "request_bytes": recomputed_bytes,
            "input_characters": recomputed_characters,
        },
        "matches": matches,
        "returned_policy_ids": returned_policy_ids,
    }


def _http_result(
    *,
    attempts: list[dict[str, Any]],
    execution_status: str,
    failure: Any,
    attempted: bool,
) -> tuple[bool | None, str | None, dict[str, Any]]:
    statuses = [
        item.get("http_status") if isinstance(item, dict) else None for item in attempts
    ]
    counts = {
        "attempts": len(attempts),
        "http_200": sum(status == 200 for status in statuses),
        "http_non_200": sum(status not in (None, 200) for status in statuses),
        "http_missing": sum(status is None for status in statuses),
    }
    if not attempts:
        if execution_status == "not_executed":
            return None, failure or "not_attempted", counts
        return False, "attempts_missing", counts
    if all(status == 200 for status in statuses):
        return True, None, counts
    if any(status is None for status in statuses):
        return False, "transport_no_http_status", counts
    return False, "http_non_200_response", counts


def _attempt_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(attempt, dict):
        return {"invalid_attempt_record": True}
    return {
        "attempt": attempt.get("attempt"),
        "http_status": attempt.get("http_status"),
        "finish_reason": attempt.get("finish_reason"),
        "usage_known": attempt.get("usage_known"),
        "usage": attempt.get("usage"),
        "elapsed_seconds": attempt.get("elapsed_seconds"),
    }


def _locate_citations(
    *,
    reply: dict[str, Any] | None,
    request: dict[str, Any] | None,
    request_check: dict[str, Any] | None,
    validation: dict[str, Any] | None,
) -> dict[str, Any]:
    empty = {
        "ok": None,
        "reason": "reply_unavailable",
        "declared": 0,
        "valid": 0,
        "invalid": 0,
        "ambiguous": 0,
        "missing": 0,
        "duplicate_declarations": 0,
        "rate": None,
        "rate_null_reason": "no_declared_citations",
        "diagnostics": [],
        "duplicate_items": [],
        "pilot_crosscheck": {"comparable": False, "reason": "reply_unavailable"},
    }
    if reply is None:
        return empty
    if request is None or request_check is None:
        return {**empty, "reason": "request_unavailable"}
    if request_check["maps_errors"]:
        return {**empty, "reason": "request_maps_invalid"}
    maps_index = request_check["maps_index"]
    diagnostics: list[dict[str, Any]] = []
    counts = {"valid": 0, "invalid": 0, "ambiguous": 0, "missing": 0}
    seen: dict[tuple[Any, ...], int] = {}
    duplicates: list[dict[str, Any]] = []
    evidence = reply.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            counts["invalid"] += 1
            diagnostics.append(
                {
                    "index": index,
                    "status": "invalid",
                    "reason": "evidence_not_object",
                }
            )
            continue
        key = (
            item.get("source_type"),
            item.get("document_id"),
            item.get("version"),
            item.get("block_id"),
            item.get("quote"),
        )
        if key in seen:
            duplicates.append({"index": index, "first_index": seen[key]})
        else:
            seen[key] = index
        result = _locate_one(item, maps_index)
        counts[result["status"]] += 1
        diagnostics.append(
            {
                "index": index,
                "source_type": item.get("source_type"),
                "document_id": item.get("document_id"),
                "version": item.get("version"),
                "block_id": item.get("block_id"),
                "quote": item.get("quote"),
                "status": result["status"],
                "reason": result["reason"],
                "start": result.get("start"),
                "end": result.get("end"),
            }
        )
    declared = sum(counts.values())
    rate = counts["valid"] / declared if declared else None
    crosscheck = _pilot_citation_crosscheck(validation, diagnostics)
    ok = None
    reason = None
    if declared == 0:
        ok = None
        reason = "no_citations_declared"
    elif counts["invalid"] or counts["ambiguous"] or counts["missing"]:
        ok = False
        reason = "citation_location_failures"
    else:
        ok = True
    return {
        "ok": ok,
        "reason": reason,
        "declared": declared,
        "valid": counts["valid"],
        "invalid": counts["invalid"],
        "ambiguous": counts["ambiguous"],
        "missing": counts["missing"],
        "duplicate_declarations": len(duplicates),
        "rate": rate,
        "rate_null_reason": None if declared else "no_declared_citations",
        "diagnostics": diagnostics,
        "duplicate_items": duplicates,
        "pilot_crosscheck": crosscheck,
    }


def _locate_one(
    item: dict[str, Any], maps_index: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    document_id = item.get("document_id")
    if document_id not in maps_index:
        return {"status": "missing", "reason": "document_not_returned"}
    mapped = maps_index[document_id]
    source = mapped["source"]
    if item.get("source_type") != source["source_type"] or item.get("version") != source["version"]:
        return {"status": "invalid", "reason": "wrong_source_or_version"}
    block = mapped["blocks_by_id"].get(item.get("block_id"))
    if block is None:
        return {"status": "missing", "reason": "block_not_returned"}
    quote = item.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return {"status": "invalid", "reason": "empty_quote"}
    positions = occurrences(block["text"], quote)
    if not positions:
        return {"status": "invalid", "reason": "quote_not_found_in_block"}
    if len(positions) > 1:
        return {"status": "ambiguous", "reason": "quote_multiple_in_block"}
    start = block["start"] + positions[0]
    end = start + len(quote)
    if source["text"][start:end] != quote:
        return {"status": "invalid", "reason": "source_slice_mismatch"}
    return {"status": "valid", "reason": None, "start": start, "end": end}


def _pilot_citation_crosscheck(
    validation: dict[str, Any] | None, diagnostics: list[dict[str, Any]]
) -> dict[str, Any]:
    pilot_diagnostics = (
        validation.get("citation_diagnostics") if isinstance(validation, dict) else None
    )
    if not isinstance(pilot_diagnostics, list):
        return {"comparable": False, "reason": "pilot_diagnostics_missing"}
    if len(pilot_diagnostics) != len(diagnostics):
        return {
            "comparable": False,
            "reason": "pilot_diagnostics_length_mismatch",
            "pilot_count": len(pilot_diagnostics),
            "evaluator_count": len(diagnostics),
        }
    mismatches = []
    for index, (pilot_item, evaluator_item) in enumerate(
        zip(pilot_diagnostics, diagnostics, strict=True)
    ):
        pilot_status = pilot_item.get("status") if isinstance(pilot_item, dict) else None
        expected = PILOT_VALIDATION_TO_CITATION.get(pilot_status)
        if expected != evaluator_item["status"]:
            mismatches.append(
                {
                    "index": index,
                    "pilot_status": pilot_status,
                    "evaluator_status": evaluator_item["status"],
                }
            )
    return {
        "comparable": True,
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
    }


def _program_result(
    *,
    request_ok: bool,
    request_reason: str | None,
    http_ok: bool | None,
    http_reason: str | None,
    structure_ok: bool | None,
    structure_reason: str | None,
    citation_ok: bool | None,
    citation_reason: str | None,
    validation_valid: bool | None,
    status_ok: bool | None,
    attempted: bool,
    failure: Any,
) -> dict[str, Any]:
    if not attempted:
        return {"success": None, "reason": f"not_executed:{failure}"}
    checks = [
        ("request", request_ok, request_reason),
        ("http", http_ok, http_reason),
        ("structure", structure_ok, structure_reason),
        (
            "citations",
            citation_ok,
            citation_reason,
        ),
        ("pilot_validation", validation_valid is True, "pilot_validation_not_valid"),
        ("execution_status", status_ok, "execution_status_not_success"),
    ]
    failed = [name for name, value, _ in checks if value is False]
    unavailable = [name for name, value, _ in checks if value is None]
    if failed or unavailable:
        reasons = [
            f"{name}:{reason}"
            for name, value, reason in checks
            if value is not True and reason is not None
        ]
        return {"success": False, "reason": ";".join(reasons) or "program_stage_failed"}
    return {"success": True, "reason": None}


def _classify_task(
    *,
    task_id: str,
    result_present: bool,
    final_answer_text: str | None,
    failure: Any,
    result_conclusion: Any,
    reply_conclusion: Any,
    validation_valid: bool | None,
    program_success: bool | None,
    program_reason: str | None,
    annotation_draft: str | None,
    annotation_matched: bool,
    annotation_task_missing: bool,
) -> dict[str, Any]:
    base = {
        "task_id": task_id,
        "annotation_draft": annotation_draft,
        "annotation_matched": annotation_matched,
        "annotation_task_missing": annotation_task_missing,
        "result_conclusion": result_conclusion,
        "reply_conclusion": reply_conclusion,
        "validation_valid": validation_valid,
        "program_success": program_success,
        "included_in_accuracy": False,
        "included_in_draft_comparison": False,
        "agrees_with_annotation": None,
        "bucket": None,
        "note": None,
    }
    if final_answer_text is None:
        return {**base, "bucket": "not_executed", "note": failure or "no_final_answer"}
    if not result_present:
        return {
            **base,
            "bucket": "failed",
            "note": "result_missing_final_answer_unvalidated",
        }
    if program_success is not True or validation_valid is not True:
        return {
            **base,
            "bucket": "failed",
            "note": program_reason or failure or "result_not_validated",
        }
    if result_conclusion == "needs_review":
        return {**base, "bucket": "needs_review", "note": "excluded_from_accuracy"}
    if result_conclusion in DETERMINATE_CONCLUSIONS:
        if not annotation_matched:
            return {
                **base,
                "bucket": "no_annotation",
                "note": "no_annotation_contract_sha256_match",
            }
        if annotation_task_missing or annotation_draft is None:
            return {
                **base,
                "bucket": "no_annotation",
                "note": "annotation_task_missing",
            }
        if annotation_draft == "needs_review":
            return {
                **base,
                "bucket": "annotation_needs_review_excluded",
                "note": "annotation_draft_is_needs_review",
            }
        return {
            **base,
            "bucket": "determinate_pair",
            "included_in_accuracy": False,
            "included_in_draft_comparison": True,
            "agrees_with_annotation": result_conclusion == annotation_draft,
            "note": "single_author_draft_comparison_not_business_accuracy",
        }
    return {
        **base,
        "bucket": "failed",
        "note": f"conclusion_not_classifiable:{result_conclusion!r}",
    }


def _review_outcome(
    *,
    result_present: bool,
    final_answer_text: str | None,
    attempts: list[dict[str, Any]],
    execution_status: str,
    failure: Any,
) -> str:
    if result_present and execution_status == "success" and final_answer_text is not None:
        return "success"
    if failure == "truncated" or any(a.get("finish_reason") == "length" for a in attempts):
        return "truncated"
    if failure in BUSINESS_VALIDATION_FAILURES:
        return "business_validation_failure"
    if final_answer_text is not None:
        return "unvalidated_final_answer"
    if failure == "truncated" or any(a.get("finish_reason") == "length" for a in attempts):
        return "truncated"
    if any(isinstance(a.get("http_status"), int) and a["http_status"] >= 400 for a in attempts):
        return "http_failure"
    if failure in {"transport", "timeout"}:
        return "transport_failure"
    if attempts:
        return "no_final_answer_unknown_cause"
    if result_present and execution_status == "not_executed":
        return "not_executed"
    if not result_present:
        return "not_executed_no_result_artifact"
    return "failed_no_final_answer"


def _count_material_states(material_state: Any) -> dict[str, int] | None:
    if not isinstance(material_state, list):
        return None
    counts: dict[str, int] = {}
    for item in material_state:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
    return counts


def _execution_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    request_true = sum(row["program"]["request"]["ok"] is True for row in rows)
    http_true = sum(row["program"]["http"]["ok"] is True for row in rows)
    http_false = sum(row["program"]["http"]["ok"] is False for row in rows)
    structure_true = sum(row["program"]["structure"]["ok"] is True for row in rows)
    structure_false = sum(row["program"]["structure"]["ok"] is False for row in rows)
    citation_pass = sum(row["program"]["citations"]["ok"] is True for row in rows)
    citation_fail = sum(row["program"]["citations"]["ok"] is False for row in rows)
    success_true = sum(row["program"]["success"] is True for row in rows)
    success_false = sum(row["program"]["success"] is False for row in rows)
    success_null = sum(row["program"]["success"] is None for row in rows)
    attempted = http_true + http_false
    validation_present = sum(
        row["program"]["structure"]["pilot_validation_present"] for row in rows
    )
    validation_valid = sum(
        row["program"]["structure"]["pilot_validation_valid"] is True for row in rows
    )
    citation_totals = _citation_totals(rows)
    metrics = {
        "slots": total,
        "request_ok": _metric("request_ok", request_true, total, "no_slots"),
        "http_ok": _metric("http_ok", http_true, attempted, "no_attempted_tasks"),
        "structure_ok": _metric(
            "structure_ok",
            structure_true,
            structure_true + structure_false,
            "no_final_answers_to_parse",
        ),
        "citations_ok": _metric(
            "citations_ok",
            citation_pass,
            citation_pass + citation_fail,
            "no_parsed_evidence_to_locate",
        ),
        "program_success_all_slots": _metric(
            "program_success_all_slots", success_true, total, "no_slots"
        ),
        "program_success_attempted": _metric(
            "program_success_attempted",
            success_true,
            attempted,
            "no_attempted_tasks",
        ),
    }
    return {
        "slots": total,
        "attempted": attempted,
        "request_ok_count": request_true,
        "request_false_count": total - request_true,
        "http_true_count": http_true,
        "http_false_count": http_false,
        "http_null_count": total - attempted,
        "structure_true_count": structure_true,
        "structure_false_count": structure_false,
        "structure_null_count": total - structure_true - structure_false,
        "citation_pass_count": citation_pass,
        "citation_fail_count": citation_fail,
        "citation_null_count": total - citation_pass - citation_fail,
        "program_success_count": success_true,
        "program_failure_count": success_false,
        "program_not_executed_count": success_null,
        "pilot_validation_present_count": validation_present,
        "pilot_validation_valid_count": validation_valid,
        "citation_totals": citation_totals,
        "metrics": metrics,
        "status_counts": {
            status: sum(row["execution_status"] == status for row in rows)
            for status in EXECUTION_STATUSES
        },
    }


def _citation_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "declared",
        "valid",
        "invalid",
        "ambiguous",
        "missing",
        "duplicate_declarations",
    )
    totals = {key: sum(row["program"]["citations"][key] for row in rows) for key in keys}
    totals["rate"] = (
        totals["valid"] / totals["declared"] if totals["declared"] else None
    )
    totals["rate_null_reason"] = (
        None if totals["declared"] else "no_declared_citations"
    )
    return totals


def _execution_coverage(
    *,
    execution_input: dict[str, Any],
    slot_ids: list[str],
    task_ids: list[str],
    rows: list[dict[str, Any]],
    case: dict[str, Any] | None,
    prepared_input_path: Path | None,
    request_paths: dict[str, Path],
    result_paths: dict[str, Path],
    fallback_paths: dict[str, Path],
) -> dict[str, Any]:
    returned_policy_ids = sorted(
        {
            policy_id
            for row in rows
            for policy_id in row["coverage"]["returned_policy_ids"]
            if isinstance(policy_id, str)
        }
    )
    request_policy_ids = sorted(
        {
            policy_id
            for row in rows
            for policy_id in row["coverage"]["request_policy_ids"]
            if isinstance(policy_id, str)
        }
    )
    policy_scope_ids = sorted(set(returned_policy_ids) | set(request_policy_ids))
    missing_dependencies = sorted(
        {
            dependency
            for row in rows
            for dependency in row["coverage"]["missing_task_dependencies"]
            if isinstance(dependency, str)
        }
    )
    missing_primary = sorted(
        {
            policy_id
            for row in rows
            for policy_id in row["coverage"]["missing_primary_policies"]
            if isinstance(policy_id, str)
        }
    )
    undeclared_materials = sorted(
        {
            material
            for row in rows
            for material in row["coverage"]["undeclared_required_materials"]
            if isinstance(material, str)
        }
    )
    unexecuted = [
        {
            "task_id": row["task_id"],
            "failure": row["failure"],
            "review_outcome": row["review_outcome"],
        }
        for row in rows
        if row["program"]["success"] is None
    ]
    failed = [
        {
            "task_id": row["task_id"],
            "failure": row["failure"],
            "review_outcome": row["review_outcome"],
        }
        for row in rows
        if row["program"]["success"] is False
    ]
    slot_counts = {task_id: slot_ids.count(task_id) for task_id in set(slot_ids)}
    return {
        "case_id": case["case_id"] if case else None,
        "annotation_status": case["annotation"]["status"] if case else None,
        "contract_sha256": (
            case["annotation"]["contract_sha256"] if case else None
        ),
        "mode": execution_input["config"]["mode"],
        "simulation": execution_input["simulation"],
        "expected_task_ids": list(EXPECTED_TASK_IDS),
        "slot_task_ids": slot_ids,
        "evaluated_task_ids": task_ids,
        "missing_expected_slots": [
            task_id for task_id in EXPECTED_TASK_IDS if task_id not in slot_ids
        ],
        "extra_slots": [task_id for task_id in slot_ids if task_id not in EXPECTED_TASK_IDS],
        "duplicate_slots": sorted(
            task_id for task_id, count in slot_counts.items() if count > 1
        ),
        "request_files": sum(path.is_file() for path in request_paths.values()),
        "result_files": sum(path.is_file() for path in result_paths.values()),
        "final_answer_fallback_files": len(fallback_paths),
        "prepared_input_present": prepared_input_path is not None,
        "policy_scope_expected": list(EXPECTED_POLICY_SCOPE),
        "returned_policy_ids": returned_policy_ids,
        "request_policy_ids": request_policy_ids,
        "out_of_scope_policy_ids": [
            policy_id
            for policy_id in policy_scope_ids
            if policy_id not in EXPECTED_POLICY_SCOPE
        ],
        "missing_primary_policies": missing_primary,
        "missing_task_dependencies": missing_dependencies,
        "undeclared_required_materials": undeclared_materials,
        "unexecuted_items": unexecuted,
        "failed_items": failed,
        "task_rows": [
            {
                "task_id": row["task_id"],
                "review_outcome": row["review_outcome"],
                "failure": row["failure"],
                "returned_policy_ids": row["coverage"]["returned_policy_ids"],
                "request_policy_ids": row["coverage"]["request_policy_ids"],
                "missing_task_dependencies": row["coverage"]["missing_task_dependencies"],
                "missing_primary_policies": row["coverage"]["missing_primary_policies"],
                "undeclared_required_materials": row["coverage"][
                    "undeclared_required_materials"
                ],
                "material_state_counts": row["coverage"]["material_state_counts"],
            }
            for row in rows
        ],
    }


def _aggregate(executions: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        {**row, "execution_id": execution["execution_id"], "mode": execution["mode"],
         "case_id": execution["case_id"], "simulation": execution["simulation"],
         "environment": execution["environment"]}
        for execution in executions
        for row in execution["tasks"]
    ]
    total_slots = len(rows)
    request_true = sum(row["program"]["request"]["ok"] is True for row in rows)
    http_true = sum(row["program"]["http"]["ok"] is True for row in rows)
    http_false = sum(row["program"]["http"]["ok"] is False for row in rows)
    structure_true = sum(row["program"]["structure"]["ok"] is True for row in rows)
    structure_false = sum(row["program"]["structure"]["ok"] is False for row in rows)
    citation_pass = sum(row["program"]["citations"]["ok"] is True for row in rows)
    citation_fail = sum(row["program"]["citations"]["ok"] is False for row in rows)
    success_true = sum(row["program"]["success"] is True for row in rows)
    success_false = sum(row["program"]["success"] is False for row in rows)
    success_null = sum(row["program"]["success"] is None for row in rows)
    attempted = http_true + http_false
    citation_totals = _citation_totals(rows)
    program_metrics = {
        "request_ok": _metric("request_ok", request_true, total_slots, "no_slots"),
        "http_ok": _metric("http_ok", http_true, attempted, "no_attempted_tasks"),
        "structure_ok": _metric(
            "structure_ok",
            structure_true,
            structure_true + structure_false,
            "no_final_answers_to_parse",
        ),
        "citations_ok": _metric(
            "citations_ok",
            citation_pass,
            citation_pass + citation_fail,
            "no_parsed_evidence_to_locate",
        ),
        "program_success_all_slots": _metric(
            "program_success_all_slots", success_true, total_slots, "no_slots"
        ),
        "program_success_attempted": _metric(
            "program_success_attempted",
            success_true,
            attempted,
            "no_attempted_tasks",
        ),
    }
    citation_metric = _metric(
        "citation_unique_verbatim_rate",
        citation_totals["valid"],
        citation_totals["declared"],
        "no_declared_citations",
    )
    classification = _classification_aggregate(rows)
    evidence_support = _evidence_support_aggregate(rows)
    coverage = _coverage_aggregate(executions, rows)
    denominators = []
    for metrics in (
        program_metrics,
        {"citation_unique_verbatim_rate": citation_metric},
        classification["metrics"],
        evidence_support["metrics"],
        coverage["metrics"],
    ):
        denominators.extend(metrics.values())
    return {
        "executions": len(executions),
        "slots": total_slots,
        "attempted": attempted,
        "program": {
            "request_ok_count": request_true,
            "request_false_count": total_slots - request_true,
            "http_true_count": http_true,
            "http_false_count": http_false,
            "http_null_count": total_slots - attempted,
            "structure_true_count": structure_true,
            "structure_false_count": structure_false,
            "structure_null_count": total_slots - structure_true - structure_false,
            "citation_pass_count": citation_pass,
            "citation_fail_count": citation_fail,
            "citation_null_count": total_slots - citation_pass - citation_fail,
            "program_success_count": success_true,
            "program_failure_count": success_false,
            "program_not_executed_count": success_null,
            "metrics": program_metrics,
            "status_counts": {
                status: sum(
                    row["execution_status"] == status for row in rows
                )
                for status in EXECUTION_STATUSES
            },
        },
        "citation_precision": {
            **citation_totals,
            "metric": citation_metric,
            "by_source_type": _citation_by_source_type(rows),
        },
        "classification": classification,
        "evaluation_v2": _facet_totals(rows),
        "evidence_support": evidence_support,
        "coverage": coverage,
        "denominators": denominators,
    }


def _classification_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    bucket_counts: dict[str, int] = {}
    for row in rows:
        bucket = row["classification"]["bucket"]
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    included = [row for row in rows if row["classification"]["included_in_draft_comparison"]]
    agreed = sum(
        row["classification"]["agrees_with_annotation"] is True for row in included
    )
    metric = _metric(
        "classification_agreement_with_single_author_draft",
        agreed,
        len(included),
        "no_determinate_result_and_determinate_annotation_pairs",
    )
    by_mode: dict[str, Any] = {}
    for mode in ("all", "rag"):
        mode_rows = [row for row in rows if row["mode"] == mode]
        mode_included = [
            row for row in mode_rows if row["classification"]["included_in_draft_comparison"]
        ]
        mode_agreed = sum(
            row["classification"]["agrees_with_annotation"] is True
            for row in mode_included
        )
        by_mode[mode] = {
            "slots": len(mode_rows),
            "buckets": _bucket_counts(mode_rows),
            "metric": _metric(
                f"classification_agreement_{mode}",
                mode_agreed,
                len(mode_included),
                "no_determinate_result_and_determinate_annotation_pairs",
            ),
        }
    by_case: dict[str, Any] = {}
    for case_id in sorted({row["case_id"] for row in rows if row["case_id"]}):
        case_rows = [row for row in rows if row["case_id"] == case_id]
        case_included = [
            row for row in case_rows if row["classification"]["included_in_draft_comparison"]
        ]
        case_agreed = sum(
            row["classification"]["agrees_with_annotation"] is True
            for row in case_included
        )
        by_case[case_id] = {
            "slots": len(case_rows),
            "buckets": _bucket_counts(case_rows),
            "metric": _metric(
                f"classification_agreement_{case_id}",
                case_agreed,
                len(case_included),
                "no_determinate_result_and_determinate_annotation_pairs",
            ),
        }
    return {
        "business_accuracy_measured": False,
        "metric_label": (
            "agreement_with_single_author_draft_not_business_accuracy"
        ),
        "bucket_counts": bucket_counts,
        "determinate_pairs": len(included),
        "agreements": agreed,
        "metric": metric,
        "metrics": {"classification_agreement": metric},
        "by_mode": by_mode,
        "by_case": by_case,
        "needs_review": _classification_items(rows, "needs_review"),
        "not_executed": _classification_items(rows, "not_executed"),
        "failed": _classification_items(rows, "failed"),
        "annotation_needs_review_excluded": _classification_items(
            rows, "annotation_needs_review_excluded"
        ),
        "no_annotation": _classification_items(rows, "no_annotation"),
    }


def _bucket_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        bucket = row["classification"]["bucket"]
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _classification_items(rows: list[dict[str, Any]], bucket: str) -> list[dict[str, Any]]:
    return [
        {
            "execution_id": row["execution_id"],
            "case_id": row["case_id"],
            "mode": row["mode"],
            "task_id": row["task_id"],
            "annotation_draft": row["classification"]["annotation_draft"],
            "result_conclusion": row["classification"]["result_conclusion"],
            "note": row["classification"]["note"],
        }
        for row in rows
        if row["classification"]["bucket"] == bucket
    ]


def _citation_by_source_type(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        for item in row["program"]["citations"]["diagnostics"]:
            source_type = item.get("source_type") or "unknown"
            bucket = totals.setdefault(
                source_type, {"declared": 0, "valid": 0, "invalid": 0, "ambiguous": 0,
                              "missing": 0}
            )
            bucket["declared"] += 1
            status = item.get("status")
            if status in bucket:
                bucket[status] += 1
    return totals


def _evidence_support_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    review_rows = [row for row in rows if row["result_present"]]
    required = sum(
        row["evidence_support"]["manual_review_required"] is True for row in review_rows
    )
    not_required = sum(
        row["evidence_support"]["manual_review_required"] is False for row in review_rows
    )
    unknown = len(review_rows) - required - not_required
    support_counts: dict[str, int] = {}
    for row in rows:
        status = row["evidence_support"]["semantic_support"]
        key = status if isinstance(status, str) else "unknown"
        support_counts[key] = support_counts.get(key, 0) + 1
    metric = _metric(
        "manual_review_required_rate",
        required,
        len(review_rows),
        "no_review_results",
    )
    return {
        "semantic_support_measured": False,
        "automated_semantic_judgement": "not_performed",
        "located_citations_do_not_imply_support": True,
        "review_result_rows": len(review_rows),
        "manual_review_required_true": required,
        "manual_review_required_false": not_required,
        "manual_review_required_unknown": unknown,
        "semantic_support_status_counts": support_counts,
        "metric": metric,
        "metrics": {"manual_review_required_rate": metric},
        "rows": [
            {
                "execution_id": row["execution_id"],
                "task_id": row["task_id"],
                "manual_review_required": row["evidence_support"][
                    "manual_review_required"
                ],
                "semantic_support": row["evidence_support"]["semantic_support"],
                "human_review_status": row["evidence_support"]["human_review_status"],
                "located_citations": row["program"]["citations"]["valid"],
            }
            for row in rows
        ],
    }


def _coverage_aggregate(
    executions: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    expected = len(executions) * len(EXPECTED_TASK_IDS)
    request_files = sum(execution["coverage"]["request_files"] for execution in executions)
    result_files = sum(execution["coverage"]["result_files"] for execution in executions)
    fallback_files = sum(
        execution["coverage"]["final_answer_fallback_files"] for execution in executions
    )
    missing_slots = sum(
        len(execution["coverage"]["missing_expected_slots"]) for execution in executions
    )
    extra_slots = sum(
        len(execution["coverage"]["extra_slots"]) for execution in executions
    )
    unexecuted = [
        {
            "execution_id": execution["execution_id"],
            "case_id": execution["case_id"],
            "mode": execution["mode"],
            **item,
        }
        for execution in executions
        for item in execution["coverage"]["unexecuted_items"]
    ]
    failed = [
        {
            "execution_id": execution["execution_id"],
            "case_id": execution["case_id"],
            "mode": execution["mode"],
            **item,
        }
        for execution in executions
        for item in execution["coverage"]["failed_items"]
    ]
    missing_dependencies = [
        {
            "execution_id": execution["execution_id"],
            "case_id": execution["case_id"],
            "mode": execution["mode"],
            "task_id": item["task_id"],
            "dependency": dependency,
        }
        for execution in executions
        for item in execution["coverage"]["task_rows"]
        for dependency in item["missing_task_dependencies"]
    ]
    out_of_scope = sorted(
        {
            policy_id
            for execution in executions
            for policy_id in execution["coverage"]["out_of_scope_policy_ids"]
        }
    )
    returned_policy_ids = sorted(
        {
            policy_id
            for execution in executions
            for policy_id in execution["coverage"]["returned_policy_ids"]
        }
    )
    request_policy_ids = sorted(
        {
            policy_id
            for execution in executions
            for policy_id in execution["coverage"]["request_policy_ids"]
        }
    )
    undeclared_materials = [
        {
            "execution_id": execution["execution_id"],
            "case_id": execution["case_id"],
            "task_id": item["task_id"],
            "material_id": material,
        }
        for execution in executions
        for item in execution["coverage"]["task_rows"]
        for material in item["undeclared_required_materials"]
    ]
    duplicate_case_modes = []
    seen: dict[tuple[Any, Any], list[str]] = {}
    for execution in executions:
        key = (execution["case_id"], execution["mode"])
        seen.setdefault(key, []).append(execution["execution_id"])
    for (case_id, mode), execution_ids in seen.items():
        if len(execution_ids) > 1:
            duplicate_case_modes.append(
                {"case_id": case_id, "mode": mode, "executions": execution_ids}
            )
    requests_metric = _metric("request_files", request_files, expected, "no_slots")
    results_metric = _metric("result_files", result_files, expected, "no_slots")
    return {
        "executions": len(executions),
        "cases_with_annotation_match": sorted(
            {execution["case_id"] for execution in executions if execution["case_id"]}
        ),
        "executions_without_annotation": [
            execution["execution_id"]
            for execution in executions
            if execution["case_id"] is None
        ],
        "expected_slots": expected,
        "request_files": request_files,
        "result_files": result_files,
        "final_answer_fallback_files": fallback_files,
        "missing_expected_slots": missing_slots,
        "extra_slots": extra_slots,
        "policy_scope_expected": list(EXPECTED_POLICY_SCOPE),
        "returned_policy_ids": returned_policy_ids,
        "request_policy_ids": request_policy_ids,
        "unexecuted_items": unexecuted,
        "failed_items": failed,
        "missing_task_dependencies": missing_dependencies,
        "out_of_scope_policy_ids": out_of_scope,
        "undeclared_required_materials": undeclared_materials,
        "duplicate_case_modes": duplicate_case_modes,
        "metrics": {
            "request_files": requests_metric,
            "result_files": results_metric,
        },
    }


def _overall_environment(executions: list[dict[str, Any]]) -> str:
    values = {execution["environment"] for execution in executions}
    if not values:
        return "UNKNOWN"
    if len(values) == 1:
        return next(iter(values))
    return "MIXED"


def _warnings(
    executions: list[dict[str, Any]],
    aggregate: dict[str, Any],
    environment: str,
) -> list[str]:
    warnings: list[str] = []
    if environment == "SIMULATED":
        warnings.append(
            "SIMULATED: offline MockTransport plumbing fixture; program success is not "
            "model quality, business accuracy, or legal review."
        )
    elif environment == "LIVE":
        warnings.append(
            "LIVE: real-run artifacts; classification is still only agreement with a "
            "single-author draft and is not business accuracy."
        )
    elif environment == "MIXED":
        warnings.append(
            "MIXED: both SIMULATED and LIVE execution artifacts were discovered; inspect "
            "each execution label before using any aggregate."
        )
    if aggregate["classification"]["metric"]["denominator"] == 0:
        warnings.append(
            "Classification agreement denominator is zero; needs_review/not_executed are "
            "listed separately and must not be converted into an accuracy percentage."
        )
    if all(
        row["classification"]["bucket"] in {"needs_review", "not_executed", "failed"}
        for execution in executions
        for row in execution["tasks"]
    ):
        warnings.append(
            "No determinate classification pair was produced by this fixture; do not "
            "report an accuracy score."
        )
    if aggregate["citation_precision"]["declared"]:
        warnings.append(
            "Located citations prove only unique verbatim position in the declared block; "
            "semantic support and conclusion correctness remain pending human review."
        )
    if any(
        row["final_answer_fallback_used"]
        for execution in executions
        for row in execution["tasks"]
    ):
        warnings.append(
            "One or more tasks used final-answers fallback because result.json was absent "
            "or had no final_answer; those tasks cannot establish pilot protocol success."
        )
    if aggregate["coverage"]["final_answer_fallback_files"] or any(
        execution["coverage"]["unexecuted_items"] for execution in executions
    ):
        warnings.append(
            "The artifact set contains missing results and/or unexecuted tasks; they are "
            "reported as coverage gaps and are not silently treated as compliant."
        )
    if any(
        row["failure"] in TRANSPORT_LIKE_FAILURES
        for execution in executions
        for row in execution["tasks"]
    ):
        warnings.append(
            "At least one task stopped on a transport/API/usage failure; no review "
            "conclusion should be inferred from that task."
        )
    if any(
        execution["annotation"]
        and execution["annotation"]["status"] != "single_author_pending_review"
        for execution in executions
    ):
        warnings.append(
            "At least one annotation is not marked single_author_pending_review; check "
            "its provenance before comparing classifications."
        )
    if aggregate["coverage"]["duplicate_case_modes"]:
        warnings.append(
            "Duplicate case/mode executions were discovered; aggregate denominators include "
            "all discovered executions and are not deduplicated."
        )
    mismatches = [
        row
        for execution in executions
        for row in execution["tasks"]
        if row["program"]["citations"]["pilot_crosscheck"].get("mismatch_count", 0)
    ]
    if mismatches:
        warnings.append(
            "Evaluator citation classification disagrees with the pilot diagnostics for at "
            "least one citation; inspect JSON diagnostics before using citation counts."
        )
    inconsistent = [
        row
        for execution in executions
        for row in execution["tasks"]
        if row["program"]["structure"]["recorded_reply_matches_parsed"] is False
        or row["program"]["structure"]["business_conclusion_matches_reply"] is False
    ]
    if inconsistent:
        warnings.append(
            "At least one result's validation.reply or business_conclusion does not match "
            "the parsed final answer; those rows are marked as structure failures."
        )
    return warnings


def _metric(
    name: str,
    numerator: int,
    denominator: int,
    null_reason: str | None,
) -> dict[str, Any]:
    if denominator < 0 or numerator < 0:
        raise ValueError("metric_counts_negative")
    if denominator == 0 and not null_reason:
        raise ValueError(f"metric_null_reason_required:{name}")
    return {
        "name": name,
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        "null_reason": None if denominator else null_reason,
    }


def _task_facets(result, fallback, attempts, failure, schema_ok, schema_errors,
                 pilot_valid, request_ok, reply_matches, conclusion_matches,
                 reply, request, classification, case, citation, fallback_used):
    """Independent observations; never repair or revalidate an original answer."""
    binding_ok = request_ok and reply_matches is not False and conclusion_matches is not False
    if failure == "truncated" or any(a.get("finish_reason") == "length" for a in attempts):
        category = "truncated"
    elif any(isinstance(a.get("http_status"), int) and a["http_status"] >= 400 for a in attempts):
        category = "http_failure"
    elif failure in {"transport", "timeout"}:
        category = "transport_failure"
    elif schema_ok is False:
        category = "json_failure" if any(e.startswith("reply_json_invalid") for e in schema_errors) else "schema_failure"
    elif pilot_valid is False or failure in BUSINESS_VALIDATION_FAILURES:
        category = "business_validation_failure"
    elif result and result["execution_status"] == "success":
        category = "success"
    elif result and result["execution_status"] == "not_executed":
        category = "not_executed"
    else:
        category = "unknown_no_accepted_answer"
    if not binding_ok:
        eligibility = "blocked_data_or_binding"
    elif not result or result["execution_status"] != "success" or pilot_valid is not True:
        eligibility = "no_accepted_result"
    elif classification["result_conclusion"] not in DETERMINATE_CONCLUSIONS:
        eligibility = "indeterminate_candidate"
    else:
        eligibility = "untrusted_or_missing_annotation"
    # Historical contract only defines development drafts, with no trusted-label provenance.
    # A future trusted annotation contract must be integrated explicitly.
    evidence = reply.get("evidence", []) if isinstance(reply, dict) else []
    cited = {e.get("document_id") for e in evidence if isinstance(e, dict) and e.get("source_type") == "demo_policy"}
    required = None
    if request:
        payload = parse_json_text(request["messages"][1]["content"])
        required = sorted({x["document_id"] for x in payload["task"]["sources"]})
    validation = result.get("validation") if result else None
    locations = validation.get("locations") if isinstance(validation, dict) else None
    return {
        "failure_category": category,
        "data_binding_ok": binding_ok,
        "schema_parse_ok": schema_ok,
        "pilot_validation_valid": pilot_valid,
        "pilot_accepted": bool(result and result["execution_status"] == "success" and pilot_valid is True),
        "pilot_accepted_location_count": len(locations) if isinstance(locations, list) and pilot_valid is True and result["execution_status"] == "success" else 0,
        "pilot_locations_recorded": isinstance(locations, list),
        "independent_located": citation["valid"],
        "independent_declared": citation["declared"],
        "candidate_classification": reply.get("conclusion") if isinstance(reply, dict) else None,
        "classification_eligibility": eligibility,
        "classification_score_eligible": False,
        "annotation_status": case["annotation"]["status"] if case else None,
        "fallback": {"present": fallback is not None, "read": fallback is not None,
                     "answer_adopted": fallback_used,
                     "attempts_adopted": result is None and fallback is not None},
        "task_required_policy_ids": required,
        "cited_policy_ids": sorted(cited),
        "task_primary_policy_declarations_complete": set(required) <= cited if required else None,
        "task_citation_requirements_original_result": (
            "accepted_by_pilot" if result and result["execution_status"] == "success" and pilot_valid is True
            else "rejected_policy_evidence" if failure in {"task_policy_evidence_required", "task_section_evidence_required", "dependency_policy_evidence_required"}
            else "not_established"
        ),
        "task_policy_rule": "historical all primary sources declared (not evidence acceptance); location and pilot acceptance separate",
    }


def _facet_totals(rows):
    from collections import Counter
    facets = [r["evaluation_v2"] for r in rows]
    return {
        "slots_denominator": len(rows),
        "execution_status_counts": dict(Counter(r["execution_status"] for r in rows)),
        "failure_category_counts": dict(Counter(f["failure_category"] for f in facets)),
        "candidate_classification_counts": dict(Counter(f["candidate_classification"] or "absent" for f in facets)),
        "classification_eligibility_counts": dict(Counter(f["classification_eligibility"] for f in facets)),
        "trusted_classification_score": _metric("trusted_classification_score", 0, 0, "historical_contract_has_no_trusted_labels"),
        "legacy_classification_role": "single_author_draft_diagnostic_only; failed buckets are eligibility, not business classes",
        "schema_parse": _metric("schema_parse", sum(f["schema_parse_ok"] is True for f in facets), sum(f["schema_parse_ok"] is not None for f in facets), "no_answers"),
        "pilot_acceptance": _metric("pilot_acceptance", sum(f["pilot_accepted"] for f in facets), len(rows), "no_slots"),
        "pilot_accepted_locations": sum(f["pilot_accepted_location_count"] for f in facets),
        "independent_location": _metric("independent_location", sum(f["independent_located"] for f in facets), sum(f["independent_declared"] for f in facets), "no_declarations"),
        "fallback_counts": {key: sum(f["fallback"][key] for f in facets) for key in ("present", "read", "answer_adopted", "attempts_adopted")},
    }
