"""组合根：仅这里选择具体模型、数据库实现并组装依赖。导入模块不连接服务。"""

from dataclasses import dataclass

from production_rag.adapters.local_vector import LocalVectorIndex
from production_rag.adapters.ollama_chat import OllamaChat
from production_rag.adapters.ollama_embed import OllamaEmbedder
from production_rag.adapters.sqlite import SqliteStore
from production_rag.application.ingest import IngestService
from production_rag.application.query import QueryService
from production_rag.config import Settings
from production_rag.domain.ports import Embedder, LanguageModel, VectorIndex
from production_rag.generation.service import Generator
from production_rag.memory.service import MemoryService


@dataclass
class Services:
    settings: Settings
    store: SqliteStore
    ingest: IngestService
    query: QueryService
    memory: MemoryService


def build_services(
    settings: Settings | None = None,
    *,
    embedder: Embedder | None = None,
    llm: LanguageModel | None = None,
    index: VectorIndex | None = None,
) -> Services:
    settings = settings or Settings()
    store = SqliteStore(settings.data_dir / "rag.sqlite3")
    embedder = embedder or OllamaEmbedder(settings)
    if settings.redis_url:
        from production_rag.adapters.redis import CachedEmbedder

        embedder = CachedEmbedder(embedder, settings.redis_url, settings.embedding_dim)
    llm = llm or OllamaChat(settings)
    if index is None:
        if settings.vector_backend == "milvus":
            from production_rag.adapters.milvus import MilvusIndex

            index = MilvusIndex(settings)
        else:
            index = LocalVectorIndex()
    memory = MemoryService(store)
    generator = Generator(llm, settings.context_chars)
    return Services(
        settings,
        store,
        IngestService(settings, store, embedder, index),
        QueryService(settings, store, embedder, index, llm, generator, memory),
        memory,
    )
