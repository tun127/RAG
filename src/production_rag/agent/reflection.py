"""教学入口 4：反思是对证据缺口的结构化判断，不是输出思维链。"""

import json

from pydantic import Field, ValidationError

from production_rag.domain.models import RagModel
from production_rag.generation.context import evidence_json, select_context
from production_rag.generation.prompts import REFLECT_SYSTEM


class EvidenceReview(RagModel):
    sufficient: bool
    missing: list[str] = Field(default_factory=list, max_length=10)
    next_query: str = Field(default="", max_length=500)


class Reflector:
    def __init__(self, llm, context_chars):
        self.llm, self.context_chars = llm, context_chars

    def review(self, question, hits, budget):
        context = select_context(hits, self.context_chars)
        response = self.llm.chat(
            [
                {"role": "system", "content": REFLECT_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "evidence": json.loads(evidence_json(context))},
                        ensure_ascii=False,
                    ),
                },
            ],
            budget,
            schema=EvidenceReview.model_json_schema(),
        )
        try:
            return EvidenceReview.model_validate_json(response.get("content", ""))
        except ValidationError:
            return EvidenceReview(
                sufficient=False, missing=["证据评审输出无效"], next_query=question[:500]
            )
