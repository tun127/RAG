"""Offline evaluation boundary. Gold may be audited here, never in model requests.

This adapter imports the unchanged evaluator only to audit its data snapshot. It
does not return labels or expected excerpts to the producer or execution loop.
"""
import importlib.util
import json
from pathlib import Path

from production_rag.review_inputs.models import digest


def _audit(root, dataset):
    script = Path(__file__).resolve().parents[3] / "scripts/contract_eval.py"
    spec = importlib.util.spec_from_file_location("contract_eval_binding_audit", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, _, audit = module.load_dataset(root, dataset)
    if not audit["valid"]:
        raise ValueError("评估数据审计失败，禁止评分导出")
    return audit


def create_binding(run, root: Path):
    """Must run BEFORE execution; never invent or fill a missing snapshot ID."""
    root = root.resolve()
    audit = _audit(root, run.dataset)
    if run.evaluation_snapshot_id is None or run.evaluation_snapshot_id != audit["snapshot_id"]:
        raise ValueError("运行与评估快照不一致或未预先指定")
    base = root / "data" / run.dataset
    demo = run.dataset == "contract-demo"
    def read(path):
        raw = path.read_bytes()
        if audit["snapshot"]["files"].get(path.relative_to(root).as_posix()) != digest(raw):
            raise ValueError("审计后材料变化")
        return raw

    manifest = json.loads(read(base / ("manifest.json" if demo else "fixtures/manifest.json")))
    cases = [c for c in manifest["cases"] if c["case_id"] == run.external_case_id]
    if len(cases) != 1:
        raise ValueError("案例身份不一致")
    case = cases[0]
    paths = {role: case["documents"][role]["path"] for role in ("standard", "supplier")} if demo else {
        "standard": case["standard_file"], "supplier": case["candidate_file"],
    }
    mappings = {}
    for role, relative in paths.items():
        path = (base / relative).resolve()
        if not path.is_relative_to(base.resolve()):
            raise ValueError("评估文件越界")
        raw = read(path)
        key = path.relative_to(root).as_posix()
        document = next(d for d in run.documents if d.role == role)
        if (audit["snapshot"]["files"].get(key) != digest(raw)
                or document.raw_sha256 != digest(raw)
                or document.text != raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")):
            raise ValueError("案例合同与运行材料不一致")
        mappings[document.document_id] = relative
    rule_bytes = read(base / "rules.json")
    if digest(rule_bytes) != run.rule_sha256:
        raise ValueError("评估规则与运行规则不一致")
    rules = json.loads(rule_bytes)["rules"]
    if len(rules) != 15 or {
        (r["rule_id"], r["version"]) for r in rules
    } != {(t.rule_id, t.rule_version) for t in run.tasks}:
        raise ValueError("评估规则清单不一致")
    return {
        "binding_version": "review-eval-binding-v1", "snapshot_id": audit["snapshot_id"],
        "dataset": run.dataset, "case_id": run.external_case_id,
        "input_sha256": digest(run.model_dump_json().encode()),
        "rule_sha256": run.rule_sha256, "material_inventory_sha256": run.material_inventory_sha256,
        "document_files": mappings,
        "limitations": ["material_inventory_is_operator_declared_not_dataset_gold",
                        "v2_scores_supplier_text_citations_only",
                        "single_case_export_not_whole_dataset_score"],
    }


def export_rows(run, record, root: Path):
    """Recheck current audited snapshot and the saved pre-execution binding."""
    binding = create_binding(run, root)
    if (record.get("evaluation_binding") != binding
            or record.get("input_sha256") != binding["input_sha256"]
            or record.get("run_id") != run.run_id
            or record.get("dataset") != run.dataset
            or "finished_at" not in record):
        raise ValueError("无有效执行前绑定或执行记录不匹配，禁止评分导出")
    results = record["results"]
    block_maps = None
    if record["config"]["output_protocol"] == "review-result-v2":
        from production_rag.review_execution.blocks import DocumentBlocks, verify_maps

        block_maps = tuple(DocumentBlocks.model_validate(m) for m in record["block_maps"])
        verify_maps(run, block_maps)
        if record["block_maps_sha256"] != digest(json.dumps(record["block_maps"], sort_keys=True).encode()):
            raise ValueError("保存的块映射摘要不符")
    if (len(results) != 15 or {r["task_id"] for r in results} != {t.task_id for t in run.tasks}):
        raise ValueError("执行记录任务不完整或重复")
    rows = []
    by_id = {t.task_id: t for t in run.tasks}
    for result in results:
        task = by_id[result["task_id"]]
        if result["rule_id"] != task.rule_id or result["rule_version"] != task.rule_version:
            raise ValueError("结果规则身份不匹配")
        status = result["runtime_status"]
        failure = None
        evidence = []
        if status == "success":
            # Revalidate every saved citation. Only supplier ranges map to v2.
            for ev in result["evidence"]:
                if block_maps is not None:
                    from production_rag.review_execution.locator import locate
                    from production_rag.review_execution.validation import BlockCitation

                    located = locate(run, BlockCitation.model_validate(ev["model_evidence"]), block_maps)
                    if any(ev.get(k) != v for k, v in located.items()):
                        raise ValueError("保存的声明/块定位不一致")
                document = next(d for d in run.documents if d.document_id == ev["document_id"])
                checked = document.cite(ev["start"], ev["end"])
                if any(ev.get(k) != v for k, v in checked.items()):
                    raise ValueError("保存的引用不再匹配固定输入")
                if document.role == "supplier":
                    evidence.append({"file": binding["document_files"][document.document_id],
                                     "start": ev["start"], "end": ev["end"], "quote": ev["quote"]})
        else:
            status = "failed"
            failure = result.get("failure_type")
            failure = failure if failure in ("api_error", "parse_error", "internal_error") else "invalid_output"
        rows.append({
            "schema_version": "2", "snapshot_id": binding["snapshot_id"],
            "dataset": run.dataset, "case_id": run.external_case_id,
            "run_id": run.run_id, "execution_id": record["execution_id"],
            "rule_id": task.rule_id, "rule_version": task.rule_version,
            "runtime_status": status, "conclusion": result["conclusion"] if status == "success" else None,
            "failure_type": failure, "evidence": evidence,
            "simulation": record["simulation"],
            "export_limitations": binding["limitations"] + (
                ["no_supplier_quote_v2_will_report_missing_evidence"]
                if status == "success" and not evidence else []),
            "execution_detail": {"runtime_status": result["runtime_status"],
                                 "failure_type": result["failure_type"],
                                 "not_executed_reason": result["not_executed_reason"]},
        })
    return rows
