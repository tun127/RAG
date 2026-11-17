import json
from time import monotonic
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from production_rag.agent_review.automatic import parse_doubts
from production_rag.agent_review.transport import StagedModel


def test_http_raw_response_and_stateless_stage_boundary():
    requests = []

    def respond(request):
        data = json.loads(request.content)
        requests.append(data)
        assert len(data["messages"]) == 2
        assert "tools" not in data
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "status": "completed", "questions": [], "limitations": []})}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12},
        })

    settings = SimpleNamespace(retries=0, thinking="disabled", timeout=10,
                               max_completion_tokens=100, model="spark-x2.6",
                               base_url="https://maas-api.cn-huabei-1.xf-yun.com/v2",
                               api_key=SecretStr("SIMULATED-NOT-A-KEY"))
    model = StagedModel(settings, monotonic() + 30, transport=httpx.MockTransport(respond))
    for stage in ("discovery", "generation", "semantic"):
        reply = model(dict(stage=stage, system="fixed constraints", data={}, max_tokens=100))
        parsed = parse_doubts(reply.value, {}, None)
        assert parsed["status"] == "completed"
        assert reply.tokens == 12
    assert len(requests) == 3
    assert len(model.ledger) == 3
