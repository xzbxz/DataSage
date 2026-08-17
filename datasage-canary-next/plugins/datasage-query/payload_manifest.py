"""Deterministic identity for files owned by a profile distribution."""

from __future__ import annotations

import hashlib
import json
import os
import math
from pathlib import Path, PurePosixPath
import stat
from typing import Any


MANIFEST_PATHS = {
    ".release/RELEASE.json",
    ".release/MANIFEST.sha256",
}
# Backward-compatible singular name used by existing release tests.  The set
# above remains authoritative because both generated identity files must be
# excluded from their own payload.
MANIFEST_PATH = ".release/RELEASE.json"
HASH_ALGORITHM = "sha256-path-nul-canonical-file-sha256-newline-v2"
INSTALLER_PROVENANCE_FIELDS = frozenset({"source", "installed_at"})
_EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".cache",
    ".hub",
    "cache",
    "caches",
    "log",
    "logs",
    "state",
    "states",
    "session",
    "sessions",
    "secret",
    "secrets",
}
_EXCLUDED_FILE_NAMES = {
    ".curator_state",
    ".env",
    ".usage.json",
    ".usage.json.lock",
    "auth.json",
    "credentials.json",
}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class PayloadManifestError(RuntimeError):
    """Raised when the owned payload cannot be enumerated safely."""


def _load_distribution_mapping(path: Path) -> dict[str, Any]:
    """Parse distribution.yaml while rejecting duplicate and non-string keys."""
    try:
        import yaml

        class UniqueKeyLoader(yaml.SafeLoader):
            pass

        def construct_mapping(loader, node, deep=False):
            mapping: dict[str, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise PayloadManifestError(
                        "distribution.yaml mapping keys must be strings"
                    )
                if key in mapping:
                    raise PayloadManifestError(
                        f"distribution.yaml contains duplicate key: {key}"
                    )
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        UniqueKeyLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping,
        )
        parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except PayloadManifestError:
        raise
    except Exception as exc:
        raise PayloadManifestError("distribution.yaml is unreadable or invalid") from exc
    if not isinstance(parsed, dict):
        raise PayloadManifestError("distribution.yaml must be a mapping")
    return parsed


def distribution_owned(profile_root: Path) -> tuple[str, ...]:
    """Read and validate the authoritative distribution_owned sequence."""
    parsed = _load_distribution_mapping(profile_root / "distribution.yaml")
    raw_values = parsed.get("distribution_owned")
    if not isinstance(raw_values, list) or not raw_values:
        raise PayloadManifestError("distribution_owned is missing or empty")
    if any(not isinstance(value, str) for value in raw_values):
        raise PayloadManifestError("distribution_owned entries must be strings")
    values = [_normalize_relative_path(value) for value in raw_values]
    folded = [value.casefold() for value in values]
    if len(folded) != len(set(folded)):
        raise PayloadManifestError("distribution_owned contains duplicate paths")
    if "distribution.yaml" not in folded:
        raise PayloadManifestError("distribution.yaml must own itself")
    if ".release" not in folded:
        raise PayloadManifestError("distribution_owned must declare .release")
    paths = [PurePosixPath(value) for value in values]
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if path == PurePosixPath(".release") or other == PurePosixPath(".release"):
                continue
            if path in other.parents or other in path.parents:
                raise PayloadManifestError("distribution_owned paths overlap")
    return tuple(values)


def _validate_canonical_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PayloadManifestError(f"non-finite YAML number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_canonical_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PayloadManifestError(f"non-string YAML key at {path}")
            _validate_canonical_value(item, f"{path}.{key}")
        return
    raise PayloadManifestError(
        f"unsupported YAML value at {path}: {type(value).__name__}"
    )


def _canonical_distribution_bytes(path: Path) -> bytes:
    """Canonicalize YAML, ignoring only Hermes installer provenance fields."""
    parsed = _load_distribution_mapping(path)
    protected = {
        key: value
        for key, value in parsed.items()
        if key not in INSTALLER_PROVENANCE_FIELDS
    }
    _validate_canonical_value(protected)
    try:
        serialized = json.dumps(
            protected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PayloadManifestError(
            "distribution.yaml cannot be canonicalized"
        ) from exc
    return serialized.encode("utf-8")


def _io_path(candidate: Path) -> Path:
    """Return a Win32 extended path without changing manifest path identity."""
    if os.name != "nt":
        return candidate
    raw = os.path.abspath(candidate)
    if raw.startswith("\\\\?\\"):
        return Path(raw)
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def _file_sha256(candidate: Path, relative: str) -> str:
    try:
        readable = _io_path(candidate)
        content = (
            _canonical_distribution_bytes(readable)
            if relative == "distribution.yaml"
            else readable.read_bytes()
        )
    except OSError as exc:
        raise PayloadManifestError(f"payload file unreadable: {relative}") from exc
    return hashlib.sha256(content).hexdigest()


def _normalize_relative_path(value: str) -> str:
    if "\\" in value:
        raise PayloadManifestError(f"invalid distribution path: {value!r}")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or normalized.endswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PayloadManifestError(f"invalid distribution path: {value!r}")
    for part in path.parts:
        if (
            any(ord(character) < 32 for character in part)
            or any(character in '<>:"|?*' for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise PayloadManifestError(
                f"invalid distribution path on Windows: {value!r}"
            )
    return path.as_posix()


def _is_reparse(path: Path) -> bool:
    details = _io_path(path).lstat()
    if stat.S_ISLNK(details.st_mode):
        return True
    return bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _excluded(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    lowered_parts = tuple(part.casefold() for part in path.parts)
    return (
        relative_path in MANIFEST_PATHS
        or any(part in _EXCLUDED_DIR_NAMES for part in lowered_parts[:-1])
        or lowered_parts[-1] in _EXCLUDED_FILE_NAMES
        or path.suffix.casefold() in _EXCLUDED_SUFFIXES
    )


def _walk_owned(target: Path):
    """Yield plain identity paths while traversing through extended I/O paths."""
    target_io = _io_path(target)
    if target_io.is_file():
        yield target
        return
    for directory, dirnames, filenames in os.walk(
        target_io, topdown=True, followlinks=False
    ):
        relative_dir = Path(directory).relative_to(target_io)
        identity_dir = target.joinpath(*relative_dir.parts)
        for dirname in dirnames:
            candidate = identity_dir / dirname
            if _is_reparse(candidate):
                raise PayloadManifestError(f"reparse points are forbidden: {candidate}")
        for filename in filenames:
            yield identity_dir / filename


def payload_files(profile_root: Path) -> list[dict[str, str]]:
    """Enumerate and hash every valid file under distribution_owned."""
    root = profile_root.resolve(strict=True)
    found: dict[str, Path] = {}
    owned_paths = distribution_owned(root)
    directory_owned = {
        owned
        for owned in owned_paths
        if owned != ".release"
        and _io_path(root.joinpath(*PurePosixPath(owned).parts)).is_dir()
    }
    if directory_owned:
        if ".no-bundled-skills" in {
            value.casefold() for value in owned_paths
        }:
            raise PayloadManifestError(
                "leaf-owned marker cannot be combined with directory-owned paths"
            )
        leaf_contract = False
    else:
        if ".no-bundled-skills" not in {
            value.casefold() for value in owned_paths
        }:
            raise PayloadManifestError(
                "leaf-owned distribution must own .no-bundled-skills"
            )
        leaf_contract = True
    for owned in owned_paths:
        if owned == ".release":
            continue
        target = root.joinpath(*PurePosixPath(owned).parts)
        if not _io_path(target).exists():
            raise PayloadManifestError(f"owned path is missing: {owned}")
        if leaf_contract and not _io_path(target).is_file():
            raise PayloadManifestError(
                f"distribution_owned path must be a file leaf: {owned}"
            )
        try:
            candidates = _walk_owned(target)
            for candidate in candidates:
                if _is_reparse(candidate):
                    raise PayloadManifestError(
                        f"reparse points are forbidden: {candidate}"
                    )
                if not _io_path(candidate).is_file():
                    continue
                relative = candidate.relative_to(root).as_posix()
                if _excluded(relative):
                    if leaf_contract:
                        raise PayloadManifestError(
                            "distribution_owned contains a non-production path: "
                            f"{relative}"
                        )
                    continue
                key = relative.casefold()
                if key in found and found[key] != candidate:
                    raise PayloadManifestError(
                        f"case-colliding payload path: {relative}"
                    )
                found[key] = candidate
        except PayloadManifestError:
            raise
        except (OSError, ValueError) as exc:
            raise PayloadManifestError("payload enumeration failed") from exc
    entries: list[dict[str, str]] = []
    for candidate in sorted(found.values(), key=lambda item: item.relative_to(root).as_posix()):
        relative = candidate.relative_to(root).as_posix()
        digest = _file_sha256(candidate, relative)
        entries.append({"path": relative, "sha256": digest})
    return entries


def aggregate_sha256(entries: list[dict[str, str]]) -> str:
    """Hash canonical path/digest records in deterministic path order."""
    digest = hashlib.sha256()
    previous = ""
    for entry in entries:
        if not isinstance(entry, dict):
            raise PayloadManifestError("payload manifest entries must be objects")
        path = entry.get("path")
        file_sha = entry.get("sha256")
        if (
            not isinstance(path, str)
            or _normalize_relative_path(path) != path
            or path in MANIFEST_PATHS
            or path <= previous
            or not isinstance(file_sha, str)
            or len(file_sha) != 64
            or any(char not in "0123456789abcdef" for char in file_sha)
        ):
            raise PayloadManifestError("invalid or unsorted payload manifest entry")
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha.encode("ascii"))
        digest.update(b"\n")
        previous = path
    return digest.hexdigest()


def build_payload_manifest(profile_root: Path) -> dict[str, Any]:
    entries = payload_files(profile_root)
    return {
        "payload_hash_algorithm": HASH_ALGORITHM,
        "payload_file_count": len(entries),
        "payload_files": entries,
        "payload_sha256": aggregate_sha256(entries),
    }
