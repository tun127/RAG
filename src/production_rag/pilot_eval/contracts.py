"""JSON-only contract helpers for the M3.2 evaluation adapter.

The adapter deliberately does not import the pilot execution package.  It reads
the frozen pilot artifacts as a data contract and fails loudly when a required
field is missing or two identity bindings disagree.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "pilot-eval-report-v1"
PROTOCOL = "pilot-review-block-v1"
PROTOCOL_V2 = "pilot-review-block-v2"
SUPPORTED_PROTOCOLS = {PROTOCOL, PROTOCOL_V2}

EXPECTED_TASK_IDS = (
    "RAG-PILOT-PAY-01",
    "RAG-PILOT-PAY-02",
    "RAG-PILOT-ACC-01",
    "RAG-PILOT-ACC-02",
    "RAG-PILOT-EXC-01",
    "RAG-PILOT-EXC-02",
)
EXPECTED_POLICY_SCOPE = ("DP-PAY", "DP-ACC", "DP-EXC")
CONCLUSIONS = ("compliant", "deviation", "needs_review", "not_applicable")
DETERMINATE_CONCLUSIONS = frozenset({"compliant", "deviation", "not_applicable"})
REASON_CODES = (
    "text_consistent",
    "text_deviation",
    "condition_unknown",
    "condition_not_triggered",
    "material_unavailable",
    "absence_candidate",
)
SOURCE_TYPES = ("contract", "material", "demo_policy")
EXECUTION_STATUSES = ("success", "failed", "not_executed")

_HEX64 = re.compile(r"[a-f0-9]{64}")
_HEX32 = re.compile(r"[a-f0-9]{32}")


class ContractError(Exception):
    """Artifact contract cannot be evaluated without guessing."""

    def __init__(
        self,
        message: str,
        *,
        path: Path | str | None = None,
        missing_fields: list[str] | None = None,
        detail: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.path = str(path) if path is not None else None
        self.missing_fields = list(missing_fields or [])
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "blocked",
            "reason": self.message,
            "path": self.path,
            "missing_fields": self.missing_fields,
            "detail": self.detail,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def is_hex64(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def is_hex32(value: Any) -> bool:
    return isinstance(value, str) and _HEX32.fullmatch(value) is not None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContractError("artifact_unreadable", path=path, detail=str(exc)) from exc
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("artifact_invalid_json", path=path, detail=str(exc)) from exc


def parse_json_text(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def require_object(value: Any, *, path: Path | str, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("artifact_not_object", path=path, detail=role)
    return value


def require_fields(
    value: dict[str, Any], fields: tuple[str, ...] | list[str], *, path: Path | str, role: str
) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise ContractError(
            "missing_required_fields",
            path=path,
            missing_fields=missing,
            detail=role,
        )


def artifact_record(path: Path | None, role: str, base: Path | None) -> dict[str, Any]:
    display = display_path(path, base) if path is not None else None
    if path is None or not path.is_file():
        return {
            "role": role,
            "path": display,
            "present": False,
            "sha256": None,
            "bytes": None,
        }
    raw = path.read_bytes()
    return {
        "role": role,
        "path": display,
        "present": True,
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
    }


def display_path(path: Path, base: Path | None) -> str:
    if base is not None:
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return str(path)


def discover_execution_dirs(run_root: Path) -> list[Path]:
    if (run_root / "execution-input.json").is_file():
        return [run_root]
    found = sorted({path.parent for path in run_root.rglob("execution-input.json")})
    if not found:
        raise ContractError("no_execution_inputs_found", path=run_root)
    return found


def load_cases_manifest(path: Path) -> list[dict[str, Any]]:
    manifest = load_json(path)
    if not isinstance(manifest, list) or not manifest:
        raise ContractError("cases_manifest_not_nonempty_list", path=path)
    cases: list[dict[str, Any]] = []
    by_contract: dict[str, str] = {}
    for index, entry in enumerate(manifest):
        entry = require_object(entry, path=path, role=f"cases[{index}]")
        require_fields(entry, ("case_id", "annotation"), path=path, role=f"cases[{index}]")
        case_id = entry["case_id"]
        if not isinstance(case_id, str) or not case_id:
            raise ContractError("invalid_case_id", path=path, detail=f"cases[{index}]")
        annotation_path = Path(entry["annotation"])
        if not annotation_path.is_absolute():
            annotation_path = path.parent / annotation_path
        if not annotation_path.is_file():
            raise ContractError("annotation_not_found", path=annotation_path, detail=case_id)
        annotation = require_object(
            load_json(annotation_path), path=annotation_path, role="annotation"
        )
        require_fields(
            annotation,
            ("status", "contract_sha256", "task_drafts"),
            path=annotation_path,
            role="annotation",
        )
        if not is_hex64(annotation["contract_sha256"]):
            raise ContractError("invalid_annotation_contract_sha256", path=annotation_path)
        drafts = annotation["task_drafts"]
        if not isinstance(drafts, dict) or not drafts:
            raise ContractError("annotation_task_drafts_not_nonempty_object", path=annotation_path)
        for task_id, draft in drafts.items():
            if not isinstance(task_id, str) or not isinstance(draft, dict):
                raise ContractError("invalid_annotation_task_draft", path=annotation_path)
            candidate = draft.get("candidate_for_review")
            if candidate not in CONCLUSIONS:
                raise ContractError(
                    "invalid_annotation_candidate",
                    path=annotation_path,
                    detail=f"{task_id}:{candidate!r}",
                )
        contract_sha = annotation["contract_sha256"]
        if contract_sha in by_contract:
            raise ContractError(
                "duplicate_annotation_contract_sha256",
                path=annotation_path,
                detail=f"{by_contract[contract_sha]} and {case_id}",
            )
        by_contract[contract_sha] = case_id
        cases.append(
            {
                "case_id": case_id,
                "annotation_path": annotation_path,
                "annotation": annotation,
                "annotation_record": artifact_record(annotation_path, "annotation", path.parent),
            }
        )
    return cases


def load_execution_input(path: Path) -> dict[str, Any]:
    data = require_object(load_json(path), path=path, role="execution_input")
    require_fields(
        data,
        (
            "run_id",
            "input_sha256",
            "knowledge_id",
            "config",
            "execution_revision",
            "implementation_sha256",
            "prompt_sha256",
            "simulation",
            "slots",
        ),
        path=path,
        role="execution_input",
    )
    if not is_hex32(data["run_id"]):
        raise ContractError("invalid_run_id", path=path, detail=str(data["run_id"]))
    if not is_hex64(data["input_sha256"]):
        raise ContractError("invalid_input_sha256", path=path)
    config = require_object(data["config"], path=path, role="execution_input.config")
    require_fields(config, ("mode", "simulation"), path=path, role="execution_input.config")
    if config["mode"] not in {"all", "rag"}:
        raise ContractError("invalid_execution_mode", path=path, detail=str(config["mode"]))
    if not isinstance(data["simulation"], bool) or not isinstance(config["simulation"], bool):
        raise ContractError("invalid_simulation_flag", path=path)
    if data["simulation"] != config["simulation"]:
        raise ContractError("simulation_flag_conflict", path=path)
    if config.get("output_protocol", PROTOCOL) not in SUPPORTED_PROTOCOLS:
        raise ContractError("unsupported_execution_protocol", path=path)
    slots = data["slots"]
    if not isinstance(slots, list):
        raise ContractError("execution_slots_not_list", path=path)
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict) or not isinstance(slot.get("task_id"), str):
            raise ContractError("invalid_execution_slot", path=path, detail=f"slots[{index}]")
    return data


def find_prepared_input(
    execution_dir: Path, run_id: str, inputs_root: Path | None
) -> Path | None:
    if inputs_root is not None:
        candidate = inputs_root / run_id / "input.json"
        return candidate if candidate.is_file() else None
    for root in search_roots(execution_dir):
        candidate = root / "inputs" / run_id / "input.json"
        if candidate.is_file():
            return candidate
    return None


def find_final_answer(
    execution_dir: Path, task_id: str, final_answers_root: Path | None
) -> Path | None:
    if final_answers_root is not None:
        candidate = final_answers_root / f"{task_id}.json"
        return candidate if candidate.is_file() else None
    for root in search_roots(execution_dir):
        candidate = root / "final-answers" / f"{task_id}.json"
        if candidate.is_file():
            return candidate
    return None


def search_roots(start: Path) -> list[Path]:
    roots = [start]
    current = start
    for _ in range(5):
        if current.parent == current:
            break
        current = current.parent
        roots.append(current)
    return roots


def load_prepared_input(path: Path) -> dict[str, Any]:
    data = require_object(load_json(path), path=path, role="prepared_input")
    require_fields(data, ("input_sha256", "payload"), path=path, role="prepared_input")
    if not is_hex64(data["input_sha256"]):
        raise ContractError("invalid_prepared_input_sha256", path=path)
    payload = require_object(data["payload"], path=path, role="prepared_input.payload")
    require_fields(
        payload,
        ("run_id", "code_revision", "knowledge_id", "contract"),
        path=path,
        role="prepared_input.payload",
    )
    contract = require_object(
        payload["contract"], path=path, role="prepared_input.payload.contract"
    )
    require_fields(contract, ("raw_sha256", "text"), path=path, role="prepared_input.contract")
    if not is_hex64(contract["raw_sha256"]):
        raise ContractError("invalid_contract_sha256", path=path)
    return data


def load_request(path: Path) -> dict[str, Any]:
    data = require_object(load_json(path), path=path, role="request")
    require_fields(
        data,
        (
            "retrieval",
            "maps",
            "messages",
            "request_sha256",
            "request_bytes",
            "input_characters",
        ),
        path=path,
        role="request",
    )
    retrieval = require_object(data["retrieval"], path=path, role="request.retrieval")
    require_fields(
        retrieval,
        (
            "snapshot_id",
            "contexts",
            "missing_task_dependencies",
            "missing_primary_policies",
        ),
        path=path,
        role="request.retrieval",
    )
    if not isinstance(data["maps"], list) or not data["maps"]:
        raise ContractError("request_maps_not_nonempty_list", path=path)
    if not isinstance(data["messages"], list) or len(data["messages"]) < 2:
        raise ContractError("request_messages_invalid", path=path)
    if not isinstance(retrieval["contexts"], list):
        raise ContractError("request_retrieval_contexts_not_list", path=path)
    if not isinstance(retrieval["missing_task_dependencies"], list):
        raise ContractError("request_missing_task_dependencies_not_list", path=path)
    if not isinstance(retrieval["missing_primary_policies"], list):
        raise ContractError("request_missing_primary_policies_not_list", path=path)
    if not is_hex64(data["request_sha256"]):
        raise ContractError("invalid_request_sha256", path=path)
    return data


def load_result(path: Path) -> dict[str, Any]:
    data = require_object(load_json(path), path=path, role="result")
    require_fields(
        data,
        (
            "task_id",
            "request_sha256",
            "request_bytes",
            "input_characters",
            "execution_status",
            "failure",
            "business_conclusion",
            "manual_review_required",
            "missing_task_dependencies",
            "returned_policy_ids",
            "undeclared_required_materials",
            "attempts",
            "validation",
            "final_answer",
        ),
        path=path,
        role="result",
    )
    if data["execution_status"] not in EXECUTION_STATUSES:
        raise ContractError(
            "invalid_result_execution_status", path=path, detail=str(data["execution_status"])
        )
    if data["business_conclusion"] is not None and data["business_conclusion"] not in CONCLUSIONS:
        raise ContractError(
            "invalid_result_business_conclusion",
            path=path,
            detail=str(data["business_conclusion"]),
        )
    if not isinstance(data["attempts"], list):
        raise ContractError("result_attempts_not_list", path=path)
    if data["validation"] is not None and not isinstance(data["validation"], dict):
        raise ContractError("result_validation_not_object", path=path)
    if isinstance(data["validation"], dict):
        require_fields(data["validation"], ("valid",), path=path, role="result.validation")
        if not isinstance(data["validation"]["valid"], bool):
            raise ContractError("result_validation_valid_not_bool", path=path)
    if data["manual_review_required"] is not None and not isinstance(
        data["manual_review_required"], bool
    ):
        raise ContractError("result_manual_review_required_not_bool", path=path)
    if data["final_answer"] is not None and not isinstance(data["final_answer"], str):
        raise ContractError("result_final_answer_not_text", path=path)
    if not is_hex64(data["request_sha256"]):
        raise ContractError("invalid_result_request_sha256", path=path)
    return data


def load_final_answer(path: Path) -> dict[str, Any]:
    data = require_object(load_json(path), path=path, role="final_answer")
    require_fields(data, ("task_id", "final_text", "attempts"), path=path, role="final_answer")
    if data["final_text"] is not None and not isinstance(data["final_text"], str):
        raise ContractError("final_answer_text_not_text_or_null", path=path)
    if not isinstance(data["attempts"], list):
        raise ContractError("final_answer_attempts_not_list", path=path)
    return data


def request_metrics(messages: list[dict[str, Any]]) -> tuple[str, int, int]:
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    characters = sum(len(message["content"]) for message in messages)
    return sha256_bytes(encoded), len(encoded), characters


def occurrences(text: str, quote: str) -> list[int]:
    result: list[int] = []
    start = 0
    while True:
        found = text.find(quote, start)
        if found < 0:
            return result
        result.append(found)
        start = found + 1


def validate_reply_schema(reply: Any, protocol: str = PROTOCOL) -> list[str]:
    if protocol == PROTOCOL_V2:
        return validate_v2_schema(reply)
    if protocol != PROTOCOL:
        return ["unsupported_reply_protocol"]
    errors: list[str] = []
    if not isinstance(reply, dict):
        return ["reply_not_object"]
    expected = {"conclusion", "reason_code", "evidence", "material_ids", "coverage"}
    missing = sorted(expected - reply.keys())
    extra = sorted(reply.keys() - expected)
    if missing:
        errors.append("reply_missing_keys:" + ",".join(missing))
    if extra:
        errors.append("reply_extra_keys:" + ",".join(extra))
    if reply.get("conclusion") not in CONCLUSIONS:
        errors.append("reply_invalid_conclusion")
    if reply.get("reason_code") not in REASON_CODES:
        errors.append("reply_invalid_reason_code")
    evidence = reply.get("evidence")
    if not isinstance(evidence, list):
        errors.append("reply_evidence_not_list")
        evidence = []
    elif len(evidence) > 4:
        errors.append("reply_evidence_over_limit")
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            errors.append(f"evidence[{index}]_not_object")
            continue
        keys = {"source_type", "document_id", "version", "block_id", "quote"}
        if set(item.keys()) != keys:
            errors.append(f"evidence[{index}]_key_set_mismatch")
        if item.get("source_type") not in SOURCE_TYPES:
            errors.append(f"evidence[{index}]_invalid_source_type")
        for key in ("document_id", "version", "block_id"):
            if not isinstance(item.get(key), str) or not item.get(key):
                errors.append(f"evidence[{index}]_{key}_invalid")
        quote = item.get("quote")
        if not isinstance(quote, str) or not 1 <= len(quote) <= 240 or not quote.strip():
            errors.append(f"evidence[{index}]_quote_invalid")
    material_ids = reply.get("material_ids")
    if not isinstance(material_ids, list) or not all(
        isinstance(item, str) for item in material_ids
    ):
        errors.append("reply_material_ids_invalid")
    elif len(material_ids) > 16:
        errors.append("reply_material_ids_over_limit")
    elif len(set(material_ids)) != len(material_ids):
        errors.append("reply_material_ids_duplicate")
    coverage = reply.get("coverage")
    if coverage is not None:
        if not isinstance(coverage, dict):
            errors.append("reply_coverage_invalid")
        else:
            coverage_keys = {"document_id", "version", "text_sha256", "block_ids"}
            if set(coverage.keys()) != coverage_keys:
                errors.append("reply_coverage_key_set_mismatch")
            if not is_hex64(coverage.get("text_sha256")):
                errors.append("reply_coverage_text_sha256_invalid")
            if not isinstance(coverage.get("block_ids"), list) or not all(
                isinstance(item, str) for item in coverage["block_ids"]
            ):
                errors.append("reply_coverage_block_ids_invalid")
    return errors


def parse_reply(text: str, protocol: str = PROTOCOL) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        reply = parse_json_text(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, [f"reply_json_invalid:{exc}"]
    errors = validate_reply_schema(reply, protocol)
    return (reply if isinstance(reply, dict) else None), errors


def verify_source(source: Any, *, role: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(source, dict):
        return {}, [f"{role}_not_object"]
    required = (
        "document_id",
        "version",
        "source_type",
        "raw_base64",
        "raw_sha256",
        "text",
        "text_sha256",
    )
    missing = [key for key in required if key not in source]
    if missing:
        return {}, [f"{role}_missing_keys:" + ",".join(missing)]
    if source["source_type"] not in SOURCE_TYPES:
        errors.append(f"{role}_invalid_source_type")
    try:
        raw = base64.b64decode(source["raw_base64"], validate=True)
    except (ValueError, TypeError):
        return source, errors + [f"{role}_raw_base64_invalid"]
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return source, errors + [f"{role}_raw_not_utf8"]
    if decoded != source["text"]:
        errors.append(f"{role}_raw_text_mismatch")
    if sha256_bytes(raw) != source["raw_sha256"]:
        errors.append(f"{role}_raw_sha256_mismatch")
    if sha256_text(source["text"]) != source["text_sha256"]:
        errors.append(f"{role}_text_sha256_mismatch")
    return source, errors


def verify_maps(maps: list[Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    index: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for map_index, mapping in enumerate(maps):
        if not isinstance(mapping, dict):
            errors.append(f"maps[{map_index}]_not_object")
            continue
        source, source_errors = verify_source(
            mapping.get("source"), role=f"maps[{map_index}].source"
        )
        errors.extend(source_errors)
        if not source:
            continue
        document_id = source.get("document_id")
        if document_id in index:
            errors.append(f"maps[{map_index}]_duplicate_document_id")
            continue
        blocks = mapping.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            errors.append(f"maps[{map_index}]_blocks_not_nonempty_list")
            continue
        by_id: dict[str, dict[str, Any]] = {}
        expected_start = 0
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                errors.append(f"maps[{map_index}].blocks[{block_index}]_not_object")
                continue
            required = ("block_id", "start", "end", "text")
            if any(key not in block for key in required):
                errors.append(f"maps[{map_index}].blocks[{block_index}]_missing_keys")
                continue
            block_id = block["block_id"]
            start = block["start"]
            end = block["end"]
            text = block["text"]
            if not isinstance(block_id, str) or not block_id:
                errors.append(f"maps[{map_index}].blocks[{block_index}]_bad_block_id")
                continue
            if block_id in by_id:
                errors.append(f"maps[{map_index}]_duplicate_block_id")
                continue
            if not isinstance(start, int) or not isinstance(end, int):
                errors.append(f"maps[{map_index}].blocks[{block_index}]_bad_bounds")
                continue
            if start != expected_start or end <= start:
                errors.append(f"maps[{map_index}].blocks[{block_index}]_non_contiguous")
                continue
            if not isinstance(text, str) or source["text"][start:end] != text:
                errors.append(f"maps[{map_index}].blocks[{block_index}]_text_slice_mismatch")
                continue
            expected_start = end
            by_id[block_id] = block
        if expected_start != len(source["text"]):
            errors.append(f"maps[{map_index}]_blocks_do_not_cover_source")
        index[document_id] = {
            "source": source,
            "blocks": blocks,
            "blocks_by_id": by_id,
        }
    return index, errors


def validate_v2_schema(reply):
    """Independent JSON schema checks only; never re-run business acceptance."""
    if not isinstance(reply, dict):
        return ["reply_not_object"]
    expected = {"protocol", "conclusion", "reason_code", "evidence", "material_refs",
                "requested_material_ids", "coverage", "review_issue"}
    errors = []
    if set(reply) != expected:
        errors.append("reply_v2_key_set_mismatch")
    if reply.get("protocol") != PROTOCOL_V2:
        errors.append("reply_protocol_mismatch")
    # Shared citation/coverage primitives; material_refs remain state references.
    common = {k: reply.get(k) for k in ("conclusion", "reason_code", "evidence", "coverage")}
    common["material_ids"] = reply.get("material_refs")
    errors.extend(e.replace("material_ids", "material_refs") for e in validate_reply_schema(common, PROTOCOL))
    ids = reply.get("requested_material_ids")
    if not isinstance(ids, list) or len(ids) > 16 or not all(isinstance(i, str) for i in ids):
        errors.append("reply_requested_material_ids_invalid")
    issue = reply.get("review_issue")
    if issue is not None:
        if not isinstance(issue, dict) or set(issue) != {"question", "evidence_indexes"}:
            errors.append("reply_review_issue_invalid")
        else:
            q, indexes = issue["question"], issue["evidence_indexes"]
            if not isinstance(q, str) or not 8 <= len(q) <= 240:
                errors.append("reply_review_question_invalid")
            if not isinstance(indexes, list) or not 1 <= len(indexes) <= 4 or not all(type(i) is int for i in indexes):
                errors.append("reply_review_indexes_invalid")
    return errors
