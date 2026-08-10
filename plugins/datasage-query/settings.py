"""Profile-owned DataSage behavior settings.

Behavior has one authority: ``config.yaml``.  Environment variables are
reserved for connection coordinates and secrets.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _profile_root() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(configured).resolve()
        if configured
        else Path(__file__).resolve().parents[2]
    )


@lru_cache(maxsize=8)
def _read_settings_cached(
    path_text: str,
    modified_ns: int,
    size: int,
) -> dict[str, Any]:
    del modified_ns, size
    parsed = yaml.safe_load(Path(path_text).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return {}
    plugins = parsed.get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    plugin = (
        entries.get("datasage-query")
        if isinstance(entries, dict)
        else None
    )
    settings = plugin.get("settings") if isinstance(plugin, dict) else None
    return dict(settings) if isinstance(settings, dict) else {}


def profile_settings() -> dict[str, Any]:
    path = _profile_root() / "config.yaml"
    try:
        stat = path.stat()
        return _read_settings_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, UnicodeError, yaml.YAMLError):
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
