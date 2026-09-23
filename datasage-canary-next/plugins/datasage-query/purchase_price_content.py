"""Complete, bounded purchase-price Markdown content.

The purchase comparison has already happened before this module is called.
This module is therefore deliberately presentation-only: it does not select
quotes, open a database, read a snapshot, or send a message.  It reuses the
reviewed legacy purchase renderer for direction/grouping and record blocks,
then packs complete blocks into independently readable UTF-8 Markdown parts.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping, Sequence

from .legacy_price_compat import (
    _purchase_block,
    _purchase_changed,
    _short_date,
    decimal_value,
    purchase_message,
)


DEFAULT_SAFE_BYTES = 4000
MAX_MARKDOWN_BYTES = 4096

TITLE = "**采购报价变更提醒**"
GROUP_DOWN = "🔻 采购价下调"
GROUP_UP = "🔺 采购价上调"
GROUP_MISSING = "缺失状态变化（需核对）"
GROUP_ORDER = (GROUP_DOWN, GROUP_UP, GROUP_MISSING)
GROUP_SEPARATOR = "**————————————**"
RECORD_SEPARATOR = "────────────"


class PurchaseContentError(ValueError):
    """Raised when a complete purchase record cannot be rendered safely."""


@dataclass(frozen=True)
class PurchaseContent:
    """Rendered standalone parts and an auditable rendering summary."""

    parts: tuple[str, ...]
    summary: dict[str, Any]


def _clock(value: Any) -> str:
    """Return the caller's fixed observation clock in a stable display form."""

    if isinstance(value, datetime):
        parsed = value
    elif value not in (None, ""):
        text = str(value).strip()
        # A date-only value would be silently normalized to midnight by
        # datetime.fromisoformat().  The observation contract requires the
        # actual timestamp supplied by the governed comparison.
        if "T" not in text and " " not in text:
            raise PurchaseContentError("PURCHASE_OBSERVED_CLOCK_INVALID")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise PurchaseContentError("PURCHASE_OBSERVED_CLOCK_INVALID") from None
    else:
        raise PurchaseContentError("PURCHASE_OBSERVED_CLOCK_INVALID")
    return parsed.isoformat(sep=" ")


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _escape(value: Any, fallback: str = "-") -> str:
    """Escape source text without changing the source value's visible data."""

    text = fallback if value in (None, "") else str(value)
    # Escape source backslashes before adding the visible ``\\n``/``\\r``
    # representation for line breaks.  This keeps a source newline as one
    # escaped pair instead of accidentally doubling the escape we just added.
    text = text.replace("\\", "\\\\")
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    for char in ("`", "*", "_", "[", "]", "~", "<", ">", "&", "#"):
        text = text.replace(char, "\\" + char)
    return text


def _safe_display_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a row and escape only free-text fields used by the old block."""

    result = dict(row)
    for field in ("supplier_name", "supplier_no", "goods_no", "goods_name", "color_label", "unit_cuur", "currency_no"):
        if field in result and result[field] not in (None, ""):
            result[field] = _escape(result[field])
    return result


def _canonical_row(row: Mapping[str, Any]) -> str:
    """Canonicalize a full source row for multiset and order evidence."""

    # Source mappings normally have string keys.  Converting keys here keeps
    # the summary deterministic even for a synthetic mapping with unusual
    # keys, without changing what is rendered.
    normalized = {str(key): value for key, value in row.items()}
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(values: Sequence[str]) -> str:
    payload = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _validate_price_value(row: Mapping[str, Any], field: str) -> None:
    """Reject malformed non-empty price values instead of treating them as null."""

    for prefix in ("old_", "new_"):
        key = prefix + field
        if key not in row:
            continue
        value = row.get(key)
        if value in (None, ""):
            continue
        if decimal_value(value) is None:
            raise PurchaseContentError(f"PURCHASE_{key.upper()}_INVALID")


def _validate_rows(changes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if changes is None or isinstance(changes, (str, bytes, bytearray)):
        raise PurchaseContentError("PURCHASE_CHANGES_INVALID")
    try:
        source_rows = list(changes)
    except TypeError:
        raise PurchaseContentError("PURCHASE_CHANGES_INVALID") from None

    rows: list[dict[str, Any]] = []
    for row in source_rows:
        if not isinstance(row, Mapping):
            raise PurchaseContentError("PURCHASE_CHANGE_RECORD_INVALID")
        item = dict(row)
        goods_no = item.get("goods_no")
        if goods_no in (None, "") or not str(goods_no).strip():
            raise PurchaseContentError("PURCHASE_GOODS_NO_MISSING")
        if not any(key in item for key in ("old_inc", "new_inc", "old_exc", "new_exc")):
            raise PurchaseContentError("PURCHASE_PRICE_FIELDS_MISSING")
        _validate_price_value(item, "inc")
        _validate_price_value(item, "exc")
        rows.append(item)
    return rows


def _changed(row: Mapping[str, Any]) -> bool:
    return _purchase_changed(row, "inc") or _purchase_changed(row, "exc")


def _legacy_group(row: Mapping[str, Any]) -> str:
    """Read the reviewed renderer's group decision without reimplementing it."""

    # Group detection reads only the fixed renderer's heading.  Escape source
    # fields first so a supplier/product value containing a newline cannot
    # inject a line that looks like a group heading.
    rendered = purchase_message([_safe_display_row(row)])
    if rendered is None:
        raise PurchaseContentError("PURCHASE_CHANGE_RECORD_HAS_NO_CHANGE")
    lines = set(rendered.splitlines())
    for title in GROUP_ORDER:
        if f"**{title}（1条）**" in lines:
            return title
    raise PurchaseContentError("PURCHASE_LEGACY_GROUP_UNRESOLVED")


def _adjustment_dates(rows: Sequence[Mapping[str, Any]], legacy_text: str) -> str:
    """Keep the legacy date order, while failing closed if it is unavailable."""

    for line in legacy_text.splitlines():
        if line.startswith("调整日期："):
            return line[len("调整日期：") :]
    # This should be unreachable for purchase_message, but a malformed or
    # monkey-patched compatibility renderer must not silently lose dates.
    dates = sorted({_short_date(row.get("adjust_date")) for row in rows})
    if not dates:
        raise PurchaseContentError("PURCHASE_ADJUSTMENT_DATE_UNAVAILABLE")
    return "、".join(dates)


def _render_part(
    items: Sequence[tuple[str, str]],
    *,
    dates: str,
    observed_at: str,
    total_count: int,
    part_number: int,
    part_count: int,
    disclosure: str,
) -> str:
    grouped: OrderedDict[str, list[str]] = OrderedDict((title, []) for title in GROUP_ORDER)
    for group, block in items:
        grouped[group].append(block)

    header = [
        TITLE,
        f"调整日期：{dates}",
        f"观察时点：{observed_at}",
        f"批总数：{total_count}条",
        f"Part {part_number}/{part_count}",
    ]
    if disclosure:
        header.append(f"披露：{disclosure}")

    sections: list[str] = []
    for title in GROUP_ORDER:
        blocks = grouped[title]
        if not blocks:
            continue
        body = f"**{title}（{len(blocks)}条）**\n\n" + f"\n\n{RECORD_SEPARATOR}\n\n".join(blocks)
        sections.append(body)
    if not sections:
        raise PurchaseContentError("PURCHASE_PART_HAS_NO_RECORDS")
    return "\n".join(header) + "\n\n" + f"\n\n{GROUP_SEPARATOR}\n\n".join(sections)


def _pack(
    records: Sequence[tuple[str, str]],
    *,
    dates: str,
    observed_at: str,
    total_count: int,
    part_count: int,
    disclosure: str,
    max_bytes: int,
) -> list[list[tuple[str, str]]]:
    """Greedily pack records while keeping each record block intact."""

    parts: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for record in records:
        candidate = current + [record]
        candidate_text = _render_part(
            candidate,
            dates=dates,
            observed_at=observed_at,
            total_count=total_count,
            part_number=len(parts) + 1,
            part_count=part_count,
            disclosure=disclosure,
        )
        if _bytes(candidate_text) > max_bytes:
            if not current:
                raise PurchaseContentError("PURCHASE_SINGLE_CHANGE_EXCEEDS_BYTE_BUDGET")
            parts.append(current)
            current = [record]
            single_text = _render_part(
                current,
                dates=dates,
                observed_at=observed_at,
                total_count=total_count,
                part_number=len(parts) + 1,
                part_count=part_count,
                disclosure=disclosure,
            )
            if _bytes(single_text) > max_bytes:
                raise PurchaseContentError("PURCHASE_SINGLE_CHANGE_EXCEEDS_BYTE_BUDGET")
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _part_group_counts(parts: Sequence[Sequence[tuple[str, str]]]) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    for part in parts:
        counts = {title: 0 for title in GROUP_ORDER}
        for group, _ in part:
            counts[group] += 1
        result.append(counts)
    return result


def build_purchase_parts(
    changes: Iterable[Mapping[str, Any]],
    *,
    observed_at: Any,
    disclosure: str = "",
    max_bytes: int = DEFAULT_SAFE_BYTES,
) -> PurchaseContent:
    """Render complete purchase-change records under a UTF-8 byte budget.

    ``changes`` must already be a comparable, sendable change set.  Empty or
    all-unchanged input is represented explicitly by an empty result.  A
    mixture of changed and unchanged records is rejected so no record can be
    silently filtered from a message.  Every non-empty part repeats the fixed
    adjustment dates, observation clock, batch count and part number.
    """

    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_MARKDOWN_BYTES:
        raise PurchaseContentError("PURCHASE_BYTE_BUDGET_INVALID")
    observed_text = _clock(observed_at)
    if disclosure is None:
        disclosure = ""
    if not isinstance(disclosure, str):
        raise PurchaseContentError("PURCHASE_DISCLOSURE_INVALID")
    disclosure_text = _escape(disclosure, "") if disclosure else ""

    rows = _validate_rows(changes)
    unchanged_rows = [row for row in rows if not _changed(row)]
    changed_rows = [row for row in rows if _changed(row)]
    input_canonical = [_canonical_row(row) for row in rows]
    input_multiset = sorted(input_canonical)

    base_summary: dict[str, Any] = {
        "status": "empty_no_change" if not changed_rows else "ready",
        "observed_at": observed_text,
        "max_bytes": max_bytes,
        "channel": "markdown",
        "input_row_count": len(rows),
        "change_record_count": len(changed_rows),
        "unchanged_record_count": len(unchanged_rows),
        "input_change_multiset_sha256": _digest(input_multiset),
        "input_change_order_sha256": _digest(input_canonical),
        "part_count": 0,
        "part_byte_sizes": [],
        "rendered_record_count": 0,
        "group_counts": {title: 0 for title in GROUP_ORDER},
        "part_group_counts": [],
    }

    if not changed_rows:
        return PurchaseContent(parts=(), summary=base_summary)
    if unchanged_rows:
        raise PurchaseContentError("PURCHASE_UNCHANGED_RECORD_MIXED_WITH_CHANGES")

    # Call the fixed renderer once for its canonical date/header behavior, and
    # once per row for its already-reviewed group decision.  The custom
    # renderer below only adds metadata and packs those complete legacy blocks.
    legacy_text = purchase_message(changed_rows)
    if legacy_text is None:
        raise PurchaseContentError("PURCHASE_LEGACY_RENDER_EMPTY")
    dates = _escape(_adjustment_dates(changed_rows, legacy_text))
    records: list[tuple[str, str]] = []
    for row in changed_rows:
        group = _legacy_group(row)
        records.append((group, _purchase_block(_safe_display_row(row))))

    # The legacy message always renders complete groups in this order.  Sort
    # before packing so a part boundary can never put an up group before a
    # later down group merely because the input iterable was interleaved.
    group_rank = {title: index for index, title in enumerate(GROUP_ORDER)}
    records.sort(key=lambda item: group_rank[item[0]])

    # Part numbers contribute bytes to every header, so pack to a fixed point
    # after the real record count is known.  This is the same convergence rule
    # used by the bounded IDK content module, with whole purchase blocks as the
    # indivisible units.
    estimated_part_count = 1
    packed: list[list[tuple[str, str]]] = []
    seen_counts: set[int] = set()
    for _ in range(64):
        packed = _pack(
            records,
            dates=dates,
            observed_at=observed_text,
            total_count=len(records),
            part_count=estimated_part_count,
            disclosure=disclosure_text,
            max_bytes=max_bytes,
        )
        actual = len(packed)
        if actual == estimated_part_count:
            break
        if estimated_part_count in seen_counts:
            raise PurchaseContentError("PURCHASE_PART_COUNT_DID_NOT_CONVERGE")
        seen_counts.add(estimated_part_count)
        estimated_part_count = actual
    else:
        raise PurchaseContentError("PURCHASE_PART_COUNT_DID_NOT_CONVERGE")

    parts = tuple(
        _render_part(
            part,
            dates=dates,
            observed_at=observed_text,
            total_count=len(records),
            part_number=index,
            part_count=len(packed),
            disclosure=disclosure_text,
        )
        for index, part in enumerate(packed, 1)
    )
    sizes = [_bytes(part) for part in parts]
    if any(size > max_bytes for size in sizes):
        raise PurchaseContentError("PURCHASE_RENDERED_PART_EXCEEDS_BYTE_BUDGET")

    part_counts = _part_group_counts(packed)
    totals = {title: sum(item[title] for item in part_counts) for title in GROUP_ORDER}
    ordered_rendered = [f"{group}\x00{block}" for group, block in records]
    rendered_multiset = sorted(ordered_rendered)
    base_summary.update(
        {
            "status": "ready",
            "part_count": len(parts),
            "part_byte_sizes": sizes,
            "rendered_record_count": len(ordered_rendered),
            "group_counts": totals,
            "part_group_counts": part_counts,
            # Keep the duplicate-preserving sorted multiset visible for an
            # independent reconstruction test; its digest is convenient for
            # persisted evidence and does not collapse equal records.
            "rendered_record_multiset": rendered_multiset,
            "rendered_record_multiset_sha256": _digest(rendered_multiset),
            "rendered_record_order_sha256": _digest(ordered_rendered),
            # Names parallel the source-row evidence names used by the other
            # bounded content module, while retaining the explicit rendered
            # aliases above for callers that distinguish source vs. output.
            "change_record_multiset_sha256": _digest(rendered_multiset),
            "change_record_order_sha256": _digest(ordered_rendered),
            "record_multiset": rendered_multiset,
            "adjustment_dates": dates,
            "disclosure": disclosure,
        }
    )
    return PurchaseContent(parts=parts, summary=base_summary)


__all__ = [
    "DEFAULT_SAFE_BYTES",
    "MAX_MARKDOWN_BYTES",
    "PurchaseContent",
    "PurchaseContentError",
    "build_purchase_parts",
]
