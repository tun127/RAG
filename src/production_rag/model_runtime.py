"""Text-only model boundary and explicit server authorization. No secrets at startup."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from production_rag.e2e_support import fixture_client
from production_rag.pilot_review.execution import Config
from production_rag.review_execution.clients import spark_client


class TextModel(Protocol):
    """chat returns final content only; attempts carries known/unknown usage and errors.

    Adapter must honor budget.allowance/remaining_seconds/charge, never return reasoning,
    and raise classified SparkError (the existing adapter error contract). No tools/schema.
    Identity is the frozen provider/model/endpoint in Config, not vendor response text.
    """

    model: str
    attempts: list[dict]

    def chat(self, messages: list[dict[str, str]], budget) -> dict: ...


@dataclass(frozen=True)
class ModelPolicy:
    allow_live: bool = False
    env_file: Path | None = None  # Server-only. Never serialized into run/export.
    max_attempts: int = 6
    total_output: int = 4608
    total_seconds: float = 600
    live_factory: Callable | None = None  # Offline dependency injection, not a page option.

    @classmethod
    def from_environment(cls):
        # Do not read dotenv, SPARK_API_KEY or credential-bearing settings here.
        path = os.environ.get("CONTRACT_SPARK_ENV_FILE")
        if os.environ.get("CONTRACT_ALLOW_LIVE") == "1":
            from production_rag.adapters.spark_gateway.settings import SparkSettings

            if path and not Path(path).is_file():
                raise ValueError("CONTRACT_SPARK_ENV_FILE 指向的配置文件不存在；请填写服务端文件路径")
            try:
                SparkSettings.load(path)
            except ValueError:
                raise ValueError("真实审查配置不可用：检查服务端 SPARK_API_KEY 及 Spark 配置项（环境变量或 CONTRACT_SPARK_ENV_FILE 指定文件）") from None
        return cls(
            allow_live=os.environ.get("CONTRACT_ALLOW_LIVE") == "1",
            env_file=Path(path) if path else None,
        )

    def config(self, mode="SIMULATED", scenario="normal"):
        if mode not in ("SIMULATED", "LIVE"):
            raise ValueError("invalid_execution_mode")
        if mode == "LIVE" and (not self.allow_live or scenario != "normal"):
            raise ValueError("live_not_authorized_or_fixture_requested")
        return Config(
            mode="rag",
            output_protocol="pilot-review-block-v2",
            prompt_version="pilot-presign-findings-v2" if mode == "SIMULATED" else "pilot-presign-v1",
            simulation=mode == "SIMULATED",
            presign_live_allowed=mode == "LIVE" and self.allow_live,
            per_call_output=1024,
            max_attempts=1 if scenario == "budget_after_one" else self.max_attempts,
            total_output=self.total_output,
            total_seconds=self.total_seconds,
        )

    def factory(self, config) -> TextModel:
        if config.simulation:
            return fixture_client(config)
        if not self.allow_live or not config.presign_live_allowed:
            raise ValueError("live_not_authorized")
        if self.live_factory is not None:
            return self.live_factory(config)
        return spark_client(config, env_file=self.env_file)
