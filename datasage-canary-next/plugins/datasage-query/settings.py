"""DataSage settings and deferred access to the official Hermes secret scope."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any


_CONFIG_READER: Callable[[str, Any], Any] | None = None
_MISSING = object()


def bind_config_reader(reader: Callable[[str, Any], Any] | None) -> None:
    """Bind the plugin-relative reader provided by ``PluginContext``."""

    if reader is not None and not callable(reader):
        raise TypeError("config reader must be callable")
    global _CONFIG_READER
    _CONFIG_READER = reader


def get(key: str, default: Any = None) -> Any:
    reader = _CONFIG_READER
    if reader is None:
        return default
    try:
        value = reader(key, _MISSING)
        if value is _MISSING:
            return default
        return copy.deepcopy(value)
    except Exception:
        return default


def get_int(
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        value = default
    return max(minimum, min(maximum, value))


def get_list(key: str) -> list[str]:
    value = get(key, [])
    if not isinstance(value, (list, tuple)):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def get_secret(name: str, default: Any = None) -> Any:
    """Delegate at use time to Hermes; never copy its scope or read os.environ.

    Importing a compiler/catalog/security rule must not require the secret
    subsystem. An actual credential read still requires the official host and
    propagates import/scope failures; absence is NOT treated as a default value.
    No getter or secret is cached, so the current bound scope is used each time.
    """

    from agent.secret_scope import get_secret as host_get_secret

    return host_get_secret(name, default)
