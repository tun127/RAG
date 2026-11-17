import io
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from production_rag.contracts.models import ParseLimits, version_id
from production_rag.contracts.parser import parse_document
from production_rag.contracts.service import ContractService


def docx(body, extras=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + body
            + "</w:body></w:document>",
        )
        for key, value in (extras or {}).items():
            z.writestr(key, value)
    return buffer.getvalue()


def paragraph(text):
    from xml.sax.saxutils import escape

    return '<w:p><w:r><w:t xml:space="preserve">' + escape(text) + "</w:t></w:r></w:p>"


def pdf(text=None, pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=400, height=400)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 10 300 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def parse(data, limits=None):
    limits = limits or ParseLimits()
    return parse_document(data, version_id(data, limits), limits)


def test_literals_structure_lists_and_table_order_preserved():
    exact = "甲方：示例企业；不得提前支付；2026年11月7日；￥12,345.60；比例 5％ / 30%"
    body = (
        paragraph("第一条 付款")
        + '<w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="2"/></w:numPr></w:pPr><w:r><w:t>不得变更</w:t></w:r></w:p>'
    )
    body += (
        "<w:tbl><w:tr><w:tc>"
        + paragraph(exact)
        + "</w:tc><w:tc>"
        + paragraph("乙方")
        + "</w:tc></w:tr></w:tbl>"
        + paragraph("结束")
    )
    r = parse(docx(body))
    assert [b.kind for b in r.blocks] == ["heading", "list", "table", "paragraph"]
    assert r.blocks[1].level == 1
    assert r.blocks[2].rows[0][0].text == exact
    assert r.blocks[2].rows[0][1].text == "乙方"
    assert r.blocks[-1].text == "结束"
    assert "list_markers" in {i.code for i in r.issues}


@pytest.mark.parametrize(
    "data,code",
    [
        (b"not a DOCX", "unsupported_format"),
        (b"PK\x03\x04broken", "invalid_document"),
        (b"%PDF-broken", "invalid_document"),
        (bytes.fromhex("d0cf11e0a1b11ae1"), "encrypted_or_legacy"),
        (docx("<w:p><w:ins><w:r><w:t>修订</w:t></w:r></w:ins></w:p>"), "revisions"),
        (docx(paragraph("x"), {"../escape": "x"}), "unsafe_archive"),
        (pdf(encrypted=True), "encrypted"),
        (pdf(pages=3), "page_limit"),
        (pdf(), "no_text"),
    ],
)
def test_invalid_and_unsupported_inputs_are_visible(data, code):
    r = parse(data)
    assert r.status in ("failed", "rejected")
    assert not r.coverage_complete
    assert code in {i.code for i in r.issues}


def test_limits_do_not_silently_truncate():
    data = docx(paragraph("不得" * 20))
    for limits, code in [
        (ParseLimits(max_bytes=10), "file_limit"),
        (ParseLimits(max_expanded_bytes=10), "expanded_limit"),
        (ParseLimits(max_text_chars=10), "text_limit"),
    ]:
        r = parse(data, limits)
        assert r.status == "rejected" and not r.blocks
        assert r.issues[0].code == code


def test_version_and_task_isolation_and_safe_filename(tmp_path):
    service = ContractService(tmp_path / "private", tmp_path / "kb")
    data = docx(paragraph("付款 30%，不得提前。"))
    first = service.upload(data, "../../outside.docx")
    assert first.result.status == "parsed"
    assert first.original_name == "outside.docx" and not (tmp_path / "outside.docx").exists()
    assert service.upload(data, "same.docx", first.task_id) == first
    second = service.upload(docx(paragraph("付款 50%，不得提前。")), "a.docx", first.task_id)
    assert first.version_id != second.version_id
    a, b = first.result.blocks[0], second.result.blocks[0]
    assert a.anchor != b.anchor
    assert service.locate(first.task_id, first.version_id, a.anchor)["text"] == a.text
    with pytest.raises(FileNotFoundError):
        service.locate(first.task_id, second.version_id, a.anchor)
    restarted = ContractService(service.root, tmp_path / "kb")
    assert restarted.get(first.task_id, first.version_id) == first
    other = restarted.upload(data, "same.docx")
    assert other.task_id != first.task_id
    assert service.original(first) == data
    with pytest.raises(FileNotFoundError):
        service.get("../escape", first.version_id)
    (service._path(first.task_id, first.version_id) / "source.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="校验失败"):
        service.locate(first.task_id, first.version_id, a.anchor)


def test_private_store_cannot_be_shared_knowledge(tmp_path):
    with pytest.raises(ValueError):
        ContractService(tmp_path / "kb/private", tmp_path / "kb")


def test_attachment_completeness_is_not_clause_judgement():
    r = parse(docx(paragraph("技术标准见附件一。")))
    assert r.status == "partial" and "未核实" in r.material_status
    assert r.clause_assessment == "未实施条款判断"
    assert "attachment_unverified" in {i.code for i in r.issues}


def test_pdf_critical_literals_and_page_identity():
    exact = "Buyer: Acme; 2026-11-07; 12,345.60; 30%; NOT permitted"
    r = parse(pdf(exact, pages=2))
    assert r.status == "partial" and len(r.pages) == 2
    assert all(exact in page.text for page in r.pages)
    assert r.blocks[0].anchor != r.blocks[1].anchor
    assert [b.locator.page for b in r.blocks] == [1, 2]


def test_nested_tables_and_visual_objects_cannot_claim_full_coverage():
    body = paragraph("正文") + "<w:p><w:r><w:drawing/></w:r></w:p>"
    body += (
        '<w:tbl><w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
        + paragraph("上格")
        + "<w:tbl><w:tr><w:tc>"
        + paragraph("嵌套")
        + "</w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>"
    )
    r = parse(docx(body))
    assert r.status == "partial" and not r.coverage_complete
    assert {"unsupported_drawing", "nested_table", "vertical_merge"}.issubset(
        {i.code for i in r.issues}
    )
    assert r.blocks[-1].rows[0][0].vertical_merge == "restart"


def test_style_inherited_list_is_preserved():
    styles = '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:styleId="L"><w:pPr><w:numPr><w:ilvl w:val="2"/><w:numId w:val="3"/></w:numPr></w:pPr></w:style></w:styles>'
    body = '<w:p><w:pPr><w:pStyle w:val="L"/></w:pPr><w:r><w:t>不得删去</w:t></w:r></w:p>'
    r = parse(docx(body, {"word/styles.xml": styles}))
    assert r.blocks[0].kind == "list" and r.blocks[0].level == 2
    assert r.blocks[0].text == "不得删去"


def test_table_revision_outside_runs_is_rejected():
    data = docx("<w:tbl><w:tblPrChange/></w:tbl>")
    assert parse(data).issues[0].code == "revisions"


def test_entity_expansion_cannot_read_external_material():
    data = docx(
        paragraph("x"),
        {
            "word/comments.xml": '<!DOCTYPE x [<!ENTITY steal SYSTEM "file:///C:/Windows/win.ini">]><x>&steal;</x>'
        },
    )
    r = parse(data)
    assert r.status == "failed" and r.issues[0].code == "invalid_document"
