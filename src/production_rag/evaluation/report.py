import json


def write_report(directory, report):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (report["run_id"] + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    lines = [
        f"# 评估 {report['run_id']}",
        "",
        f"快照：{report['snapshot_id']}",
        "",
        "自动字面覆盖不是语义正确性或忠实度。以下为小样本开发集结果。",
        "",
        "| 模式 | 请求 | 失败 | 证据覆盖 | 字面事实覆盖 |",
        "|---|---:|---:|---:|---:|",
    ]
    for mode, summary in report["summary"].items():
        metrics = summary["metrics"]
        lines.append(
            f"| {mode} | {summary['requests']} | {summary['failures']} | "
            f"{metrics.get('evidence_recall', {}).get('mean')} | "
            f"{metrics.get('fact_string_coverage', {}).get('mean')} |"
        )
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return path
