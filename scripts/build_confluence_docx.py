"""Build a Word-compatible DOCX from the Confluence-ready Markdown draft."""

from __future__ import annotations

import html
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "confluence_cfc_shipping_cost_estimator_draft.md"
OUTPUT = ROOT / "docs" / "CFC_Last_Mile_Shipping_Cost_Estimator_Confluence_Draft.docx"

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def w(tag: str) -> str:
    return f"w:{tag}"


def xml_text(value: str) -> str:
    return escape(html.unescape(value))


def run(text: str, *, bold: bool = False, italic: bool = False, mono: bool = False, color: str = "000000") -> str:
    props = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if mono:
        props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
    props.append(f'<w:color w:val="{color}"/>')
    props.append('<w:sz w:val="20"/>')
    return f"<w:r><w:rPr>{''.join(props)}</w:rPr><w:t xml:space=\"preserve\">{xml_text(text)}</w:t></w:r>"


def inline_runs(text: str) -> str:
    """Convert the small inline Markdown subset used by the draft."""
    parts = re.split(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)", text)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("`") and part.endswith("`"):
            out.append(run(part[1:-1], mono=True))
        elif part.startswith("**") and part.endswith("**"):
            out.append(run(part[2:-2], bold=True))
        elif part.startswith("*") and part.endswith("*"):
            out.append(run(part[1:-1], italic=True))
        else:
            out.append(run(part))
    return "".join(out)


def paragraph(text: str = "", *, style: str = "Normal", indent: int = 0, keep_next: bool = False) -> str:
    ppr = [f'<w:pStyle w:val="{style}"/>']
    if indent:
        ppr.append(f'<w:ind w:left="{indent}"/>')
    if keep_next:
        ppr.append("<w:keepNext/>")
    return f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{inline_runs(text)}</w:p>"


def table_cell(text: str, width: int, *, header: bool = False, shade: str = "FFFFFF") -> str:
    cell_props = (
        f'<w:tcW w:w="{width}" w:type="dxa"/>'
        f'<w:shd w:fill="{shade}" w:val="clear"/>'
        '<w:tcMar><w:top w:w="100" w:type="dxa"/><w:left w:w="120" w:type="dxa"/>'
        '<w:bottom w:w="100" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tcMar>'
        '<w:vAlign w:val="center"/>'
    )
    cell_paragraph = f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{run(text, bold=header, color="FFFFFF" if header else "000000")}</w:p>'
    return f"<w:tc><w:tcPr>{cell_props}</w:tcPr>{cell_paragraph}</w:tc>"


def table(rows: list[list[str]], widths: list[int]) -> str:
    parts = [
        '<w:tbl><w:tblPr><w:tblW w:w="9000" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:left w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:right w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="D9D9D9"/></w:tblBorders>'
        '</w:tblPr>',
        "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{width}"/>' for width in widths) + "</w:tblGrid>",
    ]
    for row_index, row in enumerate(rows):
        header = row_index == 0
        shade = "1F4E78" if header else ("F3F7FA" if row_index % 2 == 0 else "FFFFFF")
        tr_props = "<w:tblHeader/><w:cantSplit/>" if header else "<w:cantSplit/>"
        cells = []
        for index, value in enumerate(row):
            cells.append(table_cell(value, widths[index], header=header, shade=shade))
        parts.append(f"<w:tr><w:trPr>{tr_props}</w:trPr>{''.join(cells)}</w:tr>")
    parts.append("</w:tbl>")
    return "".join(parts)


def parse_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append([re.sub(r"`([^`]+)`", r"\1", cell) for cell in cells])
    return rows


def build_body(markdown: str) -> str:
    lines = markdown.splitlines()
    body: list[str] = []
    index = 0
    first_title = True
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("|"):
            block: list[str] = []
            while index < len(lines) and lines[index].startswith("|"):
                block.append(lines[index])
                index += 1
            rows = parse_table(block)
            if rows:
                widths = [9000 // len(rows[0])] * len(rows[0])
                if len(rows[0]) == 3:
                    if rows[0][0].lower() in {"carrier/service", "data area"}:
                        widths = [1900, 3600, 3500]
                    else:
                        widths = [2200, 3600, 3200]
                body.append(table(rows, widths))
                body.append(paragraph())
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 1 and first_title:
                body.append(paragraph(title, style="Title", keep_next=True))
                first_title = False
            else:
                body.append(paragraph(title, style=f"Heading{level}", keep_next=True))
            index += 1
            continue
        metadata = re.match(r"^\*\*(.+?):\*\*\s*(.*)$", line)
        if metadata:
            body.append(paragraph(f"{metadata.group(1)}: {metadata.group(2)}", style="Metadata"))
            index += 1
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            body.append(paragraph(bullet.group(1), style="ListBullet"))
            index += 1
            continue
        numbered = re.match(r"^(\d+)\.\s+(.+)$", line)
        if numbered:
            body.append(paragraph(numbered.group(2), style="ListNumber"))
            index += 1
            continue
        if line.strip() == "---":
            index += 1
            continue
        body.append(paragraph(line.strip()))
        index += 1
    return "".join(body)


def styles_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{NS_W}">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:color w:val="000000"/><w:sz w:val="20"/></w:rPr></w:rPrDefault>
  <w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:color w:val="000000"/><w:sz w:val="20"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:spacing w:before="0" w:after="260"/><w:keepNext/></w:pPr><w:rPr><w:rFonts w:ascii="Aptos Display" w:hAnsi="Aptos Display"/><w:color w:val="000000"/><w:b/><w:sz w:val="36"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="300" w:after="120"/><w:keepNext/></w:pPr><w:rPr><w:rFonts w:ascii="Aptos Display" w:hAnsi="Aptos Display"/><w:color w:val="000000"/><w:b/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="Heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="220" w:after="100"/><w:keepNext/></w:pPr><w:rPr><w:rFonts w:ascii="Aptos Display" w:hAnsi="Aptos Display"/><w:color w:val="000000"/><w:b/><w:sz w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="Heading 3"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="160" w:after="80"/><w:keepNext/></w:pPr><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:color w:val="000000"/><w:b/><w:sz w:val="21"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Metadata"><w:name w:val="Metadata"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="40"/></w:pPr><w:rPr><w:color w:val="404040"/><w:sz w:val="18"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="360" w:hanging="180"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListNumber"><w:name w:val="List Number"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="360" w:hanging="180"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:style>
</w:styles>'''


def numbering_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{NS_W}">
  <w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="-"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="180"/></w:pPr></w:lvl></w:abstractNum>
  <w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="180"/></w:pPr></w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>'''


def document_xml(body: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}">
  <w:body>{body}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>'''


def core_properties_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>CFC Last Mile Shipping Cost Estimator and Carrier Opportunity Comparator</dc:title>
  <dc:subject>Confluence replacement page draft</dc:subject>
  <dc:creator>Advanced Analytics</dc:creator>
  <cp:keywords>CFC, last mile, shipping cost, estimator, carrier opportunities, invoice reconciliation</cp:keywords>
</cp:coreProperties>'''


def content_types_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''


def root_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''


def document_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>'''


def app_properties_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Microsoft Word</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <Company>Kmart Australia Limited</Company>
  <LinksUpToDate>false</LinksUpToDate>
  <SharedDoc>false</SharedDoc>
  <HyperlinksChanged>false</HyperlinksChanged>
  <AppVersion>16.0000</AppVersion>
</Properties>'''


def write_docx() -> Path:
    markdown = SOURCE.read_text(encoding="utf-8")
    body = build_body(markdown)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml())
        archive.writestr("_rels/.rels", root_rels_xml())
        archive.writestr("docProps/core.xml", core_properties_xml())
        archive.writestr("docProps/app.xml", app_properties_xml())
        archive.writestr("word/document.xml", document_xml(body))
        archive.writestr("word/styles.xml", styles_xml())
        archive.writestr("word/numbering.xml", numbering_xml())
        archive.writestr("word/_rels/document.xml.rels", document_rels_xml())
    return OUTPUT


if __name__ == "__main__":
    if not SOURCE.exists():
        print(f"Source draft not found: {SOURCE}", file=sys.stderr)
        raise SystemExit(1)
    path = write_docx()
    print(path)
