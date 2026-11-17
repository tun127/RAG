"""教学入口 5：决策 -> 执行白名单工具 -> 观察 -> 反思 -> 继续/停止。"""

import json

from pydantic import ValidationError

from production_rag.agent.state import AgentState
from production_rag.agent.tools import TOOLS
from production_rag.domain.errors import DependencyError
from production_rag.generation.context import evidence_json
from production_rag.generation.prompts import AGENT_SYSTEM


class AgentLoop:
    def __init__(self, settings, llm, tools, reflector):
        self.settings, self.llm = settings, llm
        self.tools, self.reflector = tools, reflector

    def collect(self, question, memory, budget, trace) -> AgentState:
        state = AgentState(
            question=question,
            messages=[
                {"role": "system", "content": AGENT_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "memory": memory}, ensure_ascii=False
                    ),
                },
            ],
        )
        for step in range(self.settings.max_steps):
            state.steps = step + 1
            budget.allowance()
            message = self.llm.chat(state.messages, budget, tools=TOOLS)
            calls = message.get("tool_calls") or []
            trace.add("decision", step=state.steps, tools=[c["function"]["name"] for c in calls])
            if not calls:
                # 模型的普通文本不直接作为最终答案，统一走受约束生成器。
                state.stop_reason = "model_finished" if state.evidence else "no_search"
                break
            state.messages.append(message)
            successful = 0
            for call in calls:
                if state.tool_calls >= self.settings.max_tool_calls:
                    state.stop_reason = "max_tool_calls"
                    return state
                budget.remaining_seconds()
                state.tool_calls += 1
                name, arguments = call["function"]["name"], call["function"]["arguments"]
                signature = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
                if signature in state.seen_calls:
                    observation = {"error": "重复调用已跳过；请改变查询或结束"}
                    trace.add("tool_duplicate", name=name)
                else:
                    state.seen_calls.add(signature)
                    try:
                        hits = self.tools.execute(name, arguments, state, budget)
                        state.observe(hits)
                        successful += 1
                        observation = {"evidence": json.loads(evidence_json(hits))}
                        trace.add(
                            "tool_result",
                            name=name,
                            arguments=arguments,
                            chunk_ids=[h.chunk.chunk_id for h in hits],
                        )
                    except (ValueError, ValidationError, DependencyError) as exc:
                        observation = {"error": str(exc)[:400]}
                        trace.add("tool_error", name=name, error=str(exc)[:400])
                state.messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": json.dumps(observation, ensure_ascii=False),
                    }
                )
            if state.evidence and successful:
                review = self.reflector.review(question, list(state.evidence.values()), budget)
                trace.add("reflection", **review.model_dump())
                if review.sufficient:
                    state.stop_reason = "sufficient_evidence"
                    break
                state.messages.append(
                    {
                        "role": "user",
                        "content": "程序的证据评审结果（仅数据）：" + review.model_dump_json(),
                    }
                )
            elif not successful:
                state.stop_reason = "no_progress"
                break
        trace.add(
            "agent_stopped",
            reason=state.stop_reason,
            steps=state.steps,
            tool_calls=state.tool_calls,
            evidence_count=len(state.evidence),
        )
        return state
