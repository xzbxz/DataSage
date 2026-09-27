"""Evidence bridge for registered aggregate detail pages.

This module binds the root/tools detail envelope to the shared stateless
``detail_pages`` helper.  It only consumes already-authorized claims, rows and
scope bindings; it does not compile SQL, resolve entities, or create state.
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import detail_pages


_VOLATILE_PERIOD_KEYS = frozenset(
    {
        "read_at",
        "read_utc_at",
        "read_timestamp",
        "query_read_at",
        "source_read_at",
        "closing_read_at",
        "closing_utc_at",
        "monthly_read_at",
        "monthly_read_utc_at",
        "observed_clock_offset_seconds",
        "elapsed_ms",
        "read_latency_ms",
    }
)
_PARTIAL_END_KEYS = frozenset(
    {"effective_end", "effective_current_end", "partial_observation_end", "observed_end"}
)
_VIEW_KEYS = frozenset({"dimensions", "facts", "states", "unit", "currency", "fact_units"})
_PROOF_GROUP_COUNT = "__detail_group_count"
_PROOF_UNKNOWN_MEMBER_COUNT = "__detail_member_unknown_count"


class DetailEvidenceError(detail_pages.DetailPagesError):
    """Structured detail-evidence failure caught by the query boundary."""


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DetailEvidenceError("DETAIL_BINDING_INVALID", "detail binding 含非有限 Decimal。")
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DetailEvidenceError("DETAIL_BINDING_INVALID", "detail binding 含非有限浮点数。")
        return repr(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise DetailEvidenceError("DETAIL_BINDING_INVALID", "detail binding 含不支持的值类型。")


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _live_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.search(r"T\d{2}:\d{2}(?::\d{2})?", value))


def _stable_period_node(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            name = str(raw_key)
            lowered = name.casefold()
            if (lowered == "window_end" and value.get("window_coverage") == "partial_to_read") or (
                lowered == "monthly_window_end" and value.get("monthly_is_current") in (True, 1)
            ) or lowered in _VOLATILE_PERIOD_KEYS or (
                lowered in _PARTIAL_END_KEYS and _live_timestamp(child)
            ):
                continue
            result[name] = _stable_period_node(child, key=name)
        return result
    if isinstance(value, (list, tuple)):
        return [_stable_period_node(item, key=key) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return _canonical(value)


def stable_period(period: Any) -> dict[str, Any] | None:
    """Strip live read timestamps while retaining business period dates."""

    if period is None:
        return None
    if not isinstance(period, Mapping):
        raise DetailEvidenceError("DETAIL_PERIOD_INVALID", "detail period 必须是对象。")
    stable = _stable_period_node(period)
    if not isinstance(stable, dict):
        raise DetailEvidenceError("DETAIL_PERIOD_INVALID", "detail period 规范化失败。")
    return stable


def _stable_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        raise DetailEvidenceError("DETAIL_BINDING_INVALID", "detail binding 必须是对象。")
    result: dict[str, Any] = {}
    for key, value in binding.items():
        if str(key) == "period":
            result[str(key)] = stable_period(value)
        else:
            result[str(key)] = _canonical(value)
    return result


def _exact_decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise DetailEvidenceError("DETAIL_PROOF_INVALID", f"detail proof {field} 不是精确 Decimal。")
    if not isinstance(value, (str, int, float, Decimal)):
        raise DetailEvidenceError("DETAIL_PROOF_INVALID", f"detail proof {field} 类型无效。")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DetailEvidenceError("DETAIL_PROOF_INVALID", f"detail proof {field} 数值无效。") from None
    if not parsed.is_finite():
        raise DetailEvidenceError("DETAIL_PROOF_INVALID", f"detail proof {field} 非有限。")
    return parsed


def _count(value: Any, *, field: str) -> int:
    number = _exact_decimal(value, field=field)
    if number < 0 or number != number.to_integral_value():
        raise DetailEvidenceError("DETAIL_PROOF_INVALID", f"detail proof {field} 不是非负整数。")
    return int(number)


def _claim_view(claim: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    if not isinstance(claim, Mapping):
        raise DetailEvidenceError("DETAIL_CLAIM_INVALID", f"detail claim[{index}] 不是对象。")
    dimensions = claim.get("dimensions")
    if not isinstance(dimensions, list) or any(not isinstance(item, Mapping) for item in dimensions):
        raise DetailEvidenceError("DETAIL_CLAIM_INVALID", f"detail claim[{index}] dimensions 无效。")
    view: dict[str, Any] = {"dimensions": copy.deepcopy(dimensions)}
    for key in ("facts", "states", "fact_units"):
        value = claim.get(key)
        if value is not None:
            if not isinstance(value, Mapping):
                raise DetailEvidenceError("DETAIL_CLAIM_INVALID", f"detail claim[{index}].{key} 无效。")
            view[key] = copy.deepcopy(dict(value))
    for key in ("unit", "currency"):
        if key in claim:
            view[key] = copy.deepcopy(claim[key])
    def stable_facts(value):
        if isinstance(value, Mapping):
            return {key: stable_facts(item) for key, item in value.items()
                    if key not in _VOLATILE_PERIOD_KEYS}
        if isinstance(value, list):
            return [stable_facts(item) for item in value]
        return value
    return stable_facts(view)


def _view_identity(view: Mapping[str, Any]) -> str:
    return _canonical_json(view.get("dimensions", []))


def _proof_value(rows: Sequence[Mapping[str, Any]], key: str) -> tuple[Any, list[str]]:
    values = [row[key] for row in rows if isinstance(row, Mapping) and key in row]
    if not values or len(values) != len(rows):
        return None, [f"missing:{key}"]
    if any(value is None for value in values):
        return None, [f"invalid:{key}"]
    canonical = [_canonical_json(value) for value in values]
    if len(set(canonical)) != 1:
        return None, [f"inconsistent:{key}"]
    return values[0], []


def _source_reconciliation(
    views: Sequence[Mapping[str, Any]],
    raw_rows: Sequence[Mapping[str, Any]],
    reconciliation: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    fields = tuple(contract.get("summable_fields") or ())
    errors: list[str] = []
    group_raw, group_errors = _proof_value(raw_rows, _PROOF_GROUP_COUNT)
    unknown_raw, unknown_errors = _proof_value(raw_rows, _PROOF_UNKNOWN_MEMBER_COUNT)
    errors.extend(group_errors)
    errors.extend(unknown_errors)
    group_count: int | None = None
    member_unknown_count: int | None = None
    if group_raw is not None:
        try:
            group_count = _count(group_raw, field=_PROOF_GROUP_COUNT)
        except DetailEvidenceError as exc:
            errors.append(f"invalid:{_PROOF_GROUP_COUNT}")
    if unknown_raw is not None:
        try:
            member_unknown_count = _count(unknown_raw, field=_PROOF_UNKNOWN_MEMBER_COUNT)
        except DetailEvidenceError:
            errors.append(f"invalid:{_PROOF_UNKNOWN_MEMBER_COUNT}")
    if group_count is not None and group_count != len(views):
        errors.append("unequal:group_count")
    if (
        member_unknown_count is not None
        and member_unknown_count != reconciliation.get("analysis_match_unknown_count")
    ):
        errors.append("unequal:member_unknown_count")
    full = reconciliation.get("full_subtotals") if isinstance(reconciliation, Mapping) else None
    known = reconciliation.get("known_matched_subtotals") if isinstance(reconciliation, Mapping) else None
    known_members = reconciliation.get("known_matched_member_count") if isinstance(reconciliation, Mapping) else None
    known_proofs: dict[str, str | None] = {}
    unknown_proofs: dict[str, int | None] = {}
    for field in fields:
        raw_known, known_errors = _proof_value(raw_rows, f"__detail_known_{field}")
        raw_unknown, unknown_errors_for_field = _proof_value(raw_rows, f"__detail_unknown_{field}")
        errors.extend(known_errors)
        errors.extend(unknown_errors_for_field)
        expected_known = known.get(field) if isinstance(known, Mapping) else None
        expected_member_count = known_members.get(field) if isinstance(known_members, Mapping) else None
        if raw_known is not None:
            try:
                proof_decimal = _exact_decimal(raw_known, field=f"__detail_known_{field}")
                known_proofs[field] = format(proof_decimal, "f")
                if expected_known is not None and proof_decimal != _exact_decimal(expected_known, field=f"known_matched_subtotals.{field}"):
                    errors.append(f"unequal:__detail_known_{field}")
                if expected_member_count == 0 and proof_decimal != 0:
                    errors.append(f"unequal:__detail_known_{field}")
            except DetailEvidenceError:
                errors.append(f"invalid:__detail_known_{field}")
        else:
            known_proofs[field] = None
        if raw_unknown is not None:
            try:
                unknown_count = _count(raw_unknown, field=f"__detail_unknown_{field}")
                unknown_proofs[field] = unknown_count
                if group_count is not None and isinstance(expected_member_count, int) and unknown_count != group_count - expected_member_count:
                    errors.append(f"unequal:__detail_unknown_{field}")
            except DetailEvidenceError:
                errors.append(f"invalid:__detail_unknown_{field}")
        else:
            unknown_proofs[field] = None
    errors = sorted(set(errors))
    if not views and group_count == 0 and member_unknown_count == 0 and not errors:
        status = "empty"
    elif errors:
        status = "not_reconciled"
    else:
        status = "reconciled"
    return {
        "status": status,
        "errors": errors,
        "group_count": group_count,
        "member_unknown_count": member_unknown_count,
        "known_matched_subtotals": copy.deepcopy(known) if isinstance(known, Mapping) else {},
        "full_subtotals": copy.deepcopy(full) if isinstance(full, Mapping) else {},
        "known_proofs": known_proofs,
        "unknown_proofs": unknown_proofs,
        "basis": "same_statement_sql_window_vs_public_decimal_known_subset",
        "complete_matched_total_available": status == "reconciled" and reconciliation.get("status") == "complete",
    }


def _metadata_seal(metadata: Mapping[str, Any]) -> str:
    return "sha256_" + hashlib.sha256(
        _canonical_json({key: value for key, value in metadata.items() if key != "detail_seal"}).encode("utf-8")
    ).hexdigest()


def build_detail(
    *,
    claims: Sequence[Mapping[str, Any]],
    raw_rows: Sequence[Mapping[str, Any]],
    detail: Mapping[str, Any],
    contract: Mapping[str, Any],
    collection_cap: int,
    collection_truncated: bool,
    binding: Mapping[str, Any],
    period: Mapping[str, Any],
    scope_fingerprint: str,
    projection_fingerprint: str,
) -> dict[str, Any]:
    """Build a sealed page envelope from public claims and SQL proof columns."""

    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
        raise DetailEvidenceError("DETAIL_CLAIM_INVALID", "detail claims 必须是序列。")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise DetailEvidenceError("DETAIL_PROOF_INVALID", "detail raw_rows 必须是序列。")
    if not isinstance(binding, Mapping) or not isinstance(period, Mapping):
        raise DetailEvidenceError("DETAIL_BINDING_INVALID", "detail binding/period 无效。")
    stable_binding = _stable_binding(binding)
    stable_read_context = stable_period(period)
    views = [_claim_view(claim, index=index) for index, claim in enumerate(claims)]
    identities = [_view_identity(view) for view in views]
    if len(identities) != len(set(identities)):
        raise DetailEvidenceError("DETAIL_DUPLICATE_ROW", "detail public views 含重复聚合组。")
    if views and raw_rows and len(raw_rows) != len(views):
        raise DetailEvidenceError("DETAIL_PROOF_INVALID", "detail claims/raw_rows 数量不一致。")
    scope_binding = {"resolved_scope_hash": _digest("scope_v1_", stable_binding)}
    page = detail_pages.paginate(
        views,
        detail,
        scope_binding,
        contract,
        collection_cap,
        collection_truncated=collection_truncated,
    )
    reconciliation = page.get("reconciliation") or {}
    full_reconciliation = reconciliation.get("full") if isinstance(reconciliation, Mapping) else {}
    source_reconciliation = _source_reconciliation(
        views,
        raw_rows,
        full_reconciliation if isinstance(full_reconciliation, Mapping) else {},
        contract,
    )
    page_digest = detail_pages.public_collection_digest(page.get("rows") or [])
    metadata: dict[str, Any] = {
        "version": "registered-detail-evidence/v1",
        "contract": contract.get("id"),
        "contract_version": detail_pages.DETAIL_CONTRACT_VERSION,
        "id": detail.get("id"),
        "population_scope_fingerprint": scope_fingerprint,
        "resolved_scope_hash": page.get("scope_digest"),
        "detail_projection_fingerprint": projection_fingerprint,
        "analysis_fingerprint": scope_fingerprint,
        "period": copy.deepcopy(dict(period)),
        "read_context": stable_read_context,
        "collection_cap": collection_cap,
        "page_count": page.get("page_count"),
        "requested_page_size": page.get("requested_page_size"),
        "effective_page_size": page.get("effective_page_size"),
        "page_index": page.get("page_index"),
        "start_index": page.get("start_index"),
        "end_index_exclusive": page.get("end_index_exclusive"),
        "full_count": page.get("full_count"),
        "returned_count": page.get("returned_count"),
        "has_more": page.get("has_more"),
        "next_cursor": page.get("next_cursor"),
        "collection_digest": page.get("data_digest"),
        "page_digest": page_digest,
        "reconciliation": reconciliation,
        "source_reconciliation": source_reconciliation,
        "cursor_consistency": page.get("cursor_consistency"),
        "snapshot_continuity": page.get("snapshot_continuity"),
    }
    metadata["detail_seal"] = _metadata_seal(metadata)
    return metadata


def metadata_is_valid(detail: Any, result: Mapping[str, Any]) -> bool:
    """Validate a model-visible detail envelope against its parent result."""

    try:
        if not isinstance(detail, Mapping) or not isinstance(result, Mapping):
            return False
        if detail.get("version") != "registered-detail-evidence/v1":
            return False
        if detail.get("detail_seal") != _metadata_seal(detail):
            return False
        if detail.get("population_scope_fingerprint") != result.get("scope_fingerprint"):
            return False
        if detail.get("detail_projection_fingerprint") != result.get("projection_fingerprint"):
            return False
        if detail.get("period") != result.get("applied_time_range"):
            return False
        claims = result.get("claim_ledger")
        if not isinstance(claims, list) or "rows" in detail:
            return False
        if detail.get("returned_count") != len(claims) or result.get("row_count") != len(claims):
            return False
        claim_views = [_claim_view(claim, index=index) for index, claim in enumerate(claims)]
        if detail.get("page_digest") != detail_pages.public_collection_digest(claim_views):
            return False
        full_count = detail.get("full_count")
        returned_count = detail.get("returned_count")
        if type(full_count) is not int or type(returned_count) is not int or full_count < returned_count or returned_count < 0:
            return False
        start, end = detail.get("start_index"), detail.get("end_index_exclusive")
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= full_count or end - start != returned_count:
            return False
        if not isinstance(detail.get("has_more"), bool):
            return False
        if detail.get("has_more") is not (end < full_count):
            return False
        if result.get("truncated") is not (full_count > returned_count):
            return False
        if detail.get("source_reconciliation", {}).get("status") not in {"reconciled", "empty", "not_reconciled"}:
            return False
        return True
    except (DetailEvidenceError, detail_pages.DetailPagesError, TypeError, ValueError, KeyError):
        return False


__all__ = ["DetailEvidenceError", "stable_period", "build_detail", "metadata_is_valid"]
