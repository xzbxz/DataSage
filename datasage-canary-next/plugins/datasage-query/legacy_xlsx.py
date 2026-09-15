"""Legacy XLSX layout emitter, extracted from 3fd38fc send_slow_report.py.
No legacy module import, environment load, network or sending. Standard-library
runtime remains usable under Hermes independently of a Codex authoring runtime.
"""
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
import re,zipfile,math,unicodedata
from decimal import Decimal

def _display_width(value):
    return max((sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in line) for line in str(value or '').splitlines()),default=0)

def _xml_text(value):
    return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','�',str(value if value is not None else '')))

def _column_name(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result

def _safe_workbook_sheet_title(title: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "_", str(title or "Sheet")).strip(" '") or "Sheet"
    candidate = base[:31]
    suffix = 2
    while candidate.casefold() in used:
        marker = f"_{suffix}"
        candidate = f"{base[:31 - len(marker)]}{marker}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate

def gen_workbook_xlsx(
    sheets: list[tuple[str, list[str], list[list]]],
    outpath: Path,
    *,
    borders: bool = False,
    landscape: bool = False,
) -> None:
    if not sheets:
        raise ValueError("Workbook requires at least one sheet")
    used: set[str] = set()
    sheet_parts = []
    for spec in sheets:
        raw_title, headers, items = spec[:3]
        formats = spec[3] if len(spec) == 4 else {}
        title = _safe_workbook_sheet_title(raw_title, used)
        rows = [headers, *items]
        width_values=[max(12,min(42,max(_display_width(row[c]) if c<len(row) else 0 for row in rows)+2)) for c in range(len(headers))]
        row_xml = []
        for row_index, row in enumerate(rows, 1):
            cells = []
            for column_index, value in enumerate(row):
                ref = f"{_column_name(column_index)}{row_index}"
                if borders:
                    style = ' s="3"' if row_index == 1 else ' s="2"'
                else:
                    style = ' s="1"' if row_index == 1 else ""
                if row_index > 1 and formats.get((row_index-1,column_index),formats.get(column_index)) == 'percent' and isinstance(value,(int,float,Decimal)):
                    style = ' s="5"' if borders else ' s="4"'
                if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                    if not (value.is_finite() if isinstance(value,Decimal) else math.isfinite(value)):
                        raise ValueError('Non-finite worksheet value')
                    cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"{style}><is><t>{_xml_text(value)}</t></is></c>'
                    )
            line_count=max((max(1,math.ceil(_display_width(value)/max(1,width_values[c]-2))) for c,value in enumerate(row) if c<len(width_values)),default=1)
            row_xml.append(f'<row r="{row_index}" ht="{15*line_count}" customHeight="1">{"".join(cells)}</row>')
        column_count = max(1, len(headers))
        last_column = _column_name(column_count - 1)
        last_row = max(1, len(rows))
        widths = []
        for column_index in range(column_count):
            values = [str(row[column_index] if column_index < len(row) and row[column_index] is not None else "") for row in rows]
            width = width_values[column_index]
            widths.append(f'<col min="{column_index + 1}" max="{column_index + 1}" width="{width}" customWidth="1"/>')
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + ('<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>' if landscape else '')
            + f'<dimension ref="A1:{last_column}{last_row}"/>'
            '<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
            f'<cols>{"".join(widths)}</cols><sheetData>{"".join(row_xml)}</sheetData>'
            f'<autoFilter ref="A1:{last_column}{last_row}"/>'
            + ('<pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
               '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>' if landscape else '')
            + '</worksheet>'
        )
        sheet_parts.append((title, sheet_xml))

    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + ''.join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(sheet_parts) + 1)
        )
        + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + ''.join(
            f'<sheet name={quoteattr(title)} sheetId="{index}" r:id="rId{index}"/>'
            for index, (title, _) in enumerate(sheet_parts, 1)
        )
        + '</sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheet_parts) + 1)
        )
        + f'<Relationship Id="rId{len(sheet_parts) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.0%"/></numFmts>'
        '<fonts count="2"><font/><font><b/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="2"><border/><border>'
        '<left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/></right>'
        '<top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/></bottom>'
        '<diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    outpath.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(outpath, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        for index, (_, sheet_xml) in enumerate(sheet_parts, 1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml)
