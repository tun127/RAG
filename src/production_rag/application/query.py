from collections import OrderedDict
from threading import BoundedSemaphore, Lock
from uuid import uuid4

from production_rag.agent.budgets import RunBudget
from production_rag.agent.loop import AgentLoop
from production_rag.agent.reflection import Reflector
from production_rag.agent.tools import ToolRegistry
from production_rag.domain.errors import BudgetExceeded, Busy, DependencyError
from production_rag.domain.models import QueryResult
from production_rag.generation.service import refusal
from production_rag.observability.tracing import Trace
from production_rag.retrieval.pipeline import Retriever


class QueryService:
    def __init__(self, settings, store, embedder, index, llm, generator, memory):
        self.settings, self.store = settings, store
        self.embedder, self.index, self.llm = embedder, index, llm
        self.generator, self.memory = generator, memory
        self.slots = BoundedSemaphore(settings.max_concurrent_queries)
        self.cache, self.cache_lock = OrderedDict(), Lock()

    def retriever(self, snapshot_id=None):
        snapshot_id = snapshot_id or self.store.active_id()
        with self.cache_lock:
            if snapshot_id not in self.cache:
                snapshot = self.store.snapshot(snapshot_id)
                self.cache[snapshot_id] = Retriever(
                    self.settings, snapshot, self.embedder, self.index
                )
                while len(self.cache) > 2:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(snapshot_id)
            return self.cache[snapshot_id]

    def ask(
        self,
        question: str,
        *,
        mode="agent",
        user_id="demo",
        session_id="default",
        retrieval="hybrid",
        reranking=False,
        remember_episode=False,
        snapshot_id=None,
    ):
        if not question.strip() or len(question) > 2000:
            raise ValueError("问题长度必须为 1 至 2000 字符")
        if mode not in {"baseline", "agent"} or retrieval not in {"vector", "keyword", "hybrid"}:
            raise ValueError("无效的问答/检索模式")
        if not self.slots.acquire(blocking=False):
            raise Busy("模型正在处理请求，请稍后重试")
        try:
            return self._ask(
                question,
                mode,
                user_id,
                session_id,
                retrieval,
                reranking,
                remember_episode,
                snapshot_id,
            )
        finally:
            self.slots.release()

    def _ask(
        self,
        question,
        mode,
        user_id,
        session_id,
        retrieval,
        reranking,
        remember_episode,
        snapshot_id,
    ):
        trace, request_id = Trace(), uuid4().hex
        budget = RunBudget(self.settings.request_timeout, self.settings.max_output_tokens)
        retriever = self.retriever(snapshot_id)
        memory = self.memory.context(user_id, session_id, question)
        trace.add(
            "started",
            mode=mode,
            snapshot_id=retriever.snapshot.snapshot_id,
            memory_count=len(memory["preferences_and_notes"]),
            retrieval=retrieval,
            reranking=reranking,
            model=self.llm.model,
        )
        hits, stop_reason = [], "baseline"
        try:
            if self.embedder.digest() != retriever.snapshot.config["embedding_digest"]:
                raise DependencyError("embedding 模型摘要已变化，请重新导入知识库")
            if mode == "agent":
                loop = AgentLoop(
                    self.settings,
                    self.llm,
                    ToolRegistry(retriever),
                    Reflector(self.llm, self.settings.context_chars),
                )
                state = loop.collect(question, memory, budget, trace)
                hits, stop_reason = list(state.evidence.values()), state.stop_reason
            else:
                hits = retriever.search(question, budget, mode=retrieval, reranking=reranking)
                trace.add("retrieval", chunk_ids=[h.chunk.chunk_id for h in hits])
            answer, context = self.generator.answer(
                question, hits, memory, request_id, budget, trace
            )
        except (BudgetExceeded, DependencyError) as exc:
            stop_reason = str(exc) if isinstance(exc, BudgetExceeded) else "dependency_error"
            trace.add("run_error", error_type=type(exc).__name__, detail=str(exc))
            answer, context = (
                refusal(request_id, "本次运行受预算或依赖故障限制，请查看执行轨迹。"),
                [],
            )
        usage = {
            **budget.usage(),
            "agent_steps": sum(e.stage == "decision" for e in trace.events),
            "tool_calls": sum(
                e.stage in {"tool_result", "tool_error", "tool_duplicate"} for e in trace.events
            ),
        }
        trace.add("finished", status=answer.status, stop_reason=stop_reason, **usage)
        result = QueryResult(
            answer=answer,
            mode=mode,
            snapshot_id=retriever.snapshot.snapshot_id,
            model=self.llm.model,
            evidence=context,
            trace=trace.events,
            stop_reason=stop_reason,
            usage=usage,
            latency_ms=trace.elapsed_ms,
        )
        self.store.save_trace(user_id, result)
        self.memory.record(user_id, session_id, question, answer, remember_episode)
        return result
