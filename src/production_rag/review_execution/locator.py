"""Exact quotation resolution confined to the declared current document/block."""
from production_rag.review_execution.blocks import build_maps, verify_maps

LOCATOR_METHOD = "unique_exact_in_declared_block-v1"


class LocationError(ValueError):
    pass


def locate(run, declaration, maps=None):
    maps = build_maps(run) if maps is None else maps
    try:
        verify_maps(run, maps)
    except ValueError:
        raise LocationError("block_map_mismatch") from None
    document = next((d for d in run.documents if d.document_id == declaration.document_id), None)
    if document is None:
        raise LocationError("citation_unknown_document")
    mapped = next(m for m in maps if m.document_id == document.document_id)
    block = next((b for b in mapped.blocks if b.block_id == declaration.block_id), None)
    if block is None:
        raise LocationError("citation_unknown_block")
    quote = declaration.quote
    if not quote or not quote.strip():
        raise LocationError("citation_empty_quote")
    offset = block.text.find(quote)
    if offset < 0:
        raise LocationError("citation_not_found")
    # Include overlapping occurrences (e.g. 'aa' in 'aaa'). Never pick the first
    # of multiple matches or search a neighbouring block to rescue the answer.
    if block.text.find(quote, offset + 1) >= 0:
        raise LocationError("citation_ambiguous")
    source = document.cite(block.start + offset, block.start + offset + len(quote))
    if source["quote"] != quote:
        raise LocationError("citation_source_mismatch")
    return {
        **source, "block_id": block.block_id, "block_start": block.start, "block_end": block.end,
        "block_algorithm": mapped.algorithm, "locator_method": LOCATOR_METHOD,
        "model_evidence": declaration.model_dump(),
    }
