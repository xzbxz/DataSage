#!/usr/bin/env python3
"""Convert a real Hermes/WeCom transcript into a normalized golden candidate.

The SQLite source is opened with ``mode=ro`` and ``PRAGMA query_only=ON``.
Business prompts, tool result rows, and final answer text are never copied into
the output.  Free-text conclusions are never inferred: labels must be supplied
as an explicit reviewer assertion or remain ``unreviewed``.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote


CANDIDATE_SCHEMA = "datasage-golden-expert-candidate/v1"
BINDING_SCHEMA = "datasage-canary-bindings/v1"
LIVE_BINDING_SCHEMA = "datasage-canary-bindings/v2-live-fixture"
RECEIPT_SCHEMA = "datasage-canary-receipt/v1"
PLAN_TRACE_SCHEMA = "datasage-structured-plan-trace/v1"
REVIEW_SCHEMA = "datasage-review-assertion/v2"
WATERMARK_SCHEMA = "datasage-replay-watermark/v1"
LIVE_WATERMARK_SCHEMA = "datasage-replay-watermark/v2-live-fixture"
CONTEXT_FINGERPRINT_SCHEMA = "datasage-context-binding-fingerprint/v1"
MODEL_SOURCE_REFERENCE_SCHEMA = "datasage-query-model-source-reference/v1"
LEGACY_SOURCE_EVIDENCE_SCHEMA = "datasage-query-source-evidence/v1"
LEGACY_SOURCE_EVIDENCE_FIELDS = {
    "schema",
    "identity_sha256",
    "connection_verified",
    "transport_mode",
    "transport_policy_verified",
    "grant_policy",
    "grants_verified",
    "read_only",
    "source_commitment_sha256",
    "security_evidence_sha256",
}
LEGACY_SOURCE_GRANT_POLICIES = {
    "strict_object_read_only",
    "user_accepted_canary_existing_account",
}
LEGACY_SOURCE_SECURITY_DOMAIN = b"datasage-query-source-evidence/v1\x00"
PUBLIC_TOOLS = {
    "datasage_catalog",
    "datasage_entity_resolve",
    "datasage_query",
}
_PERFORMANCE_SCORECARD_FIRST = "performance_scorecard_first"
MAX_JSON_CHARS = 2_000_000
_HOST_TOOL_GUARDRAIL_SUFFIX = re.compile(
    r"\n\n\[(?:Tool loop warning|Tool loop hard stop): "
    r"[a-z][a-z0-9_]*; count=[1-9][0-9]*; [^\r\n\]]+\]\Z"
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(payload).hexdigest()


def _business_database_source_digest(reference: Any) -> str:
    """Resolve only the governed public wrapper or the exact legacy evidence schema."""

    if not isinstance(reference, dict):
        raise ValueError("business database source reference must be an object")
    schema = reference.get("schema")
    if schema == MODEL_SOURCE_REFERENCE_SCHEMA:
        if set(reference) != {"schema", "source_ref_sha256"}:
            raise ValueError("model source reference fields are invalid")
        digest = reference.get("source_ref_sha256")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("model source reference digest is invalid")
        return digest
    if schema == LEGACY_SOURCE_EVIDENCE_SCHEMA:
        is_digest = lambda value: (
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        )
        if (
            set(reference) != LEGACY_SOURCE_EVIDENCE_FIELDS
            or not is_digest(reference.get("identity_sha256"))
            or reference.get("connection_verified") is not True
            or reference.get("transport_mode") not in {"tls", "plaintext"}
            or reference.get("transport_policy_verified") is not True
            or reference.get("grant_policy")
            not in LEGACY_SOURCE_GRANT_POLICIES
            or reference.get("grants_verified") is not True
            or reference.get("read_only") is not True
            or not is_digest(reference.get("source_commitment_sha256"))
            or not is_digest(reference.get("security_evidence_sha256"))
        ):
            raise ValueError("legacy source evidence fields are invalid")
        sealed = {
            key: value
            for key, value in reference.items()
            if key != "security_evidence_sha256"
        }
        expected_seal = hashlib.sha256(
            LEGACY_SOURCE_SECURITY_DOMAIN + _canonical(sealed)
        ).hexdigest()
        if reference["security_evidence_sha256"] != expected_seal:
            raise ValueError("legacy source evidence seal is invalid")
        return _sha256(reference)
    raise ValueError("business database source reference schema is invalid")


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(handle.name).replace(path)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _database_identity(path: Path) -> str:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    connection = _open_read_only(path)
    try:
        schemas = connection.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE name IN ('sessions','messages') ORDER BY name"
        ).fetchall()
        return _sha256(
            {
                "path": str(resolved),
                "file_identity": [stat.st_dev, stat.st_ino],
                "schemas": [[row[0], row[1]] for row in schemas],
            }
        )
    finally:
        connection.close()


def _watermark(
    *,
    test_id: str,
    conversation_id: str,
    turn: int,
    canonical_prompt_sha256: str,
    user_message_id: int,
    database_identity_sha256: str,
    profile: dict[str, str],
    fixture_attestation_sha256: str | None = None,
    business_database_ref_sha256: str | None = None,
) -> str:
    live_values = (
        fixture_attestation_sha256,
        business_database_ref_sha256,
    )
    if any(value is not None for value in live_values) and not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        for value in live_values
    ):
        raise ValueError("live fixture watermark hashes are invalid")
    return _sha256(
        {
            "schema": (
                LIVE_WATERMARK_SCHEMA
                if fixture_attestation_sha256 is not None
                else WATERMARK_SCHEMA
            ),
            "test_id": test_id,
            "conversation_id": conversation_id,
            "turn": turn,
            "canonical_prompt_sha256": canonical_prompt_sha256,
            "user_message_id": user_message_id,
            "database_identity_sha256": database_identity_sha256,
            "artifact_id": profile["artifact_id"],
            "payload_sha256": profile["payload_sha256"],
            **(
                {
                    "fixture_attestation_sha256": fixture_attestation_sha256,
                    "business_database_ref_sha256": business_database_ref_sha256,
                }
                if fixture_attestation_sha256 is not None
                else {}
            ),
        }
    )


def _json_value(raw: Any, label: str) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw or len(raw) > MAX_JSON_CHARS:
        raise ValueError(f"{label} is missing or exceeds the JSON size limit")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        match = _HOST_TOOL_GUARDRAIL_SUFFIX.search(raw)
        if match is not None:
            try:
                return json.loads(raw[: match.start()])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"{label} is not valid JSON") from exc


def _ordered_add(target: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in target:
        target.append(value)


def _scalars(value: Any) -> list[Any]:
    values = value if isinstance(value, list) else [value]
    return [item for item in values if item is None or isinstance(item, (str, int, float, bool))]


def _typed_fingerprint(value: Any) -> dict[str, str]:
    return {"schema": CONTEXT_FINGERPRINT_SCHEMA, "sha256": _sha256(value)}


def _is_typed_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"schema", "sha256"}
        and value.get("schema") == CONTEXT_FINGERPRINT_SCHEMA
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
    )


def _filter_fingerprints(filters: Any, *, typed: bool) -> dict[str, Any]:
    if not isinstance(filters, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for key, value in sorted(filters.items()):
        if isinstance(key, str) and _scalars(value) == (value if isinstance(value, list) else [value]):
            result[key] = _typed_fingerprint(value) if typed else _sha256(value)
    return result


def _time_token(request: dict[str, Any]) -> str:
    if isinstance(request.get("calendar_month"), str):
        current = f"calendar_month:{request['calendar_month']}"
    elif isinstance(request.get("time_range"), dict):
        period = request["time_range"]
        current = f"range:{period.get('start')}/{period.get('end')}"
    else:
        current = "governed_default"
    comparison = request.get("comparison")
    if isinstance(comparison, dict) and isinstance(comparison.get("kind"), str):
        suffix = comparison["kind"]
        if comparison.get("months") is not None:
            suffix += f":{comparison['months']}"
        current += f"|comparison:{suffix}"
    return current


def _composite_time_token(
    requests: list[dict[str, Any]],
    catalog_time_policies: dict[tuple[str, str], str],
    *,
    expected_observed_on: date | None = None,
) -> str | None:
    flow_years: set[str] = set()
    has_separate_snapshot = False
    for request in requests:
        domain = request.get("domain")
        metric = request.get("metric")
        if not isinstance(domain, str) or not isinstance(metric, str):
            continue
        policy = catalog_time_policies.get((domain, metric))
        period = request.get("time_range")
        if policy == "current_month" and isinstance(period, dict):
            start, end = period.get("start"), period.get("end")
            try:
                start_date = date.fromisoformat(start) if isinstance(start, str) else None
                end_date = date.fromisoformat(end) if isinstance(end, str) else None
            except ValueError:
                start_date = end_date = None
            if (
                start_date is not None
                and end_date is not None
                and (start_date.month, start_date.day) == (1, 1)
                and end_date.year == start_date.year
                and end_date > start_date
                and (
                    expected_observed_on is None
                    or (
                        start_date.year == expected_observed_on.year
                        and end_date
                        == date(
                            expected_observed_on.year
                            + (1 if expected_observed_on.month == 12 else 0),
                            1 if expected_observed_on.month == 12 else expected_observed_on.month + 1,
                            1,
                        )
                    )
                )
            ):
                flow_years.add(str(start_date.year))
        elif (
            policy == "current_snapshot"
            and "calendar_month" not in request
            and "time_range" not in request
        ):
            has_separate_snapshot = True
    if len(flow_years) == 1 and has_separate_snapshot:
        return f"{next(iter(flow_years))}_ytd_flows_with_separate_snapshots"
    return None


def _is_canonical_entity_ambiguity(
    args: dict[str, Any], payload: dict[str, Any]
) -> bool:
    token = args.get("token")
    if not isinstance(token, str) or not token or len(token) > 128:
        return False
    domain = args.get("domain")
    metric = args.get("metric")
    entity_types = args.get("entity_types")
    has_bound_metric = isinstance(domain, str) and bool(domain) and isinstance(metric, str) and bool(metric)
    has_explicit_types = (
        isinstance(entity_types, list)
        and bool(entity_types)
        and len(entity_types) == len(set(entity_types))
        and all(isinstance(item, str) and item for item in entity_types)
    )
    if not has_bound_metric and not has_explicit_types:
        return False
    candidates = payload.get("candidates")
    candidate_count = payload.get("candidate_count")
    resolution_path = payload.get("resolution_path")
    lower_bound = payload.get("candidate_count_is_lower_bound")
    truncated = payload.get("truncated")
    if (
        payload.get("status") != "ambiguous"
        or resolution_path
        not in {
            "registered_exact",
            "registered_and_master_exact",
            "master_candidate_query",
            "master_exact_short_token",
        }
        or payload.get("token") != token
        or payload.get("must_clarify") is not True
        or payload.get("must_stop_business_query") is not True
        or not isinstance(candidates, list)
        or not candidates
        or type(candidate_count) is not int
        or candidate_count < len(candidates)
        or candidate_count < 2
        or type(lower_bound) is not bool
        or type(truncated) is not bool
        or (candidate_count != len(candidates) and truncated is not True)
        or (lower_bound is True and truncated is not True)
    ):
        return False
    allowed_types = set(entity_types) if has_explicit_types else None
    identities: list[bytes] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        entity_type = candidate.get("entity_type")
        display_name = candidate.get("display_name")
        filter_values = candidate.get("filter_values")
        normalized_token = " ".join(token.strip().casefold().split())
        if (
            not isinstance(entity_type, str)
            or not entity_type
            or (allowed_types is not None and entity_type not in allowed_types)
            or not isinstance(display_name, str)
            or not display_name
            or not isinstance(filter_values, list)
            or not filter_values
            or any(not isinstance(value, str) or not value for value in filter_values)
            or candidate.get("match_kind")
            not in {"registered_exact", "exact", "prefix", "contains"}
            or candidate.get("confidence") not in {"exact", "candidate"}
            or (
                not (
                    isinstance(candidate.get("canonical_id"), str)
                    and bool(candidate["canonical_id"])
                )
                and not (
                    isinstance(candidate.get("canonical_code"), str)
                    and bool(candidate["canonical_code"])
                )
                and candidate.get("match_kind") != "registered_exact"
            )
            or (
                has_bound_metric
                and not (
                    isinstance(candidate.get("filter_role"), str)
                    and re.fullmatch(
                        r"[a-z][a-z0-9_]{0,63}", candidate["filter_role"]
                    )
                    is not None
                )
                and not (
                    isinstance(candidate.get("filter_role_candidates"), list)
                    and bool(candidate["filter_role_candidates"])
                    and all(
                        isinstance(role, str)
                        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role)
                        is not None
                        for role in candidate["filter_role_candidates"]
                    )
                )
            )
            or (
                candidate.get("match_kind") != "registered_exact"
                and not any(
                    normalized_token in " ".join(value.casefold().split())
                    or " ".join(value.casefold().split()) in normalized_token
                    for value in (
                        display_name,
                        *filter_values,
                        *(
                            [candidate["canonical_id"]]
                            if isinstance(candidate.get("canonical_id"), str)
                            and candidate["canonical_id"]
                            else []
                        ),
                        *(
                            [candidate["canonical_code"]]
                            if isinstance(candidate.get("canonical_code"), str)
                            and candidate["canonical_code"]
                            else []
                        ),
                    )
                )
            )
        ):
            return False
        identities.append(
            _canonical(
                {
                    "entity_type": entity_type,
                    "canonical_id": candidate.get("canonical_id"),
                    "canonical_code": candidate.get("canonical_code"),
                    "display_name": display_name,
                    "filter_values": filter_values,
                }
            )
        )
    return len(identities) == len(set(identities))


def _applied_time_matches_live_request(
    value: Any,
    request: dict[str, Any],
    expected_observed_on: date,
) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    requested = request.get("time_range")
    candidates = [value]
    current = value.get("current")
    if isinstance(current, dict):
        candidates.append(current)
    if isinstance(requested, dict):
        matching = [
            candidate
            for candidate in candidates
            if candidate.get("start") == requested.get("start")
            and candidate.get("end") == requested.get("end")
            and isinstance(candidate.get("source"), str)
            and bool(candidate["source"])
        ]
        if not matching:
            return False
        calendar = matching[0].get("calendar_evidence")
        try:
            start_date = date.fromisoformat(str(requested.get("start")))
            end_date = date.fromisoformat(str(requested.get("end")))
        except ValueError:
            return False
        expected_period_state = (
            "completed"
            if end_date <= expected_observed_on
            else "not_started"
            if start_date > expected_observed_on
            else "in_progress"
        )
        return (
            start_date < end_date
            and isinstance(calendar, dict)
            and calendar.get("version") == "calendar-period-evidence/v2"
            and calendar.get("observation_basis")
            == "business_clock_query_observation"
            and calendar.get("observed_on") == expected_observed_on.isoformat()
            and calendar.get("period_state") == expected_period_state
            and calendar.get("source_freshness") == "not_proven"
        )
    source = value.get("source")
    if source == "current_snapshot":
        try:
            as_of_date = date.fromisoformat(str(value.get("as_of_date")))
        except ValueError:
            return False
        return (
            as_of_date == expected_observed_on
            and value.get("resolution_state") == "resolved"
        )
    if source in {"latest_snapshot", "latest_non_null_snapshot", "latest_snapshot_offset"}:
        snapshot_month = value.get("snapshot_month")
        try:
            snapshot_date = date.fromisoformat(f"{snapshot_month}-01")
        except ValueError:
            return False
        return (
            snapshot_date <= expected_observed_on.replace(day=1)
            and value.get("resolution_state") == "resolved"
        )
    return False


def _has_finite_business_fact(facts: dict[str, Any]) -> bool:
    for value in facts.values():
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if number.is_finite():
            return True
    return False


def _has_substantive_live_query_evidence(
    result: dict[str, Any],
    *,
    request: dict[str, Any] | None,
    expected_observed_on: date | None,
    wire_version: Any,
) -> bool:
    if result.get("status") != "success":
        return False
    row_count = result.get("row_count")
    expected_metric_ref = (
        "metric_"
        + hashlib.sha256(
            f"{request.get('domain')}\0{request.get('metric')}".encode("utf-8")
        ).hexdigest()[:16]
        if isinstance(request, dict)
        else None
    )
    if (
        wire_version != "datasage-query-model-wire/v3"
        or not isinstance(request, dict)
        or expected_observed_on is None
        or type(row_count) is not int
        or row_count <= 0
        or result.get("business_metric_ref") != expected_metric_ref
        or not isinstance(result.get("business_metric_label"), str)
        or not result["business_metric_label"]
        or result.get("data_state")
        not in {"complete", "rows", "zero", "truncated", "incomplete"}
        or type(result.get("truncated")) is not bool
        or not _applied_time_matches_live_request(
            result.get("applied_time_range"), request, expected_observed_on
        )
        or result.get("error") is not None
    ):
        return False
    rows = result.get("rows")
    required_row_keys = {
        "claim_id",
        "dimensions",
        "facts",
        "states",
        "allowed_relations",
        "unit",
        "currency",
    }
    return (
        isinstance(rows, list)
        and len(rows) == row_count
        and all(
            isinstance(row, dict)
            and set(row) == required_row_keys
            and isinstance(row.get("claim_id"), str)
            and re.fullmatch(r"claim_[0-9a-f]{20}", row["claim_id"]) is not None
            and isinstance(row.get("dimensions"), list)
            and all(
                isinstance(dimension, dict)
                and set(dimension) == {"label", "value"}
                and isinstance(dimension.get("label"), str)
                and bool(dimension["label"])
                and isinstance(dimension.get("value"), str)
                and bool(dimension["value"])
                for dimension in row["dimensions"]
            )
            and isinstance(row.get("facts"), dict)
            and bool(row["facts"])
            and all(
                isinstance(key, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key) is not None
                for key in row["facts"]
            )
            and _has_finite_business_fact(row["facts"])
            and isinstance(row.get("states"), dict)
            and all(
                isinstance(key, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key) is not None
                and isinstance(value, str)
                and bool(value)
                for key, value in row["states"].items()
            )
            and isinstance(row.get("allowed_relations"), list)
            and "observation" in row["allowed_relations"]
            and all(
                isinstance(relation, str) and bool(relation)
                for relation in row["allowed_relations"]
            )
            and (
                row.get("unit") is None
                or (isinstance(row.get("unit"), str) and bool(row["unit"]))
            )
            and (
                row.get("currency") is None
                or (
                    isinstance(row.get("currency"), str)
                    and bool(row["currency"])
                )
            )
            for row in rows
        )
    )


def _extract_calls(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    result_rows: dict[str, sqlite3.Row] = {}
    for row in rows:
        if row["role"] == "tool" and row["tool_call_id"]:
            call_id = str(row["tool_call_id"])
            if call_id in result_rows:
                raise ValueError(f"duplicate tool result for call {call_id}")
            result_rows[call_id] = row

    calls: list[dict[str, Any]] = []
    used_results: set[str] = set()
    for row in rows:
        if row["role"] != "assistant" or not row["tool_calls"]:
            continue
        parsed = _json_value(row["tool_calls"], f"message {row['id']} tool_calls")
        if not isinstance(parsed, list):
            raise ValueError(f"message {row['id']} tool_calls must be a list")
        for position, item in enumerate(parsed):
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                raise ValueError(f"message {row['id']} tool call {position} is invalid")
            function = item["function"]
            name = function.get("name")
            raw_arguments = function.get("arguments")
            if name == "tool_call":
                dispatched = _json_value(
                    raw_arguments,
                    f"message {row['id']} dispatcher tool call {position} arguments",
                )
                if not isinstance(dispatched, dict):
                    raise ValueError(
                        f"message {row['id']} dispatcher tool call {position} "
                        "arguments must be an object"
                    )
                name = dispatched.get("name")
                raw_arguments = dispatched.get("arguments")
            if name not in PUBLIC_TOOLS:
                continue
            call_id = item.get("id") or item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(f"message {row['id']} DataSage call has no ID")
            tool_row = result_rows.get(call_id)
            if tool_row is None:
                raise ValueError(f"DataSage call {call_id} has no persisted tool result")
            if tool_row["tool_name"] != name:
                raise ValueError(f"DataSage call {call_id} result tool name mismatch")
            if call_id in used_results:
                raise ValueError(f"duplicate DataSage tool call declaration for {call_id}")
            used_results.add(call_id)
            arguments = _json_value(raw_arguments, f"call {call_id} arguments")
            payload = _json_value(tool_row["content"], f"call {call_id} result")
            if not isinstance(arguments, dict) or not isinstance(payload, dict):
                raise ValueError(f"DataSage call {call_id} arguments/result must be objects")
            calls.append(
                {
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "result": payload,
                    "assistant_message_id": row["id"],
                    "tool_message_id": tool_row["id"],
                }
            )
    orphaned = sorted(
        call_id
        for call_id, row in result_rows.items()
        if row["tool_name"] in PUBLIC_TOOLS and call_id not in used_results
    )
    if orphaned:
        raise ValueError(f"orphaned DataSage tool results: {orphaned!r}")
    return calls


def _result_error_codes(payload: dict[str, Any]) -> list[str]:
    codes: list[str] = []

    def add_error(container: Any) -> None:
        if isinstance(container, dict):
            code = container.get("code")
            if isinstance(code, str):
                _ordered_add(codes, code)

    add_error(payload.get("error"))
    for item in payload.get("results") or []:
        if isinstance(item, dict):
            add_error(item.get("error"))
    bundle = payload.get("evidence_bundle")
    if isinstance(bundle, dict):
        for gap in bundle.get("evidence_gaps") or []:
            if isinstance(gap, dict):
                reason = gap.get("reason")
                if isinstance(reason, str) and reason.isupper():
                    _ordered_add(codes, reason)
    return codes


def _is_bound_metric_detail_catalog_call(
    args: dict[str, Any], payload: dict[str, Any]
) -> bool:
    requests = args.get("requests")
    results = payload.get("results")
    content_hash = payload.get("content_hash")
    model_wire_version = payload.get("model_wire_version")
    raw_keys = {
        "status",
        "catalog_version",
        "contract_role",
        "query_policy",
        "results",
        "content_hash",
    }
    compact_keys = raw_keys | {"model_wire_version"}
    if "dimension_value_policies" in payload:
        compact_keys.add("dimension_value_policies")
    common_invalid = (
        payload.get("status") != "success"
        or payload.get("catalog_version") != "datasage-metric-catalog/v1"
        or payload.get("contract_role") != "governed_metric_catalog"
        or not isinstance(payload.get("query_policy"), dict)
        or not isinstance(content_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
        or not isinstance(requests, list)
        or len(requests) != 1
        or not isinstance(requests[0], dict)
        or set(requests[0]) != {"domain", "metric"}
        or not isinstance(results, list)
        or len(results) != 1
        or not isinstance(results[0], dict)
    )
    if common_invalid:
        return False
    if model_wire_version is None:
        if (
            set(payload) != raw_keys
            or content_hash
            != _sha256(
                {key: value for key, value in payload.items() if key != "content_hash"}
            )
        ):
            return False
    elif model_wire_version in {
        "datasage-catalog-model-wire/v2",
        "datasage-catalog-model-wire/v3",
    }:
        if set(payload) != compact_keys:
            return False
        detail_receipt = results[0].get("detail_receipt")
        if (
            not isinstance(detail_receipt, str)
            or re.fullmatch(r"[0-9a-f]{64}", detail_receipt) is None
        ):
            return False
    else:
        return False
    request = requests[0]
    result = results[0]
    domain = request.get("domain")
    metric = request.get("metric")
    projected_metric = result.get("metric")
    return (
        isinstance(domain, str)
        and bool(domain)
        and isinstance(metric, str)
        and bool(metric)
        and result.get("level") == "metric"
        and result.get("domain") == domain
        and isinstance(projected_metric, dict)
        and projected_metric.get("code") == metric
    )


def _is_reconciled(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.lower() in {"reconciled", "complete", "proven"}
    if isinstance(value, dict):
        return any(
            _is_reconciled(value.get(key))
            for key in ("status", "state", "reconciliation", "reconciled")
            if key in value
        )
    return False


def _normalize(
    calls: list[dict[str, Any]],
    *,
    expected_business_database_ref_sha256: str | None = None,
    typed_context_bindings: bool = False,
    expected_observed_on: date | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    domains: list[str] = []
    metrics: list[str] = []
    dimensions: list[str] = []
    operations: list[str] = []
    time_tokens: list[str] = []
    filter_fingerprints: dict[str, str] = {}
    receipts: list[str] = []
    error_codes: list[str] = []
    requested_ids: list[str] = []
    result_ids: list[str] = []
    successful_ids: set[str] = set()
    substantive_successful_ids: set[str] = set()
    failed_ids: set[str] = set()
    coverage_ids: set[str] = set()
    request_time_tokens: dict[str, str] = {}
    request_by_id: dict[str, dict[str, Any]] = {}
    catalog_time_policies: dict[tuple[str, str], str] = {}
    decomposition_ids: set[str] = set()
    reconciled_ids: set[str] = set()
    decomposition_overall_ids: dict[str, str] = {}
    target_gap_ids: set[str] = set()
    target_gap_reconciled_ids: set[str] = set()
    target_gap_overall_ids: dict[str, str] = {}
    authorized_internal_ids: set[str] = set()
    truncated_ids: set[str] = set()
    request_result_integrity = True
    truncated = False
    decomposition_requested = False
    reconciled = False
    query_attempted = False

    for call_index, call in enumerate(calls):
        name = call["name"]
        args = call["arguments"]
        payload = call["result"]
        if name == "datasage_catalog" and payload.get("status") == "success":
            requests = args.get("requests") or []
            results = payload.get("results") or []
            if (
                isinstance(requests, list)
                and requests
                and all(isinstance(request, dict) and request for request in requests)
                and isinstance(results, list)
                and results
                and all(isinstance(result, dict) and result for result in results)
            ):
                _ordered_add(receipts, "catalog")
            if (
                call_index == 0
                and len(requests) == 1
                and isinstance(requests[0], dict)
                and set(requests[0]) == {"view"}
                and requests[0].get("view") == "performance_scorecard"
                and len(results) == 1
                and isinstance(results[0], dict)
                and results[0].get("level") == "performance_scorecard"
            ):
                _ordered_add(operations, _PERFORMANCE_SCORECARD_FIRST)
            if _is_bound_metric_detail_catalog_call(args, payload):
                _ordered_add(receipts, "metric_detail")
                request = requests[0]
                metric = results[0]["metric"]
                time_policy = metric.get("time_policy")
                if isinstance(time_policy, str):
                    catalog_time_policies[(request["domain"], request["metric"])] = time_policy
            for request in requests:
                if isinstance(request, dict):
                    _ordered_add(domains, request.get("domain"))
                    _ordered_add(metrics, request.get("metric"))
        elif name == "datasage_entity_resolve":
            if (
                isinstance(args.get("token"), str)
                and bool(args["token"])
                and payload.get("status") in {"resolved", "ambiguous", "not_found"}
                and isinstance(payload.get("resolution_path"), str)
                and bool(payload["resolution_path"])
            ):
                _ordered_add(receipts, "entity_resolution")
            _ordered_add(domains, args.get("domain"))
            _ordered_add(metrics, args.get("metric"))
            for entity_type in args.get("entity_types") or []:
                _ordered_add(dimensions, entity_type)
            for candidate in payload.get("candidates") or []:
                if isinstance(candidate, dict):
                    _ordered_add(dimensions, candidate.get("entity_type"))
            _ordered_add(operations, "entity_preflight")
            if _is_canonical_entity_ambiguity(args, payload):
                _ordered_add(error_codes, "ENTITY_AMBIGUOUS")
                _ordered_add(operations, "clarify_entity_mapping")
        elif name == "datasage_query":
            if expected_business_database_ref_sha256 is not None:
                reference = payload.get("source_evidence_ref")
                try:
                    source_digest = _business_database_source_digest(reference)
                except ValueError as exc:
                    raise ValueError(
                        "live fixture business database source binding changed"
                    ) from exc
                if source_digest != expected_business_database_ref_sha256:
                    raise ValueError(
                        "live fixture business database source binding changed"
                    )
            query_attempted = True
            requests = args.get("requests") or []
            if len(requests) > 1:
                _ordered_add(operations, "parallel_evidence")
            for request in requests:
                if not isinstance(request, dict):
                    continue
                _ordered_add(domains, request.get("domain"))
                _ordered_add(metrics, request.get("metric"))
                for dimension in request.get("dimensions") or []:
                    _ordered_add(dimensions, dimension)
                for key, digest in _filter_fingerprints(
                    request.get("metric_filters"), typed=typed_context_bindings
                ).items():
                    filter_fingerprints[key] = digest
                request_id = request.get("request_id")
                if isinstance(request_id, str) and request_id:
                    request_time_tokens[request_id] = _time_token(request)
                    request_by_id[request_id] = request
                comparison = request.get("comparison")
                if isinstance(comparison, dict):
                    _ordered_add(operations, comparison.get("kind"))
                if request.get("complete_change_decomposition") is not None:
                    decomposition_requested = True
                    _ordered_add(operations, "complete_change_decomposition")
                    decomposition = request.get("complete_change_decomposition")
                    if isinstance(decomposition, dict):
                        _ordered_add(dimensions, decomposition.get("dimension"))
                if request.get("complete_target_gap_decomposition") is not None:
                    target_gap = request.get("complete_target_gap_decomposition")
                    _ordered_add(operations, "complete_target_gap_decomposition")
                    _ordered_add(operations, "target_actual_gap")
                    if isinstance(target_gap, dict):
                        _ordered_add(dimensions, target_gap.get("dimension"))
                if request.get("time_bucket") == "month":
                    _ordered_add(operations, "monthly_trend")
                if "currency" in (request.get("dimensions") or []):
                    _ordered_add(operations, "currency_partition")
                order_by = request.get("order_by")
                if isinstance(order_by, dict) and isinstance(request.get("limit"), int):
                    _ordered_add(
                        operations,
                        "top_n" if order_by.get("direction") == "desc" else "bottom_n",
                    )
            call_request_ids = [
                request.get("request_id")
                for request in requests
                if isinstance(request, dict)
                and isinstance(request.get("request_id"), str)
                and request["request_id"]
            ]
            if len(call_request_ids) != len(requests) or len(call_request_ids) != len(set(call_request_ids)):
                request_result_integrity = False
            requested_ids.extend(call_request_ids)
            decomposition_ids.update({
                request["request_id"]
                for request in requests
                if isinstance(request, dict)
                and isinstance(request.get("request_id"), str)
                and request.get("complete_change_decomposition") is not None
            })
            target_gap_ids.update({
                request["request_id"]
                for request in requests
                if isinstance(request, dict)
                and isinstance(request.get("request_id"), str)
                and request.get("complete_target_gap_decomposition") is not None
            })
            bundle = payload.get("evidence_bundle")
            if isinstance(bundle, dict):
                if payload.get("model_wire_version") in {
                    "datasage-query-model-wire/v2",
                    "datasage-query-model-wire/v3",
                }:
                    items = bundle.get("items")
                    if isinstance(items, list):
                        for item in items:
                            if (
                                isinstance(item, dict)
                                and item.get("status") == "success"
                                and isinstance(item.get("request_id"), str)
                                and item["request_id"]
                            ):
                                coverage_ids.add(item["request_id"])
                else:
                    coverage = bundle.get("coverage_receipts")
                    items = coverage.get("items") if isinstance(coverage, dict) else None
                    for item in items if isinstance(items, list) else []:
                        if not isinstance(item, dict):
                            continue
                        for request_id in item.get("request_ids") or []:
                            if isinstance(request_id, str) and request_id:
                                coverage_ids.add(request_id)
            results = payload.get("results")
            if (
                isinstance(requests, list)
                and requests
                and all(isinstance(request, dict) and request for request in requests)
                and isinstance(results, list)
                and results
                and all(isinstance(result, dict) and result for result in results)
            ):
                _ordered_add(receipts, "query")
            if isinstance(results, list):
                for result in results:
                    if not isinstance(result, dict):
                        request_result_integrity = False
                        continue
                    request_id = result.get("request_id")
                    if not isinstance(request_id, str) or not request_id:
                        request_result_integrity = False
                        continue
                    result_ids.append(request_id)
                    if result.get("status") == "success":
                        successful_ids.add(request_id)
                        if (
                            expected_business_database_ref_sha256 is None
                            or _has_substantive_live_query_evidence(
                                result,
                                request=request_by_id.get(request_id),
                                expected_observed_on=expected_observed_on,
                                wire_version=payload.get("model_wire_version"),
                            )
                        ):
                            substantive_successful_ids.add(request_id)
                    else:
                        failed_ids.add(request_id)
                    truncated = truncated or result.get("truncated") is True
                    if result.get("truncated") is True:
                        truncated_ids.add(request_id)
                    for claim in result.get("claim_ledger") or []:
                        if isinstance(claim, dict):
                            truncated = truncated or claim.get("source_truncated") is True
                            if claim.get("source_truncated") is True:
                                truncated_ids.add(request_id)
                    change_reconciliation = result.get("change_reconciliation")
                    if _is_reconciled(change_reconciliation):
                        reconciled_ids.add(request_id)
                    if (
                        request_id in decomposition_ids
                        and isinstance(change_reconciliation, dict)
                        and change_reconciliation.get("operation")
                        == "complete_change_decomposition"
                        and isinstance(change_reconciliation.get("overall_request_id"), str)
                        and change_reconciliation["overall_request_id"]
                    ):
                        overall_id = change_reconciliation["overall_request_id"]
                        decomposition_overall_ids[request_id] = overall_id
                        authorized_internal_ids.add(overall_id)
                    target_gap = result.get("target_gap_reconciliation")
                    if (
                        request_id in target_gap_ids
                        and isinstance(target_gap, dict)
                        and target_gap.get("operation")
                        == "complete_target_gap_decomposition"
                        and isinstance(target_gap.get("overall_request_id"), str)
                        and target_gap["overall_request_id"]
                    ):
                        overall_id = target_gap["overall_request_id"]
                        target_gap_overall_ids[request_id] = overall_id
                        authorized_internal_ids.add(overall_id)
                    if (
                        result.get("status") == "success"
                        and isinstance(target_gap, dict)
                        and target_gap.get("status") == "reconciled"
                        and target_gap.get("operation")
                        == "complete_target_gap_decomposition"
                    ):
                        target_gap_reconciled_ids.add(request_id)
            else:
                request_result_integrity = False
        for code in _result_error_codes(payload):
            _ordered_add(error_codes, code)

    requested_set = set(requested_ids)
    expected_result_set = requested_set | authorized_internal_ids
    result_set = set(result_ids)
    if (
        not request_result_integrity
        or len(requested_ids) != len(requested_set)
        or len(result_ids) != len(result_set)
        or expected_result_set != result_set
        or successful_ids.intersection(failed_ids)
        or successful_ids.union(failed_ids) != result_set
        or coverage_ids != successful_ids
    ):
        request_result_integrity = False
        _ordered_add(error_codes, "TRANSCRIPT_REQUEST_RESULT_MISMATCH")
    if request_result_integrity:
        for request_id in requested_ids:
            if request_id in substantive_successful_ids and request_id in coverage_ids:
                _ordered_add(time_tokens, request_time_tokens.get(request_id))
    successful_queries = len(substantive_successful_ids)
    failed_queries = len(failed_ids | (expected_result_set - result_set))
    if expected_result_set and request_result_integrity:
        _ordered_add(receipts, "coverage")
    decomposition_all_reconciled = bool(decomposition_ids) and (
        request_result_integrity
        and decomposition_ids.issubset(successful_ids)
        and set(decomposition_overall_ids) == decomposition_ids
        and set(decomposition_overall_ids.values()).issubset(successful_ids)
        and reconciled_ids == decomposition_ids
        and not (
            decomposition_ids | set(decomposition_overall_ids.values())
        ).intersection(truncated_ids)
    )
    reconciled = decomposition_all_reconciled
    if decomposition_requested and decomposition_all_reconciled:
        _ordered_add(receipts, "decomposition")
    target_gap_all_reconciled = bool(target_gap_ids) and (
        request_result_integrity
        and target_gap_ids.issubset(successful_ids)
        and set(target_gap_overall_ids) == target_gap_ids
        and set(target_gap_overall_ids.values()).issubset(successful_ids)
        and target_gap_reconciled_ids == target_gap_ids
        and not (
            target_gap_ids | set(target_gap_overall_ids.values())
        ).intersection(truncated_ids)
    )
    if target_gap_all_reconciled:
        _ordered_add(receipts, "target_gap_decomposition")
    if failed_queries or error_codes:
        _ordered_add(receipts, "partial_failure")
    if "DATA_ENTITLEMENT_DENIED" in error_codes:
        _ordered_add(receipts, "entitlement")
        if not query_attempted:
            _ordered_add(operations, "deny_before_data_access")
    composite_time = _composite_time_token(
        [
            request_by_id[request_id]
            for request_id in requested_ids
            if request_id in request_by_id
            and request_id in substantive_successful_ids
            and request_id in coverage_ids
        ]
        if request_result_integrity
        else [],
        catalog_time_policies,
        expected_observed_on=expected_observed_on,
    )
    time_semantics = composite_time or (
        time_tokens[0]
        if len(time_tokens) == 1
        else "multi:" + _sha256(time_tokens)
        if time_tokens
        else "no_query"
    )
    return (
        {
            "domains": domains,
            "metrics": metrics,
            "dimensions": dimensions,
            "operations": operations,
            "time_semantics": time_semantics,
            "context_action": "new",
            "context_bindings": {"filter_fingerprints": filter_fingerprints},
        },
        {
            "receipts": receipts,
            "successful_queries": successful_queries,
            "failed_queries": failed_queries,
            "truncated": truncated,
            "reconciled": reconciled,
            "query_attempted": query_attempted,
            "error_codes": error_codes,
        },
    )


def _is_unconfirmed_entity_ambiguity(
    plan: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    return (
        "entity_resolution" in evidence["receipts"]
        and "ENTITY_AMBIGUOUS" in evidence["error_codes"]
        and "clarify_entity_mapping" in plan["operations"]
        and evidence["query_attempted"] is False
    )


def _plan_trace(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    required = {
        "schema", "domains", "metrics", "dimensions", "operations",
        "time_semantics", "context_action", "context_bindings",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("plan_trace has invalid keys")
    if value.get("schema") != PLAN_TRACE_SCHEMA:
        raise ValueError(f"plan_trace schema must be {PLAN_TRACE_SCHEMA}")
    for field in ("domains", "metrics", "dimensions", "operations"):
        items = value.get(field)
        if (
            not isinstance(items, list)
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 128
                or re.fullmatch(r"[A-Za-z0-9_.:@/-]+", item) is None
                for item in items
            )
            or len(items) != len(set(items))
        ):
            raise ValueError(f"plan_trace.{field} must contain unique strings")
    if (
        not isinstance(value.get("time_semantics"), str)
        or not value["time_semantics"]
        or len(value["time_semantics"]) > 160
        or re.fullmatch(r"[A-Za-z0-9_.:@|/-]+", value["time_semantics"]) is None
    ):
        raise ValueError("plan_trace.time_semantics is invalid")
    if value.get("context_action") not in {"new", "preserve", "replace", "reset"}:
        raise ValueError("plan_trace.context_action is invalid")
    def controlled_scalar(item: Any) -> bool:
        if item is None or isinstance(item, (int, float, bool)):
            return True
        return (
            isinstance(item, str)
            and 0 < len(item) <= 128
            and re.fullmatch(r"[A-Za-z0-9_.:@/-]+", item) is not None
        )

    bindings = value.get("context_bindings")
    if not isinstance(bindings, dict) or any(
        not isinstance(key, str)
        or not key
        or not (
            controlled_scalar(item)
            or _is_typed_fingerprint(item)
            or (
                key == "filter_fingerprints"
                and isinstance(item, dict)
                and all(
                    isinstance(name, str) and name and _is_typed_fingerprint(digest)
                    for name, digest in item.items()
                )
            )
            or (
                isinstance(item, list)
                and all(controlled_scalar(value) for value in item)
            )
        )
        for key, item in bindings.items()
    ):
        raise ValueError("plan_trace.context_bindings is invalid")
    return {key: value[key] for key in required if key != "schema"}


def _validate_performance_scorecard_trace(
    persisted_plan: dict[str, Any],
    review_trace: dict[str, Any],
) -> None:
    persisted = _PERFORMANCE_SCORECARD_FIRST in persisted_plan["operations"]
    reviewed = _PERFORMANCE_SCORECARD_FIRST in review_trace["operations"]
    if persisted != reviewed:
        raise ValueError(
            "review plan_trace.operations contradicts persisted scorecard call"
        )


def _validate_live_context_bindings(bindings: Any) -> None:
    if not isinstance(bindings, dict):
        raise ValueError("live fixture context_bindings must be an object")
    for key, value in bindings.items():
        if key == "limit" and isinstance(value, int) and value > 0:
            continue
        if key == "filter_fingerprints" and isinstance(value, dict):
            if all(
                isinstance(name, str) and name and _is_typed_fingerprint(item)
                for name, item in value.items()
            ):
                continue
        if _is_typed_fingerprint(value):
            continue
        raise ValueError(
            f"live fixture context binding {key!r} is not a typed fingerprint"
        )


def _validate_profile_binding(value: Any) -> dict[str, str]:
    required = {"profile_id", "artifact_id", "payload_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("profile_artifact must contain profile_id, artifact_id, payload_sha256")
    if any(not isinstance(value[key], str) or not value[key] for key in required):
        raise ValueError("profile_artifact fields must be non-empty strings")
    digest = value["payload_sha256"]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("profile_artifact payload_sha256 must be lowercase SHA-256")
    return dict(value)


def _review(
    value: Any,
    *,
    expected: dict[str, Any],
) -> tuple[list[str], dict[str, Any], dict[str, Any] | None]:
    if value is None or value == {"status": "unreviewed"}:
        return [], {"status": "unreviewed", "assertion_sha256": None}, None
    required = {
        "schema", "status", "test_id", "artifact_id", "payload_sha256",
        "session_id", "user_message_id", "canonical_prompt_sha256",
        "database_identity_sha256", "watermark_sha256",
        "final_answer_sha256", "reviewer_id", "labels",
    }
    live_fields = {
        "fixture_attestation_sha256",
        "business_database_ref_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) - (required | live_fields | {"plan_trace"})
        or not required.issubset(value)
        or any(field in expected and field not in value for field in live_fields)
    ):
        raise ValueError("review must be unreviewed or a v2 bound reviewer assertion")
    labels = value.get("labels")
    if (
        value.get("schema") != REVIEW_SCHEMA
        or value.get("status") != "reviewed"
        or not isinstance(labels, list)
        or any(
            not isinstance(label, str)
            or not label
            or len(label) > 128
            or re.fullmatch(r"[A-Za-z0-9_.:@/-]+", label) is None
            for label in labels
        )
        or len(labels) != len(set(labels))
        or not isinstance(value.get("reviewer_id"), str)
        or not value["reviewer_id"]
    ):
        raise ValueError("reviewed labels/reviewer ID are invalid")
    for field, expected_value in expected.items():
        if value.get(field) != expected_value:
            raise ValueError(f"review assertion {field} does not match transcript binding")
    trace = _plan_trace(value.get("plan_trace"))
    binding = {field: value[field] for field in expected}
    return (
        list(labels),
        {
            "status": "reviewed",
            "reviewer_id_sha256": _sha256(value["reviewer_id"]),
            "assertion_sha256": _sha256(value),
            "binding_sha256": _sha256(binding),
            "plan_trace_sha256": _sha256(value["plan_trace"]) if trace else None,
        },
        trace,
    )


def _open_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True).as_posix()
    uri = f"file:{quote(resolved, safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("SQLite query_only could not be enabled")
    return connection


def _turn_rows(
    connection: sqlite3.Connection,
    binding: dict[str, Any],
    expected_source: str,
) -> tuple[sqlite3.Row, sqlite3.Row, list[sqlite3.Row]]:
    session_id = binding.get("session_id")
    user_id = binding.get("user_message_id")
    final_id = binding.get("final_message_id")
    if not isinstance(session_id, str) or not session_id or not isinstance(user_id, int) or not isinstance(final_id, int) or user_id >= final_id:
        raise ValueError("turn binding session/message IDs are invalid")
    rows = connection.execute(
        "SELECT m.id,m.session_id,m.role,m.content,m.tool_call_id,m.tool_calls,m.tool_name,"
        "m.platform_message_id,s.source,s.profile_name "
        "FROM messages AS m JOIN sessions AS s ON s.id=m.session_id "
        "WHERE m.session_id=? AND m.active=1 AND m.id BETWEEN ? AND ? ORDER BY m.id",
        (session_id, user_id, final_id),
    ).fetchall()
    if not rows or rows[0]["id"] != user_id or rows[-1]["id"] != final_id:
        raise ValueError("bound transcript endpoints do not exist or are inactive")
    user_row, final_row = rows[0], rows[-1]
    if user_row["role"] != "user" or final_row["role"] != "assistant":
        raise ValueError("bound transcript endpoints must be user then final assistant")
    if user_row["source"] != expected_source:
        raise ValueError(
            f"bound session source is not the declared {expected_source!r} transcript source"
        )
    if not isinstance(final_row["content"], str) or not final_row["content"]:
        raise ValueError("final assistant answer is missing")
    return user_row, final_row, rows


def adapt(
    state_db: Path,
    bindings: dict[str, Any],
    *,
    _connection: sqlite3.Connection | None = None,
    _database_identity_override: str | None = None,
) -> dict[str, Any]:
    if not isinstance(bindings, dict) or bindings.get("schema") not in {
        BINDING_SCHEMA,
        LIVE_BINDING_SCHEMA,
    }:
        raise ValueError(
            f"bindings schema must be {BINDING_SCHEMA} or {LIVE_BINDING_SCHEMA}"
        )
    live_fixture = bindings.get("schema") == LIVE_BINDING_SCHEMA
    allowed_binding_keys = {
        "schema", "captured_at", "transcript_source", "profile_artifact",
        "state_db_identity_sha256", "turns",
    }
    if set(bindings) - allowed_binding_keys:
        raise ValueError("bindings contain unknown keys")
    profile = _validate_profile_binding(bindings.get("profile_artifact"))
    transcript_source = bindings.get("transcript_source", "wecom")
    allowed_transcript_sources = {"cli", "wecom", "datasage-trusted-replay"}
    if transcript_source not in allowed_transcript_sources:
        raise ValueError(
            "transcript_source must be 'cli', 'wecom', or "
            "'datasage-trusted-replay'"
        )
    captured_at = bindings.get("captured_at")
    supplied_database_identity = bindings.get("state_db_identity_sha256")
    actual_database_identity = (
        _database_identity_override
        if _database_identity_override is not None
        else _database_identity(state_db)
    )
    if (
        not isinstance(supplied_database_identity, str)
        or supplied_database_identity != actual_database_identity
    ):
        raise ValueError("bindings state_db_identity_sha256 does not match the transcript database")
    turns = bindings.get("turns")
    if not isinstance(captured_at, str) or not captured_at or not isinstance(turns, list) or not turns:
        raise ValueError("bindings captured_at and non-empty turns are required")
    captured_observed_on: date | None = None
    if live_fixture:
        try:
            captured_timestamp = datetime.fromisoformat(
                captured_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("live bindings captured_at must be an ISO-8601 timestamp") from exc
        if captured_timestamp.tzinfo is None:
            raise ValueError("live bindings captured_at must include a timezone offset")
        captured_observed_on = captured_timestamp.date()
    if any(not isinstance(turn, dict) for turn in turns):
        raise ValueError("turn bindings must be objects")
    test_ids = [turn.get("test_id") for turn in turns]
    if any(not isinstance(value, str) or not value for value in test_ids) or len(test_ids) != len(set(test_ids)):
        raise ValueError("turn test_ids must be unique non-empty strings")

    candidate_rows: list[dict[str, Any]] = []
    receipt_turns: list[dict[str, Any]] = []
    previous: dict[
        str,
        tuple[str, str, bool, frozenset[str], frozenset[str]],
    ] = {}
    connection = _connection or _open_read_only(state_db)
    owns_connection = _connection is None
    try:
        for binding in sorted(turns, key=lambda item: (str(item.get("conversation_id")), int(item.get("turn", 0)))):
            required_keys = {
                "test_id", "conversation_id", "turn", "session_id",
                "user_message_id", "final_message_id", "canonical_prompt_sha256",
                "watermark_sha256",
            }
            if live_fixture:
                required_keys |= {
                    "fixture_attestation_sha256",
                    "business_database_ref_sha256",
                }
            if set(binding) - (required_keys | {"review", "session_lineage"}) or not required_keys.issubset(binding):
                raise ValueError(f"turn {binding.get('test_id')!r} has invalid binding keys")
            conversation = binding["conversation_id"]
            turn_number = binding["turn"]
            if not isinstance(conversation, str) or not conversation or not isinstance(turn_number, int) or turn_number < 1:
                raise ValueError("conversation_id/turn are invalid")
            user_row, final_row, rows = _turn_rows(
                connection,
                binding,
                transcript_source,
            )
            if user_row["profile_name"] != profile["profile_id"]:
                raise ValueError(
                    f"turn {binding['test_id']!r} session Profile does not match "
                    "profile_artifact.profile_id"
                )
            canonical_prompt_sha256 = binding["canonical_prompt_sha256"]
            if (
                not isinstance(canonical_prompt_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", canonical_prompt_sha256) is None
                or _sha256(user_row["content"] if isinstance(user_row["content"], str) else "")
                != canonical_prompt_sha256
            ):
                raise ValueError(
                    f"turn {binding['test_id']!r} does not match its canonical prompt hash"
                )
            expected_watermark = _watermark(
                test_id=binding["test_id"],
                conversation_id=conversation,
                turn=turn_number,
                canonical_prompt_sha256=canonical_prompt_sha256,
                user_message_id=user_row["id"],
                database_identity_sha256=actual_database_identity,
                profile=profile,
                fixture_attestation_sha256=(
                    binding.get("fixture_attestation_sha256")
                    if live_fixture
                    else None
                ),
                business_database_ref_sha256=(
                    binding.get("business_database_ref_sha256")
                    if live_fixture
                    else None
                ),
            )
            if binding["watermark_sha256"] != expected_watermark:
                raise ValueError(f"turn {binding['test_id']!r} replay watermark is invalid")
            calls = _extract_calls(rows)
            plan, evidence = _normalize(
                calls,
                expected_business_database_ref_sha256=(
                    binding["business_database_ref_sha256"]
                    if live_fixture
                    else None
                ),
                typed_context_bindings=live_fixture,
                expected_observed_on=captured_observed_on,
            )
            signature = _sha256({key: value for key, value in plan.items() if key != "context_action"})
            prior = previous.get(conversation)
            ambiguity_preserve = (
                prior is not None
                and prior[0] == binding["session_id"]
                and _is_unconfirmed_entity_ambiguity(plan, evidence)
                and bool(plan["domains"])
                and bool(plan["metrics"])
                and set(plan["domains"]).issubset(prior[3])
                and set(plan["metrics"]).issubset(prior[4])
            )
            final_answer_sha256 = _sha256(final_row["content"])
            conclusions, review, trace = _review(
                binding.get("review"),
                expected={
                    "test_id": binding["test_id"],
                    "artifact_id": profile["artifact_id"],
                    "payload_sha256": profile["payload_sha256"],
                    "session_id": binding["session_id"],
                    "user_message_id": user_row["id"],
                    "canonical_prompt_sha256": canonical_prompt_sha256,
                    "database_identity_sha256": actual_database_identity,
                    "watermark_sha256": expected_watermark,
                    "final_answer_sha256": final_answer_sha256,
                    **(
                        {
                            "fixture_attestation_sha256": binding[
                                "fixture_attestation_sha256"
                            ],
                            "business_database_ref_sha256": binding[
                                "business_database_ref_sha256"
                            ],
                        }
                        if live_fixture
                        else {}
                    ),
                },
            )
            lineage = binding.get("session_lineage", [])
            if (
                not isinstance(lineage, list)
                or any(not isinstance(item, str) or not item for item in lineage)
                or len(lineage) != len(set(lineage))
            ):
                raise ValueError("session_lineage must contain unique session IDs")
            if trace is not None:
                _validate_performance_scorecard_trace(plan, trace)
                for field in ("domains", "metrics", "dimensions"):
                    if not set(plan[field]).issubset(set(trace[field])):
                        raise ValueError(
                            f"review plan_trace.{field} contradicts persisted tool plan"
                        )
                plan = dict(trace)
                if live_fixture:
                    _validate_live_context_bindings(plan.get("context_bindings"))
                action = plan["context_action"]
                if prior is None and action not in {"new", "reset"}:
                    raise ValueError("first bound turn must be new or an explicit reset")
                if prior is not None:
                    same_lineage = (
                        prior[0] == binding["session_id"]
                        or (
                            len(lineage) >= 2
                            and lineage[0] == prior[0]
                            and lineage[-1] == binding["session_id"]
                        )
                    )
                    if action == "reset" and same_lineage:
                        raise ValueError("reset plan_trace did not create a new session lineage")
                    if action in {"preserve", "replace"} and not same_lineage:
                        raise ValueError("context transition is not backed by session lineage")
                    if action == "new":
                        raise ValueError("non-initial turn cannot use context_action=new")
            elif prior is None:
                plan["context_action"] = "new"
            elif prior[0] != binding["session_id"]:
                plan["context_action"] = "reset"
            elif ambiguity_preserve or prior[1] == signature:
                plan["context_action"] = "preserve"
            else:
                plan["context_action"] = "replace"
            if live_fixture:
                _validate_live_context_bindings(plan.get("context_bindings"))
            if prior is not None and plan["context_action"] in {"preserve", "replace"}:
                _ordered_add(evidence["receipts"], "context_transition")
            if ambiguity_preserve and prior[2]:
                _ordered_add(evidence["receipts"], "catalog")
            if plan["context_action"] == "reset":
                _ordered_add(evidence["receipts"], "session_reset")
            previous[conversation] = (
                binding["session_id"],
                signature,
                "catalog" in evidence["receipts"],
                frozenset(plan["domains"]),
                frozenset(plan["metrics"]),
            )
            transcript_projection = [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "tool_name": row["tool_name"],
                    "tool_call_id": row["tool_call_id"],
                    "tool_calls": row["tool_calls"],
                    "content": row["content"],
                }
                for row in rows
            ]
            candidate_row = {
                "id": binding["test_id"],
                "session_id": binding["session_id"],
                "session_lineage": list(lineage),
                "plan": plan,
                "conclusions": conclusions,
                "conclusion_review": review,
                "evidence": evidence,
            }
            candidate_rows.append(candidate_row)
            receipt_turns.append(
                {
                    "test_id": binding["test_id"],
                    "conversation_id": conversation,
                    "turn": turn_number,
                    "session_id": binding["session_id"],
                    "database_message_ids": [row["id"] for row in rows],
                    "user_message_id": user_row["id"],
                    "canonical_prompt_sha256": canonical_prompt_sha256,
                    "database_identity_sha256": actual_database_identity,
                    "watermark_sha256": expected_watermark,
                    "user_platform_message_id": user_row["platform_message_id"],
                    "final_message_id": final_row["id"],
                    "final_platform_message_id": final_row["platform_message_id"],
                    "final_answer_sha256": final_answer_sha256,
                    "transcript_sha256": _sha256(transcript_projection),
                    "candidate_case_sha256": _sha256(candidate_row),
                    "conclusion_review": review,
                    **(
                        {
                            "fixture_attestation_sha256": binding[
                                "fixture_attestation_sha256"
                            ],
                            "business_database_ref_sha256": binding[
                                "business_database_ref_sha256"
                            ],
                        }
                        if live_fixture
                        else {}
                    ),
                }
            )
    finally:
        if owns_connection:
            connection.close()

    receipt_body = {
        "schema": RECEIPT_SCHEMA,
        "captured_at": captured_at,
        "source": {
            "platform": transcript_source,
            "sqlite_mode": "ro",
            "query_only": True,
            "state_db_identity_sha256": actual_database_identity,
        },
        "profile_artifact": profile,
        "state_db_identity_sha256": actual_database_identity,
        "candidate_cases_sha256": _sha256(candidate_rows),
        "turns": receipt_turns,
    }
    receipt = {**receipt_body, "receipt_sha256": _sha256(receipt_body)}
    return {
        "schema": CANDIDATE_SCHEMA,
        "profile_artifact": profile,
        "state_db_identity_sha256": actual_database_identity,
        "cases": candidate_rows,
        "canary_receipt": receipt,
    }


def verify_receipt(candidate: dict[str, Any]) -> bool:
    receipt = candidate.get("canary_receipt") if isinstance(candidate, dict) else None
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        return False
    expected = receipt.get("receipt_sha256")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    cases = candidate.get("cases")
    turns = receipt.get("turns")
    if not isinstance(cases, list) or not isinstance(turns, list) or len(cases) != len(turns):
        return False
    profile = receipt.get("profile_artifact")
    database_identity = receipt.get("state_db_identity_sha256")
    if not isinstance(profile, dict) or not isinstance(database_identity, str):
        return False
    turn_by_id = {
        turn.get("test_id"): turn for turn in turns if isinstance(turn, dict)
    }
    case_hashes_match = len(turn_by_id) == len(turns)
    for case in cases:
        if not case_hashes_match or not isinstance(case, dict):
            case_hashes_match = False
            break
        turn = turn_by_id.get(case.get("id"))
        if not isinstance(turn, dict):
            case_hashes_match = False
            break
        live_fields = (
            "fixture_attestation_sha256",
            "business_database_ref_sha256",
        )
        live_fixture = any(field in turn for field in live_fields)
        if live_fixture and not all(
            isinstance(turn.get(field), str)
            and re.fullmatch(r"[0-9a-f]{64}", turn[field]) is not None
            for field in live_fields
        ):
            case_hashes_match = False
            break
        try:
            expected_watermark = _watermark(
                test_id=turn.get("test_id"),
                conversation_id=turn.get("conversation_id"),
                turn=turn.get("turn"),
                canonical_prompt_sha256=turn.get("canonical_prompt_sha256"),
                user_message_id=turn.get("user_message_id"),
                database_identity_sha256=database_identity,
                profile=profile,
                fixture_attestation_sha256=(
                    turn.get("fixture_attestation_sha256") if live_fixture else None
                ),
                business_database_ref_sha256=(
                    turn.get("business_database_ref_sha256") if live_fixture else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            case_hashes_match = False
            break
        review = case.get("conclusion_review")
        if isinstance(review, dict) and review.get("status") == "reviewed":
            review_binding = {
                "test_id": turn.get("test_id"),
                "artifact_id": profile.get("artifact_id"),
                "payload_sha256": profile.get("payload_sha256"),
                "session_id": turn.get("session_id"),
                "user_message_id": turn.get("user_message_id"),
                "canonical_prompt_sha256": turn.get("canonical_prompt_sha256"),
                "database_identity_sha256": turn.get("database_identity_sha256"),
                "watermark_sha256": turn.get("watermark_sha256"),
                "final_answer_sha256": turn.get("final_answer_sha256"),
                **(
                    {field: turn.get(field) for field in live_fields}
                    if live_fixture
                    else {}
                ),
            }
            if review.get("binding_sha256") != _sha256(review_binding):
                case_hashes_match = False
                break
        if (
            turn.get("candidate_case_sha256") != _sha256(case)
            or turn.get("conclusion_review") != review
            or turn.get("database_identity_sha256") != database_identity
            or turn.get("watermark_sha256") != expected_watermark
        ):
            case_hashes_match = False
            break
    return (
        isinstance(expected, str)
        and expected == _sha256(body)
        and candidate.get("profile_artifact") == receipt.get("profile_artifact")
        and candidate.get("state_db_identity_sha256")
        == receipt.get("state_db_identity_sha256")
        and receipt.get("candidate_cases_sha256") == _sha256(cases)
        and case_hashes_match
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    bindings = json.loads(args.bindings.read_text(encoding="utf-8"))
    candidate = adapt(args.state_db, bindings)
    if not verify_receipt(candidate):
        raise RuntimeError("generated canary receipt failed self-verification")
    _write_json_atomic(args.output, candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
