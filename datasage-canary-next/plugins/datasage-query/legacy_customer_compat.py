"""Pure customer artifact compatibility helpers.

The customer image and dispatch workbook in the old slow-stock workflow were
generated in ``datasage-core/send_slow_customer.py`` at
``3fd38fcb803307e1688688ca1dfbde271131157a``.  This module keeps that
presentation contract in a small, import-safe module.  It deliberately does
not import the old workflow, Hermes transport, a database adapter, or the
current workflow module.

The normal (short-field) rendering is byte/pixel compatible with the old
renderer on the same host and font installation.  Long titles and cell values
use bounded wrapping so the complete business value remains readable.  That
is an explicit content-preservation exception: a long-field PNG can have a
different height and pixels from the old clipped image, while retaining the
old colours, ``Discountable`` marker, repeated item headers, and table
geometry.

The public names are intentionally boring so the workflow owner can wire
these helpers into the existing atomic ZIP publisher without creating a
second delivery path:

* :func:`make_image` preserves the old renderer signature;
* :func:`render_customer_card` accepts the current customer object;
* :func:`customer_zip_member_name` preserves the old business filename rule;
* :func:`generate_customer_dispatch_xlsx` preserves the old audit workbook.
"""

from __future__ import annotations

from pathlib import Path
import re
import zipfile
from typing import Any
import unicodedata
from xml.sax.saxutils import escape, quoteattr

try:  # Pillow is an existing Profile dependency used by the current renderer.
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised only on hosts without Pillow
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]


LEGACY_SOURCE = "datasage-core/send_slow_customer.py"
LEGACY_REF = "3fd38fcb803307e1688688ca1dfbde271131157a"

HEADERS = ["Item No", "Color", "Stock Rolls"]
COL_WIDTHS = [180, 130, 100]
ROW_HEIGHT = 26
HEADER_HEIGHT = 30
PADDING = 8


def _require_pillow() -> None:
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("CUSTOMER_IMAGE_RENDERER_REQUIRES_PILLOW")


def _font(size: int):
    """Resolve fonts in the same order as the old renderer."""

    _require_pillow()
    for candidate in ("arial.ttf", "DejaVuSans.ttf"):
        key = (candidate, size, "base")
        if key in _FONT_CACHE:
            return _FONT_CACHE[key]
        try:
            font = ImageFont.truetype(candidate, size)
            _FONT_CACHE[key] = font
            return font
        except OSError:
            continue
    return ImageFont.load_default()


def _format_cell(value: Any, _column_index: int) -> str:
    return "" if value is None else str(value)


_CJK_RE = re.compile(r"[\u2e80-\u2fff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
_CJK_FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
_THAI_FONT_PATH = Path("C:/Windows/Fonts/LeelawUI.ttf")
_FONT_CACHE: dict[tuple[str, int, str], Any] = {}
RAQM_AVAILABLE = bool(getattr(getattr(ImageFont, "core", None), "HAVE_RAQM", False))
THAI_SHAPING_LIMITATION = (
    "Pillow basic per-run glyph placement; RAQM is unavailable on this host, "
    "so glyph coverage does not prove complex Thai shaping correctness."
)
MISSING_GLYPH_PROBE = "\U0010ffff"


def _contains_cjk(value: Any) -> bool:
    return bool(_CJK_RE.search("" if value is None else str(value)))


def _contains_thai(value: Any) -> bool:
    return bool(_THAI_RE.search("" if value is None else str(value)))


def _load_script_font(path: Path, size: int, fallback: Any) -> Any:
    if not path.exists():
        raise RuntimeError(f"CUSTOMER_IMAGE_REQUIRED_FONT_MISSING:{path.name}")
    key = (str(path).lower(), size, "raqm" if RAQM_AVAILABLE else "basic")
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    try:
        kwargs = {"layout_engine": ImageFont.Layout.RAQM} if RAQM_AVAILABLE else {}
        font = ImageFont.truetype(str(path), size, **kwargs)
        _FONT_CACHE[key] = font
        return font
    except OSError:
        raise RuntimeError(f"CUSTOMER_IMAGE_REQUIRED_FONT_UNREADABLE:{path.name}") from None


def _font_for_char(char: str, fallback: Any, size: int) -> Any:
    if _THAI_RE.match(char):
        return _load_script_font(_THAI_FONT_PATH, size, fallback)
    if _CJK_RE.match(char):
        return _load_script_font(_CJK_FONT_PATH, size, fallback)
    return fallback


def _font_for_value(value: Any, fallback: Any, size: int) -> Any:
    """Choose the installed script font for values that need it.

    Arial/DejaVu is retained for ordinary English cards so the short-field
    rendering stays pixel-compatible with the old sender.  The old Arial
    path renders some CJK/Thai glyphs as tofu on this host, so using Microsoft
    YaHei or Leelaw UI for script-bearing values is an explicit readability
    exception.
    """

    if _contains_thai(value):
        return _load_script_font(_THAI_FONT_PATH, size, fallback)
    if _contains_cjk(value):
        return _load_script_font(_CJK_FONT_PATH, size, fallback)
    return fallback


def _font_runs(value: Any, fallback: Any, size: int) -> list[tuple[Any, str]]:
    """Split mixed-language text into runs with a font covering each script."""

    text = _format_cell(value, 0)
    runs: list[tuple[Any, str]] = []
    for char in text:
        font = _font_for_char(char, fallback, size)
        if runs and getattr(runs[-1][0], "path", None) == getattr(font, "path", None):
            runs[-1] = (runs[-1][0], runs[-1][1] + char)
        else:
            runs.append((font, char))
    return runs or [(fallback, "")]


def _text_units(text: str) -> list[str]:
    """Keep combining marks attached so wrapping never splits a Thai cluster."""

    units: list[str] = []
    for char in text:
        if units and (unicodedata.combining(char) or _THAI_RE.match(char) and unicodedata.category(char) in {"Mn", "Mc"}):
            units[-1] += char
        else:
            units.append(char)
    return units


def _measure(draw: Any, text: str, font: Any) -> int:
    runs = _font_runs(text, font, getattr(font, "size", 13))
    if len(runs) == 1 and runs[0][0] is font:
        box = draw.textbbox((0, 0), text, font=font)
        return int(box[2] - box[0])
    return int(sum(float(draw.textlength(part, font=run_font)) for run_font, part in runs if part))


def _draw_text(draw: Any, xy: tuple[int, int], value: Any, fallback: Any, fill: Any, size: int) -> None:
    x, y = xy
    for font, text in _font_runs(value, fallback, size):
        if not text:
            continue
        draw.text((x, y), text, fill=fill, font=font)
        x += int(round(float(draw.textlength(text, font=font))))


def _wrap_cell(value: Any, font: Any, max_width: int, draw: Any) -> list[str]:
    """Wrap a cell only when needed; preserve explicit newlines."""

    text = _format_cell(value, 0)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for unit in _text_units(paragraph):
            trial = line + unit
            if _measure(draw, trial, font) > max_width:
                if not line:
                    # A glyph wider than the cell remains visible as one line.
                    lines.append(unit)
                    line = ""
                else:
                    lines.append(line)
                    line = unit
            else:
                line = trial
        lines.append(line)
    return lines or [""]


def _title_lines(title: Any, font: Any, draw: Any, max_width: int) -> list[str]:
    text = "Unknown" if title is None else str(title)
    # The old renderer has a right-aligned Discountable marker.  Use the
    # actual font width and available title lane so a long CJK title cannot
    # collide with that marker.  Ordinary short titles take the same path and
    # retain the old one-line geometry.
    if _measure(draw, text, font) <= max_width:
        return [text]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for unit in _text_units(paragraph):
            trial = line + unit
            if _measure(draw, trial, font) > max_width:
                if not line:
                    # Keep an over-wide glyph visible rather than dropping a
                    # business character.  The lane is otherwise bounded.
                    lines.append(unit)
                    line = ""
                else:
                    lines.append(line)
                    line = unit
            else:
                line = trial
        lines.append(line)
    return lines or ["Unknown"]


def make_image(
    sheet: list[list[Any]],
    title: str,
    filepath: Path,
    *,
    preserve_long_text: bool = True,
) -> None:
    """Render one customer card using the legacy presentation contract.

    With short fields this is the old ``make_image`` implementation: 424px
    wide, blue headers, the right-hand ``Discountable`` label, and a repeated
    header whenever the first column changes.  ``preserve_long_text`` adds
    wrapping and row growth only when the old renderer would clip a value.
    """

    _require_pillow()
    font = _font(13)
    font_bold = _font(13)
    font_title = _font(15)
    total_width = sum(COL_WIDTHS) + PADDING + len(COL_WIDTHS) * 2
    image = Image.new("RGB", (total_width, 1), "white")
    draw = ImageDraw.Draw(image)

    right_title = "Discountable"
    right_bbox = draw.textbbox((0, 0), right_title, font=font_title)
    right_width = right_bbox[2] - right_bbox[0]
    right_x = total_width - PADDING - right_width
    title_lane_width = max(1, right_x - PADDING - 8)
    title_draw_font = _font_for_value(title, font_title, 15)
    title_lines = (
        _title_lines(title, title_draw_font, draw, title_lane_width)
        if preserve_long_text
        else [_format_cell(title, 0)]
    )
    # The old renderer starts the table at y=30.  Extra title lines consume
    # space above it; short names therefore retain the exact old geometry.
    table_top = 30 + max(0, len(title_lines) - 1) * 23

    layouts: list[tuple[list[list[str]], int, bool, list[Any]]] = []
    last_item: Any = None
    for row in sheet:
        if not isinstance(row, (list, tuple)) or len(row) < len(HEADERS):
            raise ValueError("CUSTOMER_IMAGE_ROW_SHAPE_INVALID")
        starts_group = row[0] != last_item
        if starts_group:
            header_cells = [[header] for header in HEADERS]
            layouts.append((header_cells, HEADER_HEIGHT, True, [font_bold] * len(HEADERS)))
            last_item = row[0]
        if preserve_long_text:
            cell_fonts = [_font_for_value(value, font, 13) for value in row]
            cells = [
                _wrap_cell(value, cell_font, width - 12, draw)
                for value, width, cell_font in zip(row, COL_WIDTHS, cell_fonts)
            ]
            row_height = max(ROW_HEIGHT, max(len(lines) for lines in cells) * 18 + 8)
        else:
            cell_fonts = [font] * len(COL_WIDTHS)
            cells = [[_format_cell(value, index)] for index, value in enumerate(row[: len(COL_WIDTHS)])]
            row_height = ROW_HEIGHT
        layouts.append((cells, row_height, False, cell_fonts))

    total_height = table_top + sum(height for _cells, height, _header, _fonts in layouts) + PADDING + 2
    image = Image.new("RGB", (total_width, total_height), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(title_lines):
        _draw_text(draw, (PADDING, 4 + index * 23), line, title_draw_font, "#2E75B6", 15)
    draw.text(
        (right_x, 4),
        right_title,
        fill="#2E75B6",
        font=font_title,
    )

    y = table_top
    data_index = 0
    for cells, height, is_header, cell_fonts in layouts:
        for column_index, (lines, width, cell_font) in enumerate(zip(cells, COL_WIDTHS, cell_fonts)):
            x = PADDING + sum(COL_WIDTHS[:column_index]) + column_index * 2
            if is_header:
                draw.rectangle([x, y, x + width, y + height], fill="#2E75B6", outline="#1a5a8a")
                draw.text((x + 4, y + 7), lines[0], fill="white", font=font_bold)
            else:
                background = "#F2F2F2" if data_index % 2 == 0 else "white"
                draw.rectangle([x, y, x + width, y + height], fill=background, outline="#D9D9D9")
                for line_index, text in enumerate(lines):
                    _draw_text(draw, (x + 4, y + 6 + line_index * 18), text, cell_font, "#333333", 13)
        y += height
        if not is_header:
            data_index += 1
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    image.save(filepath, "PNG")


def render_customer_card(customer: dict[str, Any], filepath: Path, *, preserve_long_text: bool = True) -> None:
    """Render the current package customer shape through :func:`make_image`."""

    make_image(
        list(customer.get("products") or []),
        "Unknown" if customer.get("customer_name") is None else str(customer.get("customer_name")),
        filepath,
        preserve_long_text=preserve_long_text,
    )


def _safe_filename(value: Any, fallback: str) -> str:
    # ``None`` is missing data, not the literal business name "None".  The
    # caller's positional fallback keeps the old filename shape without
    # smuggling an internal customer id into a customer-facing filename.
    raw = "" if value is None else str(value)
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", raw).strip(" ._")
    return (cleaned or fallback)[:100]


def customer_zip_member_name(customer: dict[str, Any], index: int) -> str:
    """Return the old business-facing ``customer_no_customer_name.png`` name."""

    customer_no = _safe_filename(customer.get("customer_no"), f"CustomerNo_{index + 1}")
    customer_name = _safe_filename(customer.get("customer_name"), f"Customer_{index + 1}")
    return f"{customer_no}_{customer_name}.png"
def _safe_sheet_title(package: dict[str, Any], used: set[str]) -> str:
    names = [str(name).strip() for name in package.get("sales_names", []) if str(name).strip()]
    base = names[0] if names else str(package.get("account") or "Sales").strip()
    base = re.sub(r"[\\/*?:\[\]]", "_", base).strip(" '") or "Sales"
    candidate = base[:31]
    suffix = 2
    while candidate.casefold() in used:
        marker = f"_{suffix}"
        candidate = f"{base[:31 - len(marker)]}{marker}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def generate_customer_dispatch_xlsx(packages: list[dict[str, Any]], outpath: Path) -> None:
    """Write the old one-sheet-per-sales-owner dispatch audit workbook.

    This is the pure XML writer extracted from the old customer sender.  It
    keeps the old sheet names, two columns, frozen header, auto-filter, and
    bold header style.  No live recipients are read here.
    """

    used: set[str] = set()
    sheets: list[tuple[str, str]] = []
    for package in packages:
        title = _safe_sheet_title(package, used)
        customers = sorted(
            {
                (str(row.get("customer_no") or "").strip(), str(row.get("customer_name") or "").strip())
                for row in package.get("customers", [])
                if str(row.get("customer_no") or "").strip() and str(row.get("customer_name") or "").strip()
            },
            key=lambda row: (row[0].casefold(), row[1].casefold()),
        )
        values = [("Customer No", "Customer"), *customers]
        rows: list[str] = []
        for row_index, (customer_no, customer_name) in enumerate(values, 1):
            style = ' s="1"' if row_index == 1 else ""
            rows.append(
                f'<row r="{row_index}"><c r="A{row_index}" t="inlineStr"{style}>'
                f'<is><t>{escape(customer_no)}</t></is></c>'
                f'<c r="B{row_index}" t="inlineStr"{style}><is><t>{escape(customer_name)}</t></is></c></row>'
            )
        no_width = max(16, min(30, max(len(value[0]) for value in values) + 2))
        name_width = max(24, min(60, max(len(value[1]) for value in values) + 2))
        last_row = max(1, len(values))
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="A1:B{last_row}"/>'
            '<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>'
            f'<cols><col min="1" max="1" width="{no_width}" customWidth="1"/>'
            f'<col min="2" max="2" width="{name_width}" customWidth="1"/></cols>'
            f'<sheetData>{"".join(rows)}</sheetData>'
            f'<autoFilter ref="A1:B{last_row}"/>'
            '</worksheet>'
        )
        sheets.append((title, sheet_xml))
    if not sheets:
        raise ValueError("Customer dispatch workbook requires at least one sales package")

    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(sheets) + 1)
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
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        + "".join(
            f'<sheet name={quoteattr(title)} sheetId="{index}" r:id="rId{index}"/>'
            for index, (title, _) in enumerate(sheets, 1)
        )
        + '</sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font/><font><b/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
        '</styleSheet>'
    )
    outpath = Path(outpath)
    outpath.parent.mkdir(parents=True, exist_ok=True)
    def write_fixed(archive: zipfile.ZipFile, name: str, payload: str) -> None:
        # XLSX container timestamps are transport metadata, not business
        # content.  Fixing them keeps retries/delivery fingerprints stable.
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        archive.writestr(info, payload.encode("utf-8"))

    with zipfile.ZipFile(outpath, "w", zipfile.ZIP_DEFLATED) as archive:
        write_fixed(archive, "[Content_Types].xml", content_types)
        write_fixed(archive, "_rels/.rels", root_rels)
        write_fixed(archive, "xl/workbook.xml", workbook)
        write_fixed(archive, "xl/_rels/workbook.xml.rels", workbook_rels)
        write_fixed(archive, "xl/styles.xml", styles)
        for index, (_, sheet_xml) in enumerate(sheets, 1):
            write_fixed(archive, f"xl/worksheets/sheet{index}.xml", sheet_xml)


__all__ = [
    "LEGACY_REF",
    "LEGACY_SOURCE",
    "make_image",
    "render_customer_card",
    "customer_zip_member_name",
    "generate_customer_dispatch_xlsx",
]
