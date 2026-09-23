"""Backward-compatible sales price reference entry point.

The implementation lives in price_reference; this module preserves
the legacy sales names and private helper imports used by existing callers.
"""

from .price_reference import (
    SCHEMA,
    _HEAD_SCHEMA,
    _BATCH_RE,
    _SIDE_CONFIG,
    _canonical,
    _digest,
    _json,
    _time,
    _origin_provenance,
    _side_config,
    _legacy_spec,
    _validate_decimal_string,
    _validate_reference_rows,
    _batch_id,
    _hex,
    _component_keys,
    _validate_decimal_paths,
    _seal,
    _head_root,
    PriceReferenceError,
    PriceReferenceStore,
)

SalesReferenceError = PriceReferenceError
SalesReferenceStore = PriceReferenceStore

__all__ = [
    "SCHEMA",
    "_HEAD_SCHEMA",
    "_BATCH_RE",
    "_SIDE_CONFIG",
    "_canonical",
    "_digest",
    "_json",
    "_time",
    "_origin_provenance",
    "_side_config",
    "_legacy_spec",
    "_validate_decimal_string",
    "_validate_reference_rows",
    "_batch_id",
    "_hex",
    "_component_keys",
    "_validate_decimal_paths",
    "_seal",
    "_head_root",
    "PriceReferenceError",
    "PriceReferenceStore",
    "SalesReferenceError",
    "SalesReferenceStore",
]

