"""Pure price-reminder compatibility adapters.

The fixed legacy object is ``release/datasage-0.2.0`` at
``3fd38fcb803307e1688688ca1dfbde271131157a``.  Only the presentation and
small, deterministic source-shaping rules from that object are represented
here.  This module deliberately does not import the old Node/Python modules,
open a database, write a snapshot, call a transport, or accept a baseline.

The adapters are an intentionally narrow seam for the current workflow:
ordinary valid price changes retain the old message layout and workbook
ordering, while the current contract keeps its safety semantics for missing
values, ambiguous rows, unknown basis, and first observations.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence


LEGACY_REFERENCE = "3fd38fcb803307e1688688ca1dfbde271131157a"
LEGACY_REFERENCE_NAME = "release/datasage-0.2.0"

# These are evidence pointers, not imports.  They make the adapter reviewable
# without making the installed Profile depend on the old repository.
LEGACY_SOURCES = {
    "idk": "datasage-core/idk_no_price_reminder.js:46-96",
    "sales": "datasage-core/ready_goods_price_push.py:31-95",
    "sales_workbook": "datasage-core/ready_goods_price_push.py:111-120",
    "purchase": "datasage-core/ready_goods_purchase_tracker.js:139-213",
}


class CompatibilityError(ValueError):
    """Raised when a pure compatibility input is structurally ambiguous."""


def decimal_value(value: Any) -> Decimal | None:
    """Return a finite Decimal, preserving source precision, or ``None``."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _date_value(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Database values are often written as ``YYYY-MM-DD HH:MM:SS``; the
        # fromisoformat path handles those, while date-only values are also
        # accepted.  A malformed source timestamp stays unknown.
        return None


def price_text(value: Any, *, missing: str = "-") -> str:
    """Format a price like the old notice, without hiding real precision.

    Exact cent values retain the old two-decimal display.  When a value has
    meaningful sub-cent precision (including ``-0.001``), its normalized
    decimal is shown instead of rounding it to ``0.00`` or ``-0.00``.
    """

    number = decimal_value(value)
    if number is None:
        return missing
    cents = number.quantize(Decimal("0.01"))
    if cents == number:
        return format(cents, ".2f")
    return format(number.normalize(), "f")


def source_price_text(value: Any, *, missing: str = "Unknown") -> str:
    """Render a sales-price source value without imposing purchase rounding."""

    number = decimal_value(value)
    if number is None:
        return missing
    # Sales' old Python helper interpolated the source value directly.  Keep a
    # textual DB value's visible scale, while numeric fixtures remain compact.
    if isinstance(value, str):
        return value.strip()
    return format(number.normalize(), "f")


def signed_delta(old: Any, new: Any, *, missing: str = "Unknown") -> str:
    before, after = decimal_value(old), decimal_value(new)
    if before is None or after is None:
        return missing
    delta = after - before
    rendered = price_text(delta, missing=missing)
    return ("+" if delta >= 0 else "") + rendered


def _currency(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else "Unknown"


def _unit_currency(row: Mapping[str, Any]) -> str:
    # The old purchase message showed the unit only.  The current contract
    # requires a known currency to remain visible, so append it when present.
    parts = [str(row[name]).strip() for name in ("unit_cuur", "currency_no") if row.get(name) not in (None, "")]
    return f"（{'；'.join(parts)}）" if parts else ""


def _short_date(value: Any, *, missing: str = "-") -> str:
    if value in (None, ""):
        return missing
    parsed = _date_value(value)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%d")
    text = str(value)
    return text[:10] if text else missing


def _short_month_day(value: Any, *, missing: str = "-") -> str:
    parsed = _date_value(value)
    if parsed is None:
        text = str(value) if value not in (None, "") else ""
        return text[5:10] if len(text) >= 10 and text[4] == "-" else (text or missing)
    return parsed.strftime("%m-%d")


def idk_message(rows: Sequence[Mapping[str, Any]], observed_at: Any, *, window_days: int = 0) -> str:
    """Render the old IDK notice with current source-row accuracy."""

    now = _date_value(observed_at)
    if window_days > 0 and now is None:raise CompatibilityError('IDK_OBSERVATION_CLOCK_REQUIRED')
    lines = ["**IDK Slow-Moving Products Without Promotion Price**"]
    if window_days > 0:
        since = now - timedelta(days=window_days)
        lines.append(f"Scope: new in last {window_days} days ({_short_month_day(since)} ~ {_short_month_day(now)})")
    else:
        lines.append(f"Scope: all slow-moving pool (as of {_short_month_day(now)})")
    # A source row is not necessarily a unique product, so keep the current
    # accurate qualifier even though the rest of the layout is legacy-shaped.
    lines.extend([f"{len(rows)} product source row(s) without promotion price:", ""])
    for index, row in enumerate(rows, 1):
        product = row.get("goods_no", row.get("product")) or "Unknown"
        color = row.get("attr_val", row.get("color")) or "-"
        lines.append(f"{index}. {product} | Color {color}")
    lines.extend(["", "Please set promotion prices for the above products."])
    return "\n".join(lines)


def _sales_lines(changes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten both current flat-change rows and old grouped product payloads."""

    result: list[dict[str, Any]] = []
    for product in changes:
        nested = product.get("lines")
        if not isinstance(nested, Sequence) or isinstance(nested, (str, bytes)):
            result.append(dict(product))
            continue
        for line in nested:
            row = dict(product)
            row.pop("lines", None)
            row.update(dict(line))
            result.append(row)
    return result


def sales_message(
    changes: Iterable[Mapping[str, Any]],
    *,
    customer_mapping_complete: bool | None = None,
    has_attachment: bool | None = None,
) -> str:
    """Render a sales notice in the old layout with exact signed deltas."""

    rows = _sales_lines(changes)
    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        key = str(row.get("goods_no") or row.get("goods_name") or "Unknown")
        grouped.setdefault(key, []).append(row)
    lines = ["Ready Product Price Adjustment", "", f"{len(grouped)} product(s) price changed:"]
    for product, product_rows in grouped.items():
        for row in product_rows:
            grade = row.get("customer_grade") or "-"
            color = row.get("color_label") or "-"
            old = source_price_text(row.get("old_ddp_price"), missing="Unknown")
            new = source_price_text(row.get("new_ddp_price"), missing="Unknown")
            currency = _currency(row.get("currency_no"))
            lines.append(f"- {product} ({grade}/{color}): {old} -> {new} ({signed_delta(row.get('old_ddp_price'), row.get('new_ddp_price'))}) {currency}")
    if customer_mapping_complete is not None:
        lines.append("")
        if customer_mapping_complete:
            lines.append(
                "See attachment for your customers who purchased these products (one Sheet per product)."
                if has_attachment
                else "You have no customers who purchased these products, so no attachment is included."
            )
        else:
            lines.append("Customer matching is not confirmed complete; do not interpret a missing attachment as no buyers.")
    return "\n".join(lines)


def sales_manager_message(
    region: str,
    changes: Iterable[Mapping[str, Any]],
    *,
    has_attachment: bool = True,
) -> str:
    """Render the old manager summary heading and the current sales lines."""

    body = sales_message(changes)
    first, rest = body.split("\n", 1)
    return "\n".join([first, f"Region: {region}", rest, "", "See attachment for all sales' customers across this region (one Sheet per product)."])


def _purchase_changed(row: Mapping[str, Any], field: str) -> bool:
    return decimal_value(row.get("old_" + field)) != decimal_value(row.get("new_" + field))


def _purchase_direction(row: Mapping[str, Any], fields: Sequence[str]) -> Decimal | None:
    deltas: dict[str, Decimal | None] = {}
    for field in fields:
        old, new = decimal_value(row.get("old_" + field)), decimal_value(row.get("new_" + field))
        deltas[field] = None if old is None or new is None else new - old
    # The old implementation uses the tax-excluded side first.  Keep that
    # ordering for complete numeric rows; missing sides are handled separately.
    return deltas.get("exc") if deltas.get("exc") not in (None, Decimal("0")) else deltas.get("inc")


def _purchase_block(row: Mapping[str, Any]) -> str:
    supplier = row.get("supplier_name") or row.get("supplier_no") or "未知名供应商"
    goods_no = row.get("goods_no") or "Unknown"
    goods_name = row.get("goods_name")
    product = f"{goods_no}（{goods_name}）" if goods_name else str(goods_no)
    lines = [f"**供应商：{supplier}**", f"货号：{product}", f"色标：{row.get('color_label') or '-'}"]
    for field, label in (("inc", "含税价"), ("exc", "不含税价")):
        if not _purchase_changed(row, field):
            continue
        lines.append(f"{label}：{price_text(row.get('old_' + field))} → {price_text(row.get('new_' + field))}{_unit_currency(row)}")
    if row.get("validity_state") in {"unknown_validity", "recorded_quote_only"}:
        lines.insert(3, "有效期未确认，仅记录报价变化。")
    return "\n".join(lines)


def purchase_message(changes: Iterable[Mapping[str, Any]]) -> str | None:
    """Render old purchase grouping, preserving safe unknown/missing states."""

    rows = [dict(row) for row in changes if _purchase_changed(row, "inc") or _purchase_changed(row, "exc")]
    if not rows:
        return None
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict(
        (("🔻 采购价下调", []), ("🔺 采购价上调", []), ("缺失状态变化（需核对）", []))
    )
    for row in rows:
        missing_changed = any(
            (decimal_value(row.get("old_" + field)) is None) != (decimal_value(row.get("new_" + field)) is None)
            for field in ("inc", "exc")
            if _purchase_changed(row, field)
        )
        if missing_changed:
            group = "缺失状态变化（需核对）"
        else:
            direction = _purchase_direction(row, ("exc", "inc"))
            group = "🔻 采购价下调" if direction is not None and direction < 0 else "🔺 采购价上调"
        groups[group].append(row)
    dates = sorted({_short_date(row.get("adjust_date")) for row in rows})
    blocks = [f"**{title}（{len(items)}条）**\n\n" + "\n\n────────────\n\n".join(_purchase_block(row) for row in items) for title, items in groups.items() if items]
    return "**采购报价变更提醒**\n调整日期：" + "、".join(dates) + "\n\n" + "\n\n**————————————**\n\n".join(blocks)


def customer_sheets(customers_by_goods: Mapping[str, Iterable[Sequence[Any]]]) -> list[tuple[str, list[str], list[list[Any]]]]:
    """Return old per-product sales sheets in deterministic sorted order."""

    sheets: list[tuple[str, list[str], list[list[Any]]]] = []
    for goods_no in sorted(customers_by_goods or {}):
        rows = [[customer[0], customer[1]] for customer in customers_by_goods[goods_no]]
        sheets.append((str(goods_no), ["Customer No", "Customer"], rows))
    return sheets or [("Summary", ["Customer No", "Customer"], [])]


def manager_customer_sheets(rows_by_goods: Mapping[str, Iterable[Sequence[Any]]]) -> list[tuple[str, list[str], list[list[Any]]]]:
    """Return old manager sheets in deterministic sorted product order."""

    sheets: list[tuple[str, list[str], list[list[Any]]]] = []
    for goods_no in sorted(rows_by_goods or {}):
        sheets.append((str(goods_no), ["Sales", "Customer No", "Customer"], [list(row) for row in rows_by_goods[goods_no]]))
    return sheets or [("Summary", ["Sales", "Customer No", "Customer"], [])]


__all__ = [
    "CompatibilityError",
    "LEGACY_REFERENCE",
    "LEGACY_REFERENCE_NAME",
    "LEGACY_SOURCES",
    "decimal_value",
    "price_text",
    "source_price_text",
    "signed_delta",
    "idk_message",
    "sales_message",
    "sales_manager_message",
    "purchase_message",
    "customer_sheets",
    "manager_customer_sheets",
]
