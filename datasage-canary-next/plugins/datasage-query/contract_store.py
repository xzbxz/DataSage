"""Shared, profile-scoped YAML contract storage.

This module owns path containment and the file-signature cache.  Callers map
``ContractStoreError`` to their own public failure taxonomy.
"""

from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import threading
from typing import Any

import yaml

from . import capability_contract


class ContractStoreError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason_code: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        # ``code`` remains compatible with the existing contract-load error
        # taxonomy.  A drift reason is kept separately so callers can expose
        # the fail-closed cause without making it part of a model-facing
        # message.
        self.reason_code = reason_code


def _readonly(*_args: Any, **_kwargs: Any) -> Any:
    """Refuse in-place mutation of pinned contract data (F05)."""

    raise TypeError("pinned contract data is read-only")


class _FrozenDict(dict):
    """Mapping view that keeps ``dict`` semantics but cannot be mutated.

    A plain ``MappingProxyType`` would break ``isinstance(value, dict)`` and
    JSON serialisation for existing consumers, so the view stays a ``dict``
    subclass and the normal mutating methods are refused. This is an API
    ownership guard, not a sandbox against explicit base-class bypasses.
    """

    __slots__ = ()

    __init__ = _readonly
    __setitem__ = _readonly
    __delitem__ = _readonly
    clear = _readonly
    pop = _readonly
    popitem = _readonly
    setdefault = _readonly
    update = _readonly
    __ior__ = _readonly

    def __copy__(self) -> dict:
        """Explicit mutation boundary: a plain mutable shallow copy."""

        return dict(self)

    def __deepcopy__(self, memo: dict) -> dict:
        """Explicit mutation boundary: a plain mutable deep copy."""

        return copy.deepcopy(dict(self), memo)


class _FrozenList(list):
    """Sequence view that keeps ``list`` semantics but cannot be mutated."""

    __slots__ = ()

    __init__ = _readonly
    __setitem__ = _readonly
    __delitem__ = _readonly
    __iadd__ = _readonly
    __imul__ = _readonly
    append = _readonly
    clear = _readonly
    extend = _readonly
    insert = _readonly
    pop = _readonly
    remove = _readonly
    reverse = _readonly
    sort = _readonly

    def __copy__(self) -> list:
        """Explicit mutation boundary: a plain mutable shallow copy."""

        return list(self)

    def __deepcopy__(self, memo: dict) -> list:
        """Explicit mutation boundary: a plain mutable deep copy."""

        return copy.deepcopy(list(self), memo)


def freeze_contract(value: Any) -> Any:
    """Return a recursively read-only view of a parsed contract.

    The frozen structure is built with the base-class operations so the
    overridden mutators above are never consulted while constructing it.
    Scalars and unknown types are returned unchanged.
    """

    if isinstance(value, dict):
        frozen = _FrozenDict.__new__(_FrozenDict)
        dict.__init__(frozen)
        for key, item in value.items():
            dict.__setitem__(frozen, key, freeze_contract(item))
        return frozen
    if isinstance(value, list):
        frozen_list = _FrozenList.__new__(_FrozenList)
        list.__init__(frozen_list)
        for item in value:
            list.append(frozen_list, freeze_contract(item))
        return frozen_list
    return value


_SNAPSHOT_LOCK = threading.RLock()
_CONTRACT_SNAPSHOT: dict[str, Any] | None = None
_SNAPSHOT_BOOTSTRAP_COUNT = 0
_DATASETS_CONTRACT_PATH = "plugins/datasage-query/contracts/datasets.yaml"
# These are explicit execution inputs.  The list is intentionally finite and
# does not discover or hash the surrounding profile/repository.  The first
# entries are the release/audit contract set; entity-registry and target-gap
# are included because the live tools read them on the normal execution path.
PINNED_CONTRACT_PATHS = tuple(
    dict.fromkeys(
        (
            _DATASETS_CONTRACT_PATH,
            "plugins/datasage-query/contracts/operations.yaml",
            "plugins/datasage-query/contracts/legacy-workflows.json",
            capability_contract.QUERY_POLICY_PATH,
            *(
                source["semantics"]
                for source in capability_contract.DOMAIN_SOURCES.values()
            ),
            "plugins/datasage-query/contracts/entity-registry.yaml",
            capability_contract.TARGET_GAP_CONTRACT_PATH,
        )
    )
)

# F03: ``operations.yaml`` and ``legacy-workflows.json`` belong to the operator
# and legacy entry points, not to the three query tools.  Pinning them in the
# same strict group meant one malformed operator file stopped the whole query
# surface from registering.  They keep the same bytes, digest and
# restart-refresh semantics, but their read failure is recorded per file
# instead of raised, and their own readers still fail closed.
OPERATOR_CONTRACT_PATHS = (
    "plugins/datasage-query/contracts/operations.yaml",
    "plugins/datasage-query/contracts/legacy-workflows.json",
)
QUERY_CONTRACT_PATHS = tuple(
    path for path in PINNED_CONTRACT_PATHS if path not in OPERATOR_CONTRACT_PATHS
)


def profile_root() -> Path:
    """Return the Profile that physically owns this loaded plugin module.

    Contract identity must follow the code instance Hermes actually imported,
    not a mutable or missing ``HERMES_HOME`` process value.  Runtime health
    separately verifies that the loaded Profile path is approved.
    """

    return Path(__file__).resolve(strict=False).parents[2]


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


def _canonical_relative_path(relative_path: str, root: Path) -> str:
    """Return a lexical, profile-relative key for a requested contract."""

    requested = Path(relative_path).expanduser()
    lexical = requested if requested.is_absolute() else root / requested
    try:
        relative = lexical.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract path escapes the active profile"
        ) from exc
    return relative.as_posix()


def _read_current_contract_bytes(relative_path: str) -> tuple[Path, bytes, str]:
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


def _snapshot_error(
    reason_code: str,
    message: str = "contract snapshot is unavailable",
) -> ContractStoreError:
    return ContractStoreError(
        "CONTRACT_UNAVAILABLE",
        message,
        reason_code=reason_code,
    )


def _snapshot_manifest_digest(root: Path, entries: dict[str, dict[str, Any]]) -> str:
    payload = {
        "profile_root": str(root),
        "contracts": [
            {
                "relative_path": relative,
                "path": entry["path"],
                "sha256": entry["sha256"],
            }
            for relative, entry in sorted(entries.items())
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _snapshot_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a detached view so callers cannot mutate the live identity."""

    return {
        "profile_root": snapshot["profile_root"],
        "entries": {
            relative: {
                "path": entry["path"],
                "sha256": entry["sha256"],
            }
            for relative, entry in snapshot["entries"].items()
        },
        "operator_failures": {
            relative: dict(failure)
            for relative, failure in snapshot.get("operator_failures", {}).items()
        },
        "manifest_digest": snapshot["manifest_digest"],
    }


def _pin_one_contract(relative_path: str) -> dict[str, Any]:
    """Read, parse and describe one contract file for the snapshot."""

    path, content, digest = _read_current_contract_bytes(relative_path)
    text = content.decode("utf-8")
    parsed = parse_yaml_cached(str(path), digest, text)
    return {
        "path": str(path),
        "sha256": digest,
        "content": bytes(content),
        "parsed": parsed,
    }


# Failure kinds a contract file can raise while being pinned.  Kept in one
# place so the strict query group and the tolerant operator group agree on
# what "unreadable contract" means.
_PINNABLE_ERRORS = (OSError, RuntimeError, UnicodeError, ValueError, yaml.YAMLError)


def _pin_contract_snapshot() -> dict[str, Any]:
    """Pin the finite execution-contract set for this plugin process.

    The first caller reads and parses each explicit contract once and publishes
    one process-lifetime snapshot under the lock.  Later callers reuse the
    cached bytes and parsed mappings; file changes take effect after the
    process is restarted.

    Query contracts stay strict: the three registered tools cannot run without
    them, so a failure still fails closed.  Operator/legacy contracts are
    pinned from the same bytes, but a broken operator file is recorded under
    ``operator_failures`` rather than preventing query registration (F03), and
    its own readers still fail closed on the operator path.
    """

    global _CONTRACT_SNAPSHOT, _SNAPSHOT_BOOTSTRAP_COUNT
    root = profile_root()
    with _SNAPSHOT_LOCK:
        if _CONTRACT_SNAPSHOT is not None:
            if root != _CONTRACT_SNAPSHOT["profile_root"]:
                raise _snapshot_error(
                    "profile_root_changed",
                    "contract snapshot belongs to a different profile root",
                )
            return _CONTRACT_SNAPSHOT

        entries: dict[str, dict[str, Any]] = {}
        try:
            for relative_path in QUERY_CONTRACT_PATHS:
                entries[relative_path] = _pin_one_contract(relative_path)
        except ContractStoreError as exc:
            raise ContractStoreError(
                exc.code,
                exc.message,
                reason_code=exc.reason_code or "snapshot_contract_read_unavailable",
            ) from exc
        except _PINNABLE_ERRORS as exc:
            raise _snapshot_error("snapshot_contract_read_unavailable") from exc

        operator_failures: dict[str, dict[str, str]] = {}
        for relative_path in OPERATOR_CONTRACT_PATHS:
            try:
                entries[relative_path] = _pin_one_contract(relative_path)
            except ContractStoreError as exc:
                operator_failures[relative_path] = {
                    "reason_code": "operator_contract_unavailable",
                    "cause_code": exc.code,
                    "message": exc.message,
                }
            except _PINNABLE_ERRORS as exc:
                operator_failures[relative_path] = {
                    "reason_code": "operator_contract_unavailable",
                    "cause_code": exc.__class__.__name__,
                    "message": str(exc) or exc.__class__.__name__,
                }

        _CONTRACT_SNAPSHOT = {
            "profile_root": root,
            "entries": entries,
            "operator_failures": operator_failures,
            "manifest_digest": _snapshot_manifest_digest(root, entries),
        }
        _SNAPSHOT_BOOTSTRAP_COUNT += 1
        return _CONTRACT_SNAPSHOT


def pin_contract_snapshot() -> dict[str, Any]:
    """Pin and return a detached view of the execution-contract snapshot."""

    return _snapshot_view(_pin_contract_snapshot())


def _ensure_contract_snapshot() -> dict[str, Any]:
    """Return the process-lifetime snapshot, bootstrapping it exactly once."""

    return _pin_contract_snapshot()


def _cached_contract(relative_path: str) -> tuple[Path, dict[str, Any]]:
    """Return one pinned contract entry without touching the filesystem."""

    snapshot = _ensure_contract_snapshot()
    root = snapshot["profile_root"]
    path = trusted_path(relative_path)
    try:
        key = _canonical_relative_path(str(path), root)
    except ContractStoreError:
        raise
    entry = snapshot["entries"].get(key)
    if entry is None:
        failure = (snapshot.get("operator_failures") or {}).get(key)
        if failure is not None:
            # The query surface registered without this operator/legacy file;
            # its own reader must still fail closed with the recorded cause.
            raise ContractStoreError(
                "CONTRACT_UNAVAILABLE",
                "operator contract is unavailable in this process",
                reason_code=failure["reason_code"],
            )
        raise _snapshot_error(
            "contract_path_not_in_snapshot",
            "contract path is not part of the registered snapshot",
        )
    if entry["path"] != str(path):
        raise _snapshot_error(
            "contract_path_changed",
            "contract path does not match the registered snapshot",
        )
    return path, entry


def _contract_bytes(relative_path: str) -> tuple[Path, bytes, str]:
    """Return one contract's bytes and digest from the process snapshot."""

    path, entry = _cached_contract(relative_path)
    return path, bytes(entry["content"]), str(entry["sha256"])


def content_signature(relative_path: str) -> tuple[str, str]:
    """Return a Profile- and content-bound signature for one contract file."""

    path, _content, digest = _contract_bytes(relative_path)
    return str(path), digest


def contract_snapshot_status() -> dict[str, Any]:
    """Return truthful process-level contract snapshot state."""

    with _SNAPSHOT_LOCK:
        snapshot = _CONTRACT_SNAPSHOT
    root_changed = bool(
        snapshot is not None and profile_root() != snapshot["profile_root"]
    )
    return {
        "loaded": snapshot is not None and not root_changed,
        "fixed": snapshot is not None,
        "enforced_per_read": False,
        "profile_root": (
            str(snapshot["profile_root"]) if snapshot is not None else None
        ),
        "manifest_digest": (
            snapshot["manifest_digest"] if snapshot is not None else None
        ),
        "file_count": len(snapshot["entries"]) if snapshot is not None else 0,
        "operator_contracts_loaded": bool(
            snapshot is not None and not snapshot.get("operator_failures")
        ),
        "operator_contracts_unavailable": sorted(
            (snapshot.get("operator_failures") or {}) if snapshot is not None else {}
        ),
        "drifted": root_changed,
        "drift_reason": "profile_root_changed" if root_changed else None,
        "reason_code": "CONTRACT_SNAPSHOT_ROOT_CHANGED" if root_changed else None,
    }


def reset_contract_snapshot_for_tests() -> None:
    """TEST-ONLY: clear the process snapshot between isolated fixture roots."""

    global _CONTRACT_SNAPSHOT, _SNAPSHOT_BOOTSTRAP_COUNT
    with _SNAPSHOT_LOCK:
        _CONTRACT_SNAPSHOT = None
        _SNAPSHOT_BOOTSTRAP_COUNT = 0


def bootstrap_contract_snapshot_for_tests() -> dict[str, Any]:
    """TEST-ONLY: explicitly bootstrap and inspect the current snapshot."""

    pin_contract_snapshot()
    return dict(contract_snapshot_status())


class DuplicateContractKeyError(yaml.YAMLError):
    """A contract mapping repeats the same explicit key at one level (F06)."""


class _ContractLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate explicit keys inside one mapping.

    ``<<`` merge keys stay legal: the document's own keys are checked first and
    the merged pairs are expanded afterwards, so an explicit key may still
    override a merged value exactly as the YAML merge specification allows.
    Parsing remains safe-loading; this only adds a same-level key check.
    """

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        # Merge flattening mutates shared YAML nodes. Check explicit keys once,
        # BEFORE that mutation, including nodes reached through a merge alias.
        self._checked_explicit_nodes: set[Any] = set()

    def flatten_mapping(self, node: Any) -> None:
        if node not in self._checked_explicit_nodes:
            seen: set[Any] = set()
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=False)
                try:
                    repeated = key in seen
                except TypeError:  # SafeLoader reports unsupported complex keys.
                    continue
                if repeated:
                    mark = key_node.start_mark
                    raise DuplicateContractKeyError(
                        f"duplicate explicit key {key!r} at line "
                        f"{mark.line + 1} column {mark.column + 1}"
                    )
                seen.add(key)
            self._checked_explicit_nodes.add(node)
        super().flatten_mapping(node)


@lru_cache(maxsize=64)
def parse_yaml_cached(
    path_text: str, content_sha256: str, content_text: str
) -> dict[str, Any]:
    del path_text, content_sha256
    value = yaml.load(content_text, Loader=_ContractLoader)
    if not isinstance(value, dict):
        raise ContractStoreError(
            "CONTRACT_UNAVAILABLE", "contract root must be a mapping"
        )
    # F05: the parsed object is cached and shared by every reader, so it is
    # published as a recursively read-only view.  A consumer that needs to
    # modify contract data must copy it explicitly instead of editing the
    # process cache.
    return freeze_contract(value)


def read_yaml(relative_path: str) -> dict[str, Any]:
    """Return the pinned, read-only parsed contract for ``relative_path``.

    The digest recorded for the entry always describes the source bytes; this
    returned mapping is the process-lifetime parsed view of those bytes and
    cannot be mutated in place.
    """

    try:
        _path, entry = _cached_contract(relative_path)
        return entry["parsed"]
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
