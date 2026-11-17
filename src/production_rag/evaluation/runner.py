import hashlib
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from uuid import uuid4

from production_rag.evaluation.dataset import load_dataset
from production_rag.evaluation.metrics import generation_metrics, retrieval_metrics


def run_evaluation(services, dataset: Path, modes=("baseline", "agent"), *, judge=False):
    """调用同一 QueryService；固定快照、样本、模型。每题隔离会话和记忆。"""
    samples = load_dataset(dataset)
    snapshot = services.store.snapshot()
    # 标注必须能在当前原文定位；不能静默拿错语料跑分。
    texts = [d.text for d in snapshot.documents]
    for sample in samples:
        if any(not any(e in text for text in texts) for e in sample.expected_evidence):
            raise ValueError(f"{sample.sample_id} 的证据不在当前语料，请核对评估集")
    run_id = uuid4().hex
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unversioned"
    # 未建立 Git 时也保存代码摘要，避免报告无法识别代码。
    source = Path(__file__).resolve().parents[1]
    code_hash = hashlib.sha256()
    for path in sorted(source.rglob("*.py")):
        code_hash.update(path.relative_to(source).as_posix().encode())
        code_hash.update(path.read_bytes())
    rows = []
    for sample in samples:
        for mode in modes:
            try:
                result = services.query.ask(
                    sample.question,
                    mode=mode,
                    user_id=f"eval-{run_id}-{sample.sample_id}-{mode}",
                    session_id="eval",
                    snapshot_id=snapshot.snapshot_id,
                )
                failed = result.stop_reason in {"dependency_error", "deadline", "output_tokens"}
                metrics = {
                    **retrieval_metrics(result.evidence, sample.expected_evidence),
                    **generation_metrics(result.answer, sample.expected_facts, sample.answerable),
                }
                row = {
                    "sample_id": sample.sample_id,
                    "mode": mode,
                    "category": sample.category,
                    "failed": failed,
                    "metrics": metrics,
                    "latency_ms": result.latency_ms,
                    "usage": result.usage,
                    "result": result.model_dump(),
                }
                if judge:
                    from production_rag.evaluation.judge import judge_answer

                    try:
                        row["judge"] = judge_answer(services, sample, result)
                    except Exception as exc:
                        row["judge_error"] = str(exc)
                rows.append(row)
            except Exception as exc:
                rows.append(
                    {
                        "sample_id": sample.sample_id,
                        "mode": mode,
                        "failed": True,
                        "error": str(exc),
                        "metrics": {},
                    }
                )
    summaries = {}
    for mode in modes:
        selected = [r for r in rows if r["mode"] == mode]
        keys = {key for row in selected for key in row["metrics"]}
        metrics = {}
        for key in sorted(keys):
            eligible = [
                r
                for r, s in zip(selected, samples, strict=True)
                if key not in {"hit", "evidence_recall", "mrr"} or s.expected_evidence
            ]
            # 错误计为 0；不可计算的指标（例如无事实标注）单列分母。
            values = [
                0.0 if r["failed"] else r["metrics"].get(key, 0.0)
                for r in eligible
                if r["failed"] or r["metrics"].get(key) is not None
            ]
            metrics[key] = {"mean": mean(values) if values else None, "n": len(values)}
        summaries[mode] = {
            "requests": len(selected),
            "failures": sum(r["failed"] for r in selected),
            "metrics": metrics,
            "mean_latency_ms": mean([r.get("latency_ms", 0) for r in selected]),
        }
    report = {
        "run_id": run_id,
        "created": datetime.now(UTC).isoformat(),
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "dataset": str(dataset),
        "snapshot_id": snapshot.snapshot_id,
        "corpus_config": snapshot.config,
        "settings": services.settings.model_dump(
            mode="json", exclude={"api_token", "milvus_token", "redis_url"}
        ),
        "python": platform.python_version(),
        "revision": revision,
        "code_sha256": code_hash.hexdigest(),
        "summary": summaries,
        "rows": rows,
    }
    from production_rag.evaluation.report import write_report

    target = write_report(services.settings.data_dir / "reports", report)
    return {"run_id": run_id, "report": str(target), "summary": summaries}
