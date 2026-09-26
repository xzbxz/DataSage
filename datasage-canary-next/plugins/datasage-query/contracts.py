"""Read-only model-facing semantic catalog for DataSage Expert."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .capability_contract import (
    DOMAIN_SOURCES,
    CapabilityContractError,
    FLOW_COMPARISON_KINDS,
    SNAPSHOT_MONTHS_BEFORE_COMPARISON,
    assert_capability_boundary,
)
from . import analytical_handlers, capability_contract, contract_store, public_fields, currency_basis
from .query_errors import QueryFailure
from .scorecard import performance_scorecard_manifest

_MODEL_PROJECTION_VERSION = "datasage-model-semantic-projection/v6"
_CATALOG_VERSION = "datasage-metric-catalog/v1"
_DATASETS_CONTRACT_PATH = "plugins/datasage-query/contracts/datasets.yaml"
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
def metric_comparison_kinds(definition: Mapping[str, Any]) -> list[str]:
    """Return only comparison kinds accepted by the metric execution path."""

    if (
        definition.get("query_kind") is not None
        or definition.get("required_time_bucket") is not None
    ):
        return []
    time_policy = definition.get("time_policy")
    time_field = definition.get("time_field")
    if time_policy == "latest_snapshot" and isinstance(time_field, str) and time_field:
        return [SNAPSHOT_MONTHS_BEFORE_COMPARISON]
    if time_policy not in {
        None,
        "",
        "current_snapshot",
        "latest_snapshot",
        "latest_non_null_snapshot",
    }:
        return list(FLOW_COMPARISON_KINDS)
    return []
_FORBIDDEN_MODEL_KEYS = {
    "public_display_fields",
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
    "public_display_fields",
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
_TABLE_REFERENCE = re.compile(r"(?i)\bvk_(?:dw|dwd|ods|ads|ai)\.[A-Za-z_][A-Za-z0-9_]*\b")
_FORMULA_EXPRESSION = re.compile(r"(?i)\b(?:avg|count|max|min|sum)\s*\(")


class ContractFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _read_yaml(relative_path: str) -> dict[str, Any]:
    try:
        return contract_store.read_yaml(relative_path)
    except contract_store.ContractStoreError as exc:
        if "escapes" in exc.message:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "语义合同路径不安全。"
            ) from exc
        if "mapping" in exc.message:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "语义合同格式无效。"
            ) from exc
        raise ContractFailure("CONTRACT_UNAVAILABLE", "暂时无法读取语义合同。") from exc


def _contract_source_version(
    relative_path: str,
    contract: Mapping[str, Any],
    *,
    source_name: str,
) -> dict[str, str]:
    """Return a public version plus the exact content identity of a contract."""

    version = contract.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"{source_name} contract version is invalid",
        )
    try:
        _path, digest = contract_store.content_signature(relative_path)
    except contract_store.ContractStoreError as exc:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"{source_name} contract content identity is unavailable",
        ) from exc
    return {
        "version": version,
        "content_sha256": digest,
    }


def _execution_source_versions(
    datasets: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    """Bind detail projections to the common execution contracts."""

    try:
        query_policy = contract_store.read_query_policy()
    except (
        contract_store.ContractStoreError,
        capability_contract.CapabilityContractError,
    ) as exc:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "common query policy is invalid"
        ) from exc
    return {
        "datasets": _contract_source_version(
            _DATASETS_CONTRACT_PATH,
            datasets,
            source_name="datasets",
        ),
        "query_policy": _contract_source_version(
            capability_contract.QUERY_POLICY_PATH,
            query_policy.as_mapping(),
            source_name="query policy",
        ),
    }


def execution_contracts(domain: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load executor-facing datasets and semantics from the shared store."""

    source = DOMAIN_SOURCES.get(domain)
    if not isinstance(source, Mapping):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "业务域语义合同不存在。")
    return (
        _read_yaml("plugins/datasage-query/contracts/datasets.yaml"),
        _read_yaml(str(source["semantics"])),
    )


def _query_policy_projection() -> dict[str, Any]:
    """Load the single versioned planning/execution policy authority."""

    try:
        policy = contract_store.read_query_policy()
    except (
        contract_store.ContractStoreError,
        capability_contract.CapabilityContractError,
    ) as exc:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "common query policy is invalid"
        ) from exc
    return _copy_guidance(policy.as_mapping())


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


def _value_contract_projection(
    raw: Any,
    *,
    dimension: str,
    filterable_default: bool = True,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"dimension {dimension} has an invalid value_contract",
        )
    _assert_business_safe_tree(raw, context=f"dimension {dimension} value_contract")
    try:
        contract = capability_contract.parse_value_contract(
            raw,
            filterable_default=filterable_default,
        )
    except capability_contract.CapabilityContractError as exc:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"dimension {dimension} has an invalid value_contract",
        ) from exc
    return _copy_guidance(contract.as_mapping())


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


def _related_metric_refs_projection(
    domain: str,
    semantics: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Validate and project explicit cross-domain metric references.

    These references are capability metadata only.  They do not select a plan,
    execute a query, or create a new lens engine.
    """

    if "evidence_axes" in semantics:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "legacy evidence_axes are not executable metadata",
        )
    raw_refs = semantics.get("related_metric_refs")
    if raw_refs is None:
        return []
    if not isinstance(raw_refs, list) or not raw_refs:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"domain {domain} related_metric_refs are invalid",
        )

    projected: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, Mapping) or set(raw_ref) != {"domain", "metric"}:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"domain {domain} related_metric_refs must contain domain and metric",
            )
        ref_domain = raw_ref.get("domain")
        ref_metric = raw_ref.get("metric")
        if (
            not isinstance(ref_domain, str)
            or ref_domain not in DOMAIN_SOURCES
            or ref_domain != ref_domain.strip()
            or not isinstance(ref_metric, str)
            or not ref_metric
            or ref_metric != ref_metric.strip()
        ):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"domain {domain} related_metric_refs contain an invalid metric reference",
            )
        identity = (ref_domain, ref_metric)
        if identity in seen:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"domain {domain} related_metric_refs contain a duplicate",
            )
        seen.add(identity)

        referenced_semantics = _read_yaml(
            str(DOMAIN_SOURCES[ref_domain]["semantics"])
        )
        referenced_metrics = referenced_semantics.get("metrics")
        referenced_definition = (
            referenced_metrics.get(ref_metric)
            if isinstance(referenced_metrics, Mapping)
            else None
        )
        if not isinstance(referenced_definition, Mapping):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"related metric {ref_domain}.{ref_metric} does not exist",
            )
        if _is_unavailable(referenced_definition):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"related metric {ref_domain}.{ref_metric} is unavailable",
            )
        projected.append({"domain": ref_domain, "metric": ref_metric})
    return projected


def _metric_group_dimension_limit(
    definition: Mapping[str, Any], allowed_dimensions: list[str]
) -> int:
    """Return the governed grouping arity exposed to planner and executor."""

    raw_limit = definition.get("max_group_dimensions")
    try:
        limit = capability_contract._metric_group_dimension_limit(definition)
    except CapabilityContractError as exc:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "analytical metric lacks max_group_dimensions",
        ) from exc
    if raw_limit is None:
        return min(limit, len(allowed_dimensions))
    if limit > len(allowed_dimensions):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "metric max_group_dimensions is invalid",
        )
    return limit


def _metric_operation_summary(definition: Mapping[str, Any]) -> list[str]:
    """Return the execution operations exposed for one projected metric."""

    operations = ["direct_fact"]
    if definition.get("comparison_kinds"):
        operations.append("returned_comparison")
    if definition.get("allowed_dimensions") or definition.get(
        "allowed_dimension_set"
    ):
        operations.append("dimension_breakdown")
    if definition.get("change_decomposition_dimensions"):
        operations.append("complete_change_decomposition")
    if definition.get("target_gap_decomposition"):
        operations.append("complete_target_gap_decomposition")
    raw_modes = definition.get("allowed_attribution_modes")
    modes = {
        str(mode)
        for mode in raw_modes
        if isinstance(mode, str) and mode
    } if isinstance(raw_modes, list) else set()
    by_mode = definition.get("dimensions_by_attribution_mode")
    if isinstance(by_mode, Mapping):
        modes.update(str(mode) for mode in by_mode if str(mode))
    if len(modes) <= 1:
        return operations

    mode_operations = _metric_operation_summary_by_attribution_mode(definition)
    if not mode_operations:
        return operations
    common = set.intersection(
        *(set(values) for values in mode_operations.values())
    )
    return [operation for operation in operations if operation in common]


def _metric_operation_summary_by_attribution_mode(
    definition: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Keep path-specific operations separate from the metric-wide union.

    A target completion metric can support customer grouping on the ordinary
    transaction-detail path while supporting salesperson grouping on the
    allocation path.  Publishing one flat operation list for both paths would
    invite the planner to combine an operation with the wrong ledger.
    """

    raw_by_mode = definition.get("dimensions_by_attribution_mode")
    by_mode = raw_by_mode if isinstance(raw_by_mode, Mapping) else {}
    declared_modes: list[str] = []
    raw_modes = definition.get("allowed_attribution_modes")
    if isinstance(raw_modes, list):
        declared_modes.extend(
            str(mode) for mode in raw_modes if isinstance(mode, str) and mode
        )
    required_mode = definition.get("required_attribution_mode")
    if isinstance(required_mode, str) and required_mode:
        declared_modes.append(required_mode)
    declared_modes.extend(str(mode) for mode in by_mode if str(mode))
    modes = sorted(set(declared_modes))
    if not modes:
        return {}

    target_gap = definition.get("target_gap_decomposition")
    target_gap_mode = (
        target_gap.get("required_attribution_mode")
        if isinstance(target_gap, Mapping)
        else None
    )
    result: dict[str, list[str]] = {}
    for mode in modes:
        dimensions = by_mode.get(mode)
        has_dimensions = isinstance(dimensions, list) and bool(dimensions)
        if not has_dimensions and mode == required_mode:
            has_dimensions = bool(definition.get("allowed_dimensions"))
        operations = ["direct_fact"]
        if definition.get("comparison_kinds"):
            operations.append("returned_comparison")
        if has_dimensions:
            operations.append("dimension_breakdown")
        if definition.get("change_decomposition_dimensions"):
            operations.append("complete_change_decomposition")
        if target_gap_mode == mode:
            operations.append("complete_target_gap_decomposition")
        result[mode] = operations
    return result


def _metric_allowed_dimensions(
    metric: Mapping[str, Any], planner: Mapping[str, Any]
) -> list[str]:
    """Resolve compressed dimension authorization for catalog views."""

    raw_allowed = metric.get("allowed_dimensions")
    if isinstance(raw_allowed, list):
        return [str(value) for value in raw_allowed if isinstance(value, str)]
    set_id = metric.get("allowed_dimension_set")
    dimension_sets = planner.get("allowed_dimension_sets")
    if isinstance(set_id, str) and isinstance(dimension_sets, Mapping):
        values = dimension_sets.get(set_id)
        if isinstance(values, list):
            return [str(value) for value in values if isinstance(value, str)]
    return []


def _is_unavailable(definition: Mapping[str, Any]) -> bool:
    try:
        status = capability_contract.validate_availability(definition)
    except capability_contract.AvailabilityContractError as exc:
        raise ContractFailure(exc.code, exc.message) from exc
    return status != "available"


def delivery_scope_policy(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Read the selected metric's sole scope authority; no metric-name inference."""
    policy = definition.get("delivery_scope_policy")
    if not isinstance(policy, Mapping) or set(policy) != {"allowed_scopes", "required", "default"}:
        raise ContractFailure("CONTRACT_UNAVAILABLE", "交付指标范围合同缺失或格式无效。")
    allowed = policy.get("allowed_scopes")
    required = policy.get("required")
    default = policy.get("default")
    if (
        not isinstance(allowed, (list, tuple)) or not allowed
        or any(not isinstance(value, str) or value not in capability_contract.DELIVERY_SCOPES for value in allowed)
        or len(set(allowed)) != len(allowed)
        or not isinstance(required, bool)
        or (required and default is not None)
        or (not required and default not in allowed)
    ):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "交付指标范围合同的允许值、必选或默认值不一致。")
    return {"allowed_scopes": list(allowed), "required": required, "default": default}


def _delivery_scope_flags(
    metric_code: str,
    definition: Mapping[str, Any],
    semantics: Mapping[str, Any],
) -> dict[str, bool]:
    """Return conservative business-scope flags without physical fields."""

    default_disclosures = semantics.get("default_disclosures")
    inherited = (
        default_disclosures
        if isinstance(default_disclosures, (list, tuple))
        else ()
    )
    external_customers_only = any(
        isinstance(item, Mapping)
        and item.get("id") == "delivery.external-customer.scope"
        and item.get("mode") == "required_always"
        for item in inherited
    )
    if definition.get("query_kind") == "fabric_source":
        # Source summaries retain policy drift as unresolved rows, rather than
        # claiming every returned source record is a verified external sale.
        external_customers_only = False

    completed_returns_only = (
        definition.get("time_field") == "statement_time"
        and not isinstance(definition.get("ratio"), Mapping)
    )
    return {
        "external_customers_only": external_customers_only,
        "completed_returns_only": completed_returns_only,
    }


def _answer_boundary_summary(
    definition: Mapping[str, Any],
    semantics: Mapping[str, Any],
    physical_identifiers: set[str],
) -> list[str]:
    """Project only safe required disclosure prose for planning context."""

    candidates: list[Any] = []
    inherited = semantics.get("default_disclosures")
    if isinstance(inherited, (list, tuple)):
        candidates.extend(inherited)
    declared = definition.get("disclosures")
    if isinstance(declared, (list, tuple)):
        candidates.extend(declared)

    summary: list[str] = []
    for raw in candidates:
        if not isinstance(raw, Mapping) or raw.get("mode") != "required_always":
            continue
        text = _safe_business_text(raw.get("text"), physical_identifiers)
        if text is not None and text not in summary:
            summary.append(text)

    return summary


def _pending_capability_projection(
    domain: str,
    metric_code: str,
    definition: Mapping[str, Any],
    semantics: Mapping[str, Any],
    physical_identifiers: set[str],
) -> dict[str, Any]:
    """Expose a blocked capability without authorizing it for execution."""

    availability = definition.get("availability")
    if not isinstance(availability, Mapping):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"metric {metric_code} has an invalid availability contract",
        )
    status = availability.get("status")
    error_code = availability.get("error_code")
    raw_reason = availability.get("message")
    if (
        not isinstance(status, str)
        or not status
        or not isinstance(error_code, str)
        or not error_code
    ):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            f"metric {metric_code} has an invalid availability contract",
        )
    label = _safe_business_text(definition.get("label"), physical_identifiers)
    reason = _safe_business_text(raw_reason, physical_identifiers)
    if label is None:
        label = metric_code
    if reason is None:
        reason = "This governed capability is pending validation."
    error = {"code": error_code, "message": reason}
    result: dict[str, Any] = {
        "code": metric_code,
        "label": label,
        "status": status,
        "reason": reason,
        "error": error,
        "selectable": False,
        "operation_summary": [],
        "supports_dimensions": False,
        "supports_change_decomposition": False,
    }
    # Delivery scope facts are owned only by delivery metrics. Pending
    # capabilities in other domains must remain discoverable without importing
    # delivery-only policy into their public contract.
    if domain == "delivery":
        result["delivery_scope_policy"] = delivery_scope_policy(definition)
        result["scope_flags"] = _delivery_scope_flags(metric_code, definition, semantics)
    activation_gate = availability.get("activation_gate")
    if isinstance(activation_gate, Mapping):
        state = activation_gate.get("state")
        if isinstance(state, str) and state:
            result["activation_gate"] = state
    answer_boundary_summary = _answer_boundary_summary(
        definition, semantics, physical_identifiers
    )
    if answer_boundary_summary:
        result["answer_boundary_summary"] = answer_boundary_summary
    return result


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
    try:
        contract = contract_store.read_target_gap_contract(
            f"plugins/datasage-query/{reference}"
        )
    except (
        contract_store.ContractStoreError,
        capability_contract.CapabilityContractError,
    ) as exc:
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE",
            "target gap decomposition capability is not active",
        ) from exc
    return {
        "version": contract.version,
        "operation": contract.receipt_operation,
        "metrics": list(contract.metrics),
        "dimensions": list(contract.dimensions),
        "required_attribution_mode": contract.attribution_mode,
    }


def _model_dimension_projection(code, definition, physical_identifiers):
    business_definition = definition.get("business_definition")
    if business_definition is None:
        business_definition = definition.get("semantics")
    label = _safe_business_text(definition.get("label"), physical_identifiers)
    value_contract = _value_contract_projection(
        definition.get("value_contract"),
        dimension=code,
        filterable_default=definition.get("filterable", True),
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
    return item


def _model_semantic_projection(
    domain: str,
    semantics: Mapping[str, Any],
    *,
    source_versions: Mapping[str, Any] | None = None,
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
    related_metric_refs = _related_metric_refs_projection(domain, semantics)
    projected_metrics: list[dict[str, Any]] = []
    pending_capabilities: list[dict[str, Any]] = []
    known_dimensions = {str(code) for code in dimensions}
    for raw_code in sorted(metrics, key=str):
        code = str(raw_code)
        definition = metrics[raw_code]
        if not isinstance(definition, Mapping):
            raise ContractFailure("CONTRACT_UNAVAILABLE", f"metric {code} is invalid")
        try:
            capability_contract.validate_metric_execution_contract(definition)
        except CapabilityContractError as exc:
            raise ContractFailure(exc.code, exc.message) from exc
        # Unverified data paths are absent from model authorization. Keep a
        # separate non-executable capability index for every domain so Hermes
        # can distinguish a known pending capability from an unknown metric.
        if _is_unavailable(definition):
            pending_capabilities.append(
                _pending_capability_projection(
                    domain,
                    code,
                    definition,
                    semantics,
                    physical_identifiers,
                )
            )
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
        label = _safe_business_text(definition.get("label"), physical_identifiers)
        business_definition = _safe_business_text(
            definition.get("business_definition"), physical_identifiers
        )
        if label is None or business_definition is None:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE",
                f"metric {code} lacks a safe business label or definition",
            )
        comparison_kinds = metric_comparison_kinds(definition)
        required_time_bucket = definition.get("required_time_bucket")
        if required_time_bucket is not None:
            if (
                required_time_bucket not in {"day", "month"}
                or not isinstance(definition.get("time_field"), str)
            ):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE",
                    f"metric {code} has an invalid required time bucket",
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
            "comparison_kinds": comparison_kinds,
            "supports_generic_comparison": bool(comparison_kinds),
        }
        try:
            grouping=capability_contract.metric_grouping(definition)
        except capability_contract.CapabilityContractError as exc:
            raise ContractFailure(exc.code,exc.message) from exc
        if grouping is not None:item['grouping']=grouping
        buckets = capability_contract.analytical_time_buckets(definition)
        if buckets is not None:item['allowed_time_buckets']=buckets
        handler = analytical_handlers.get_handler(definition.get('query_kind'))
        if handler is not None and 'order_by' in handler.forbidden_parameters:
            item['ordering'] = {'fields': [], 'directions': []}
        if definition.get("query_kind") == "target_completion":
            amount_fields = [field for field in capability_contract.target_completion_fact_units(definition)
                             if field not in {"metric_value", "completion_rate"}]
            item["period_summary_fields"] = amount_fields
            item["ordering"] = {"fields": ["completion_rate", *amount_fields], "directions": ["asc", "desc"], "default": {"field": "completion_rate", "direction": "desc"}, "rank_scope": "queried_population_if_all_ranking_values_known", "complete_target_gap_requires_full_partition": True}
        if required_time_bucket is not None:
            item["required_time_bucket"] = required_time_bucket
        if change_decomposition_dimensions:
            item["change_decomposition_orderings"] = list(capability_contract.CHANGE_DIRECTIONS)
            item["change_decomposition_dimensions"] = (
                change_decomposition_dimensions
            )
        answer_note = _safe_business_text(
            definition.get("answer_note"), physical_identifiers
        )
        try:
            basis_capability = currency_basis.capability(code, semantics)
        except QueryFailure as exc:
            raise ContractFailure(exc.code, exc.message) from exc
        optional_metric_fields = {
            "currency_basis": basis_capability,
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
        item["operation_summary"] = _metric_operation_summary(item)
        operation_summary_by_mode = _metric_operation_summary_by_attribution_mode(
            item
        )
        if operation_summary_by_mode:
            item["operation_summary_by_attribution_mode"] = (
                operation_summary_by_mode
            )
        scopes = definition.get("inventory_scope_filters")
        if isinstance(scopes, Mapping):
            item["available_inventory_scopes"] = sorted(str(scope) for scope in scopes)
        if isinstance(definition.get("default_inventory_scope"), str):
            item["default_inventory_scope"] = definition["default_inventory_scope"]
        if domain == "delivery":
            item["delivery_scope_policy"] = delivery_scope_policy(definition)
            item["scope_flags"] = _delivery_scope_flags(
                code, definition, semantics
            )
        answer_boundary_summary = _answer_boundary_summary(
            definition, semantics, physical_identifiers
        )
        if answer_boundary_summary:
            item["answer_boundary_summary"] = answer_boundary_summary
        projected_metrics.append(item)

    executable_dimension_codes = {
        dimension
        for metric in projected_metrics
        for dimension in metric["allowed_dimensions"]
    }
    projected_dimensions: list[dict[str, Any]] = []
    for raw_code in sorted(dimensions, key=str):
        code = str(raw_code)
        if code not in executable_dimension_codes:
            continue
        definition = dimensions[raw_code]
        if not isinstance(definition, Mapping):
            raise ContractFailure("CONTRACT_UNAVAILABLE", f"dimension {code} is invalid")
        projected_dimensions.append(_model_dimension_projection(code, definition, physical_identifiers))

    compressed_metrics, allowed_dimension_sets = (
        _compress_metric_dimension_sets(projected_metrics)
    )
    projected_source_versions: dict[str, Any] = {
        "semantics": semantics.get("version"),
    }
    if target_gap_decomposition is not None:
        projected_source_versions["target_gap_decomposition"] = (
            target_gap_decomposition["version"]
        )
    if source_versions is not None:
        projected_source_versions.update(_copy_guidance(source_versions))
    projection = {
        "version": _MODEL_PROJECTION_VERSION,
        "domain": domain,
        "source_versions": projected_source_versions,
        "metrics": compressed_metrics,
        "allowed_dimension_sets": allowed_dimension_sets,
        "dimensions": projected_dimensions,
    }
    if pending_capabilities:
        projection["pending_capabilities"] = pending_capabilities
    if related_metric_refs:
        projection["related_metric_refs"] = related_metric_refs
    _assert_business_safe_tree(
        projection,
        context=f"domain {domain} semantic projection",
        physical_identifiers=physical_identifiers,
    )
    try:
        assert_capability_boundary(projection)
    except CapabilityContractError as exc:
        raise ContractFailure("CONTRACT_UNAVAILABLE", exc.message) from exc
    return projection


def _domain_contract(domain: str, view: str) -> dict[str, Any]:
    if view != "planner":
        raise ContractFailure(
            "DETAIL_CONTRACT_UNAVAILABLE",
            "当前发布版本尚未开放语义明细合同，请使用已登记指标和维度。",
        )
    datasets, semantics = execution_contracts(domain)
    return {
        "planner": _model_semantic_projection(
            domain,
            semantics,
            source_versions=_execution_source_versions(datasets),
        )
    }


def _capability_affordances(
    domain: str,
    planner: Mapping[str, Any],
    *,
    selected_metric: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return execution-neutral facts without selecting an analysis plan."""

    raw_metrics = planner.get("metrics")
    if not isinstance(raw_metrics, list) or any(
        not isinstance(item, Mapping) for item in raw_metrics
    ):
        raise ContractFailure("CONTRACT_UNAVAILABLE", "分析能力目录格式无效。")

    metrics = [item for item in raw_metrics if isinstance(item, Mapping)]
    capability_codes = {"direct_fact"}
    if any(item.get("comparison_kinds") for item in metrics):
        capability_codes.add("returned_comparison")
    if any(
        item.get("allowed_dimensions") or item.get("allowed_dimension_set")
        for item in metrics
    ):
        capability_codes.add("dimension_breakdown")
    if any(item.get("change_decomposition_dimensions") for item in metrics):
        capability_codes.add("complete_change_decomposition")
    if any(item.get("target_gap_decomposition") for item in metrics):
        capability_codes.add("complete_target_gap_decomposition")

    affordances: dict[str, Any] = {
        "version": "datasage-capability-affordances/v1",
        "contract_role": "capability_facts_only",
        "domain": domain,
        "ownership": {
            "plan_owner": "Hermes",
            "execution_authority": "none",
        },
        "capabilities": sorted(capability_codes),
        "evidence_non_authorization": [
            "A comparison or decomposition does not by itself prove a business cause.",
            "Cross-metric interpretation requires compatible returned scope proof.",
            "A normative judgment requires a returned governed benchmark.",
        ],
    }
    if selected_metric is not None:
        metric_code = selected_metric.get("code")
        if not isinstance(metric_code, str) or not metric_code:
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "所选指标的分析能力格式无效。"
            )
        metric_facts: dict[str, Any] = {
            "code": metric_code,
            "comparison_kinds": _copy_guidance(
                selected_metric.get("comparison_kinds") or []
            ),
            "allowed_dimensions": _copy_guidance(
                selected_metric.get("allowed_dimensions") or []
            ),
            "change_decomposition_dimensions": _copy_guidance(
                selected_metric.get("change_decomposition_dimensions") or []
            ),
        }
        target_gap = selected_metric.get("target_gap_decomposition")
        if isinstance(target_gap, Mapping):
            metric_facts["target_gap_decomposition"] = _copy_guidance(target_gap)
        for key in (
            "delivery_scope_policy",
            "scope_flags",
            "answer_boundary_summary",
        ):
            if selected_metric.get(key) is not None:
                metric_facts[key] = _copy_guidance(selected_metric.get(key))
        affordances["selected_metric_capabilities"] = metric_facts

    try:
        assert_capability_boundary(affordances)
    except CapabilityContractError as exc:
        raise ContractFailure("CONTRACT_UNAVAILABLE", exc.message) from exc
    _assert_business_safe_tree(
        affordances,
        context=f"domain {domain} capability affordances",
    )
    return affordances


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
                "currency_basis",
                "required_attribution_mode",
                "allowed_attribution_modes",
                "allowed_dimensions",
                "dimensions_by_attribution_mode",
                "operation_summary",
                "operation_summary_by_attribution_mode",
                "target_gap_decomposition",
                "comparison_kinds",
                "allowed_time_buckets",
                "required_time_bucket",
                "ordering",
                "change_decomposition_orderings",
                "period_summary_fields",
                "exact_default_lookup_supported",
                "available_inventory_scopes",
                "default_inventory_scope",
                "max_group_dimensions",
                "grouping",
                "delivery_scope_policy",
                "scope_flags",
            )
            if raw.get(key) is not None
        }
        item["supports_dimensions"] = bool(
            raw.get("allowed_dimensions") or raw.get("allowed_dimension_set")
        )
        item["supports_change_decomposition"] = bool(
            raw.get("change_decomposition_dimensions")
        )
        allowed_dimensions = _metric_allowed_dimensions(raw, planner)
        if allowed_dimensions:
            item["allowed_dimensions"] = allowed_dimensions
        metrics.append(item)
    result: dict[str, Any] = {
        "domain": domain,
        "level": "summary",
        "source_versions": _copy_guidance(planner.get("source_versions")),
        "capability_affordances": _capability_affordances(domain, planner),
        "metric_count": len(metrics),
        "metrics": metrics,
    }
    related_metric_refs = planner.get("related_metric_refs")
    if isinstance(related_metric_refs, list):
        result["related_metric_refs"] = _copy_guidance(related_metric_refs)
    pending_capabilities = planner.get("pending_capabilities")
    if isinstance(pending_capabilities, list):
        result["pending_capabilities"] = _copy_guidance(pending_capabilities)
    return result


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
                "currency_basis",
                "required_attribution_mode",
                "allowed_attribution_modes",
                "allowed_dimensions",
                "dimensions_by_attribution_mode",
                "available_inventory_scopes",
                "default_inventory_scope",
                "exact_default_lookup_supported",
                "max_group_dimensions",
                "grouping",
                "comparison_kinds",
                "allowed_time_buckets",
                "required_time_bucket",
                "ordering",
                "change_decomposition_orderings",
                "period_summary_fields",
                "operation_summary",
                "operation_summary_by_attribution_mode",
                "target_gap_decomposition",
                "delivery_scope_policy",
                "scope_flags",
            )
            if raw.get(key) is not None
        }
        item["supports_dimensions"] = bool(
            raw.get("allowed_dimensions") or raw.get("allowed_dimension_set")
        )
        item["supports_change_decomposition"] = bool(
            raw.get("change_decomposition_dimensions")
        )
        allowed_dimensions = _metric_allowed_dimensions(raw, planner)
        if allowed_dimensions:
            item["allowed_dimensions"] = allowed_dimensions
        item["operation_summary"] = _metric_operation_summary(raw)
        if raw.get("answer_contract") is not None:
            item["limitations"] = _copy_guidance(raw.get("answer_contract"))
        item["requires_metric_detail"] = (
            raw.get("exact_default_lookup_supported") is not True
        )
        metrics.append(item)
    result: dict[str, Any] = {
        "domain": domain,
        "level": "expert_index",
        "source_versions": _copy_guidance(planner.get("source_versions")),
        "metric_count": len(metrics),
        "metrics": metrics,
    }
    related_metric_refs = planner.get("related_metric_refs")
    if isinstance(related_metric_refs, list):
        result["related_metric_refs"] = _copy_guidance(related_metric_refs)
    pending_capabilities = planner.get("pending_capabilities")
    if isinstance(pending_capabilities, list):
        result["pending_capabilities"] = _copy_guidance(pending_capabilities)
    return result


def _result_fields_projection(definition: Mapping[str, Any], physical: set[str]) -> dict[str, Any]:
    """Describe declared facts; the shared public-field allowlist is the authority."""
    raw = definition.get("result_fields")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not raw or "metric_value" not in raw:
        raise ContractFailure("CONTRACT_UNAVAILABLE", "Invalid result field semantics.")
    result = {}
    units = definition.get("result_fact_units") or {}
    for field, spec in raw.items():
        if field not in public_fields.PUBLIC_FACT_FIELDS or not isinstance(spec, Mapping):
            raise ContractFailure("CONTRACT_UNAVAILABLE", "Result field is not publicly permitted.")
        if set(spec) - {"meaning", "unit"} or not isinstance(spec.get("meaning"), str) or not spec["meaning"].strip():
            raise ContractFailure("CONTRACT_UNAVAILABLE", "Invalid result field description.")
        unit = definition.get("unit") if field == "metric_value" else units.get(field, spec.get("unit"))
        if not isinstance(unit, str) or not unit.strip():
            raise ContractFailure("CONTRACT_UNAVAILABLE", "Result field unit is unavailable.")
        item = {"role": "main" if field == "metric_value" else "additional", "unit": unit, "meaning": spec["meaning"]}
        _assert_business_safe_tree(item, context="result field semantics", physical_identifiers=physical)
        result[field] = item
    return result


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

    pending_capabilities = planner.get("pending_capabilities")
    if isinstance(pending_capabilities, list):
        pending = next(
            (
                dict(item)
                for item in pending_capabilities
                if isinstance(item, Mapping)
                and item.get("code") == metric_code
            ),
            None,
        )
        if pending is not None:
            # Preserve one structured reason in the exact-detail response and
            # keep it explicitly non-selectable.  The compact wire knows how
            # to retain this pending metric shape without inventing operations.
            pending["pending"] = True
            return {
                "domain": domain,
                "level": "metric",
                "source_versions": _copy_guidance(planner.get("source_versions")),
                "metric": pending,
                "dimensions": [],
                "pending_capability": _copy_guidance(pending),
            }
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
    _datasets, semantic_contract = execution_contracts(domain)
    try:
        effective = capability_contract.effective_dimension_definitions(semantic_contract, metric_code)
    except CapabilityContractError as exc:
        raise ContractFailure(exc.code, exc.message) from exc
    physical = _physical_identifiers(semantic_contract) - _business_tokens(semantic_contract)
    definitions = {
        code: _model_dimension_projection(code, effective[code], physical)
        for code in allowed_dimensions if code in effective
    }
    metric["allowed_dimensions"] = allowed_dimensions
    result_fields = _result_fields_projection(semantic_contract["metrics"][metric_code], physical)
    if result_fields:
        metric["result_fields"] = result_fields
    if metric.get("change_decomposition_dimensions"):
        metric["change_decomposition_policy"] = (
            "structural_contribution_after_reconciled_full_rows_or_same_statement_full_partition_aggregate_proof_with_bounded_claims"
        )
    result: dict[str, Any] = {
        "domain": domain,
        "level": "metric",
        "source_versions": _copy_guidance(planner.get("source_versions")),
        "capability_affordances": _capability_affordances(
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
    related_metric_refs = planner.get("related_metric_refs")
    if isinstance(related_metric_refs, list):
        result["related_metric_refs"] = _copy_guidance(related_metric_refs)
    return result


def _catalog_performance_scorecard() -> dict[str, Any]:
    """Resolve candidate lenses through ordinary independent metric contracts."""

    manifest = performance_scorecard_manifest()
    raw_lenses = manifest.get("candidate_lenses")
    if not isinstance(raw_lenses, (list, tuple)):
        raise ContractFailure(
            "CONTRACT_UNAVAILABLE", "performance scorecard lenses are invalid"
        )
    candidate_lenses: list[dict[str, Any]] = []
    for raw_lens in raw_lenses:
        if not isinstance(raw_lens, Mapping):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "performance scorecard lens is invalid"
            )
        raw_candidates = raw_lens.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)):
            raise ContractFailure(
                "CONTRACT_UNAVAILABLE", "performance scorecard candidates are invalid"
            )
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, Mapping):
                raise ContractFailure(
                    "CONTRACT_UNAVAILABLE", "performance scorecard candidate is invalid"
                )
            domain = raw_candidate.get("domain")
            metric = raw_candidate.get("metric")
            if domain not in DOMAIN_SOURCES or not isinstance(metric, str):
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
            candidates.append(
                {
                    "domain": str(domain),
                    "metric": metric,
                    "detail": detail,
                }
            )
        lens = {
            key: _copy_guidance(value)
            for key, value in raw_lens.items()
            if key != "candidates"
        }
        lens["candidates"] = candidates
        candidate_lenses.append(lens)
    return {
        "level": "performance_scorecard",
        "version": manifest.get("version"),
        "kind": manifest.get("kind"),
        "candidate_lenses": candidate_lenses,
        "evidence_boundaries": _copy_guidance(
            manifest.get("evidence_boundaries") or []
        ),
    }


def datasage_catalog(args: dict[str, Any], **_kwargs: Any) -> str:
    """Return governed planning catalogs without owning the conversation."""
    try:
        if not isinstance(args, dict) or set(args) != {"requests"}:
            raise ContractFailure("INVALID_INPUT", "目录参数只接受 requests。")
        requests = args.get("requests")
        if not isinstance(requests, list) or not 1 <= len(requests) <= len(DOMAIN_SOURCES):
            raise ContractFailure("INVALID_INPUT", f"requests 必须包含一到{len(DOMAIN_SOURCES)}个域合同请求。")
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
            if not isinstance(domain, str) or domain not in DOMAIN_SOURCES:
                raise ContractFailure("INVALID_INPUT", "业务域不受支持。")
            if metric is not None and (
                not isinstance(metric, str)
                or not metric.strip()
                or len(metric) > 100
            ):
                raise ContractFailure("INVALID_INPUT", "metric 格式无效。")
            if view is not None and (
                not isinstance(view, str)
                or view not in {"expert_index", "full", "audit"}
            ):
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
        if any(view == "performance_scorecard" for _, _, view in normalized) and len(
            normalized
        ) > 1:
            raise ContractFailure(
                "INVALID_INPUT", "performance_scorecard 必须单独请求。"
            )
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
