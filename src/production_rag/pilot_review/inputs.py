import base64
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from production_rag.knowledge_baseline.snapshot import Snapshot
from production_rag.review_inputs.models import Frozen, digest

SCOPE = ("DP-PAY", "DP-ACC", "DP-EXC")
TASK_VERSION = "pay-acc-exc-checklist-0.1"


class Source(Frozen):
    document_id: str
    version: str
    source_type: Literal["contract", "material", "demo_policy"]
    raw_base64: str
    raw_sha256: str
    text: str
    text_sha256: str

    @model_validator(mode="after")
    def verify(self):
        raw = base64.b64decode(self.raw_base64, validate=True)
        if (raw.decode("utf-8") != self.text or digest(raw) != self.raw_sha256
                or digest(self.text.encode()) != self.text_sha256):
            raise ValueError("fixed_source_mismatch")
        return self


def source(raw, document_id, version, source_type):
    return Source(document_id=document_id, version=version, source_type=source_type,
                  raw_base64=base64.b64encode(raw).decode(), raw_sha256=digest(raw),
                  text=raw.decode("utf-8"), text_sha256=digest(raw))


class Material(Frozen):
    material_id: str
    status: Literal["provided", "missing", "unknown", "not_applicable"]
    statement: str
    document: Source | None = None

    @model_validator(mode="after")
    def verify(self):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.material_id):
            raise ValueError("invalid_material_id")
        if not self.statement.strip() or (self.status == "provided") != (self.document is not None):
            raise ValueError("material_declaration_mismatch")
        if self.document and self.document.source_type != "material":
            raise ValueError("material_role_mismatch")
        return self


class PolicyRef(Frozen):
    document_id: str
    version: str
    heading: str
    unit_id: str
    source_sha256: str


class Task(Frozen):
    task_id: str
    version: Literal["pay-acc-exc-checklist-0.1"] = TASK_VERSION
    question: str
    sources: tuple[PolicyRef, ...]
    applicability: str
    exceptions: str
    required_materials: tuple[str, ...]
    required_policy_dependencies: tuple[str, ...]


# Manually curated from the named source sections. No retrieval/gold drives membership.
# Summaries are task instructions, not substitutes for the cited source wording.
CATALOG = (
    ("PAY-01", "普通货物付款触发、结算材料和默认30自然日算法是否明确，并区分发票、签收与质量验收？",
     (("DP-PAY", "2. 默认要求与证据"),), "普通货物价款；规模事实不明转人工，不作法律适用判断",
     "更早到期义务不顺延；预付、账期与替代材料偏离按DP-EXC另核",
     ("settlement_records", "size_declaration"), ("DP-ACC",)),
    ("PAY-02", "预付或延期付款偏离是否完成前置复核和最终商业例外，付款与验收是否一致？",
     (("DP-PAY", "4. 例外与审批"), ("DP-ACC", "4. 异常、例外与付款关联")),
     "出现预付款、分期、超默认账期或替代材料时", "部门同意不是最终批准；到期付款不因缺票自动停止",
     ("approval_record", "acceptance_records"), ("DP-EXC",)),
    ("ACC-01", "数量外观签收和质量验收是否分层，检验期限、技术附件及工作日日历是否可核对？",
     (("DP-ACC", "2. 签署前检查"), ("DP-ACC", "3. 分层验收与期限")),
     "有可验证技术规格的普通货物；复杂性能和扫描附件转人工",
     "潜在瑕疵、安装试运转等需合理计划和DP-EXC批准；演示2/10工作日不是法定期限",
     ("technical_spec", "acceptance_records"), ("DP-EXC",)),
    ("ACC-02", "检验延期、资源安排、付款触发变更是否区分并保留对方同意及最终例外？",
     (("DP-ACC", "4. 异常、例外与付款关联"),), "出现验收异常、延期或分批争议时",
     "不能等内部审批错过通知；内部延期不自动改变合同期限，不能无限阻断付款",
     ("acceptance_records", "approval_record"), ("DP-PAY", "DP-EXC")),
    ("EXC-01", "例外申请是否包含偏离、材料版本和保障措施，最终批准权限是否区别于资源及签署授权？",
     (("DP-EXC", "2. 申请内容"), ("DP-EXC", "3. 审批程序与证据"),
      ("DP-EXC", "共同审批术语与边界")), "可申请的政策偏离或标准文本实质变化时",
     "代理须书面授权且无利益冲突；必要角色缺位或意见冲突不能视为已批准",
     ("approval_record", "authority_record"), ("DP-AUTH",)),
    ("EXC-02", "附条件批准及实质变更后，条件履行和批准版本是否仍有效？",
     (("DP-EXC", "5. 批准效力与变更"), ("DP-EXC", "6. 状态、归档与撤回")),
     "存在例外批准、附条件批准或批准后实质变化时", "旧批准不自动继承；法律适用和真实性不能商业豁免",
     ("approval_record", "condition_records"), ()),
)


def tasks(snapshot):
    versions = {d.document_id: d.version for d in snapshot.documents}
    if any(versions.get(k) != v for k, v in {"DP-PAY": "0.2.0", "DP-ACC": "0.2.1", "DP-EXC": "0.2.0"}.items()):
        raise ValueError("checklist_requires_review_for_new_policy_version")
    result = []
    for suffix, question, locations, applicability, exceptions, materials, dependencies in CATALOG:
        refs = []
        for did, heading in locations:
            unit, = [u for u in snapshot.units if u.document_id == did and u.heading == heading]
            d, = [d for d in snapshot.documents if d.document_id == did]
            refs.append(PolicyRef(document_id=did, version=d.version, heading=heading,
                                  unit_id=unit.unit_id, source_sha256=d.sha256))
        result.append(Task(task_id=f"RAG-PILOT-{suffix}", question=question, sources=tuple(refs),
                           applicability=applicability, exceptions=exceptions,
                           required_materials=materials, required_policy_dependencies=dependencies))
    return tuple(result)


class Run(Frozen):
    protocol: Literal["pilot-review-block-v1"] = "pilot-review-block-v1"
    run_id: str
    code_revision: str
    adapter_version: Literal["pilot-exact-utf8-v1"] = "pilot-exact-utf8-v1"
    contract: Source
    materials: tuple[Material, ...]
    knowledge: Snapshot
    knowledge_id: str
    policy_scope: tuple[str, ...] = SCOPE
    checklist: tuple[Task, ...]

    @model_validator(mode="after")
    def verify(self):
        if not re.fullmatch(r"[a-f0-9]{32}", self.run_id) or not re.fullmatch(r"[a-f0-9]{40}", self.code_revision):
            raise ValueError("invalid_run_identity")
        if self.contract.source_type != "contract" or not self.contract.text.strip():
            raise ValueError("invalid_contract")
        for doc in (self.contract, *(m.document for m in self.materials if m.document)):
            if doc.version != doc.raw_sha256:
                raise ValueError("transaction_version_mismatch")
        if (self.knowledge.mode != "policies15" or self.knowledge_id != self.knowledge.snapshot_id
                or self.policy_scope != SCOPE or self.checklist != tasks(self.knowledge)):
            raise ValueError("snapshot_or_checklist_mismatch")
        ids = [self.contract.document_id] + [m.document.document_id for m in self.materials if m.document]
        if len(set(ids)) != len(ids) or set(ids) & set(SCOPE):
            raise ValueError("source_identity_collision")
        if len({m.material_id for m in self.materials}) != len(self.materials):
            raise ValueError("duplicate_material")
        return self

    @property
    def input_sha256(self):
        return digest(self.model_dump_json().encode())


def read_text_file(path):
    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("only_md_txt")
    with path.open("rb") as stream:
        raw = stream.read(64001)
    if len(raw) > 64000 or b"\x00" in raw:
        raise ValueError("unsupported_or_oversized_text")
    raw.decode("utf-8")
    return raw


def prepare(contract_path, knowledge, *, run_id, code_revision, material_specs=()):
    materials = []
    for i, spec in enumerate(material_specs):
        if set(spec) - {"material_id", "status", "statement", "path"}:
            raise ValueError("unapproved_material_fields")
        path = spec.get("path")
        raw = read_text_file(Path(path)) if path else None
        doc = source(raw, f"material-{i+1:04}", digest(raw), "material") if raw is not None else None
        materials.append(Material(**{k: v for k, v in spec.items() if k != "path"}, document=doc))
    raw = read_text_file(contract_path)
    return Run(run_id=run_id, code_revision=code_revision,
               contract=source(raw, "contract-0001", digest(raw), "contract"), materials=tuple(materials),
               knowledge=knowledge, knowledge_id=knowledge.snapshot_id, checklist=tasks(knowledge))


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def save_run(run, root):
    folder = root / run.run_id
    folder.mkdir(parents=True, exist_ok=False)
    write_new(folder / "input.json", {"input_sha256": run.input_sha256, "payload": run.model_dump(mode="json")})
    return folder


def load_run(path):
    saved = json.loads(path.read_bytes())
    if saved["payload"].get("protocol") == "pilot-presign-input-v1":
        from .presign import PresignRun
        run = PresignRun.model_validate(saved["payload"])
    else:
        run = Run.model_validate(saved["payload"])
    if saved["input_sha256"] != run.input_sha256:
        raise ValueError("run_digest_mismatch")
    return run
