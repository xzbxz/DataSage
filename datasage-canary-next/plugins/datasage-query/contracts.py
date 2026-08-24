"""Read-only model-facing semantic catalog for DataSage Mini."""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

from .scorecard import performance_scorecard_manifest

_DOMAIN_FOLDERS = {
    "delivery": "delivery-query",
    "receipt": "receipt-query",
    "receivable": "receivable-query",
    "target": "target-query",
    "customer_risk": "customer-risk-query",
    "inventory": "inventory-query",
}
_MODEL_PROJECTION_VERSION = "datasage-model-semantic-projection/v4"
_CATALOG_VERSION = "datasage-metric-catalog/v1"
_ANALYSIS_AFFORDANCES_VERSION = "datasage-analysis-affordances/v8"
_CATALOG_PLANNING_GUIDANCE_VERSION = "datasage-catalog-planning-guidance/v1"
_METRIC_SELECTION_BOUNDARY_VERSION = "datasage-metric-selection-boundary/v1"
_CATALOG_PLANNING_GUIDANCE_KEYS = (
    "planning_rules",
    "answer_boundary",
    "intent_routes",
    "recipe_policy",
    "recipes",
    "analysis_recipes",
)
_MAX_CATALOG_PLANNING_GUIDANCE_JSON_CHARS = 12_000
_MANUAL_CATALOG_KEYS = {
    "catalog_status",
    "metric_catalog",
    "metric_families",
    "owned_metrics",
    "dimension_catalog",
    "dimension_sets",
    "metric_labels",
    "reused_receivable_evidence",
    "reused_receivable_dimension_mapping",
}
_MODEL_GUIDANCE_KEYS = (
    "purpose",
    "planning_rules",
    "dimension_policy",
    "tool_planning",
    "removed_capabilities",
    "defaults",
    "intent_routes",
    "recipe_policy",
    "recipes",
    "analysis_recipes",
    "inventory_scope_catalog",
    "attribution_modes",
    "answer_boundary",
    "answer_contract",
)
_QUERY_POLICY_PATH = (
    "plugins/datasage-query/contracts/query-policy.yaml"
)
_FORBIDDEN_MODEL_KEYS = {
    "aggregation",
    "column",
    "columns",
    "dimension_mappings",
    "filter_column",
    "formula",
    "identity_filter",
    "join",
    "joins",
    "measure",
    "measure_columns",
    "metric_join",
    "physical_dataset_contract",
    "primary_key",
    "required_filters",
    "structured_scope_field",
    "table",
    "tables",
    "time_field",
    "updated_at",
}
_PHYSICAL_VALUE_KEY_SUFFIXES = (
    "_column",
    "_columns",
    "_field",
    "_key",
    "_measure",
    "_table",
)
_PHYSICAL_VALUE_KEYS = {
    "column",
    "columns",
    "document_key",
    "filter_column",
    "measure",
    "measure_columns",
    "primary_key",
    "table",
    "tables",
    "time_field",
    "updated_at",
}
_TABLE_REFERENCE = re.compile(r"(?i)\bvk_(?:dw|dwd|ods)\.[A-Za-z_][A-Za-z0-9_]*\b")
_FORMULA_EXPRESSION = re.compile(r"(?i)\b(?:avg|count|max|min|sum)\s*\(")


_KNOWN_REASONING_TOPICS = {
    "collection_timing",
    "credit_or_settlement",
    "customer_mix",
    "demand_timing",
    "delivery_execution",
    "external_event",
    "price_or_terms",
    "product_mix",
    "return_activity",
    "supply_availability",
    "target_or_plan_change",
}


class ContractFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _profile_root() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def _trusted_path(relative_path: str) -> Path:
    root = _profile_root()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContractFailure("CONTRACT_UNAVAILABLE", "语义合同路径不安全。") from exc
    return path


@lru_cache(maxsize=64)
def _parse_yaml_cached(path_text: str, modified_ns: int, size: int) -> dict[str, Any]:
    del modified_ns, size
    value = yaml.safe_load(Path(path_text).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "语义合同格式无效。")
    return value


def _read_yaml(relative_path: str) -> dict[str, Any]:
    try:
        path = _trusted_path(relative_path)
        stat = path.stat()
        value = _parse_yaml_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, yaml.YAMLError) as exc:
        raise ContractFailure("CONTRACT_UNAVAILABLE", "暂时无法读取语义合同。") from exc
    return value


def _query_policy_projection() -> dict[str, Any]:
    """Load the single versioned planning/execution policy authority."""

    policy = _read_yaml(_QUERY_POLICY_PATH)
    time_range = policy.get("governed_metric_time_range")
    if (
        policy.get("version") != "datasage-query-policy/v1"
        or set(policy) != {"version", "governed_metric_time_range"}
        or not isinstance(time_range, Mapping)
        or set(time_range)
        != {"start_inclusive", "end_exclusive", "max_days", "wider_analysis"}
        or time_range.get("start_inclusive") is not True
        or time_range.get("end_exclusive") is not True
        or not isinstance(time_range.get("max_days"), int)
        or isinstance(time_range.get("max_days"), bool)
        or time_range["max_days"] < 1
        or time_range.get("wider_analysis")
        != "split_into_independently_bounded_periods"
    ):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "common query policy is invalid"
        )
    return _copy_guidance(policy)


def _copy_guidance(value: Any) -> Any:
    """Copy business planning guidance while dropping physical implementation keys."""

    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for raw_key in sorted(value, key=str):
            key = str(raw_key)
            if key in _FORBIDDEN_MODEL_KEYS or key in _MANUAL_CATALOG_KEYS:
                continue
            copied[key] = _copy_guidance(value[raw_key])
        return copied
    if isinstance(value, (list, tuple)):
        return [_copy_guidance(item) for item in value]
    return value


def _collect_strings(value: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(value, str):
        if value:
            values.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            values.update(_collect_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.update(_collect_strings(item))
    return values


def _physical_identifiers(contract: Mapping[str, Any]) -> set[str]:
    """Collect declared physical identifiers so prose containing them can be omitted."""

    identifiers: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key).lower()
                if key.endswith("_filters") or key == "required_filters":
                    if isinstance(item, Mapping):
                        identifiers.update(str(name) for name in item)
                elif key in _PHYSICAL_VALUE_KEYS or key.endswith(
                    _PHYSICAL_VALUE_KEY_SUFFIXES
                ):
                    identifiers.update(_collect_strings(item))
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(contract)
    return {item for item in identifiers if len(item) >= 3}


def _business_tokens(contract: Mapping[str, Any]) -> set[str]:
    """Return explicit model-facing codes/values that may resemble column names."""

    tokens: set[str] = set()
    metrics = contract.get("metrics")
    dimensions = contract.get("dimensions")
    if isinstance(metrics, Mapping):
        tokens.update(str(code) for code in metrics)
        for definition in metrics.values():
            if not isinstance(definition, Mapping):
                continue
            scopes = definition.get("inventory_scope_filters")
            if isinstance(scopes, Mapping):
                tokens.update(str(scope) for scope in scopes)
            for key in (
                "default_inventory_scope",
                "required_attribution_mode",
            ):
                value = definition.get(key)
                if isinstance(value, str):
                    tokens.add(value)
            modes = definition.get("allowed_attribution_modes")
            if isinstance(modes, list):
                tokens.update(str(mode) for mode in modes if isinstance(mode, str))
    if isinstance(dimensions, Mapping):
        tokens.update(str(code) for code in dimensions)
        for definition in dimensions.values():
            if not isinstance(definition, Mapping):
                continue
            value_contract = definition.get("value_contract")
            if not isinstance(value_contract, Mapping):
                continue
            allowed = value_contract.get("allowed_values")
            if isinstance(allowed, list):
                tokens.update(str(item) for item in allowed if isinstance(item, str))
            aliases = value_contract.get("canonical_aliases")
            if isinstance(aliases, Mapping):
                tokens.update(str(item) for item in aliases)
                tokens.update(str(item) for item in aliases.values())
    return {token for token in tokens if token}


def _contains_physical_identifier(text: str, identifiers: set[str]) -> bool:
    for identifier in identifiers:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


def _safe_business_text(value: Any, physical_identifiers: set[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _TABLE_REFERENCE.search(text) or _FORMULA_EXPRESSION.search(text):
        return None
    if _contains_physical_identifier(text, physical_identifiers):
        return None
    return text


def _assert_business_safe_tree(
    value: Any,
    *,
    context: str,
    physical_identifiers: set[str] | None = None,
) -> None:
    """Fail closed if a model-facing tree contains a physical contract detail."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _FORBIDDEN_MODEL_KEYS:
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"{context} contains forbidden model-facing key: {key}",
                )
            _assert_business_safe_tree(
                item,
                context=context,
                physical_identifiers=physical_identifiers,
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_business_safe_tree(
                item,
                context=context,
                physical_identifiers=physical_identifiers,
            )
    elif isinstance(value, str):
        if (
            _TABLE_REFERENCE.search(value)
            or _FORMULA_EXPRESSION.search(value)
            or (
                physical_identifiers is not None
                and _contains_physical_identifier(value, physical_identifiers)
            )
        ):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"{context} contains a physical table or formula expression",
            )


def _value_contract_projection(raw: Any, *, dimension: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"dimension {dimension} has an invalid value_contract",
        )
    _assert_business_safe_tree(raw, context=f"dimension {dimension} value_contract")
    kind = raw.get("kind")
    if kind not in {"closed", "source_exact", "entity_exact"}:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"dimension {dimension} has an unsupported value_contract kind",
        )
    if "filterable" in raw and not isinstance(raw.get("filterable"), bool):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"dimension {dimension} has an invalid filterable policy",
        )
    if kind == "closed":
        allowed = raw.get("allowed_values")
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(isinstance(item, (Mapping, list, tuple)) for item in allowed)
        ):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"dimension {dimension} has an invalid closed value contract",
            )
        meanings = raw.get("business_meanings")
        if meanings is not None and not isinstance(meanings, Mapping):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"dimension {dimension} has invalid business meanings",
            )
        aliases = raw.get("canonical_aliases")
        if aliases is not None:
            if not isinstance(aliases, Mapping):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"dimension {dimension} has invalid canonical aliases",
                )
            alias_names = set(aliases)
            for alias, canonical in aliases.items():
                if (
                    not isinstance(alias, str)
                    or not alias
                    or isinstance(canonical, (Mapping, list, tuple))
                    or canonical not in allowed
                    or alias in allowed
                    or canonical in alias_names
                ):
                    raise ContractFailure(
                        "CONTRACT_UNAVAILABLE",
                        f"dimension {dimension} has a malformed or chained canonical alias",
                    )
    elif "canonical_aliases" in raw:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"dimension {dimension} allows aliases only for a closed value contract",
        )
    return _copy_guidance(raw)


def _metric_dimension_contract(
    definition: Mapping[str, Any], metrics: Mapping[str, Any]
) -> tuple[list[str], dict[str, list[str]]]:
    direct = definition.get("allowed_dimensions")
    dimensions = {
        str(item) for item in direct or [] if isinstance(item, str) and item
    }
    by_attribution: dict[str, list[str]] = {}
    paths = definition.get("paths")
    if isinstance(paths, Mapping):
        for raw_mode in sorted(paths, key=str):
            path = paths[raw_mode]
            if isinstance(path, Mapping) and _is_unavailable(path):
                continue
            allowed = path.get("allowed_dimensions") if isinstance(path, Mapping) else None
            if isinstance(allowed, list):
                values = sorted({str(item) for item in allowed if isinstance(item, str)})
                by_attribution[str(raw_mode)] = values
                dimensions.update(values)

    source_metric = definition.get("source_completion_metric")
    source_path = definition.get("source_path")
    if isinstance(source_metric, str) and isinstance(source_path, str):
        source = metrics.get(source_metric)
        source_paths = source.get("paths") if isinstance(source, Mapping) else None
        path = source_paths.get(source_path) if isinstance(source_paths, Mapping) else None
        if isinstance(path, Mapping) and _is_unavailable(path):
            return sorted(dimensions), by_attribution
        allowed = path.get("allowed_dimensions") if isinstance(path, Mapping) else None
        if isinstance(allowed, list):
            values = sorted({str(item) for item in allowed if isinstance(item, str)})
            by_attribution[source_path] = values
            dimensions.update(values)
    return sorted(dimensions), by_attribution


def _metric_group_dimension_limit(
    definition: Mapping[str, Any], allowed_dimensions: list[str]
) -> int:
    """Return the governed grouping arity exposed to planner and executor."""

    raw_limit = definition.get("max_group_dimensions")
    if definition.get("query_kind") is not None and raw_limit is None:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "analytical metric lacks max_group_dimensions",
        )
    if raw_limit is None:
        return min(5, len(allowed_dimensions))
    if (
        not isinstance(raw_limit, int)
        or isinstance(raw_limit, bool)
        or not 0 <= raw_limit <= 5
        or raw_limit > len(allowed_dimensions)
    ):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "metric max_group_dimensions is invalid",
        )
    return raw_limit


def _is_unavailable(definition: Mapping[str, Any]) -> bool:
    availability = definition.get("availability")
    if availability is None:
        return False
    if not isinstance(availability, Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "metric availability contract is invalid"
        )
    status = str(availability.get("status") or "available")
    if status == "available":
        return False
    if status not in {"blocked", "pending_validation"}:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", f"unknown metric availability status: {status}"
        )
    return True


def _currency_policy_projection(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    allowed_keys = {
        "mode",
        "never_sum_mixed_currency",
        "require_filter_or_group",
    }
    return {
        key: _copy_guidance(value[key])
        for key in sorted(allowed_keys)
        if key in value
    }


def _mentions_any(value: Any, tokens: set[str]) -> bool:
    if isinstance(value, str):
        return any(token in value for token in tokens)
    if isinstance(value, Mapping):
        return any(
            str(key) in tokens or _mentions_any(item, tokens)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_mentions_any(item, tokens) for item in value)
    return False


def _remove_unavailable_guidance(
    guidance: dict[str, Any], blocked_tokens: set[str]
) -> dict[str, Any]:
    """Remove planner hints that could re-authorize a hidden metric or mode."""

    cleaned = dict(guidance)
    for key in (
        "attribution_modes",
        "intent_routes",
        "recipes",
        "analysis_recipes",
    ):
        section = cleaned.get(key)
        if isinstance(section, Mapping):
            cleaned[key] = {
                name: value
                for name, value in section.items()
                if str(name) not in blocked_tokens
                and not _mentions_any(value, blocked_tokens)
            }
    for key in ("planning_rules", "answer_boundary"):
        section = cleaned.get(key)
        if isinstance(section, list):
            cleaned[key] = [
                item for item in section if not _mentions_any(item, blocked_tokens)
            ]
    return cleaned


def _compress_metric_dimension_sets(
    metrics: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Deduplicate repeated authorization lists without changing authority."""

    sets: dict[str, list[str]] = {}
    compressed: list[dict[str, Any]] = []
    for metric in metrics:
        dimensions = metric.get("allowed_dimensions")
        if not isinstance(dimensions, list):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "projected metric dimension authorization is invalid",
            )
        canonical = json.dumps(
            dimensions,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        set_id = "dimensions_" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()[:12]
        previous = sets.get(set_id)
        if previous is not None and previous != dimensions:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "projected metric dimension set identity collided",
            )
        sets[set_id] = list(dimensions)
        item = dict(metric)
        item.pop("allowed_dimensions", None)
        # Required disclosures are emitted and sealed by datasage_query. The
        # planner does not need legacy answer-note prose in every contract.
        item.pop("answer_note", None)
        item["allowed_dimension_set"] = set_id
        compressed.append(item)
    return compressed, {key: sets[key] for key in sorted(sets)}


def _tree_keys(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _tree_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _tree_keys(item)


def _recipe_mapping(guidance: Mapping[str, Any]) -> Mapping[str, Any]:
    sections = [
        guidance.get(section_name)
        for section_name in ("recipes", "analysis_recipes")
        if section_name in guidance
    ]
    if len(sections) != 1 or not isinstance(sections[0], Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "model guidance must contain exactly one structured recipe section",
        )
    return sections[0]


def _validate_target_change_handoff(recipes: Mapping[str, Any]) -> None:
    handoff = recipes.get("completion_change_handoff")
    if (
        not isinstance(handoff, Mapping)
        or set(handoff) != {"kind", "plan", "evidence_routes"}
        or handoff.get("kind") != "analysis_seed"
        or handoff.get("plan")
        != "query_completion_then_route_same_period_change_capability"
    ):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "target change handoff is missing or invalid",
        )
    routes = handoff.get("evidence_routes")
    if not isinstance(routes, Mapping) or set(routes) != {"delivery", "receipt"}:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "target change handoff routes are incomplete",
        )
    expected_metrics = {
        "delivery": "delivery_amount",
        "receipt": "net_receipt_amount",
    }
    for route_domain, route in routes.items():
        if not isinstance(route, Mapping) or set(route) != {
            "metric",
            "first",
            "fallback",
        }:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"target route {route_domain} is invalid",
            )
        folder = _DOMAIN_FOLDERS[route_domain]
        downstream = _model_semantic_projection(
            route_domain,
            _read_yaml(f"skills/{folder}/references/planner-contract.yaml"),
            _read_yaml(f"plugins/datasage-query/contracts/{route_domain}-semantics.yaml"),
        )
        downstream_metrics = {
            item["code"]: item for item in downstream["metrics"]
        }
        metric = downstream_metrics.get(route.get("metric"))
        downstream_recipes = _recipe_mapping(downstream["guidance"])
        if (
            not isinstance(metric, Mapping)
            or not metric.get("change_decomposition_dimensions")
            or route.get("metric") != expected_metrics[route_domain]
            or route.get("first") != "governed_change_decomposition"
            or route.get("fallback") != "change_observations"
            or route["first"] not in downstream_recipes
            or route["fallback"] not in downstream_recipes
        ):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"target route {route_domain} exceeds downstream capability",
            )


def _validate_change_guidance(
    domain: str,
    guidance: Mapping[str, Any],
    projected_metrics: list[dict[str, Any]],
) -> None:
    """Keep model-visible change recipes aligned with metric-owned capability."""

    decomposition_metrics = {
        item["code"]: set(item["change_decomposition_dimensions"])
        for item in projected_metrics
        if item.get("change_decomposition_dimensions")
    }
    recipes = _recipe_mapping(guidance)
    for recipes in (recipes,):
        recipe_names = {str(name) for name in recipes}
        retired = {
            name
            for name in recipe_names
            if "driver" in name.casefold()
        }
        if retired:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"retired change recipe names are not allowed: {sorted(retired)}",
            )

        governed = recipes.get("governed_change_decomposition")
        if governed is not None and not decomposition_metrics:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "governed change decomposition lacks a metric-owned capability",
            )
        if decomposition_metrics and governed is None:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "metric-owned change decomposition lacks a governed recipe",
            )
        if governed is not None:
            supported_dimensions = set.intersection(
                *decomposition_metrics.values()
            )
            governed_dimensions = governed.get("dimensions") if isinstance(
                governed, Mapping
            ) else None
            if (
                not isinstance(governed, Mapping)
                or governed.get("relation") != "reconciled_change_only"
                or not isinstance(governed_dimensions, list)
                or not governed_dimensions
                or any(
                    not isinstance(dimension, str)
                    for dimension in governed_dimensions
                )
                or len(set(governed_dimensions)) != len(governed_dimensions)
                or not set(governed_dimensions) <= supported_dimensions
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    "governed change recipe exceeds metric-owned capability",
                )

        observations = recipes.get("change_observations")
        if observations is not None and (
            not isinstance(observations, Mapping)
            or observations.get("label") != "partial_observations_only"
        ):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "change observations must be explicitly labelled partial",
            )
        if isinstance(observations, Mapping):
            allowed_observation_fields = {
                "kind",
                "request_policy",
                "label",
                "dimensions",
                "comparison",
                "two_sided_requests",
                "default_limit_per_side",
                "default_limit",
                "occurrence",
                "debt_snapshot",
            }
            unknown_observation_fields = (
                set(observations) - allowed_observation_fields
            )
            if unknown_observation_fields:
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    "change observations contain unsupported authority: "
                    f"{sorted(unknown_observation_fields)}",
                )
            projected_metric_codes = {
                str(item["code"]) for item in projected_metrics
            }
            projected_dimensions = {
                str(dimension)
                for item in projected_metrics
                for dimension in item.get("allowed_dimensions", [])
            }
            observation_dimensions = observations.get("dimensions")
            comparison = observations.get("comparison")
            two_sided_requests = observations.get("two_sided_requests")
            comparison_is_valid = (
                comparison is None
                or comparison == "previous_period"
                or comparison == {"kind": "previous_period"}
            )
            if (
                observations.get("kind") != "analysis_seed"
                or observations.get("request_policy") not in {None, "fallback"}
                or not comparison_is_valid
                or observation_dimensions is not None
                and (
                    not isinstance(observation_dimensions, list)
                    or not observation_dimensions
                    or any(
                        not isinstance(dimension, str)
                        for dimension in observation_dimensions
                    )
                    or len(set(observation_dimensions))
                    != len(observation_dimensions)
                    or not set(observation_dimensions) <= projected_dimensions
                )
                or two_sided_requests is not None
                and two_sided_requests
                != {
                    "increases": "delta_value_desc",
                    "decreases": "delta_value_asc",
                }
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    "change observations exceed the structured observation policy",
                )
            for limit_field in ("default_limit", "default_limit_per_side"):
                limit = observations.get(limit_field)
                if limit is not None and (
                    type(limit) is not int or not 1 <= limit <= 100
                ):
                    raise ContractFailure(
                        "CONTRACT_UNAVAILABLE",
                        f"change observations contain invalid {limit_field}",
                    )
            for metric_field in ("occurrence", "debt_snapshot"):
                metric_reference = observations.get(metric_field)
                if (
                    metric_reference is not None
                    and metric_reference not in projected_metric_codes
                ):
                    raise ContractFailure(
                        "CONTRACT_UNAVAILABLE",
                        "change observations reference an unavailable metric: "
                        f"{metric_reference}",
                    )

        for name, recipe in recipes.items():
            if "change" not in str(name).casefold() or not isinstance(recipe, Mapping):
                continue
            bounded_keys = {
                key.casefold()
                for key in _tree_keys(recipe)
                if "limit" in key.casefold()
                or "top_n" in key.casefold()
                or key.casefold() == "two_sided_requests"
            }
            if bounded_keys and (
                str(name) != "change_observations"
                or recipe.get("label") != "partial_observations_only"
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"bounded change recipe {name} is not a partial observation",
                )

        explain = recipes.get("explain_change")
        if (governed is not None or observations is not None) and explain is None:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "change capability requires a structured explain policy",
            )
        if isinstance(explain, Mapping):
            for field in ("first", "fallback"):
                if field not in explain:
                    continue
                reference = explain[field]
                if (
                    not isinstance(reference, str)
                    or not reference
                    or reference not in recipe_names
                ):
                    raise ContractFailure(
                        "CONTRACT_UNAVAILABLE",
                        f"explain_change references missing recipe {reference}",
                    )
            if decomposition_metrics and (
                explain.get("kind") != "analysis_seed"
                or explain.get("request_policy") not in {None, "adaptive"}
                or explain.get("first") != "governed_change_decomposition"
                or explain.get("fallback") != "change_observations"
                or set(explain)
                - {"kind", "request_policy", "first", "fallback"}
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    "explain_change must prefer governed decomposition",
                )
            if not decomposition_metrics and (
                explain.get("kind") != "analysis_seed"
                or explain.get("first") != "change_observations"
                or explain.get("causal_status")
                != "unavailable_without_independent_evidence"
                or set(explain) - {"kind", "first", "causal_status"}
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    "non-decomposable change explanation must remain observational",
                )
        elif explain is not None:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "explain_change must be structured",
            )
    if domain == "target":
        _validate_target_change_handoff(recipes)


def _validate_customer_risk_recipe_requests(
    guidance: Mapping[str, Any],
) -> None:
    """Require exact customer-risk recipes to contain executable cross-domain plans."""

    recipes = guidance.get("recipes")
    if not isinstance(recipes, Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "customer risk recipes are unavailable",
        )
    expected_batches = {
        ("portfolio_overview", "first_call"): ("receivable", 5),
        ("portfolio_overview", "optional_second_call"): ("customer_risk", 2),
        ("attention_map", "first_call"): ("receivable", 5),
        ("customer_evidence_card", "first_call"): ("receivable", 5),
        ("customer_evidence_card", "optional_second_call"): ("customer_risk", 2),
    }
    semantics_by_domain: dict[str, Mapping[str, Any]] = {}
    for (recipe_name, batch_name), (expected_domain, expected_count) in (
        expected_batches.items()
    ):
        recipe = recipes.get(recipe_name)
        batch = recipe.get(batch_name) if isinstance(recipe, Mapping) else None
        requests = batch.get("requests") if isinstance(batch, Mapping) else None
        if (
            not isinstance(requests, list)
            or len(requests) != expected_count
            or batch.get("batch_policy") is None
            or batch.get("request_id_policy") is None
        ):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"customer risk recipe {recipe_name}.{batch_name} is incomplete",
            )
        for node in requests:
            request = node.get("request") if isinstance(node, Mapping) else None
            bindings = node.get("bindings") if isinstance(node, Mapping) else None
            if (
                not isinstance(request, Mapping)
                or request.get("domain") != expected_domain
                or request.get("mode") != "metric"
                or not isinstance(request.get("metric"), str)
                or not isinstance(request.get("dimensions"), list)
                or "metric_filters" in request
                or "time_range" in request
                or not isinstance(bindings, Mapping)
                or bindings.get("time_range") != "omit_use_metric_default"
                or not isinstance(bindings.get("metric_filter_roles"), Mapping)
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"customer risk recipe {recipe_name}.{batch_name} has an invalid request node",
                )
            if expected_domain not in semantics_by_domain:
                semantics_by_domain[expected_domain] = _read_yaml(
                    f"plugins/datasage-query/contracts/{expected_domain}-semantics.yaml"
                )
            domain_semantics = semantics_by_domain[expected_domain]
            metrics = domain_semantics.get("metrics")
            metric = (
                metrics.get(request["metric"])
                if isinstance(metrics, Mapping)
                else None
            )
            if not isinstance(metric, Mapping) or _is_unavailable(metric):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"customer risk recipe references unavailable metric {request['metric']}",
                )
            allowed_dimensions, _by_attribution = _metric_dimension_contract(
                metric,
                metrics,
            )
            dimensions = request["dimensions"]
            if (
                any(not isinstance(value, str) for value in dimensions)
                or not set(dimensions) <= set(allowed_dimensions)
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"customer risk recipe metric {request['metric']} has invalid dimensions",
                )
            roles = bindings["metric_filter_roles"]
            if (
                any(
                    role != "customer"
                    or value != "required_exact_user_token"
                    for role, value in roles.items()
                )
                or ("customer" in roles and "customer" not in allowed_dimensions)
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"customer risk recipe metric {request['metric']} has invalid filter roles",
                )


def _target_gap_decomposition_projection(
    domain: str,
    semantics: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Load the existing executor-owned target-gap capability for model projection."""

    if domain != "target":
        return None
    reference = semantics.get("target_gap_decomposition_contract")
    if reference != "contracts/target-gap-decomposition.yaml":
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "target gap decomposition contract reference is invalid",
        )
    contract = _read_yaml(f"plugins/datasage-query/{reference}")
    applicability = contract.get("applicability")
    receipt = contract.get("receipt")
    rollout = contract.get("rollout")
    if (
        contract.get("version") != "datasage-target-gap-decomposition/v1"
        or contract.get("status") != "active"
        or not isinstance(applicability, Mapping)
        or not isinstance(receipt, Mapping)
        or not isinstance(rollout, Mapping)
        or rollout.get("status") != "active"
        or rollout.get("model_visible_operation") is not True
        or receipt.get("operation") != "complete_target_gap_decomposition"
    ):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "target gap decomposition capability is not active",
        )
    metric_codes = applicability.get("metrics")
    dimensions = applicability.get("dimensions")
    attribution_mode = applicability.get("attribution_mode")
    if (
        not isinstance(metric_codes, list)
        or not metric_codes
        or any(not isinstance(value, str) for value in metric_codes)
        or len(set(metric_codes)) != len(metric_codes)
        or not isinstance(dimensions, list)
        or not dimensions
        or any(not isinstance(value, str) for value in dimensions)
        or len(set(dimensions)) != len(dimensions)
        or not isinstance(attribution_mode, str)
        or not attribution_mode
    ):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "target gap decomposition applicability is invalid",
        )
    return {
        "version": contract.get("version"),
        "operation": receipt["operation"],
        "metrics": list(metric_codes),
        "dimensions": list(dimensions),
        "required_attribution_mode": attribution_mode,
    }


def _model_semantic_projection(
    domain: str,
    planner: Mapping[str, Any],
    semantics: Mapping[str, Any],
) -> dict[str, Any]:
    if semantics.get("domain") != domain:
        raise ContractFailure("CONTRACT_UNAVAILABLE", "domain semantic contract mismatch")
    metrics = semantics.get("metrics")
    dimensions = semantics.get("dimensions")
    if not isinstance(metrics, Mapping) or not isinstance(dimensions, Mapping):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "domain semantic catalog is incomplete")

    physical_identifiers = _physical_identifiers(semantics) - _business_tokens(semantics)
    target_gap_decomposition = _target_gap_decomposition_projection(
        domain, semantics
    )
    projected_metrics: list[dict[str, Any]] = []
    blocked_metric_codes: set[str] = set()
    known_dimensions = {str(code) for code in dimensions}
    for raw_code in sorted(metrics, key=str):
        code = str(raw_code)
        definition = metrics[raw_code]
        if not isinstance(definition, Mapping):
            raise ContractFailure("CONTRACT_UNAVAILABLE", f"metric {code} is invalid")
        # Unverified data paths are absent from model authorization. The query
        # executor independently enforces the same availability contract.
        if _is_unavailable(definition):
            blocked_metric_codes.add(code)
            continue
        allowed_dimensions, by_attribution = _metric_dimension_contract(
            definition, metrics
        )
        unknown = set(allowed_dimensions) - known_dimensions
        if unknown:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"metric {code} references unknown dimensions: {sorted(unknown)}",
            )
        change_decomposition = definition.get("change_decomposition")
        change_decomposition_dimensions: list[str] = []
        if change_decomposition is not None:
            if (
                not isinstance(change_decomposition, Mapping)
                or change_decomposition.get("mode") != "additive_partition"
                or not isinstance(change_decomposition.get("dimensions"), list)
                or not change_decomposition["dimensions"]
                or any(
                    not isinstance(value, str)
                    for value in change_decomposition["dimensions"]
                )
                or len(set(change_decomposition["dimensions"]))
                != len(change_decomposition["dimensions"])
                or not set(change_decomposition["dimensions"])
                <= set(allowed_dimensions)
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"metric {code} has an invalid change decomposition contract",
                )
            change_decomposition_dimensions = list(
                change_decomposition["dimensions"]
            )
        reasoning_topics = definition.get("reasoning_topics")
        projected_reasoning_topics: list[str] = []
        if reasoning_topics is not None:
            if (
                not change_decomposition_dimensions
                or not isinstance(reasoning_topics, list)
                or not reasoning_topics
                or any(not isinstance(value, str) for value in reasoning_topics)
                or len(set(reasoning_topics)) != len(reasoning_topics)
                or not set(reasoning_topics) <= _KNOWN_REASONING_TOPICS
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"metric {code} has invalid reasoning topics",
                )
            projected_reasoning_topics = list(reasoning_topics)
        label = _safe_business_text(definition.get("label"), physical_identifiers)
        business_definition = _safe_business_text(
            definition.get("business_definition"), physical_identifiers
        )
        if label is None or business_definition is None:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"metric {code} lacks a safe business label or definition",
            )
        item: dict[str, Any] = {
            "code": code,
            "label": label,
            "business_definition": business_definition,
            "time_policy": _copy_guidance(definition.get("time_policy")),
            "allowed_dimensions": allowed_dimensions,
            "max_group_dimensions": _metric_group_dimension_limit(
                definition, allowed_dimensions
            ),
            "exact_default_lookup_supported": definition.get(
                "exact_default_lookup_supported"
            )
            is True,
            "supports_generic_comparison": definition.get("query_kind") is None,
        }
        if change_decomposition_dimensions:
            item["change_decomposition_dimensions"] = (
                change_decomposition_dimensions
            )
        if projected_reasoning_topics:
            item["reasoning_topics"] = projected_reasoning_topics
        answer_note = _safe_business_text(
            definition.get("answer_note"), physical_identifiers
        )
        if (
            domain == "target"
            and isinstance(answer_note, str)
            and "salesperson_allocation" in answer_note
        ):
            answer_note = None
        optional_metric_fields = {
            "unit": _copy_guidance(definition.get("unit")),
            "unit_policy": _copy_guidance(definition.get("unit_policy")),
            "currency_policy": _currency_policy_projection(
                definition.get("currency_policy")
            ),
            "answer_note": answer_note,
        }
        metric_answer_contract = definition.get("answer_contract")
        if metric_answer_contract is not None:
            if (
                not isinstance(metric_answer_contract, list)
                or not 1 <= len(metric_answer_contract) <= 12
                or any(
                    _safe_business_text(clause, physical_identifiers) is None
                    for clause in metric_answer_contract
                )
                or len(set(metric_answer_contract)) != len(metric_answer_contract)
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"metric {code} has an invalid answer contract",
                )
            optional_metric_fields["answer_contract"] = [
                str(clause).strip() for clause in metric_answer_contract
            ]
        item.update(
            {
                key: value
                for key, value in optional_metric_fields.items()
                if value is not None
            }
        )
        if isinstance(definition.get("required_attribution_mode"), str):
            item["required_attribution_mode"] = definition["required_attribution_mode"]
        if isinstance(definition.get("allowed_attribution_modes"), list):
            declared_modes = {
                str(mode)
                for mode in definition["allowed_attribution_modes"]
                if isinstance(mode, str)
            }
            item["allowed_attribution_modes"] = sorted(
                declared_modes.intersection(by_attribution)
                if by_attribution
                else declared_modes
            )
        if by_attribution:
            item["dimensions_by_attribution_mode"] = by_attribution
        if (
            target_gap_decomposition is not None
            and code in target_gap_decomposition["metrics"]
        ):
            required_mode = target_gap_decomposition[
                "required_attribution_mode"
            ]
            target_gap_dimensions = target_gap_decomposition["dimensions"]
            if (
                definition.get("query_kind") != "target_completion"
                or required_mode not in by_attribution
                or not set(target_gap_dimensions)
                <= set(by_attribution[required_mode])
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"metric {code} cannot execute the target gap decomposition contract",
                )
            item["target_gap_decomposition"] = {
                "operation": target_gap_decomposition["operation"],
                "dimensions": list(target_gap_dimensions),
                "required_attribution_mode": required_mode,
            }
        scopes = definition.get("inventory_scope_filters")
        if isinstance(scopes, Mapping):
            item["available_inventory_scopes"] = sorted(str(scope) for scope in scopes)
        if isinstance(definition.get("default_inventory_scope"), str):
            item["default_inventory_scope"] = definition["default_inventory_scope"]
        projected_metrics.append(item)

    executable_dimension_codes = {
        dimension
        for metric in projected_metrics
        for dimension in metric["allowed_dimensions"]
    }
    blocked_dimension_codes = known_dimensions - executable_dimension_codes
    projected_dimensions: list[dict[str, Any]] = []
    for raw_code in sorted(dimensions, key=str):
        code = str(raw_code)
        if code not in executable_dimension_codes:
            continue
        definition = dimensions[raw_code]
        if not isinstance(definition, Mapping):
            raise ContractFailure("CONTRACT_UNAVAILABLE", f"dimension {code} is invalid")
        business_definition = definition.get("business_definition")
        if business_definition is None:
            business_definition = definition.get("semantics")
        label = _safe_business_text(definition.get("label"), physical_identifiers)
        value_contract = _value_contract_projection(
            definition.get("value_contract"), dimension=code
        )
        if label is None or value_contract is None:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"dimension {code} lacks a safe label or value contract",
            )
        item = {
            "code": code,
            "label": label,
            "value_contract": value_contract,
        }
        optional_dimension_fields = {
            "business_definition": _safe_business_text(
                business_definition, physical_identifiers
            ),
            "unit": _copy_guidance(definition.get("unit")),
            "time_semantics": _copy_guidance(definition.get("time_semantics")),
            "answer_note": _safe_business_text(
                definition.get("answer_note"), physical_identifiers
            ),
        }
        item.update(
            {
                key: value
                for key, value in optional_dimension_fields.items()
                if value is not None
            }
        )
        projected_dimensions.append(item)

    guidance = {
        key: _copy_guidance(planner[key])
        for key in _MODEL_GUIDANCE_KEYS
        if key in planner
    }
    blocked_guidance_tokens = set(blocked_metric_codes)
    blocked_guidance_tokens.update(blocked_dimension_codes)
    if domain == "target":
        blocked_guidance_tokens.update(
            {"salesperson_allocation", "salesperson-allocation"}
        )
    guidance = _remove_unavailable_guidance(
        guidance, blocked_guidance_tokens
    )
    if domain == "customer_risk":
        _validate_customer_risk_recipe_requests(guidance)
    _validate_change_guidance(domain, guidance, projected_metrics)
    compressed_metrics, allowed_dimension_sets = (
        _compress_metric_dimension_sets(projected_metrics)
    )
    source_versions = {
        "planner": planner.get("version"),
        "semantics": semantics.get("version"),
    }
    if target_gap_decomposition is not None:
        source_versions["target_gap_decomposition"] = (
            target_gap_decomposition["version"]
        )
    projection = {
        "version": _MODEL_PROJECTION_VERSION,
        "domain": domain,
        "source_versions": source_versions,
        "guidance": guidance,
        "metrics": compressed_metrics,
        "allowed_dimension_sets": allowed_dimension_sets,
        "dimensions": projected_dimensions,
    }
    _assert_business_safe_tree(
        projection,
        context=f"domain {domain} planner projection",
        physical_identifiers=physical_identifiers,
    )
    return projection


def model_guidance_projection(
    domain: str,
    planner: Mapping[str, Any],
    semantics: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the same availability-filtered guidance used by the catalog."""

    if domain not in _DOMAIN_FOLDERS:
        raise ContractFailure("CONTRACT_UNAVAILABLE", "unknown semantic domain")
    guidance = _model_semantic_projection(domain, planner, semantics).get("guidance")
    if not isinstance(guidance, dict):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "model guidance projection is invalid"
        )
    return guidance


def _domain_contract(domain: str, view: str) -> dict[str, Any]:
    folder = _DOMAIN_FOLDERS[domain]
    if view != "planner":
        raise ContractFailure(
            "DETAIL_CONTRACT_UNAVAILABLE",
            "当前发布版本尚未开放语义明细合同，请使用已登记指标和维度。",
        )
    semantics = _read_yaml(f"plugins/datasage-query/contracts/{domain}-semantics.yaml")
    return {
        "planner": _model_semantic_projection(
            domain,
            _read_yaml(f"skills/{folder}/references/planner-contract.yaml"),
            semantics,
        )
    }


def _catalog_summary(domain: str, planner: Mapping[str, Any]) -> dict[str, Any]:
    """Project a compact first-level catalog without dumping every dimension."""

    raw_metrics = planner.get("metrics")
    if not isinstance(raw_metrics, list):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "指标目录格式无效。")
    metrics: list[dict[str, Any]] = []
    for raw in raw_metrics:
        if not isinstance(raw, Mapping):
            raise ContractFailure("CONTRACT_UNAVAILABLE", "指标目录项格式无效。")
        item = {
            key: _copy_guidance(raw.get(key))
            for key in (
                "code",
                "label",
                "business_definition",
                "time_policy",
                "unit",
                "unit_policy",
                "currency_policy",
                "required_attribution_mode",
                "allowed_attribution_modes",
                "available_inventory_scopes",
                "default_inventory_scope",
                "max_group_dimensions",
            )
            if raw.get(key) is not None
        }
        item["supports_dimensions"] = bool(
            raw.get("allowed_dimensions") or raw.get("allowed_dimension_set")
        )
        item["supports_change_decomposition"] = bool(
            raw.get("change_decomposition_dimensions")
        )
        metrics.append(item)
    return {
        "domain": domain,
        "level": "summary",
        "source_versions": _copy_guidance(planner.get("source_versions")),
        "planning_guidance": _catalog_planning_guidance(planner),
        "analysis_affordances": _analysis_affordances(domain, planner),
        "metric_count": len(metrics),
        "metrics": metrics,
    }


def _catalog_expert_index(domain: str, planner: Mapping[str, Any]) -> dict[str, Any]:
    """Project the smallest safe metric-selection index for expert planning.

    This view deliberately omits definitions and broad planning guidance.  An
    exact single-metric lookup may rely on governed defaults and let query
    validation remain the execution authority.  Non-default scopes, filters,
    dimensions, comparisons, and advanced analysis still require metric detail.
    """

    raw_metrics = planner.get("metrics")
    if not isinstance(raw_metrics, list):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "指标目录格式无效。")
    metrics: list[dict[str, Any]] = []
    for raw in raw_metrics:
        if not isinstance(raw, Mapping):
            raise ContractFailure("CONTRACT_UNAVAILABLE", "指标目录项格式无效。")
        item = {
            key: _copy_guidance(raw.get(key))
            for key in (
                "code",
                "label",
                "business_definition",
                "time_policy",
                "unit",
                "unit_policy",
                "currency_policy",
                "required_attribution_mode",
                "allowed_attribution_modes",
                "available_inventory_scopes",
                "default_inventory_scope",
                "exact_default_lookup_supported",
                "max_group_dimensions",
            )
            if raw.get(key) is not None
        }
        item["supports_dimensions"] = bool(
            raw.get("allowed_dimensions") or raw.get("allowed_dimension_set")
        )
        item["supports_change_decomposition"] = bool(
            raw.get("change_decomposition_dimensions")
        )
        operations = ["direct_fact"]
        if raw.get("supports_generic_comparison") is True:
            operations.append("returned_comparison")
        if item["supports_dimensions"]:
            operations.append("dimension_breakdown")
        if item["supports_change_decomposition"]:
            operations.append("complete_change_decomposition")
        if raw.get("target_gap_decomposition"):
            operations.append("complete_target_gap_decomposition")
        item["operation_summary"] = operations
        if raw.get("answer_contract") is not None:
            item["limitations"] = _copy_guidance(raw.get("answer_contract"))
        item["requires_metric_detail"] = (
            raw.get("exact_default_lookup_supported") is not True
        )
        metrics.append(item)
    return {
        "domain": domain,
        "level": "expert_index",
        "source_versions": _copy_guidance(planner.get("source_versions")),
        "metric_count": len(metrics),
        "metrics": metrics,
        "metric_selection_boundary": {
            "version": _METRIC_SELECTION_BOUNDARY_VERSION,
            "producer_scope": "candidate_index_only_no_match_classification",
            "branches": {
                "unique_compatible": {
                    "condition": (
                        "user wording and existing context leave one compatible "
                        "returned metric"
                    ),
                    "direct_query_when_all": {
                        "selected_metric.exact_default_lookup_supported": True,
                        "explicit_qualifiers_present": False,
                    },
                    "detail_first_when_any": [
                        {
                            "selected_metric.exact_default_lookup_supported": False
                        },
                        {
                            "selected_metric.exact_default_lookup_supported": "missing"
                        },
                        {
                            "explicit_qualifiers_present": True,
                            "examples": [
                                "calendar_month",
                                "time_range",
                                "dimensions",
                                "filters",
                                "entity",
                                "comparison",
                                "decomposition",
                                "ranking",
                            ],
                            "empty_values_do_not_count_as_present": {
                                "dimensions": []
                            },
                        },
                    ],
                    "actions": {
                        "direct_query": (
                            "query_selected_metric_at_exact_governed_default"
                        ),
                        "detail_first": (
                            "load_selected_metric_detail_before_query"
                        ),
                    },
                },
                "multiple_materially_distinct": {
                    "condition": (
                        "multiple materially distinct returned metrics remain "
                        "compatible with the user request"
                    ),
                    "next_step": "call_official_clarify",
                    "before_clarification_response": {
                        "metric_detail_calls": 0,
                        "datasage_query_calls": 0,
                    },
                },
                "zero_compatible": {
                    "condition": (
                        "no returned metric in the current requested domain is "
                        "compatible with the user request"
                    ),
                    "scope": "current_returned_domain_only",
                    "next_step": (
                        "report_domain_local_gap_or_call_official_clarify"
                    ),
                    "before_response": {
                        "metric_detail_calls": 0,
                        "datasage_query_calls": 0,
                    },
                    "cross_domain_check": {
                        "allowed_only_when": (
                            "user_semantics_explicitly_support_one_minimal_"
                            "related_domain"
                        ),
                        "action": "load_only_that_related_domain_expert_index",
                        "otherwise": (
                            "report_domain_local_gap_or_call_official_clarify"
                        ),
                    },
                    "forbidden": [
                        "enumerate_all_domains",
                        "claim_globally_unsupported",
                    ],
                },
            },
        },
    }


def _catalog_planning_guidance(planner: Mapping[str, Any]) -> dict[str, Any]:
    """Expose a bounded view copied from the versioned planner authority."""

    raw_guidance = planner.get("guidance")
    if not isinstance(raw_guidance, Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "planner guidance projection is invalid"
        )
    projection = {
        "version": _CATALOG_PLANNING_GUIDANCE_VERSION,
        **{
            key: _copy_guidance(raw_guidance[key])
            for key in _CATALOG_PLANNING_GUIDANCE_KEYS
            if key in raw_guidance
        },
    }
    if len(json.dumps(projection, ensure_ascii=False)) > (
        _MAX_CATALOG_PLANNING_GUIDANCE_JSON_CHARS
    ):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "planner guidance projection exceeds its bound"
        )
    return projection


def _analysis_affordances(
    domain: str,
    planner: Mapping[str, Any],
    *,
    selected_metric: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose planning capabilities without turning recipes into authority.

    The versioned planner contracts remain the business-owned source.  This
    projection deliberately describes proof capabilities and requirements,
    not question keywords, metric bundles, request payloads, or execution
    permission.
    """

    raw_metrics = planner.get("metrics")
    guidance = planner.get("guidance")
    if not isinstance(raw_metrics, list) or not isinstance(guidance, Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "分析能力目录格式无效。",
        )
    metrics = [item for item in raw_metrics if isinstance(item, Mapping)]
    if len(metrics) != len(raw_metrics):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "分析能力指标目录格式无效。",
        )
    domain_purpose = guidance.get("purpose")
    if not isinstance(domain_purpose, str) or not domain_purpose.strip():
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "分析能力缺少业务目标。",
        )

    capabilities: list[dict[str, str]] = [
        {
            "code": "direct_fact",
            "proof_capability": "result_or_state",
            "availability": "select_a_registered_metric_matching_the_requested_business_quantity",
        },
        {
            "code": "returned_comparison",
            "proof_capability": "benchmark_or_change",
            "availability": "when_the_selected_metric_time_scope_and_query_contract_support_comparison",
        },
    ]
    if any(
        item.get("allowed_dimensions") or item.get("allowed_dimension_set")
        for item in metrics
    ):
        capabilities.append(
            {
                "code": "governed_dimension_breakdown",
                "proof_capability": "structure",
                "availability": "only_for_dimensions_listed_by_the_selected_metric_detail",
            }
        )
    if any(item.get("change_decomposition_dimensions") for item in metrics):
        capabilities.append(
            {
                "code": "reconciled_change_decomposition",
                "proof_capability": "structural_contribution",
                "availability": "only_when_metric_detail_and_returned_reconciliation_both_authorize_it",
            }
        )
    if any(item.get("target_gap_decomposition") for item in metrics):
        capabilities.append(
            {
                "code": "reconciled_target_gap_decomposition",
                "proof_capability": "additive_target_actual_and_gap_composition",
                "availability": "only_when_metric_detail_advertises_the_operation_and_returned_target_gap_reconciliation_is_reconciled",
            }
        )
    if any(item.get("reasoning_topics") for item in metrics):
        capabilities.append(
            {
                "code": "reasoning_topic_follow_up",
                "proof_capability": "hypothesis_direction",
                "availability": "only_for_topics_listed_by_the_selected_metric_detail_and_never_as_a_causal_conclusion",
            }
        )

    affordances: dict[str, Any] = {
        "version": _ANALYSIS_AFFORDANCES_VERSION,
        "contract_role": "non_binding_planning_guidance",
        "domain_purpose": domain_purpose.strip(),
        "ownership": {
            "plan_owner": "Hermes",
            "execution_authority": "none",
            "authorization_source": "selected_metric_detail_and_datasage_query_validation",
        },
        "selection_policy": {
            "metric_count": "adaptive_not_fixed",
            "evidence_coverage": "choose_the_smallest_question_relevant_set_and_label_each_request_with_the_role_it_actually_serves",
            "first_pass": "batch_independent_evidence_when_practical",
            "follow_up": "continue_only_for_a_material_evidence_gap_that_could_change_the_conclusion",
            "stop_condition": "stop_when_more_evidence_cannot_materially_change_the_conclusion",
        },
        "capabilities": capabilities,
        "evidence_boundaries": {
            "comparative_judgment": "requires_a_returned_compatible_benchmark",
            "structural_contribution": "requires_reconciled_full_returned_rows_or_same_statement_full_partition_aggregate_proof_bounded_to_returned_rows_and_explicit_residual_never_an_ordinary_truncated_ranking",
            "cross_metric_direction_divergence": "observation_or_next_evidence_cue_only_not_proof_of_price_product_mix_placed_demand_or_business_cause_without_corresponding_returned_metric_or_dimension_evidence",
            "hypothesis": "must_be_labelled_and_tied_to_returned_evidence_or_an_explicit_evidence_gap",
            "causal_conclusion": "requires_independent_returned_business_evidence_beyond_structure_rate_movement_or_reasoning_topic",
        },
        "claim_wire_contracts": {
            "net_change_contribution_rate": {
                "field_path": "results[].claim_ledger[].facts.net_change_contribution_rate",
                "consume_only_when": [
                    "same_result_change_reconciliation_operation_is_complete_change_decomposition",
                    "same_result_change_reconciliation_status_is_reconciled",
                    "claim_is_validly_sealed",
                    "claim_allowed_relations_contains_structural_contribution",
                    "returned_overall_delta_is_nonzero",
                    "producer_returned_the_field",
                ],
                "success_boundary": "result_status_success_alone_never_authorizes_consumption",
                "wire_type": "decimal_string",
                "semantic_type": "signed_dimensionless_fraction",
                "scale": "one_equals_one_hundred_percent",
                "valid_range": "negative_and_absolute_value_greater_than_one_are_valid",
                "provenance": "sealed_returned_value_direct_use_only",
                "percentage_display": "multiply_by_100_exactly_once_with_one_consistent_display_precision",
                "forbidden_transformations": [
                    "recompute_from_visible_amounts",
                    "take_absolute_value",
                    "clamp",
                    "normalize_partition_rates_to_one_hundred_percent",
                    "force_partition_rates_to_sum_to_one_hundred_percent",
                    "invent_or_fill_when_absent",
                ],
                "absence_semantics": {
                    "zero_overall_delta": "field_absent_not_zero_rate",
                    "zero_partition_delta": "field_absent_not_zero_rate",
                    "producer_omission": "field_absent_never_infer_or_fill",
                },
            }
        },
    }
    if selected_metric is not None:
        metric_code = selected_metric.get("code")
        allowed_dimensions = selected_metric.get("allowed_dimensions")
        if (
            not isinstance(metric_code, str)
            or not metric_code
            or not isinstance(allowed_dimensions, list)
            or any(not isinstance(value, str) for value in allowed_dimensions)
        ):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                "所选指标的分析能力格式无效。",
            )
        change_planning: dict[str, Any] = {
            "contract_role": "non_binding_request_shape_guidance",
            "metric_code": metric_code,
            "execution_authority": "none",
        }
        if selected_metric.get("supports_generic_comparison") is True:
            change_planning["change_extreme_ranking"] = {
                "availability": "requires_a_returned_compatible_comparison_and_one_dimension_from_metric_allowed_dimensions",
                "largest_decline": {
                    "order_by": {"field": "delta_value", "direction": "asc"}
                },
                "largest_increase": {
                    "order_by": {"field": "delta_value", "direction": "desc"}
                },
                "global_extreme_claim": "requires_returned_governed_ordering_matching_the_requested_delta_direction",
                "truncated_result": "may_support_the_returned_ranked_top_when_governed_ordering_matches_but_never_a_complete_population_or_contribution_sum",
                "mismatched_ordering": "never_claim_a_change_extreme_from_a_truncated_result_when_the_returned_ordering_does_not_match",
            }
        if selected_metric.get("change_decomposition_dimensions"):
            change_planning["complete_change_decomposition"] = {
                "availability": "only_for_a_dimension_listed_in_metric_change_decomposition_dimensions",
                "planning_decision": "when_the_answer_needs_complete_structure_or_contribution_select_complete_change_decomposition_for_the_Hermes_selected_dimension_not_a_ranked_shape",
                "planning_ownership": {
                    "owner": "Hermes",
                    "retained_choices": [
                        "whether_complete_structure_is_material_to_the_answer",
                        "selected_metric",
                        "selected_dimension",
                        "time_entity_and_business_scope",
                        "any_additional_question_relevant_evidence",
                        "follow_up_and_stop_decision",
                    ],
                    "query_role": "validate_and_execute_only_without_selecting_the_analysis_plan",
                },
                "evidence_shape_separation": {
                    "discovery_ranking": "ranked_change_observation_only_not_a_reconciled_decomposition",
                    "reconciled_decomposition": "returned_same_scope_overall_and_either_full_non_truncated_dimension_rows_or_a_bounded_partition_with_same_statement_window_full_partition_aggregate_proof",
                    "bounded_same_statement_proof": {
                        "authorization": "returned_nonzero_rows_structural_contribution_only",
                        "population_detail": "incomplete_hidden_tail_and_residual_must_be_explicit",
                        "complete_population_claims_returned": False,
                        "structural_contributor_count_semantics": "returned_nonzero_contributor_count_covers_returned_rows_only_full_partition_row_count_covers_population",
                        "causality": "never_authorized",
                    },
                    "metric_identity_explanation": "controlled_metric_definition_only_not_governed_dimension_structural_contribution_not_a_substitute_for_returned_reconciliation_and_not_causal_evidence",
                },
                "scope_change": {
                    "trigger": "focusing_or_filtering_to_an_entity_creates_a_new_scope",
                    "required_evidence": "issue_complete_change_decomposition_with_the_new_scope_selected_by_Hermes",
                    "overall_reuse": "never_reuse_an_overall_request_from_a_different_scope",
                },
                "focus_transition": {
                    "discovery_dimension_role": "identifies_the_entity_to_focus_only_not_the_default_structural_dimension_after_focus",
                    "focused_scope_definition": "the_complete_change_decomposition_request_includes_the_selected_entity_as_a_metric_filter",
                    "focused_dimension_decision": "Hermes_reselects_a_question_relevant_code_from_metric_change_decomposition_dimensions_for_the_new_scope",
                    "dimension_reuse_boundary": "never_inherit_the_discovery_dimension_as_a_default_after_focus",
                    "selection_owner": "Hermes",
                },
                "model_facing_request_shapes": {
                    "preferred": {
                        "complete_change_decomposition": {
                            "dimension": "one_code_from_metric_change_decomposition_dimensions",
                        },
                    },
                    "compatibility_form": {
                        "contract_role": "legacy_compatibility_only",
                        "overall": "same_metric_dimensionless_comparison",
                        "decomposition": "same_metric_full_non_truncated_dimension_comparison",
                        "decomposition_of_request_id": "overall_request_id",
                    },
                    "mutual_exclusion": "never_mix_the_preferred_and_compatibility_forms_in_one_request",
                    "ranking_shape": "a_request_with_order_by_or_limit_is_discovery_ranking_not_a_complete_decomposition",
                },
                "query_execution_boundary": {
                    "attempt": "within_existing_governed_execution_limits_return_the_same_scope_overall_and_complete_partition_for_the_Hermes_selected_metric_dimension_and_scope",
                    "ordinary_limit_exceeded": "return_bounded_observation_with_not_reconciled_status_never_structural_contribution",
                    "same_statement_aggregate_proof": "may_reconcile_full_partition_aggregates_but_authorizes_only_returned_nonzero_rows_with_hidden_tail_counts_and_residuals_explicit",
                    "planning_effect": "none_does_not_require_this_operation_or_block_other_returned_evidence",
                },
                "completion_check": {
                    "returned_evidence": "returned_change_reconciliation_status",
                    "required_value": "reconciled",
                    "complete_population_claims_returned": "true_only_when_the_full_partition_rows_are_returned_false_for_same_statement_aggregate_proof",
                    "otherwise": "material_evidence_gap_do_not_claim_structure_or_contribution",
                },
                "acceptance": "returned_change_reconciliation_status_reconciled",
                "allowed_conclusion": "returned_governed_dimension_structural_contribution_only_never_complete_hidden_tail_detail_or_causality",
            }
        affordances["selected_metric_change_planning"] = change_planning
        target_gap = selected_metric.get("target_gap_decomposition")
        if isinstance(target_gap, Mapping):
            affordances["selected_metric_target_gap_planning"] = {
                "contract_role": "non_binding_request_shape_guidance",
                "metric_code": metric_code,
                "execution_authority": "none",
                "complete_target_gap_decomposition": {
                    "availability": "only_for_a_dimension_listed_in_metric_target_gap_decomposition_dimensions_and_the_required_attribution_mode",
                    "model_facing_request_shape": {
                        "complete_target_gap_decomposition": {
                            "dimension": "one_code_from_metric_target_gap_decomposition_dimensions"
                        }
                    },
                    "acceptance": "returned_target_gap_reconciliation_operation_matches_and_status_is_reconciled",
                    "allowed_conclusion": "returned_additive_target_actual_and_gap_composition_only_completion_rate_is_not_additive_and_causality_is_never_authorized",
                    "failure_fallback": "when_reconciliation_is_not_reconciled_or_missing_preserve_returned_local_facts_gaps_and_typed_states_without_a_complete_composition_claim",
                    "planning_effect": "failure_is_local_and_does_not_block_other_independently_governed_evidence",
                },
            }
    _assert_business_safe_tree(
        affordances,
        context=f"domain {domain} analysis affordances",
    )
    return affordances


def _catalog_metric_detail(
    domain: str,
    metric_code: str,
    planner: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one metric and only the dimensions that it can execute."""

    raw_metrics = planner.get("metrics")
    dimension_sets = planner.get("allowed_dimension_sets")
    raw_dimensions = planner.get("dimensions")
    if (
        not isinstance(raw_metrics, list)
        or not isinstance(dimension_sets, Mapping)
        or not isinstance(raw_dimensions, list)
    ):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "指标详细目录格式无效。")
    metric = next(
        (
            dict(raw)
            for raw in raw_metrics
            if isinstance(raw, Mapping) and raw.get("code") == metric_code
        ),
        None,
    )
    if metric is None:
        raise ContractFailure(
            "METRIC_UNAVAILABLE",
            f"业务域 {domain} 未发布指标 {metric_code}。",
        )
    set_id = metric.pop("allowed_dimension_set", None)
    raw_allowed = metric.pop("allowed_dimensions", None)
    if isinstance(raw_allowed, list):
        allowed_dimensions = [str(value) for value in raw_allowed]
    elif isinstance(set_id, str) and isinstance(dimension_sets.get(set_id), list):
        allowed_dimensions = [
            str(value) for value in dimension_sets[set_id]
        ]
    else:
        allowed_dimensions = []
    definitions = {
        str(raw.get("code")): dict(raw)
        for raw in raw_dimensions
        if isinstance(raw, Mapping) and isinstance(raw.get("code"), str)
    }
    metric["allowed_dimensions"] = allowed_dimensions
    if metric.get("change_decomposition_dimensions"):
        metric["change_decomposition_policy"] = (
            "structural_contribution_after_reconciled_full_rows_or_same_statement_full_partition_aggregate_proof_with_bounded_claims"
        )
    if metric.get("reasoning_topics"):
        metric["reasoning_topic_policy"] = (
            "hypothesis_or_next_evidence_direction_only_not_a_causal_conclusion"
        )
    return {
        "domain": domain,
        "level": "metric",
        "source_versions": _copy_guidance(planner.get("source_versions")),
        "planning_guidance": _catalog_planning_guidance(planner),
        "analysis_affordances": _analysis_affordances(
            domain,
            planner,
            selected_metric=metric,
        ),
        "metric": metric,
        "dimensions": [
            definitions[code]
            for code in allowed_dimensions
            if code in definitions
        ],
    }


def _catalog_metric_detail_receipt(detail: Mapping[str, Any]) -> str:
    """Seal one metric detail independently of its surrounding catalog batch."""

    canonical = {
        "schema": "datasage-metric-detail-receipt/v1",
        "catalog_version": _CATALOG_VERSION,
        "detail": detail,
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _catalog_performance_scorecard() -> dict[str, Any]:
    """Resolve the code-owned scorecard through ordinary metric contracts."""

    manifest = performance_scorecard_manifest()
    raw_bundle = manifest.get("recommended_bundle")
    if not isinstance(raw_bundle, list):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "performance scorecard bundle is invalid"
        )
    metric_details: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_bundle:
        if not isinstance(item, Mapping):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "performance scorecard item is invalid"
            )
        template = item.get("request_template")
        if not isinstance(template, Mapping):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "performance scorecard request is invalid"
            )
        domain = template.get("domain")
        metric = template.get("metric")
        if domain not in _DOMAIN_FOLDERS or not isinstance(metric, str):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "performance scorecard metric is invalid"
            )
        identity = (str(domain), metric)
        if identity in seen:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "performance scorecard metric is duplicated"
            )
        seen.add(identity)
        planner = _domain_contract(str(domain), "planner")["planner"]
        detail = _catalog_metric_detail(str(domain), metric, planner)
        metric_details.append(
            {
                "lens": item.get("lens"),
                "time_binding": item.get("time_binding"),
                "request_template": _copy_guidance(template),
                "detail": detail,
                "detail_receipt": _catalog_metric_detail_receipt(detail),
            }
        )
    return {
        "level": "performance_scorecard",
        "recipe": manifest,
        "metric_count": len(metric_details),
        "metric_details": metric_details,
    }


def datasage_catalog(args: dict[str, Any], **_kwargs: Any) -> str:
    """Return governed planning catalogs without owning the conversation."""
    try:
        if not isinstance(args, dict) or set(args) != {"requests"}:
            raise ContractFailure("INVALID_INPUT", "目录参数只接受 requests。")
        requests = args.get("requests")
        if not isinstance(requests, list) or not 1 <= len(requests) <= 6:
            raise ContractFailure("INVALID_INPUT", "requests 必须包含一到六个域合同请求。")
        normalized: list[tuple[str | None, str | None, str | None]] = []
        for request in requests:
            if (
                not isinstance(request, Mapping)
                or set(request) - {"domain", "metric", "view"}
            ):
                raise ContractFailure("INVALID_INPUT", "目录请求格式无效。")
            domain = request.get("domain")
            metric = request.get("metric")
            view = request.get("view")
            if view == "performance_scorecard":
                if set(request) != {"view"}:
                    raise ContractFailure(
                        "INVALID_INPUT",
                        "performance_scorecard view does not accept domain or metric.",
                    )
                normalized.append((None, None, "performance_scorecard"))
                continue
            if domain not in _DOMAIN_FOLDERS:
                raise ContractFailure("INVALID_INPUT", "业务域不受支持。")
            if metric is not None and (
                not isinstance(metric, str)
                or not metric.strip()
                or len(metric) > 100
            ):
                raise ContractFailure("INVALID_INPUT", "metric 格式无效。")
            if view is not None and view not in {"expert_index", "full", "audit"}:
                raise ContractFailure("INVALID_INPUT", "view 不受支持。")
            if metric is not None and view is not None:
                raise ContractFailure(
                    "INVALID_INPUT",
                    "metric detail request cannot be combined with view.",
                )
            normalized.append(
                (
                    str(domain),
                    metric.strip() if isinstance(metric, str) else None,
                    str(view) if isinstance(view, str) else None,
                )
            )
        if len(set(normalized)) != len(normalized):
            raise ContractFailure("INVALID_INPUT", "同一个目录请求不能重复。")
        results: list[dict[str, Any]] = []
        for domain, metric, view in normalized:
            if view == "performance_scorecard":
                result = _catalog_performance_scorecard()
                results.append(result)
                continue
            if domain is None:
                raise ContractFailure("INVALID_INPUT", "目录请求缺少业务域。")
            planner = _domain_contract(domain, "planner")["planner"]
            if metric is not None:
                result = _catalog_metric_detail(domain, metric, planner)
                result["detail_receipt"] = _catalog_metric_detail_receipt(result)
            elif view == "expert_index":
                result = _catalog_expert_index(domain, planner)
            elif view in {"full", "audit"}:
                result = _catalog_summary(domain, planner)
                result["projection_mode"] = "audit_full"
            else:
                result = _catalog_summary(domain, planner)
            results.append(result)
        payload: dict[str, Any] = {
            "status": "success",
            "catalog_version": _CATALOG_VERSION,
            "contract_role": "governed_metric_catalog",
            "query_policy": _query_policy_projection(),
            "results": results,
        }
        payload["content_hash"] = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    except ContractFailure as failure:
        payload = {
            "status": "failed",
            "error": {"code": failure.code, "message": failure.message},
        }
    except Exception:
        payload = {
            "status": "failed",
            "error": {"code": "INTERNAL_ERROR", "message": "语义合同工具暂时不可用。"},
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
