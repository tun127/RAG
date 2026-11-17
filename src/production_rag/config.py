"""所有环境差异集中在这里；业务代码不读取环境变量。"""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data/runtime")
    document_root: Path = Path("data/knowledge")
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen2.5:latest"
    embedding_model: str = "bge-m3:latest"
    embedding_dim: int = Field(default=1024, gt=0)
    embedding_batch_size: int = Field(default=8, gt=0)
    model_context_tokens: int = Field(default=8192, ge=2048, le=32768)
    vector_backend: Literal["milvus", "sqlite"] = "milvus"
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: str = ""
    collection_prefix: str = Field(default="agentic_v1", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    chunk_size: int = Field(default=600, ge=50, le=8000)
    chunk_overlap: int = Field(default=100, ge=0)
    top_k: int = Field(default=6, ge=1, le=20)
    candidate_k: int = Field(default=20, ge=1, le=100)
    context_chars: int = Field(default=12000, ge=1000, le=32000)
    max_steps: int = Field(default=5, ge=1, le=20)
    max_tool_calls: int = Field(default=8, ge=1, le=30)
    max_output_tokens: int = Field(default=2400, ge=128, le=16000)
    request_timeout: float = Field(default=120, gt=0, le=600)
    max_concurrent_queries: int = Field(default=1, ge=1, le=16)
    api_token: str = ""
    redis_url: str = ""

    @model_validator(mode="after")
    def validate_chunk_window(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self
