"""Explicit input allowlist; never walks directories or reads evaluation answers."""

import base64
import json
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from production_rag.review_inputs.models import Material, RuleTask, RunInput, TextInput, digest
from production_rag.rule_retrieval import RuleRetriever, Scope


def _read(root, relative, kind, dataset):
    relative = Path(relative)
    path = (root / relative).resolve()
    if path != root / relative:
        raise ValueError("输入白名单不接受路径重定向")
    if relative.is_absolute() or not path.is_relative_to(root):
        raise ValueError("输入必须在指定数据包白名单内")
    name = relative.as_posix()
    patterns = {
        "contract-demo": r"M0-00[123]/(?:standard|supplier)\.(?:md|txt)",
        "contract-review-pilot": r"fixtures/F0[123]/(?:standard|risk|safe|incomplete)\.md",
    }
    allowed = (
        name == "rules.json"
        if kind == "rules"
        else bool(re.fullmatch(patterns[dataset], name))
        if kind == "contract"
        else bool(re.fullmatch(r"materials/[A-Za-z0-9_-]+\.(?:md|txt)", name))
    )
    if not allowed:
        raise ValueError("不在合同/规则/材料输入白名单内")
    with path.open("rb") as f:
        raw = f.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError("输入超过512 KiB试验限制")
    return path, raw


def _document(raw, document_id, role):
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text or not text.strip():
        raise ValueError("输入必须是非空UTF-8纯文本")
    return TextInput(
        document_id=document_id,
        role=role,
        raw_base64=base64.b64encode(raw).decode(),
        raw_sha256=digest(raw),
        text=text,
        text_sha256=digest(text.encode()),
    )


def _unique(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("JSON重复键")
        out[k] = v
    return out


def prepare(
    *,
    root: Path,
    dataset: str,
    external_case_id: str,
    standard: str,
    supplier: str,
    scope: Scope,
    materials: tuple[dict, ...] = (),
    evaluation_snapshot_id: str | None = None,
):
    if dataset not in ("contract-demo", "contract-review-pilot"):
        raise ValueError("不支持的数据包命名空间")
    root = root.resolve()
    if standard == supplier:
        raise ValueError("标准和供应商必须显式指定不同输入")
    documents = []
    for path, role in ((standard, "standard"), (supplier, "supplier")):
        _, raw = _read(root, path, "contract", dataset)
        documents.append(_document(raw, "document-" + str(len(documents) + 1).zfill(4), role))
    inventory = []
    for index, item in enumerate(materials, 1):
        if (
            set(item) - {"required", "availability", "path"}
            or type(item.get("required")) is not bool
        ):
            raise ValueError("材料仅接受required/availability/path声明")
        doc_id = None
        if item.get("availability") == "provided":
            _, raw = _read(root, item["path"], "material", dataset)
            doc_id = "document-" + str(len(documents) + 1).zfill(4)
            documents.append(_document(raw, doc_id, "material"))
        elif item.get("path") is not None:
            raise ValueError("缺少或未核实材料不能声明文件路径")
        inventory.append(
            Material(
                material_id=f"material-{index:04}",
                required=item["required"],
                availability=item["availability"],
                document_id=doc_id,
            )
        )
    rule_path, raw = _read(root, "rules.json", "rules", dataset)
    source = json.loads(raw, object_pairs_hook=_unique)
    if dataset == "contract-review-pilot":
        loader = RuleRetriever(rule_path)
        if loader.sha256 != digest(raw):
            raise ValueError("加载期间规则变化，请重新创建运行")
        selected = loader.search("完整检查清单", scope=scope, method="all", top_k=None)
        rows = [
            (
                r["source_id"],
                r["rule_id"],
                r["version"],
                r["source_pointer"],
                source["rules"][int(r["source_pointer"].split("/")[-1])],
            )
            for r in selected
        ]
    else:
        if source.get("version") != "0.1" or source.get("demo_only") is not True:
            raise ValueError("不支持的M0规则版本")
        rows = []
        for i, r in enumerate(source["rules"]):
            if any(
                not isinstance(r.get(k), str) or not r[k].strip()
                for k in (
                    "rule_id",
                    "version",
                    "acceptance",
                    "exception",
                    "business_conditions",
                    "basis",
                    "enterprise_role",
                    "contract_type",
                )
            ):
                raise ValueError("M0规则字段不完整")
            if (
                r["enterprise_role"],
                r["contract_type"],
                r["version"],
                "synthetic_enterprise_policy_not_law",
            ) == tuple(vars(scope).values()):
                rows.append(
                    ("contract-demo-authored-policy", r["rule_id"], r["version"], f"/rules/{i}", r)
                )
    if len(rows) != 15 or len({(a, b, c) for a, b, c, _, _ in rows}) != 15:
        raise ValueError("适用范围必须恰好包含15条唯一规则")
    run_id = uuid.uuid4().hex
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[3], text=True
    ).strip()
    tasks = tuple(
        RuleTask(
            task_id=f"{dataset}:{run_id}:rule-{i:02}",
            source_id=a,
            rule_id=b,
            rule_version=c,
            source_pointer=p,
            rule_json=json.dumps(r, ensure_ascii=False, sort_keys=True),
        )
        for i, (a, b, c, p, r) in enumerate(sorted(rows), 1)
    )
    inventory_bytes = json.dumps(
        [m.model_dump() for m in inventory], sort_keys=True, ensure_ascii=False
    ).encode()
    return RunInput(
        run_id=run_id,
        dataset=dataset,
        external_case_id=external_case_id,
        created_at=datetime.now(UTC).isoformat(),
        code_revision=revision,
        adapter_code_sha256=digest(
            b"".join(
                p.name.encode() + b"\x00" + p.read_bytes()
                for p in sorted(Path(__file__).parent.glob("*.py"))
            )
        ),
        evaluation_snapshot_id=evaluation_snapshot_id,
        documents=tuple(documents),
        materials=tuple(inventory),
        material_inventory_sha256=digest(inventory_bytes),
        rule_scope_json=json.dumps(vars(scope), sort_keys=True),
        rule_raw_base64=base64.b64encode(raw).decode(),
        rule_sha256=digest(raw),
        tasks=tasks,
    )


def work_item(run: RunInput, task_id: str):
    """Future runner boundary. Metadata is not part of the future model payload."""
    task = next((t for t in run.tasks if t.task_id == task_id), None)
    if task is None:
        raise ValueError("任务不属于此运行")
    return {
        "execution_envelope": {
            "dataset": run.dataset,
            "case_id": run.external_case_id,
            "run_id": run.run_id,
            "snapshot_id": run.evaluation_snapshot_id,
            "output_protocol_target": run.output_protocol,
            "task_id": task.task_id,
            "runtime_status": "pending",
            "conclusion": None,
        },
        "model_input": {
            "documents": [
                {
                    "document_id": d.document_id,
                    "role": d.role,
                    "text": d.text,
                    "text_sha256": d.text_sha256,
                }
                for d in run.documents
            ],
            "materials": [m.model_dump() for m in run.materials],
            "rule": json.loads(task.rule_json),
        },
    }
