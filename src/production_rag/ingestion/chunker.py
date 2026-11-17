import json
from hashlib import sha256

from production_rag.domain.models import Chunk, Document, validate_chunk


def split_document(
    document: Document,
    chunk_size: int = 800,
    overlap: int = 120,
) -> list[Chunk]:
    # 1. 检查配置，避免无效参数或循环无法前进。
    if type(chunk_size) is not int or type(overlap) is not int:
        raise ValueError("chunk_size 和 overlap 必须是整数")

    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size")

    if not document.text.strip():
        raise ValueError("文档正文不能只有空白字符")

    # 2. 保留原文，计算内容摘要。
    text = document.text
    document_hash = sha256(text.encode("utf-8")).hexdigest()

    chunks: list[Chunk] = []
    start = 0

    # 3. 按窗口切分。
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk_text = text[start:end]

        # 跳过纯空白窗口，但不修改实际保存的正文。
        if chunk_text.strip():
            identity = json.dumps(
                [
                    document.document_id,
                    document.version,
                    document_hash,
                    start,
                    end,
                ],
                ensure_ascii=False,
            )

            chunk_id = sha256(identity.encode("utf-8")).hexdigest()

            chunk = Chunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                document_version=document.version,
                start_char=start,
                end_char=end,
                text=chunk_text,
            )

            validate_chunk(chunk, document)
            chunks.append(chunk)

        # 已覆盖到原文末尾，直接结束。
        if end == len(text):
            break

        start = end - overlap

    return chunks
