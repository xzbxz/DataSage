"""Deterministic evidence coverage for model-visible DataSage results.

This module does not interpret business values.  It reports optional caller
annotations beside proof capabilities already sealed by the query tool; labels
never authorize a claim.  The answer layer sees what is supported, missing, or
bounded.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from . import capability_contract, contract_store


EVIDENCE_BUNDLE_VERSION = "evidence-bundle/v1"
COVERAGE_RECEIPTS_VERSION = "semantic-coverage-receipts/v1"
_SEMANTIC_FINGERPRINT_PREFIX = "semreq_v1_"
_SEMANTIC_REQUEST_FINGERPRINT_VERSION = "semantic-request/v1"
_SEMANTIC_FINGERPRINT_PRESENTATION_KEYS = {
    "request_id",
    "purpose",
    "decomposition_of_request_id",
}

_LIMITED_STATES = {"empty", "undefined", "incomplete"}
_COMPLETE_STATES = {"rows", "complete", "zero"}


def _canonical_semantic_value(value: Any, path: tuple[str, ...] = ()) -> Any:
    """Canonicalize unordered semantic collections without exposing them."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_semantic_value(item, (*path, str(key)))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        items = [_canonical_semantic_value(item, path) for item in value]
        if (path and path[-1] == "dimensions") or "metric_filters" in path:
            return sorted(
                items,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
            )
        return items
    return value


def _canonical_semantic_boundary(value: Any) -> Any:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}", value):
        return f"{value}-01"
    return value


def _calendar_month_time_range(value: Any) -> dict[str, str] | None:
    try:
        return capability_contract._calendar_month_time_range(value)
    except (ValueError, OverflowError):
        return None


def _semantic_request_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        str(key): value
        for key, value in request.items()
        if str(key) not in _SEMANTIC_FINGERPRINT_PRESENTATION_KEYS
        and not str(key).startswith("_")
    }
    # Public requests no longer expose mode; retain the established semantic
    # fingerprint by projecting the runtime's sole governed mode explicitly.
    normalized.setdefault("mode", "metric")
    calendar_month = normalized.pop("calendar_month", None)
    if calendar_month is not None and "time_range" not in normalized:
        expanded = _calendar_month_time_range(calendar_month)
        if expanded is None:
            normalized["calendar_month"] = calendar_month
        else:
            normalized["time_range"] = expanded
    time_range = normalized.get("time_range")
    if isinstance(time_range, Mapping):
        normalized["time_range"] = {
            "start": _canonical_semantic_boundary(time_range.get("start")),
            "end": _canonical_semantic_boundary(time_range.get("end")),
        }
    if (
        isinstance(normalized.get("complete_change_decomposition"), Mapping)
        and "comparison" not in normalized
    ):
        normalized["comparison"] = {"kind": "previous_period"}
    if normalized.get("domain") == "delivery":
        normalized.setdefault("delivery_scope", "default_net")
    if normalized.get("domain") == "inventory":
        normalized.pop("inventory_scope", None)
    return _canonical_semantic_value(normalized)


def semantic_request_fingerprint(
    request: Mapping[str, Any],
    *,
    scope_fingerprint: str | None = None,
) -> str:
    """Hash a versioned semantic request shape for production and test doubles."""

    projection = {
        "version": _SEMANTIC_REQUEST_FINGERPRINT_VERSION,
        "request": _semantic_request_projection(request),
    }
    if isinstance(scope_fingerprint, str) and scope_fingerprint:
        projection["compiled_scope_fingerprint"] = scope_fingerprint
    digest = hashlib.sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"{_SEMANTIC_FINGERPRINT_PREFIX}{digest}"


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _finite_decimal(value: Any) -> Decimal | None:
    """Parse a finite business number without accepting booleans or NaN."""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_close(left: Decimal, right: Decimal) -> bool:
    """Compare generated decimal strings while allowing bounded rounding."""

    scale = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) <= scale * Decimal("0.000000001")


def _target_amounts_consistent(
    target: Decimal | None,
    actual: Decimal | None,
    gap: Decimal | None,
) -> bool:
    """Check only the additive target/actual/gap relationship."""

    return (
        target is not None
        and actual is not None
        and gap is not None
        and _decimal_close(gap, target - actual)
    )


def _target_claim_amounts(
    claim: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[Decimal, Decimal, Decimal, Decimal | None] | None:
    """Validate one target-status claim and return its governed amounts."""

    relations = claim.get("allowed_relations")
    facts = claim.get("facts")
    states = claim.get("states")
    target_state = states.get("target_data_state") if isinstance(states, Mapping) else None
    zero_target_with_undefined_metric = (
        target_state == "zero"
        and result.get("data_state") == "undefined"
        and isinstance(facts, Mapping)
        and "metric_value" in facts
        and facts.get("metric_value") is None
        and "completion_rate" in facts
        and facts.get("completion_rate") is None
    )
    if (
        not isinstance(relations, list)
        or "target_status" not in relations
        or not isinstance(facts, Mapping)
        or not isinstance(states, Mapping)
        or result.get("status") != "success"
        or result.get("truncated") is not False
        or (
            result.get("data_state") not in _COMPLETE_STATES
            and not zero_target_with_undefined_metric
        )
        or states.get("target_data_state") not in {"set", "zero"}
        or states.get("period_state") in {"not_started", "includes_future", "future"}
    ):
        return None

    target = _finite_decimal(facts.get("target_amount_rmb"))
    actual = _finite_decimal(facts.get("actual_amount_rmb"))
    gap = _finite_decimal(facts.get("gap_amount_rmb"))
    completion = _finite_decimal(facts.get("completion_rate"))
    metric_value = _finite_decimal(facts.get("metric_value"))
    if not _target_amounts_consistent(target, actual, gap):
        return None
    if target_state == "zero":
        if target != 0 or completion is not None or metric_value is not None:
            return None
        return target, actual, gap, None
    if target <= 0 or completion is None:
        return None
    if not _decimal_close(completion, actual / target):
        return None
    if facts.get("metric_value") is not None and metric_value is None:
        return None
    if metric_value is not None and not _decimal_close(metric_value, completion):
        return None
    return target, actual, gap, completion


def _claim_relations(result: Mapping[str, Any]) -> set[str]:
    claims = result.get("claim_ledger")
    if not isinstance(claims, list):
        return set()
    return {
        relation
        for claim in claims
        if isinstance(claim, Mapping) and claim_is_valid_for_result(claim, result)
        for relation in _string_set(claim.get("allowed_relations"))
    }


def _reconciliation_status(result: Mapping[str, Any]) -> str:
    reconciliation = result.get("change_reconciliation")
    if isinstance(reconciliation, Mapping):
        status = reconciliation.get("status")
        if status == "reconciled" and not _reconciliation_is_valid(result):
            return "invalid_reconciliation"
        if isinstance(status, str) and status:
            return status
    return "not_requested_or_unavailable"


def _target_gap_reconciliation_is_valid(
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
    overall_result: Mapping[str, Any] | None = None,
) -> bool:
    """Verify a target-gap receipt against both physical result branches.

    A partition result cannot prove the overall claim by itself.  Callers must
    therefore provide the hidden overall result and the exact partition request
    context.  This intentionally fails closed when either context is absent;
    callers that only have one public result must not manufacture a composition
    capability from a self-contained receipt.
    """

    reconciliation = result.get("target_gap_reconciliation")
    claims = result.get("claim_ledger")
    if (
        not isinstance(reconciliation, Mapping)
        or not isinstance(claims, list)
        or not claims
        or not isinstance(request, Mapping)
        or not isinstance(overall_result, Mapping)
    ):
        return False
    try:
        contract = contract_store.read_target_gap_contract()
    except (
        contract_store.ContractStoreError,
        capability_contract.CapabilityContractError,
    ):
        return False

    request_id = result.get("request_id")
    overall_request_id = overall_result.get("request_id")
    business_metric_ref = result.get("business_metric_ref")
    overall_business_metric_ref = overall_result.get("business_metric_ref")
    business_metric_unit = result.get("business_metric_unit")
    overall_business_metric_unit = overall_result.get("business_metric_unit")
    applied_time_range = result.get("applied_time_range")
    overall_applied_time_range = overall_result.get("applied_time_range")
    partition_claim_ids = [
        claim.get("claim_id") for claim in claims if isinstance(claim, Mapping)
    ]
    if (
        not isinstance(request_id, str)
        or not request_id
        or request.get("request_id") != request_id
        or not isinstance(overall_request_id, str)
        or not overall_request_id
        or not isinstance(business_metric_ref, str)
        or not business_metric_ref
        or business_metric_ref != overall_business_metric_ref
        or business_metric_unit != capability_contract.TARGET_COMPLETION_UNIT
        or business_metric_unit != overall_business_metric_unit
        or not isinstance(applied_time_range, Mapping)
        or applied_time_range != overall_applied_time_range
        or reconciliation.get("partition_request_id") != request_id
        or reconciliation.get("overall_request_id") != overall_request_id
        or reconciliation.get("version") != contract.receipt_version
        or reconciliation.get("status") != "reconciled"
        or reconciliation.get("operation") != contract.receipt_operation
        or reconciliation.get("reconciliation_id")
        != _canonical_reconciliation_id(reconciliation)
        or reconciliation.get("completion_rate_aggregated") is not False
        or reconciliation.get("causal_attribution_authorized") is not False
        or reconciliation.get("interpretation_boundary")
        != contract.receipt_interpretation_code
        or reconciliation.get("snapshot_consistency")
        != "same_connection_repeatable_read_consistent_snapshot"
        or reconciliation.get("proof_mode") != "returned_full_partition"
        or not isinstance(reconciliation.get("full_partition_row_count"), int)
        or isinstance(reconciliation.get("full_partition_row_count"), bool)
        or reconciliation.get("full_partition_row_count") != len(claims)
        or reconciliation.get("completion_rate_basis")
        != "overall_actual_amount_rmb / overall_target_amount_rmb"
        or not isinstance(reconciliation.get("dimension"), str)
        or request.get("dimensions") != [reconciliation.get("dimension")]
        or reconciliation.get("dimension") not in contract.dimensions
        or len(partition_claim_ids) != len(claims)
        or len(set(partition_claim_ids)) != len(partition_claim_ids)
        or reconciliation.get("partition_claim_ids") != partition_claim_ids
        or reconciliation.get("population_fingerprint")
        != result.get("scope_fingerprint")
        or result.get("scope_fingerprint") != overall_result.get("scope_fingerprint")
        or reconciliation.get("partition_projection_fingerprint")
        != result.get("projection_fingerprint")
        or reconciliation.get("overall_projection_fingerprint")
        != overall_result.get("projection_fingerprint")
        or not isinstance(result.get("scope_fingerprint"), str)
        or not result.get("scope_fingerprint")
        or not isinstance(result.get("projection_fingerprint"), str)
        or not result.get("projection_fingerprint")
        or not isinstance(overall_result.get("projection_fingerprint"), str)
        or not overall_result.get("projection_fingerprint")
        or result.get("_snapshot_group_marker")
        != overall_result.get("_snapshot_group_marker")
        or not isinstance(result.get("_snapshot_group_marker"), str)
        or not result.get("_snapshot_group_marker")
    ):
        return False

    overall_claims = overall_result.get("claim_ledger")
    if (
        overall_result.get("status") != "success"
        or overall_result.get("truncated") is not False
        or not isinstance(overall_claims, list)
        or len(overall_claims) != 1
        or not isinstance(overall_claims[0], Mapping)
        or overall_claims[0].get("dimensions")
        or not _claim_is_validly_sealed(overall_claims[0])
        or not claim_is_valid_for_result(overall_claims[0], overall_result)
        or reconciliation.get("overall_claim_id")
        != overall_claims[0].get("claim_id")
    ):
        return False

    if any(
        claim.get("unit") != business_metric_unit
        or claim.get("fact_units") != capability_contract.TARGET_COMPLETION_FACT_UNITS
        for claim in [overall_claims[0], *claims]
    ):
        return False

    if any(
        not isinstance(claim, Mapping)
        or not _claim_is_validly_sealed(claim)
        or not claim_is_valid_for_result(claim, result)
        or _target_claim_amounts(claim, result) is None
        or not claim.get("dimensions")
        for claim in claims
    ):
        return False
    overall_amounts = _target_claim_amounts(overall_claims[0], overall_result)
    if overall_amounts is None:
        return False
    partition_amounts = [
        _target_claim_amounts(claim, result) for claim in claims
    ]
    if any(item is None for item in partition_amounts):
        return False
    complete = [item for item in partition_amounts if item is not None]
    partition_sums = tuple(
        sum((item[index] for item in complete), Decimal("0"))
        for index in range(3)
    )
    if partition_sums != overall_amounts[:3]:
        return False
    if any(
        _finite_decimal(reconciliation.get(overall_key))
        != _finite_decimal(overall_value)
        or _finite_decimal(reconciliation.get(partition_key))
        != _finite_decimal(partition_value)
        for (overall_key, partition_key), overall_value, partition_value in zip(
            (
                ("overall_target_amount_rmb", "partition_target_sum_rmb"),
                ("overall_actual_amount_rmb", "partition_actual_sum_rmb"),
                ("overall_gap_amount_rmb", "partition_gap_sum_rmb"),
            ),
            overall_amounts[:3],
            partition_sums,
        )
    ):
        return False
    receipt_completion = _finite_decimal(reconciliation.get("overall_completion_rate"))
    overall_completion = overall_amounts[3]
    if (
        (receipt_completion is None) != (overall_completion is None)
        or receipt_completion is not None
        and overall_completion is not None
        and not _decimal_close(receipt_completion, overall_completion)
    ):
        return False
    return True


def _completeness(result: Mapping[str, Any]) -> str:
    if result.get("status") == "partial":
        return "limited"
    if result.get("status") != "success":
        return "failed"
    if result.get("truncated") is True or result.get("data_state") == "truncated":
        return "truncated"
    if result.get("data_state") in _LIMITED_STATES:
        return "limited"
    if result.get("data_state") in _COMPLETE_STATES:
        return "complete"
    return "limited"


def _supports(
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
    overall_result: Mapping[str, Any] | None = None,
) -> list[str]:
    if result.get("status") != "success":
        return []
    error = result.get("error")
    if (
        isinstance(error, Mapping)
        and error.get("code") == "EVIDENCE_INTEGRITY_INVALID"
    ):
        return []
    supported = _claim_relations(result)
    # Reconciliation, not a claim-supplied relation string, is the authority
    # for structural contribution. Translate legacy fixture vocabulary without
    # allowing either spelling to bypass that proof boundary.
    supported.discard("change_driver")
    supported.discard("structural_contribution")
    if _reconciliation_status(result) == "reconciled":
        supported.add("structural_contribution")
    if _target_gap_reconciliation_is_valid(
        result,
        request=request,
        overall_result=overall_result,
    ):
        supported.add("target_gap_composition")
    if result.get("data_state") == "empty":
        supported.add("empty_result_state")
    if result.get("data_state") == "undefined":
        supported.add("undefined_result_state")
    if result.get("data_state") == "zero":
        supported.add("verified_zero_state")
    return sorted(supported)


def _calendar_period_states(value: Any) -> list[str]:
    """Read only canonical v2 calendar evidence nodes."""

    if not isinstance(value, Mapping):
        return []
    states: list[str] = []
    calendar = value.get("calendar_evidence")
    if (
        isinstance(calendar, Mapping)
        and calendar.get("version") == "calendar-period-evidence/v2"
        and calendar.get("period_state")
        in {"completed", "in_progress", "not_started"}
    ):
        states.append(str(calendar["period_state"]))
    for key, child in value.items():
        if key != "calendar_evidence" and isinstance(child, Mapping):
            states.extend(_calendar_period_states(child))
    return states


def _limitations(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    overall_result: Mapping[str, Any] | None = None,
) -> list[str]:
    limitations: list[str] = []
    if result.get("status") == "partial":
        limitations.append("REQUEST_PARTIALLY_FAILED")
    elif result.get("status") != "success":
        limitations.append("REQUEST_FAILED")
    if result.get("truncated") is True or result.get("data_state") == "truncated":
        limitations.extend(("SOURCE_TRUNCATED", "COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED"))
    data_state = result.get("data_state")
    if data_state in _LIMITED_STATES:
        limitations.append(f"DATA_STATE_{str(data_state).upper()}")
    if data_state not in _COMPLETE_STATES | _LIMITED_STATES:
        limitations.append("DATA_STATE_UNRECOGNIZED")
    change_reconciliation = result.get("change_reconciliation")
    if (
        isinstance(change_reconciliation, Mapping)
        and _reconciliation_status(result) != "reconciled"
    ):
        limitations.append("STRUCTURAL_CONTRIBUTION_NOT_RECONCILED")
    target_gap_reconciliation = result.get("target_gap_reconciliation")
    if isinstance(target_gap_reconciliation, Mapping):
        if target_gap_reconciliation.get("status") != "reconciled":
            limitations.append("TARGET_GAP_NOT_RECONCILED")
        elif not _target_gap_reconciliation_is_valid(
            result,
            request=request,
            overall_result=overall_result,
        ):
            limitations.append("TARGET_GAP_NOT_RECONCILED")
    period = result.get("applied_time_range")
    if isinstance(period, Mapping):
        compatibility = period.get("comparison_compatibility")
        if isinstance(compatibility, Mapping):
            reasons = compatibility.get("reason_codes")
            if isinstance(reasons, list):
                limitations.extend(
                    str(reason)
                    for reason in reasons
                    if isinstance(reason, str) and reason
                )
        period_states = _calendar_period_states(period)
        if "in_progress" in period_states:
            limitations.append("PERIOD_IN_PROGRESS")
        if "not_started" in period_states:
            limitations.append("PERIOD_NOT_STARTED")
    return sorted(set(limitations))


def _semantic_fingerprint(result: Mapping[str, Any]) -> str | None:
    value = result.get("_semantic_request_fingerprint")
    if (
        not isinstance(value, str)
        or not value.startswith(_SEMANTIC_FINGERPRINT_PREFIX)
        or len(value) != len(_SEMANTIC_FINGERPRINT_PREFIX) + 64
        or any(character not in "0123456789abcdef" for character in value[-64:])
    ):
        return None
    return value


def _canonical_claim_identity(claim: Mapping[str, Any]) -> tuple[str, str]:
    """Reproduce the production claim ID and seal over canonical public fields."""

    canonical = {
        key: value
        for key, value in claim.items()
        if key not in {"claim_id", "claim_seal"}
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    claim_id = f"claim_{digest[:20]}"
    sealed_claim = {**canonical, "claim_id": claim_id}
    seal = hashlib.sha256(
        json.dumps(
            sealed_claim,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return claim_id, f"sha256_{seal}"


def seal_claim(claim: dict[str, Any]) -> None:
    """Assign the one canonical production identity accepted by receipts."""

    claim_id, claim_seal = _canonical_claim_identity(claim)
    claim["claim_id"] = claim_id
    claim["claim_seal"] = claim_seal


def _canonical_reconciliation_id(reconciliation: Mapping[str, Any]) -> str:
    canonical = {
        key: value
        for key, value in reconciliation.items()
        if key != "reconciliation_id"
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"reconciliation_{digest[:20]}"


def seal_reconciliation(reconciliation: dict[str, Any]) -> None:
    """Assign the canonical reconciliation identity consumed by evidence."""

    reconciliation["reconciliation_id"] = _canonical_reconciliation_id(
        reconciliation
    )


def _is_structural_claim(claim: Mapping[str, Any]) -> bool:
    relations = _string_set(claim.get("allowed_relations"))
    return bool(relations & {"structural_contribution", "change_driver"})


_COMPARISON_COMPLETENESS_COUNT_FIELDS = {
    "current_missing_value_count",
    "current_known_value_count",
    "comparison_missing_value_count",
    "comparison_known_value_count",
}
_COMPARISON_COMPLETENESS_STATE_FIELDS = {
    "current_metric_data_state",
    "comparison_metric_data_state",
}
_COMPLETENESS_PROOF_FIELDS = {
    "policy",
    "overall",
    "partition_totals",
    "returned_partition_totals",
}


def _exact_nonnegative_int(value: Any) -> int | None:
    parsed = _finite_decimal(value)
    if parsed is None or parsed < 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _exact_completeness_counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or set(value) != (
        _COMPARISON_COMPLETENESS_COUNT_FIELDS
    ):
        return None
    counts = {
        field: _exact_nonnegative_int(value.get(field))
        for field in _COMPARISON_COMPLETENESS_COUNT_FIELDS
    }
    if any(count is None for count in counts.values()):
        return None
    return {field: int(count) for field, count in counts.items()}


def _claim_completeness_counts(claim: Mapping[str, Any]) -> dict[str, int] | None:
    facts = claim.get("facts")
    states = claim.get("states")
    if not isinstance(facts, Mapping) or not isinstance(states, Mapping):
        return None
    counts = _exact_completeness_counts(
        {
            field: facts.get(field)
            for field in _COMPARISON_COMPLETENESS_COUNT_FIELDS
        }
    )
    if counts is None:
        return None
    if not _COMPARISON_COMPLETENESS_STATE_FIELDS <= set(states):
        return None
    for prefix in ("current", "comparison"):
        missing = counts[f"{prefix}_missing_value_count"]
        known = counts[f"{prefix}_known_value_count"]
        state = str(states.get(f"{prefix}_metric_data_state") or "").casefold()
        if state == "complete" and missing == 0 and known > 0:
            continue
        if state == "not_present" and missing == known == 0:
            continue
        return None
    return counts


def _structural_completeness_proof_is_valid(
    reconciliation: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    structural_claims: Sequence[Mapping[str, Any]],
    *,
    truncated: bool,
) -> bool:
    structural_claim_has_coverage = any(
        (
            isinstance(claim.get("facts"), Mapping)
            and bool(
                _COMPARISON_COMPLETENESS_COUNT_FIELDS
                & set(claim["facts"])
            )
        )
        or (
            isinstance(claim.get("states"), Mapping)
            and bool(
                _COMPARISON_COMPLETENESS_STATE_FIELDS
                & set(claim["states"])
            )
        )
        for claim in structural_claims
    )
    receipt_has_coverage_policy = "completeness_proof" in reconciliation
    if not structural_claim_has_coverage and not receipt_has_coverage_policy:
        return True

    proof = reconciliation.get("completeness_proof")
    if (
        not isinstance(proof, Mapping)
        or set(proof) != _COMPLETENESS_PROOF_FIELDS
        or proof.get("policy") != "exact_integer_counts_both_periods"
    ):
        return False
    overall = _exact_completeness_counts(proof.get("overall"))
    partition = _exact_completeness_counts(proof.get("partition_totals"))
    returned = _exact_completeness_counts(proof.get("returned_partition_totals"))
    claim_counts = [_claim_completeness_counts(claim) for claim in claims]
    if (
        overall is None
        or partition is None
        or returned is None
        or any(counts is None for counts in claim_counts)
        or overall != partition
    ):
        return False
    for prefix in ("current", "comparison"):
        if (
            overall[f"{prefix}_missing_value_count"] != 0
            or overall[f"{prefix}_known_value_count"] <= 0
        ):
            return False
    expected_returned = {
        field: sum(
            counts[field]
            for counts in claim_counts
            if counts is not None
        )
        for field in _COMPARISON_COMPLETENESS_COUNT_FIELDS
    }
    if returned != expected_returned:
        return False
    if truncated:
        return all(returned[field] <= partition[field] for field in returned)
    return returned == partition


def _reconciliation_is_valid(result: Mapping[str, Any]) -> bool:
    reconciliation = result.get("change_reconciliation")
    claims = result.get("claim_ledger")
    if (
        not isinstance(reconciliation, Mapping)
        or reconciliation.get("status") != "reconciled"
        or reconciliation.get("operation") != "complete_change_decomposition"
        or reconciliation.get("causal_attribution_authorized") is True
        or reconciliation.get("reconciliation_id")
        != _canonical_reconciliation_id(reconciliation)
        or not isinstance(claims, list)
        or not claims
        or any(
            not isinstance(claim, Mapping)
            or not _claim_is_validly_sealed(claim)
            or not claim_is_valid_for_result(claim, result)
            for claim in claims
        )
    ):
        return False
    claim_ids = [claim.get("claim_id") for claim in claims]
    if (
        any(not isinstance(claim_id, str) or not claim_id for claim_id in claim_ids)
        or len(set(claim_ids)) != len(claim_ids)
    ):
        return False
    structural_claims = [claim for claim in claims if _is_structural_claim(claim)]
    structural_ids = [claim.get("claim_id") for claim in structural_claims]
    if (
        not structural_ids
        or reconciliation.get("partition_claim_ids") != claim_ids
        or reconciliation.get("driver_claim_ids") != structural_ids
        or not isinstance(reconciliation.get("overall_request_id"), str)
        or not isinstance(reconciliation.get("overall_claim_id"), str)
        or not str(reconciliation.get("overall_claim_id")).startswith("claim_")
    ):
        return False
    for claim in structural_claims:
        facts = claim.get("facts")
        semantics = claim.get("relation_semantics")
        if (
            not isinstance(facts, Mapping)
            or "net_change_contribution_rate" not in facts
            or not isinstance(semantics, Mapping)
            or semantics.get("structural_contribution")
            != "structural_not_causal"
        ):
            return False
        overall_delta = _finite_decimal(reconciliation.get("overall_delta"))
        delta = _finite_decimal(facts.get("delta_value"))
        contribution_rate = _finite_decimal(
            facts.get("net_change_contribution_rate")
        )
        if (
            overall_delta is None
            or overall_delta == 0
            or delta is None
            or contribution_rate is None
            or not _decimal_close(
                contribution_rate,
                delta / overall_delta,
            )
        ):
            return False
    if not _structural_completeness_proof_is_valid(
        reconciliation,
        claims,
        structural_claims,
        truncated=result.get("truncated") is True,
    ):
        return False
    scope_fingerprint = result.get("scope_fingerprint")
    if (
        isinstance(scope_fingerprint, str)
        and reconciliation.get("population_fingerprint") != scope_fingerprint
    ):
        return False
    projection_fingerprint = result.get("projection_fingerprint")
    if (
        isinstance(projection_fingerprint, str)
        and reconciliation.get("driver_projection_fingerprint")
        != projection_fingerprint
    ):
        return False
    if (
        "returned_driver_row_count" in reconciliation
        and reconciliation.get("returned_driver_row_count") != len(claims)
    ):
        return False
    if (
        reconciliation.get("returned_nonzero_driver_count", len(structural_ids))
        != len(structural_ids)
        or reconciliation.get("nonzero_driver_count", len(structural_ids))
        != len(structural_ids)
    ):
        return False
    for overall_key, driver_key in (
        ("overall_current", "driver_current_sum"),
        ("overall_comparison", "driver_comparison_sum"),
        ("overall_delta", "driver_delta_sum"),
    ):
        if (
            overall_key in reconciliation
            or driver_key in reconciliation
        ) and reconciliation.get(overall_key) != reconciliation.get(driver_key):
            return False
    return True


def _claim_is_validly_sealed(claim: Mapping[str, Any]) -> bool:
    expected_id, expected_seal = _canonical_claim_identity(claim)
    return (
        claim.get("claim_id") == expected_id
        and claim.get("claim_seal") == expected_seal
    )


def _has_calendar_period_evidence(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("version") == "calendar-period-evidence/v2":
        return True
    return any(
        _has_calendar_period_evidence(item)
        for item in value.values()
        if isinstance(item, Mapping)
    )


def claim_is_valid_for_result(
    claim: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    """Verify the sealed claim and every result binding exposed to the model."""

    request_id = result.get("request_id")
    metric_ref = result.get("business_metric_ref")
    scope_fingerprint = result.get("scope_fingerprint")
    projection_fingerprint = result.get("projection_fingerprint")
    truncated = result.get("truncated")
    applied_time_range = result.get("applied_time_range")
    return (
        _claim_is_validly_sealed(claim)
        and isinstance(request_id, str)
        and bool(request_id)
        and claim.get("request_id") == request_id
        and isinstance(metric_ref, str)
        and bool(metric_ref)
        and claim.get("metric_ref") == metric_ref
        and isinstance(scope_fingerprint, str)
        and bool(scope_fingerprint)
        and claim.get("scope_fingerprint") == scope_fingerprint
        and isinstance(projection_fingerprint, str)
        and bool(projection_fingerprint)
        and claim.get("projection_fingerprint") == projection_fingerprint
        and isinstance(truncated, bool)
        and claim.get("source_truncated") is truncated
        and (
            not isinstance(applied_time_range, Mapping)
            or claim.get("period") == applied_time_range
        )
    )


def _has_only_sealed_claims(
    result: Mapping[str, Any],
    request_id: str,
) -> bool:
    if result.get("status") != "success":
        return False
    claims = result.get("claim_ledger")
    if not isinstance(claims, list) or not claims:
        return False
    row_count = result.get("row_count")
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count != len(claims)
        or result.get("request_id") != request_id
    ):
        return False
    return all(
        isinstance(claim, Mapping)
        and claim_is_valid_for_result(claim, result)
        for claim in claims
    )


def _is_successful_empty_terminal(result: Mapping[str, Any]) -> bool:
    return (
        result.get("status") == "success"
        and result.get("data_state") == "empty"
        and result.get("row_count") == 0
        and not isinstance(result.get("row_count"), bool)
        and result.get("truncated") is False
        and result.get("claim_ledger") == []
    )


def _receipt_evidence_state(result: Mapping[str, Any]) -> str | None:
    if result.get("truncated") is True or result.get("data_state") == "truncated":
        return "truncated"
    state = result.get("data_state")
    if state in {"rows", "zero", "undefined", "empty", "incomplete"}:
        return str(state)
    return None


def _target_overall_result(
    result: Mapping[str, Any],
    result_by_id: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    reconciliation = result.get("target_gap_reconciliation")
    if not isinstance(reconciliation, Mapping):
        return None
    overall_request_id = reconciliation.get("overall_request_id")
    if not isinstance(overall_request_id, str) or not overall_request_id:
        return None
    overall = result_by_id.get(overall_request_id)
    return overall if isinstance(overall, Mapping) else None


def _coverage_receipts(
    request_by_id: Mapping[str, Mapping[str, Any]],
    result_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate opaque proof-shape receipts without copying request or fact values."""

    items: list[dict[str, Any]] = []
    item_by_key: dict[
        tuple[str, str, str, str, tuple[str, ...]], dict[str, Any]
    ] = {}
    for request_id in request_by_id:
        request = request_by_id[request_id]
        result = result_by_id.get(request_id, {})
        fingerprint = _semantic_fingerprint(result)
        evidence_state = _receipt_evidence_state(result)
        scope_fingerprint = result.get("scope_fingerprint")
        expected_fingerprint = semantic_request_fingerprint(
            request,
            scope_fingerprint=(
                scope_fingerprint if isinstance(scope_fingerprint, str) else None
            ),
        )
        has_sealed_claims = _has_only_sealed_claims(result, request_id)
        is_empty_terminal = _is_successful_empty_terminal(result)
        valid_terminal = (
            is_empty_terminal
            if evidence_state == "empty"
            else has_sealed_claims
        )
        if (
            fingerprint is None
            or fingerprint != expected_fingerprint
            or evidence_state is None
            or not valid_terminal
        ):
            continue
        completeness = _completeness(result)
        reconciliation = _reconciliation_status(result)
        supports = _supports(
            result,
            request=request,
            overall_result=_target_overall_result(result, result_by_id),
        )
        key = (
            fingerprint,
            completeness,
            reconciliation,
            evidence_state,
            tuple(supports),
        )
        item = item_by_key.get(key)
        if item is None:
            item = {
                "semantic_request_fingerprint": fingerprint,
                "request_ids": [],
                "evidence_state": evidence_state,
                "completeness": completeness,
                "reconciliation": reconciliation,
                "supports": supports,
            }
            item_by_key[key] = item
            items.append(item)
        item["request_ids"].append(request_id)
    return {
        "version": COVERAGE_RECEIPTS_VERSION,
        "items": items,
    }


def build_evidence_bundle(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a compact proof-coverage index without copying business values."""

    request_by_id = {
        str(request.get("request_id")): request
        for request in requests
        if isinstance(request, Mapping) and isinstance(request.get("request_id"), str)
    }
    result_by_id = {
        str(result.get("request_id")): result
        for result in results
        if isinstance(result, Mapping) and isinstance(result.get("request_id"), str)
    }

    items: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, Any]] = []

    for request_id, request in request_by_id.items():
        result = result_by_id.get(request_id, {})
        item: dict[str, Any] = {
            "request_id": request_id,
            "status": result.get("status", "missing_result"),
            "data_state": result.get("data_state"),
            "completeness": _completeness(result),
            "reconciliation": _reconciliation_status(result),
            "supports": _supports(
                result,
                request=request,
                overall_result=_target_overall_result(result, result_by_id),
            ),
            "limitations": _limitations(
                request,
                result,
                overall_result=_target_overall_result(result, result_by_id),
            ),
        }
        error = result.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("code"), str):
            item["error_code"] = error["code"]
        items.append(item)

        completeness = item["completeness"]
        if completeness in {"failed", "truncated"} or result.get("data_state") == "incomplete":
            gap: dict[str, Any] = {
                "request_id": request_id,
                "reason": (
                    "request_failed"
                    if completeness == "failed"
                    else "source_truncated"
                    if completeness == "truncated"
                    else "data_incomplete"
                ),
            }
            evidence_gaps.append(gap)

    return {
        "version": EVIDENCE_BUNDLE_VERSION,
        "coverage": {
            "request_count": len(request_by_id),
        },
        "coverage_receipts": _coverage_receipts(request_by_id, result_by_id),
        "items": items,
        "evidence_gaps": evidence_gaps,
    }
