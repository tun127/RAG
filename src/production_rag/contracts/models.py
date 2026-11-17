from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PARSER_VERSION = "contract-structure-1.1.1"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParseLimits(ContractModel):
    # Conservative pilot limits, not the PRD's unverified 20 MB / 80 pages.
    max_bytes: int = 512 * 1024
    max_expanded_bytes: int = 5 * 1024 * 1024
    max_zip_entries: int = 256
    max_pages: int = 2
    max_text_chars: int = 12000
    timeout_seconds: int = 20


class Locator(ContractModel):
    part: str
    body_index: int | None = None
    paragraph: int | None = None
    table: int | None = None
    row: int | None = None
    cell: int | None = None
    grid_column: int | None = None
    page: int | None = None
    start: int | None = None
    end: int | None = None
    # PDF offsets refer to stored extracted page text, never to visual coordinates.


class ParseIssue(ContractModel):
    code: str
    message: str
    location: str = ""


class TableCell(ContractModel):
    anchor: str
    locator: Locator
    text: str
    colspan: int = 1
    vertical_merge: str | None = None


class StructureBlock(ContractModel):
    anchor: str
    kind: Literal["heading", "paragraph", "list", "table"]
    locator: Locator
    text: str = ""
    level: int | None = None
    numbering: str | None = None
    rows: list[list[TableCell]] = Field(default_factory=list)


class ParsedPage(ContractModel):
    number: int
    text: str


class ParseResult(ContractModel):
    status: Literal["parsed", "partial", "failed", "rejected"]
    format: Literal["docx", "pdf", "unknown"] = "unknown"
    blocks: list[StructureBlock] = Field(default_factory=list)
    pages: list[ParsedPage] = Field(default_factory=list)
    issues: list[ParseIssue] = Field(default_factory=list)
    coverage_scope: str = ""
    coverage_complete: bool = False
    # Material completeness and clause judgability are separate; neither is a risk verdict.
    material_status: str = "未核实：仅接收一份主文档，未核实附件是否齐全"
    clause_assessment: str = "未实施条款判断"
    text_chars: int = 0


class DocumentVersion(ContractModel):
    task_id: str
    version_id: str
    content_sha256: str
    parser_version: str = PARSER_VERSION
    limits: ParseLimits
    original_name: str
    byte_size: int
    created_at: str
    result: ParseResult


def version_id(content: bytes, limits: ParseLimits) -> str:
    identity = PARSER_VERSION.encode() + limits.model_dump_json().encode() + content
    return sha256(identity).hexdigest()


def anchor_id(version: str, location: Locator) -> str:
    return "b-" + sha256((version + location.model_dump_json()).encode()).hexdigest()[:24]
