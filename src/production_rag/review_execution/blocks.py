"""Lossless, run-scoped blocks over the already fixed LF text, never raw bytes."""
from typing import Literal

from production_rag.review_inputs.models import Frozen, TextInput, digest

BLOCK_ALGORITHM = "paragraph-char1200-v1"
MAX_BLOCK_CHARS = 1200


class TextBlock(Frozen):
    block_id: str
    start: int
    end: int
    text: str


class DocumentBlocks(Frozen):
    algorithm: Literal["paragraph-char1200-v1"] = BLOCK_ALGORITHM
    run_id: str
    document_id: str
    role: Literal["standard", "supplier", "material"]
    text_sha256: str
    blocks: tuple[TextBlock, ...]


def document_blocks(document: TextInput, run_id: str) -> DocumentBlocks:
    document = TextInput.model_validate_json(document.model_dump_json())
    text = document.text
    blocks = []
    start = 0
    while start < len(text):
        separator = text.find("\n\n", start)
        paragraph_end = len(text) if separator < 0 else separator + 2
        while start < paragraph_end:
            end = min(start + MAX_BLOCK_CHARS, paragraph_end)
            identity = "\x00".join((BLOCK_ALGORITHM, run_id, document.document_id,
                                   document.role, document.text_sha256, str(start), str(end)))
            blocks.append(TextBlock(
                block_id=f"b{len(blocks) + 1:04}-{digest(identity.encode())[:16]}",
                start=start, end=end, text=text[start:end],
            ))
            start = end
    return DocumentBlocks(run_id=run_id, document_id=document.document_id,
                          role=document.role, text_sha256=document.text_sha256, blocks=tuple(blocks))


def build_maps(run):
    return tuple(document_blocks(d, run.run_id) for d in run.documents)


def verify_maps(run, maps):
    """Reject changed roles, texts, ranges, algorithm, run, order or document IDs."""
    expected = build_maps(run)
    if tuple(maps) != expected:
        raise ValueError("block_map_mismatch")
    return expected


def model_documents(maps):
    # Offsets stay in the persisted source map; the model only selects blocks.
    return [{"document_id": m.document_id, "role": m.role, "text_sha256": m.text_sha256,
             "blocks": [{"block_id": b.block_id, "text": b.text} for b in m.blocks]}
            for m in maps]
