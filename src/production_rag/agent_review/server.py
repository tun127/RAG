"""Single explicit offline product entry; no credential configuration loading."""
import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from production_rag.adapters.spark_gateway.client import SparkChat
from production_rag.model_runtime import ModelPolicy
from production_rag.pilot_review.execution import Config
from production_rag.pilot_review.inputs import write_new
from production_rag.review_execution.clients import spark_client
from production_rag.review_inputs.models import digest

from .offline_http import settings, transport
from .transport import StagedModel


@dataclass(frozen=True)
class IntegrationPolicy(ModelPolicy):
    strategy: str = 'rag'
    case: str = 'no_doubt'
    extra_output: int = 3072

    def config(self, mode='SIMULATED', scenario='normal'):
        if mode != 'SIMULATED' or self.strategy not in ('rag', 'agentic'):
            raise ValueError('explicit_offline_strategy_required')
        return super().config(mode, scenario).model_copy(update=dict(
            prompt_version='pilot-presign-findings-v3', output_protocol='pilot-presign-findings-v3',
            integration_enabled=True, review_strategy=self.strategy, agent_output=self.extra_output,
            simulated_case=self.case))

    def factory(self, config):
        if not config.simulation:
            raise ValueError('offline_only')
        return SparkChat(settings(config.per_call_output), transport=transport(self.case))


def implementation_digest():
    root = Path(__file__).resolve().parents[1]
    paths = sorted([*Path(__file__).parent.glob('*.py'), * (root/'pilot_review').glob('*.py'),
                    root/'e2e.py', root/'model_runtime.py', root/'review_execution/clients.py',
                    root/'adapters/spark_gateway/client.py'])
    return digest(b''.join(str(p.relative_to(root)).encode()+p.read_bytes() for p in paths))


@dataclass(frozen=True)
class LiveIntegrationPolicy(IntegrationPolicy):
    """One explicitly authorized frozen product run; no fallback or startup requests."""
    runtime: Path | None = None
    freeze: Path | None = None

    def config(self, mode='LIVE', scenario='normal'):
        if mode != 'LIVE' or not self.allow_live or scenario != 'normal' or not self.freeze:
            raise ValueError('explicit_frozen_live_required')
        frozen = json.loads(self.freeze.read_bytes())
        config = Config.model_validate(frozen['config'])
        if config.simulation or not config.bounded_live_validation:
            raise ValueError('frozen_live_configuration_invalid')
        return config

    def claim_live_run(self, run):
        frozen = json.loads(self.freeze.read_bytes())
        if (digest(run.contract.text.encode()) != frozen['extracted_contract_sha256']
                or run.knowledge_id != frozen['knowledge_id']
                or [t.task_id for t in run.checklist] != frozen['check_ids']
                or implementation_digest() != frozen['implementation_sha256']):
            raise HTTPException(409, '上传合同、政策、检查或实现与冻结记录不符；没有调用模型')
        try:
            write_new(self.runtime/'live-claimed.json', dict(run_id=run.run_id,
                freeze_sha256=digest(self.freeze.read_bytes()), contract_sha256=run.contract.text_sha256))
        except FileExistsError:
            raise HTTPException(409, '本次有限真实验证已提交，不允许新运行或补跑') from None

    def factory(self, config):
        if config.simulation or not config.bounded_live_validation:
            raise ValueError('live_transport_requires_frozen_config')
        return spark_client(config, env_file=self.env_file)

    def agent_model(self, deadline):
        config = self.config()
        # Existing loader/transport, forced zero retries and no thinking/tools.
        client = spark_client(config.model_copy(update={'per_call_output': max(config.agent_stage_caps.values())}),
                              env_file=self.env_file)
        return StagedModel(client.settings, deadline, transport=client.transport)
