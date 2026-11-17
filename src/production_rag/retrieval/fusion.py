def reciprocal_rank_fusion(rankings: list[list[tuple[str, float]]], k: int = 60):
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for rank, (chunk_id, _) in enumerate(ranking, 1):
            if chunk_id not in seen:
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (k + rank)
                seen.add(chunk_id)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
