"""Profile-owned DataSage behavior settings.

Behavior has one authority: ``config.yaml``.  Environment variables are
reserved for connection coordinates and secrets.
"""

from __future__ import annotations

import copy
from typing import Any

from hermes_cli import config as hermes_config


def _plugin_settings(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    plugins = config.get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    plugin = (
        entries.get("datasage-query")
        if isinstance(entries, dict)
        else None
    )
    settings = plugin.get("settings") if isinstance(plugin, dict) else None
    return copy.deepcopy(settings) if isinstance(settings, dict) else {}


def profile_settings() -> dict[str, Any]:
    try:
        return _plugin_settings(hermes_config.load_config_readonly())
    except Exception:
        return {}


def get(key: str, default: Any = None) -> Any:
    values = profile_settings()
    return values[key] if key in values else default


def get_bool(key: str, default: bool = False) -> bool:
    value = get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
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
