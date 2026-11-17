"""解析目录 -> 构建新快照 -> 校验索引 -> CAS 发布；Milvus 是可重建投影。"""

from uuid import uuid4

from production_rag.adapters.sqlite import Snapshot
from production_rag.domain.errors import NoKnowledgeBase
from production_rag.ingestion.chunker import split_document
from production_rag.ingestion.parser import parse_document


class IngestService:
    def __init__(self, settings, store, embedder, index):
        self.settings, self.store = settings, store
        self.embedder, self.index = embedder, index

    def sync(self, job_context=None) -> dict:
        base = self.store.active_id()
        previous = self.store.snapshot(base) if base else None
        by_source = {d.source: d for d in previous.documents} if previous else {}
        root = self.settings.document_root.resolve(strict=True)
        documents = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
                document = parse_document(path, root, version=1)
                old = by_source.get(document.source)
                if old:
                    document = document.model_copy(
                        update={"version": old.version + (old.text != document.text)}
                    )
                documents.append(document)
        if not documents:
            raise ValueError("目录中没有 Markdown/TXT；清空知识库请逐项删除")
        return self._build(documents, base, job_context)

    def delete(self, document_id: str, job_context=None) -> dict:
        base = self.store.active_id()
        if not base:
            raise NoKnowledgeBase("尚未导入知识库")
        previous = self.store.snapshot(base)
        remaining = [d for d in previous.documents if d.document_id != document_id]
        if len(remaining) == len(previous.documents):
            raise ValueError("document_id 不存在")
        return self._build(remaining, base, job_context)

    def _build(self, documents, base, job_context=None):
        settings = self.settings
        chunks = [
            c
            for d in documents
            for c in split_document(d, settings.chunk_size, settings.chunk_overlap)
        ]
        if len(chunks) > 20000:
            raise ValueError("当前单机实现限制为 20000 个片段；大语料需要流式索引")
        config = {
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
            "embedding_digest": self.embedder.digest(),
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
            "vector_backend": settings.vector_backend,
        }
        if base:
            old = self.store.snapshot(base)
            previous_config = {k: v for k, v in old.config.items() if k != "collection"}
            if config == previous_config and {
                d.document_id: d.model_dump() for d in old.documents
            } == {d.document_id: d.model_dump() for d in documents}:
                return {
                    "snapshot_id": base,
                    "documents": len(documents),
                    "chunks": len(chunks),
                    "changed": False,
                }
        snapshot_id = uuid4().hex
        config["collection"] = f"{settings.collection_prefix}_{snapshot_id}"
        reusable = {}
        if base:
            old = self.store.snapshot(base)
            if all(
                old.config.get(k) == config[k]
                for k in ("embedding_model", "embedding_dim", "embedding_digest")
            ):
                reusable = {c.text: v for c, v in zip(old.chunks, old.vectors, strict=True)}
        missing = list(dict.fromkeys(c.text for c in chunks if c.text not in reusable))
        vectors = self.embedder.embed(missing)
        reusable.update(zip(missing, vectors, strict=True))
        snapshot = Snapshot(
            snapshot_id, config, documents, chunks, [reusable[c.text] for c in chunks]
        )
        self.store.stage(snapshot)
        try:
            self.index.build(snapshot)
            self.store.publish(snapshot_id, expected_active=base, job_context=job_context)
        except Exception as exc:
            self.store.fail_snapshot(snapshot_id, str(exc))
            raise
        return {
            "snapshot_id": snapshot_id,
            "documents": len(documents),
            "chunks": len(chunks),
            "changed": True,
        }
