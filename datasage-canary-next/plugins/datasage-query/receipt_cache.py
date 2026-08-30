"""Signature-aware cache for per-metric capability receipts.

This module intentionally caches only values built for a ``(domain, metric)``
pair.  It is not a general cache abstraction and does not read databases or
entity state.  Cache validity follows the contract files that can affect the
catalog projection for the requested domain.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any, TypeVar, cast

from . import capability_contract, contract_store


_DATASETS_PATH = "plugins/datasage-query/contracts/datasets.yaml"
_T = TypeVar("_T")
_ContractSignature = tuple[tuple[str, str], ...]


@dataclass
class _CacheEntry:
    """Keep the builder alive so its identity cannot be reused by Python."""

    builder: Callable[[str, str], Any]
    value: Any


_CACHE: dict[tuple[str, str, int, _ContractSignature], _CacheEntry] = {}
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
