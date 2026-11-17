from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryRequest(RequestModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["baseline", "agent"] = "agent"
    session_id: str = Field(default="default", min_length=1, max_length=100)
    retrieval: Literal["vector", "keyword", "hybrid"] = "hybrid"
    reranking: bool = False
    remember_episode: bool = False


class MemoryRequest(RequestModel):
    kind: Literal["preference", "fact", "episode"]
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    ttl_seconds: int | None = Field(default=None, ge=1, le=31536000)


class EvalRequest(RequestModel):
    modes: list[Literal["baseline", "agent"]] = Field(
        default=["baseline", "agent"], min_length=1, max_length=2
    )
    judge: bool = False
