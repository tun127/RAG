import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TextInput(Frozen):
    document_id: str
    role: Literal["standard", "supplier", "material"]
    raw_base64: str
    raw_sha256: str
    text: str
    text_sha256: str

    @model_validator(mode="after")
    def verify(self):
        raw = base64.b64decode(self.raw_base64, validate=True)
        decoded = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        if (
            digest(raw) != self.raw_sha256
            or self.text != decoded
            or digest(self.text.encode()) != self.text_sha256
        ):
            raise ValueError("文档快照或文字映射校验失败")
        return self

    def cite(self, start: int, end: int):
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(self.text):
            raise ValueError("无效字符区间")
        return dict(
            document_id=self.document_id,
            role=self.role,
            text_sha256=self.text_sha256,
            start=start,
            end=end,
            quote=self.text[start:end],
        )


class Material(Frozen):
    material_id: str
    required: bool
    availability: Literal["provided", "missing", "unverified"]
    document_id: str | None = None


class RuleTask(Frozen):
    task_id: str
    source_id: str
    rule_id: str
    rule_version: str
    source_pointer: str
    rule_json: str
    execution_status: Literal["pending"] = "pending"
    conclusion: None = None


class RunInput(Frozen):
    run_id: str
    dataset: Literal["contract-demo", "contract-review-pilot"]
    external_case_id: str
    created_at: str
    code_revision: str
    adapter_code_sha256: str
    adapter_version: Literal["review-text-v1"] = "review-text-v1"
    output_protocol: Literal["2"] = "2"
    evaluation_snapshot_id: str | None = None
    documents: tuple[TextInput, ...]
    materials: tuple[Material, ...]
    material_inventory_sha256: str
    rule_scope_json: str
    rule_raw_base64: str
    rule_sha256: str
    tasks: tuple[RuleTask, ...]

    @model_validator(mode="after")
    def verify(self):
        if not re.fullmatch("[a-f0-9]{32}", self.run_id) or not re.fullmatch(
            "[a-f0-9]{40}", self.code_revision
        ):
            raise ValueError("无效运行或代码版本")
        if self.evaluation_snapshot_id is not None and not re.fullmatch(
            "[a-f0-9]{64}", self.evaluation_snapshot_id
        ):
            raise ValueError("无效评估快照标识")
        ids = [d.document_id for d in self.documents]
        if (
            len(set(ids)) != len(ids)
            or sum(d.role == "standard" for d in self.documents) != 1
            or sum(d.role == "supplier" for d in self.documents) != 1
        ):
            raise ValueError("必须明确唯一标准/供应商角色")
        if (
            len(self.tasks) != 15
            or len({(t.source_id, t.rule_id, t.rule_version) for t in self.tasks}) != 15
            or len({t.task_id for t in self.tasks}) != 15
        ):
            raise ValueError("必须恰好15项唯一规则任务")
        if digest(base64.b64decode(self.rule_raw_base64, validate=True)) != self.rule_sha256:
            raise ValueError("规则摘要不符")
        source = json.loads(base64.b64decode(self.rule_raw_base64))
        for task in self.tasks:
            if not re.fullmatch(r"/rules/\d+", task.source_pointer):
                raise ValueError("无效规则来源位置")
            original = source["rules"][int(task.source_pointer.split("/")[-1])]
            if (
                json.loads(task.rule_json) != original
                or task.rule_id != original["rule_id"]
                or task.rule_version != original["version"]
            ):
                raise ValueError("规则任务与固定原文不符")
        inventory = json.dumps(
            [m.model_dump() for m in self.materials], sort_keys=True, ensure_ascii=False
        ).encode()
        if digest(inventory) != self.material_inventory_sha256 or len(
            {m.material_id for m in self.materials}
        ) != len(self.materials):
            raise ValueError("材料清单摘要或身份错误")
        docs = {d.document_id: d for d in self.documents}
        for m in self.materials:
            if m.availability == "provided":
                if m.document_id not in docs or docs[m.document_id].role != "material":
                    raise ValueError("已提供材料须绑定实际材料快照")
            elif m.document_id is not None:
                raise ValueError("未提供材料不能绑定文档")
        if {d.document_id for d in self.documents if d.role == "material"} != {
            m.document_id for m in self.materials if m.availability == "provided"
        }:
            raise ValueError("材料文档必须进入清单")
        return self


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def save(self, run: RunInput):
        run = RunInput.model_validate_json(run.model_dump_json())
        folder = self.root / run.dataset / run.run_id
        folder.mkdir(parents=True, exist_ok=False)
        payload = run.model_dump_json().encode()
        envelope = json.dumps(
            {"sha256": digest(payload), "payload": payload.decode()}, ensure_ascii=False
        ).encode()
        with (folder / "input.json").open("xb") as stream:
            stream.write(envelope)
        return folder

    def load(self, dataset: str, run_id: str):
        if dataset not in ("contract-demo", "contract-review-pilot") or not re.fullmatch(
            "[a-f0-9]{32}", run_id
        ):
            raise ValueError("无效运行命名空间")
        data = json.loads((self.root / dataset / run_id / "input.json").read_bytes())
        if digest(data["payload"].encode()) != data["sha256"]:
            raise ValueError("运行快照完整性错误")
        run = RunInput.model_validate_json(data["payload"])
        if run.dataset != dataset or run.run_id != run_id:
            raise ValueError("运行身份错误")
        return run
