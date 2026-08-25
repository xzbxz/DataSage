"""Pure cross-domain capability facts shared by Schema and runtime validation.

This module is deliberately small.  Metric semantics and physical adapters stay
in the versioned semantics files; Hermes remains responsible for planning and
business judgement.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


DOMAIN_SOURCES: dict[str, dict[str, str]] = {
    "delivery": {
        "semantics": "plugins/datasage-query/contracts/delivery-semantics.yaml",
    },
    "receipt": {
        "semantics": "plugins/datasage-query/contracts/receipt-semantics.yaml",
    },
    "receivable": {
        "semantics": "plugins/datasage-query/contracts/receivable-semantics.yaml",
    },
    "target": {
        "semantics": "plugins/datasage-query/contracts/target-semantics.yaml",
    },
    "customer_risk": {
        "semantics": "plugins/datasage-query/contracts/customer_risk-semantics.yaml",
    },
    "inventory": {
        "semantics": "plugins/datasage-query/contracts/inventory-semantics.yaml",
    },
}

SUPPORTED_DOMAINS = tuple(DOMAIN_SOURCES)
ATTRIBUTION_MODES = ("transaction_detail", "salesperson_allocation")
DELIVERY_SCOPES = ("default_net", "explicit_gross", "order_delivery_alignment")
INVENTORY_SCOPES = ("total", "on_hand", "available", "allocated", "in_transit")
PUBLIC_REQUEST_LIMIT = 10
PHYSICAL_EXECUTION_BUDGET = 10
PREVIOUS_PERIOD_COMPARISON = "previous_period"
YEAR_OVER_YEAR_COMPARISON = "year_over_year"
SNAPSHOT_MONTHS_BEFORE_COMPARISON = "snapshot_months_before"
MATCHED_ELAPSED_COVERAGE = "matched_elapsed"
FLOW_COMPARISON_KINDS = (
    PREVIOUS_PERIOD_COMPARISON,
    YEAR_OVER_YEAR_COMPARISON,
)
PUBLIC_COMPARISON_KINDS = (
    *FLOW_COMPARISON_KINDS,
    SNAPSHOT_MONTHS_BEFORE_COMPARISON,
)


class CapabilityContractError(ValueError):
    """A stable, non-sensitive capability-contract validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def query_request_schema_conditions() -> list[dict]:
    """Return JSON Schema conditions generated from cross-domain field facts."""

    conditions = [
        {
            "if": {"properties": {"domain": {"const": "target"}}},
            "then": {
                "required": ["attribution_mode"],
                "properties": {
                    "attribution_mode": {"enum": list(ATTRIBUTION_MODES)}
                },
            },
            "else": {"not": {"required": ["attribution_mode"]}},
        },
        {
            "if": {"required": ["delivery_scope"]},
            "then": {
                "properties": {
                    "domain": {"const": "delivery"},
                    "delivery_scope": {"enum": list(DELIVERY_SCOPES)},
                }
            },
        },
        {
            "if": {"required": ["inventory_scope"]},
            "then": {
                "properties": {
                    "domain": {"const": "inventory"},
                    "inventory_scope": {"enum": list(INVENTORY_SCOPES)},
                }
            },
        },
    ]
    return deepcopy(conditions)


def validate_request_field_contract(request: Mapping[str, Any]) -> None:
    """Validate only cross-domain field ownership and allowed values."""

    if not isinstance(request, Mapping):
        raise CapabilityContractError("INVALID_INPUT", "Request must be an object.")

    domain = request.get("domain")
    if domain not in SUPPORTED_DOMAINS:
        raise CapabilityContractError("INVALID_INPUT", "Unsupported request domain.")

    attribution_mode = request.get("attribution_mode")
    if domain == "target":
        if attribution_mode is None:
            raise CapabilityContractError(
                "ATTRIBUTION_MODE_REQUIRED",
                "Target requests require attribution_mode.",
            )
        if attribution_mode not in ATTRIBUTION_MODES:
            raise CapabilityContractError(
                "INVALID_INPUT", "Unsupported target attribution_mode."
            )
    elif "attribution_mode" in request:
        raise CapabilityContractError(
            "INVALID_INPUT", "attribution_mode is owned by the target domain."
        )

    if "delivery_scope" in request:
        if domain != "delivery":
            raise CapabilityContractError(
                "INVALID_INPUT", "delivery_scope is owned by the delivery domain."
            )
        if request.get("delivery_scope") not in DELIVERY_SCOPES:
            raise CapabilityContractError("INVALID_INPUT", "Unsupported delivery_scope.")

    if "inventory_scope" in request:
        if domain != "inventory":
            raise CapabilityContractError(
                "INVALID_INPUT", "inventory_scope is owned by the inventory domain."
            )
        if request.get("inventory_scope") not in INVENTORY_SCOPES:
            raise CapabilityContractError("INVALID_INPUT", "Unsupported inventory_scope.")


def physical_request_cost(request: Mapping[str, Any]) -> int:
    """Return the physical-slot cost of one public request branch."""

    if not isinstance(request, Mapping):
        raise CapabilityContractError("INVALID_INPUT", "Request must be an object.")
    complete_change = "complete_change_decomposition" in request
    complete_target_gap = "complete_target_gap_decomposition" in request
    if complete_change and complete_target_gap:
        raise CapabilityContractError(
            "INVALID_INPUT", "A request cannot contain both complete operations."
        )
    return 2 if complete_change or complete_target_gap else 1


_FORBIDDEN_CAPABILITY_KEYS = frozenset(
    {
        "triggers",
        "phrases",
        "question_types",
        "intent_routes",
        "route",
        "routes",
        "routing",
        "routing_precedence",
        "metric_selection",
        "metric_selection_boundary",
        "planning_rules",
        "tool_planning",
        "workflow",
        "plan",
        "plans",
        "fixed_plan",
        "recipes",
        "analysis_recipes",
        "bundle",
        "metric_bundle",
        "recommended_bundle",
        "required_metrics",
        "call_order",
        "sequence",
        "fixed_sequence",
        "max_tool_calls",
        "first_call",
        "second_call",
        "fallback",
        "answer_template",
        "template",
        "answer_shape",
        "output_order",
        "narrative_label",
        "threshold",
        "thresholds",
        "causal_explanation",
        "cause",
        "causes",
        "hypothesis",
        "hypotheses",
        "recommendation",
        "recommendations",
    }
)


def assert_capability_boundary(value: Any) -> None:
    """Reject planner judgement embedded in a capability-fact structure."""

    def visit(current: Any, path: tuple[str, ...]) -> None:
        if isinstance(current, Mapping):
            for raw_key, child in current.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                child_path = (*path, str(raw_key))
                if key in _FORBIDDEN_CAPABILITY_KEYS:
                    raise CapabilityContractError(
                        "CAPABILITY_BOUNDARY_VIOLATION",
                        f"Planner-owned field is not a capability fact: {'.'.join(child_path)}",
                    )
                visit(child, child_path)
        elif isinstance(current, (list, tuple)):
            for index, child in enumerate(current):
                visit(child, (*path, str(index)))

    visit(value, ())
