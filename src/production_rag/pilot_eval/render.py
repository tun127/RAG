"""Markdown rendering for the M3.2 pilot evaluation report."""

from __future__ import annotations

import json
from typing import Any

ENVIRONMENT_NOTICE = {
    "SIMULATED": (
        "**SIMULATED — 离线替身产物。** 本报告只评估程序闭环，不代表模型质量、"
        "业务准确率、法律意见或真实审查效果。"
    ),
    "LIVE": (
        "**LIVE — 真实运行产物。** 分类仍只与单作者待复核草案比对，"
        "不是业务准确率；语义支持等待人工复核。"
    ),
    "MIXED": (
        "**MIXED — 同时发现 SIMULATED 与 LIVE 产物。** 聚合前必须逐执行核对标签。"
    ),
    "UNKNOWN": "**UNKNOWN — 无法从执行输入确定离线/真实环境。**",
}


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    environment = report["environment"]
    lines.append(f"# {report['title']}")
    lines.append("")
    lines.append(f"> {ENVIRONMENT_NOTICE.get(environment, ENVIRONMENT_NOTICE['UNKNOWN'])}")
    lines.append(">")
    lines.append("> **业务准确率：未测量（business_accuracy_measured = false）。**")
    lines.append("> **语义支持：未评估（semantic_support_measured = false），单列等待人工复核。**")
    lines.append("> **needs_review / not_executed 单列，不进入分类一致率分母。**")
    lines.append("> 引用定位成功只证明指定 document/block 内唯一逐字命中，不证明支持结论。")
    lines.append("")
    lines.append(f"- 运行根目录：`{report['run_root']}`")
    lines.append(f"- 案例清单：`{report['cases_manifest']}`")
    lines.append(f"- 生成时间：`{report['generated_at_utc']}`")
    lines.append(f"- 报告协议：`{report['schema']}`")
    lines.append("")

    aggregate = report["aggregate"]
    classification = aggregate["classification"]
    citation = aggregate["citation_precision"]
    evidence = aggregate["evidence_support"]
    coverage = aggregate["coverage"]

    lines.append("## 总览")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 环境 | **{environment}** |")
    lines.append(f"| 执行数 | {aggregate['executions']} |")
    lines.append(f"| 任务槽位 | {aggregate['slots']} |")
    lines.append(f"| 实际尝试 | {aggregate['attempted']} |")
    lines.append(
        "| 程序闭环成功（全部槽位，非业务准确率） | "
        f"{_metric(aggregate['program']['metrics']['program_success_all_slots'])} |"
    )
    lines.append(
        "| 程序闭环成功（已尝试，非业务准确率） | "
        f"{_metric(aggregate['program']['metrics']['program_success_attempted'])} |"
    )
    lines.append(f"| 引用唯一逐字命中 | {_metric(citation['metric'])} |")
    lines.append(
        "| 分类与单作者草案一致率（非业务准确率） | "
        f"{_metric(classification['metric'])} |"
    )
    lines.append(
        "| 人工复核要求 | "
        f"{evidence['manual_review_required_true']} true / "
        f"{evidence['review_result_rows']} 有结果任务 |"
    )
    lines.append("| 业务准确率 | 未测量 |")
    lines.append("| 语义支持 | 未评估 |")
    lines.append("")

    lines.append("## 程序闭环成功分解（非业务准确率）")
    lines.append("")
    lines.append("| 阶段 | 结果 |")
    lines.append("| --- | --- |")
    program = aggregate["program"]
    for label, key in (
        ("请求构造", "request_ok"),
        ("HTTP 成功", "http_ok"),
        ("旧结构检查（schema 与记录一致性）", "structure_ok"),
        ("独立引用定位", "citations_ok"),
    ):
        lines.append(f"| {label} | {_metric(program['metrics'][key])} |")
    lines.append("")
    lines.append(
        "程序闭环成功定义为：请求构造、HTTP、结构、引用定位与 pilot validation 全部通过；"
        "未执行项不计为成功，也不计为业务失败。该指标不是模型质量或业务准确率。"
    )
    lines.append("")

    lines.append("## 独立引用逐字定位（非语义精度）")
    lines.append("")
    lines.append("| 声明引文 | 唯一逐字 valid | invalid | ambiguous | missing | 重复声明 |")
    lines.append("| ---: | ---: | ---: | ---: | ---: | ---: |")
    lines.append(
        f"| {citation['declared']} | {citation['valid']} | {citation['invalid']} | "
        f"{citation['ambiguous']} | {citation['missing']} | "
        f"{citation['duplicate_declarations']} |"
    )
    lines.append("")
    lines.append(f"唯一逐字命中率：{_metric(citation['metric'])}")
    lines.append("")
    if citation["by_source_type"]:
        lines.append("| 来源类型 | 声明 | valid | invalid | ambiguous | missing |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for source_type, counts in sorted(citation["by_source_type"].items()):
            lines.append(
                f"| {_escape(source_type)} | {counts['declared']} | {counts['valid']} | "
                f"{counts['invalid']} | {counts['ambiguous']} | {counts['missing']} |"
            )
        lines.append("")

    lines.append("## 分类（仅单作者待复核草案）")
    lines.append("")
    lines.append(
        f"一致率指标：{_metric(classification['metric'])}。"
        "分子/分母只包含“结果与标注均为确定分类”的配对；"
        "needs_review、not_executed、failed、标注为 needs_review 的项均单列，"
        "不混入分母。"
    )
    lines.append("")
    lines.append("| 桶 | 数量 |")
    lines.append("| --- | ---: |")
    for bucket, count in sorted(classification["bucket_counts"].items()):
        lines.append(f"| {_escape(bucket)} | {count} |")
    lines.append("")
    for title, items in (
        ("needs_review（单列，不计准确率）", classification["needs_review"]),
        ("not_executed（单列，不计准确率）", classification["not_executed"]),
        ("failed（单列，不计准确率）", classification["failed"]),
        (
            "annotation_needs_review_excluded（标注为待复核，单列）",
            classification["annotation_needs_review_excluded"],
        ),
        ("no_annotation（无标注匹配，单列）", classification["no_annotation"]),
    ):
        if not items:
            continue
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| 执行 | 案例 | 模式 | 任务 | 标注草案 | 结果候选 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for item in items:
            lines.append(
                f"| {_escape(item['execution_id'])} | {_escape(item['case_id'] or '-')} | "
                f"{_escape(item['mode'])} | {_escape(item['task_id'])} | "
                f"{_escape(item['annotation_draft'] or '-')} | "
                f"{_escape(item['result_conclusion'] or '-')} | {_escape(item['note'] or '-')} |"
            )
        lines.append("")
    if classification["by_mode"]:
        lines.append("### 按模式")
        lines.append("")
        lines.append("| 模式 | 槽位 | 一致率（非业务准确率） |")
        lines.append("| --- | ---: | --- |")
        for mode, item in classification["by_mode"].items():
            lines.append(
                f"| {_escape(mode)} | {item['slots']} | {_metric(item['metric'])} |"
            )
        lines.append("")

    lines.append("## 证据支持（人工复核状态单列）")
    lines.append("")
    lines.append(
        "| 自动语义裁判 | 语义支持测量 | 有结果任务 | manual_review_required=true | "
        "semantic_support 状态分布 |"
    )
    lines.append("| --- | --- | ---: | ---: | --- |")
    statuses = ", ".join(
        f"{_escape(key)}={value}"
        for key, value in sorted(evidence["semantic_support_status_counts"].items())
    ) or "-"
    lines.append(
        f"| {evidence['automated_semantic_judgement']} | 未测量 | "
        f"{evidence['review_result_rows']} | {evidence['manual_review_required_true']} | "
        f"{statuses} |"
    )
    lines.append("")
    lines.append(
        "引用定位与语义支持完全分开：本报告不运行 LLM judge，不把引文位置正确、"
        "程序门槛通过或草案分类一致冒充语义正确。"
    )
    lines.append("")

    lines.append("## 覆盖")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 有标注匹配的案例 | {', '.join(coverage['cases_with_annotation_match']) or '无'} |")
    lines.append(f"| 待标注执行 | {len(coverage['executions_without_annotation'])} |")
    lines.append(f"| 预期槽位 | {coverage['expected_slots']} |")
    lines.append(f"| 请求文件 | {coverage['request_files']} |")
    lines.append(f"| 结果文件 | {coverage['result_files']} |")
    lines.append(f"| final-answers 文件存在数（非使用数） | {coverage['final_answer_fallback_files']} |")
    lines.append(f"| 缺失预期槽位 | {coverage['missing_expected_slots']} |")
    lines.append(f"| 额外槽位 | {coverage['extra_slots']} |")
    lines.append(f"| 未执行项 | {len(coverage['unexecuted_items'])} |")
    lines.append(f"| 失败项 | {len(coverage['failed_items'])} |")
    lines.append(f"| 预期政策范围 | {', '.join(coverage['policy_scope_expected'])} |")
    lines.append(
        f"| 请求政策并集 | {', '.join(coverage['request_policy_ids']) or '无'} |"
    )
    lines.append(
        f"| 结果声明政策并集 | {', '.join(coverage['returned_policy_ids']) or '无'} |"
    )
    lines.append(f"| 缺失任务依赖条目 | {len(coverage['missing_task_dependencies'])} |")
    lines.append(f"| 越界政策 | {', '.join(coverage['out_of_scope_policy_ids']) or '无'} |")
    lines.append(f"| 未声明必需材料条目 | {len(coverage['undeclared_required_materials'])} |")
    lines.append("")
    if coverage["missing_task_dependencies"]:
        lines.append("### 缺失任务依赖")
        lines.append("")
        lines.append("| 执行 | 案例 | 模式 | 任务 | 依赖 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in coverage["missing_task_dependencies"]:
            lines.append(
                f"| {_escape(item['execution_id'])} | {_escape(item['case_id'] or '-')} | "
                f"{_escape(item['mode'])} | {_escape(item['task_id'])} | "
                f"{_escape(item['dependency'])} |"
            )
        lines.append("")
    if coverage["unexecuted_items"]:
        lines.append("### 未执行项")
        lines.append("")
        lines.append("| 执行 | 案例 | 模式 | 任务 | 原因 | 复核状态 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in coverage["unexecuted_items"]:
            lines.append(
                f"| {_escape(item['execution_id'])} | {_escape(item['case_id'] or '-')} | "
                f"{_escape(item['mode'])} | {_escape(item['task_id'])} | "
                f"{_escape(str(item['failure']))} | {_escape(item['review_outcome'])} |"
            )
        lines.append("")
    if coverage["failed_items"]:
        lines.append("### 失败项")
        lines.append("")
        lines.append("| 执行 | 案例 | 模式 | 任务 | 原因 | 复核状态 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in coverage["failed_items"]:
            lines.append(
                f"| {_escape(item['execution_id'])} | {_escape(item['case_id'] or '-')} | "
                f"{_escape(item['mode'])} | {_escape(item['task_id'])} | "
                f"{_escape(str(item['failure']))} | {_escape(item['review_outcome'])} |"
            )
        lines.append("")

    lines.append("## 执行明细")
    lines.append("")
    for execution in report["executions"]:
        _render_execution(lines, execution)

    lines.append("## 分母与 null 原因")
    lines.append("")
    lines.append("| 指标 | 分子 | 分母 | 值 | null 原因 |")
    lines.append("| --- | ---: | ---: | --- | --- |")
    seen = set()
    for metric in aggregate["denominators"]:
        key = (metric["name"], metric["numerator"], metric["denominator"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"| {_escape(metric['name'])} | {metric['numerator']} | "
            f"{metric['denominator']} | {_value(metric)} | "
            f"{_escape(metric['null_reason'] or '-')} |"
        )
    lines.append("")

    lines.append("## 输入哈希")
    lines.append("")
    lines.append("### 案例清单与标注")
    lines.append("")
    lines.append("| 角色 | 路径 | SHA256 | 字节 |")
    lines.append("| --- | --- | --- | ---: |")
    manifest = report["inputs"]["cases_manifest"]
    lines.append(
        f"| cases_manifest | {_escape(manifest['path'] or '-')} | "
        f"`{manifest['sha256'] or '-'}` | {manifest['bytes'] if manifest['bytes'] is not None else '-'} |"
    )
    for annotation in report["inputs"]["annotations"]:
        lines.append(
            f"| annotation | {_escape(annotation['path'] or '-')} | "
            f"`{annotation['sha256'] or '-'}` | "
            f"{annotation['bytes'] if annotation['bytes'] is not None else '-'} |"
        )
    lines.append("")
    for execution in report["executions"]:
        lines.append(f"### {execution['case_id'] or execution['run_id']} / {execution['mode']}")
        lines.append("")
        identity = execution["identity"]
        lines.append(
            f"- 执行：`{execution['execution_id']}`；环境：**{execution['environment']}**；"
            f"run_id：`{execution['run_id']}`"
        )
        lines.append(f"- input_sha256：`{identity['input_sha256']}`")
        lines.append(f"- knowledge_id：`{identity['knowledge_id']}`")
        lines.append(f"- code_revision：`{identity['code_revision']}`")
        lines.append(f"- execution_revision：`{identity['execution_revision']}`")
        lines.append(f"- implementation_sha256：`{identity['implementation_sha256']}`")
        lines.append(f"- prompt_sha256：`{identity['prompt_sha256']}`")
        lines.append("")
        lines.append("<details><summary>输入文件哈希</summary>")
        lines.append("")
        lines.append("| 角色 | 路径 | 存在 | SHA256 | 字节 |")
        lines.append("| --- | --- | --- | --- | ---: |")
        for artifact in execution["artifacts"]:
            lines.append(
                f"| {_escape(artifact['role'])} | {_escape(artifact['path'] or '-')} | "
                f"{'是' if artifact['present'] else '否'} | `{artifact['sha256'] or '-'}` | "
                f"{artifact['bytes'] if artifact['bytes'] is not None else '-'} |"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("## 警告")
    lines.append("")
    for warning in report["warnings"]:
        lines.append(f"- {warning}")
    lines.append("")
    lines.append("## 限制")
    lines.append("")
    for limitation in (
        "本评估器不调用模型、不安装依赖、不运行 judge，只读取 JSON 产物。",
        "SIMULATED 产物的程序成功率不是模型质量或业务准确率。",
        "分类只与单作者开发草案比对，不是独立测试集、业务准确率或法律意见。",
        "引用定位只证明指定块内唯一逐字命中；语义支持与结论正确性仍待人工复核。",
        "final-answers 回退缺 pilot validation，不能建立协议成功。",
        "覆盖统计是产物级覆盖，不证明业务条款、法律适用或 15 份政策已完整审查。",
        "LIVE transport/API 失败样本没有可用审查结论，不得作为业务结果使用。",
    ):
        lines.append(f"- {limitation}")
    lines.append("")
    v2 = report["aggregate"].get("evaluation_v2")
    if v2:
        lines.extend(["", "## V2 独立口径与逐任务对照", "",
                      "旧指标保留为历史诊断；failed 桶表示评分资格，非业务分类。草案一致率不属于可信标注评分。",
                      "schema 可解析、pilot 接受、独立定位分别计数；独立定位不追记原执行成绩或语义支持。",
                      "已返回政策见 coverage；任务引用要求见下表，二者独立。", "",
                      "```json", json.dumps(v2, ensure_ascii=False, indent=2), "```", "",
                      "|任务|原状态|原 failure|schema|pilot 接受|独立定位|分类资格|fallback 存在/读取/采用|返回政策 / 任务引用要求|",
                      "|---|---|---|---|---|---|---|---|---|"])
        for execution in report["executions"]:
            for row in execution["tasks"]:
                f = row["evaluation_v2"]
                lines.append(f"|{row['task_id']}|{row['execution_status']}|{row['failure']}|{f['schema_parse_ok']}|{f['pilot_accepted']}|{f['independent_located']}/{f['independent_declared']}|{f['classification_eligibility']}|{f['fallback']}|{row['coverage']['returned_policy_ids']} / {f['task_citation_requirements_original_result']}|")
    return "\n".join(lines)


def _render_execution(lines: list[str], execution: dict[str, Any]) -> None:
    lines.append(
        f"### {execution['case_id'] or execution['run_id']} / {execution['mode']} / "
        f"{execution['environment']}"
    )
    lines.append("")
    annotation = execution["annotation"]
    lines.append(f"- 执行 ID：`{execution['execution_id']}`")
    lines.append(f"- 运行 ID：`{execution['run_id']}`")
    lines.append(f"- 合同 SHA256：`{execution['contract_sha256'] or '-'}`")
    if annotation:
        lines.append(
            f"- 标注：`{annotation['case_id']}` / `{annotation['status']}`；"
            f"说明：{_escape(annotation.get('scope') or '-')}"
        )
    else:
        lines.append("- 标注：无 contract_sha256 匹配")
    lines.append("")
    lines.append(
        "| 任务 | 槽 | 请求 | 结果 | final answer | 执行状态 | HTTP | 结构 | 引用 | "
        "程序成功 | 旧评分资格桶 | 失败原因 |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for row in execution["tasks"]:
        citations = row["program"]["citations"]
        lines.append(
            f"| {_escape(row['task_id'])} | {'是' if row['slot_present'] else '否'} | "
            f"{'是' if row['request_present'] else '否'} | "
            f"{'是' if row['result_present'] else '否'} | "
            f"{_escape(row['final_answer_source'] or '-')} | "
            f"{_escape(row['execution_status'])} | "
            f"{_stage(row['program']['http']['ok'])} | "
            f"{_stage(row['program']['structure']['ok'])} | "
            f"{_stage(citations['ok'])} ({citations['valid']}/{citations['declared']}) | "
            f"{_stage(row['program']['success'])} | "
            f"{_escape(row['classification']['bucket'])} | "
            f"{_escape(str(row['failure']) if row['failure'] is not None else '-')} |"
        )
    lines.append("")
    for row in execution["tasks"]:
        fields = row.get("reply_fields") or {}
        if fields.get("protocol") == "pilot-review-block-v2":
            lines.append(f"- {_escape(row['task_id'])} 材料状态引用：{_escape(fields.get('material_refs'))}；"
                         f"待补材料：{_escape(fields.get('requested_material_ids'))}；"
                         f"待确认事项：{_escape(fields.get('review_issue'))}")
    lines.append("")
    failures = [
        (row["task_id"], item)
        for row in execution["tasks"]
        for item in row["program"]["citations"]["diagnostics"]
        if item["status"] in {"invalid", "ambiguous", "missing"}
    ]
    if failures:
        lines.append("#### 引用定位失败明细")
        lines.append("")
        lines.append("| 任务 | 序号 | 文档 | 块 | 状态 | 原因 | 引文摘要 |")
        lines.append("| --- | ---: | --- | --- | --- | --- | --- |")
        for task_id, item in failures:
            preview = (item.get("quote") or "").replace("\n", " ")
            if len(preview) > 80:
                preview = preview[:80] + "…"
            lines.append(
                f"| {_escape(task_id)} | {item['index']} | "
                f"{_escape(item.get('document_id') or '-')} | "
                f"{_escape(item.get('block_id') or '-')} | {_escape(item['status'])} | "
                f"{_escape(item['reason'] or '-')} | {_escape(preview or '-')} |"
            )
        lines.append("")
    coverage = execution["coverage"]
    lines.append(
        f"- 请求政策：{', '.join(coverage['request_policy_ids']) or '无'}；"
        f"结果声明政策：{', '.join(coverage['returned_policy_ids']) or '无'}；"
        f"预期范围：{', '.join(coverage['policy_scope_expected'])}"
    )
    lines.append("")
    gaps = []
    if coverage["missing_expected_slots"]:
        gaps.append("缺失槽位：" + ", ".join(coverage["missing_expected_slots"]))
    if coverage["extra_slots"]:
        gaps.append("额外槽位：" + ", ".join(coverage["extra_slots"]))
    if coverage["duplicate_slots"]:
        gaps.append("重复槽位：" + ", ".join(coverage["duplicate_slots"]))
    if coverage["out_of_scope_policy_ids"]:
        gaps.append("越界政策：" + ", ".join(coverage["out_of_scope_policy_ids"]))
    if coverage["missing_primary_policies"]:
        gaps.append("主政策未返回：" + ", ".join(coverage["missing_primary_policies"]))
    if coverage["missing_task_dependencies"]:
        gaps.append("缺失依赖：" + ", ".join(coverage["missing_task_dependencies"]))
    if coverage["undeclared_required_materials"]:
        gaps.append("未声明材料：" + ", ".join(coverage["undeclared_required_materials"]))
    if gaps:
        lines.append("#### 覆盖缺口")
        lines.append("")
        for gap in gaps:
            lines.append(f"- {gap}")
        lines.append("")
    fallback_rows = [row for row in execution["tasks"] if row["final_answer_fallback_used"]]
    if fallback_rows:
        lines.append("#### final-answers 回退")
        lines.append("")
        for row in fallback_rows:
            lines.append(
                f"- {row['task_id']}：使用 `{row['final_answer_fallback_path']}`；"
                "缺 result 或 final_answer，不能建立 pilot 协议成功。"
            )
        lines.append("")


def _metric(metric: dict[str, Any]) -> str:
    return f"{metric['numerator']}/{metric['denominator']} = {_value(metric)}"


def _value(metric: dict[str, Any]) -> str:
    if metric["denominator"] == 0:
        return f"null（{metric['null_reason']}）"
    return f"{metric['value'] * 100:.1f}%"


def _stage(value: Any) -> str:
    if value is True:
        return "通过"
    if value is False:
        return "失败"
    return "不适用"


def _escape(value: Any) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()
