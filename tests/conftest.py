"""仅测试使用确定性替身；产品默认路径始终调用真实 Ollama。"""

import json
from hashlib import sha256

import pytest

from production_rag.bootstrap import build_services
from production_rag.config import Settings


class FakeEmbedder:
    def digest(self):
        return "test-embedding-v1"

    def embed(self, texts, timeout=120):
        return [[(b - 128) / 128 for b in sha256(t.encode()).digest()[:4]] for t in texts]


class ScriptedLLM:
    model = "scripted-test-only"

    def __init__(self):
        self.calls = []
        self.script = []

    def chat(self, messages, budget, *, tools=None, schema=None):
        budget.allowance()
        budget.charge({"eval_count": 10, "prompt_eval_count": 20})
        self.calls.append({"messages": messages.copy(), "tools": tools, "schema": schema})
        if self.script:
            next_item = self.script.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item
        if tools:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_knowledge",
                            "arguments": {"query": "健康检查 /health"},
                        }
                    }
                ],
            }
        if "sufficient" in schema.get("properties", {}):
            return {
                "role": "assistant",
                "content": json.dumps({"sufficient": True, "missing": [], "next_query": ""}),
            }
        evidence = json.loads(messages[-1]["content"])["evidence"]
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "status": "answered",
                    "text": evidence[0]["text"],
                    "citation_chunk_ids": [evidence[0]["chunk_id"]],
                }
            ),
        }


@pytest.fixture
def services(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "health.md").write_text(
        "# 健康检查\n健康检查接口 GET /health，失败时检查数据库连接。", encoding="utf-8"
    )
    (root / "api.md").write_text(
        "# 接口\nPOST /query 接收用户问题，返回答案和引用。", encoding="utf-8"
    )
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "state",
        document_root=root,
        vector_backend="sqlite",
        embedding_dim=4,
        context_chars=2000,
    )
    return build_services(settings, embedder=FakeEmbedder(), llm=ScriptedLLM())
