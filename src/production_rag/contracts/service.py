"""Immutable, task-isolated local originals and versioned parse results."""

import io
import os
import re
import subprocess
import sys
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from defusedxml import ElementTree as ET

from production_rag.contracts.models import (
    DocumentVersion,
    ParseIssue,
    ParseLimits,
    ParseResult,
    version_id,
)


class ContractBusy(Exception):
    pass


class ContractService:
    def __init__(self, root: Path, knowledge_root: Path, limits: ParseLimits | None = None):
        self.root = root.resolve()
        knowledge_root = knowledge_root.resolve()
        if self.root.is_relative_to(knowledge_root) or knowledge_root.is_relative_to(self.root):
            raise ValueError("合同任务目录必须与共享知识库目录分离")
        self.limits = limits or ParseLimits()
        self._gate = threading.BoundedSemaphore(1)

    def _path(self, task: str, version: str | None = None) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task):
            raise FileNotFoundError("任务不存在")
        if version is not None and not re.fullmatch(r"[0-9a-f]{64}", version):
            raise FileNotFoundError("版本不存在")
        path = self.root / task
        if version:
            path = path / version
        if not path.resolve().is_relative_to(self.root):
            raise FileNotFoundError("任务路径无效")
        return path

    @staticmethod
    def _write(path: Path, record: DocumentVersion):
        temporary = path.with_name("record-" + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(record.model_dump_json(indent=2).encode("utf-8"))
        os.replace(temporary, path)

    def upload(self, content: bytes, filename: str, task: str | None = None) -> DocumentVersion:
        if len(content) > self.limits.max_bytes:
            raise ValueError("文件超过 512 KiB 试验限制")
        if not self._gate.acquire(blocking=False):
            raise ContractBusy("已有文档正在解析，请稍后重试")
        try:
            if task is None:
                task = uuid.uuid4().hex
                self._path(task).mkdir(parents=True, exist_ok=False)
            elif not self._path(task).is_dir():
                raise FileNotFoundError("任务不存在")
            version = version_id(content, self.limits)
            folder = self._path(task, version)
            if folder.exists():
                return self.get(task, version)
            folder.mkdir(exist_ok=False)
            # Neither filename nor client-controlled path is ever used for filesystem storage.
            name = re.sub(r"[\x00-\x1f\x7f]", "", filename.replace("\\", "/").rsplit("/", 1)[-1])[
                :128
            ]
            source = folder / "source.bin"
            source.write_bytes(content)
            record = DocumentVersion(
                task_id=task,
                version_id=version,
                content_sha256=sha256(content).hexdigest(),
                limits=self.limits,
                original_name=name or "未命名文件",
                byte_size=len(content),
                created_at=datetime.now(UTC).isoformat(),
                result=ParseResult(
                    status="failed",
                    issues=[
                        ParseIssue(
                            code="interrupted",
                            message="解析尚未完成或进程中断；请新建任务重试，未接受为完整合同",
                        )
                    ],
                ),
            )
            self._write(folder / "record.json", record)
            output = folder / "worker.json"
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "production_rag.contracts.worker",
                        str(source),
                        str(output),
                        version,
                        self.limits.model_dump_json(),
                    ],
                    timeout=self.limits.timeout_seconds,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if output.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("parser output limit")
                result = ParseResult.model_validate_json(output.read_bytes())
            except subprocess.TimeoutExpired:
                result = ParseResult(
                    status="failed",
                    issues=[
                        ParseIssue(
                            code="timeout", message="解析超过 20 秒试验时限；未接受为完整合同"
                        )
                    ],
                )
            except Exception:
                result = ParseResult(
                    status="failed",
                    issues=[
                        ParseIssue(code="worker_failure", message="解析进程失败；未接受为完整合同")
                    ],
                )
            finally:
                output.unlink(missing_ok=True)
            record.result = result
            self._write(folder / "record.json", record)
            return record
        finally:
            self._gate.release()

    def get(self, task: str, version: str) -> DocumentVersion:
        path = self._path(task, version)
        record = DocumentVersion.model_validate_json((path / "record.json").read_bytes())
        if record.task_id != task or record.version_id != version:
            raise ValueError("文档版本记录不匹配")
        return record

    def versions(self, task: str) -> list[DocumentVersion]:
        path = self._path(task)
        if not path.is_dir():
            raise FileNotFoundError("任务不存在")
        return sorted(
            [
                self.get(task, p.name)
                for p in path.iterdir()
                if re.fullmatch(r"[0-9a-f]{64}", p.name)
            ],
            key=lambda r: r.created_at,
        )

    def original(self, record: DocumentVersion) -> bytes:
        data = (self._path(record.task_id, record.version_id) / "source.bin").read_bytes()
        if sha256(data).hexdigest() != record.content_sha256:
            raise ValueError("原件校验失败，停止定位和下载")
        return data

    def locate(self, task: str, version: str, anchor: str) -> dict:
        record = self.get(task, version)
        # Validate stored original before presenting an anchor as belonging to it.
        original = self.original(record)
        for block in record.result.blocks:
            items = [block] + [cell for row in block.rows for cell in row]
            for item in items:
                if item.anchor != anchor:
                    continue
                text = item.text
                extra = {}
                if item.locator.page is not None:
                    page = next(p for p in record.result.pages if p.number == item.locator.page)
                    if page.text[item.locator.start : item.locator.end] != text:
                        raise ValueError("PDF 文字定位校验失败")
                    extra["page_text"] = page.text
                else:
                    from production_rag.contracts.parser import W, _text

                    with zipfile.ZipFile(io.BytesIO(original)) as archive:
                        body = ET.fromstring(archive.read("word/document.xml")).find(W + "body")
                    node = list(body)[item.locator.body_index]
                    if item.locator.row is not None:
                        node = node.findall(W + "tr")[item.locator.row].findall(W + "tc")[
                            item.locator.cell
                        ]
                        source_text = "\n".join(_text(p) for p in node.findall(W + "p"))
                    elif block.kind == "table":
                        source_text = "\n".join(_text(p) for p in node.iter(W + "p"))
                    else:
                        source_text = _text(node)
                    if block.kind != "table" or item.locator.row is not None:
                        if source_text != text:
                            raise ValueError("DOCX 原文定位校验失败")
                    extra.update(
                        original_xml=ET.tostring(node, encoding="unicode"),
                        original_text=source_text,
                    )
                return {
                    "task_id": task,
                    "version_id": version,
                    "content_sha256": record.content_sha256,
                    "anchor": anchor,
                    "locator": item.locator.model_dump(),
                    "text": text,
                    **extra,
                }
        raise FileNotFoundError("当前版本中没有此定位锚点")
