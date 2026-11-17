"""Offline attachment bridges and controlled acceptance fixture, no answer loading."""

import json
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from production_rag.adapters.spark_gateway.client import SparkChat
from production_rag.adapters.spark_gateway.settings import SparkSettings
from production_rag.contracts.alignment import text
from production_rag.pilot_review.clients import fixture_response_v2
from production_rag.pilot_review.inputs import Material, source, write_new
from production_rag.review_inputs.models import digest

MATERIALS = {
    "size_declaration": "规模声明",
    "technical_spec": "技术附件",
    "approval_record": "签署前批准",
    "authority_record": "授权文件",
    "condition_records": "签署前条件证据",
}
FIXTURE_VERSION = "payment-source-bound-v2"


def material_form():
    fields = "<h2>材料声明与附件</h2><p>触发条件默认 unknown，均为待复核声明；无需先判断全部条件。不索取未来实际验收记录。provided 必须上传固定文件。DOCX/文本PDF沿用解析限制；TXT/MD仅接受UTF-8固定文本。</p>"
    for mid, label in MATERIALS.items():
        fields += f'<fieldset><legend>{label}</legend><select name="status_{mid}"><option>unknown</option><option>provided</option><option>missing</option><option>not_applicable</option></select><input name="statement_{mid}" placeholder="声明与限制" maxlength="1000"><input type="file" name="file_{mid}" accept=".txt,.md,.docx,.pdf"></fieldset>'
    return (
        fields
        + '<p>离线验收配置（仅本次运行生效）：<select name="scenario"><option value="normal">正常模拟</option><option value="payment_quote_v1">受控付款引用夹具 v1</option><option value="budget_after_one">首项完成后预算停止</option><option value="crash_after_one">首项保存后编排异常</option></select></p>'
    )


async def material_inputs(form, service, runtime):
    materials, mappings = [], []
    for mid in MATERIALS:
        status = str(form.get("status_" + mid, "unknown"))
        if status not in ("provided", "missing", "unknown", "not_applicable"):
            raise HTTPException(400, {"message": "材料状态无效", "field": "status_" + mid})
        statement = (
            str(form.get("statement_" + mid, "")).strip() or "提交人声明；内容及适用性待人工复核"
        )
        file = form.get("file_" + mid)
        has_file = file is not None and bool(getattr(file, "filename", None))
        if (status == "provided") != has_file:
            raise HTTPException(
                400,
                {
                    "message": MATERIALS[mid] + "：provided必须有文件，其他状态不得附文件",
                    "field": "file_" + mid,
                },
            )
        doc = None
        if has_file:
            raw = await file.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise HTTPException(413, {"message": "材料文件超过512 KiB", "field": "file_" + mid})
            suffix = Path(file.filename).suffix.lower()
            mapping = {
                "material_id": mid,
                "source_sha256": digest(raw),
                "mapping_version": "material-bridge-v1",
            }
            if suffix in (".txt", ".md"):
                try:
                    fixed = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise HTTPException(
                        400, {"message": "文本附件必须UTF-8", "field": "file_" + mid}
                    ) from None
                if "\x00" in fixed or len(raw) > 64000:
                    raise HTTPException(
                        400, {"message": "文本附件不受支持或超出64KB", "field": "file_" + mid}
                    )
                path = runtime / "materials" / uuid.uuid4().hex / ("source" + suffix)
                path.parent.mkdir(parents=True)
                with path.open("xb") as stream:
                    stream.write(raw)
                mapping.update(
                    original=str(path.relative_to(runtime)),
                    coverage_complete=True,
                    parser_version="exact-utf8-v1",
                )
            elif suffix in (".docx", ".pdf"):
                record = await run_in_threadpool(service.upload, raw, file.filename)
                write_new(
                    runtime / "document-roles" / record.task_id / "material.json",
                    {"document_role": "material", "material_id": mid, "task_id": record.task_id},
                )
                if record.result.status not in ("parsed", "partial"):
                    raise HTTPException(
                        400, {"message": "附件解析失败，不能声明provided", "field": "file_" + mid}
                    )
                fixed, spans = "", []
                for block in record.result.blocks:
                    value = text(block)
                    if value:
                        start = len(fixed)
                        fixed += value + "\n\n"
                        spans.append(
                            {"start": start, "end": start + len(value), "anchor": block.anchor}
                        )
                mapping.update(
                    parse=record.model_dump(mode="json"),
                    coverage_complete=record.result.coverage_complete,
                    mapping=spans,
                )
            else:
                raise HTTPException(
                    400, {"message": "附件仅支持TXT/MD/DOCX/文本PDF", "field": "file_" + mid}
                )
            if not fixed.strip():
                raise HTTPException(400, {"message": "附件无可用固定文本", "field": "file_" + mid})
            encoded = fixed.encode()
            doc = source(encoded, "material-" + mid, digest(encoded), "material")
            mapping.update(
                document_id=doc.document_id, version=doc.version, text_sha256=doc.text_sha256
            )
            mappings.append(mapping)
        materials.append(
            Material(material_id=mid, status=status, statement=statement, document=doc)
        )
    return tuple(materials), mappings


def controlled_reply(payload):
    if payload.get("protocol") == "pilot-presign-findings-v3":
        return compact_fixture_reply(payload)
    reply = fixture_response_v2(payload)
    if payload.get("business_definition_version") == "pilot-presign-findings-v2":
        reply.update(
            contract_fact="SIMULATED：未进行合同语义判断",
            enterprise_requirement="按本项固定政策来源核对",
            comparison_reason="离线替身仅验证程序行为，不能判断条款是否正确",
            recommendation="人工逐项核对合同事实及政策，真实对照另行安排",
            pending_confirmation="合同事实、附件内容和审批均待具体核实",
            approval_status="trigger_unknown",
            approval_evidence_indexes=[],
        )
    # A technical fixture must not present one payment excerpt as support for every task.
    if not payload["task"]["task_id"].endswith("PAY-01"):
        return reply
    contract = next(
        d for d in payload["contract_and_material_documents"] if d["source_type"] == "contract"
    )
    block = next((b for b in contract["blocks"] if "自然日" in b["text"]), contract["blocks"][0])
    # A contiguous source slice only. No expected classification or development answer is read.
    position = block["text"].find("自然日")
    start = max(0, position - 80) if position >= 0 else 0
    quote = block["text"][start : start + 200].strip()
    reply["evidence"].append(
        {
            **{k: contract[k] for k in ("source_type", "document_id", "version")},
            "block_id": block["block_id"],
            "quote": quote,
        }
    )
    return reply


def fixture_client(config):
    if not config.simulation:
        raise ValueError("offline_fixture_only")

    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(controlled_reply(payload), ensure_ascii=False)
                        },
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
        )

    settings = SparkSettings.model_construct(
        api_key=SecretStr("OFFLINE-NO-CREDENTIAL"),
        model=config.model,
        base_url=config.endpoint,
        timeout=config.timeout,
        max_completion_tokens=config.per_call_output,
        thinking=config.thinking,
        retries=0,
    )
    return SparkChat(settings, transport=httpx.MockTransport(respond))


def partial_report(runtime, pending, state, failure):
    job = runtime / "jobs" / pending["run_id"]
    paths = sorted(job.glob("progress-*.json"), key=lambda p: int(p.stem.split("-")[-1]))
    rows = []
    for path in reversed(paths):
        try:
            rows = json.loads(path.read_bytes())
            break
        except (OSError, json.JSONDecodeError):
            # A concurrent poll may see the file while it is being written.
            continue
    from production_rag.pilot_review.inputs import load_run

    run = load_run(runtime / "inputs" / pending["run_id"] / "input.json")
    completed = {r["task_id"] for r in rows}
    reason = (
        failure.get("category", "orchestration_failed")
        if failure
        else ("pending" if state == "running" else "process_state_unconfirmed")
    )
    rows += [
        {
            "task_id": t.task_id,
            "execution_status": "not_executed",
            "failure": reason,
            "business_conclusion": None,
            "manual_review_required": True,
        }
        for t in run.checklist
        if t.task_id not in completed
    ]
    executions = list((runtime / "executions" / run.run_id).glob("*"))
    recovered = {}
    if executions:
        folder = executions[0]
        candidates = sorted(folder.glob('selected-progress-*.json'),
                            key=lambda p: int(p.stem.rsplit('-', 1)[1]), reverse=True)
        for path in candidates:
            try:
                recovered = json.loads(path.read_bytes())
                rows = recovered['results']
                break
            except (OSError, ValueError, KeyError):
                continue
        audits = recovered.setdefault('agent_reviews', [])
        for task in run.checklist:
            if any(a['task_id'] == task.task_id for a in audits):
                continue
            checkpoints = sorted(folder.glob(task.task_id + '-agent-stage-*.json'), reverse=True)
            for path in checkpoints:
                try:
                    audit = json.loads(path.read_bytes())
                    audit.update(task_id=task.task_id, label='复核未完成', intervened=True,
                                 candidate_v3_validation=None)
                    if audit.get('budget_snapshot'):
                        recovered['budget'] = audit['budget_snapshot']
                        recovered['budget']['settlement_status'] = 'interrupted_unsettled'
                        recovered['budget']['unsettled_stage_usage'] = audit['usage']
                    audits.append(audit)
                    next(r for r in rows if r['task_id'] == task.task_id)['agent'] = audit
                    break
                except (OSError, ValueError, KeyError):
                    continue
    return {
        **pending,
        "overall_status": state,
        "failure": failure,
        "limit": pending["bridge"]["limit"],
        "input_version": run.protocol,
        "checklist_version": run.checklist[0].version,
        "trigger_hypotheses": [h.model_dump(mode="json") for h in run.trigger_hypotheses],
        "execution": str(executions[0].relative_to(runtime)) if executions else None,
        "report": {
            **recovered,
            "run_id": run.run_id,
            "input_sha256": run.input_sha256,
            "simulation": pending.get("config", {}).get("simulation", True),
            "config": pending.get("config", {}),
            "results": rows,
            "status_counts": {
                s: sum(r["execution_status"] == s for r in rows)
                for s in ("success", "failed", "not_executed")
            },
            "coverage_complete": pending["bridge"]["coverage_complete"],
        },
    }


def compact_fixture_reply(payload, case=None):
    """SIMULATED explicit controlled answers; never reads expectations or classifies text."""
    evidence = payload["evidence"]
    contract_ids = [e["id"] for e in evidence if e["source_type"] == "contract"][:1]
    parts = []
    target = {
        "normal": "PAY-01",
        "deviation": "PAY-01",
        "approval-unknown": "PAY-01",
        "attachment-unknown": "ACC-01",
        "undefined-policy": "PAY-02",
        "partial-parse": "ACC-01",
    }.get(case)
    selected = target and payload["task"]["task_id"].endswith(target)
    for part in payload["parts"]:
        ids = contract_ids.copy()
        for uid in part["policy_units"]:
            choice = next(
                (
                    e["id"]
                    for e in evidence
                    if uid in e["policy_units"]
                    and any(
                        line.strip() and not line.startswith("#") for line in e["text"].splitlines()
                    )
                ),
                None,
            )
            if choice and choice not in ids:
                ids.append(choice)
        row = dict(
            part_id=part["part_id"],
            conclusion="needs_review",
            fact="SIMULATED：已提取文本需逐项人工核对",
            requirement="仅以列明的实验政策为依据",
            reason="离线替身不执行语义判断",
            suggestion="人工核对本部分事实与证据",
            pending="相关事实尚待核实",
            evidence_ids=ids,
            limitation="policy_unavailable" if part["missing_policy_ids"] else "fact_unknown",
            fact_scope="observed_text",
        )
        if selected and case in ("normal", "deviation", "approval-unknown"):
            row.update(
                conclusion="compliant",
                fact="付款约定30自然日，价税币种账户及发票单列",
                requirement="默认质量验收及结算资料齐备后30自然日",
                reason="所列付款要素及算法与政策一致",
                suggestion="签署前核对所列账户与触发文本",
                pending="",
                limitation="none",
            )
            if part["part_id"] == "payment_details":
                row.update(
                    fact="含税价10000元、13%税率、人民币，收款主体账户及发票单列",
                    requirement="明确价税币种、收款主体账户及发票关系",
                    reason="所列价款要素及发票关系与政策一致",
                )
            if case != "normal" and part["part_id"] == "payment_algorithm":
                row.update(
                    conclusion="deviation",
                    fact="质量验收及结算资料齐备后60自然日付款",
                    reason="60自然日较政策默认30自然日延长30日",
                    suggestion="改回30自然日，或申请书面商业例外",
                )
        elif selected and case == "attachment-unknown":
            if part["part_id"] == "layers":
                row.update(
                    conclusion="compliant",
                    fact="签收仅核数量外观，2/10工作日分层检验",
                    requirement="数量外观与质量分层，双方书面约定日历",
                    reason="分层、期限及日历的约定与政策一致",
                    suggestion="保留分层和书面日历约定",
                    pending="",
                    limitation="none",
                )
            else:
                row.update(
                    fact="技术方法依赖附件T1，附件内容未知",
                    requirement="核实技术规格、标准版本和检验方法",
                    reason="正文不能证明附件方法与标准",
                    suggestion="核实T1内容及版本，仅本部分暂缓判断",
                    pending="确认技术附件内容及适用性",
                    limitation="material_unknown",
                )
        elif selected and case == "undefined-policy":
            row.update(
                fact="拟约定预付款担保比例",
                requirement="政策要求说明担保措施，未定义统一比例",
                reason="不存在可直接比较的统一担保比例标准",
                suggestion="由企业明确交易保障方案，不补造比例阈值",
                pending="人工确认具体交易保障措施",
                limitation="standard_undefined",
            )
        elif selected and case == "partial-parse":
            row.update(
                fact="仅部分文本提取成功",
                requirement="只比较可读范围，不能推断全文缺失",
                reason="解析范围不足以判断本部分完整约定",
                suggestion="核对原件或重新提供可读文件",
                pending="确认未提取内容",
                limitation="parse_partial",
            )
        parts.append(row)
    outcomes = [p["conclusion"] for p in parts]
    conclusion = (
        "deviation"
        if "deviation" in outcomes
        else "needs_review"
        if "needs_review" in outcomes
        else "compliant"
    )
    trigger = "triggered" if selected and case in ("deviation", "approval-unknown") else "unknown"
    approval_ids = parts[0]["evidence_ids"].copy() if trigger == "triggered" else []
    if trigger == "triggered":
        for uid in payload["approval_policy_units"]:
            choice = next((e["id"] for e in evidence if uid in e["policy_units"]), None)
            if choice and choice not in approval_ids:
                approval_ids.append(choice)
    return dict(
        protocol="pilot-presign-findings-v3",
        conclusion=conclusion,
        comparisons=parts,
        approval_status="required_unknown" if trigger == "triggered" else "trigger_unknown",
        approval_trigger=trigger,
        approval_evidence_ids=approval_ids,
        requested_material_ids=["technical_spec"]
        if selected and case == "attachment-unknown"
        else [],
    )
