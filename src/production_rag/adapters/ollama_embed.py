"""Ollama embedding 边界：批处理、维度、数值检查只在这里执行。"""

from math import isfinite

from ollama import Client

from production_rag.domain.errors import DependencyError


class OllamaEmbedder:
    def __init__(self, settings):
        self.settings = settings

    def digest(self) -> str:
        try:
            client = Client(host=self.settings.ollama_url, timeout=10)
            for model in client.list().models:
                if model.model == self.settings.embedding_model:
                    return model.digest
            raise DependencyError("找不到配置的 embedding 模型，请检查 ollama list")
        except DependencyError:
            raise
        except Exception as exc:
            raise DependencyError("读取 Ollama 模型信息失败") from exc
        finally:
            if "client" in locals():
                client.close()

    def embed(self, texts: list[str], timeout: float = 120) -> list[list[float]]:
        if not texts:
            return []
        vectors = []
        client = Client(host=self.settings.ollama_url, timeout=timeout)
        try:
            for start in range(0, len(texts), self.settings.embedding_batch_size):
                batch = texts[start : start + self.settings.embedding_batch_size]
                rows = client.embed(
                    model=self.settings.embedding_model, input=batch, truncate=False
                ).embeddings
                if len(rows) != len(batch) or any(
                    len(v) != self.settings.embedding_dim or not all(isfinite(x) for x in v)
                    for v in rows
                ):
                    raise DependencyError("embedding 数量、维度或数值不符合契约")
                vectors.extend(rows)
        except DependencyError:
            raise
        except Exception as exc:
            raise DependencyError("Ollama 向量化失败；检查模型、服务和上下文长度") from exc
        finally:
            client.close()
        return vectors
