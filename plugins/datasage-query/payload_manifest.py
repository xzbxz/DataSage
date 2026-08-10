"""Deterministic identity for files owned by a profile distribution."""

from __future__ import annotations

import hashlib
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
HASH_ALGORITHM = "sha256-path-nul-file-sha256-newline-v1"
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


class PayloadManifestError(RuntimeError):
    """Raised when the owned payload cannot be enumerated safely."""


def distribution_owned(profile_root: Path) -> tuple[str, ...]:
    """Read the top-level distribution_owned YAML sequence without PyYAML."""
    path = profile_root / "distribution.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PayloadManifestError("distribution.yaml is unreadable") from exc
    values: list[str] = []
    in_owned = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_owned and stripped.startswith("-"):
            value = stripped[1:].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(_normalize_relative_path(value))
            continue
        if not raw_line[:1].isspace():
            if stripped == "distribution_owned:":
                in_owned = True
                continue
            if in_owned:
                break
        if in_owned:
            if not stripped.startswith("-"):
                raise PayloadManifestError("distribution_owned must be a sequence")
            value = stripped[1:].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(_normalize_relative_path(value))
    if not values:
        raise PayloadManifestError("distribution_owned is missing or empty")
    folded = [value.casefold() for value in values]
    if len(folded) != len(set(folded)):
        raise PayloadManifestError("distribution_owned contains duplicate paths")
    if "distribution.yaml" not in folded:
        raise PayloadManifestError("distribution.yaml must own itself")
    return tuple(values)


def _normalize_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or normalized.endswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PayloadManifestError(f"invalid distribution path: {value!r}")
    return path.as_posix()


def _is_reparse(path: Path) -> bool:
    details = path.lstat()
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


def payload_files(profile_root: Path) -> list[dict[str, str]]:
    """Enumerate and hash every valid file under distribution_owned."""
    root = profile_root.resolve(strict=True)
    found: dict[str, Path] = {}
    for owned in distribution_owned(root):
        target = root.joinpath(*PurePosixPath(owned).parts)
        if not target.exists():
            raise PayloadManifestError(f"owned path is missing: {owned}")
        candidates = [target] if target.is_file() else target.rglob("*")
        for candidate in candidates:
            try:
                if _is_reparse(candidate):
                    raise PayloadManifestError(
                        f"reparse points are forbidden: {candidate}"
                    )
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(root).as_posix()
            except (OSError, ValueError) as exc:
                raise PayloadManifestError("payload enumeration failed") from exc
            if _excluded(relative):
                continue
            key = relative.casefold()
            if key in found and found[key] != candidate:
                raise PayloadManifestError(f"case-colliding payload path: {relative}")
            found[key] = candidate
    entries: list[dict[str, str]] = []
    for candidate in sorted(found.values(), key=lambda item: item.relative_to(root).as_posix()):
        relative = candidate.relative_to(root).as_posix()
        try:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError as exc:
            raise PayloadManifestError(f"payload file unreadable: {relative}") from exc
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
