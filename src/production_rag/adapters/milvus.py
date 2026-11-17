"""每个快照一张专用集合；不修改学生此前的任何集合。"""

from pymilvus import DataType, MilvusClient

from production_rag.domain.errors import DependencyError


class MilvusIndex:
    def __init__(self, settings):
        self.settings = settings

    def _client(self, timeout=30):
        return MilvusClient(
            uri=self.settings.milvus_uri, token=self.settings.milvus_token, timeout=timeout
        )

    def build(self, snapshot):
        client = self._client()
        name = snapshot.config["collection"]
        try:
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.settings.embedding_dim)
            indexes = client.prepare_index_params()
            indexes.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
            client.create_collection(
                collection_name=name, schema=schema, index_params=indexes, timeout=60
            )
            records = [
                {"chunk_id": c.chunk_id, "vector": v}
                for c, v in zip(snapshot.chunks, snapshot.vectors, strict=True)
            ]
            for start in range(0, len(records), 128):
                batch = records[start : start + 128]
                result = client.upsert(collection_name=name, data=batch, timeout=60)
                if result["upsert_count"] != len(batch):
                    raise DependencyError("Milvus 写入数量不匹配，禁止发布快照")
            client.flush(collection_name=name, timeout=60)
            client.load_collection(collection_name=name, timeout=60)
            count = client.query(
                collection_name=name,
                filter="",
                output_fields=["count(*)"],
                consistency_level="Strong",
                timeout=30,
            )
            if count[0]["count(*)"] != len(records):
                raise DependencyError("Milvus 完整性检查失败，禁止发布快照")
        except DependencyError:
            raise
        except Exception as exc:
            raise DependencyError("Milvus 索引构建失败，旧知识库保持可用") from exc
        finally:
            client.close()

    def search(self, snapshot, vector, limit, timeout=30):
        from time import monotonic

        started = monotonic()
        client = self._client(timeout=timeout)
        try:
            result = client.search(
                collection_name=snapshot.config["collection"],
                data=[vector],
                limit=limit,
                search_params={"metric_type": "COSINE"},
                consistency_level="Strong",
                timeout=max(0.001, timeout - (monotonic() - started)),
            )
            return [(str(hit["id"]), float(hit["distance"])) for hit in result[0]]
        except Exception as exc:
            raise DependencyError("Milvus 检索失败") from exc
        finally:
            client.close()
