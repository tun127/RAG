"""Bounded OOXML/PDF parsing. No text normalization or semantic contract judgement."""

import io
import re
import zipfile
from pathlib import PurePosixPath

from defusedxml import ElementTree as ET
from pypdf import PdfReader, apply_configuration

from production_rag.contracts.models import (
    Locator,
    ParsedPage,
    ParseIssue,
    ParseLimits,
    ParseResult,
    StructureBlock,
    TableCell,
    anchor_id,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
HEADING = re.compile(r"^(第[一二三四五六七八九十百\d]+[章节条]|[一二三四五六七八九十]+[、．])")


class ParseRejected(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


def _text(element) -> str:
    parts = []
    for node in element.iter():
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
        elif node.tag == W + "noBreakHyphen":
            parts.append("\u2011")
        elif node.tag == W + "softHyphen":
            parts.append("\u00ad")
    return "".join(parts)


def _issue(issues, code, message, location=""):
    item = ParseIssue(code=code, message=message, location=location)
    if item not in issues:
        issues.append(item)


def _docx(content: bytes, version: str, limits: ParseLimits) -> ParseResult:
    issues = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (
            len(infos) > limits.max_zip_entries
            or sum(i.file_size for i in infos) > limits.max_expanded_bytes
        ):
            raise ParseRejected("expanded_limit", "DOCX 展开大小或条目数超限")
        if len(set(names)) != len(names) or any(
            PurePosixPath(n).is_absolute() or ".." in PurePosixPath(n).parts or "\\" in n
            for n in names
        ):
            raise ParseRejected("unsafe_archive", "DOCX 包含重复或不安全的内部路径")
        if any(i.flag_bits & 1 for i in infos):
            raise ParseRejected("encrypted", "不支持加密 DOCX")
        if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
            raise ParseRejected("not_docx", "ZIP 内容不是受支持的 DOCX 文档")
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        main_type = next(
            (
                n.get("ContentType", "")
                for n in types.findall(CT + "Override")
                if n.get("PartName") == "/word/document.xml"
            ),
            "",
        )
        if (
            main_type
            != "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        ):
            raise ParseRejected("unsupported_office", "仅支持无宏的标准 DOCX")
        for name in names:
            if name.startswith("word/") and name.endswith(".xml"):
                root = ET.fromstring(archive.read(name))
                revision_tags = {
                    W + t
                    for t in ("ins", "del", "moveFrom", "moveTo", "cellIns", "cellDel", "cellMerge")
                }
                if any(
                    node.tag in revision_tags
                    or (node.tag.startswith(W) and node.tag.endswith("Change"))
                    for node in root.iter()
                ):
                    raise ParseRejected("revisions", "检测到修订记录；请先由人工确认并提供清洁副本")
        root = ET.fromstring(archive.read("word/document.xml"))
        body = root.find(W + "body")
        if body is None:
            raise ParseRejected("missing_body", "DOCX 缺少正文")
        unsupported = {
            "drawing": "图片或绘图未解析",
            "pict": "图片或文本框未解析",
            "txbxContent": "文本框阅读顺序未核实",
            "object": "嵌入对象未解析",
            "altChunk": "外部内容块未解析",
            "sdt": "内容控件结构未支持",
            "fldChar": "域的显示值可能过期，未重新计算",
            "fldSimple": "域未重新计算",
            "sym": "符号字体字符未解析",
            "vanish": "存在隐藏文字，未核实显示状态",
            "hMerge": "旧式横向合并标记未恢复，按物理单元格显示",
            "subDoc": "外部子文档未解析",
        }
        for tag, message in unsupported.items():
            if any(True for _ in root.iter(W + tag)):
                _issue(issues, "unsupported_" + tag, message)
        for tag in ("footnotes", "endnotes", "comments"):
            if f"word/{tag}.xml" in names:
                _issue(issues, "omitted_" + tag, "未解析脚注、尾注或批注：" + tag)
        if any(re.match(r"word/(header|footer)\d*\.xml$", n) for n in names):
            _issue(issues, "headers_footers", "页眉页脚未纳入正文预览，请下载原件核对")
        for cols in root.iter(W + "cols"):
            if int(cols.get(W + "num", "1")) > 1:
                _issue(issues, "columns", "分栏布局的视觉阅读顺序未核实")
        blocks = []
        paragraph_index = table_index = 0
        for body_index, node in enumerate(body):
            if node.tag == W + "p":
                loc = Locator(
                    part="word/document.xml", body_index=body_index, paragraph=paragraph_index
                )
                paragraph_index += 1
                text = _text(node)
                style = node.find(f"{W}pPr/{W}pStyle")
                outline = node.find(f"{W}pPr/{W}outlineLvl")
                num = node.find(f"{W}pPr/{W}numPr")
                kind, level, numbering = "paragraph", None, None
                if (
                    outline is not None
                    or (
                        style is not None
                        and re.search(r"heading|标题", style.get(W + "val", ""), re.I)
                    )
                    or HEADING.match(text)
                ):
                    kind = "heading"
                    level = int(outline.get(W + "val", "0")) + 1 if outline is not None else None
                if num is not None:
                    kind = "list"
                    numbering = ET.tostring(num, encoding="unicode")
                    _issue(issues, "list_markers", "保留列表层级和编号定义；自动编号显示值未重建")
                    ilvl = num.find(W + "ilvl")
                    level = int(ilvl.get(W + "val", "0")) if ilvl is not None else 0
                blocks.append(
                    StructureBlock(
                        anchor=anchor_id(version, loc),
                        kind=kind,
                        locator=loc,
                        text=text,
                        level=level,
                        numbering=numbering,
                    )
                )
            elif node.tag == W + "tbl":
                loc = Locator(part="word/document.xml", body_index=body_index, table=table_index)
                table_index += 1
                rows = []
                for row_index, row in enumerate(node.findall(W + "tr")):
                    before = row.find(f"{W}trPr/{W}gridBefore")
                    grid_column = int(before.get(W + "val", "0")) if before is not None else 0
                    cells = []
                    for cell_index, cell in enumerate(row.findall(W + "tc")):
                        span = cell.find(f"{W}tcPr/{W}gridSpan")
                        colspan = int(span.get(W + "val", "1")) if span is not None else 1
                        if not 1 <= colspan <= 100:
                            raise ParseRejected("table_span", "表格合并列数不受支持")
                        merge = cell.find(f"{W}tcPr/{W}vMerge")
                        vertical = merge.get(W + "val", "continue") if merge is not None else None
                        if vertical:
                            _issue(
                                issues,
                                "vertical_merge",
                                "纵向合并按物理单元格显示，保留起始/延续标记，未恢复视觉行高",
                            )
                        if cell.find(W + "tbl") is not None:
                            _issue(issues, "nested_table", "嵌套表格未完整解析")
                        cell_loc = loc.model_copy(
                            update={
                                "row": row_index,
                                "cell": cell_index,
                                "grid_column": grid_column,
                            }
                        )
                        text = "\n".join(_text(p) for p in cell.findall(W + "p"))
                        cells.append(
                            TableCell(
                                anchor=anchor_id(version, cell_loc),
                                locator=cell_loc,
                                text=text,
                                colspan=colspan,
                                vertical_merge=vertical,
                            )
                        )
                        grid_column += colspan
                    rows.append(cells)
                blocks.append(
                    StructureBlock(
                        anchor=anchor_id(version, loc), kind="table", locator=loc, rows=rows
                    )
                )
            elif node.tag not in (
                W + "sectPr",
                W + "bookmarkStart",
                W + "bookmarkEnd",
                W + "proofErr",
            ):
                _issue(issues, "body_block", "未支持的正文结构块", str(body_index))
        # Inherited styles can carry numbering/heading information absent on the paragraph itself.
        if "word/styles.xml" in names:
            styles = ET.fromstring(archive.read("word/styles.xml"))
            style_map = {s.get(W + "styleId"): s for s in styles.findall(W + "style")}
            for block in blocks:
                if block.kind == "table":
                    continue
                p = list(body)[block.locator.body_index]
                ps = p.find(f"{W}pPr/{W}pStyle")
                seen = set()
                key = ps.get(W + "val") if ps is not None else None
                while key in style_map and key not in seen:
                    seen.add(key)
                    style = style_map[key]
                    if style.find(f"{W}pPr/{W}numPr") is not None and block.numbering is None:
                        block.kind = "list"
                        block.numbering = ET.tostring(
                            style.find(f"{W}pPr/{W}numPr"), encoding="unicode"
                        )
                        ilvl = style.find(f"{W}pPr/{W}numPr/{W}ilvl")
                        block.level = int(ilvl.get(W + "val", "0")) if ilvl is not None else 0
                        _issue(
                            issues, "list_markers", "保留列表层级和编号定义；自动编号显示值未重建"
                        )
                    if style.find(f"{W}pPr/{W}outlineLvl") is not None and block.kind != "list":
                        block.kind = "heading"
                    base = style.find(W + "basedOn")
                    key = base.get(W + "val") if base is not None else None
    return _finish(
        "docx", blocks, [], issues, limits, "DOCX 正文 XML 的段落、列表、简单表格；不含固定页码"
    )


def _pdf(content: bytes, version: str, limits: ParseLimits) -> ParseResult:
    issues = [
        ParseIssue(
            code="pdf_layout",
            message="PDF 按提取文字顺序显示；表格、分栏、阅读顺序和视觉坐标未验证，请核对原件",
        )
    ]
    # Configuration is local to this invocation (ContextVar), with no external image decoder.
    with apply_configuration(
        zlib_maximum_output_length=limits.max_expanded_bytes,
        array_based_stream_maximum_output_length=limits.max_expanded_bytes,
        lzw_maximum_output_length=limits.max_expanded_bytes,
        run_length_maximum_output_length=limits.max_expanded_bytes,
        stream_decoding_work_maximum_length=limits.max_expanded_bytes * 4,
        page_tree_maximum_entries=100,
        page_tree_maximum_page_entries=100,
        jbig2dec_binary=None,
    ):
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ParseRejected("encrypted", "不支持加密 PDF，包括空密码加密文件")
        if len(reader.pages) > limits.max_pages:
            raise ParseRejected("page_limit", "PDF 页数超出当前试验限制")
        blocks, pages = [], []
        for page_number, page in enumerate(reader.pages, 1):
            text = page.extract_text(extraction_mode="plain")
            pages.append(ParsedPage(number=page_number, text=text))
            if not text.strip():
                _issue(
                    issues,
                    "no_page_text",
                    "此页没有可提取文字；可能是扫描件，本轮不支持 OCR",
                    str(page_number),
                )
            resources = page.get("/Resources", {})
            if resources.get("/XObject"):
                _issue(
                    issues,
                    "pdf_objects",
                    "此页包含图像或外部对象，其文字和布局未完整解析",
                    str(page_number),
                )
            if page.get("/Annots"):
                _issue(issues, "pdf_annotations", "批注或表单未纳入正文", str(page_number))
            offset = 0
            for line in text.splitlines(keepends=True):
                end = offset + len(line)
                if line.strip():
                    loc = Locator(
                        part="pdf_extracted_text", page=page_number, start=offset, end=end
                    )
                    blocks.append(
                        StructureBlock(
                            anchor=anchor_id(version, loc),
                            kind="heading" if HEADING.match(line) else "paragraph",
                            locator=loc,
                            text=line,
                        )
                    )
                offset = end
        if reader.trailer["/Root"].get("/AcroForm") or reader.trailer["/Root"].get("/Names"):
            _issue(issues, "pdf_extra", "PDF 含表单或命名资源（可能含附件），未解析这些内容")
    return _finish(
        "pdf",
        blocks,
        pages,
        issues,
        limits,
        "PDF 页码与提取文字字符区间；不提供未验证的视觉坐标或表格关系",
    )


def _finish(fmt, blocks, pages, issues, limits, scope):
    texts = [b.text for b in blocks] + [c.text for b in blocks for r in b.rows for c in r]
    count = sum(len(t) for t in texts)
    if count > limits.max_text_chars:
        raise ParseRejected("text_limit", "提取文字超过当前试验限制，未截断后冒充完整结果")
    if any(re.search(r"附件|附表|附录", text) for text in texts):
        _issue(
            issues,
            "attachment_unverified",
            "正文提到附件；本轮仅接收主文档，未核实附件内容与提交完整性",
        )
    if not any(t.strip() for t in texts):
        return ParseResult(
            status="failed",
            format=fmt,
            issues=issues
            + [ParseIssue(code="no_text", message="未找到可预览正文，不支持扫描件 OCR")],
            coverage_scope=scope,
        )
    return ParseResult(
        status="partial" if issues else "parsed",
        format=fmt,
        blocks=blocks,
        pages=pages,
        issues=issues,
        coverage_scope=scope,
        coverage_complete=not issues,
        text_chars=count,
    )


def parse_document(content: bytes, version: str, limits: ParseLimits | None = None) -> ParseResult:
    limits = limits or ParseLimits()
    fmt = "unknown"
    try:
        if len(content) > limits.max_bytes:
            raise ParseRejected("file_limit", "文件大小超过当前试验限制")
        if content.startswith(b"%PDF-"):
            fmt = "pdf"
            return _pdf(content, version, limits)
        if content.startswith(b"PK\x03\x04"):
            fmt = "docx"
            return _docx(content, version, limits)
        if content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise ParseRejected("encrypted_or_legacy", "不支持加密 Office 或旧版 DOC 文件")
        raise ParseRejected(
            "unsupported_format", "内容不是受支持的 DOCX 或文本型 PDF；不依据扩展名接受文件"
        )
    except ParseRejected as exc:
        return ParseResult(
            status="rejected", format=fmt, issues=[ParseIssue(code=exc.code, message=exc.message)]
        )
    except Exception:
        # Parser internals and document snippets must not escape into error logs or responses.
        return ParseResult(
            status="failed",
            format=fmt,
            issues=[
                ParseIssue(
                    code="invalid_document",
                    message="文件损坏、内容不受支持或解析资源限制触发；未作为完整合同接受",
                )
            ],
        )
