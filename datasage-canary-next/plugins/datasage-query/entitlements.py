"""Caller-bound, deny-by-default authorization for DataSage tools.

The model never supplies caller identity.  Gateway identity is read from
Hermes' request-scoped session context, then matched against an exact operator
policy in ``plugins.entries.datasage-query.settings.data_entitlements``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from . import settings


TOOL_NAMES = frozenset(
    {
        "datasage_catalog",
        "datasage_entity_resolve",
        "datasage_reference",
        "datasage_query",
    }
)
REFERENCE_SOURCE_DOMAINS = {
    "planner_delivery": "delivery",
    "planner_receipt": "receipt",
    "planner_receivable": "receivable",
    "planner_target": "target",
    "planner_customer_risk": "customer_risk",
    "planner_inventory": "inventory",
}
SHARED_REFERENCE_SOURCES = frozenset(
    {"expert_playbooks", "answer_boundary", "entity_guidance", "query_rules"}
)
ALL_REFERENCE_SOURCES = frozenset(REFERENCE_SOURCE_DOMAINS) | SHARED_REFERENCE_SOURCES
DENIED_CODE = "DATA_ENTITLEMENT_DENIED"
DENIED_MESSAGE = "当前请求未获授权，业务查询未执行。"
_SCALAR_TYPES = (str, int, float, bool)
_REPLAY_PLATFORM = "replay"
_REPLAY_SOURCE = "datasage-trusted-replay"
_MISSING_SESSION_VALUE = object()


def _session_value(name: str) -> str:
    """Read a strictly bound Hermes request identity; never fall back to env.

    Prefer a public strict-bound helper when Hermes exposes one.  Hermes 0.20
    only has ``get_session_env()``, whose environment fallback is unsafe for an
    authorization decision, so the compatibility branch is deliberately
    isolated here and fails closed if its private ContextVar surface changes.
    """

    try:
        from gateway import session_context

        if not session_context.session_context_engaged():
            return ""

        strict_getter = getattr(session_context, "get_bound_session_env", None)
        if callable(strict_getter):
            value = strict_getter(name, _MISSING_SESSION_VALUE)
            if value is _MISSING_SESSION_VALUE:
                return ""
            return str(value).strip() if value is not None else ""

        variables = getattr(session_context, "_VAR_MAP", None)
        unset = getattr(session_context, "_UNSET", _MISSING_SESSION_VALUE)
        if not isinstance(variables, Mapping):
            return ""
        variable = variables.get(name)
        if variable is None:
            return ""
        value = variable.get()
        if value is unset:
            return ""
    except (AttributeError, ImportError, RuntimeError, TypeError):
        return ""
    return str(value).strip() if value is not None else ""


def _deny() -> str:
    return json.dumps(
        {
            "status": "failed",
            "error": {
                "code": DENIED_CODE,
                "message": DENIED_MESSAGE,
                "retryable": False,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _string_set(value: Any, *, allow_wildcard: bool = True) -> set[str] | None:
    if not isinstance(value, list) or not value:
        return None
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        normalized = item.strip()
        if normalized == "*" and not allow_wildcard:
            return None
        result.add(normalized)
    return result


def _contains(scope: set[str] | None, value: Any) -> bool:
    return (
        scope is not None
        and isinstance(value, str)
        and ("*" in scope or value in scope)
    )


def _principal_rule(policy: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if policy.get("enforcement") != "enforce" or policy.get("default_effect") != "deny":
        return None
    principals = policy.get("principals")
    if not isinstance(principals, list):
        return None

    platform = _session_value("HERMES_SESSION_PLATFORM").lower()
    user_id = _session_value("HERMES_SESSION_USER_ID")
    source = _session_value("HERMES_SESSION_SOURCE")
    if not platform or not user_id:
        return None
    if platform == _REPLAY_PLATFORM and source != _REPLAY_SOURCE:
        return None

    matches: list[Mapping[str, Any]] = []
    for entry in principals:
        if not isinstance(entry, Mapping):
            return None
        configured_platform = entry.get("platform")
        configured_source = entry.get("source")
        configured_user = entry.get("user_id")
        if (
            not isinstance(configured_platform, str)
            or not configured_platform.strip()
            or configured_platform.strip() == "*"
            or not isinstance(configured_user, str)
            or not configured_user.strip()
            or configured_source is not None
            and (not isinstance(configured_source, str) or not configured_source.strip())
        ):
            return None
        configured_user = configured_user.strip()
        user_matches = configured_user == user_id or (
            platform != _REPLAY_PLATFORM and configured_user == "*"
        )
        source_matches = configured_source is None or configured_source.strip() == source
        if (
            configured_platform.strip().lower() == platform
            and source_matches
            and user_matches
        ):
            matches.append(entry)
    return matches[0] if len(matches) == 1 else None


def _metric_allowed(rule: Mapping[str, Any], domain: Any, metric: Any) -> bool:
    metrics = rule.get("metrics")
    if not isinstance(metrics, Mapping) or not isinstance(domain, str):
        return False
    scope = _string_set(metrics.get(domain))
    return _contains(scope, metric)


def _domain_allowed(rule: Mapping[str, Any], domain: Any) -> bool:
    return _contains(_string_set(rule.get("domains")), domain)


def _catalog_allowed(rule: Mapping[str, Any], args: Any) -> bool:
    requests = args.get("requests") if isinstance(args, Mapping) else None
    if not isinstance(requests, list) or not requests:
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            return False
        domain = request.get("domain")
        if not _domain_allowed(rule, domain):
            return False
        metric = request.get("metric")
        if metric is None:
            if rule.get("allow_catalog_discovery") is not True:
                return False
        elif not _metric_allowed(rule, domain, metric):
            return False
    return True


def _entity_allowed(rule: Mapping[str, Any], args: Any) -> bool:
    if not isinstance(args, Mapping):
        return False
    domain = args.get("domain")
    metric = args.get("metric")
    if domain is None:
        if rule.get("allow_unscoped_entity_resolution") is not True:
            return False
    elif not _domain_allowed(rule, domain):
        return False
    if metric is not None and not _metric_allowed(rule, domain, metric):
        return False

    if rule.get("allow_entity_resolution_all_rows") is not True:
        return False
    allowed_types = _string_set(rule.get("entity_types"))
    requested_types = args.get("entity_types")
    if requested_types is None:
        return rule.get("allow_type_neutral_entity_resolution") is True
    return (
        isinstance(requested_types, list)
        and bool(requested_types)
        and all(_contains(allowed_types, value) for value in requested_types)
    )


def _reference_allowed(rule: Mapping[str, Any], args: Any) -> bool:
    if not isinstance(args, Mapping):
        return False
    sources = _string_set(rule.get("reference_sources"), allow_wildcard=False)
    if sources is None or not sources.issubset(ALL_REFERENCE_SOURCES):
        return False
    mode = args.get("mode")
    if mode == "index":
        # The current index response is global rather than caller-filtered.
        return rule.get("allow_reference_index") is True and sources == set(
            ALL_REFERENCE_SOURCES
        )
    requests = args.get("requests")
    if mode != "read" or not isinstance(requests, list) or not requests:
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            return False
        source_id = request.get("source_id")
        if not _contains(sources, source_id):
            return False
        domain = REFERENCE_SOURCE_DOMAINS.get(str(source_id))
        if domain is not None and not _domain_allowed(rule, domain):
            return False
        if source_id in SHARED_REFERENCE_SOURCES and rule.get(
            "allow_shared_references"
        ) is not True:
            return False
    return True


def _policy_values(value: Any) -> set[tuple[type, Any]] | None:
    if not isinstance(value, list) or not value:
        return None
    result: set[tuple[type, Any]] = set()
    for item in value:
        if not isinstance(item, _SCALAR_TYPES) or isinstance(item, float) and item != item:
            return None
        result.add((type(item), item))
    return result


def _request_filter_values(value: Any) -> list[Any] | None:
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(item, _SCALAR_TYPES) for item in values):
        return None
    return values


def _rows_allowed(rule: Mapping[str, Any], request: Mapping[str, Any]) -> bool:
    if rule.get("allow_all_rows") is True:
        return True
    row_filters = rule.get("row_filters")
    metric_filters = request.get("metric_filters")
    if not isinstance(row_filters, Mapping) or not row_filters:
        return False
    if not isinstance(metric_filters, Mapping):
        return False
    for dimension, raw_allowed in row_filters.items():
        if not isinstance(dimension, str) or not dimension or dimension not in metric_filters:
            return False
        allowed = _policy_values(raw_allowed)
        supplied = _request_filter_values(metric_filters.get(dimension))
        if allowed is None or supplied is None:
            return False
        if any((type(value), value) not in allowed for value in supplied):
            return False
    return True


def _query_allowed(rule: Mapping[str, Any], args: Any) -> bool:
    requests = args.get("requests") if isinstance(args, Mapping) else None
    if not isinstance(requests, list) or not requests:
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            return False
        domain = request.get("domain")
        if (
            not _domain_allowed(rule, domain)
            or not _metric_allowed(rule, domain, request.get("metric"))
            or not _rows_allowed(rule, request)
        ):
            return False
    return True


def authorized(tool_name: str, args: Any) -> bool:
    """Return whether trusted caller identity covers the requested scope."""

    if tool_name not in TOOL_NAMES:
        return False
    policy = settings.profile_settings().get("data_entitlements")
    if not isinstance(policy, Mapping):
        return False
    rule = _principal_rule(policy)
    if rule is None or not _contains(_string_set(rule.get("tools"), allow_wildcard=False), tool_name):
        return False
    if tool_name == "datasage_catalog":
        return _catalog_allowed(rule, args)
    if tool_name == "datasage_entity_resolve":
        return _entity_allowed(rule, args)
    if tool_name == "datasage_query":
        return _query_allowed(rule, args)
    return _reference_allowed(rule, args)


def guard(tool_name: str, handler: Callable[..., str]) -> Callable[..., str]:
    """Wrap a model-facing handler without adding forgeable schema fields."""

    def guarded(args: dict[str, Any], **kwargs: Any) -> str:
        if not authorized(tool_name, args):
            return _deny()
        return handler(args, **kwargs)

    guarded.__name__ = f"entitlement_guarded_{getattr(handler, '__name__', tool_name)}"
    guarded.__doc__ = getattr(handler, "__doc__", None)
    return guarded
