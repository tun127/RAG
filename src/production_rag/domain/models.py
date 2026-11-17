"""模块之间传递的数据契约；模型输出和外部请求在边界验证。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RagModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Document(RagModel):
    document_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    version: int = Field(ge=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Chunk(RagModel):
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version: int = Field(ge=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=1)
    text: str = Field(min_length=1)


class SearchHit(RagModel):
    chunk: Chunk
    score: float = Field(allow_inf_nan=False)
    retriever: str = Field(min_length=1)
    source: str = ""


class Answer(RagModel):
    request_id: str = Field(min_length=1)
    status: Literal["answered", "insufficient_evidence"]
    text: str = Field(min_length=1)
    citation_chunk_ids: list[str] = Field(default_factory=list)


class TraceEvent(RagModel):
    sequence: int
    stage: str
    elapsed_ms: float
    data: dict = Field(default_factory=dict)


class QueryResult(RagModel):
    answer: Answer
    mode: Literal["baseline", "agent"]
    snapshot_id: str
    model: str
    evidence: list[SearchHit] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    stop_reason: str
    usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: float


class AnswerDraft(RagModel):
    """模型输出契约；最终 request_id 由程序生成。"""

    status: Literal["answered", "insufficient_evidence"]
    text: str = Field(min_length=1, max_length=12000)
    citation_chunk_ids: list[str] = Field(default_factory=list, max_length=20)


class MemoryItem(RagModel):
    memory_id: str
    user_id: str
    kind: Literal["preference", "fact", "episode"]
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=200)
    expires_at: float | None = None
    updated_at: float


def validate_chunk(chunk: Chunk, document: Document) -> None:
    if chunk.document_id != document.document_id:
        raise ValueError("片段所属文档不匹配")

    if chunk.document_version != document.version:
        raise ValueError("片段与原文的版本不匹配")

    if not 0 <= chunk.start_char < chunk.end_char <= len(document.text):
        raise ValueError("片段位置超出原文范围或起止顺序错误")

    expected_text = document.text[chunk.start_char : chunk.end_char]

    if chunk.text != expected_text:
        raise ValueError("片段正文与原文指定位置的内容不一致")


def validate_citations(answer: Answer, context_hits: list[SearchHit]) -> None:
    if answer.status == "answered" and not answer.citation_chunk_ids:
        raise ValueError("基于知识库作答时必须提供引用")

    available_ids = {hit.chunk.chunk_id for hit in context_hits}

    unknown_ids = set(answer.citation_chunk_ids) - available_ids

    if unknown_ids:
        raise ValueError(f"引用了未提供的片段：{sorted(unknown_ids)}")
