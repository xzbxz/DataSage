"""Legacy XLSX layout emitter, extracted from 3fd38fc send_slow_report.py.
No legacy module import, environment load, network or sending. Standard-library
runtime remains usable under Hermes independently of a Codex authoring runtime.
"""
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
import re,zipfile,math,unicodedata
from decimal import Decimal

_EXCEL_MAX_SIGNIFICANT_DIGITS = 15
_EXCEL_SAFE_INTEGER_MAX = (1 << 53) - 1
# Excel does not retain IEEE-754 subnormal values (Microsoft Learn,
# office/troubleshoot/excel/floating-point-arithmetic-inaccurate-result).
_EXCEL_MIN_POSITIVE = Decimal('2.2250738585072014E-308')

def _effective_decimal_digits(value: Decimal) -> int:
    """Return significant decimal digits without converting through float.

    Leading and trailing zero placeholders do not count.  Digits between the
    first and last nonzero digit do count, so a value such as
    ``123456789012345.67`` is classified as an exact-text value while an
    ordinary amount such as ``100.15`` remains numeric.
    """
    if not value.is_finite():
        return 0
    digits = value.as_tuple().digits
    nonzero = [index for index, digit in enumerate(digits) if digit]
    if not nonzero:
        return 1
    first, last = nonzero[0], nonzero[-1]
    return last - first + 1

def requires_exact_text(value) -> bool:
    """Whether an integer/Decimal exceeds Excel's precision or integer range.

    Floats intentionally stay on the existing numeric path.  They are already
    binary values at the input boundary, and treating every small binary64
    representation difference as a report defect would change ordinary output
    such as ``100.15``.  The report pipeline supplies Decimal values for source
    facts, so this guard protects the values whose decimal precision is still
    available to preserve.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        if abs(value) > _EXCEL_SAFE_INTEGER_MAX:
            return True
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    return (
        _effective_decimal_digits(value) > _EXCEL_MAX_SIGNIFICANT_DIGITS
        or value.copy_abs() > Decimal(_EXCEL_SAFE_INTEGER_MAX)
        or (not value.is_zero() and value.copy_abs() < _EXCEL_MIN_POSITIVE)
    )

def _exact_text(value) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Non-finite worksheet value')
        return str(int(value)) if value == value.to_integral_value() else format(value, 'f')
    return str(value)

def _rendered_cell_value(value, legacy_layout: bool):
    """Return the visible value used for width and row-height calculations."""
    return _exact_text(value) if requires_exact_text(value) else value

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
    legacy_layout: bool = False,
) -> None:
    if not sheets:
        raise ValueError("Workbook requires at least one sheet")
    used: set[str] = set()
    normalized_specs = []
    for spec in sheets:
        raw_title, headers, items = spec[:3]
        formats = spec[3] if len(spec) == 4 else {}
        normalized_specs.append((_safe_workbook_sheet_title(raw_title, used), headers, items, formats))

    precision_rows = []
    for title, headers, items, formats in normalized_specs:
        for row_index, row in enumerate(items, 2):
            for column_index, value in enumerate(row):
                if requires_exact_text(value):
                    field = headers[column_index] if column_index < len(headers) else f'Column {column_index + 1}'
                    format_kind = formats.get((row_index - 1, column_index), formats.get(column_index))
                    note = '精确文本；不参与Excel数值计算'
                    if format_kind == 'percent':
                        note = '比例原值未乘100；0.1表示10%；精确文本；不参与Excel数值计算'
                    precision_rows.append([
                        title,
                        f'{_column_name(column_index)}{row_index}',
                        str(field),
                        note,
                    ])
    if precision_rows:
        note_title = _safe_workbook_sheet_title('数值精度说明', used)
        normalized_specs.append((note_title, ['工作表', '单元格', '字段', '说明'], precision_rows, {}))

    sheet_parts = []
    for title, headers, items, formats in normalized_specs:
        rows = [headers, *items]
        width_values=[]
        for c in range(len(headers)):
            values=[row[c] if c<len(row) and row[c] is not None else '' for row in rows]
            values=[_rendered_cell_value(value, legacy_layout) for value in values]
            maximum=max((len(str(value)) if legacy_layout else _display_width(value) for value in values),default=0)
            width_values.append(max(12,min(42,maximum+2)))
        row_xml = []
        for row_index, row in enumerate(rows, 1):
            cells = []
            for column_index, value in enumerate(row):
                ref = f"{_column_name(column_index)}{row_index}"
                if borders:
                    style = ' s="3"' if row_index == 1 else ' s="2"'
                else:
                    style = ' s="1"' if row_index == 1 else ""
                format_kind = formats.get((row_index-1,column_index),formats.get(column_index))
                if row_index > 1 and format_kind == 'percent' and legacy_layout:
                    raise ValueError('LEGACY_LAYOUT_PERCENT_FORMAT_UNSUPPORTED')
                if row_index > 1 and format_kind == 'percent' and isinstance(value,(int,float,Decimal)):
                    style = ' s="5"' if borders else ' s="4"'
                if row_index > 1 and format_kind == 'amount' and not legacy_layout and isinstance(value,(int,float,Decimal)):
                    style = ' s="7"' if borders else ' s="6"'
                exact_text = requires_exact_text(value)
                if exact_text:
                    # Use the built-in text format so numeric-looking text is
                    # rendered literally by spreadsheet consumers.  This also
                    # prevents a ratio from inheriting a percentage format.
                    if legacy_layout:
                        style = ' s="5"' if borders else ' s="4"'
                    else:
                        style = ' s="9"' if borders else ' s="8"'
                cell_value=value; decimal_xml=None
                if legacy_layout and isinstance(value,Decimal):
                    if not value.is_finite(): raise ValueError('Non-finite worksheet value')
                    decimal_xml=str(int(value)) if value==value.to_integral_value() else format(value,'f')
                if exact_text:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"{style}><is><t>{_xml_text(_exact_text(cell_value))}</t></is></c>'
                    )
                elif decimal_xml is not None or (isinstance(cell_value, (int, float, Decimal)) and not isinstance(cell_value, bool)):
                    if decimal_xml is None and not (cell_value.is_finite() if isinstance(cell_value,Decimal) else math.isfinite(cell_value)):
                        raise ValueError('Non-finite worksheet value')
                    cells.append(f'<c r="{ref}"{style}><v>{decimal_xml if decimal_xml is not None else cell_value}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"{style}><is><t>{_xml_text(value)}</t></is></c>'
                    )
            rendered_row=[_rendered_cell_value(value, legacy_layout) for value in row]
            line_count=max((max(1,math.ceil(_display_width(value)/max(1,width_values[c]-2))) for c,value in enumerate(rendered_row) if c<len(width_values)),default=1)
            extreme_row = any(requires_exact_text(value) for value in row)
            if not legacy_layout or extreme_row:
                row_xml.append(f'<row r="{row_index}" ht="{15*line_count}" customHeight="1">{"".join(cells)}</row>')
            else:
                row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
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
    current_styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="3"><numFmt numFmtId="164" formatCode="0.0%"/><numFmt numFmtId="165" formatCode="#,##0.00"/><numFmt numFmtId="166" formatCode="@"/></numFmts>'
        '<fonts count="2"><font/><font><b/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="2"><border/><border>'
        '<left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/></right>'
        '<top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/></bottom>'
        '<diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="10"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    legacy_styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="166" formatCode="@"/></numFmts>'
        '<fonts count="2"><font/><font><b/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="2"><border/><border>'
        '<left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/></right>'
        '<top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/></bottom>'
        '<diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"/>'
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    styles = legacy_styles if legacy_layout else current_styles
    outpath.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(outpath, "w", zipfile.ZIP_DEFLATED) as archive:
        def write_part(name,value):
            # Packaging time is not business content. Stable bytes let a retry
            # bind the same attachment without changing its accepted fingerprint.
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o600<<16
            archive.writestr(info,value)
        write_part("[Content_Types].xml", content_types)
        write_part("_rels/.rels", root_rels)
        write_part("xl/workbook.xml", workbook)
        write_part("xl/_rels/workbook.xml.rels", workbook_rels)
        write_part("xl/styles.xml", styles)
        for index, (_, sheet_xml) in enumerate(sheet_parts, 1):
            write_part(f"xl/worksheets/sheet{index}.xml", sheet_xml)
