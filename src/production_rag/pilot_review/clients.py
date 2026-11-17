"""Offline default: a marked plumbing fixture, never a semantic reviewer."""
import json

import httpx
from pydantic import SecretStr

from production_rag.adapters.spark_gateway.client import SparkChat
from production_rag.adapters.spark_gateway.settings import SparkSettings
from production_rag.review_execution.clients import spark_client


def fixture_response(payload):
    # Select input source identities only. No access to annotations, paths or case names.
    if payload["protocol"] == "pilot-review-block-v2":
        return fixture_response_v2(payload)
    cited = []
    task = payload["task"]
    for did in dict.fromkeys(s["document_id"] for s in task["sources"]):
        doc = next(d for d in payload["demo_policy_documents"] if d["document_id"] == did)
        heading = next(s["heading"] for s in task["sources"] if s["document_id"] == did)
        block = next(b for b in doc["blocks"] if f"## {heading}\n" in b["text"])
        cited.append({**{k: doc[k] for k in ("source_type", "document_id", "version")},
                      "block_id": block["block_id"], "quote": f"## {heading}\n"})
    return {"conclusion": "needs_review", "reason_code": "condition_unknown",
            "evidence": cited, "material_ids": [], "coverage": None}


def client(config, *, env_file=None):
    if not config.simulation:
        return spark_client(config, env_file=env_file)
    if env_file is not None:
        raise ValueError("env_file_requires_live")

    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        output = fixture_response(payload)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        })

    # Bypass all environment and dotenv sources, even if a real key exists there.
    settings = SparkSettings.model_construct(api_key=SecretStr("OFFLINE-NOT-A-CREDENTIAL"),
        model=config.model, base_url=config.endpoint, timeout=config.timeout,
        max_completion_tokens=config.per_call_output, thinking=config.thinking, retries=0)
    return SparkChat(settings, transport=httpx.MockTransport(respond))


def fixture_response_v2(payload):
    """SIMULATED structural fixture; cannot establish semantic support."""
    cited = []
    for ref in payload["required_policy_evidence"]:
        doc = next(d for d in payload["demo_policy_documents"] if d["document_id"] == ref["document_id"])
        block = next(b for b in doc["blocks"] if f"## {ref['heading']}\n" in b["text"])
        body = next(line for line in block["text"].splitlines() if line.strip() and not line.lstrip().startswith("#"))
        cited.append({**{k: doc[k] for k in ("source_type", "document_id", "version")},
                      "block_id": block["block_id"], "quote": body[:240]})
    missing = payload["missing_task_dependencies"]
    question = "SIMULATED：需人工核实任务触发事实及材料版本是否一致，影响候选判断"
    if missing:
        question += "；缺政策依赖：" + ",".join(missing)
    return {"protocol": "pilot-review-block-v2", "conclusion": "needs_review", "reason_code": "condition_unknown",
            "evidence": cited, "material_refs": [], "requested_material_ids": [], "coverage": None,
            "review_issue": {"question": question, "evidence_indexes": list(range(len(cited)))}}
