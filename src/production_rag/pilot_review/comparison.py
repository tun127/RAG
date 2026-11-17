"""Post-execution comparison only; no annotations are accepted by execution."""


def compare(a, b, annotation=None):
    if a["config"]["mode"] == b["config"]["mode"] or {a["config"]["mode"], b["config"]["mode"]} != {"all", "rag"}:
        raise ValueError("comparison_requires_both_modes")
    for field in ("input_sha256", "run_id", "knowledge_id", "implementation_sha256", "execution_revision"):
        if a[field] != b[field]:
            raise ValueError("comparison_binding_mismatch")
    if ({k: v for k, v in a["config"].items() if k != "mode"}
            != {k: v for k, v in b["config"].items() if k != "mode"}):
        raise ValueError("comparison_configuration_mismatch")
    if [r["task_id"] for r in a["results"]] != [r["task_id"] for r in b["results"]]:
        raise ValueError("comparison_checklist_mismatch")
    modes = {a["config"]["mode"]: a, b["config"]["mode"]: b}
    rows = []
    for left, right in zip(modes["all"]["results"], modes["rag"]["results"], strict=True):
        row = {"task_id": left["task_id"], "semantic_support": "pending_human_review"}
        for mode, item in (("all", left), ("rag", right)):
            validation = item["validation"] or {}
            row[mode] = {"execution_status": item["execution_status"], "failure": item["failure"],
                         "candidate_classification": item["business_conclusion"],
                         "located_citations": len(validation.get("locations", [])),
                         "citation_failures": sum(d["status"] != "located" for d in validation.get("citation_diagnostics", [])),
                         "request_sha256": item["request_sha256"], "request_bytes": item["request_bytes"],
                         "input_characters": item["input_characters"],
                         "returned_policy_ids": item["returned_policy_ids"],
                         "retrieved_unit_ids": item["retrieved_unit_ids"],
                         "returned_policy_characters": item["returned_policy_characters"],
                         "missing_task_dependencies": item["missing_task_dependencies"]}
        rows.append(row)
    return {"status": "single_author_development_pending_review",
            "simulation": a["simulation"], "business_accuracy_measured": False,
            "input_sha256": a["input_sha256"], "run_id": a["run_id"], "knowledge_id": a["knowledge_id"],
            "modes": {mode: {"program_status_counts": record["status_counts"],
                              "program_success_rate": record["status_counts"]["success"] / len(record["results"]),
                              "attempts": record["attempt_count"], "known_usage": record["known_usage"],
                              "unknown_usage_attempts": record["unknown_usage_attempts"],
                              "input_bytes_total": sum(r["request_bytes"] for r in record["results"])}
                      for mode, record in modes.items()},
            "rows": rows, "posthoc_reference_draft": annotation,
            "limitations": ["离线替身仅测程序；分类和引文语义支持均待人工复核",
                            "两模式共用任务条件摘要；RAG差别是提供的政策原文上下文，而非无政策知识对照"]}
