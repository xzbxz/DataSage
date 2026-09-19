"""Atomic weekly IDK content sealing.

The stored content is opaque to this module after JSON-shape validation.  Data
collection, recipient selection, Progress receipt state, and sending remain
outside the storage boundary.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import uuid
from typing import Any, Mapping


SCHEMA = "datasage-idk-batch/v1"


class IdkBatchError(ValueError):
    pass


def _canonical(value: Any) -> str:
    from .legacy_workflow import canonical

    return canonical(value)


def _digest(value: Any) -> str:
    from .legacy_workflow import digest

    return digest(value)


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_json(value: Any, code: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IdkBatchError(code) from exc


def _reparse(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _week(week: str) -> str:
    if not isinstance(week, str) or not re.fullmatch(r"\d{4}-W\d{2}", week):
        raise IdkBatchError("IDK_WEEK_INVALID")
    try:
        date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
    except ValueError as exc:
        raise IdkBatchError("IDK_WEEK_INVALID") from exc
    return week


def _observed_at(value: str) -> str:
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise IdkBatchError("IDK_OBSERVED_AT_INVALID") from exc
    return str(value)


def batch_root(profile: Path) -> Path:
    from .workflow_io import private_root

    root = private_root(profile) / "idk_batches"
    if _reparse(root) or not root.resolve().is_relative_to(profile.resolve()):
        raise IdkBatchError("IDK_BATCH_ROOT_INVALID")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(root: Path, week: str) -> Path:
    _week(week)
    root = Path(root)
    if _reparse(root):
        raise IdkBatchError("IDK_BATCH_ROOT_INVALID")
    base = root.resolve()
    result = (base / week).resolve()
    if not result.is_relative_to(base):
        raise IdkBatchError("IDK_BATCH_PATH_INVALID")
    for part in (base / week, result):
        if part.exists() and _reparse(part):
            raise IdkBatchError("IDK_BATCH_PATH_SYMLINK")
    return result


def _atomic_json(path: Path, value: Any) -> None:
    from . import operations

    operations._atomic(path, value)


def _read(path: Path, code: str) -> bytes:
    if _reparse(path) or not path.is_file():
        raise IdkBatchError(code)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise IdkBatchError(code) from exc


def _target_digest(target_map: Mapping[str, Any]) -> str:
    return _digest(target_map)


def _seal(manifest: Mapping[str, Any]) -> str:
    return _digest({key: value for key, value in manifest.items() if key != "metadata_seal"})


def _progress_has_history(progress: Any) -> bool:
    if progress is None:
        return False
    data = progress.get("data") if isinstance(progress, Mapping) else getattr(progress, "data", None)
    if not isinstance(data, Mapping):
        return False
    components = data.get("components")
    if isinstance(components, Mapping) and any((value or {}).get("status", "not_attempted") != "not_attempted" for value in components.values() if isinstance(value, Mapping)):
        return True
    return bool(data.get("notification_manifests") or data.get("notification_history") or data.get("report_phase_bindings"))


def _validate_accounts(logical_accounts: list[str], target_map: Mapping[str, Any]) -> None:
    if not isinstance(logical_accounts, list) or any(not isinstance(account, str) or not account.strip() for account in logical_accounts):
        raise IdkBatchError("IDK_LOGICAL_ACCOUNTS_INVALID")
    if len(logical_accounts) != len(set(logical_accounts)):
        raise IdkBatchError("IDK_LOGICAL_ACCOUNTS_INVALID")
    if not isinstance(target_map, dict) or set(target_map) != set(logical_accounts):
        raise IdkBatchError("IDK_TARGET_MAP_ACCOUNT_MISMATCH")
    if any(not isinstance(target, dict) or not target for target in target_map.values()):
        raise IdkBatchError("IDK_TARGET_MAP_INVALID")


class IdkBatchStore:
    def __init__(self, root: Path, week: str):
        self.root = Path(root)
        self.week = _week(week)
        self.directory = _path(self.root, week)
        self.manifest_path = self.directory / "manifest.json"
        self.content_path = self.directory / "content.json"

    def exists(self) -> bool:
        return self.manifest_path.exists() or self.directory.exists()

    def prepare(
        self,
        content: str,
        *,
        observed_at: str,
        logical_accounts: list[str],
        target_map: Mapping[str, Any],
        evidence: Mapping[str, Any],
        zero: bool = False,
        progress: Any = None,
    ) -> dict[str, Any]:
        _observed_at(observed_at)
        _validate_accounts(logical_accounts, target_map)
        if not isinstance(evidence, Mapping):
            raise IdkBatchError("IDK_EVIDENCE_INVALID")
        _require_json(target_map, "IDK_TARGET_MAP_NOT_JSON_SERIALIZABLE")
        _require_json(evidence, "IDK_EVIDENCE_NOT_JSON_SERIALIZABLE")
        if type(zero) is not bool:
            raise IdkBatchError("IDK_ZERO_FLAG_INVALID")
        if not isinstance(content, str) or not content:
            raise IdkBatchError("IDK_CONTENT_REQUIRED")
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise IdkBatchError("IDK_CONTENT_JSON_INVALID") from exc
        if not isinstance(parsed, dict):
            raise IdkBatchError("IDK_CONTENT_SHAPE_INVALID")
        if self.manifest_path.exists() or self.directory.exists():
            if not self.manifest_path.exists():
                raise IdkBatchError("IDK_INCOMPLETE_BATCH_BLOCKED")
            return {"status": "reused", "manifest": self.load()}
        if _progress_has_history(progress):
            raise IdkBatchError("IDK_PROGRESS_WITHOUT_BATCH")
        content_bytes = _canonical(parsed).encode("utf-8")
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        staging = self.directory.parent / f".{self.week}.tmp-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            _atomic_json(staging / "content.json", parsed)
            manifest = {
                "schema": SCHEMA,
                "status": "sealed",
                "week": self.week,
                "observed_at": str(observed_at),
                "zero": zero,
                "logical_accounts": list(logical_accounts),
                "target_map": dict(target_map),
                "target_map_digest": _target_digest(target_map),
                "content": {"path": "content.json", "bytes": len(content_bytes), "sha256": _bytes_digest(content_bytes)},
                "evidence": dict(evidence),
            }
            manifest["metadata_seal"] = _seal(manifest)
            _atomic_json(staging / "manifest.json", manifest)
            if self.directory.exists() or _reparse(self.directory):
                raise IdkBatchError("IDK_BATCH_CREATED_CONCURRENTLY")
            staging.replace(self.directory)
            return {"status": "sealed", "manifest": self.load()}
        except Exception:
            raise

    def load(self) -> dict[str, Any]:
        if _reparse(self.directory) or not self.directory.is_dir():
            raise IdkBatchError("IDK_BATCH_DIRECTORY_INVALID")
        if {path.name for path in self.directory.iterdir()} != {"manifest.json", "content.json"}:
            raise IdkBatchError("IDK_BATCH_PATH_SET_INVALID")
        try:
            manifest = json.loads(_read(self.manifest_path, "IDK_MANIFEST_MISSING").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdkBatchError("IDK_MANIFEST_INVALID") from exc
        required = {"schema", "status", "week", "observed_at", "zero", "logical_accounts", "target_map", "target_map_digest", "content", "evidence", "metadata_seal"}
        if not isinstance(manifest, dict) or set(manifest) != required or manifest.get("schema") != SCHEMA or manifest.get("status") != "sealed":
            raise IdkBatchError("IDK_MANIFEST_SHAPE_INVALID")
        if manifest.get("week") != self.week or type(manifest.get("zero")) is not bool:
            raise IdkBatchError("IDK_SCOPE_INVALID")
        _observed_at(manifest.get("observed_at"))
        _validate_accounts(manifest["logical_accounts"], manifest["target_map"])
        if not isinstance(manifest.get("evidence"), dict):
            raise IdkBatchError("IDK_EVIDENCE_INVALID")
        if not isinstance(manifest["target_map_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["target_map_digest"]) or manifest["target_map_digest"] != _target_digest(manifest["target_map"]):
            raise IdkBatchError("IDK_TARGET_MAP_CHANGED")
        if not isinstance(manifest["metadata_seal"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["metadata_seal"]) or manifest["metadata_seal"] != _seal(manifest):
            raise IdkBatchError("IDK_METADATA_SEAL_MISMATCH")
        content = manifest["content"]
        if not isinstance(content, dict) or set(content) != {"path", "bytes", "sha256"} or content.get("path") != "content.json" or type(content.get("bytes")) is not int or content.get("bytes") <= 0 or not isinstance(content.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", content.get("sha256")):
            raise IdkBatchError("IDK_CONTENT_RECORD_INVALID")
        raw = _read(self.content_path, "IDK_CONTENT_MISSING")
        if len(raw) != content["bytes"] or _bytes_digest(raw) != content["sha256"]:
            raise IdkBatchError("IDK_CONTENT_HASH_MISMATCH")
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdkBatchError("IDK_CONTENT_JSON_INVALID") from exc
        return manifest

    def load_envelope(self) -> dict[str, Any]:
        """Return the validated manifest plus opaque parsed weekly content."""
        manifest = self.load()
        raw = _read(self.content_path, "IDK_CONTENT_MISSING")
        content_record = manifest["content"]
        if len(raw) != content_record["bytes"] or _bytes_digest(raw) != content_record["sha256"]:
            raise IdkBatchError("IDK_CONTENT_HASH_MISMATCH")
        try:
            content = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdkBatchError("IDK_CONTENT_JSON_INVALID") from exc
        return {"manifest": manifest, "content": content, "directory": self.directory}


__all__ = ["IdkBatchError", "IdkBatchStore", "batch_root"]
