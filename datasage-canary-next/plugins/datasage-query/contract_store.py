"""Shared, profile-scoped YAML contract storage.

This module owns path containment and the file-signature cache.  Callers map
``ContractStoreError`` to their own public failure taxonomy.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from hermes_constants import get_hermes_home

from . import capability_contract


class ContractStoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def profile_root() -> Path:
    """Return the active Hermes Profile root for the current call context."""

    return get_hermes_home().expanduser().resolve(strict=False)


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


def _contract_bytes(relative_path: str) -> tuple[Path, bytes, str]:
    try:
        path = trusted_path(relative_path)
        content = path.read_bytes()
    except ContractStoreError:
        raise
    except OSError as exc:
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract could not be read"
        ) from exc
    return path, content, hashlib.sha256(content).hexdigest()


def content_signature(relative_path: str) -> tuple[str, str]:
    """Return a Profile- and content-bound signature for one contract file."""

    path, _content, digest = _contract_bytes(relative_path)
    return str(path), digest


@lru_cache(maxsize=64)
def parse_yaml_cached(
    path_text: str, content_sha256: str, content_text: str
) -> dict[str, Any]:
    del path_text, content_sha256
    value = yaml.safe_load(content_text)
    if not isinstance(value, dict):
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract root must be a mapping"
        )
    return value


def read_yaml(relative_path: str) -> dict[str, Any]:
    try:
        path, content, digest = _contract_bytes(relative_path)
        text = content.decode("utf-8")
        return parse_yaml_cached(str(path), digest, text)
    except ContractStoreError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract could not be read"
        ) from exc


def read_query_policy() -> capability_contract.QueryPolicy:
    """Read and validate the common query policy through its sole parser."""

    return capability_contract.parse_query_policy(
        read_yaml(capability_contract.QUERY_POLICY_PATH)
    )


def read_target_gap_contract(
    relative_path: str = capability_contract.TARGET_GAP_CONTRACT_PATH,
) -> capability_contract.TargetGapContract:
    """Read the minimal executable target-gap contract."""

    return capability_contract.parse_target_gap_contract(read_yaml(relative_path))
