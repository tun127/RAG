def retrieval_metrics(hits, expected_evidence):
    """以原文证据子串标注，避免分块后 chunk_id 变化使标注失效。

    recall: 预期证据事实覆盖率；MRR: 第一个命中任一证据的片段名次。
    """
    if not expected_evidence:
        return {"hit": None, "evidence_recall": None, "mrr": None}
    covered = {
        i
        for hit in hits
        for i, evidence in enumerate(expected_evidence)
        if evidence in hit.chunk.text
    }
    first = next(
        (
            rank
            for rank, h in enumerate(hits, 1)
            if any(e in h.chunk.text for e in expected_evidence)
        ),
        None,
    )
    return {
        "hit": float(bool(covered)),
        "evidence_recall": len(covered) / len(expected_evidence),
        "mrr": 1 / first if first else 0.0,
    }


def generation_metrics(answer, expected_facts, answerable):
    # 字面事实覆盖只是可重复代理指标，不能代替正确性/忠实度评审。
    return {
        "fact_string_coverage": (
            sum(f in answer.text for f in expected_facts) / len(expected_facts)
            if expected_facts
            else None
        ),
        "abstention_correct": float((answer.status == "answered") == answerable),
    }
