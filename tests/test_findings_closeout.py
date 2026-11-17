import json
import uuid
from pathlib import Path

import pytest

from production_rag.e2e_support import compact_fixture_reply
from production_rag.knowledge_baseline.snapshot import build
from production_rag.pilot_review import findings_v2 as f
from production_rag.pilot_review import presign
from production_rag.pilot_review.execution import Config, execute
from production_rag.pilot_review.inputs import load_run, save_run

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    "normal",
    "deviation",
    "approval-unknown",
    "attachment-unknown",
    "undefined-policy",
    "partial-parse",
]


@pytest.fixture(scope="module")
def snapshot():
    return build(ROOT / "data/contract-knowledge-candidate", mode="policies15")


def config(**kwargs):
    return Config(
        mode="all",
        output_protocol="pilot-presign-findings-v3",
        prompt_version=f.VERSION_V3,
        per_call_output=1024,
        **kwargs,
    )


def make_run(snapshot, tmp_path, case):
    raw = json.loads((ROOT / "data/review-v2-dev/inputs" / f"{case}.json").read_bytes())
    path = tmp_path / "contract.md"
    path.write_text(raw["contract_text"], encoding="utf-8")
    run = presign.prepare_presign(
        path,
        snapshot,
        run_id=uuid.uuid4().hex,
        code_revision="e" * 40,
        material_specs=[
            {"material_id": mid, "status": "unknown", "statement": "SIMULATED：尚未核实"}
            for mid in (
                "technical_spec",
                "approval_record",
                "authority_record",
                "condition_records",
                "size_declaration",
            )
        ],
    )
    return run, raw


@pytest.mark.parametrize("case", CASES)
def test_six_cases_request_validation_save(snapshot, tmp_path, case):
    run, raw = make_run(snapshot, tmp_path, case)
    save_run(run, tmp_path)
    assert load_run(tmp_path / run.run_id / "input.json") == run
    checked = []
    for task in run.checklist:
        prepared = f.request_v3(
            run, task, config(), coverage_complete=raw["parse_coverage_complete"]
        )
        payload = json.loads(prepared["messages"][1]["content"])
        assert "expectation" not in json.dumps(payload) and "expectations-draft" not in json.dumps(
            payload
        )
        reply = compact_fixture_reply(payload, case)
        result = f.validate_v3(run, task, prepared, json.dumps(reply))
        assert result["valid"]
        checked.append(result)
        for eid, reg in prepared["evidence_registry"].items():
            doc = next(
                m["source"]
                for m in prepared["maps"]
                if m["source"]["document_id"] == reg["document_id"]
            )
            assert doc["text"][reg["start"] : reg["end"]] == reg["quote"]
        for mapping in prepared["maps"]:
            intervals = [
                m
                for m in prepared["selection_manifest"]
                if m["document_id"] == mapping["source"]["document_id"]
            ]
            assert sum(m["end"] - m["start"] for m in intervals) == len(mapping["source"]["text"])
    index = {
        "normal": 0,
        "deviation": 0,
        "approval-unknown": 0,
        "attachment-unknown": 2,
        "undefined-policy": 1,
        "partial-parse": 2,
    }[case]
    reply = checked[index]["reply"]
    if case == "normal":
        assert reply["conclusion"] == "compliant"
    if case in ("deviation", "approval-unknown"):
        assert reply["conclusion"] == "deviation" and reply["approval_status"] == "required_unknown"
    if case == "attachment-unknown":
        assert [c["conclusion"] for c in reply["comparisons"]] == ["compliant", "needs_review"]
        assert reply["conclusion"] == "needs_review"
    if case == "undefined-policy":
        assert reply["comparisons"][0]["limitation"] == "standard_undefined"
    if case == "partial-parse":
        assert all(c["fact_scope"] == "observed_text" for c in reply["comparisons"])


def test_negative_boundaries(snapshot, tmp_path):
    run, _ = make_run(snapshot, tmp_path, "attachment-unknown")
    task = run.checklist[2]
    prepared = f.request_v3(run, task, config(), coverage_complete=False)
    reply = compact_fixture_reply(
        json.loads(prepared["messages"][1]["content"]), "attachment-unknown"
    )
    reply["conclusion"] = "compliant"
    with pytest.raises(ValueError, match="partial_result_aggregate_mismatch"):
        f.validate_v3(run, task, prepared, json.dumps(reply))
    reply["conclusion"] = "needs_review"
    reply["requested_material_ids"] = ["approval_record"]
    with pytest.raises(ValueError, match="material_request_not_applicable"):
        f.validate_v3(run, task, prepared, json.dumps(reply))
    reply["requested_material_ids"] = []
    reply["comparisons"][1]["fact_scope"] = "absence_candidate"
    with pytest.raises(ValueError, match="parse_coverage_blocks_absence_candidate"):
        f.validate_v3(run, task, prepared, json.dumps(reply))
    reply["comparisons"][1]["fact_scope"] = "observed_text"
    reply["comparisons"][0]["evidence_ids"] = ["FAKE"]
    with pytest.raises(ValueError, match="unknown_evidence_id"):
        f.validate_v3(run, task, prepared, json.dumps(reply))


def test_reservation_before_calls(snapshot, tmp_path):
    run, _ = make_run(snapshot, tmp_path, "normal")

    def forbidden(_):
        pytest.fail("insufficient reservation must not create client")

    execute(run, config(total_output=4607), tmp_path / "executions", forbidden)
    rows = [json.loads(p.read_bytes()) for p in tmp_path.rglob("*-result.json")]
    assert len(rows) == 6 and all(r["execution_status"] == "not_executed" for r in rows)
    plan = f.budget_plan(run, config())
    assert plan["reserved_total"] == 4608 and plan["fits"]
    assert [s["cap"] for s in plan["slots"]] == [768, 640, 896, 640, 896, 768]


def test_local_process_survives_missing_authority(snapshot, tmp_path):
    run, _ = make_run(snapshot, tmp_path, "normal")
    task = run.checklist[4]
    prepared = f.request_v3(run, task, config())
    reply = compact_fixture_reply(json.loads(prepared["messages"][1]["content"]))
    reply["comparisons"][0].update(conclusion="compliant", limitation="none", pending="")
    assert prepared["parts"][1][
        "unreturned_policy_units"
    ]  # Exists in snapshot but outside returned scope.
    checked = f.validate_v3(run, task, prepared, json.dumps(reply))
    assert checked["reply"]["comparisons"][0]["conclusion"] == "compliant"
    assert checked["reply"]["comparisons"][1]["conclusion"] == "needs_review"
    assert checked["reply"]["conclusion"] == "needs_review"


def test_reserved_caps_reach_last_slot(snapshot, tmp_path):
    run, _ = make_run(snapshot, tmp_path, "normal")
    caps = []

    class Client:
        attempts = []

        def chat(self, messages, budget):
            cap = budget.allowance()
            caps.append(cap)
            budget.charge({"eval_count": cap, "prompt_eval_count": 0})
            reply = compact_fixture_reply(json.loads(messages[1]["content"]))
            return {"content": json.dumps(reply), "eval_count": cap}

    _, report = execute(run, config(), tmp_path / "executions", lambda _: Client())
    assert caps == [768, 640, 896, 640, 896, 768]
    assert all(row["execution_status"] == "success" for row in report["results"])


def test_unknown_approval_does_not_allow_authority_request(snapshot, tmp_path):
    run, _ = make_run(snapshot, tmp_path, "normal")
    task = run.checklist[4]
    prepared = f.request_v3(run, task, config())
    reply = compact_fixture_reply(json.loads(prepared["messages"][1]["content"]))
    reply["comparisons"][1]["limitation"] = "material_unknown"
    reply["requested_material_ids"] = ["authority_record"]
    with pytest.raises(ValueError, match="material_request_not_applicable"):
        f.validate_v3(run, task, prepared, json.dumps(reply))


def test_v3_protocol_boundary_and_no_live_client(snapshot, tmp_path):
    with pytest.raises(ValueError, match="protocol_prompt_version_mismatch"):
        Config(mode="all", output_protocol="pilot-review-block-v2", prompt_version=f.VERSION_V3)
    run, _ = make_run(snapshot, tmp_path, "normal")

    def forbidden(_):
        pytest.fail("real client must never be created")

    with pytest.raises(ValueError, match="findings_v2_offline_only"):
        execute(
            run,
            config(simulation=False, presign_live_allowed=True),
            tmp_path / "executions",
            forbidden,
        )


def test_ambiguous_source_is_retained_as_unselectable_context(snapshot, tmp_path):
    path = tmp_path / "repeated.md"
    path.write_text("甲" * 440, encoding="utf-8")
    run = presign.prepare_presign(path, snapshot, run_id=uuid.uuid4().hex, code_revision="e" * 40)
    prepared = f.request_v3(run, run.checklist[0], config())
    payload = json.loads(prepared["messages"][1]["content"])
    assert (
        "".join(
            c["text"] for c in payload["unselectable_context"] if c["source_type"] == "contract"
        )
        == "甲" * 440
    )
    assert not any(r["source_type"] == "contract" for r in prepared["evidence_registry"].values())


def test_approval_trigger_requires_its_own_policy_section(snapshot, tmp_path):
    run, _ = make_run(snapshot, tmp_path, "approval-unknown")
    task = run.checklist[0]
    prepared = f.request_v3(run, task, config())
    reply = compact_fixture_reply(
        json.loads(prepared["messages"][1]["content"]), "approval-unknown"
    )
    assert (
        f.validate_v3(run, task, prepared, json.dumps(reply))["reply"]["conclusion"] == "deviation"
    )
    reply["approval_evidence_ids"] = reply["comparisons"][0]["evidence_ids"]
    with pytest.raises(ValueError, match="approval_trigger_evidence_required"):
        f.validate_v3(run, task, prepared, json.dumps(reply))
