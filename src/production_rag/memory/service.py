import time
from typing import Literal
from uuid import uuid4

from production_rag.domain.models import MemoryItem
from production_rag.memory.semantic import relevant_memories


class MemoryService:
    def __init__(self, store):
        self.store = store

    def put(
        self,
        user_id: str,
        kind: Literal["preference", "fact", "episode"],
        key: str,
        value: str,
        source: str = "user_explicit",
        ttl_seconds: int | None = None,
    ):
        if ttl_seconds is not None and not 1 <= ttl_seconds <= 365 * 86400:
            raise ValueError("ttl_seconds 必须在 1 秒至 365 天之间")
        item = MemoryItem(
            memory_id=uuid4().hex,
            user_id=user_id,
            kind=kind,
            key=key,
            value=value,
            source=source,
            updated_at=time.time(),
            expires_at=time.time() + ttl_seconds if ttl_seconds else None,
        )
        self.store.put_memory(item)
        return item

    def context(self, user_id, session_id, question):
        selected = relevant_memories(question, self.store.memories(user_id))
        # 历史不是知识证据；保留提问用于指代消解，不把旧回答当作真实事实。
        return {
            "preferences_and_notes": [
                {"kind": m.kind, "key": m.key, "value": m.value[:400]} for m in selected
            ],
            "recent_turns": [
                {"question": t["question"][:300], "answer": t["answer"][:400]}
                for t in self.store.recent_turns(user_id, session_id)
            ],
        }

    def record(self, user_id, session_id, question, answer, remember_episode=False):
        self.store.save_turn(user_id, session_id, question, answer.text)
        if remember_episode and answer.status == "answered":
            self.put(
                user_id,
                "episode",
                key=answer.request_id,
                value=(question + "\n" + answer.text)[:1000],
                source="opt_in_query:" + answer.request_id,
                ttl_seconds=30 * 86400,
            )
