"""Signature-aware metric and entity-selection receipt storage.

The metric cache is keyed by a ``(domain, metric)`` pair.  Entity receipts are
random, process-local tokens backed by a bounded selection registry.  Neither
path is a general cache abstraction, reads a database, or stores conversation
Memory; contract signatures determine validity for governed values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
from threading import RLock
from typing import Any, TypeVar, cast

from . import capability_contract, contract_store


_DATASETS_PATH = "plugins/datasage-query/contracts/datasets.yaml"
_ENTITY_REGISTRY_PATH = "plugins/datasage-query/contracts/entity-registry.yaml"
RESOLUTION_RECEIPT_VERSION = "datasage-entity-resolution-receipt/v1"
RESOLUTION_RECEIPT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESOLUTION_RECEIPTS = 4096
_T = TypeVar("_T")
_ContractSignature = tuple[tuple[str, str], ...]


@dataclass
class _CacheEntry:
    """Keep the builder alive so its identity cannot be reused by Python."""

    builder: Callable[[str, str], Any]
    value: Any


_CACHE: dict[tuple[str, str, int, _ContractSignature], _CacheEntry] = {}
# This is intentionally a narrow, process-local registry for opaque entity
# resolution receipts.  It is not a general cache and it never stores the
# raw session identifier or any conversation/Memory state.
_RESOLUTION_RECEIPTS: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = RLock()


def _contract_paths(domain: str) -> tuple[str, ...]:
    source = capability_contract.DOMAIN_SOURCES.get(domain)
    semantics = source.get("semantics") if isinstance(source, Mapping) else None
    if not isinstance(semantics, str) or not semantics:
        raise ValueError(f"unsupported capability-receipt domain: {domain!r}")

    paths = [
        _DATASETS_PATH,
        semantics,
        capability_contract.QUERY_POLICY_PATH,
    ]
    if domain == "target":
        paths.append(capability_contract.TARGET_GAP_CONTRACT_PATH)
    return tuple(paths)


def _contract_signature(domain: str) -> _ContractSignature:
    return tuple(
        contract_store.content_signature(path) for path in _contract_paths(domain)
    )


def resolution_contract_signature(domain: str) -> str:
    """Return the current identity of contracts governing entity resolution.

    The entity registry is included in addition to the existing metric-detail
    contract set.  A receipt therefore cannot survive a role, identity-filter,
    dataset, semantic, query-policy, or target-gap contract change.
    """

    signatures = tuple(
        (*item,)
        for item in (*_contract_signature(domain),
                     contract_store.content_signature(_ENTITY_REGISTRY_PATH))
    )
    canonical = json.dumps(
        {"version": RESOLUTION_RECEIPT_VERSION, "contracts": signatures},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256_" + hashlib.sha256(canonical).hexdigest()


def resolution_session_binding(session_id: Any) -> dict[str, str | None]:
    """Hash a host-provided session id without retaining the raw identifier."""

    if isinstance(session_id, str) and session_id.strip():
        return {
            "session_binding": "bound",
            "session_id_sha256": hashlib.sha256(
                session_id.encode("utf-8", "surrogatepass")
            ).hexdigest(),
            "session_ref": hashlib.sha256(
                session_id.encode("utf-8", "surrogatepass")
            ).hexdigest()[:16],
        }
    return {
        "session_binding": "session_unbound",
        "session_id_sha256": None,
        "session_ref": "session_unbound",
    }


def issue_resolution_receipt(payload: Mapping[str, Any]) -> str:
    """Issue one opaque, process-local entity-resolution receipt.

    The token is random rather than derivable from candidate fields, so a
    model cannot manufacture a valid receipt in the same assistant batch.
    Stored records are bounded and deep-copied; only the opaque token crosses
    the model boundary.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("resolution receipt payload must be a mapping")
    token = secrets.token_hex(32)
    record = deepcopy(dict(payload))
    record.setdefault("version", RESOLUTION_RECEIPT_VERSION)
    with _CACHE_LOCK:
        while len(_RESOLUTION_RECEIPTS) >= _MAX_RESOLUTION_RECEIPTS:
            _RESOLUTION_RECEIPTS.pop(next(iter(_RESOLUTION_RECEIPTS)))
        _RESOLUTION_RECEIPTS[token] = record
    return token


def get_resolution_receipt(token: str) -> dict[str, Any] | None:
    """Return a copy of one issued receipt, or ``None`` for unknown/tampered."""

    if not isinstance(token, str) or RESOLUTION_RECEIPT_PATTERN.fullmatch(token) is None:
        return None
    with _CACHE_LOCK:
        record = _RESOLUTION_RECEIPTS.get(token)
        return deepcopy(record) if record is not None else None


def clear_resolution_receipts() -> None:
    """Clear process-local entity receipts for isolated tests or shutdown."""

    with _CACHE_LOCK:
        _RESOLUTION_RECEIPTS.clear()


def get_metric_capability_receipt(
    domain: str,
    metric: str,
    *,
    builder: Callable[[str, str], _T],
) -> _T:
    """Return a contract-current receipt value for one governed metric.

    ``builder`` is invoked as ``builder(domain, metric)``.  The lock covers the
    cache lookup and a cache miss build, so concurrent callers for the same
    signature cannot duplicate catalog work.  Both the stored value and every
    returned value are deep copies; a mutable receipt projection therefore
    cannot be used to mutate cache state.  Exceptions from file inspection,
    the builder, or copying are propagated and never cached.
    """

    if not isinstance(metric, str) or not metric.strip():
        raise ValueError("metric capability receipt requires a non-empty metric")
    if not callable(builder):
        raise TypeError("metric capability receipt builder must be callable")

    normalized_metric = metric.strip()
    with _CACHE_LOCK:
        signature = _contract_signature(domain)
        builder_identity = id(builder)
        key = (domain, normalized_metric, builder_identity, signature)
        cached = _CACHE.get(key)
        if cached is not None and cached.builder is builder:
            return cast(_T, deepcopy(cached.value))

        built = builder(domain, normalized_metric)
        stored = deepcopy(built)

        # Keep only the current signature for this builder/domain/metric tuple.
        stale_keys = [
            candidate
            for candidate, entry in _CACHE.items()
            if candidate[:3] == key[:3] and entry.builder is builder
        ]
        for stale_key in stale_keys:
            del _CACHE[stale_key]
        _CACHE[key] = _CacheEntry(builder=builder, value=stored)
        return cast(_T, deepcopy(stored))


def clear_metric_capability_receipt_cache() -> None:
    """Clear all cached metric capability receipts for this process."""

    with _CACHE_LOCK:
        _CACHE.clear()
