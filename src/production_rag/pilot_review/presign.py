"""Explicit pre-signature checklist. Hypotheses are declarations, not verified facts."""
import json
from typing import Literal

from pydantic import Field, model_validator

from production_rag.review_inputs.models import Frozen, digest

from . import context, protocol_v2
from .inputs import Run, Task, prepare, tasks

INPUT_VERSION = "pilot-presign-input-v1"
TASK_VERSION = "pay-acc-exc-presign-1.0"
PROMPT_VERSION = "pilot-presign-v1"
PROMPT = protocol_v2.PROMPT + """
本输入仅签署前合同条款审查，结论只针对拟签条款和签署前例外，不证明已履约。
先逐条比较合同/附件与政策，再核查偏离是否需要例外。未申请例外的声明不证明无偏离。
不要求尚未发生的交付、验收、发票、付款或异常通知记录；不得待补excluded_materials。
required_materials只列核心材料，conditional_materials仅在对应trigger_hypotheses为triggered时启用。
假设是待人工复核的开发输入，不是事实或业务金标；不得声称其可信性已验证。
unknown保持待确认，不推定条件未触发。not_triggered仍须条款比较引文，不能只引未申请声明。
明确条款偏离而无批准仍可提出deviation；compliant不能用缺失或不明的批准条件放行。
EXC-02仅检查拟签版本与签署前条件；未来履约条件仅确认约定，不核验未来完成事实。
缺DP-AUTH不能形成确定性结论；指定政策章节/依赖仍须实际引用。
"""

TRIGGERS = ("payment_deviation", "acceptance_deviation", "exception_needed", "existing_approval",
            "delegated_approval", "signing_condition")


class Basis(Frozen):
    document_id: str
    version: str
    quote: str = Field(min_length=1, max_length=240)


class Hypothesis(Frozen):
    condition: Literal["payment_deviation", "acceptance_deviation", "exception_needed", "existing_approval",
                       "delegated_approval", "signing_condition"]
    state: Literal["unknown", "triggered", "not_triggered"]
    basis_kind: Literal["unassessed", "submission_statement", "clause_comparison", "approval_content"]
    basis: tuple[Basis, ...] = ()
    note: str = Field(min_length=1, max_length=400)
    role: Literal["development_draft_pending_human_review"] = "development_draft_pending_human_review"


class ConditionalMaterial(Frozen):
    condition: str
    material_ids: tuple[str, ...]
    purpose: str


class PresignTask(Task):
    version: Literal["pay-acc-exc-presign-1.0"] = TASK_VERSION
    review_stage: Literal["pre_signature"] = "pre_signature"
    trigger: str | None
    conditional_materials: tuple[ConditionalMaterial, ...]
    excluded_materials: tuple[str, ...] = ("acceptance_records", "settlement_records")
    excluded_performance_evidence: str = "实际交付、验收、发票、付款、异常照片/通知及签署后条件完成事实"


# Curated from unchanged policy sections; never inferred from keywords or gold.
DEFINITIONS = (
    ("签署前付款价款要素、触发约定及30自然日算法、发票与签收/质量验收区分是否明确？", ("size_declaration",), None, ()),
    ("先检查拟签付款条款有无预付、分期、超默认账期或替代材料偏离，再核对相应签署前批准。", (), "payment_deviation", (("payment_deviation", ("approval_record",)),)),
    ("签署前分层验收约定、可实施期限/日历及技术附件版本、标准、方法和确认/冲突顺序是否可检查？", ("technical_spec",), None, (("acceptance_deviation", ("approval_record",)),)),
    ("拟签条款是否约定异常通知、整改复验、延期与对方同意、资源安排和商业批准区别及付款衔接？", (), None, (("acceptance_deviation", ("approval_record",)),)),
    ("先核条款偏离，再核签署前例外申请的版本、保障措施和最终批准权限层级。", (), "exception_needed", (("exception_needed", ("approval_record",)), ("delegated_approval", ("authority_record",)))),
    ("已有例外批准是否覆盖拟签版本，签署前条件是否明确并有证据，实质变化是否需要重审？", (), "existing_approval", (("existing_approval", ("approval_record",)), ("signing_condition", ("condition_records",)))),
)


def presign_tasks(snapshot):
    result = []
    for old, (question, core, trigger, conditional) in zip(tasks(snapshot), DEFINITIONS):
        values = old.model_dump()
        values.update(version=TASK_VERSION, question=question, required_materials=core,
                      applicability="仅签署前普通货物条款；先比较条款，不以未申请声明替代偏离识别",
                      trigger=trigger, conditional_materials=[ConditionalMaterial(condition=c, material_ids=ids,
                          purpose="仅核对已触发的签署前例外/授权/条件，不核验未来履约") for c, ids in conditional])
        result.append(PresignTask(**values))
    return tuple(result)


class PresignRun(Run):
    protocol: Literal["pilot-presign-input-v1"] = INPUT_VERSION
    checklist: tuple[PresignTask, ...]
    review_stage: Literal["pre_signature"] = "pre_signature"
    trigger_hypotheses: tuple[Hypothesis, ...]

    @model_validator(mode="after")
    def verify(self):
        # Validate all original source/snapshot/identity constraints through the legacy contract.
        old = self.model_dump()
        for key in ("review_stage", "trigger_hypotheses"):
            old.pop(key)
        old.update(protocol="pilot-review-block-v1", checklist=[t.model_dump() for t in tasks(self.knowledge)])
        Run.model_validate(old)
        if self.checklist != presign_tasks(self.knowledge):
            raise ValueError("presign_checklist_mismatch")
        if sorted(h.condition for h in self.trigger_hypotheses) != sorted(TRIGGERS):
            raise ValueError("condition_set_mismatch")
        documents = {d.document_id: d for d in (self.contract, *(m.document for m in self.materials if m.document))}
        documents.update({d.document_id: d for d in self.knowledge.documents if d.document_id in self.policy_scope})
        for h in self.trigger_hypotheses:
            roles = set()
            for b in h.basis:
                doc = documents.get(b.document_id)
                if (doc is None or doc.version != b.version or not b.quote.strip() or doc.text.count(b.quote) != 1
                        or not any(line.strip() and not line.lstrip().startswith("#") for line in b.quote.splitlines())):
                    raise ValueError("condition_basis_not_located")
                roles.add("contract" if doc.document_id == self.contract.document_id else
                          "demo_policy" if doc.document_id in self.policy_scope else "material")
            if h.state != "unknown":
                if h.basis_kind == "clause_comparison" and {"contract", "demo_policy"} <= roles:
                    pass
                elif (h.condition in {"existing_approval", "delegated_approval", "signing_condition"}
                      and h.basis_kind == "approval_content" and "material" in roles):
                    pass
                else:
                    raise ValueError("condition_requires_comparison_basis")
        return self


def prepare_presign(*args, trigger_hypotheses=(), **kwargs):
    old = prepare(*args, **kwargs)
    values = old.model_dump()
    values.update(protocol=INPUT_VERSION, checklist=presign_tasks(old.knowledge),
                  trigger_hypotheses=trigger_hypotheses or [Hypothesis(condition=c, state="unknown",
                      basis_kind="unassessed", note="尚未进行条款比较，不能从未申请声明或材料状态推断") for c in TRIGGERS])
    return PresignRun(**values)


def request(run, task, config, provider=None, coverage_complete=True):
    prepared = protocol_v2.request_from_prepared(run, task, context.request(run, task, config, provider=provider))
    payload = json.loads(prepared["messages"][1]["content"])
    payload.update(input_version=INPUT_VERSION, checklist_version=TASK_VERSION, review_stage=run.review_stage,
                   parse_coverage_complete=coverage_complete,
                   review_constraints="仅基于提取文本；解析不完整时禁止全文缺失与absence_candidate，只允许局部证据范围候选" if not coverage_complete else "仍须核实附件与材料范围，不宣称原件全文完整",
                   prompt_version=PROMPT_VERSION,
                   trigger_hypotheses=[h.model_dump(mode="json") for h in run.trigger_hypotheses])
    prepared["messages"] = [{"role": "system", "content": PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)}]
    encoded = json.dumps(prepared["messages"], ensure_ascii=False, sort_keys=True).encode()
    return {**prepared, "prompt_version": PROMPT_VERSION, "input_version": INPUT_VERSION,
            "checklist_version": TASK_VERSION, "request_sha256": digest(encoded), "request_bytes": len(encoded),
            "input_characters": sum(len(m["content"]) for m in prepared["messages"])}


def validate(run, task, prepared, text):
    reply = protocol_v2.ProtocolReply.model_validate_json(text, strict=True)
    hypotheses = {h.condition: h for h in run.trigger_hypotheses}
    allowed = set(task.required_materials)
    for rule in task.conditional_materials:
        if hypotheses[rule.condition].state == "triggered":
            allowed.update(rule.material_ids)
    if set(reply.requested_material_ids) - allowed:
        raise ValueError("presign_material_request_out_of_scope")
    if task.trigger:
        state = hypotheses[task.trigger].state
        if state == "unknown" and reply.conclusion != "needs_review":
            raise ValueError("presign_trigger_unknown")
        if state == "not_triggered" and reply.conclusion not in {"not_applicable", "needs_review"}:
            raise ValueError("presign_condition_not_triggered")
        if state == "triggered" and reply.conclusion == "not_applicable":
            raise ValueError("presign_condition_triggered")
    elif reply.conclusion == "not_applicable":
        raise ValueError("presign_base_task_applicable")
    if reply.conclusion == "compliant":
        if any(hypotheses[r.condition].state == "unknown" for r in task.conditional_materials):
            raise ValueError("presign_conditional_requirement_unknown")
        materials = {m.material_id: m for m in run.materials}
        if any(mid not in materials or materials[mid].status != "provided" for mid in allowed):
            raise ValueError("presign_required_material_incomplete")
    checked = protocol_v2.validate(run, task, prepared, text)
    return {**checked, "input_version": INPUT_VERSION, "checklist_version": TASK_VERSION,
            "review_stage": "pre_signature", "condition_gate_basis": "declared_hypotheses_not_verified_facts",
            "conditional_materials_active": sorted(allowed - set(task.required_materials))}
