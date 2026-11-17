"""Pre-signature development fixtures. Offline only; responses are not predictions."""
import json

from . import presign
from .inputs import save_run, write_new


def prepare_case(folder, snapshot, *, run_id, code_revision):
    specs = json.loads((folder / "materials.json").read_bytes())
    for spec in specs:
        if spec.get("path"):
            spec["path"] = str(folder / spec["path"])
    run = presign.prepare_presign(folder / "contract.md", snapshot, run_id=run_id,
                                  code_revision=code_revision, material_specs=specs)
    draft = json.loads((folder / "hypotheses-draft.json").read_bytes())
    hypotheses = []
    for condition, state in draft["states"].items():
        basis = []
        kind = "unassessed"
        if state != "unknown":
            if condition == "existing_approval" and state == "triggered":
                material = next(m for m in run.materials if m.material_id == "approval_record")
                basis = [presign.Basis(document_id=material.document.document_id,
                    version=material.document.version, quote=material.document.text)]
                kind = "approval_content"
            else:
                # Explicit draft states above drive this fixture; no text classification.
                paragraph = run.contract.text.split("\n\n")[2 if condition == "acceptance_deviation" else 1]
                did = "DP-ACC" if condition == "acceptance_deviation" else "DP-PAY"
                heading = "3. 分层验收与期限" if did == "DP-ACC" else "2. 默认要求与证据"
                unit = next(u for u in snapshot.units if u.document_id == did and u.heading == heading)
                doc = next(d for d in snapshot.documents if d.document_id == did)
                text = doc.text[unit.start:unit.end]
                body = next(line for line in text.splitlines() if line.strip() and not line.startswith("#"))
                basis = [presign.Basis(document_id=run.contract.document_id, version=run.contract.version, quote=paragraph),
                         presign.Basis(document_id=did, version=doc.version, quote=body[:240])]
                kind = "clause_comparison"
        hypotheses.append(presign.Hypothesis(condition=condition, state=state, basis_kind=kind, basis=basis,
                           note="人工指定开发假设，仅验证条件字段流转；证据语义仍待人工复核"))
    values = run.model_dump()
    values["trigger_hypotheses"] = hypotheses
    return presign.PresignRun(**values)


def simulated_reply(run, task, prepared, choice):
    """Construct source-bound structural responses, without reading annotations."""
    conclusion = "needs_review" if choice == "material_unavailable" else choice
    reason = {"compliant": "text_consistent", "deviation": "text_deviation", "not_applicable": "condition_not_triggered",
              "needs_review": "condition_unknown", "material_unavailable": "material_unavailable"}[choice]
    evidence = []
    for ref in task.sources:
        unit = next(u for u in run.knowledge.units if u.unit_id == ref.unit_id)
        mapping = next(m for m in prepared["maps"] if m["source"]["document_id"] == ref.document_id)
        block = next(b for b in mapping["blocks"] if b["start"] <= unit.start < b["end"])
        body = next(line for line in block["text"].splitlines() if line.strip() and not line.startswith("#"))
        evidence.append({**{k: mapping["source"][k] for k in ("source_type", "document_id", "version")},
                         "block_id": block["block_id"], "quote": body[:200]})
    contract = prepared["maps"][0]
    block = contract["blocks"][1]
    evidence.append({**{k: contract["source"][k] for k in ("source_type", "document_id", "version")},
                     "block_id": block["block_id"], "quote": block["text"].strip()[:240]})
    if conclusion != "needs_review":
        for did in task.required_policy_dependencies:
            mapping = next(m for m in prepared["maps"] if m["source"]["document_id"] == did)
            block = next(b for b in mapping["blocks"] if any(line.strip() and not line.startswith("#") for line in b["text"].splitlines()))
            body = next(line for line in block["text"].splitlines() if line.strip() and not line.startswith("#"))
            evidence.append({**{k: mapping["source"][k] for k in ("source_type", "document_id", "version")},
                             "block_id": block["block_id"], "quote": body[:200]})
    return {"protocol": "pilot-review-block-v2", "conclusion": conclusion, "reason_code": reason,
            "evidence": evidence, "material_refs": ["technical_spec"] if choice == "material_unavailable" else [],
            "requested_material_ids": ["technical_spec"] if choice == "material_unavailable" else [], "coverage": None,
            "review_issue": {"question": "SIMULATED：需人工核实拟签版本、技术附件或签署前条件及政策依赖；不核验未来履约",
                             "evidence_indexes": [0]} if conclusion == "needs_review" else None}


def run_cases(root, output, revision):
    import uuid

    import httpx
    from pydantic import SecretStr

    from production_rag.adapters.spark_gateway.client import SparkChat
    from production_rag.adapters.spark_gateway.settings import SparkSettings
    from production_rag.knowledge_baseline.snapshot import build
    from production_rag.pilot_eval import evaluate_pilot
    from production_rag.pilot_eval.render import render_markdown
    from production_rag.pilot_review.execution import Config, execute

    output.mkdir(parents=True, exist_ok=False)
    data = root / "data/presign-review-dev"
    snapshot = build(root / "data/contract-knowledge-candidate", mode="policies15")
    summaries = []
    for case in json.loads((data / "cases.json").read_bytes()):
        run = prepare_case(data / case["input_directory"], snapshot, run_id=uuid.uuid4().hex, code_revision=revision)
        save_run(run, output / "inputs")
        # Answers injected separately, never included in the request producer.
        choices = json.loads((data / case["fixture"]).read_bytes())["responses"]
        config = Config(mode="all", output_protocol="pilot-review-block-v2", prompt_version=presign.PROMPT_VERSION)
        prepared = {t.task_id: presign.request(run, t, config) for t in run.checklist}
        tasks = {t.task_id: t for t in run.checklist}

        def respond(request):
            payload = json.loads(json.loads(request.content)["messages"][1]["content"])
            tid = payload["task"]["task_id"]
            reply = simulated_reply(run, tasks[tid], prepared[tid], choices[tid.removeprefix("RAG-PILOT-")])
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply)}}],
                                           "usage": {"prompt_tokens": 0, "completion_tokens": 0}})

        def factory(config):
            settings = SparkSettings.model_construct(api_key=SecretStr("SIMULATED-NO-CREDENTIAL"), model=config.model,
                base_url=config.endpoint, timeout=config.timeout, max_completion_tokens=config.per_call_output,
                thinking=config.thinking, retries=0)
            return SparkChat(settings, transport=httpx.MockTransport(respond))

        folder, report = execute(run, config, output / "executions", factory)
        summaries.append({"case_id": case["case_id"], "environment": "SIMULATED", "status_counts": report["status_counts"],
            "results": [{"task_id": r["task_id"], "conclusion": r["business_conclusion"], "failure": r["failure"]} for r in report["results"]]})
    evaluation = evaluate_pilot(run_root=output / "executions", inputs_root=output / "inputs", cases_manifest=data / "cases.json")
    write_new(output / "evaluation.json", evaluation)
    (output / "evaluation.md").write_text(render_markdown(evaluation), encoding="utf-8")
    write_new(output / "summary.json", {"environment": "SIMULATED", "business_accuracy_measured": False, "cases": summaries})
    return summaries
