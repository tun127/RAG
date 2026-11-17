"""Presentation adapters for product pages. Pure display: no storage, model or writes."""

from __future__ import annotations

import re
from datetime import datetime

BUSINESS_REJECTIONS = {
    'approval_trigger_evidence_required': '审批触发依据不完整；首次答复未通过业务校验',
    'local_evidence_insufficient': '子比较的结论与依据限制不一致；首次答复未通过业务校验',
}

MATERIALS = (
    (
        "size_declaration",
        "规模声明",
        "用于核对双方企业规模及付款条件；不确定可保留“尚未核实”。",
    ),
    (
        "technical_spec",
        "技术附件",
        "用于核对技术规格、验收标准、检验方法和附件版本；没有可标记“未提供”。",
    ),
    (
        "approval_record",
        "签署前批准",
        "已有的签署前批准、偏离批准或商业例外文件；不要求未来履约记录。",
    ),
    (
        "authority_record",
        "授权文件",
        "签署人或代理人的书面授权文件；不确定时保留“尚未核实”。",
    ),
    (
        "condition_records",
        "签署前条件证据",
        "附条件批准对应的签署前条件落实证据；没有可标记“未提供”。",
    ),
)
MATERIAL_NAMES = {mid: name for mid, name, _ in MATERIALS}
MATERIAL_PURPOSES = {mid: purpose for mid, _, purpose in MATERIALS}
TASK_TITLES = {
    "PAY-01": "付款安排",
    "PAY-02": "付款条件",
    "ACC-01": "验收约定",
    "ACC-02": "验收条件",
    "EXC-01": "例外审批",
    "EXC-02": "例外适用条件",
}
MATERIAL_STATUS = {
    "unknown": ("尚未核实", "warn"),
    "provided": ("已提供", "ok"),
    "missing": ("未提供", "danger"),
    "not_applicable": ("已声明不适用", "neutral"),
}
CONCLUSIONS = {
    "deviation": ("发现问题（偏离候选）", "deviation"),
    "compliant": ("未发现偏离", "compliant"),
    "needs_review": ("依据不足，无法判断", "review"),
    "not_applicable": ("不适用候选", "neutral"),
}
EXECUTION = {
    "success": "程序已接受答复",
    "failed": "未形成有效结果",
    "not_executed": "未执行",
}
PARSE_STATUS = {
    "parsed": ("解析完成", "ok"),
    "partial": ("部分解析", "warn"),
    "failed": ("解析失败", "danger"),
    "rejected": ("文件不受支持", "danger"),
}
REASONS = {
    "text_consistent": "模型提出文本一致候选；仍需人工核对语义支持",
    "text_deviation": "模型提出文本偏离候选；仍需人工核对语义支持",
    "condition_unknown": "适用条件或事实尚未确认",
    "condition_not_triggered": "模型提出条件未触发候选；仍需人工核对",
    "material_unavailable": "判断所需材料尚未齐备",
    "absence_candidate": "固定文本中可能缺少相关内容；不能直接认定为条款缺失",
}
FAILURES = {
    "authentication": "模型服务认证失败；后续事项已停止，请管理员检查服务端配置",
    "server": "模型服务暂不可用；后续事项已停止",
    "timeout": "模型请求超时；后续事项已停止",
    "transport": "未能连接模型服务；后续事项已停止",
    "usage_unknown": "服务未返回可核实用量；为控制预算已停止后续事项",
    "truncated": "模型答复被截断，本项未形成有效结果",
    "reply_schema_invalid": "模型答复格式不符合要求，本项未形成有效结果",
    "citation_failed": "模型引用未通过严格定位，本项未形成有效结果",
    "unknown_or_duplicate_material": "模型使用了无效材料标识，本项未形成有效结果",
    "primary_policy_unreturned": "未取得任务要求的主要政策，本项未形成有效结果",
    "retrieval_empty_not_a_business_verdict": "未检索到可用政策片段，本项未形成业务判断",
    "context_limit_no_truncation": "固定输入超过上限，本项未执行（不做截断）",
    "budget_stop": "预算额度已用完，本项及后续未执行",
    "run_deadline": "运行时限已到，本项及后续未执行",
    "stopped_after_api_or_usage_failure": "前序模型接口或用量异常，本项未执行",
    "orchestration_failed": "执行过程异常，本项未执行；已完成结果已保留",
    "process_state_unconfirmed": "服务进程中断，执行状态待确认",
    "model_configuration_unavailable": "真实审查配置不可用，本项未执行；无需在页面填写密钥",
    "client_configuration_failed": "因模型配置或客户端不可用，本项未执行",
    "parse_coverage_blocks_absence_candidate": "解析覆盖受限，不能接受条款缺失判断",
    "invalid_response_or_configuration": "本项未形成有效结果；技术原因见诊断",
    "presign_material_request_out_of_scope": "模型请求了本项范围外的材料，严格校验拒绝，本项未形成有效结果",
    "presign_trigger_unknown": "模型将尚未确认的触发条件当作已知，严格校验拒绝",
    "presign_required_material_incomplete": "必要材料尚未核实，不能接受确定性结论",
}
CITATION_FAILURES = {
    "citation_not_found": "模型摘录未在指定文本块中找到",
    "citation_ambiguous": "摘录在指定文本块中出现多次，不能唯一定位",
    "citation_wrong_source_or_version": "引用来源或版本不匹配",
    "citation_unknown_block": "引用块不属于该来源",
    "citation_unknown_document": "引用了未提供的文档",
    "citation_empty": "引用摘录为空",
    "citation_fixed_text_mismatch": "引用区间与固定文本不一致",
}


def _fallback(value, default="历史记录未提供"):
    return default if value in (None, "") else value


def short_version(value, limit=12):
    if not value:
        return "版本未提供"
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "…"


def fmt_time(value):
    if not value:
        return "历史记录未提供"
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if stamp.tzinfo is None:
        return str(value)
    return stamp.strftime("%Y-%m-%d %H:%M") + "（UTC）"


def stored_mode(v):
    mode = v.get("execution_mode")
    if mode in ("SIMULATED", "LIVE"):
        return mode
    return "SIMULATED" if v.get("report", {}).get("simulation", True) else "LIVE"


def mode_view(v, fallback=None):
    mode = v.get("execution_mode")
    if mode not in ("SIMULATED", "LIVE"):
        if "simulation" in (v.get("report") or {}):
            mode = "SIMULATED" if v["report"]["simulation"] else "LIVE"
        elif fallback in ("SIMULATED", "LIVE"):
            mode = fallback
        else:
            mode = "SIMULATED"
    if mode == "LIVE":
        return {
            "label": "LIVE · 真实模型审查",
            "tone": "live",
            "is_simulation": False,
            "note": "结果来自真实模型调用；候选仍不是法律结论。",
        }
    return {
        "label": "SIMULATED · 流程演练",
        "tone": "sim",
        "is_simulation": True,
        "note": "本次使用固定流程替身，没有分析你上传的合同。",
    }


def example_mode():
    return {
        "label": "示例报告 · 人工构造",
        "tone": "example",
        "is_simulation": False,
        "is_example": True,
        "note": "示例数据用于展示页面结构，不是模型成绩。",
    }


def _overall(v, rows, simulated):
    status = v.get("overall_status") or "historical"
    if status == "running":
        return {
            "label": "正在执行",
            "tone": "running",
            "detail": "页面只读取已保存进度；刷新不会再次提交审查。",
        }
    if status == "state_unconfirmed":
        return {
            "label": "状态待确认",
            "tone": "warn",
            "detail": "服务进程已中断。请勿重复提交，先联系管理员确认运行状态。",
        }
    if status == "failed":
        return {
            "label": "执行中断",
            "tone": "danger",
            "detail": "已保存已完成事项；未执行事项不会自动重跑。",
        }
    if status == "partial":
        return {
            "label": "部分完成",
            "tone": "warn",
            "detail": "执行已结束，但有事项失败或未执行；已有结果继续保留。",
        }
    if status == "completed":
        return {
            "label": "流程演练结束" if simulated else "执行结束",
            "tone": "done",
            "detail": "全部事项均已处理；处理完成不等于业务通过。",
        }
    return {
        "label": "历史记录",
        "tone": "neutral",
        "detail": "旧运行按存储内容展示，不按新清单重新解释。",
    }


def _materials(states, mapping):
    by_id = {m.get("material_id"): m for m in states or []}
    files = {m.get("material_id"): m for m in mapping or []}
    result = []
    for mid, name, purpose in MATERIALS:
        state = by_id.get(mid)
        if state is None:
            label, tone = "运行未保存该材料声明", "neutral"
            statement = "历史记录没有这项材料声明；不能据此推定已提供或不适用。"
            status = None
        else:
            status = state.get("status") or "unknown"
            label, tone = MATERIAL_STATUS.get(status, ("待核对", "neutral"))
            statement = state.get("statement") or "未填写说明"
        file_name = ""
        entry = files.get(mid) or {}
        if entry:
            parsed = entry.get("parse") or {}
            original = entry.get("original") or ""
            file_name = parsed.get("original_name") or original.replace("\\", "/").split("/")[-1]
        result.append(
            {
                "id": mid,
                "name": name,
                "purpose": purpose,
                "status": status,
                "status_label": label,
                "tone": tone,
                "statement": statement,
                "file_name": file_name,
            }
        )
    return result


def _material_names(states):
    return {m.get("material_id"): m for m in states or []}


def _selection_materials():
    return [
        {
            "id": mid,
            "name": name,
            "purpose": purpose,
            "status": "unknown",
            "status_label": "尚未核实",
            "tone": "warn",
            "statement": "",
            "file_name": "",
        }
        for mid, name, purpose in MATERIALS
    ]


def _material_required(name, state):
    return {
        "id": name,
        "name": MATERIAL_NAMES.get(name, name),
        "status": (state or {}).get("status"),
        "status_label": MATERIAL_STATUS.get((state or {}).get("status"), ("尚未核实", "warn"))[0],
        "tone": MATERIAL_STATUS.get((state or {}).get("status"), ("尚未核实", "warn"))[1],
        "statement": (state or {}).get("statement") or "运行未保存该材料声明",
    }


def _requested_materials(row, states):
    checked = row.get("validation") or {}
    reply = checked.get("reply") or {}
    if not checked.get("valid"):
        return []
    ids = reply.get("requested_material_ids")
    if ids is None:
        ids = reply.get("material_ids") or []
    if reply.get("reason_code") != "material_unavailable":
        return []
    known = _material_names(states)
    return [_material_required(mid, known.get(mid)) for mid in ids]


def _task_meta(c, task_id):
    return (c.get("task_meta") or {}).get(task_id) or {}


def _task_title(task_id, meta):
    key = str(task_id).removeprefix("RAG-PILOT-")
    return TASK_TITLES.get(key, meta.get("title") or key)


def _item_status(row, index, completed, v, simulated):
    if row.get('execution_status') == 'failed' and row.get('failure') in BUSINESS_REJECTIONS:
        return {'label': '业务校验未通过', 'tone': 'warn', 'kind': 'failed'}
    agent = row.get('agent') or {}
    if agent.get('intervened'):
        return {'label': agent['label'], 'tone': 'warn' if agent['label'] in ('仍有疑点', '复核未完成') else 'ok',
                'kind': 'agent_review'}
    simulated = simulated and row.get("prompt_version") != "pilot-presign-findings-v3"
    overall = v.get("overall_status")
    execution = row.get("execution_status")
    conclusion = row.get("business_conclusion")
    if overall == "state_unconfirmed":
        if execution == "success":
            return {"label": "已处理", "tone": "ok", "kind": "processed"}
        if execution == "failed":
            return {"label": "执行失败（无有效结果）", "tone": "danger", "kind": "failed"}
        return {"label": "状态待确认", "tone": "warn", "kind": "unconfirmed"}
    if overall == "running":
        if execution in ("success", "failed") and row.get("failure") != "pending":
            if execution == "success":
                return {"label": "已处理", "tone": "ok", "kind": "processed"}
            return {"label": "执行失败（无有效结果）", "tone": "danger", "kind": "failed"}
        if completed and index == completed:
            return {"label": "处理中", "tone": "running", "kind": "processing"}
        return {"label": "等待", "tone": "neutral", "kind": "waiting"}
    if simulated:
        if execution == "success":
            return {"label": "流程已走通", "tone": "sim-ok", "kind": "fixture-ok"}
        if execution == "failed":
            return {"label": "流程失败", "tone": "danger", "kind": "failed"}
        return {"label": "未执行", "tone": "neutral", "kind": "not-executed"}
    if execution == "success" and conclusion in CONCLUSIONS:
        label, tone = CONCLUSIONS[conclusion]
        return {"label": label, "tone": tone, "kind": "conclusion"}
    if execution == "failed":
        return {"label": "执行失败（无有效结果）", "tone": "danger", "kind": "failed"}
    return {"label": "未执行", "tone": "neutral", "kind": "not-executed"}


def _failure_text(row):
    code = row.get("failure")
    if code in BUSINESS_REJECTIONS:
        return BUSINESS_REJECTIONS[code]
    if not code:
        return ""
    if code in {"transport", "timeout"}:
        for attempt in row.get("attempts") or []:
            diagnostic = attempt.get("transport_diagnostic") or {}
            if any(c.get("reason") == "network_access_denied_by_os_or_environment"
                   for c in diagnostic.get("causes", [])):
                return "本机或启动环境拒绝网络连接；后续事项已停止，请检查网络访问权限"
            labels = {
                "dns_failed": "无法解析模型服务域名",
                "connection_failed": "无法建立模型服务连接",
                "proxy_connection_failed": "代理连接失败",
                "tls_failed": "安全连接证书或握手失败",
                "connect_timeout": "连接模型服务超时",
                "read_timeout": "等待模型服务答复超时",
                "transport_other": "模型服务传输异常",
            }
            if diagnostic.get("category") in labels:
                return labels[diagnostic["category"]] + "；后续事项已停止"
    return FAILURES.get(code, "本项未形成有效结果；技术原因见诊断")


def _question(row, simulated):
    simulated = simulated and row.get("prompt_version") != "pilot-presign-findings-v3"
    if simulated:
        return ""
    checked = row.get("validation") or {}
    reply = checked.get("reply") or {}
    issue = reply.get("review_issue") or {}
    if checked.get("valid") and reply.get("conclusion") == "needs_review":
        return issue.get("question") or ""
    return ""


def _reason_text(row, simulated):
    simulated = simulated and row.get("prompt_version") != "pilot-presign-findings-v3"
    if simulated:
        return ""
    checked = row.get("validation") or {}
    reply = checked.get("reply") or {}
    return REASONS.get(reply.get("reason_code"), "") if checked.get("valid") else ""


def _program_status(row):
    checked = row.get("validation") or {}
    execution = EXECUTION.get(row.get("execution_status"), "待核对")
    if checked.get("valid"):
        return execution + "；引用已通过严格定位（不代表语义支持）"
    if row.get("execution_status") == "failed":
        return execution
    return execution


def _outcome(v, rows, simulated, completed):
    simulated = simulated and not any(r.get("prompt_version") == "pilot-presign-findings-v3" for r in rows)
    total = len(rows)
    done = sum(
        1
        for r in rows
        if r.get("execution_status") in ("success", "failed") and r.get("failure") != "pending"
    )
    if simulated:
        walked = sum(1 for r in rows if r.get("execution_status") == "success")
        failed = sum(1 for r in rows if r.get("execution_status") == "failed")
        pending = sum(1 for r in rows if r.get("execution_status") not in ("success", "failed"))
        chips = [
            {"label": "业务判断", "value": 0, "tone": "neutral"},
            {"label": "流程走通", "value": walked, "tone": "sim-ok"},
        ]
        if failed:
            chips.append({"label": "流程失败", "value": failed, "tone": "danger"})
        if pending:
            chips.append({"label": "未执行", "value": pending, "tone": "neutral"})
        status = v.get("overall_status")
        if status == "running":
            return {
                "headline": f"已完成 {done} / {total} 个流程步骤",
                "subline": "正在执行流程演练；系统没有分析你上传的合同。",
                "chips": chips,
                "note": "刷新只读取进度；流程走通不代表审查通过。",
            }
        if status == "state_unconfirmed":
            return {
                "headline": "流程演练状态待确认，已有记录已保留",
                "subline": "服务进程已中断；系统没有分析你上传的合同，请勿重复提交。",
                "chips": [
                    {"label": "业务判断", "value": 0, "tone": "neutral"},
                    {"label": "已保存步骤", "value": walked + failed, "tone": "neutral"},
                ],
                "note": "联系管理员确认进程状态后再决定是否重新开始。",
            }
        if status in ("failed", "partial"):
            return {
                "headline": f"流程演练{'中断' if status == 'failed' else '部分完成'}：{walked} / {total} 个步骤已走通",
                "subline": (
                    f"流程失败 {failed} 项，未执行 {pending} 项；"
                    "系统没有分析你上传的合同，已有记录保留。"
                ),
                "chips": chips,
                "note": "未执行或失败事项不会自动重跑；流程走通不代表审查通过。",
            }
        return {
            "headline": f"{walked} / {total} 个流程步骤已走通",
            "subline": "这是一次流程演练，不是六项业务发现；系统没有分析你上传的合同。",
            "chips": chips,
            "note": "如需查看有业务候选的完整结果形态，请打开“示例报告”。",
        }
    counts = {key: 0 for key in CONCLUSIONS}
    for row in rows:
        if row.get("execution_status") == "success":
            conclusion = row.get("business_conclusion")
            if conclusion in counts:
                counts[conclusion] += 1
    failed = sum(1 for r in rows if r.get("execution_status") == "failed")
    pending = sum(1 for r in rows if r.get("execution_status") not in ("success", "failed"))
    candidates = sum(counts.values())
    chips = []
    for key in ("deviation", "needs_review", "compliant", "not_applicable"):
        if counts[key]:
            label, tone = CONCLUSIONS[key]
            chips.append({"label": label, "value": counts[key], "tone": tone})
    if failed:
        chips.append({"label": "无有效结果", "value": failed, "tone": "danger"})
    if pending:
        chips.append({"label": "未执行", "value": pending, "tone": "neutral"})
    status = v.get("overall_status")
    if status == "running":
        headline = f"正在执行，已完成 {done} / {total} 项"
        subline = "结果会随执行增加；页面显示的是已保存部分。"
        note = "没有更细粒度进度时不显示百分比；刷新不会重新提交审查。"
    elif status == "state_unconfirmed":
        headline = "执行状态待确认，已有记录已保留"
        subline = "服务进程中断；不要重复提交。"
        note = "联系管理员确认进程状态后再决定是否重新开始。"
    elif status in ("partial", "failed"):
        headline = f"已处理 {done} / {total} 项，形成 {candidates} 项候选"
        subline = f"无有效结果 {failed} 项，未执行 {pending} 项。"
        note = "候选不是结论；失败和未执行事项已保留原因。"
    else:
        if candidates:
            headline = f"形成 {candidates} 项候选，等待人工复核"
            subline = "、".join(
                f"{CONCLUSIONS[k][0]} {counts[k]} 项" for k in ("deviation", "needs_review") if counts[k]
            ) or "请逐项查看候选及依据。"
        else:
            headline = "本次没有形成可用业务候选"
            subline = "请查看各项失败、未执行或材料不足的原因，不能写成“未发现问题”。"
        note = "程序接受答复不等于业务通过；待确认不等于风险成立。"
    return {"headline": headline, "subline": subline, "chips": chips, "note": note}


def _scope(v, c, simulated):
    parse = v.get("bridge", {}).get("parse") or {}
    result = parse.get("result") or {}
    status = result.get("status") or "historical"
    parse_label, parse_tone = PARSE_STATUS.get(status, ("历史解析状态未提供", "neutral"))
    stats = c.get("parse_stats") or {}
    policies = c.get("policy_documents") or []
    policy_labels = [
        f"{p.get('document_id')} {p.get('version')}" for p in policies if p.get("document_id")
    ]
    if not policy_labels:
        policy_labels = ["政策快照 " + short_version(v.get("policy"), 16)]
    warnings = []
    for issue in result.get("issues") or []:
        if issue.get("message"):
            warnings.append(issue["message"])
    if result.get("coverage_complete") is False:
        warnings.append("解析只覆盖部分内容；不能据此认定完整条款缺失。")
    return {
        "contract_name": _fallback(parse.get("original_name"), "历史合同（名称未提供）"),
        "version_short": short_version(parse.get("version_id")),
        "version_full": _fallback(parse.get("version_id"), "版本未提供"),
        "stage": _fallback(v.get("stage"), "阶段未提供"),
        "task_count": len(v.get("report", {}).get("results") or []),
        "policy_labels": policy_labels,
        "parse_status": status,
        "parse_label": parse_label,
        "parse_tone": parse_tone,
        "characters": stats.get("characters", 0),
        "blocks": stats.get("blocks", 0),
        "format": stats.get("format") or "历史记录未提供",
        "coverage_scope": _fallback(result.get("coverage_scope"), "解析范围未提供"),
        "warnings": warnings,
        "simulated": simulated,
    }


def _diagnostics(v, c, simulated):
    config = v.get("config") or v.get("report", {}).get("config") or {}
    report = v.get("report") or {}
    known = report.get("known_usage") or {}
    usage = ""
    if known:
        usage = (
            f"模型调用 {known.get('model_calls', 0)} 次；"
            f"输入 {known.get('input_tokens', 0)} / 输出 {known.get('output_tokens', 0)} token"
        )
    elif simulated:
        usage = "本地流程替身；不调用外部模型，不产生模型费用"
    if report.get("unknown_usage_attempts"):
        usage += f"；另有 {report['unknown_usage_attempts']} 次用量未确认"
    lines = [
        ("运行编号", _fallback(v.get("run_id"))),
        ("执行模式", "SIMULATED（流程替身）" if simulated else "LIVE（真实模型）"),
        ("场景 / 夹具", f"{v.get('scenario') or '未提供'} / {short_version(v.get('fixture_version'), 24)}"),
        ("合同解析版本", short_version((v.get("bridge", {}).get("parse") or {}).get("version_id"), 20)),
        ("政策快照", _fallback(v.get("policy"))),
        ("输入协议", _fallback(v.get("input_version"))),
        ("清单版本", _fallback(v.get("checklist_version"))),
        ("输出协议", _fallback(config.get("output_protocol"))),
        (
            "模型配置",
            f"请求提供方：{config.get('provider', '未提供')}；"
            f"模型：{config.get('model', '未提供')}",
        ),
        (
            "调用预算",
            f"最多 {config.get('max_attempts', '未提供')} 次请求；单次输出 "
            f"{config.get('per_call_output', '未提供')} / 总输出 "
            f"{config.get('total_output', '未提供')} token；总期限 "
            f"{config.get('total_seconds', '未提供')} 秒",
        ),
        ("实际用量", usage or "历史记录未提供"),
    ]
    hypotheses = v.get("trigger_hypotheses") or []
    hypothesis_text = "；".join(
        f"{h.get('condition')}={h.get('state')}" for h in hypotheses if h.get("condition")
    )
    if hypothesis_text:
        lines.append(("触发条件声明", hypothesis_text + "（均为开发声明，不是已核实事实）"))
    return {"lines": lines, "report": report, "config": config}


def _opinions(v, task_id=None):
    result = []
    for opinion in v.get("human_opinions") or []:
        if task_id and opinion.get("task_id") != task_id:
            continue
        decision = opinion.get("decision") or "待复核"
        tone = {"确认": "ok", "更正": "warn", "待复核": "neutral"}.get(decision, "neutral")
        result.append(
            {
                "decision": decision,
                "decision_label": {
                    "确认": "确认记录",
                    "更正": "更正记录",
                    "待复核": "待复核",
                }.get(decision, decision),
                "tone": tone,
                "reviewer": _fallback(opinion.get("reviewer"), "历史记录未提供"),
                "note": _fallback(opinion.get("note"), "历史记录未提供"),
                "saved_at": fmt_time(opinion.get("saved_at")),
                "notice": opinion.get("reviewer_notice")
                or "复核人自行填写，未经身份认证，不构成电子签名。",
            }
        )
    return sorted(result, key=lambda x: x["saved_at"], reverse=True)


def _segments(text, spans):
    clean = []
    for start, end in spans or []:
        start = max(0, min(len(text), int(start)))
        end = max(0, min(len(text), int(end)))
        if end > start:
            clean.append((start, end))
    merged = []
    for start, end in sorted(clean):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    result = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            result.append({"text": text[cursor:start], "mark": False})
        result.append({"text": text[start:end], "mark": True})
        cursor = end
    if cursor < len(text):
        result.append({"text": text[cursor:], "mark": False})
    return result


def _section_label(text, start):
    window = text[max(0, start - 600) : start]
    lines = list(re.finditer(r"(?m)^(?:#{1,4}\s*)?(第[一二三四五六七八九十百零〇0-9]+条[^\n]{0,48})$", window))
    if lines:
        return lines[-1].group(1).strip()
    headings = list(re.finditer(r"(?m)^(#{2,4})\s+(.+)$", window))
    if headings:
        return headings[-1].group(2).strip()
    return "章节未标注"


def _interval_ok(text, cite):
    try:
        start = int(cite.get("start"))
        end = int(cite.get("end"))
    except (TypeError, ValueError):
        return False, "缺少可用定位区间"
    quote = cite.get("quote") or ""
    if not quote or start < 0 or end > len(text) or end <= start:
        return False, "定位区间越界或摘录为空"
    if text[start:end] != quote:
        return False, "定位区间与固定文本不一致"
    return True, ""


def _excerpt(text, blocks, cite):
    start, end = cite["start"], cite["end"]
    block = next(
        (b for b in blocks or [] if b.get("start", 0) <= start and end <= b.get("end", 0)),
        None,
    )
    if block:
        offset = block.get("start", 0)
        body = block.get("text") or text[offset : block.get("end", offset)]
    else:
        offset = max(0, start - 180)
        body = text[offset : min(len(text), end + 180)]
    return _segments(body, [(start - offset, end - offset)])


def _document_name(source, contract_name, materials):
    source_type = source.get("source_type")
    if source_type == "contract":
        return contract_name
    if source_type == "material":
        mid = str(source.get("document_id", "")).removeprefix("material-")
        return MATERIAL_NAMES.get(mid, "材料附件")
    return f"演示政策 {source.get('document_id', '')}"


def _document_role(source_type):
    return {
        "contract": "合同事实",
        "material": "材料事实",
        "demo_policy": "政策依据",
    }.get(source_type, "其他来源")


def _cite_view(cite, document, contract_name, materials, task_id, simulated, anchors):
    source = document["source"]
    text = source.get("text") or ""
    valid, invalid_reason = _interval_ok(text, cite)
    if (
        valid
        and cite.get("version")
        and source.get("version")
        and cite["version"] != source["version"]
    ):
        valid, invalid_reason = False, "引用版本与来源版本不一致"
    start = cite.get("start") or 0
    end = cite.get("end") or 0
    anchor_url = ""
    if valid and source.get("source_type") == "contract":
        for anchor in anchors or []:
            if anchor.get("start", 0) <= start and end <= anchor.get("end", 0):
                anchor_url = anchor.get("url") or ""
                break
    key = str(task_id).removeprefix("RAG-PILOT-")
    fixture_cross_topic = (
        simulated
        and source.get("source_type") == "contract"
        and "付款" in (cite.get("quote") or "")
        and key not in ("PAY-01", "PAY-02")
    )
    return {
        "quote": cite.get("quote") or "",
        "valid": valid,
        "invalid_reason": invalid_reason,
        "role": _document_role(source.get("source_type")),
        "source_type": source.get("source_type"),
        "document_label": _document_name(source, contract_name, materials),
        "heading": _section_label(text, start) if valid else "章节未确认",
        "version_short": short_version(cite.get("version") or source.get("version")),
        "version_full": _fallback(cite.get("version") or source.get("version")),
        "anchor_url": anchor_url,
        "fixture_cross_topic": fixture_cross_topic,
        "source_dom_id": "source-" + re.sub(r"[^a-zA-Z0-9_-]", "-", str(source.get("document_id"))),
        "excerpt_segments": _excerpt(text, document.get("blocks"), cite) if valid else [],
    }


def _evidence_from_context(e, *, simulated, contract_name, materials, task_id):
    if not e:
        return {"error": "本项没有可读取的证据上下文。", "has_any": False}
    if e.get("error"):
        return {"error": e["error"], "has_any": False}
    documents = e.get("documents") or []
    by_id = {}
    for doc in documents:
        source = doc.get("source") or {}
        by_id[(source.get("document_id"), source.get("source_type"))] = doc
    citations = []
    for cite in e.get("citations") or []:
        doc = by_id.get((cite.get("document_id"), cite.get("source_type")))
        if doc is None and not cite.get("source_type"):
            doc = next(
                (
                    d
                    for d in documents
                    if (d.get("source") or {}).get("document_id") == cite.get("document_id")
                ),
                None,
            )
        if doc is None:
            citations.append(
                {
                    "quote": cite.get("quote") or "",
                    "valid": False,
                    "invalid_reason": "来源文档不在本次固定输入中",
                    "role": _document_role(cite.get("source_type")),
                    "source_type": cite.get("source_type"),
                    "document_label": cite.get("document_id") or "未知来源",
                    "heading": "章节未确认",
                    "version_short": short_version(cite.get("version")),
                    "version_full": _fallback(cite.get("version")),
                    "anchor_url": "",
                    "fixture_cross_topic": False,
                    "source_dom_id": "source-unknown",
                    "excerpt_segments": [],
                }
            )
            continue
        citations.append(
            _cite_view(
                cite,
                doc,
                contract_name,
                materials,
                task_id,
                simulated,
                e.get("anchors"),
            )
        )
    contract_cites = [c for c in citations if c["source_type"] == "contract"]
    material_cites = [c for c in citations if c["source_type"] == "material"]
    policy_cites = [c for c in citations if c["source_type"] == "demo_policy"]
    sources = []
    for doc in documents:
        source = doc.get("source") or {}
        spans = [
            (c["start"], c["end"])
            for c in e.get("citations") or []
            if c.get("document_id") == source.get("document_id")
            and c.get("source_type") == source.get("source_type")
            and (
                not c.get("version")
                or not source.get("version")
                or c["version"] == source["version"]
            )
            and _interval_ok(source.get("text") or "", c)[0]
        ]
        dom_id = "source-" + re.sub(r"[^a-zA-Z0-9_-]", "-", str(source.get("document_id")))
        sources.append(
            {
                "dom_id": dom_id,
                "label": _document_name(source, contract_name, materials),
                "role": _document_role(source.get("source_type")),
                "source_type": source.get("source_type"),
                "version_short": short_version(source.get("version")),
                "version_full": _fallback(source.get("version")),
                "segments": _segments(source.get("text") or "", spans),
                "citation_count": len(spans),
            }
        )
    diagnostics = []
    for item in e.get("citation_diagnostics") or []:
        status = item.get("status") or "待核对"
        diagnostics.append(
            {
                "status": status,
                "label": CITATION_FAILURES.get(status, status),
                "quote": (item.get("declaration") or {}).get("quote") or "",
            }
        )
    return {
        "error": "",
        "simulated": simulated,
        "contract_cites": contract_cites,
        "material_cites": material_cites,
        "policy_cites": policy_cites,
        "has_any": bool(citations),
        "valid_count": sum(1 for c in citations if c["valid"]),
        "invalid_count": sum(1 for c in citations if not c["valid"]),
        "sources": sources,
        "diagnostics": diagnostics,
        "validation_valid": bool(e.get("validation_valid")),
        "validation_failure": e.get("validation_failure") or "",
        "notice": (
            "以下引用来自流程夹具，只验证程序能否定位文本；不是模型业务判断，"
            "不能支撑事项结论。"
            if simulated
            else "定位有效只证明引文存在于固定文本，不证明语义支持；请人工核对。"
        ),
        "source_note": "以下为固定提取文本，不是 PDF 版面或视觉坐标。",
    }


def _evidence_for_row(row, materials, contract_name, simulated, task_id=""):
    simulated = simulated and row.get("prompt_version") != "pilot-presign-findings-v3"
    checked = row.get("validation") or {}
    if not checked.get("valid"):
        return {
            "error": "本项没有通过严格定位的引用。",
            "has_any": False,
            "simulated": simulated and row.get("prompt_version") != "pilot-presign-findings-v3",
        }
    cites = []
    key = str(task_id).removeprefix("RAG-PILOT-")
    for location in checked.get("locations") or []:
        source_type = location.get("source_type")
        label = (
            contract_name
            if source_type == "contract"
            else MATERIAL_NAMES.get(
                str(location.get("document_id", "")).removeprefix("material-"), "材料附件"
            )
            if source_type == "material"
            else f"演示政策 {location.get('document_id', '')}"
        )
        quote = location.get("quote") or ""
        cites.append(
            {
                "quote": quote,
                "role": _document_role(source_type),
                "source_type": source_type,
                "document_label": label,
                "heading": "固定文本引用",
                "version_short": short_version(location.get("version")),
                "version_full": _fallback(location.get("version")),
                "valid": True,
                "invalid_reason": "",
                "anchor_url": "",
                "source_dom_id": "report-source-"
                + re.sub(r"[^a-zA-Z0-9_-]", "-", str(location.get("document_id"))),
                "excerpt_segments": [{"text": quote, "mark": True}] if quote else [],
                "fixture_cross_topic": (
                    simulated
                    and source_type == "contract"
                    and "付款" in quote
                    and key not in ("PAY-01", "PAY-02")
                ),
            }
        )
    return {
        "error": "",
        "simulated": simulated and row.get("prompt_version") != "pilot-presign-findings-v3",
        "citations": cites,
        "has_any": bool(cites),
        "notice": (
            "以下引用来自流程夹具，只验证定位链路，不是业务判断。"
            if simulated
            else "定位有效不等于语义支持；请人工核对。"
        ),
    }


def _parse_blocks(result):
    blocks = []
    for block in result.get("blocks") or []:
        rows = block.get("rows") or []
        if rows:
            blocks.append(
                {
                    "kind": "table",
                    "rows": [
                        [cell.get("text") or "" for cell in row]
                        for row in rows
                    ],
                }
            )
        elif (block.get("text") or "").strip():
            blocks.append({"kind": "paragraph", "text": block.get("text")})
    return blocks


def _checklist(c):
    items = []
    for task in c.get("checklist") or []:
        key = str(task.get("task_id", "")).removeprefix("RAG-PILOT-")
        required = [
            MATERIAL_NAMES.get(mid, mid) for mid in task.get("required_materials") or []
        ]
        items.append(
            {
                "task_id": task.get("task_id"),
                "title": TASK_TITLES.get(key, key),
                "question": task.get("question") or "",
                "applicability": task.get("applicability") or "适用范围未提供",
                "exceptions": task.get("exceptions") or "边界未提供",
                "required_materials": required,
            }
        )
    return items


def _mode_options(c, selection=False):
    live = bool(c.get("live_enabled"))
    modes = [
        {
            "value": "SIMULATED",
            "title": "流程演练（SIMULATED）",
            "description": "走通上传、材料、执行、证据、复核和导出；系统不分析你的合同。",
            "note": "不会产生业务判断，也不调用外部模型。",
            "available": True,
            "checked": True,
            "tone": "sim",
        },
        {
            "value": "LIVE",
            "title": "真实模型审查（LIVE）",
            "description": "调用服务端已配置的真实模型；结果仅为候选，仍需人工复核。",
            "note": (
                "服务端已允许真实审查。"
                if live
                else "服务端未启用真实审查；不会自动改用模拟。"
            ),
            "available": live,
            "checked": False,
            "tone": "live",
        },
    ]
    if selection and not live:
        modes[0]["checked"] = True
    return modes


def _budget_lines(config):
    if not config:
        return ["历史记录未提供模型预算。"]
    lines = [
        f"最多 {config.get('max_attempts', '未提供')} 次模型请求",
        f"单次输出上限 {config.get('per_call_output', '未提供')} token",
        f"总输出上限 {config.get('total_output', '未提供')} token",
        f"运行总期限 {config.get('total_seconds', '未提供')} 秒",
    ]
    return lines


def build_home(c):
    documents = []
    for record in sorted(
        c.get("documents") or [], key=lambda r: r.get("created_at") or "", reverse=True
    ):
        status = (record.get("result") or {}).get("status")
        label, tone = PARSE_STATUS.get(status, ("状态待核对", "neutral"))
        documents.append(
            {
                "name": record.get("original_name") or "历史合同（名称未提供）",
                "version_short": short_version(record.get("version_id")),
                "version_full": record.get("version_id") or "版本未提供",
                "status_label": label,
                "tone": tone,
                "time": fmt_time(record.get("created_at")),
                "href": f"/select/{record.get('task_id')}/{record.get('version_id')}",
            }
        )
    runs = []
    for entry in c.get("runs") or []:
        parse = entry.get("parse") or {}
        simulated = entry.get("execution_mode") != "LIVE"
        fixture = bool(entry.get("fixture_version")) or simulated
        mode = (
            {"label": "流程演练", "tone": "sim"}
            if simulated
            else {"label": "真实审查", "tone": "live"}
        )
        counts = entry.get("status_counts") or {}
        overall = entry.get("overall_status") or "historical"
        if fixture:
            total = sum(counts.values()) or 6
            success = counts.get("success", 0)
            if overall == "state_unconfirmed":
                count_text = f"已保存步骤 {success} / {total} 项；状态待确认"
            elif overall in ("partial", "failed"):
                count_text = f"流程走通 {success} / {total} 项；仍有未完成"
            else:
                count_text = f"流程走通 {success} / {total} 项；无业务判断"
        else:
            count_text = (
                f"有结果 {counts.get('success', 0)} 项 · "
                f"无有效结果 {counts.get('failed', 0)} 项 · "
                f"未执行 {counts.get('not_executed', 0)} 项"
            )
        overall_view = _overall({"overall_status": overall}, [], simulated)
        runs.append(
            {
                "href": f"/runs/{entry.get('run_id')}",
                "name": parse.get("original_name") or "历史合同（名称未提供）",
                "mode": mode,
                "status_label": overall_view["label"],
                "status_tone": overall_view["tone"],
                "created": fmt_time(entry.get("created_at")),
                "updated": fmt_time(entry.get("updated_at")),
                "version_short": short_version(parse.get("version_id")),
                "count": count_text,
                "short_id": str(entry.get("run_id") or "")[:8],
                "fixture": fixture,
            }
        )
    unclassified = [
        {
            "name": record.get("original_name") or "历史文件（名称未提供）",
            "version_short": short_version(record.get("version_id")),
            "time": fmt_time(record.get("created_at")),
        }
        for record in c.get("unclassified_documents") or []
    ]
    live = bool(c.get("live_enabled"))
    failed = c.get("parse_error") or {}
    return {
        "live_enabled": live,
        "product_name": "采购合同审查",
        "tagline": "上传采购合同，系统按固定的签署前清单审六项，给出候选和依据；结论仍由人工复核。",
        "button_label": "开始审查" if live else "体验模拟流程",
        "action_note": (
            "上传后自动解析、固定版本、套用当前政策与清单并提交真实审查。"
            if live
            else "当前未启用真实审查：上传只走流程演练，系统不会分析你上传的合同。"
        ),
        "example_href": "/example",
        "ticket": c.get("ticket") or "",
        "documents": documents,
        "runs": runs,
        "unclassified": unclassified,
        "has_documents": bool(documents),
        "has_runs": bool(runs),
        "error": {
            "message": failed.get("message") or "",
            "name": failed.get("name") or "",
            "reasons": failed.get("reasons") or [],
        }
        if failed
        else None,
    }


def build_selection(c):
    record = c["record"]
    result = record.get("result") or {}
    status = result.get("status") or "failed"
    parse_label, parse_tone = PARSE_STATUS.get(status, ("状态待核对", "neutral"))
    can_start = status in ("parsed", "partial") and bool(result.get("blocks"))
    stats = c.get("parse_stats") or {}
    warnings = [i.get("message") for i in result.get("issues") or [] if i.get("message")]
    if result.get("coverage_complete") is False:
        warnings.append("解析只覆盖部分内容；不能据此认定完整条款缺失。")
    checklist = _checklist(c)
    policy_labels = [
        f"{p.get('document_id')} {p.get('version')}"
        for p in c.get("policy_documents") or []
        if p.get("document_id")
    ]
    config = c.get("model_config") or {}
    return {
        "contract": {
            "name": record.get("original_name") or "合同（名称未提供）",
            "version_short": short_version(record.get("version_id")),
            "version_full": record.get("version_id") or "版本未提供",
            "status_label": parse_label,
            "tone": parse_tone,
            "time": fmt_time(record.get("created_at")),
            "task": record.get("task_id"),
            "version": record.get("version_id"),
        },
        "warnings": warnings,
        "coverage_scope": result.get("coverage_scope") or "解析范围未提供",
        "parse_stats": {
            "characters": stats.get("characters", 0),
            "blocks": stats.get("blocks", 0),
            "format": stats.get("format") or "历史记录未提供",
            "label": parse_label,
        },
        "blocks": _parse_blocks(result),
        "can_start": can_start,
        "parse_error": (
            ""
            if can_start
            else "解析未成功，无法提交审查。请按原因修正后重新上传；部分覆盖只能审查已提取内容。"
        ),
        "materials": _selection_materials(),
        "checklist": checklist,
        "modes": _mode_options(c, selection=True),
        "live_enabled": bool(c.get("live_enabled")),
        "budget_lines": _budget_lines(config),
        "simulation_budget": "本地流程替身；不调用外部模型，不产生模型费用。",
        "policy_labels": policy_labels,
        "policy_note": "政策为虚构演示政策，不是真实企业制度或法律文本。",
        "token": c.get("csrf_token"),
        "submission_id": c.get("submission_id"),
        "task": record.get("task_id"),
        "version": record.get("version_id"),
        "policy": c.get("policy"),
        "stage": c.get("stage"),
        "reviewer_notice": c.get("reviewer_notice"),
    }


def _detail(c, active, item_display):
    v = c["view"]
    simulated = item_display["simulated"]
    row = active["row"]
    meta = _task_meta(c, item_display["task_id"])
    states = c.get("materials") or []
    known = _material_names(states)
    required = [
        _material_required(mid, known.get(mid)) for mid in meta.get("required_materials") or []
    ]
    conditional = [
        {
            "condition": rule.get("condition"),
            "materials": [MATERIAL_NAMES.get(m, m) for m in rule.get("material_ids") or []],
            "purpose": rule.get("purpose") or "",
        }
        for rule in meta.get("conditional_materials") or []
    ]
    requested = [] if simulated else _requested_materials(row, states)
    missing_dependencies = row.get("missing_task_dependencies") or []
    question = _question(row, simulated)
    reason = _reason_text(row, simulated)
    failure = _failure_text(row)
    candidate = ""
    candidate_tone = "neutral"
    if (not simulated or row.get("prompt_version") == "pilot-presign-findings-v3") and row.get("execution_status") == "success":
        conclusion = row.get("business_conclusion")
        if conclusion in CONCLUSIONS:
            candidate, candidate_tone = CONCLUSIONS[conclusion]
    constraints = []
    for material in required:
        if material["status"] != "provided":
            constraints.append(
                f"本项政策要求“{material['name']}”；当前为“{material['status_label']}”。"
            )
    for dependency in missing_dependencies:
        constraints.append(f"缺少任务要求的政策依赖 {dependency}，本项只能转人工处理。")
    if simulated:
        constraints.insert(
            0,
            "本项使用固定流程替身，没有模型业务判断；下方引用只用于验证定位链路。",
        )
    next_steps = []
    overall = v.get("overall_status")
    if simulated:
        next_steps = [
            "本次只验证流程，不能把流程走通当作审查通过。",
            "如需了解有业务候选时的页面形态，请查看“示例报告”。",
            "如需真实判断，请等服务端启用真实审查后重新提交。",
        ]
    elif overall == "running":
        next_steps = [
            "页面每 3 秒只读取进度，不会再次提交审查。",
            "已有结果可以先查看，后续可能继续增加。",
        ]
    elif overall == "state_unconfirmed":
        next_steps = [
            "不要重复提交同一份合同；先联系管理员确认运行状态。",
            "已保存的记录仍可阅读和导出。",
        ]
    elif row.get("execution_status") == "not_executed":
        next_steps = [
            "本项未执行，没有业务结果；原因见本页状态与诊断。",
            "如为预算、时限或服务异常，由管理员确认后再决定是否重新开始。",
        ]
    elif row.get("execution_status") == "failed":
        next_steps = [
            "本项没有有效结果，不能写成“未发现问题”。",
            "核对失败原因；如为模型或配置问题，请联系管理员。",
        ]
    elif candidate:
        next_steps = [
            "核对合同事实与政策依据，确认引用是否真的支持候选。",
            "在下方留下复核意见；候选不是结论，系统不会替你作出法律判断。",
        ]
    if question:
        next_steps.insert(0, "先核对需要确认的事实，再决定是否需要补充材料或修改合同。")
    decisions = [
        {"value": "待复核", "label": "待复核"},
        {
            "value": "确认",
            "label": "确认流程记录" if simulated else "确认候选记录",
        },
        {
            "value": "更正",
            "label": "更正流程记录" if simulated else "更正候选记录",
        },
    ]
    review_notice = c.get("reviewer_notice") or "复核人自行填写，未经身份认证。"
    if simulated:
        review_notice += " 本项没有真实业务判断；“确认”只表示你已查看流程记录，不代表确认风险成立。"
    evidence = _evidence_from_context(
        c.get("evidence"),
        simulated=simulated,
        contract_name=(v.get("bridge", {}).get("parse") or {}).get("original_name")
        or "合同（名称未提供）",
        materials=states,
        task_id=item_display["task_id"],
    )
    return {
        "task_id": item_display["task_id"],
        "task_title": item_display["title"],
        "question": question,
        "reason": reason,
        "failure": failure,
        "candidate": candidate,
        "finding": (row.get("validation") or {}).get("reply", {}),
        "candidate_tone": candidate_tone,
        "required_materials": required,
        "conditional_materials": conditional,
        "requested_materials": requested,
        "missing_dependencies": missing_dependencies,
        "constraints": constraints,
        "applicability": meta.get("applicability") or "适用范围未提供",
        "exceptions": meta.get("exceptions") or "边界未提供",
        "next_steps": next_steps,
        "opinions": _opinions(v, item_display["task_id"]),
        "review": {
            "action": f"/opinion/{v.get('run_id')}/{item_display['task_id']}",
            "token": c.get("csrf_token"),
            "decisions": decisions,
            "notice": review_notice,
            "submit_label": "保存演练意见" if simulated else "保存复核意见",
        },
        "evidence": evidence,
        "program_status": _program_status(row),
    }


def build_run(c):
    v = c["view"]
    rows = v.get("report", {}).get("results") or []
    mode = mode_view(v, c.get("mode"))
    simulated = mode["is_simulation"]
    status = _overall(v, rows, simulated)
    completed = sum(
        1
        for r in rows
        if r.get("execution_status") in ("success", "failed") and r.get("failure") != "pending"
    )
    selected = c.get("selected_task_id")
    has_selection = bool(selected)
    items = []
    active_raw = None
    for index, row in enumerate(rows):
        task_id = row.get("task_id")
        meta = _task_meta(c, task_id)
        title = _task_title(task_id, meta)
        row_status = _item_status(row, index, completed, v, simulated)
        checked = row.get("validation") or {}
        evidence_count = len(checked.get("locations") or []) if checked.get("valid") else 0
        item = {
            "task_id": task_id,
            "number": index + 1,
            "title": title,
            "status": row_status,
            "simulated": simulated and row.get("prompt_version") != "pilot-presign-findings-v3",
            "candidate": "",
            "candidate_tone": "neutral",
            "one_line": "",
            "opinion_count": 0,
            "is_active": task_id == selected or (selected is None and index == 0),
            "evidence_count": evidence_count,
            "failure": _failure_text(row),
        }
        if (not simulated or row.get("prompt_version") == "pilot-presign-findings-v3") and row.get("execution_status") == "success":
            conclusion = row.get("business_conclusion")
            if conclusion in CONCLUSIONS:
                item["candidate"], item["candidate_tone"] = CONCLUSIONS[conclusion]
        if simulated:
            item["one_line"] = {
                "fixture-ok": "流程夹具返回占位答复；无业务判断",
                "failed": "流程执行失败；查看原因",
                "not-executed": "本项未执行",
                "processing": "正在处理；无更细进度",
                "waiting": "等待执行",
                "unconfirmed": "状态待确认",
            }.get(row_status["kind"], "流程演练记录")
        else:
            question = _question(row, simulated)
            if question:
                item["one_line"] = question[:72] + ("…" if len(question) > 72 else "")
            elif item["candidate"]:
                item["one_line"] = "候选已形成；核对证据与边界"
            elif item["failure"]:
                item["one_line"] = item["failure"]
            else:
                item["one_line"] = row_status["label"]
        item["opinion_count"] = sum(
            1
            for o in v.get("human_opinions") or []
            if o.get("task_id") == task_id
        )
        items.append(item)
        if item["is_active"]:
            active_raw = {"row": row, "item": item}
    if active_raw is None and items:
        items[0]["is_active"] = True
        active_raw = {"row": rows[0], "item": items[0]}
    active = None
    if active_raw:
        active = {
            **active_raw["item"],
            **_detail(c, active_raw, active_raw["item"]),
        }
    scope = _scope(v, c, simulated)
    summary = _outcome(v, rows, simulated, completed)
    materials = _materials(c.get("materials") or [], _material_mapping(c))
    return {
        "run_id": v.get("run_id"),
        "mode": mode,
        "status": status,
        "summary": summary,
        "scope": scope,
        "materials": materials,
        "task_items": items,
        "active": active,
        "has_selection": has_selection,
        "saved": bool(c.get("saved")),
        "diagnostics": _diagnostics(v, c, simulated),
        "report_href": f"/print/{v.get('run_id')}",
        "json_href": f"/export/{v.get('run_id')}",
        "back_href": f"/runs/{v.get('run_id')}",
        "home_href": "/",
        "is_fixture": simulated,
        "run_id_short": str(v.get("run_id") or "")[:8],
    }


def build_result(c):
    v = c["view"]
    rows = v.get("report", {}).get("results") or []
    mode = mode_view(v, c.get("mode"))
    simulated = mode["is_simulation"]
    status = _overall(v, rows, simulated)
    completed = sum(
        1
        for r in rows
        if r.get("execution_status") in ("success", "failed") and r.get("failure") != "pending"
    )
    selected = c.get("selected_task_id")
    name = (v.get("bridge", {}).get("parse") or {}).get("original_name") or "合同（名称未提供）"
    states = c.get("materials") or []
    items = []
    for index, row in enumerate(rows):
        task_id = row.get("task_id")
        meta = _task_meta(c, task_id)
        row_status = _item_status(row, index, completed, v, simulated)
        question = _question(row, simulated)
        failure = _failure_text(row)
        candidate = ""
        if (not simulated or row.get("prompt_version") == "pilot-presign-findings-v3") and row.get("execution_status") == "success":
            candidate = CONCLUSIONS.get(row.get("business_conclusion"), ("", ""))[0]
        if simulated and row.get("prompt_version") != "pilot-presign-findings-v3":
            one_line = {
                "fixture-ok": "流程走通；没有业务判断",
                "failed": "流程执行失败；原因见展开项",
                "not-executed": "本项未执行",
                "processing": "正在处理",
                "waiting": "等待执行",
                "unconfirmed": "状态待确认",
            }.get(row_status["kind"], "流程演练记录")
        elif (row.get("validation") or {}).get("reply", {}).get("comparison_reason"):
            one_line = row["validation"]["reply"]["comparison_reason"]
        elif question:
            one_line = question
        elif candidate:
            one_line = "已形成候选；展开可核对依据"
        elif failure:
            one_line = failure
        else:
            one_line = row_status["label"]
        items.append(
            {
                "number": index + 1,
                "task_id": task_id,
                "title": _task_title(task_id, meta),
                "status": row_status,
                "one_line": one_line,
                "question": question,
                "failure": failure,
                "candidate": candidate,
                "evidence": _evidence_for_row(row, states, name, simulated, task_id),
                "reason": _reason_text(row, simulated),
                "check": meta.get("question") or _task_title(task_id, meta),
                "diagnostic": row.get("failure") if row.get("execution_status") == "failed" else "",
                "evidence_href": f"/evidence/{v.get('run_id')}/{task_id}",
                "opinion_count": sum(
                    1
                    for o in v.get("human_opinions") or []
                    if o.get("task_id") == task_id
                ),
                "finding": (row.get("validation") or {}).get("reply", {}),
                "comparison": row.get("business_conclusion") if row.get("execution_status") == "success" else "execution",
                "open": task_id == selected,
            }
        )
    priority = {"deviation": 0, "compliant": 1, "needs_review": 2, "not_applicable": 3, "execution": 4}
    items.sort(key=lambda item: priority.get(item["comparison"], 4))
    if selected is None:
        first_problem = next((item for item in items if item["comparison"] == "deviation"), None)
        if first_problem:
            first_problem["open"] = True
    return {
        "run_id": v.get("run_id"),
        "name": name,
        "mode": mode,
        "status": status,
        "summary": _outcome(v, rows, simulated, completed),
        "scope": _scope(v, c, simulated),
        "entries": items,
        "report_href": f"/print/{v.get('run_id')}",
        "export_href": f"/export/{v.get('run_id')}",
        "review_href": f"/review/{v.get('run_id')}",
        "example_href": "/example",
        "home_href": "/",
        "simulated": simulated,
        "is_fixture": simulated,
    }


def build_review(c):
    data = build_run(c)
    keep = (
        "task_id",
        "number",
        "title",
        "status",
        "one_line",
        "opinion_count",
        "is_active",
    )
    return {
        "run_id": data["run_id"],
        "name": data["scope"]["contract_name"],
        "mode": data["mode"],
        "status": data["status"],
        "entries": [{key: item[key] for key in keep} for item in data["task_items"]],
        "active": data["active"],
        "materials": data["materials"],
        "back_href": data["back_href"],
        "saved": data["saved"],
        "reviewer_notice": c.get("reviewer_notice"),
        "simulated": data["is_fixture"],
    }


def _material_mapping(c):
    return c.get("material_mapping") or []


def build_standalone_evidence(c):
    coverage = c.get("coverage") or {}
    parse = coverage.get("parse") or {}
    materials = c.get("materials") or []
    simulated = c.get("mode") == "SIMULATED"
    evidence = _evidence_from_context(
        c,
        simulated=simulated,
        contract_name=parse.get("original_name") or "合同（名称未提供）",
        materials=materials,
        task_id=c.get("task_id"),
    )
    return {
        "run_id": c.get("run_id"),
        "task_id": c.get("task_id"),
        "mode": {
            "label": "流程演练 · SIMULATED" if simulated else "真实模型审查 · LIVE",
            "tone": "sim" if simulated else "live",
        },
        "contract_name": parse.get("original_name") or "合同（名称未提供）",
        "version_short": short_version(parse.get("version_id")),
        "evidence": evidence,
        "back_href": f"/runs/{c.get('run_id')}?item={c.get('task_id')}",
        "result": c.get("result") or {},
    }


def build_report(c):
    v = c["view"]
    rows = v.get("report", {}).get("results") or []
    mode = mode_view(v, c.get("mode"))
    simulated = mode["is_simulation"]
    status = _overall(v, rows, simulated)
    completed = sum(
        1
        for r in rows
        if r.get("execution_status") in ("success", "failed") and r.get("failure") != "pending"
    )
    scope = _scope(v, c, simulated)
    parse = v.get("bridge", {}).get("parse") or {}
    contract_name = parse.get("original_name") or "合同（名称未提供）"
    materials = _materials(c.get("materials") or [], _material_mapping(c))
    items = []
    for index, row in enumerate(rows):
        task_id = row.get("task_id")
        meta = _task_meta(c, task_id)
        title = _task_title(task_id, meta)
        row_status = _item_status(row, index, completed, v, simulated)
        evidence = _evidence_for_row(
            row, c.get("materials") or [], contract_name, simulated, task_id
        )
        candidate = ""
        candidate_tone = "neutral"
        if (not simulated or row.get("prompt_version") == "pilot-presign-findings-v3") and row.get("execution_status") == "success":
            conclusion = row.get("business_conclusion")
            if conclusion in CONCLUSIONS:
                candidate, candidate_tone = CONCLUSIONS[conclusion]
        items.append(
            {
                "task_id": task_id,
                "number": index + 1,
                "title": title,
                "status": row_status,
                "simulated": simulated and row.get("prompt_version") != "pilot-presign-findings-v3",
                "candidate": candidate,
                "candidate_tone": candidate_tone,
                "finding": (row.get("validation") or {}).get("reply", {}),
                "question": _question(row, simulated),
                "reason": _reason_text(row, simulated),
                "failure": _failure_text(row),
                "program_status": _program_status(row),
                "evidence": evidence,
                "requested_materials": [] if simulated else _requested_materials(row, c.get("materials") or []),
                "opinions": _opinions(v, task_id),
                "applicability": meta.get("applicability") or "适用范围未提供",
                "exceptions": meta.get("exceptions") or "边界未提供",
            }
        )
    return {
        "run_id": v.get("run_id"),
        "mode": mode,
        "status": status,
        "summary": _outcome(v, rows, simulated, completed),
        "scope": scope,
        "materials": materials,
        "task_items": items,
        "diagnostics": _diagnostics(v, c, simulated),
        "opinions": _opinions(v),
        "report_title": "合同审查报告",
        "report_subtitle": "审查辅助记录，不是法律意见；所有候选均需人工复核。",
        "example": False,
        "back_href": f"/runs/{v.get('run_id')}",
        "json_href": f"/export/{v.get('run_id')}",
        "print_href": f"/print/{v.get('run_id')}",
        "generated_note": f"运行更新时间：{fmt_time(v.get('updated_at'))}",
    }
