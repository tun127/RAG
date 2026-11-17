"""真正需要替换的两个边界：模型、向量索引。测试不需要启动外部服务。"""

from typing import Protocol


class LanguageModel(Protocol):
    model: str

    def chat(self, messages: list[dict], budget, *, tools=None, schema=None) -> dict: ...


class Embedder(Protocol):
    def embed(self, texts: list[str], timeout: float = 120) -> list[list[float]]: ...

    def digest(self) -> str: ...


class VectorIndex(Protocol):
    def build(self, snapshot) -> None: ...

    def search(
        self, snapshot, vector: list[float], limit: int, timeout: float = 30
    ) -> list[tuple[str, float]]: ...
