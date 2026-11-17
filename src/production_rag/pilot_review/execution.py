import uuid
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from production_rag.adapters.spark_gateway.client import SparkError
from production_rag.domain.errors import BudgetExceeded
from production_rag.provenance import code_revision
from production_rag.review_execution.core import ExecutionBudget, safe_attempts
from production_rag.review_inputs.models import Frozen, digest

from . import findings_v2, presign, protocol_v2
from .context import PROMPT, PROMPT_VERSION, PROTOCOL, request
from .inputs import Run, write_new
from .validation import validate


class Config(Frozen):
    review_strategy: Literal['rag', 'agentic'] = 'rag'
    integration_enabled: bool = False
    simulated_case: str = 'no_doubt'
    agent_output: int = Field(default=3072, ge=0, le=18432)
    bounded_live_validation: bool = False
    agent_check_ids: tuple[str, ...] | None = None
    agent_stage_caps: dict[str, int] = Field(default_factory=lambda: dict(discovery=128, generation=1024, semantic=1024))
    semantic_protocol: Literal['fields-v1', 'scoped-v1'] = 'fields-v1'
    mode: Literal["all", "rag"]
    top_k: int = Field(default=3, ge=1, le=30)
    provider: Literal["spark"] = "spark"
    model: Literal["spark-x2.6"] = "spark-x2.6"
    endpoint: Literal["https://maas-api.cn-huabei-1.xf-yun.com/v2"] = "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    prompt_version: Literal["pilot-pay-acc-exc-v2", "pilot-pay-acc-exc-protocol-v3", "pilot-presign-v1", "pilot-presign-findings-v2", "pilot-presign-findings-v3"] = PROMPT_VERSION
    output_protocol: Literal["pilot-review-block-v1", "pilot-review-block-v2", "pilot-presign-findings-v3"] = PROTOCOL
    thinking: Literal["disabled"] = "disabled"
    timeout: float = Field(default=60, gt=0, le=300)
    retries: Literal[0] = 0
    per_call_output: int = Field(default=768, ge=1, le=1024, strict=True)
    total_output: int = Field(default=4608, ge=1, le=4608)
    total_seconds: float = Field(default=600, gt=0, le=600)
    max_attempts: int = Field(default=6, ge=1, le=6)
    max_input_bytes: int = Field(default=64000, ge=1, le=128000)
    simulation: bool = True
    presign_live_allowed: bool = False


    @model_validator(mode="after")
    def versions(self):
        if self.bounded_live_validation and (
            self.agent_check_ids != ('RAG-PILOT-PAY-01',) or self.agent_output != 3072
            or self.total_output != 4608 or self.total_seconds != 600 or self.retries != 0
            or self.agent_stage_caps != dict(discovery=512, generation=896, semantic=1664)
            or self.semantic_protocol != 'scoped-v1' or self.review_strategy != 'agentic'
        ):
            raise ValueError('bounded_live_config_mismatch')
        if self.output_protocol == findings_v2.VERSION_V3 or self.prompt_version == findings_v2.VERSION_V3:
            if self.output_protocol != findings_v2.VERSION_V3 or self.prompt_version != findings_v2.VERSION_V3:
                raise ValueError("protocol_prompt_version_mismatch")
            return self
        expected = protocol_v2.PROMPT_VERSION if self.output_protocol == protocol_v2.PROTOCOL else PROMPT_VERSION
        if self.prompt_version != expected and not (self.output_protocol == protocol_v2.PROTOCOL and self.prompt_version in (presign.PROMPT_VERSION, findings_v2.VERSION, findings_v2.VERSION_V3)):
            raise ValueError("protocol_prompt_version_mismatch")
        return self


def execute(run: Run, config: Config, root: Path, client_factory, provider=None, progress=None,
            coverage_complete=True, deadline=None, clock=monotonic, shared_pool=None):
    deadline = deadline if deadline is not None else clock() + config.total_seconds
    is_presign = isinstance(run, presign.PresignRun)
    run = (presign.PresignRun if is_presign else Run).model_validate_json(run.model_dump_json())
    config = Config.model_validate_json(config.model_dump_json())
    if is_presign != (config.prompt_version in (presign.PROMPT_VERSION, findings_v2.VERSION, findings_v2.VERSION_V3)):
        raise ValueError("checklist_prompt_mismatch")
    if is_presign and not config.simulation and not config.presign_live_allowed:
        raise ValueError("presign_offline_only")
    compact = config.prompt_version == findings_v2.VERSION_V3
    new_findings = config.prompt_version in (findings_v2.VERSION, findings_v2.VERSION_V3)
    if new_findings and not config.simulation and not config.bounded_live_validation:
        raise ValueError("findings_v2_offline_only")
    is_v2 = config.output_protocol == protocol_v2.PROTOCOL
    prompt = protocol_v2.PROMPT if is_v2 else PROMPT
    validator = protocol_v2.validate if is_v2 else validate
    if is_presign:
        prompt, validator = presign.PROMPT, presign.validate
    if new_findings:
        prompt, validator = findings_v2.PROMPT, findings_v2.validate
    if compact:
        prompt, validator = findings_v2.PROMPT_V3, findings_v2.validate_v3
    plan = findings_v2.budget_plan(run, config) if compact else None
    # Fixed equal caps reserve every slot before any call. No late squeezed request.
    if new_findings and not compact:
        slot_cap = min(config.per_call_output, config.total_output // len(run.checklist))
        config = config.model_copy(update={"per_call_output": slot_cap})
    folder = root / run.run_id / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    implementation_sha256 = digest(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py"))))
    execution_revision = code_revision(Path(__file__).parent, simulation=config.simulation)
    write_new(folder / "execution-input.json", {
        "run_id": run.run_id, "input_sha256": run.input_sha256, "knowledge_id": run.knowledge_id,
        "code_revision": run.code_revision, "config": config.model_dump(),
        "execution_revision": execution_revision, "implementation_sha256": implementation_sha256,
        "prompt_sha256": digest(prompt.encode()), "simulation": config.simulation,
        "parse_coverage_complete": coverage_complete,
        "output_reservation": plan,
        "deadline_scope": "retrieval_supplement_generation_shared",
        "slots": [{"task_id": t.task_id, "status": "pending"} for t in run.checklist],
    })
    client = None
    budget = None
    stop = "budget_stop" if (compact and not plan["fits"]) or (new_findings and not compact and config.per_call_output < 768) else None
    results = []
    for task in run.checklist:
        if compact:
            prepared = findings_v2.request_v3(run, task, config, provider=provider, coverage_complete=coverage_complete)
        elif new_findings:
            prepared = findings_v2.request(run, task, config, provider=provider, coverage_complete=coverage_complete)
        elif is_presign:
            prepared = presign.request(run, task, config, provider=provider, coverage_complete=coverage_complete)
        else:
            prepared = request(run, task, config, provider=provider)
        if is_v2 and not is_presign:
            prepared = protocol_v2.request_from_prepared(run, task, prepared)
        write_new(folder / f"{task.task_id}-request.json", prepared)
        row = {"task_id": task.task_id, "request_sha256": prepared["request_sha256"],
               "request_bytes": prepared["request_bytes"], "input_characters": prepared["input_characters"],
               "execution_status": "not_executed", "failure": None,
               "business_conclusion": None, "manual_review_required": True,
               "output_protocol": config.output_protocol, "prompt_version": config.prompt_version,
               "simulation_label": "SIMULATED" if config.simulation else "LIVE",
               "material_state": [{"material_id": m.material_id, "status": m.status, "statement": m.statement}
                                  for m in run.materials],
               "undeclared_required_materials": sorted(set(task.required_materials) - {m.material_id for m in run.materials}),
               "missing_task_dependencies": prepared["retrieval"]["missing_task_dependencies"],
               "returned_policy_ids": [c["document_id"] for c in prepared["retrieval"]["contexts"]],
               "retrieved_unit_ids": [h["unit_id"] for h in prepared["retrieval"]["hits"]],
               "returned_policy_characters": prepared["retrieval"]["context_characters"],
               "attempts": [], "validation": None, "final_answer": None}
        try:
            if clock() >= deadline:
                row['failure'] = 'run_deadline'
                stop = 'run_deadline'
            elif stop:
                row["failure"] = stop
            elif prepared["request_bytes"] > config.max_input_bytes:
                row["failure"] = "context_limit_no_truncation"
            elif not compact and not prepared["retrieval"]["hits"]:
                row["failure"] = "retrieval_empty_not_a_business_verdict"
            elif not compact and prepared["retrieval"]["missing_primary_policies"]:
                row["failure"] = "primary_policy_unreturned"
            else:
                if client is None:
                    remaining = deadline - clock()
                    client = client_factory(config.model_copy(update={'timeout': min(config.timeout, remaining), 'total_seconds': remaining}))
                    budget = ExecutionBudget(config, client) if shared_pool is None else None
                if shared_pool is not None:
                    from production_rag.agent_review.product_boundary import FirstBudget
                    budget = FirstBudget(shared_pool, task.task_id, config, client, deadline, clock)
                if clock() >= deadline:
                    raise BudgetExceeded('run_deadline')
                # Adapter's own remaining_seconds is bounded by the shared deadline.
                original_remaining = budget.remaining_seconds
                budget.remaining_seconds = lambda fn=original_remaining: min(fn(), max(0, deadline-clock()))
                if compact:
                    cap = next(slot["cap"] for slot in plan["slots"] if slot["task_id"] == task.task_id)
                    budget.config = config.model_copy(update={"per_call_output": cap})
                    budget.base.per_call_cap = cap
                    if budget.allowance() < cap:
                        raise BudgetExceeded("reserved_slot_insufficient")
                budget.allowance()
                row["execution_status"] = "failed"
                response = client.chat(prepared["messages"], budget)
                row["final_answer"] = response["content"]
                checked = validator(run, task, prepared, response["content"])
                if not coverage_complete and checked['reply']['reason_code'] == 'absence_candidate':
                    raise ValueError('parse_coverage_blocks_absence_candidate')
                row["validation"] = checked
                row["failure"] = checked["failure"]
                if checked["valid"]:
                    row["execution_status"] = "success"
                    row["business_conclusion"] = checked["reply"]["conclusion"]
        except BudgetExceeded:
            row["failure"] = "run_deadline" if clock() >= deadline else "budget_stop"
            stop = row['failure']
        except SparkError as exc:
            if exc.kind == "truncated":
                row["final_answer"] = exc.final_answer
            category = exc.kind if exc.kind in {"authentication", "rate_limit", "server", "http_error",
                "business_error", "timeout", "transport", "usage_unknown", "truncated", "deadline",
                "reported_output_over_limit", "invalid_response", "invalid_json", "unsupported_finish", "empty_content"} else "model_error"
            row["failure"] = category
            stop = "stopped_after_api_or_usage_failure"
        except ValidationError:
            row["failure"] = "reply_schema_invalid"
        except ValueError as exc:
            allowed = {"task_policy_evidence_required", "task_section_evidence_required",
                       "contract_fact_evidence_required", "required_policy_dependency_unreturned",
                       "dependency_policy_evidence_required", "required_material_facts_incomplete",
                       "material_basis_unverified", "unknown_or_duplicate_material", "conclusion_reason_mismatch",
                       "incomplete_contract_coverage_claim", "unexpected_coverage", "source_map_mismatch",
                       "requested_material_not_referenced", "unexpected_material_request", "review_issue_required",
                       "invalid_issue_evidence", "unexpected_review_issue", "task_section_body_evidence_required",
                       "issue_body_evidence_required", "presign_material_request_out_of_scope",
                       "presign_trigger_unknown", "presign_condition_not_triggered", "presign_condition_triggered",
                       "presign_base_task_applicable", "presign_conditional_requirement_unknown",
                       "presign_required_material_incomplete"}
            allowed.add('parse_coverage_blocks_absence_candidate')
            if compact:
                allowed.update(findings_v2.DIAGNOSTICS_V3)
            row["failure"] = str(exc) if str(exc) in allowed else "invalid_response_or_configuration"
            if client is None:
                stop = "client_configuration_failed"
                if not config.simulation:
                    row['failure'] = 'model_configuration_unavailable'
        except Exception:
            # Do not interpolate adapter/Pydantic messages or vendor payloads.
            row["failure"] = "invalid_response_or_configuration"
            if client is None:
                stop = "client_configuration_failed"
                if not config.simulation:
                    row['failure'] = 'model_configuration_unavailable'
        finally:
            if client is not None:
                row["attempts"] = safe_attempts(client.attempts)
                budget.finish_call()
        results.append(row)
        write_new(folder / f"{task.task_id}-result.json", row)
        if progress:
            progress(results)
    attempts = [a for r in results for a in r["attempts"]]
    summary = {"run_id": run.run_id, "input_sha256": run.input_sha256,
               "execution_revision": execution_revision, "implementation_sha256": implementation_sha256,
               "knowledge_id": run.knowledge_id, "config": config.model_dump(),
               "simulation": config.simulation, "real_model_called": not config.simulation and bool(attempts),
               "model_evaluation_claimed": False, "results": results,
               "stop_reason": stop,
               "attempt_count": len(attempts), "unknown_usage_attempts": sum(not a["usage_known"] for a in attempts),
               "known_usage": budget.base.usage() if budget else {"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
               "unknown_output_reserved": budget.reserved if budget else 0,
               "status_counts": {s: sum(r["execution_status"] == s for r in results)
                                 for s in ("success", "failed", "not_executed")}}
    write_new(folder / "report.json", summary)
    return folder, summary
