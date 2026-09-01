"""Strict, single-source lifecycle and review governance for DataSage metrics."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import yaml

from . import capability_contract, contract_store


GOVERNANCE_PATH = "plugins/datasage-query/contracts/metric-governance.yaml"
SCHEMA = "datasage-metric-governance/v1"
LIFECYCLES = {"active", "deprecated", "retired"}
_OWNER_ROLE = re.compile(r"^datasage\.[a-z][a-z0-9_.-]{0,119}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_RECORD_KEYS = {
    "owner_role",
    "lifecycle",
    "reviewed_at",
    "review_interval_days",
    "validation_gate",
}
_POLICY = {
    "timezone": "Asia/Shanghai",
    "missing_review": "release_blocker",
    "overdue_review": "release_blocker",
    "missing_owner": "release_blocker",
    "missing_interval": "release_blocker",
    "runtime_review_failure": "allow",
    "deprecated_runtime": "allow_with_warning",
    "retired_runtime": "deny",
}


class MetricGovernanceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: yaml.Loader, node: yaml.Node, deep: bool = False):
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise MetricGovernanceError(
                "METRIC_GOVERNANCE_CONTRACT_INVALID",
                "governance mapping keys must be scalar and hashable",
            ) from exc
        if duplicate:
            raise MetricGovernanceError(
                "METRIC_GOVERNANCE_CONTRACT_INVALID",
                f"duplicate governance key: {key}",
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping,
)


def _strict_governance_payload() -> tuple[dict[str, Any], str, str]:
    path, content, digest = contract_store.read_contract_bytes(GOVERNANCE_PATH)
    try:
        text = content.decode("utf-8")
        payload = yaml.load(text, Loader=_UniqueSafeLoader)
    except MetricGovernanceError:
        raise
    except (UnicodeError, yaml.YAMLError) as exc:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            "metric governance YAML is invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            "metric governance root must be a mapping",
        )
    return payload, path, digest


def _observed_at(value: date | datetime | None) -> datetime:
    zone = ZoneInfo(_POLICY["timezone"])
    if value is None:
        return datetime.now(zone)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MetricGovernanceError(
                "METRIC_GOVERNANCE_OBSERVED_AT_INVALID",
                "observed_at must be timezone-aware",
            )
        return value.astimezone(zone)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=zone)
    raise MetricGovernanceError(
        "METRIC_GOVERNANCE_OBSERVED_AT_INVALID",
        "observed_at must be a date or timezone-aware datetime",
    )


def _reviewed_at(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_REVIEW_DATE_INVALID",
            f"{label}.reviewed_at must be RFC3339 or null",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_REVIEW_DATE_INVALID",
            f"{label}.reviewed_at must be RFC3339 or null",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_REVIEW_DATE_INVALID",
            f"{label}.reviewed_at must include a timezone",
        )
    return parsed


def _availability(definition: Mapping[str, Any], label: str) -> str:
    raw = definition.get("availability")
    if raw is None:
        return "available"
    if not isinstance(raw, Mapping):
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            f"{label}.availability must be a mapping",
        )
    status = raw.get("status")
    if status not in {"pending_validation", "blocked"}:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            f"{label}.availability status is invalid",
        )
    return str(status)


def _validation_gate(value: Any, *, required: bool, label: str) -> None:
    if value is None:
        if required:
            raise MetricGovernanceError(
                "METRIC_GOVERNANCE_CONTRACT_INVALID",
                f"{label}.validation_gate is required",
            )
        return
    if not required or not isinstance(value, Mapping) or set(value) != {
        "state",
        "required_evidence",
        "activation_authority",
    }:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            f"{label}.validation_gate is invalid",
        )
    evidence = value.get("required_evidence")
    if (
        value.get("state") != "blocked"
        or not isinstance(evidence, list)
        or not evidence
        or len(evidence) != len(set(evidence))
        or any(not isinstance(item, str) or not item for item in evidence)
        or not isinstance(value.get("activation_authority"), str)
        or not value.get("activation_authority")
    ):
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            f"{label}.validation_gate is invalid",
        )


def _semantic_metrics() -> dict[str, dict[str, Mapping[str, Any]]]:
    result: dict[str, dict[str, Mapping[str, Any]]] = {}
    for domain, source in capability_contract.DOMAIN_SOURCES.items():
        semantics = contract_store.read_yaml(str(source["semantics"]))
        metrics = semantics.get("metrics")
        if not isinstance(metrics, Mapping) or any(
            not isinstance(code, str) or not isinstance(definition, Mapping)
            for code, definition in metrics.items()
        ):
            raise MetricGovernanceError(
                "METRIC_GOVERNANCE_CONTRACT_INVALID",
                f"{domain} semantic metrics are invalid",
            )
        result[domain] = {
            str(code): definition for code, definition in metrics.items()
        }
    return result


def _release_blockers(
    *,
    availability: str,
    lifecycle: str,
    owner_role: str | None,
    reviewed: datetime | None,
    review_state: str,
    interval: int | None,
) -> list[str]:
    blockers: list[str] = []
    if owner_role is None:
        blockers.append("METRIC_GOVERNANCE_OWNER_MISSING")
    if reviewed is None:
        blockers.append("METRIC_GOVERNANCE_REVIEW_MISSING")
    if interval is None:
        blockers.append("METRIC_GOVERNANCE_INTERVAL_INVALID")
    if review_state == "overdue":
        blockers.append("METRIC_GOVERNANCE_REVIEW_OVERDUE")
    if review_state == "invalid":
        blockers.append("METRIC_GOVERNANCE_REVIEW_INVALID")
    if availability != "available":
        blockers.append("METRIC_GOVERNANCE_VALIDATION_PENDING")
    if lifecycle == "retired":
        blockers.append("METRIC_GOVERNANCE_METRIC_RETIRED")
    return blockers


def load_contract(
    observed_on: date | datetime | None = None,
) -> dict[str, Any]:
    """Load, validate, derive, and return a detached governance projection."""

    observed = _observed_at(observed_on)
    payload, path, digest = _strict_governance_payload()
    if set(payload) != {"schema", "version", "review_policy", "metrics"}:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            "metric governance top-level keys are invalid",
        )
    if payload.get("schema") != SCHEMA or payload.get("version") != 1:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            "metric governance schema/version is unsupported",
        )
    if payload.get("review_policy") != _POLICY:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_CONTRACT_INVALID",
            "metric governance review policy is invalid",
        )
    raw_domains = payload.get("metrics")
    semantics = _semantic_metrics()
    if not isinstance(raw_domains, Mapping) or set(raw_domains) != set(semantics):
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_COVERAGE_MISMATCH",
            "metric governance domains do not match semantics",
        )

    projected: dict[str, dict[str, Any]] = {}
    blocker_counts: dict[str, int] = {}
    availability_counts: dict[str, int] = {}
    lifecycle_counts: dict[str, int] = {}
    review_counts: dict[str, int] = {}
    execution_counts: dict[str, int] = {}
    release_counts = {"pass": 0, "block": 0}
    total = 0
    for domain, semantic_metrics in semantics.items():
        raw_metrics = raw_domains.get(domain)
        if not isinstance(raw_metrics, Mapping) or set(raw_metrics) != set(
            semantic_metrics
        ):
            raise MetricGovernanceError(
                "METRIC_GOVERNANCE_COVERAGE_MISMATCH",
                f"metric governance coverage differs for {domain}",
            )
        domain_projection: dict[str, Any] = {}
        for code, definition in semantic_metrics.items():
            label = f"{domain}.{code}"
            record = raw_metrics.get(code)
            if not isinstance(record, Mapping) or set(record) != _RECORD_KEYS:
                raise MetricGovernanceError(
                    "METRIC_GOVERNANCE_CONTRACT_INVALID",
                    f"{label} governance record has invalid keys",
                )
            if "derived_status" in record:
                raise MetricGovernanceError(
                    "METRIC_GOVERNANCE_CONTRACT_INVALID",
                    f"{label} must not persist derived status",
                )
            owner = record.get("owner_role")
            if owner is not None and (
                not isinstance(owner, str)
                or _OWNER_ROLE.fullmatch(owner) is None
            ):
                raise MetricGovernanceError(
                    "METRIC_GOVERNANCE_OWNER_INVALID",
                    f"{label}.owner_role is invalid",
                )
            lifecycle = record.get("lifecycle")
            if lifecycle not in LIFECYCLES:
                raise MetricGovernanceError(
                    "METRIC_GOVERNANCE_LIFECYCLE_INVALID",
                    f"{label}.lifecycle is invalid",
                )
            interval = record.get("review_interval_days")
            if interval is not None and (
                type(interval) is not int or interval <= 0 or interval > 3660
            ):
                raise MetricGovernanceError(
                    "METRIC_GOVERNANCE_INTERVAL_INVALID",
                    f"{label}.review_interval_days is invalid",
                )
            reviewed = _reviewed_at(record.get("reviewed_at"), label)
            availability = _availability(definition, label)
            _validation_gate(
                record.get("validation_gate"),
                required=availability != "available",
                label=label,
            )
            if reviewed is None:
                review_state = "missing"
            elif reviewed.astimezone(observed.tzinfo) > observed:
                review_state = "invalid"
            elif interval is None:
                review_state = "invalid"
            elif observed > reviewed.astimezone(observed.tzinfo) + timedelta(
                days=interval
            ):
                review_state = "overdue"
            else:
                review_state = "current"

            warnings: list[str] = []
            if availability != "available" or lifecycle == "retired":
                execution = "denied"
                if lifecycle == "retired":
                    warnings.append("METRIC_RETIRED")
            elif lifecycle == "deprecated":
                execution = "allowed_with_warning"
                warnings.append("METRIC_DEPRECATED")
            else:
                execution = "allowed"
            blockers = _release_blockers(
                availability=availability,
                lifecycle=str(lifecycle),
                owner_role=owner,
                reviewed=reviewed,
                review_state=review_state,
                interval=interval,
            )
            release = "block" if blockers else "pass"
            domain_projection[code] = {
                "owner_role": owner,
                "lifecycle": lifecycle,
                "reviewed_at": record.get("reviewed_at"),
                "review_interval_days": interval,
                "validation_gate": deepcopy(record.get("validation_gate")),
                "derived_status": {
                    "availability": availability,
                    "review": review_state,
                    "execution": execution,
                    "release": release,
                    "warnings": warnings,
                    "release_blockers": blockers,
                },
            }
            total += 1
            for bucket, value in (
                (availability_counts, availability),
                (lifecycle_counts, str(lifecycle)),
                (review_counts, review_state),
                (execution_counts, execution),
            ):
                bucket[value] = bucket.get(value, 0) + 1
            release_counts[release] += 1
            for blocker in blockers:
                blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
        projected[domain] = domain_projection

    result = {
        "schema": SCHEMA,
        "version": 1,
        "contract_path": path,
        "contract_sha256": digest,
        "observed_at": observed.isoformat(),
        "review_policy": deepcopy(_POLICY),
        "metrics": projected,
        "summary": {
            "metric_count": total,
            "availability": dict(sorted(availability_counts.items())),
            "lifecycle": dict(sorted(lifecycle_counts.items())),
            "review": dict(sorted(review_counts.items())),
            "execution": dict(sorted(execution_counts.items())),
            "release": release_counts,
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
    }
    return deepcopy(result)


def metric_status(
    domain: str,
    metric: str,
    observed_on: date | datetime | None = None,
) -> dict[str, Any]:
    contract = load_contract(observed_on)
    try:
        return deepcopy(contract["metrics"][domain][metric])
    except KeyError as exc:
        raise MetricGovernanceError(
            "METRIC_GOVERNANCE_METRIC_UNKNOWN",
            "metric governance entry is unavailable",
        ) from exc


def summary(observed_on: date | datetime | None = None) -> dict[str, Any]:
    return deepcopy(load_contract(observed_on)["summary"])
