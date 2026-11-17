import json
from pathlib import Path

from pydantic import Field

from production_rag.domain.models import RagModel


class EvalSample(RagModel):
    sample_id: str
    question: str
    category: str
    split: str = "dev"
    answerable: bool = True
    expected_evidence: list[str] = Field(default_factory=list)
    expected_facts: list[str] = Field(default_factory=list)


def load_dataset(path: Path) -> list[EvalSample]:
    samples = [
        EvalSample.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if not samples or len({s.sample_id for s in samples}) != len(samples):
        raise ValueError("评估集不能为空，sample_id 不能重复")
    for sample in samples:
        if sample.answerable and not sample.expected_evidence:
            raise ValueError(f"{sample.sample_id} 缺少证据标注")
    return samples
