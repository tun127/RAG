import json
import re

from fastapi.testclient import TestClient
from test_contract_parser import docx, paragraph

from production_rag.adapters.spark_gateway.settings import SparkSettings
from production_rag.e2e import create_app


def test_http_complete_flow(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("credential loading forbidden")

    monkeypatch.setattr(SparkSettings, "load", forbidden)
    app = create_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/contracts",
            files={
                "file": ("demo.docx", docx(paragraph("采购合同：货物验收后付款，例外批准待核实。")))
            },
        )
        assert r.status_code == 201
        record = r.json()
        task, version = record["task_id"], record["version_id"]
        select = c.get(f"/select/{task}/{version}")
        token = re.search('name="token" value="([^"]+)"', select.text)[1]
        policy = re.search("<option>([a-f0-9]{64})</option>", select.text)[1]
        result = c.post(
            "/start",
            data={
                "task": task,
                "version": version,
                "policy": policy,
                "stage": "签署前（现有六项试点）",
                "submission_id": re.search(
                    'name="submission_id" value="([^"]+)"',
                    select.text if hasattr(select, "text") else select,
                )[1],
                "token": token,
            },
        )
        assert result.status_code == 200, result.text
        run_id = str(result.url).split("/")[-1]
        exported = c.get("/export/" + run_id).json()
        assert len(exported["report"]["results"]) == 6
        tid = exported["report"]["results"][0]["task_id"]
        assert c.get(f"/evidence/{run_id}/{tid}").status_code == 200
        assert (
            c.post(
                f"/opinion/{run_id}/{tid}",
                data={
                    "token": token,
                    "decision": "待复核",
                    "reviewer": "OFFLINE-TEST-REVIEWER",
                    "note": "需核实付款条件",
                },
            ).status_code
            == 200
        )
        assert c.get("/export/" + run_id).json()["human_opinions"][0]["note"] == "需核实付款条件"
        assert c.get("/print/" + run_id).status_code == 200
        assert c.get("/").status_code == 200
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/export/" + run_id).json()["report"] == exported["report"]


def test_rejected_upload_visible(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        r = c.post("/api/contracts", files={"file": ("wrong.txt", b"unsupported")})
        assert r.json()["result"]["status"] == "rejected"


def test_graph_supplement_budget_and_unavailable(tmp_path):
    import uuid

    from production_rag.e2e import ROOT, run_graph
    from production_rag.knowledge_baseline.snapshot import build
    from production_rag.pilot_review.execution import Config
    from production_rag.pilot_review.inputs import prepare
    from production_rag.pilot_review.providers import provider_for

    knowledge = build(ROOT / "data/contract-knowledge-candidate", mode="policies15")
    run = prepare(
        ROOT / "data/pilot-review-dev/inputs/C001/contract.md",
        knowledge,
        run_id=uuid.uuid4().hex,
        code_revision="c6b5031" + "0" * 33,
    )

    class Stub:
        def __init__(self, progress):
            self.progress = progress

        def retrieve(self, snapshot, query, **kw):
            result = provider_for("all").retrieve(
                snapshot, query, sources="policies", document_ids=run.policy_scope
            )
            if kw["top_k"] != 30 or not self.progress:
                result["contexts"] = [c for c in result["contexts"] if c["document_id"] == "DP-PAY"]
                result["hits"] = [h for h in result["hits"] if h["document_id"] == "DP-PAY"]
            return result

    cfg = Config(
        mode="rag",
        output_protocol="pilot-review-block-v2",
        prompt_version="pilot-pay-acc-exc-protocol-v3",
        per_call_output=1024,
        total_output=1,
        max_attempts=1,
    )
    for success in (True, False):
        folder, report = run_graph(run, cfg, tmp_path, provider=Stub(success))
        trace = json.loads((folder / "graph-trace.json").read_bytes())
        assert any(
            t.get("outcome") == ("new_evidence" if success else "no_progress_stop") for t in trace
        )
        assert len(report["results"]) == 6
        if success:
            assert any(r["failure"] == "budget_stop" for r in report["results"])

    def unavailable(config):
        raise ValueError("unavailable")

    _, report = run_graph(run, cfg, tmp_path, factory=unavailable, provider=Stub(True))
    assert report["results"][0]["failure"] == "invalid_response_or_configuration"
    assert any(r["failure"] == "client_configuration_failed" for r in report["results"][1:])
