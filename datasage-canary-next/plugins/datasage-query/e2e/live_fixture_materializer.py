"""Model-free, bounded live-fixture selection for trusted replay.

Selector plaintext is returned only to the in-memory caller. Durable metadata
contains salted fingerprints and source commitments, never selected labels.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Callable, Mapping


SCHEMA = "datasage-live-fixture-materialization/v1"
ALGORITHM_VERSION = "datasage-live-fixture-selector/v1"
CONTEXT_FINGERPRINT_SCHEMA = "datasage-context-binding-fingerprint/v1"
MAX_FACADE_CALLS = 4
ALLOWED_RECIPES = frozenset(
    {"delivery_organization", "delivery_region_pair"}
)
RECIPE_DIMENSIONS = {
    "delivery_organization": "organization",
    "delivery_region_pair": "customer_region",
}
SLOT_DECLARATIONS = {
    "change_01_delivery_department:organization": {
        "case_ids": ["change_01_delivery_department"],
        "placeholder": "PLACEHOLDER_CHANGE_ORGANIZATION",
        "canonical_placeholder": "示例A区",
        "recipe": "delivery_organization",
        "slot_type": "source_exact",
    },
    "multiturn_region:region_a": {
        "case_ids": ["multiturn_01_base"],
        "placeholder": "PLACEHOLDER_REGION_A",
        "canonical_placeholder": "示例A区",
        "recipe": "delivery_region_pair",
        "slot_type": "source_exact",
    },
    "multiturn_region:region_b": {
        "case_ids": ["multiturn_02_replace_entity"],
        "placeholder": "PLACEHOLDER_REGION_B",
        "canonical_placeholder": "示例B区",
        "recipe": "delivery_region_pair",
        "slot_type": "source_exact",
    },
}
_SOURCE_SCHEMA = "datasage-query-source-evidence/v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class LiveFixtureUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.classification = "environment-not-executable"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _source_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiveFixtureUnavailable("LIVE_FIXTURE_SOURCE_EVIDENCE_MISSING")
    reference = dict(value)
    schema = reference.get("schema")
    common = (
        _HEX64.fullmatch(str(reference.get("identity_sha256") or ""))
        and reference.get("connection_verified") is True
        and reference.get("grants_verified") is True
        and reference.get("read_only") is True
    )
    current = (
        schema == _SOURCE_SCHEMA
        and reference.get("transport_policy_verified") is True
        and reference.get("transport_mode") in {"tls", "plaintext"}
        and reference.get("grant_policy")
        in {"strict_object_read_only", "user_accepted_canary_existing_account"}
        and _HEX64.fullmatch(str(reference.get("source_commitment_sha256") or ""))
        and _HEX64.fullmatch(str(reference.get("security_evidence_sha256") or ""))
    )
    if not common or not current:
        raise LiveFixtureUnavailable("LIVE_FIXTURE_SOURCE_EVIDENCE_MISSING")
    return reference


def _invoke(
    invoke_facade: Callable[[str, dict[str, Any]], Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if tool_name not in {"datasage_catalog", "datasage_query"}:
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    if set(arguments) - {"requests", "calculations"}:
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    raw = invoke_facade(tool_name, copy.deepcopy(arguments))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE") from exc
    if not isinstance(raw, dict) or raw.get("status") not in {"success", "partial"}:
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    return raw


def _candidate_rows(payload: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    for result in payload.get("results") or []:
        if not isinstance(result, Mapping) or result.get("status") != "success":
            continue
        expected_labels = {
            label
            for label in result.get("business_dimension_labels") or []
            if isinstance(label, str) and label
        }
        for row in result.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            for key in ("selector", "organization", "department", "display_name"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    labels.append(value.strip())
                    break
        for claim in result.get("claim_ledger") or []:
            if not isinstance(claim, Mapping):
                continue
            for dimension in claim.get("dimensions") or []:
                label = dimension.get("label") if isinstance(dimension, Mapping) else None
                value = dimension.get("value") if isinstance(dimension, Mapping) else None
                if (
                    label in expected_labels
                    and isinstance(value, str)
                    and value.strip()
                ):
                    labels.append(value.strip())
    return sorted(set(labels), key=lambda item: _sha({"selector": item}))


def _replace(case: dict[str, Any], placeholders: tuple[str, ...], selector: str) -> None:
    prompt = case.get("prompt", "")
    matches = [placeholder for placeholder in placeholders if placeholder in prompt]
    if len(matches) != 1:
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    case["prompt"] = prompt.replace(matches[0], selector)


def _bind_expected_plan(case: dict[str, Any], slot: str, selector: str) -> None:
    plan = case.get("expected_plan")
    if not isinstance(plan, dict):
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    before = copy.deepcopy(plan)
    bindings = copy.deepcopy(plan.get("context_bindings") or {})
    if not isinstance(bindings, dict):
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    binding_name = (
        "organization"
        if slot == "change_01_delivery_department:organization"
        else "department"
    )
    bindings[binding_name] = {
        "schema": CONTEXT_FINGERPRINT_SCHEMA,
        "sha256": hashlib.sha256(selector.encode("utf-8")).hexdigest(),
    }
    plan["context_bindings"] = bindings
    for field, value in before.items():
        if field != "context_bindings" and plan.get(field) != value:
            raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")


def _query_arguments(recipe: str) -> dict[str, Any]:
    dimension = RECIPE_DIMENSIONS.get(recipe)
    if dimension is None:
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    request = {
        "request_id": f"live_fixture_{recipe}",
        "domain": "delivery",
        "mode": "metric",
        "purpose": "bounded live fixture source-exact discovery",
        "metric": "delivery_amount",
        "dimensions": [dimension],
        "calendar_month": "2026-06",
        "order_by": {"field": "metric_value", "direction": "desc"},
        "limit": 10,
    }
    return {"requests": [request]}


def materialize_suite(
    suite: dict[str, Any],
    *,
    invoke_facade: Callable[[str, dict[str, Any]], Any],
    salt: bytes,
) -> dict[str, Any]:
    if not isinstance(salt, bytes) or len(salt) < 32:
        raise ValueError("live fixture salt must contain at least 32 bytes")
    materialized = copy.deepcopy(suite)
    cases = {
        case.get("id"): case
        for case in materialized.get("cases") or []
        if isinstance(case, dict)
    }
    selected_slots = {
        key: declaration
        for key, declaration in SLOT_DECLARATIONS.items()
        if any(case_id in cases for case_id in declaration["case_ids"])
    }
    if not selected_slots:
        return {
            "suite": materialized,
            "selectors": {},
            "durable_binding": {
                "schema": SCHEMA,
                "algorithm_version": ALGORITHM_VERSION,
                "business_database_ref": None,
                "fixture_sha256": _sha(materialized),
                "slots": [],
            },
        }

    catalog = _invoke(
        invoke_facade,
        "datasage_catalog",
        {"requests": [{"domain": "delivery", "metric": "delivery_amount"}]},
    )
    if catalog.get("status") != "success":
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")
    query_by_recipe: dict[str, dict[str, Any]] = {}
    references: list[dict[str, Any]] = []
    for recipe in sorted({slot["recipe"] for slot in selected_slots.values()}):
        response = _invoke(invoke_facade, "datasage_query", _query_arguments(recipe))
        references.append(_source_ref(response.get("source_evidence_ref")))
        query_by_recipe[recipe] = response
    if len(references) + 1 > MAX_FACADE_CALLS or any(
        reference != references[0] for reference in references[1:]
    ):
        raise LiveFixtureUnavailable("LIVE_FIXTURE_SOURCE_EVIDENCE_MISSING")

    candidates_by_recipe = {
        recipe: _candidate_rows(response)
        for recipe, response in query_by_recipe.items()
    }
    needed = {recipe: 0 for recipe in candidates_by_recipe}
    for slot in selected_slots.values():
        needed[slot["recipe"]] += 1
    if any(len(candidates_by_recipe[recipe]) < count for recipe, count in needed.items()):
        raise LiveFixtureUnavailable("LIVE_FIXTURE_UNAVAILABLE")

    selectors: dict[str, str] = {}
    indexes = {recipe: 0 for recipe in candidates_by_recipe}
    for key, declaration in selected_slots.items():
        recipe = declaration["recipe"]
        selector = candidates_by_recipe[recipe][indexes[recipe]]
        indexes[recipe] += 1
        selectors[key] = selector
        for case_id in declaration["case_ids"]:
            if case_id in cases:
                _replace(
                    cases[case_id],
                    (
                        declaration["placeholder"],
                        declaration["canonical_placeholder"],
                    ),
                    selector,
                )
                _bind_expected_plan(cases[case_id], key, selector)

    slots = []
    for key, declaration in selected_slots.items():
        selector = selectors[key]
        slots.append(
            {
                "slot": key,
                "slot_type": declaration["slot_type"],
                "recipe": declaration["recipe"],
                "salted_selector_fingerprint": hashlib.sha256(
                    salt + b"\x00" + key.encode("utf-8") + b"\x00" + selector.encode("utf-8")
                ).hexdigest(),
                "coverage": {
                    "catalog_supported": True,
                    "positive_candidate": True,
                    "untruncated": all(
                        not result.get("truncated")
                        for result in query_by_recipe[
                            declaration["recipe"]
                        ].get("results") or []
                        if isinstance(result, Mapping)
                    ),
                },
                "count_bounds": {
                    "minimum_compatible_candidates": needed[declaration["recipe"]],
                    "maximum_rows_requested": 10,
                },
            }
        )
    durable = {
        "schema": SCHEMA,
        "algorithm_version": ALGORITHM_VERSION,
        "business_database_ref": references[0],
        "fixture_sha256": _sha(materialized),
        "slots": slots,
    }
    return {"suite": materialized, "selectors": selectors, "durable_binding": durable}


def synthetic_fixture_response(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    source_ref: dict[str, Any],
    metric_values: list[Any] | None = None,
    candidate_count: int = 3,
) -> dict[str, Any]:
    """Deterministic, model/DB-free fixture used only by unit tests."""

    if tool_name == "datasage_catalog":
        return {"status": "success", "results": [{"status": "success"}]}
    values = metric_values or list(range(candidate_count))
    rows = [
        {"selector": f"fixture-selector-{index}", "metric_value": value}
        for index, value in enumerate(values[:candidate_count])
    ]
    return {
        "status": "success",
        "source_evidence_ref": copy.deepcopy(source_ref),
        "results": [{"status": "success", "rows": rows, "truncated": False}],
        "truncated": False,
    }
