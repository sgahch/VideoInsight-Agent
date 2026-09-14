"""用纯标准库（zipfile + XML）生成 Word 文档（.docx）。

docx 本质是 OOXML 的 zip 包，这里手工构造最小但完整的结构：
[Content_Types].xml / _rels/.rels / word/document.xml / word/styles.xml / docProps/*.xml。
支持：多级标题、正文、列表（-、数字）、表格、粗体、斜体、引用。
"""

import re
import zipfile
from pathlib import Path

ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def esc(text: str) -> str:
    return "".join(ESCAPE.get(c, c) for c in str(text))


def xml_paragraph(style: str, text: str) -> str:
    """普通段落（支持行内 **bold**、*italic*）。"""
    runs = []
    for token in re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            runs.append(f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{esc(token[2:-2])}</w:t></w:r>')
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            runs.append(f'<w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve">{esc(token[1:-1])}</w:t></w:r>')
        else:
            runs.append(f'<w:r><w:t xml:space="preserve">{esc(token)}</w:t></w:r>')
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{"".join(runs)}</w:p>'


def xml_list_item(text: str, numbered: bool) -> str:
    numid = "2" if numbered else "1"
    return (
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="'
        + numid
        + '"/></w:numPr><w:ind w:left="420"/></w:pPr>'
        + inline(text)
        + "</w:p>"
    )


def inline(text: str) -> str:
    """行内标记（无段落结构）。"""
    return xml_paragraph("Normal", text).replace("<w:p>", "").replace("</w:p>", "")


def xml_table(headers: list[str], rows: list[list[str]]) -> str:
    def cell(text: str, bold: bool = False) -> str:
        rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
        return (
            '<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/></w:tcPr>'
            f'<w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr><w:r>{rpr}<w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p></w:tc>'
        )

    head = "".join(cell(h, bold=True) for h in headers)
    body = "".join("<w:tr>" + "".join(cell(c) for c in row) + "</w:tr>" for row in rows)
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="999999"/><w:left w:val="single" w:sz="4" w:color="999999"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="999999"/><w:right w:val="single" w:sz="4" w:color="999999"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="999999"/><w:insideV w:val="single" w:sz="4" w:color="999999"/>'
        "</w:tblBorders></w:tblPr>"
        f'<w:tr>{head}</w:tr>{body}</w:tbl>'
        + '<w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr></w:p>'
    )


def md_to_docx_body(md: str) -> str:
    parts = []
    table_buf: list[list[str]] | None = None
    table_headers: list[str] | None = None

    def flush_table():
        nonlocal table_buf, table_headers
        if table_buf is not None:
            parts.append(xml_table(table_headers or [], table_buf))
            table_buf = None
            table_headers = None

    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        # 表格块
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if table_buf is None:
                table_headers = cells
                table_buf = []
            elif re.fullmatch(r"[-:|\s]+", stripped.replace("|", " ").strip()):
                continue  # 分隔行
            else:
                table_buf.append(cells)
            continue
        flush_table()

        # 引用
        if stripped.startswith(">"):
            parts.append(xml_paragraph("Quote", stripped.lstrip("> ")))
            continue
        # 分隔线
        if re.fullmatch(r"[-*_]{3,}", stripped):
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 3)
            parts.append(xml_paragraph(f"Heading{level}", m.group(2).strip()))
            continue
        # 列表
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            parts.append(xml_list_item(m.group(1).strip(), numbered=False))
            continue
        m = re.match(r"^\d+[.、)]\s+(.*)$", stripped)
        if m:
            parts.append(xml_list_item(m.group(1).strip(), numbered=True))
            continue
        # 空行
        if not stripped:
            continue
        # 普通段落
        parts.append(xml_paragraph("Normal", stripped))

    flush_table()
    return "".join(parts)


DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{body}
<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
</w:body></w:document>"""

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>
<w:rPr><w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:hAnsi="Microsoft YaHei"/>
<w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:rPr><w:b/><w:sz w:val="32"/><w:color w:val="1F4E79"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
<w:rPr><w:b/><w:sz w:val="26"/><w:color w:val="2E74B5"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
<w:rPr><w:b/><w:sz w:val="24"/><w:color w:val="404040"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>
<w:rPr><w:i/><w:color w:val="666666"/></w:rPr></w:style>
<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/></w:style>
</w:styles>"""

NUMBERING_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0">
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/><w:lvlJc w:val="left"/></w:lvl>
</w:abstractNum>
<w:abstractNum w:abstractNumId="1">
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>"""

CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>视界通商业计划书</dc:title><dc:creator>VideoInsight Team</dc:creator>
<cp:lastModifiedBy>VideoInsight Team</cp:lastModifiedBy></cp:coreProperties>"""

APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
<Application>VideoInsight</Application></Properties>"""


def md_to_docx(md_path: Path, out_path: Path) -> None:
    md = md_path.read_text(encoding="utf-8")
    body = md_to_docx_body(md)
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": RELS,
        "word/document.xml": DOCUMENT_XML.format(body=body),
        "word/styles.xml": STYLES_XML,
        "word/numbering.xml": NUMBERING_XML,
        "word/_rels/document.xml.rels": DOC_RELS,
        "docProps/core.xml": CORE,
        "docProps/app.xml": APP,
    }
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    print(f"docx saved: {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    md_to_docx(root / "02-商业计划书-提交版.md", root / "02-商业计划书-提交版.docx")
