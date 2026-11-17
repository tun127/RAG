import json

from pydantic import ValidationError

from production_rag.domain.models import Answer, AnswerDraft, validate_citations
from production_rag.generation.context import evidence_json, select_context
from production_rag.generation.prompts import ANSWER_SYSTEM


def refusal(request_id: str, detail="当前证据不足以支持可靠回答。") -> Answer:
    return Answer(
        request_id=request_id, status="insufficient_evidence", text=detail, citation_chunk_ids=[]
    )


class Generator:
    def __init__(self, llm, context_chars: int):
        self.llm, self.context_chars = llm, context_chars

    def answer(self, question, hits, memory, request_id, budget, trace):
        context = select_context(hits, self.context_chars)
        trace.add("context", chunk_ids=[h.chunk.chunk_id for h in context])
        if not context:
            return refusal(request_id), []
        messages = [
            {"role": "system", "content": ANSWER_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "memory": memory,
                        "evidence": json.loads(evidence_json(context)),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.llm.chat(messages, budget, schema=AnswerDraft.model_json_schema())
        try:
            draft = AnswerDraft.model_validate_json(response.get("content", ""))
            answer = Answer(request_id=request_id, **draft.model_dump())
            if answer.status == "insufficient_evidence":
                answer.citation_chunk_ids = []
            validate_citations(answer, context)
        except (ValidationError, ValueError) as exc:
            trace.add("generation_rejected", error=type(exc).__name__)
            return refusal(request_id, "模型输出格式或引用未通过校验，本次拒绝作答。"), context
        trace.add("generation", status=answer.status, citations=answer.citation_chunk_ids)
        return answer, context
