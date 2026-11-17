import json


def select_context(hits, max_chars: int):
    """优先完整保留片段，不将被截断的片段标成已提供给模型的证据。"""
    selected, size = [], 0
    for hit in hits:
        record = {"chunk_id": hit.chunk.chunk_id, "source": hit.source, "text": hit.chunk.text}
        cost = len(json.dumps(record, ensure_ascii=False))
        if size + cost <= max_chars:
            selected.append(hit)
            size += cost
    return selected


def evidence_json(hits):
    return json.dumps(
        [{"chunk_id": h.chunk.chunk_id, "source": h.source, "text": h.chunk.text} for h in hits],
        ensure_ascii=False,
    )
