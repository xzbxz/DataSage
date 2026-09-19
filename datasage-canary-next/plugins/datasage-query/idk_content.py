"""Bounded IDK unpriced reminder content.

This module is presentation-only.  Selection, source-row grain, window
filtering and as-of time come from the caller's governed observation.  It
preserves every input row, including duplicate product/color rows, and only
segments the resulting Markdown body for the delivery byte budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_SAFE_BYTES = 4000
MAX_MARKDOWN_BYTES = 4096
TITLE = "**IDK Slow-Moving Products Without Promotion Price**"
FOOTER = "Please set promotion prices for the above products."


class IdkContentError(ValueError):
    """Raised when a complete source row cannot fit within the channel budget."""


@dataclass(frozen=True)
class IdkContent:
    """Rendered parts plus an auditable, non-business summary."""

    parts: tuple[str, ...]
    summary: dict[str, Any]


def _clock(value: Any, *, code: str) -> tuple[datetime, str]:
    if isinstance(value, datetime):
        parsed = value
    elif value not in (None, ""):
        if isinstance(value, str) and "T" not in value and " " not in value:
            raise IdkContentError(code)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise IdkContentError(code) from None
    else:
        raise IdkContentError(code)
    return parsed, parsed.isoformat(sep=" ")


def _escape(value: Any, fallback: str = "-") -> str:
    text = fallback if value in (None, "") else str(value)
    # Keep CJK/Thai and all source characters intact; escape Markdown syntax
    # characters so names cannot alter headings, links or emphasis.
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    for char in ("\\", "`", "*", "_", "[", "]", "~", "<", ">", "&", "#"):
        text = text.replace(char, "\\" + char)
    return text


def _record_fields(record: Mapping[str, Any]) -> tuple[str, str]:
    product = record.get("product", record.get("goods_no", record.get("product_no")))
    color = record.get("color", record.get("attr_val", record.get("color_label")))
    return _escape(product, "Unknown"), _escape(color, "-")


def _scope_line(window_days: int, asof: str, window_start: str | None) -> str:
    if window_days:
        return f"Scope: new in last {window_days} days (created >= {window_start}; observed at {asof})"
    return f"Scope: all slow-moving pool (as of {asof})"


def _entry_line(index: int, record: Mapping[str, Any]) -> str:
    product, color = _record_fields(record)
    return f"{index}. {product} | Color {color}"


def _body(
    entries: Sequence[str],
    *,
    part_number: int,
    part_count: int,
    source_row_count: int,
    window_days: int,
    window_start: str | None,
    asof: str,
) -> str:
    header = "\n".join((TITLE, _scope_line(window_days, asof, window_start), f"{source_row_count} product source row(s) without promotion price:", f"Part {part_number}/{part_count}", ""))
    return header + "\n".join(entries) + "\n\n" + FOOTER


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _pack(
    entry_lines: Sequence[str],
    *,
    part_count: int,
    source_row_count: int,
    window_days: int,
    window_start: str | None,
    asof: str,
    max_bytes: int,
) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for line in entry_lines:
        candidate = _body(current + [line], part_number=len(groups) + 1, part_count=part_count, source_row_count=source_row_count, window_days=window_days, window_start=window_start, asof=asof)
        if _bytes(candidate) > max_bytes:
            if not current:
                raise IdkContentError("IDK_SINGLE_SOURCE_ROW_EXCEEDS_BYTE_BUDGET")
            groups.append(current)
            current = [line]
            if _bytes(_body(current, part_number=len(groups) + 1, part_count=part_count, source_row_count=source_row_count, window_days=window_days, window_start=window_start, asof=asof)) > max_bytes:
                raise IdkContentError("IDK_SINGLE_SOURCE_ROW_EXCEEDS_BYTE_BUDGET")
        else:
            current.append(line)
    if current:
        groups.append(current)
    return groups


def build_idk_parts(
    records: Iterable[Mapping[str, Any]],
    *,
    observed_at: Any,
    window_days: int = 0,
    window_start: Any = None,
    max_bytes: int = DEFAULT_SAFE_BYTES,
) -> IdkContent:
    """Render complete source-row parts under a UTF-8 byte budget.

    Empty input returns no body parts and an auditable summary.  A single row
    that cannot fit is blocked with ``IdkContentError``; it is never truncated
    or silently moved into an attachment.
    """

    if type(window_days) is not int or not 0 <= window_days <= 366:
        raise IdkContentError("IDK_WINDOW_DAYS_INVALID")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_MARKDOWN_BYTES:
        raise IdkContentError("IDK_BYTE_BUDGET_INVALID")
    observed_clock, asof = _clock(observed_at, code="IDK_OBSERVED_CLOCK_INVALID")
    cutoff_text: str | None = None
    if window_days:
        cutoff_clock, cutoff_text = _clock(window_start, code="IDK_WINDOW_START_INVALID")
        if cutoff_clock > observed_clock:
            raise IdkContentError("IDK_WINDOW_START_AFTER_OBSERVED_CLOCK")
    elif window_start not in (None, ""):
        _, cutoff_text = _clock(window_start, code="IDK_WINDOW_START_INVALID")
    source_rows = []
    for record in records:
        if not isinstance(record, Mapping):
            raise IdkContentError("IDK_SOURCE_ROW_INVALID")
        row = dict(record)
        product = row.get("product", row.get("goods_no", row.get("product_no")))
        if type(product) not in (str,int) or str(product).strip() in {"", "Unknown", "未知"}:
            raise IdkContentError("IDK_SOURCE_PRODUCT_ID_MISSING")
        if "source_ref" in row and row.get("source_ref") in (None, ""):
            raise IdkContentError("IDK_SOURCE_REF_MISSING")
        source_rows.append(row)
    fingerprint_rows = [
        {
            "source_ref": r.get("source_ref", r.get("id")),
            "product": r.get("product", r.get("goods_no")),
            "color": r.get("color", r.get("attr_val", r.get("color_label"))),
        }
        for r in source_rows
    ]
    canonical_rows = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str) for row in fingerprint_rows]
    source_digest = hashlib.sha256(json.dumps(sorted(canonical_rows), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    source_order_digest = hashlib.sha256(json.dumps(canonical_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    summary: dict[str, Any] = {
        "source_row_count": len(source_rows),
        "source_row_multiset_sha256": source_digest,
        "source_row_order_sha256": source_order_digest,
        "observed_at": asof,
        "window_days": window_days,
        "window_start": cutoff_text,
        "max_bytes": max_bytes,
        "channel": "markdown",
        "status": "empty_no_task" if not source_rows else "ready",
        "part_count": 0,
        "entry_count": len(source_rows),
    }
    if not source_rows:
        return IdkContent(parts=(), summary=summary)
    lines = [_entry_line(index, row) for index, row in enumerate(source_rows, 1)]
    part_count = 1
    groups: list[list[str]] = []
    for _ in range(32):
        groups = _pack(lines, part_count=part_count, source_row_count=len(source_rows), window_days=window_days, window_start=cutoff_text, asof=asof, max_bytes=max_bytes)
        actual = len(groups)
        if actual == part_count:
            break
        part_count = actual
    else:
        raise IdkContentError("IDK_PART_COUNT_DID_NOT_CONVERGE")
    parts = tuple(_body(group, part_number=index, part_count=len(groups), source_row_count=len(source_rows), window_days=window_days, window_start=cutoff_text, asof=asof) for index, group in enumerate(groups, 1))
    sizes = [_bytes(part) for part in parts]
    if any(size > max_bytes for size in sizes):
        raise IdkContentError("IDK_RENDERED_PART_EXCEEDS_BYTE_BUDGET")
    summary.update({"part_count": len(parts), "part_byte_sizes": sizes, "status": "ready"})
    return IdkContent(parts=parts, summary=summary)


__all__ = ["DEFAULT_SAFE_BYTES", "IdkContent", "IdkContentError", "build_idk_parts"]
