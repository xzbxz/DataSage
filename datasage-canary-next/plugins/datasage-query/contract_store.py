"""Shared, profile-scoped YAML contract storage.

This module owns path containment and the file-signature cache.  Callers map
``ContractStoreError`` to their own public failure taxonomy.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


class ContractStoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def profile_root() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def trusted_path(relative_path: str) -> Path:
    root = profile_root()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract path escapes the active profile"
        ) from exc
    return path


@lru_cache(maxsize=64)
def parse_yaml_cached(path_text: str, modified_ns: int, size: int) -> dict[str, Any]:
    del modified_ns, size
    value = yaml.safe_load(Path(path_text).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract root must be a mapping"
        )
    return value


def read_yaml(relative_path: str) -> dict[str, Any]:
    try:
        path = trusted_path(relative_path)
        stat = path.stat()
        return parse_yaml_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except ContractStoreError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract could not be read"
        ) from exc
