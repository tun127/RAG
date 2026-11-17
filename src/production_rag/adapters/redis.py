"""可选查询向量缓存：故障时退回真实 embedding。"""

import hashlib
import json
from math import isfinite


class CachedEmbedder:
    def __init__(self, inner, url: str, dimension: int):
        from redis import Redis

        self.inner, self.dimension = inner, dimension
        self.cache = Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)

    def digest(self):
        return self.inner.digest()

    def query(self, text: str, identity: str, timeout: float):
        key = "agentic:embedding:" + hashlib.sha256((identity + text).encode()).hexdigest()
        try:
            raw = self.cache.get(key)
            if raw:
                vector = json.loads(raw)
                if len(vector) == self.dimension and all(
                    type(x) in (int, float) and isfinite(x) for x in vector
                ):
                    return vector
        except Exception:
            pass
        vector = self.inner.embed([text], timeout=timeout)[0]
        try:
            self.cache.setex(key, 3600, json.dumps(vector))
        except Exception:
            pass
        return vector

    def embed(self, texts, timeout=120):
        return self.inner.embed(texts, timeout)
