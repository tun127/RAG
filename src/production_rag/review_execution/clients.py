"""Explicit Spark construction; offline simulation never reads credentials."""
import json
from pathlib import Path

import httpx
from pydantic import SecretStr

from production_rag.adapters.spark_gateway.client import SparkChat
from production_rag.adapters.spark_gateway.settings import SparkSettings


def spark_client(config, *, env_file: Path | None = None):
    if config.simulation and env_file is not None:
        raise ValueError("env_file_requires_live")
    fixed = dict(
        model=config.model, base_url=config.endpoint, timeout=config.timeout,
        max_completion_tokens=config.per_call_output, thinking=config.thinking,
        retries=config.retries,
    )
    transport = None
    if config.simulation:
        key = "simulation-no-credential"

        def simulate(request):
            # A plumbing fixture, not a prediction or reference answer.
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                    "conclusion": "needs_review", "reason": "SIMULATED plumbing fixture only",
                    "evidence": [], "basis": "full_supplier_text", "material_ids": [],
                })}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            })

        transport = httpx.MockTransport(simulate)
    else:
        # BaseSettings silently ignores missing dotenv files; explicit selection
        # must instead fail, even if a credential exists in the environment.
        if env_file is not None and not env_file.is_file():
            raise ValueError("env_file_unavailable")
        settings = SparkSettings.load(env_file, **fixed)
        return SparkChat(settings)
    # Simulation bypasses all environment/file sources, including BaseSettings.
    settings = SparkSettings.model_construct(api_key=SecretStr(key), **fixed)
    return SparkChat(settings, transport=transport)
