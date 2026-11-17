"""教学入口 1：一次运行的状态；与跨会话长期记忆分开。"""

from dataclasses import dataclass, field

from production_rag.domain.models import SearchHit


@dataclass
class AgentState:
    question: str
    messages: list[dict]
    evidence: dict[str, SearchHit] = field(default_factory=dict)
    seen_calls: set[str] = field(default_factory=set)
    steps: int = 0
    tool_calls: int = 0
    stop_reason: str = "max_steps"

    def observe(self, hits):
        for hit in hits:
            self.evidence[hit.chunk.chunk_id] = hit
