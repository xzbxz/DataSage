"""Model-facing result and calculation projection.

This module owns only the final semantic projection. Evidence sealing and
numeric/ranking helpers remain in evidence.py; transport compaction remains in
wire.py. tools.py keeps compatibility aliases for existing callers.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from . import evidence
from .evidence import _decimal_close, _finite_decimal
from .query_errors import QueryFailure

_FORMAL_DSO_ATTESTATION_VERSION = (
    "formal-receivable-turnover-calculation-attestation/v2"
)
_FORMAL_DSO_ORIGINAL_ATTESTATION_VERSION = (
    "formal-receivable-turnover-original-calculation-attestation/v1"
)

_FORMAL_DSO_COVERAGE_DISCLOSURE = (
    "receivable.formal-receivable-turnover.coverage"
)

_FORMAL_DSO_EXTERNAL_SCOPE_DISCLOSURE = (
    "receivable.formal-receivable-turnover.external-customer.scope"
)

_FORMAL_DSO_FORMULA_DISCLOSURE = (
    "receivable.formal-receivable-turnover.formula"
)

_FORMAL_DSO_ORIGINAL_SCOPE_DISCLOSURE = (
    "receivable.formal-receivable-turnover-original.scope"
)

_FORMAL_DSO_ORIGINAL_EXTERNAL_SCOPE_DISCLOSURE = (
    "receivable.formal-receivable-turnover-original.external-customer.scope"
)

_FORMAL_DSO_ORIGINAL_FORMULA_DISCLOSURE = (
    "receivable.formal-receivable-turnover-original.formula"
)

_FORMAL_DSO_GROSS_DELIVERY_FACT = "same_period_gross_delivery_rmb"
_FORMAL_DSO_ORIGINAL_GROSS_DELIVERY_FACT = "delivery_amount_original"

_FORMAL_DSO_ATTESTED_FACTS = (
    "metric_value",
    "average_net_debt_rmb",
    _FORMAL_DSO_GROSS_DELIVERY_FACT,
    "period_natural_days",
    "snapshot_month_count",
    "effective_month_count",
)
_FORMAL_DSO_ORIGINAL_ATTESTED_FACTS = (
    "metric_value",
    "average_net_debt_original",
    _FORMAL_DSO_ORIGINAL_GROSS_DELIVERY_FACT,
    "period_natural_days",
    "snapshot_month_count",
    "effective_month_count",
)
_FORMAL_DSO_ORIGINAL_COMPONENT_UNITS = {
    "metric_value": "自然日",
    "average_net_debt_original": "原币金额（按币种分别计量）",
    _FORMAL_DSO_ORIGINAL_GROSS_DELIVERY_FACT: "原币金额（按币种分别计量）",
    "period_natural_days": "自然日",
    "snapshot_month_count": "月末快照数",
    "effective_month_count": "有效出库月份数",
}

_MODEL_WIRE_RESULT_FIELDS = (
    "request_id",
    "status",
    "data_state",
    "business_metric_ref",
    "business_metric_label",
    "business_dimension_labels",
    "scope_fingerprint",
    "projection_fingerprint",
    "claim_ledger",
    "disclosure_contract_version",
    "disclosure_ledger",
    "disclosure_ledger_seal",
    "change_reconciliation",
    "target_gap_reconciliation",
    "row_count",
    "truncated",
    "requested_limit",
    "effective_limit",
    "has_more",
    "applied_time_range",
    "currency_scope",
    "error",
)

_MODEL_WIRE_RECONCILIATION_FIELD_ALIASES = {
    "driver_projection_fingerprint": "contributor_projection_fingerprint",
    "driver_current_sum": "contributor_current_sum",
    "driver_comparison_sum": "contributor_comparison_sum",
    "driver_delta_sum": "contributor_delta_sum",
    "returned_driver_row_count": "returned_partition_row_count",
    "unreturned_driver_row_count": "unreturned_partition_row_count",
    "driver_claim_ids": "structural_contributor_claim_ids",
    "driver_row_count": "full_partition_row_count",
    "returned_nonzero_driver_count": "returned_nonzero_contributor_count",
    "nonzero_driver_count_scope": "nonzero_contributor_count_scope",
    "nonzero_driver_count": "nonzero_contributor_count",
}

_FORMAL_DSO_ATTESTATION_GUARDS = (
    "metric_value_present",
    "average_net_debt_present",
    "gross_delivery_denominator_present",
    "gross_delivery_denominator_positive",
    "period_natural_days_present",
    "period_matches_complete_window",
    "complete_natural_month_window",
    "complete_month_end_snapshots",
    "effective_month_count_within_window",
    "coverage_disclosure_sealed",
    "formula_disclosure_sealed",
    "both_external_customer_scopes_disclosed",
)

_FORMAL_DSO_AUTHORIZED_COMPONENTS = (
    "formal_receivable_turnover_value",
    "average_net_debt",
    "same_period_gross_delivery_amount",
    "gross_delivery_denominator_semantics",
    "period_natural_days",
    "snapshot_month_count",
    "effective_month_count",
)

_FORMAL_DSO_PROJECTION_UNDEFINED_REASONS = {
    "ATTESTATION_MISSING",
    "ATTESTATION_INTEGRITY_INVALID",
    "CLAIM_INTEGRITY_INVALID",
    "FORMAL_DSO_BATCH_INCOMPLETE",
}

_MODEL_DISCLOSURE_PROJECTION_VERSION = (
    "metric-disclosure-ledger-model-projection/v1"
)

_MODEL_WIRE_EVIDENCE_INTEGRITY_ERROR = {
    "code": "EVIDENCE_INTEGRITY_INVALID",
    "message": "模型可见证据未通过完整性校验；未密封证据已被剔除。",
}

_MODEL_WIRE_OPTIONAL_METRIC_CONTEXT_FIELDS = {
    "unit_policy": "business_metric_unit_policy",
    "currency_policy": "business_metric_currency_policy",
    "answer_note": "business_metric_answer_note",
}

_MODEL_WIRE_CALCULATION_FIELDS = (
    "calculation_id",
    "operation",
    "status",
    "allowed_relations",
    "relation_semantics",
    "operands",
    "value",
    "unit",
    "scope_compatibility",
    "period_compatibility",
    "limitations",
    "error",
)

_MODEL_WIRE_CALCULATION_OPERAND_FIELDS = (
    "request_id",
    "claim_id",
    "metric_ref",
    "value",
    "unit",
    "period",
    "scope_entities",
    "scope_fingerprint",
    "projection_fingerprint",
    "metric_basis_fingerprint",
    "filter_fingerprint",
    "claim_seal",
)

_MODEL_WIRE_CALCULATION_ERROR_FIELDS = (
    "code",
    "message",
    "retryable",
    "max_retry_attempts",
    "retry_advice",
    "retry_after_seconds",
)

_MODEL_WIRE_SCOPE_COMPATIBILITY_FIELDS = (
    "rule",
    "same_metric_basis",
    "same_filter_scope",
    "period_relation",
    "same_period",
    "numerator_strict_subset_of_denominator",
    "subset_dimensions",
    "same_unit",
    "scalar_untruncated_operands",
)

def _disclosure_is_valid_for_result(
    disclosure: Any,
    *,
    request_id: str,
    metric_ref: str | None,
    scope_fingerprint: str,
    projection_fingerprint: str,
) -> bool:
    """Accept only applicable items after the complete item has been verified."""

    return (
        _disclosure_item_is_valid_for_result(
            disclosure,
            request_id=request_id,
            metric_ref=metric_ref,
            scope_fingerprint=scope_fingerprint,
            projection_fingerprint=projection_fingerprint,
        )
        and disclosure.get("applies") is True
    )

def _disclosure_item_is_valid_for_result(
    disclosure: Any,
    *,
    request_id: str,
    metric_ref: str | None,
    scope_fingerprint: str,
    projection_fingerprint: str,
) -> bool:
    if not isinstance(disclosure, Mapping):
        return False
    expected_item_seal = "sha256_" + hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in disclosure.items()
                if key != "disclosure_seal"
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return (
        disclosure.get("disclosure_seal") == expected_item_seal
        and isinstance(disclosure.get("applies"), bool)
        and isinstance(disclosure.get("disclosure_id"), str)
        and bool(disclosure.get("disclosure_id"))
        and disclosure.get("contract_version") == "metric-disclosure/v1"
        and disclosure.get("request_id") == request_id
        and disclosure.get("metric_ref") == metric_ref
        and disclosure.get("scope_fingerprint") == scope_fingerprint
        and disclosure.get("projection_fingerprint") == projection_fingerprint
    )

def _disclosure_ledger_has_valid_seal(
    ledger: Any,
    ledger_seal: Any,
    *,
    request_id: str,
    metric_ref: str | None,
    scope_fingerprint: str,
    projection_fingerprint: str,
    ledger_contract_version: str,
) -> bool:
    if not isinstance(ledger, list) or not isinstance(ledger_seal, str):
        return False
    expected_ledger_seal = "sha256_" + hashlib.sha256(
        json.dumps(
            {
                "contract_version": ledger_contract_version,
                "request_id": request_id,
                "metric_ref": metric_ref,
                "scope_fingerprint": scope_fingerprint,
                "projection_fingerprint": projection_fingerprint,
                "ledger": ledger,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return ledger_seal == expected_ledger_seal

def _fail_closed_formal_dso_model_wire(projected: dict[str, Any]) -> None:
    claims = projected.get("claim_ledger")
    ledger = projected.get("disclosure_ledger")
    formal_disclosure_ids = {
        _FORMAL_DSO_COVERAGE_DISCLOSURE,
        _FORMAL_DSO_EXTERNAL_SCOPE_DISCLOSURE,
        _FORMAL_DSO_FORMULA_DISCLOSURE,
        _FORMAL_DSO_ORIGINAL_SCOPE_DISCLOSURE,
        _FORMAL_DSO_ORIGINAL_EXTERNAL_SCOPE_DISCLOSURE,
        _FORMAL_DSO_ORIGINAL_FORMULA_DISCLOSURE,
    }
    has_formal_disclosure = isinstance(ledger, list) and any(
        isinstance(item, Mapping)
        and item.get("disclosure_id") in formal_disclosure_ids
        for item in ledger
    )
    has_formal_attestation = isinstance(claims, list) and any(
        isinstance(claim, Mapping)
        and isinstance(claim.get("facts"), Mapping)
        and isinstance(claim["facts"].get("calculation_attestation"), Mapping)
        for claim in claims
    )
    if not has_formal_disclosure and not has_formal_attestation:
        return

    disclosure_contract_version = projected.get("disclosure_contract_version")
    if disclosure_contract_version not in {
        "metric-disclosure-ledger/v1",
        _MODEL_DISCLOSURE_PROJECTION_VERSION,
    }:
        disclosure_contract_version = "invalid"
    sealed_disclosure_ids = _sealed_disclosure_ids(
        ledger,
        projected.get("disclosure_ledger_seal"),
        request_id=str(projected.get("request_id")),
        metric_ref=projected.get("business_metric_ref"),
        scope_fingerprint=str(projected.get("scope_fingerprint")),
        projection_fingerprint=str(projected.get("projection_fingerprint")),
        ledger_contract_version=disclosure_contract_version,
    )
    states: list[tuple[dict[str, Any], str]] = []
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            facts = claim.get("facts")
            attestation = (
                facts.get("calculation_attestation")
                if isinstance(facts, Mapping)
                else None
            )
            states.append(
                (
                    claim,
                    _formal_dso_attestation_state(
                        attestation,
                        claim=claim,
                        result=projected,
                        sealed_disclosure_ids=sealed_disclosure_ids,
                    ),
                )
            )
    if states and all(state == "verified" for _, state in states):
        return

    projected["data_state"] = "undefined"
    if not states or any(state != "undefined" for _, state in states):
        projected["claim_ledger"] = []
        _mark_model_wire_evidence_integrity_failure(projected)
    else:
        for claim, _state in states:
            facts = claim.get("facts")
            if isinstance(facts, dict):
                facts.pop("metric_value", None)
            evidence.seal_claim(claim)
        projected["row_count"] = len(states)
        projected.pop("change_reconciliation", None)
        projected.pop("target_gap_reconciliation", None)

    if isinstance(ledger, list):
        projected["disclosure_ledger"] = [
            item
            for item in ledger
            if isinstance(item, Mapping)
            and item.get("disclosure_id") in sealed_disclosure_ids
            and item.get("disclosure_id")
            not in {
                _FORMAL_DSO_FORMULA_DISCLOSURE,
                _FORMAL_DSO_ORIGINAL_FORMULA_DISCLOSURE,
            }
        ]
        _reseal_model_disclosure_ledger(projected)

def _fail_closed_period_comparison_model_wire(projected: dict[str, Any]) -> None:
    """Project raw period observations without an unauthorized derived comparison."""

    period = projected.get("applied_time_range")
    compatibility = (
        period.get("comparison_compatibility")
        if isinstance(period, Mapping)
        else None
    )
    if (
        not isinstance(compatibility, Mapping)
        or compatibility.get("status") == "compatible"
    ):
        return
    claims = projected.get("claim_ledger")
    if not isinstance(claims, list):
        return
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        relations = claim.get("allowed_relations")
        if isinstance(relations, list):
            claim["allowed_relations"] = [
                relation
                for relation in relations
                if relation != "period_comparison"
            ]
        facts = claim.get("facts")
        if isinstance(facts, dict):
            facts.pop("delta_value", None)
            facts.pop("change_rate", None)
        evidence.seal_claim(claim)

def _fail_closed_structural_model_wire(projected: dict[str, Any]) -> None:
    """Reject structural claims whose raw-key reconciliation is not valid."""

    claims = projected.get("claim_ledger")
    has_structural_claim = isinstance(claims, list) and any(
        isinstance(claim, Mapping)
        and "structural_contribution" in claim.get("allowed_relations", [])
        for claim in claims
    )
    reconciliation = projected.get("change_reconciliation")
    invalid_reconciled_receipt = (
        isinstance(reconciliation, Mapping)
        and reconciliation.get("status") == "reconciled"
        and evidence._reconciliation_status(projected)
        == "invalid_reconciliation"
    )
    if (
        has_structural_claim
        and "structural_contribution" not in evidence._supports(projected)
    ) or invalid_reconciled_receipt:
        projected["claim_ledger"] = []
        _mark_model_wire_evidence_integrity_failure(projected)

def _fail_closed_target_gap_model_wire(
    projected: dict[str, Any],
    *,
    validation_result: Mapping[str, Any] | None = None,
    request: Mapping[str, Any] | None = None,
    overall_result: Mapping[str, Any] | None = None,
) -> None:
    """Never expose a reconciled target-gap receipt that fails its full binding."""

    reconciliation = projected.get("target_gap_reconciliation")
    if not isinstance(reconciliation, Mapping):
        return
    if reconciliation.get("status") != "reconciled":
        projected.pop("target_gap_reconciliation", None)
        return
    if evidence._target_gap_reconciliation_is_valid(
        validation_result if isinstance(validation_result, Mapping) else projected,
        request=request,
        overall_result=overall_result,
    ):
        return
    projected.pop("target_gap_reconciliation", None)
    projected["claim_ledger"] = []
    _mark_model_wire_evidence_integrity_failure(projected)

def _filter_model_wire_evidence(projected: dict[str, Any]) -> None:
    """Expose only sealed, result-bound claims and applicable disclosures."""

    if projected.get("status") != "success":
        # A local failure carries no business evidence to validate. Preserve
        # its original typed error while making it impossible for stale proof
        # fields to leak from a partially constructed execution result.
        projected["claim_ledger"] = []
        projected["disclosure_ledger"] = []
        projected.pop("disclosure_contract_version", None)
        projected.pop("disclosure_ledger_seal", None)
        projected.pop("change_reconciliation", None)
        projected.pop("target_gap_reconciliation", None)
        return

    claims = projected.get("claim_ledger")
    claim_ledger_present = "claim_ledger" in projected
    if claim_ledger_present:
        original_count = len(claims) if isinstance(claims, list) else 0
        valid_claims = (
            [
                claim
                for claim in claims
                if isinstance(claim, Mapping)
                and evidence.claim_is_valid_for_result(claim, projected)
            ]
            if isinstance(claims, list)
            else []
        )
        row_count = projected.get("row_count")
        data_state = projected.get("data_state")
        truncated = projected.get("truncated")
        success_shape_valid = True
        if projected.get("status") == "success":
            if data_state == "empty":
                success_shape_valid = (
                    original_count == 0
                    and row_count == 0
                    and truncated is False
                )
            else:
                success_shape_valid = (
                    data_state
                    in {
                        "complete",
                        "rows",
                        "zero",
                        "undefined",
                        "truncated",
                        "incomplete",
                    }
                    and original_count > 0
                    and isinstance(truncated, bool)
                    and ((data_state == "truncated") is truncated)
                )
        claim_integrity_failed = (
            not isinstance(claims, list)
            or len(valid_claims) != original_count
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count != original_count
            or not success_shape_valid
        )
        projected["claim_ledger"] = valid_claims
        if claim_integrity_failed:
            _mark_model_wire_evidence_integrity_failure(projected)
    elif projected.get("status") == "success":
        projected["claim_ledger"] = []
        _mark_model_wire_evidence_integrity_failure(projected)

    disclosure_fields = {
        "disclosure_contract_version",
        "disclosure_ledger",
        "disclosure_ledger_seal",
    }
    present_disclosure_fields = disclosure_fields.intersection(projected)
    if not present_disclosure_fields:
        if projected.get("status") == "success":
            projected["disclosure_ledger"] = []
            projected["claim_ledger"] = []
            _mark_model_wire_evidence_integrity_failure(projected)
            _reseal_model_disclosure_ledger(projected)
        return

    ledger = projected.get("disclosure_ledger")
    contract_version = projected.get("disclosure_contract_version")
    valid_contract_versions = {
        "metric-disclosure-ledger/v1",
        _MODEL_DISCLOSURE_PROJECTION_VERSION,
    }
    common_binding_valid = (
        isinstance(projected.get("request_id"), str)
        and bool(projected.get("request_id"))
        and isinstance(projected.get("business_metric_ref"), str)
        and bool(projected.get("business_metric_ref"))
        and isinstance(projected.get("scope_fingerprint"), str)
        and bool(projected.get("scope_fingerprint"))
        and isinstance(projected.get("projection_fingerprint"), str)
        and bool(projected.get("projection_fingerprint"))
    )
    ledger_sealed = (
        present_disclosure_fields == disclosure_fields
        and common_binding_valid
        and contract_version in valid_contract_versions
        and _disclosure_ledger_has_valid_seal(
            ledger,
            projected.get("disclosure_ledger_seal"),
            request_id=projected["request_id"],
            metric_ref=projected["business_metric_ref"],
            scope_fingerprint=projected["scope_fingerprint"],
            projection_fingerprint=projected["projection_fingerprint"],
            ledger_contract_version=contract_version,
        )
    )
    valid_disclosures: list[Mapping[str, Any]] = []
    all_items_valid = False
    all_disclosure_ids: list[str] = []
    if ledger_sealed and isinstance(ledger, list):
        all_items_valid = all(
            _disclosure_item_is_valid_for_result(
                item,
                request_id=projected["request_id"],
                metric_ref=projected["business_metric_ref"],
                scope_fingerprint=projected["scope_fingerprint"],
                projection_fingerprint=projected["projection_fingerprint"],
            )
            for item in ledger
        )
        for item in ledger:
            if isinstance(item, Mapping) and isinstance(
                item.get("disclosure_id"),
                str,
            ):
                all_disclosure_ids.append(item["disclosure_id"])
            if (
                all_items_valid
                and isinstance(item, Mapping)
                and item.get("applies") is True
            ):
                valid_disclosures.append(item)
    disclosure_integrity_failed = (
        not ledger_sealed
        or not all_items_valid
        or len(set(all_disclosure_ids)) != len(all_disclosure_ids)
    )
    projected["disclosure_ledger"] = valid_disclosures
    if disclosure_integrity_failed:
        projected["claim_ledger"] = []
        _mark_model_wire_evidence_integrity_failure(projected)
    if (
        not ledger_sealed
        or not isinstance(ledger, list)
        or len(valid_disclosures) != len(ledger)
    ):
        _reseal_model_disclosure_ledger(projected)

def _finite_decimal_present(value: Any) -> bool:
    return _finite_decimal(value) is not None

def _formal_dso_attestation_seal(attestation: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {
            key: value
            for key, value in attestation.items()
            if key != "attestation_seal"
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256_" + hashlib.sha256(canonical).hexdigest()

def _formal_dso_attestation_state(
    attestation: Any,
    *,
    claim: Mapping[str, Any],
    result: Mapping[str, Any],
    sealed_disclosure_ids: set[str],
) -> str:
    """Return verified/undefined only for a complete sealed v2 statement."""

    original_basis = isinstance(attestation, Mapping) and attestation.get("currency_basis") == "original"
    expected_version = (
        _FORMAL_DSO_ORIGINAL_ATTESTATION_VERSION
        if original_basis
        else _FORMAL_DSO_ATTESTATION_VERSION
    )
    expected_facts = (
        _FORMAL_DSO_ORIGINAL_ATTESTED_FACTS
        if original_basis
        else _FORMAL_DSO_ATTESTED_FACTS
    )

    if (
        not isinstance(attestation, Mapping)
        or attestation.get("contract_version") != expected_version
        or attestation.get("attestation_seal")
        != _formal_dso_attestation_seal(attestation)
        or not evidence.claim_is_valid_for_result(claim, result)
        or attestation.get("request_id") != result.get("request_id")
        or attestation.get("request_id") != claim.get("request_id")
        or attestation.get("metric_ref") != result.get("business_metric_ref")
        or attestation.get("metric_ref") != claim.get("metric_ref")
        or attestation.get("scope_fingerprint")
        != result.get("scope_fingerprint")
        or attestation.get("scope_fingerprint")
        != claim.get("scope_fingerprint")
        or attestation.get("projection_fingerprint")
        != result.get("projection_fingerprint")
        or attestation.get("projection_fingerprint")
        != claim.get("projection_fingerprint")
        or claim.get("period") != result.get("applied_time_range")
        or (
            isinstance(result.get("currency_scope"), Mapping)
            and not original_basis
        )
        or (
            original_basis
            and attestation.get("currency_scope") != result.get("currency_scope")
        )
    ):
        return "invalid"

    status = attestation.get("status")
    guards = attestation.get("guards")
    components = attestation.get("authorized_components")
    component_values = attestation.get("component_values")
    reasons = attestation.get("undefined_reason_codes")
    facts = claim.get("facts")
    if (
        status == "undefined"
        and guards == {}
        and components == []
        and component_values == {}
        and isinstance(reasons, list)
        and len(reasons) == 1
        and reasons[0] in _FORMAL_DSO_PROJECTION_UNDEFINED_REASONS
    ):
        return "undefined"
    if (
        not isinstance(guards, Mapping)
        or tuple(guards) != _FORMAL_DSO_ATTESTATION_GUARDS
        or any(not isinstance(value, bool) for value in guards.values())
        or not isinstance(components, list)
        or not isinstance(reasons, list)
        or any(not isinstance(reason, str) or not reason for reason in reasons)
    ):
        return "invalid"
    if status == "verified":
        if original_basis:
            expected_units = _FORMAL_DSO_ORIGINAL_COMPONENT_UNITS
            component_units = attestation.get("component_units")
            fact_units = claim.get("fact_units")
            if (
                component_units != expected_units
                or not isinstance(fact_units, Mapping)
                or any(
                    fact_units.get(name) != unit
                    for name, unit in expected_units.items()
                )
            ):
                return "invalid"
        required_disclosures = {
            _FORMAL_DSO_COVERAGE_DISCLOSURE,
            _FORMAL_DSO_EXTERNAL_SCOPE_DISCLOSURE,
            _FORMAL_DSO_FORMULA_DISCLOSURE,
        }
        if (
            all(guards.values())
            and tuple(components) == _FORMAL_DSO_AUTHORIZED_COMPONENTS
            and reasons == []
            and _formal_dso_attested_components_are_valid(
                component_values,
                facts=facts,
                applied_time_range=result.get("applied_time_range"),
                attested_facts=expected_facts,
            )
            and (
                (
                    {
                        _FORMAL_DSO_ORIGINAL_SCOPE_DISCLOSURE,
                        _FORMAL_DSO_ORIGINAL_EXTERNAL_SCOPE_DISCLOSURE,
                        _FORMAL_DSO_ORIGINAL_FORMULA_DISCLOSURE,
                    }
                    if original_basis
                    else required_disclosures
                ).issubset(sealed_disclosure_ids)
            )
        ):
            return "verified"
        return "invalid"
    if status == "undefined":
        expected_reasons = [
            key.upper() for key, passed in guards.items() if passed is not True
        ]
        if (
            expected_reasons
            and reasons == expected_reasons
            and components == []
            and component_values == {}
        ):
            return "undefined"
    return "invalid"

def _formal_dso_attested_components_are_valid(
    component_values: Any,
    *,
    facts: Any,
    applied_time_range: Any,
    attested_facts: Sequence[str] = _FORMAL_DSO_ATTESTED_FACTS,
) -> bool:
    """Verify exact claim copies and formal-DSO component business ranges."""

    if (
        not isinstance(component_values, Mapping)
        or set(component_values) != set(attested_facts)
        or not isinstance(facts, Mapping)
        or any(
            fact_name not in facts
            or facts[fact_name] != component_values[fact_name]
            for fact_name in attested_facts
        )
        or not _finite_decimal_present(component_values.get("metric_value"))
        or not any(
            _finite_decimal_present(component_values.get(field))
            for field in ("average_net_debt_rmb", "average_net_debt_original")
        )
    ):
        return False

    gross_field = (
        _FORMAL_DSO_ORIGINAL_GROSS_DELIVERY_FACT
        if "average_net_debt_original" in attested_facts
        else _FORMAL_DSO_GROSS_DELIVERY_FACT
    )
    average_field = (
        "average_net_debt_original"
        if "average_net_debt_original" in attested_facts
        else "average_net_debt_rmb"
    )
    gross_delivery = _finite_decimal(component_values.get(gross_field))
    metric_value = _finite_decimal(component_values.get("metric_value"))
    average_net_debt = _finite_decimal(
        component_values.get(average_field)
    )
    effective_month_count = _finite_decimal(
        component_values.get("effective_month_count")
    )
    period_days = component_values.get("period_natural_days")
    snapshot_month_count = component_values.get("snapshot_month_count")
    expected_months, expected_period_days = _formal_dso_window_requirements(
        applied_time_range
    )
    formula_value = (
        average_net_debt * Decimal(period_days) / gross_delivery
        if average_net_debt is not None
        and gross_delivery is not None
        and gross_delivery > 0
        and isinstance(period_days, int)
        and not isinstance(period_days, bool)
        else None
    )
    return (
        metric_value is not None
        and average_net_debt is not None
        and gross_delivery is not None
        and gross_delivery > 0
        and isinstance(period_days, int)
        and not isinstance(period_days, bool)
        and period_days > 0
        and expected_months is not None
        and period_days == expected_period_days
        and isinstance(snapshot_month_count, int)
        and not isinstance(snapshot_month_count, bool)
        and snapshot_month_count == expected_months + 1
        and effective_month_count is not None
        and effective_month_count == effective_month_count.to_integral_value()
        and 1 <= effective_month_count <= expected_months
        and formula_value is not None
        and _decimal_close(metric_value, formula_value)
    )

def _formal_dso_window_requirements(value: Any) -> tuple[int | None, int | None]:
    """Derive required month coverage and days from the applied date boundaries."""
    if not isinstance(value, Mapping):
        return None, None
    try:
        start = date.fromisoformat(str(value.get("start")))
        end = date.fromisoformat(str(value.get("end")))
    except (TypeError, ValueError):
        return None, None
    months = (end.year - start.year) * 12 + end.month - start.month
    if start.day != 1 or end.day != 1 or months < 1:
        return None, None
    return months, (end - start).days

def _mark_model_wire_evidence_integrity_failure(
    projected: dict[str, Any],
) -> None:
    claims = projected.get("claim_ledger")
    visible_count = len(claims) if isinstance(claims, list) else 0
    projected["data_state"] = "incomplete" if visible_count else "undefined"
    projected["row_count"] = visible_count
    projected.pop("change_reconciliation", None)
    projected.pop("target_gap_reconciliation", None)
    projected["error"] = dict(_MODEL_WIRE_EVIDENCE_INTEGRITY_ERROR)

def _model_wire_calculation_integrity_failure(
    calculation: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep a typed failure while removing every dependent evidence field."""

    return {
        "calculation_id": calculation.get("calculation_id"),
        "operation": calculation.get("operation"),
        "status": "failed",
        "error": {
            "code": "CALCULATION_SOURCE_INTEGRITY_INVALID",
            "message": "计算引用的查询证据未通过完整性校验。",
            "retryable": False,
        },
    }

def _model_wire_calculation_period_failure(
    calculation: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep source observations visible while withholding an unauthorized comparison."""

    compatibility = calculation.get("period_compatibility")
    reason_codes = (
        [
            str(reason)
            for reason in compatibility.get("reason_codes", [])
            if isinstance(reason, str) and reason
        ]
        if isinstance(compatibility, Mapping)
        else []
    )
    if not reason_codes:
        reason_codes = ["PERIOD_COMPARABILITY_NOT_ASSESSABLE"]
    operands = calculation.get("operands")
    return {
        "calculation_id": calculation.get("calculation_id"),
        "operation": calculation.get("operation"),
        "status": "failed",
        "allowed_relations": [],
        "operands": [
            {"request_id": operand["request_id"]}
            for operand in operands
            if isinstance(operand, Mapping)
            and isinstance(operand.get("request_id"), str)
        ]
        if isinstance(operands, list)
        else [],
        "period_compatibility": {
            "status": (
                compatibility.get("status")
                if isinstance(compatibility, Mapping)
                else "not_assessable"
            ),
            "reason_codes": reason_codes,
        },
        "limitations": sorted(
            {
                *[
                    str(item)
                    for item in calculation.get("limitations", [])
                    if isinstance(item, str) and item
                ],
                *reason_codes,
            }
        ),
        "error": {
            "code": reason_codes[0],
            "message": "期间证据不授权把该算术结果作为正式期间比较。",
            "retryable": False,
        },
    }

def _model_wire_calculation_projection(
    calculation: Mapping[str, Any],
    *,
    seal_success: bool,
) -> dict[str, Any]:
    """Project only answer-contract fields, including nested calculation objects."""

    projected: dict[str, Any] = {}
    for field in _MODEL_WIRE_CALCULATION_FIELDS:
        if field not in calculation:
            continue
        value = calculation[field]
        if field == "operands":
            if isinstance(value, list):
                projected[field] = [
                    {
                        key: copy.deepcopy(operand[key])
                        for key in _MODEL_WIRE_CALCULATION_OPERAND_FIELDS
                        if key in operand
                    }
                    for operand in value
                    if isinstance(operand, Mapping)
                ]
            continue
        if field == "error":
            if value is None:
                projected[field] = None
            elif isinstance(value, Mapping):
                projected[field] = {
                    key: copy.deepcopy(value[key])
                    for key in _MODEL_WIRE_CALCULATION_ERROR_FIELDS
                    if key in value
                }
            continue
        if field == "relation_semantics":
            if isinstance(value, Mapping) and "derived_observation" in value:
                projected[field] = {
                    "derived_observation": copy.deepcopy(
                        value["derived_observation"]
                    )
                }
            continue
        if field == "scope_compatibility":
            if isinstance(value, Mapping):
                projected[field] = {
                    key: copy.deepcopy(value[key])
                    for key in _MODEL_WIRE_SCOPE_COMPATIBILITY_FIELDS
                    if key in value
                }
            continue
        if field == "period_compatibility":
            if isinstance(value, Mapping):
                projected[field] = {
                    key: copy.deepcopy(value[key])
                    for key in ("status", "reason_codes")
                    if key in value
                }
            continue
        projected[field] = copy.deepcopy(value)
    if seal_success:
        canonical = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        projected["calculation_seal"] = (
            "sha256_" + hashlib.sha256(canonical).hexdigest()
        )
    return projected

def _model_wire_calculations(
    calculations: Sequence[Mapping[str, Any]],
    public_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Expose calculations only while every sealed source claim remains visible."""

    visible_claims: dict[tuple[str, str], Mapping[str, Any]] = {}
    for result in public_results:
        request_id = result.get("request_id")
        claims = result.get("claim_ledger")
        if (
            result.get("status") != "success"
            or not isinstance(request_id, str)
            or not isinstance(claims, list)
        ):
            continue
        for claim in claims:
            if (
                not isinstance(claim, Mapping)
                or not evidence.claim_is_valid_for_result(claim, result)
            ):
                continue
            claim_id = claim.get("claim_id")
            if isinstance(claim_id, str):
                visible_claims[(request_id, claim_id)] = claim

    projected: list[dict[str, Any]] = []
    for calculation in calculations:
        if calculation.get("status") != "success":
            error = calculation.get("error")
            if (
                isinstance(error, Mapping)
                and error.get("code") == "CALCULATION_SOURCE_INTEGRITY_INVALID"
            ):
                projected.append(
                    _model_wire_calculation_integrity_failure(calculation)
                )
                continue
            projected.append(
                _model_wire_calculation_projection(
                    calculation,
                    seal_success=False,
                )
            )
            continue

        operands = calculation.get("operands")
        if (
            not _calculation_has_valid_seal(calculation)
            or not isinstance(operands, list)
            or len(operands) != 2
        ):
            projected.append(
                _model_wire_calculation_integrity_failure(calculation)
            )
            continue
        valid = True
        for operand in operands:
            if not isinstance(operand, Mapping):
                valid = False
                break
            request_id = operand.get("request_id")
            claim_id = operand.get("claim_id")
            claim = (
                visible_claims.get((request_id, claim_id))
                if isinstance(request_id, str) and isinstance(claim_id, str)
                else None
            )
            facts = claim.get("facts") if isinstance(claim, Mapping) else None
            if (
                not isinstance(claim, Mapping)
                or not isinstance(facts, Mapping)
                or operand.get("claim_seal") != claim.get("claim_seal")
                or operand.get("metric_ref") != claim.get("metric_ref")
                or operand.get("value") != facts.get("metric_value")
                or operand.get("unit") != claim.get("unit")
                or operand.get("period") != claim.get("period")
                or operand.get("scope_entities") != claim.get("scope_entities")
                or operand.get("scope_fingerprint")
                != claim.get("scope_fingerprint")
                or operand.get("projection_fingerprint")
                != claim.get("projection_fingerprint")
            ):
                valid = False
                break
        if valid:
            period_compatibility = calculation.get("period_compatibility")
            if (
                not isinstance(period_compatibility, Mapping)
                or period_compatibility.get("status") != "compatible"
            ):
                projected.append(
                    _model_wire_calculation_period_failure(calculation)
                )
            else:
                projected.append(
                    _model_wire_calculation_projection(
                        calculation,
                        seal_success=True,
                    )
                )
        else:
            projected.append(
                _model_wire_calculation_integrity_failure(calculation)
            )
    return projected

def _model_wire_change_reconciliation(value: Any) -> Any:
    """Rename legacy reconciliation vocabulary only at the model boundary."""

    if not isinstance(value, Mapping):
        return value
    return {
        _MODEL_WIRE_RECONCILIATION_FIELD_ALIASES.get(key, key): item
        for key, item in value.items()
    }

def _model_wire_evidence_bundle_results(
    raw_results: Sequence[Mapping[str, Any]],
    public_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind bundle capability summaries to the evidence surviving projection."""

    if len(raw_results) != len(public_results):
        raise QueryFailure(
            "INTERNAL_ERROR",
            "模型证据投影数量不一致。",
            stage="result_validation",
        )
    evidence_results: list[dict[str, Any]] = []
    projected_state_fields = {
        "request_id",
        "status",
        "data_state",
        "business_metric_ref",
        "scope_fingerprint",
        "projection_fingerprint",
        "claim_ledger",
        "row_count",
        "truncated",
        "error",
    }
    for raw, public in zip(raw_results, public_results):
        result = copy.deepcopy(dict(raw))
        for field in projected_state_fields:
            if field in public:
                result[field] = copy.deepcopy(public[field])
            else:
                result.pop(field, None)
        # Keep the private reconciliation representation only when the public
        # projection retained the corresponding proof. Its canonical seal uses
        # legacy internal field names that are renamed only for model display.
        for proof_field in ("change_reconciliation", "target_gap_reconciliation"):
            if proof_field not in public:
                result.pop(proof_field, None)
        evidence_results.append(result)
    return evidence_results

def _model_wire_metric_contexts(
    results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate safe business meaning without repeating it in every result."""

    contexts_by_ref: dict[str, dict[str, Any]] = {}
    ordered_refs: list[str] = []
    for result in results:
        if result.get("status") != "success":
            continue
        metric_ref = result.get("business_metric_ref")
        label = result.get("business_metric_label")
        if not isinstance(metric_ref, str) or not metric_ref.strip():
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少业务指标引用。")
        if not isinstance(label, str) or not label.strip():
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少业务指标名称。")
        metric_ref = metric_ref.strip()
        context: dict[str, Any] = {
            "business_metric_ref": metric_ref,
            "label": label.strip(),
            "definition": (
                result["business_metric_definition"].strip()
                if isinstance(result.get("business_metric_definition"), str)
                and result["business_metric_definition"].strip()
                else None
            ),
            "unit": (
                result["business_metric_unit"].strip()
                if isinstance(result.get("business_metric_unit"), str)
                and result["business_metric_unit"].strip()
                else None
            ),
        }
        for public_field, private_field in (
            _MODEL_WIRE_OPTIONAL_METRIC_CONTEXT_FIELDS.items()
        ):
            value = result.get(private_field)
            if isinstance(value, str) and value.strip():
                context[public_field] = value.strip()
        previous = contexts_by_ref.get(metric_ref)
        if previous is None:
            contexts_by_ref[metric_ref] = context
            ordered_refs.append(metric_ref)
        elif previous != context:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "同一业务指标引用的语义上下文不一致。",
            )
    return [contexts_by_ref[metric_ref] for metric_ref in ordered_refs]

def _model_wire_result(
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
    overall_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project private execution state to the minimal model-visible result."""

    projected = {
        field: copy.deepcopy(result.get(field))
        for field in _MODEL_WIRE_RESULT_FIELDS
        if field in result
    }
    _filter_model_wire_evidence(projected)
    _fail_closed_period_comparison_model_wire(projected)
    _fail_closed_formal_dso_model_wire(projected)
    _fail_closed_target_gap_model_wire(
        projected,
        validation_result=result,
        request=request,
        overall_result=overall_result,
    )
    _fail_closed_structural_model_wire(projected)
    if "change_reconciliation" in projected:
        projected["change_reconciliation"] = _model_wire_change_reconciliation(
            projected["change_reconciliation"]
        )
    if (
        isinstance(projected.get("error"), Mapping)
        and projected["error"].get("code") == "EVIDENCE_INTEGRITY_INVALID"
    ):
        projected["status"] = "partial" if projected.get("claim_ledger") else "failed"
    if request is not None and projected.get("status") == "success":
        resolved_request = result.get("_resolved_currency_request")
        numeric_request = resolved_request if isinstance(resolved_request, Mapping) else request
        numeric = evidence.build_numeric_evidence(projected, numeric_request, result.get("_period_additive_fields", []))
        if numeric:
            projected["numeric_evidence"] = numeric
        ranking = evidence.build_ranking_evidence(projected, result.get("_ranking_plan"))
        if ranking:
            projected["ranking_evidence"] = ranking
        elif result.get("_ranking_plan"):
            projected["ranking_evidence"] = {"status": "unavailable", "reason": "RANK_PROOF_INVALID", **result["_ranking_plan"]}
    return projected

def _reseal_model_disclosure_ledger(projected: dict[str, Any]) -> None:
    ledger = projected.get("disclosure_ledger")
    if not isinstance(ledger, list):
        return
    canonical = json.dumps(
        {
            "contract_version": _MODEL_DISCLOSURE_PROJECTION_VERSION,
            "request_id": projected.get("request_id"),
            "metric_ref": projected.get("business_metric_ref"),
            "scope_fingerprint": projected.get("scope_fingerprint"),
            "projection_fingerprint": projected.get("projection_fingerprint"),
            "ledger": ledger,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    projected["disclosure_contract_version"] = (
        _MODEL_DISCLOSURE_PROJECTION_VERSION
    )
    projected["disclosure_ledger_seal"] = (
        "sha256_" + hashlib.sha256(canonical).hexdigest()
    )

def _sealed_disclosure_ids(
    ledger: Any,
    ledger_seal: Any,
    *,
    request_id: str,
    metric_ref: str | None,
    scope_fingerprint: str,
    projection_fingerprint: str,
    ledger_contract_version: str = "metric-disclosure-ledger/v1",
) -> set[str]:
    if not _disclosure_ledger_has_valid_seal(
        ledger,
        ledger_seal,
        request_id=request_id,
        metric_ref=metric_ref,
        scope_fingerprint=scope_fingerprint,
        projection_fingerprint=projection_fingerprint,
        ledger_contract_version=ledger_contract_version,
    ):
        return set()
    sealed_ids: set[str] = set()
    for disclosure in ledger:
        if not _disclosure_is_valid_for_result(
            disclosure,
            request_id=request_id,
            metric_ref=metric_ref,
            scope_fingerprint=scope_fingerprint,
            projection_fingerprint=projection_fingerprint,
        ):
            continue
        sealed_ids.add(disclosure["disclosure_id"])
    return sealed_ids

def _calculation_has_valid_seal(calculation: Mapping[str, Any]) -> bool:
    seal = calculation.get("calculation_seal")
    if not isinstance(seal, str):
        return False
    canonical = json.dumps(
        {
            key: value
            for key, value in calculation.items()
            if key != "calculation_seal"
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return seal == "sha256_" + hashlib.sha256(canonical).hexdigest()
