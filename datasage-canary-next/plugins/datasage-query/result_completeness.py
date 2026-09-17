"""Small read-only projection of the existing query evidence boundary.

This module does not add a wire contract or reinterpret a business metric.  It
reuses the query result's status, data state, truncation flag, claims/facts and
period evidence for local consumers and the delivery gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Sequence

from . import evidence


_GROUP_FIELDS = (
    "population_group_count", "population_count", "fabric_population_groups",
    "population_union_groups", "population_display_groups", "rank_population_count",
)
_ROW_FIELDS = (
    "population_row_count", "scope_row_count", "fabric_scope_rows",
    "eligible_row_count", "assessed_row_count",
)
_TIME_FIELDS = (
    "observed_at", "read_at", "monthly_read_at", "fabric_read_at",
    "fabric_read_utc_at", "report_at", "report_utc",
)
_UNKNOWN_PARTS = ("unknown", "missing", "unassessable", "incomplete")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 and str(parsed) == value.strip() else None
    return None


def _rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = result.get("rows")
    if not isinstance(rows, list):
        rows = result.get("claim_ledger")
    return [row for row in rows or () if isinstance(row, Mapping)]


def _consistent_field(result: Mapping[str, Any], field: str) -> int | None:
    values: list[int] = []
    value = _integer(result.get(field))
    if value is not None:
        values.append(value)
    for row in _rows(result):
        facts = row.get("facts") if isinstance(row.get("facts"), Mapping) else row
        value = _integer(facts.get(field))
        if value is not None:
            values.append(value)
    return values[0] if values and len(set(values)) == 1 else None


def _first_field(result: Mapping[str, Any], fields: Sequence[str]) -> int | None:
    for field in fields:
        value = _consistent_field(result, field)
        if value is not None:
            return value
    ranking = result.get("ranking_evidence")
    if isinstance(ranking, Mapping):
        return _integer(ranking.get("population_count"))
    return None


def _times(result: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    sources: list[Mapping[str, Any]] = [result]
    for key in ("applied_time_range", "time"):
        period = result.get(key)
        if isinstance(period, Mapping):
            sources.append(period)
    for row in _rows(result):
        facts = row.get("facts") if isinstance(row.get("facts"), Mapping) else row
        if isinstance(facts, Mapping):
            sources.append(facts)
    for source in sources:
        for field in _TIME_FIELDS:
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                found.setdefault(field, value)
    return found


def _unknown(result: Mapping[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def add(path: str, value: Any) -> None:
        if value in (None, False, "", 0):
            return
        if isinstance(value, (str, int, float, bool)):
            found[path] = value
        elif isinstance(value, list) and value:
            found[path] = [item for item in value if isinstance(item, (str, int, float, bool))]

    for field in ("limitations", "evidence_gaps"):
        add(field, result.get(field))
    error = result.get("error")
    if isinstance(error, Mapping):
        add("error_code", error.get("code"))
    if result.get("data_state") in {"empty", "undefined", "incomplete", "truncated"}:
        add("data_state", result.get("data_state"))
    sources: list[tuple[str, Mapping[str, Any]]] = [("result", result)]
    period = result.get("applied_time_range")
    if isinstance(period, Mapping):
        sources.append(("period", period))
    for index, row in enumerate(_rows(result)):
        facts = row.get("facts") if isinstance(row.get("facts"), Mapping) else row
        if isinstance(facts, Mapping):
            sources.append((f"rows[{index}].facts", facts))
        states = row.get("states")
        if isinstance(states, Mapping):
            sources.append((f"rows[{index}].states", states))
    for prefix, source in sources:
        for key, value in source.items():
            if any(part in str(key).casefold() for part in _UNKNOWN_PARTS):
                add(f"{prefix}.{key}", value)
    return found


def summarize_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project one result for display/gating without changing the result."""

    value = result if isinstance(result, Mapping) else {}
    rows = _rows(value)
    returned = _integer(value.get("row_count"))
    if returned is None:
        returned = len(rows)
    status = value.get("status") if isinstance(value.get("status"), str) else "missing_result"
    data_state = value.get("data_state")
    truncated = value.get("truncated")
    completeness = evidence._completeness(value)
    observed = _times(value)
    report_complete = (
        status == "success"
        and truncated is False
        and completeness == "complete"
        and not (data_state == "rows" and returned == 0)
    )
    return {
        "request_id": value.get("request_id"),
        "status": status,
        "data_state": data_state,
        "completeness": completeness,
        "truncated": truncated,
        "returned_group_count": returned,
        "population_group_count": _first_field(value, _GROUP_FIELDS),
        "population_row_count": _first_field(value, _ROW_FIELDS),
        "observed_at": next(iter(observed.values()), None),
        "observed_times": observed,
        "unknown_items": _unknown(value),
        "report_complete": report_complete,
    }


def _gate_from_summaries(summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reasons: set[str] = set()
    summaries = list(summaries)
    if not summaries:
        reasons.add("NO_RESULT_EVIDENCE")
    for item in summaries:
        request_id = str(item.get("request_id") or "unknown")
        status = str(item.get("status") or "missing_result")
        if status != "success":
            reasons.add(f"{request_id}:STATUS_{status.upper()}")
        if item.get("truncated") is True:
            reasons.add(f"{request_id}:SOURCE_TRUNCATED")
        elif item.get("truncated") is not False:
            reasons.add(f"{request_id}:TRUNCATION_EVIDENCE_MISSING")
        if item.get("completeness") != "complete":
            reasons.add(f"{request_id}:COMPLETENESS_{str(item.get('completeness')).upper()}")
        if item.get("report_complete") is not True:
            reasons.add(f"{request_id}:REPORT_COMPLETENESS_NOT_PROVEN")
        population = item.get("population_group_count")
        returned = item.get("returned_group_count")
        if isinstance(population, int) and isinstance(returned, int) and returned > population:
            reasons.add(f"{request_id}:RETURNED_GROUPS_EXCEED_POPULATION")
    return {"allowed": not reasons, "reason_codes": sorted(reasons), "items": [dict(item) for item in summaries]}


def report_delivery_gate(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Allow delivery only when every raw result proves complete coverage."""

    return _gate_from_summaries([summarize_result(result) for result in results])


def _fail_gate(reason: str) -> dict[str, Any]:
    gate = _gate_from_summaries([])
    gate["reason_codes"] = [reason]
    gate["allowed"] = False
    return gate


def gate_for_document(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """Gate raw packets by expected IDs; cached coverage is audit-only."""

    value = document if isinstance(document, Mapping) else {}
    packets = value.get("query_packets")
    if not isinstance(packets, list):
        return _fail_gate("RAW_QUERY_EVIDENCE_MISSING")
    expected = value.get("expected_request_ids")
    if (
        not isinstance(expected, list)
        or not expected
        or any(not isinstance(item, str) for item in expected)
        or len(set(expected)) != len(expected)
    ):
        return _fail_gate("REQUEST_ID_SET_MISMATCH")
    expected_ids = list(expected)
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    missing = []
    set_mismatch = expected_ids is not None and len(packets) != len(expected_ids)
    for index, packet in enumerate(packets):
        if not isinstance(packet, Mapping):
            set_mismatch = True
            missing.append(f"packet_{index}")
            continue
        results = packet.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], Mapping):
            set_mismatch = True
            missing.append(f"packet_{index}")
            continue
        result = results[0]
        request_id = result.get("request_id")
        if not isinstance(request_id, str) or (expected_ids is not None and request_id not in expected_ids):
            set_mismatch = True
            missing.append(f"packet_{index}")
            continue
        if request_id in raw_by_id:
            set_mismatch = True
            missing.append(f"packet_{index}")
            continue
        bound = dict(result)
        if packet.get("status") != "success":
            bound["status"] = packet.get("status") if isinstance(packet.get("status"), str) else "missing_packet_status"
        raw_by_id[request_id] = bound
    ordered_ids = expected_ids
    ordered = [raw_by_id.get(request_id, {"request_id": request_id, "status": "missing_result", "data_state": "incomplete"}) for request_id in ordered_ids]
    gate = report_delivery_gate(ordered)
    reasons = set(gate["reason_codes"])
    if set_mismatch or set(raw_by_id) != set(expected_ids):
        reasons.add("REQUEST_ID_SET_MISMATCH")

    cached = value.get("coverage")
    if cached is not None:
        if not isinstance(cached, list) or any(not isinstance(item, Mapping) for item in cached):
            reasons.add("COVERAGE_EVIDENCE_MISSING")
        else:
            cached_by_id: dict[str, Mapping[str, Any]] = {}
            duplicate = False
            for item in cached:
                request_id = item.get("request_id")
                if not isinstance(request_id, str) or request_id in cached_by_id:
                    duplicate = True
                    continue
                cached_by_id[request_id] = item
            if duplicate or set(cached_by_id) != set(expected_ids):
                reasons.add("REQUEST_ID_SET_MISMATCH")
            for raw_result in ordered:
                request_id = raw_result.get("request_id")
                cached_item = cached_by_id.get(request_id)
                if cached_item is None:
                    reasons.add("COVERAGE_RESULT_MISMATCH")
                    continue
                actual = summarize_result(raw_result)
                fields = ("status", "data_state", "truncated", "returned_group_count", "population_group_count", "population_row_count", "observed_at", "unknown_items", "report_complete")
                if any(cached_item.get(field) != actual.get(field) for field in fields):
                    reasons.add("COVERAGE_RESULT_MISMATCH")

    if value.get("status") not in (None, "success"):
        reasons.add(f"DOCUMENT_STATUS_{str(value.get('status')).upper()}")
    if value.get("error") not in (None, {}):
        reasons.add("DOCUMENT_ERROR_PRESENT")
    gate["reason_codes"] = sorted(reasons)
    gate["allowed"] = not reasons
    return gate
