"""Artificial, self-contained example report. Never stored or scored."""

from __future__ import annotations

import re

from . import display

CONTRACT_NAME = "星海智造设备采购合同（虚构示例）"
CONTRACT_VERSION = "示例版本 1.0（人工构造）"

CONTRACT_TEXT = """# 星海智造设备采购合同（虚构示例）

合同编号：XH-2026-011
买方：星海智造有限公司（虚构）
卖方：远山机电有限公司（虚构）
签订地点：演示市（虚构）

## 第一条 合同标的与价款
买方向卖方采购精密装配线设备两套，用于演示工厂。合同总价为人民币1,200,000元（含税，税率13%），币种为人民币。收款账户以卖方书面提供并经买方核验的账户为准。

## 第二条 付款安排
合同生效后5个工作日内，买方支付合同总价30%作为预付款。设备送达买方指定厂房后10个工作日内，买方支付至合同总价的90%。剩余10%在收到卖方发票后支付。买方内部审批未完成的，付款时间相应顺延，卖方不得催告。

## 第三条 交付与验收
卖方负责将设备运送至买方指定厂房，运输及卸货费用由卖方承担。买方在到货时签署收货单，签署收货单即视为完成数量、外观和质量验收，不再另行组织质量检验。技术附件由卖方在交付时提供，以卖方最新版本为准。

## 第四条 质量异议与变更
买方发现设备问题的，应在3个工作日内通知卖方。逾期未通知的，视为设备质量符合约定。合同履行中如需变更付款比例或验收安排，由采购经办与卖方协商，经采购部门负责人同意后生效，无需提交书面例外申请。

## 第五条 生效与其他
本合同自双方盖章之日起生效，一式两份。未尽事宜由双方另行协商。因本合同产生的争议，提交演示市仲裁委员会仲裁。
"""


def _policy_source(knowledge, document_id, heading, limit=240):
    document = next(d for d in knowledge.documents if d.document_id == document_id)
    unit = next(
        u
        for u in knowledge.units
        if u.document_id == document_id and u.heading == heading
    )
    section = document.text[unit.start : unit.end]
    body = next(
        line
        for line in section.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    quote = body[:limit]
    if len(body) > limit:
        cut = max(quote.rfind("。"), quote.rfind("；"))
        if cut >= 20:
            quote = quote[: cut + 1]
    start = document.text.find(quote)
    if start < 0 or document.text.find(quote, start + 1) >= 0:
        raise ValueError("example_policy_quote_not_unique")
    return {
        "document": document,
        "unit": unit,
        "quote": quote,
        "start": start,
        "end": start + len(quote),
        "section": section,
    }


def _policy_cite(source):
    excerpt_start = source["unit"].start
    excerpt_text = source["section"]
    return {
        "quote": source["quote"],
        "valid": True,
        "invalid_reason": "",
        "role": "政策依据",
        "source_type": "demo_policy",
        "document_label": f"演示政策 {source['document'].document_id}",
        "heading": source["unit"].heading,
        "version_short": source["document"].version,
        "version_full": source["document"].version,
        "anchor_url": "",
        "fixture_cross_topic": False,
        "source_dom_id": "example-policy-"
        + source["document"].document_id.replace(".", "-"),
        "excerpt_segments": display._segments(
            excerpt_text,
            [(source["start"] - excerpt_start, source["end"] - excerpt_start)],
        ),
    }


def _contract_cite(quote, heading):
    start = CONTRACT_TEXT.find(quote)
    if start < 0 or CONTRACT_TEXT.find(quote, start + 1) >= 0:
        raise ValueError("example_contract_quote_not_unique")
    end = start + len(quote)
    paragraph_start = CONTRACT_TEXT.rfind("\n\n", 0, start) + 2
    paragraph_end = CONTRACT_TEXT.find("\n\n", end)
    if paragraph_end < 0:
        paragraph_end = len(CONTRACT_TEXT)
    excerpt = CONTRACT_TEXT[paragraph_start:paragraph_end]
    return {
        "quote": quote,
        "valid": True,
        "invalid_reason": "",
        "role": "合同事实",
        "source_type": "contract",
        "document_label": "示例合同（虚构）",
        "heading": heading,
        "version_short": "示例版本 1.0",
        "version_full": CONTRACT_VERSION,
        "anchor_url": "",
        "fixture_cross_topic": False,
        "source_dom_id": "example-contract",
        "excerpt_segments": display._segments(
            excerpt, [(start - paragraph_start, end - paragraph_start)]
        ),
        "start": start,
        "end": end,
    }


def _item(
    task_id,
    number,
    conclusion,
    question,
    reason,
    contract_quotes,
    policy_refs,
    requested=None,
    opinions=None,
):
    key = task_id.removeprefix("RAG-PILOT-")
    status_label, status_tone = display.CONCLUSIONS[conclusion]
    contract_cites = [_contract_cite(quote, heading) for quote, heading in contract_quotes]
    policy_cites = [_policy_cite(source) for source in policy_refs]
    citations = contract_cites + policy_cites
    evidence = {
        "error": "",
        "simulated": False,
        "contract_cites": contract_cites,
        "material_cites": [],
        "policy_cites": policy_cites,
        "citations": citations,
        "has_any": True,
        "valid_count": len(citations),
        "invalid_count": 0,
        "notice": "示例引用均可在示例合同或虚构政策快照中逐字定位；示例不证明模型能力。",
        "source_note": "示例文本为人工构造，不是 PDF 版面。",
    }
    return {
        "task_id": task_id,
        "number": number,
        "title": display.TASK_TITLES[key],
        "status": {"label": status_label, "tone": status_tone, "kind": "example"},
        "candidate": status_label,
        "candidate_tone": status_tone,
        "question": question,
        "reason": reason,
        "failure": "",
        "program_status": "示例数据；不来自程序执行或模型调用",
        "evidence": evidence,
        "requested_materials": requested or [],
        "opinions": opinions or [],
        "applicability": "示例：仅用于展示签署前六项检查的产品页面结构。",
        "exceptions": "示例：所有引文和结论均为人工构造，不代表真实交易判断。",
    }


def _requested(name, status, statement):
    label, tone = display.MATERIAL_STATUS[status]
    return {
        "id": name,
        "name": display.MATERIAL_NAMES[name],
        "status": status,
        "status_label": label,
        "tone": tone,
        "statement": statement,
    }


def _contract_blocks(spans):
    blocks = []
    current = None
    position = 0
    for line in CONTRACT_TEXT.splitlines(keepends=True):
        start = position
        position += len(line)
        content = line.rstrip("\n")
        if not content.strip():
            if current is not None:
                blocks.append(current)
                current = None
            continue
        heading = re.match(r"^(#{1,2})\s+(.*)$", content)
        if heading:
            if current is not None:
                blocks.append(current)
                current = None
            text = heading.group(2)
            blocks.append(
                {
                    "kind": "h1" if heading.group(1) == "#" else "h2",
                    "text": text,
                    "segments": [{"text": text, "mark": False}],
                }
            )
            continue
        if current is None:
            current = {"kind": "p", "start": start, "end": start + len(content), "lines": [content]}
        else:
            current["lines"].append(content)
            current["end"] = start + len(content)
    if current is not None:
        blocks.append(current)
    for block in blocks:
        if block["kind"] != "p":
            continue
        text = "\n".join(block["lines"])
        local = [
            (start - block["start"], end - block["start"])
            for start, end in spans
            if block["start"] <= start and end <= block["end"]
        ]
        block["text"] = text
        block["segments"] = display._segments(text, local)
    return blocks


def example_report(knowledge):
    pay_default = _policy_source(knowledge, "DP-PAY", "2. 默认要求与证据")
    pay_exception = _policy_source(knowledge, "DP-PAY", "4. 例外与审批")
    acc_precheck = _policy_source(knowledge, "DP-ACC", "2. 签署前检查")
    acc_layers = _policy_source(knowledge, "DP-ACC", "3. 分层验收与期限")
    acc_exception = _policy_source(knowledge, "DP-ACC", "4. 异常、例外与付款关联")
    exc_apply = _policy_source(knowledge, "DP-EXC", "2. 申请内容")
    exc_procedure = _policy_source(knowledge, "DP-EXC", "3. 审批程序与证据")
    exc_validity = _policy_source(knowledge, "DP-EXC", "5. 批准效力与变更")

    pay_quote = (
        "合同生效后5个工作日内，买方支付合同总价30%作为预付款。"
        "设备送达买方指定厂房后10个工作日内，买方支付至合同总价的90%。"
        "剩余10%在收到卖方发票后支付。买方内部审批未完成的，付款时间相应顺延，卖方不得催告。"
    )
    approval_quote = (
        "合同履行中如需变更付款比例或验收安排，由采购经办与卖方协商，"
        "经采购部门负责人同意后生效，无需提交书面例外申请。"
    )
    acceptance_quote = (
        "买方在到货时签署收货单，签署收货单即视为完成数量、外观和质量验收，"
        "不再另行组织质量检验。技术附件由卖方在交付时提供，以卖方最新版本为准。"
    )
    notice_quote = (
        "买方发现设备问题的，应在3个工作日内通知卖方。逾期未通知的，视为设备质量符合约定。"
    )

    example_opinions = {
        "RAG-PILOT-PAY-01": [
            {
                "reviewer": "示例复核人（虚构）",
                "decision": "待复核",
                "note": "示例意见：30%预付款和内部审批顺延需要财务、法务复核；不是真实签署。",
                "saved_at": "2026-11-08T09:30:00+00:00",
                "reviewer_notice": "示例人工意见为虚构；不构成电子签名。",
            }
        ],
        "RAG-PILOT-ACC-01": [
            {
                "reviewer": "示例复核人（虚构）",
                "decision": "更正",
                "note": "示例意见：到货签收不能替代质量验收，需补充检验方法、期限和异议程序。",
                "saved_at": "2026-11-08T09:35:00+00:00",
                "reviewer_notice": "示例人工意见为虚构；不构成电子签名。",
            }
        ],
    }

    items = [
        _item(
            "RAG-PILOT-PAY-01",
            1,
            "deviation",
            "合同约定30%预付款、到货后付至90%、内部审批可顺延付款，且未把质量验收作为付款前提；"
            "与演示政策默认的验收后30个自然日付款和“内部程序不得拖延到期义务”不一致。",
            "示例偏离候选：付款触发、验收前提和内部程序限制均需人工核对。",
            [(
                pay_quote,
                "第二条 付款安排",
            )],
            [pay_default],
            opinions=display._opinions(
                {"human_opinions": [
                    {"task_id": "RAG-PILOT-PAY-01", **example_opinions["RAG-PILOT-PAY-01"][0]}
                ]}
            ),
        ),
        _item(
            "RAG-PILOT-PAY-02",
            2,
            "needs_review",
            "30%预付款和到货后付至90%属于非默认付款安排；合同仅约定采购部门负责人同意，"
            "是否已按演示政策完成财务、法务前置复核和企业负责人书面批准，当前材料为未提供。",
            "示例待确认：需要核对例外批准链和批准版本；材料未提供不证明批准不存在。",
            [(
                pay_quote,
                "第二条 付款安排",
            ), (
                approval_quote,
                "第四条 质量异议与变更",
            )],
            [pay_exception, exc_procedure],
            requested=[
                _requested("approval_record", "missing", "示例：未提供签署前批准或例外批准文件。")
            ],
        ),
        _item(
            "RAG-PILOT-ACC-01",
            3,
            "deviation",
            "合同把“签署收货单”直接等同于完成数量、外观和质量验收，不再组织质量检验；"
            "技术附件还以卖方最新版本为准，缺少固定版本、检验方法和期限。",
            "示例偏离候选：验收分层、技术附件版本和检验方法需要人工核对。",
            [(
                acceptance_quote,
                "第三条 交付与验收",
            )],
            [acc_precheck, acc_layers],
            opinions=display._opinions(
                {"human_opinions": [
                    {"task_id": "RAG-PILOT-ACC-01", **example_opinions["RAG-PILOT-ACC-01"][0]}
                ]}
            ),
        ),
        _item(
            "RAG-PILOT-ACC-02",
            4,
            "needs_review",
            "合同只约定3个工作日问题通知期，逾期视为质量符合；没有整改复验、延期检验、"
            "资源安排与对方同意的区分，也没有付款触发变化的处理约定。",
            "示例待确认：需要业务确认通知期是否可实施，以及异常处理是否已按政策完成审批。",
            [(
                notice_quote,
                "第四条 质量异议与变更",
            )],
            [acc_exception],
            requested=[
                _requested("approval_record", "missing", "示例：未提供异常处理或例外批准文件。")
            ],
        ),
        _item(
            "RAG-PILOT-EXC-01",
            5,
            "needs_review",
            "合同第四条允许采购部门负责人同意后变更付款或验收安排，并写明无需书面例外申请；"
            "演示政策要求逐字偏离说明、财务/质量/法务前置复核及企业负责人最终书面批准。"
            "本项还缺少 DP-AUTH 授权政策依赖。",
            "示例待确认：需要核对例外申请、授权链和最终批准；缺少 DP-AUTH 时不能形成确定性结论。",
            [(
                approval_quote,
                "第四条 质量异议与变更",
            )],
            [exc_apply, exc_procedure],
            requested=[
                _requested("approval_record", "missing", "示例：未提供例外申请或批准文件。"),
                _requested("authority_record", "unknown", "示例：尚未核实签署和批准授权。"),
            ],
        ),
        _item(
            "RAG-PILOT-EXC-02",
            6,
            "not_applicable",
            "示例合同写明无需提交书面例外申请，材料中也没有已有例外批准或附条件批准记录；"
            "因此“既有批准的条件履行和版本效力”目前为不适用候选。",
            "示例不适用候选：没有既有批准可供核对；但“无需书面申请”本身仍需按 EXC-01 处理。",
            [(
                approval_quote,
                "第四条 质量异议与变更",
            )],
            [exc_validity],
        ),
    ]

    states = [
        {
            "material_id": "size_declaration",
            "status": "unknown",
            "statement": "示例：尚未核实双方规模声明。",
        },
        {
            "material_id": "technical_spec",
            "status": "missing",
            "statement": "示例：合同称由卖方交付时提供，当前未提供固定版本。",
        },
        {
            "material_id": "approval_record",
            "status": "missing",
            "statement": "示例：未提供签署前批准或例外批准文件。",
        },
        {
            "material_id": "authority_record",
            "status": "unknown",
            "statement": "示例：尚未核实签署和批准授权。",
        },
        {
            "material_id": "condition_records",
            "status": "missing",
            "statement": "示例：不存在附条件批准记录。",
        },
    ]

    contract_spans = []
    for item in items:
        for cite in item["evidence"]["contract_cites"]:
            contract_spans.append((cite["start"], cite["end"]))
    contract_segments = display._segments(CONTRACT_TEXT, contract_spans)
    for cite in (
        item["evidence"]["contract_cites"][0] for item in items if item["evidence"]["contract_cites"]
    ):
        cite.pop("start", None)
        cite.pop("end", None)

    material_rows = display._materials(states, [])
    return {
        "example": True,
        "mode": display.example_mode(),
        "status": {
            "label": "示例报告",
            "tone": "example",
            "detail": "人工构造的完整示例，不是模型运行，不进入运行或评估统计。",
        },
        "summary": {
            "headline": "6 项示例结果：2 项偏离候选、3 项待确认、1 项不适用候选",
            "subline": "示例覆盖不同结果形态、材料缺口和证据位置，用于展示页面结构与阅读顺序。",
            "chips": [
                {"label": "偏离候选", "value": 2, "tone": "deviation"},
                {"label": "待确认事项", "value": 3, "tone": "review"},
                {"label": "不适用候选", "value": 1, "tone": "neutral"},
            ],
            "note": "示例数据不是模型成绩；真实运行的所有候选仍需人工复核。",
        },
        "scope": {
            "contract_name": CONTRACT_NAME,
            "version_short": "示例版本 1.0",
            "version_full": CONTRACT_VERSION,
            "stage": "签署前（六项试点）",
            "task_count": 6,
            "policy_labels": [
                "DP-PAY 0.2.0",
                "DP-ACC 0.2.1",
                "DP-EXC 0.2.0",
            ],
            "parse_status": "example",
            "parse_label": "示例全文（人工构造）",
            "parse_tone": "example",
            "characters": len(CONTRACT_TEXT),
            "blocks": len([line for line in CONTRACT_TEXT.splitlines() if line.strip()]),
            "format": "示例文本",
            "coverage_scope": "示例合同全文由人工构造，用于展示检查范围；不代表真实解析能力。",
            "warnings": [
                "示例合同、材料、结果和人工意见均为虚构，不可用于真实交易。",
                "示例不调用模型、不进入运行存储，也不参与评估统计。",
            ],
            "simulated": False,
        },
        "materials": material_rows,
        "task_items": items,
        "contract": {
            "name": CONTRACT_NAME,
            "version": CONTRACT_VERSION,
            "segments": contract_segments,
            "blocks": _contract_blocks(contract_spans),
            "characters": len(CONTRACT_TEXT),
        },
        "diagnostics": {
            "lines": [
                ("示例性质", "人工构造展示数据；不调用模型，不写运行存储"),
                ("合同", CONTRACT_NAME),
                ("政策", "虚构演示政策 DP-PAY 0.2.0、DP-ACC 0.2.1、DP-EXC 0.2.0"),
                ("定位", "所有示例引文均按示例合同全文或政策快照逐字核对"),
                ("限制", "示例不证明模型效果、业务准确率或产品验收"),
            ],
            "report": {},
            "config": {},
        },
        "opinions": display._opinions(
            {
                "human_opinions": [
                    {"task_id": task_id, **opinion}
                    for task_id, rows in example_opinions.items()
                    for opinion in rows
                ]
            }
        ),
        "report_title": "示例审查报告（人工构造）",
        "report_subtitle": "人工构造的完整示例，用于展示页面结构；不是模型成绩或法律意见。",
        "back_href": "/",
        "json_href": "",
        "print_href": "/example/print",
        "generated_note": "示例数据为人工构造；不来自任何模型调用或真实合同。",
    }
