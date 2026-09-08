"""Caller-bound admission for the DataSage tools.

DataSage deliberately has one business-data audience: authenticated WeCom
members.  The database account and the query contracts provide the read-only
and metric-safety boundaries; this module does not implement user-, row-,
domain-, metric-, or entity-level policy.

The only non-WeCom admission path is the explicit trusted replay harness.  It
must be bound by the host/test runner as the pair ``platform=replay`` and
``source=datasage-trusted-replay``.  Model arguments and process environment
variables never provide caller identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any


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

_WECOM_PLATFORM = "wecom"
_REPLAY_PLATFORM = "replay"
_REPLAY_SOURCE = "datasage-trusted-replay"
_MISSING_SESSION_VALUE = object()
_AUDIT_DOMAIN = b"datasage-entitlement-principal/v2\x00"


def _session_value(name: str) -> str:
    """Read a strictly bound Hermes request identity; never use ``os.environ``.

    The supported Hermes host exposes the bound values through its private ContextVar map;
    newer hosts may expose ``get_bound_session_env``.  Both branches are
    intentionally fail-closed when the session layer is absent or changes.
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


def _principal_ref() -> str:
    """Return a one-way reference for the host-bound session principal."""

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
    return hashlib.sha256(
        _AUDIT_DOMAIN + principal.encode("utf-8", "surrogatepass")
    ).hexdigest()


def _audit(tool_name: Any, *, allowed: bool, reason: str) -> None:
    """Record only the decision, tool, reason, and an irreversible principal ref."""

    del allowed
    try:
        event = {
            "event": "datasage_entitlement_decision",
            "tool": (
                tool_name
                if isinstance(tool_name, str) and tool_name in TOOL_NAMES
                else "<invalid>"
            ),
            "reason": reason,
            "principal_ref": _principal_ref(),
        }
        logger.info(
            json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    except Exception:
        # Authorization must remain independent of logging availability.
        try:
            logger.debug("datasage entitlement audit emission failed", exc_info=True)
        except Exception:
            pass


def _identity_allowed() -> tuple[bool, str]:
    """Return whether the host-bound session is an admitted DataSage caller.

    For WeCom, chat type/ID and group membership are intentionally not
    authorization dimensions: every authenticated member with a non-empty
    bound user ID shares the same DataSage admission in DM and group chats.
    """

    platform = _session_value("HERMES_SESSION_PLATFORM").lower()
    source = _session_value("HERMES_SESSION_SOURCE")
    user_id = _session_value("HERMES_SESSION_USER_ID")

    if not user_id:
        return False, "bound_user_identity_missing"

    if platform == _REPLAY_PLATFORM:
        if source != _REPLAY_SOURCE:
            return False, "trusted_replay_source_required"
        return True, "trusted_replay"

    if platform != _WECOM_PLATFORM:
        return False, "platform_not_wecom"
    return True, "wecom_authenticated_member"


def _authorize(tool_name: Any) -> tuple[bool, str]:
    """Apply the one shared admission rule used by both public gates."""

    if not isinstance(tool_name, str) or tool_name not in TOOL_NAMES:
        return False, "tool_not_allowed"
    return _identity_allowed()


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
    """Return the stable public denial without exposing identity or policy details."""

    return _deny()


def coarse_authorized(tool_name: str, args: Any) -> bool:
    """Check only the bound caller identity and the registered tool name.

    ``args`` remains in the public signature for callers that perform the cheap
    pre-validation gate.  It is intentionally ignored: DataSage has no
    Profile-level domain, metric, entity, or row entitlement policy.
    """

    del args
    allowed, reason = _authorize(tool_name)
    _audit(tool_name, allowed=allowed, reason=reason)
    return allowed


def authorized(
    tool_name: str,
    args: Any,
    *,
    validated_requests: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Check only the bound caller identity and the registered tool name.

    ``validated_requests`` is retained for compatibility with the query
    composition layer, but no request field participates in authorization.
    """

    del args, validated_requests
    allowed, reason = _authorize(tool_name)
    _audit(tool_name, allowed=allowed, reason=reason)
    return allowed


def guard(tool_name: str, handler: Callable[..., str]) -> Callable[..., str]:
    """Wrap a model-facing handler with the shared admission gate."""

    def guarded(args: dict[str, Any], **kwargs: Any) -> str:
        if not authorized(tool_name, args):
            return _deny()
        return handler(args, **kwargs)

    guarded.__name__ = f"entitlement_guarded_{getattr(handler, '__name__', tool_name)}"
    guarded.__doc__ = getattr(handler, "__doc__", None)
    return guarded
