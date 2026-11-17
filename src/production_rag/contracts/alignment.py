"""Explainable proposals only; original text is never normalized in storage."""

import re
from difflib import SequenceMatcher
from typing import Literal

from pydantic import Field

from production_rag.contracts.models import ContractModel


class Pair(ContractModel):
    standard_version: str
    supplier_version: str


class Decision(ContractModel):
    revision: int = Field(ge=0)
    action: Literal["confirm", "correct", "reject", "pending"]
    standard_anchors: list[str] = Field(min_length=1, max_length=40)
    supplier_anchors: list[str] = Field(default_factory=list, max_length=40)
    note: str = Field(default="", max_length=1000)


def units(record):
    return {b.anchor: b for b in record.result.blocks if b.text.strip() or b.kind == "table"}


def text(block):
    return block.text if block.kind != "table" else "\n".join(c.text for r in block.rows for c in r)


def normalized(value):
    return re.sub(r"\s+", "", value).casefold()[:1000]


def number(value):
    match = re.match(r"\s*(第[^\s章节条]{1,12}[章节条]|[一二三四五六七八九十\d]+[、.．])", value)
    return match[1] if match else None


def simple_table(block):
    return (
        bool(block.rows)
        and all(c.colspan == 1 and not c.vertical_merge for r in block.rows for c in r)
        and len({len(r) for r in block.rows}) == 1
    )


def propose(standard, supplier):
    left, right = list(units(standard).values()), list(units(supplier).values())
    if max(len(left), len(right)) > 400:
        raise ValueError("对齐试验限每侧 400 个非空结构块")
    output = []
    for i, a in enumerate(left):
        candidates = []
        for j, b in enumerate(right):
            if (a.kind == "table") != (b.kind == "table"):
                continue
            if a.kind == "table":
                # No cell correspondence inferred from ordinal positions or merged grids.
                if not simple_table(a) or not simple_table(b):
                    continue
                ah, bh = [c.text for c in a.rows[0]], [c.text for c in b.rows[0]]
                if ah != bh or len(set(ah)) != len(ah) or not all(ah):
                    continue
            similarity = SequenceMatcher(None, normalized(text(a)), normalized(text(b))).ratio()
            if similarity < 0.45:
                continue
            reasons = {
                "text_similarity": round(similarity, 4),
                "same_number": bool(number(a.text) and number(a.text) == number(b.text)),
                "heading": a.kind == b.kind == "heading",
                "relative_position": round(
                    1 - abs(i / max(1, len(left)) - j / max(1, len(right))), 4
                ),
            }
            neighbor = (
                i > 0 and j > 0 and normalized(text(left[i - 1])) == normalized(text(right[j - 1]))
            )
            reasons["previous_block_equal"] = neighbor
            score = (
                0.8 * similarity
                + 0.08 * reasons["same_number"]
                + 0.04 * reasons["heading"]
                + 0.04 * reasons["relative_position"]
                + 0.04 * neighbor
            )
            candidates.append(
                {
                    "supplier_anchor": b.anchor,
                    "score": round(score, 4),
                    "method": "text-number-heading-neighbor-position-v1",
                    "signals": reasons,
                }
            )
        candidates.sort(key=lambda c: (-c["score"], c["supplier_anchor"]))
        output.append(
            {
                "standard_anchor": a.anchor,
                "candidates": candidates[:3],
                "notice": "仅为候选，重复文字、移动和分拆须人工核对；无候选不表示缺失。"
                if a.kind != "table"
                else "仅建议整表关联；合并、表头或列关系不明时不自动提议，不建立单元格对应。",
            }
        )
    return output
