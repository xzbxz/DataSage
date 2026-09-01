"""Caller-bound, deny-by-default authorization for DataSage tools.

The model never supplies caller identity.  Gateway identity is read from
Hermes' request-scoped session context, then matched against an exact operator
policy in ``plugins.entries.datasage-query.settings.data_entitlements``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import settings
from .scorecard import SCORECARD_METRICS


logger = logging.getLogger(__name__)


TOOL_NAMES = frozenset(
    {
        "datasage_catalog",
        "datasage_entity_resolve",
        "datasage_query",
    }
)
DENIED_CODE = "DATA_ENTITLEMENT_DENIED"
DENIED_MESSAGE = "当前请求未获授权，业务查询未执行。"
_SCALAR_TYPES = (str, int, float, bool)
_REPLAY_PLATFORM = "replay"
_REPLAY_SOURCE = "datasage-trusted-replay"
_MISSING_SESSION_VALUE = object()
_AUDIT_DOMAIN = b"datasage-entitlement-audit/v1\x00"


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


def denied_response() -> str:
    """Return the stable public denial without exposing policy internals."""

    return _deny()


def _audit_hash(value: str) -> str:
    return hashlib.sha256(_AUDIT_DOMAIN + value.encode("utf-8", "surrogatepass")).hexdigest()


def _audit_scope(args: Any) -> dict[str, list[str]]:
    """Project only one-way scope fingerprints for authorization telemetry."""

    domains: list[str] = []
    metrics: list[str] = []
    request_ids: list[str] = []
    if isinstance(args, Mapping):
        direct_domain = args.get("domain")
        direct_metric = args.get("metric")
        if isinstance(direct_domain, str):
            domains.append(_audit_hash(direct_domain))
        if isinstance(direct_metric, str):
            metrics.append(_audit_hash(direct_metric))
        requests = args.get("requests")
        if isinstance(requests, Sequence) and not isinstance(requests, (str, bytes)):
            for request in requests[:64]:
                if not isinstance(request, Mapping):
                    continue
                domain = request.get("domain")
                metric = request.get("metric")
                request_id = request.get("request_id")
                if isinstance(domain, str):
                    domains.append(_audit_hash(domain))
                if isinstance(metric, str):
                    metrics.append(_audit_hash(metric))
                if isinstance(request_id, str):
                    request_ids.append(_audit_hash(request_id))
    return {
        "domain_hashes": sorted(set(domains)),
        "metric_hashes": sorted(set(metrics)),
        "request_id_hashes": sorted(set(request_ids)),
    }


def _audit_decision(
    tool_name: Any,
    args: Any,
    *,
    allowed: bool,
    reason: str,
    stage: str,
) -> None:
    """Emit a structured, non-reversible authorization decision record."""

    try:
        principal = "\x00".join(
            _session_value(name)
            for name in (
                "HERMES_SESSION_PLATFORM",
                "HERMES_SESSION_SOURCE",
                "HERMES_SESSION_USER_ID",
                "HERMES_SESSION_CHAT_ID",
                "HERMES_SESSION_CHAT_TYPE",
            )
        )
        principal_hash = _audit_hash(principal)
        event = {
            "event": "datasage_entitlement_decision",
            "stage": stage,
            "allowed": bool(allowed),
            "decision": "allow" if allowed else "deny",
            "tool": (
                tool_name
                if isinstance(tool_name, str) and tool_name in TOOL_NAMES
                else "<invalid>"
            ),
            "reason": reason,
            "principal_sha256": principal_hash,
            "principal_hash": principal_hash,
            **_audit_scope(args),
        }
        logger.info(json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    except Exception:
        # Authorization must remain fail-closed independently of a logging
        # backend failure, and the exception must not expose request values.
        try:
            logger.debug("datasage entitlement audit emission failed", exc_info=True)
        except Exception:
            pass


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
        if set(request) == {"view"} and request.get("view") == "performance_scorecard":
            if rule.get("allow_catalog_discovery") is not True:
                return False
            for spec in SCORECARD_METRICS:
                domain = spec.get("domain")
                metric = spec.get("metric")
                if not _domain_allowed(rule, domain) or not _metric_allowed(
                    rule, domain, metric
                ):
                    return False
            continue
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


def _query_allowed(
    rule: Mapping[str, Any],
    args: Any,
    *,
    validated_requests: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    requests = (
        validated_requests
        if validated_requests is not None
        else args.get("requests")
        if isinstance(args, Mapping)
        else None
    )
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)) or not requests:
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


def _coarse_catalog_allowed(rule: Mapping[str, Any], args: Any) -> bool:
    requests = args.get("requests") if isinstance(args, Mapping) else None
    if (
        not isinstance(requests, Sequence)
        or isinstance(requests, (str, bytes))
        or not requests
    ):
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            return False
        if request.get("view") == "performance_scorecard":
            if rule.get("allow_catalog_discovery") is not True:
                return False
            continue
        domain = request.get("domain")
        if not isinstance(domain, str) or not _domain_allowed(rule, domain):
            return False
        metric = request.get("metric")
        if metric is None:
            if rule.get("allow_catalog_discovery") is not True:
                return False
        elif not isinstance(metric, str) or not _metric_allowed(rule, domain, metric):
            return False
    return True


def _coarse_entity_allowed(rule: Mapping[str, Any], args: Any) -> bool:
    if not isinstance(args, Mapping):
        return False
    domain = args.get("domain")
    metric = args.get("metric")
    if domain is None:
        if rule.get("allow_unscoped_entity_resolution") is not True:
            return False
    elif not isinstance(domain, str) or not _domain_allowed(rule, domain):
        return False
    if metric is not None:
        if (
            not isinstance(metric, str)
            or domain is None
            or not _metric_allowed(rule, domain, metric)
        ):
            return False
    return True


def _coarse_query_allowed(rule: Mapping[str, Any], args: Any) -> bool:
    requests = args.get("requests") if isinstance(args, Mapping) else None
    if (
        not isinstance(requests, Sequence)
        or isinstance(requests, (str, bytes))
        or not requests
    ):
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            return False
        domain = request.get("domain")
        metric = request.get("metric")
        if (
            not isinstance(domain, str)
            or not isinstance(metric, str)
            or not _domain_allowed(rule, domain)
            or not _metric_allowed(rule, domain, metric)
        ):
            return False
    return True


def coarse_authorized(tool_name: str, args: Any) -> bool:
    """Apply identity/tool/domain/metric gating before contract evaluation.

    This intentionally does not load business contracts or validate row
    filters.  It is a cheap envelope gate for the composition layer; callers
    must still invoke :func:`authorized` with the fully validated request.
    Unknown or malformed envelopes fail closed.
    """

    allowed = False
    reason = "invalid_tool"
    if not isinstance(tool_name, str) or tool_name not in TOOL_NAMES:
        _audit_decision(tool_name, args, allowed=False, reason=reason, stage="coarse")
        return False
    policy = settings.get("data_entitlements")
    if not isinstance(policy, Mapping):
        reason = "policy_unavailable"
    else:
        rule = _principal_rule(policy)
        if rule is None:
            reason = "principal_unmatched"
        elif not _contains(
            _string_set(rule.get("tools"), allow_wildcard=False), tool_name
        ):
            reason = "tool_not_entitled"
        elif tool_name == "datasage_catalog":
            allowed = _coarse_catalog_allowed(rule, args)
            reason = "coarse_scope_allowed" if allowed else "catalog_scope_denied"
        elif tool_name == "datasage_entity_resolve":
            allowed = _coarse_entity_allowed(rule, args)
            reason = "coarse_scope_allowed" if allowed else "entity_scope_denied"
        elif tool_name == "datasage_query":
            allowed = _coarse_query_allowed(rule, args)
            reason = "coarse_scope_allowed" if allowed else "query_scope_denied"
    _audit_decision(tool_name, args, allowed=allowed, reason=reason, stage="coarse")
    return allowed


def authorized(
    tool_name: str,
    args: Any,
    *,
    validated_requests: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Return whether trusted caller identity covers the requested scope."""

    allowed = False
    reason = "invalid_tool"
    if not isinstance(tool_name, str) or tool_name not in TOOL_NAMES:
        reason = "invalid_tool"
    else:
        policy = settings.get("data_entitlements")
        if not isinstance(policy, Mapping):
            reason = "policy_unavailable"
        else:
            rule = _principal_rule(policy)
            if rule is None:
                reason = "principal_unmatched"
            elif not _contains(
                _string_set(rule.get("tools"), allow_wildcard=False), tool_name
            ):
                reason = "tool_not_entitled"
            elif tool_name == "datasage_catalog":
                allowed = _catalog_allowed(rule, args)
                reason = "full_scope_allowed" if allowed else "catalog_scope_denied"
            elif tool_name == "datasage_entity_resolve":
                allowed = _entity_allowed(rule, args)
                reason = "full_scope_allowed" if allowed else "entity_scope_denied"
            elif tool_name == "datasage_query":
                allowed = _query_allowed(
                    rule,
                    args,
                    validated_requests=validated_requests,
                )
                reason = "full_scope_allowed" if allowed else "query_scope_denied"
    _audit_decision(tool_name, args, allowed=allowed, reason=reason, stage="full")
    return allowed


def guard(tool_name: str, handler: Callable[..., str]) -> Callable[..., str]:
    """Wrap a model-facing handler without adding forgeable schema fields."""

    def guarded(args: dict[str, Any], **kwargs: Any) -> str:
        if not authorized(tool_name, args):
            return _deny()
        return handler(args, **kwargs)

    guarded.__name__ = f"entitlement_guarded_{getattr(handler, '__name__', tool_name)}"
    guarded.__doc__ = getattr(handler, "__doc__", None)
    return guarded
