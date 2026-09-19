"""Atomic, profile-local slow_report batch storage.

This module owns only preparation, validation, reuse, and publication of an
already-produced report batch.  It does not choose a generation, query data,
resolve current roles, send messages, or register schedules.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Mapping


SCHEMA = "datasage-slow-report-batch/v1"
_SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,160}\.xlsx$")


class BatchError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_reparse(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _validate_part(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise BatchError(f"BATCH_{label.upper()}_INVALID")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise BatchError(f"BATCH_{label.upper()}_INVALID")
    return value


def _validate_week(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-W\d{2}", value):
        raise BatchError("BATCH_WEEK_INVALID")
    try:
        year, week = int(value[:4]), int(value[6:])
        date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise BatchError("BATCH_WEEK_INVALID") from exc
    return value


def _validate_phase(value: str) -> str:
    if value not in ("weekly", "monthly"):
        raise BatchError("BATCH_PHASE_INVALID")
    return value


def _validate_month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise BatchError("BATCH_MONTH_INVALID")
    try:
        date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise BatchError("BATCH_MONTH_INVALID") from exc
    return value


def _validate_context(week: str, phase: str, generation: int) -> None:
    _validate_week(week)
    _validate_phase(phase)
    if type(generation) is not int or not 0 <= generation <= 999:
        raise BatchError("BATCH_GENERATION_INVALID")


def batch_root(profile: Path) -> Path:
    """Return the existing profile-private root without creating a new queue."""
    from .workflow_io import private_root

    root = private_root(profile) / "slow_report_batches"
    if _is_reparse(root) or not root.resolve().is_relative_to(profile.resolve()):
        raise BatchError("BATCH_ROOT_INVALID")
    root.mkdir(parents=True, exist_ok=True)
    return root


def batch_path(root: Path, week: str, phase: str, generation: int) -> Path:
    _validate_context(week, phase, generation)
    root = Path(root)
    if _is_reparse(root):
        raise BatchError("BATCH_ROOT_INVALID")
    base = root.resolve()
    raw = base / week / phase / f"g{generation}"
    for parent in (base / week, base / week / phase, raw):
        if parent.exists() and _is_reparse(parent):
            raise BatchError("BATCH_SCOPE_SYMLINK")
    candidate = raw.resolve()
    if not candidate.is_relative_to(base):
        raise BatchError("BATCH_SCOPE_PATH_INVALID")
    return candidate


def _atomic_json(path: Path, value: Any) -> None:
    from . import operations

    path.parent.mkdir(parents=True, exist_ok=True)
    operations._atomic(path, value)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(path):
        raise BatchError("BATCH_PATH_SYMLINK")
    temp = path.with_name(path.name + ".tmp-" + hashlib.sha256(value).hexdigest()[:16])
    try:
        with temp.open("xb") as stream:
            stream.write(value)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _read_bytes(path: Path, code: str) -> bytes:
    if _is_reparse(path) or not path.is_file():
        raise BatchError(code)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BatchError(code) from exc


def _read_manifest(path: Path) -> dict[str, Any]:
    raw = _read_bytes(path, "BATCH_MANIFEST_MISSING")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchError("BATCH_MANIFEST_INVALID") from exc
    if not isinstance(value, dict):
        raise BatchError("BATCH_MANIFEST_INVALID")
    return value


def _target_digest(target_map: Mapping[str, Any]) -> str:
    return _digest(target_map)


def _manifest_seal(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "metadata_seal"}
    return _digest(payload)


def _head_seal(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "head_seal"}
    return _digest(payload)


def _validate_target_map(accounts: list[str], target_map: Mapping[str, Any]) -> None:
    if not isinstance(target_map, dict) or set(target_map) != set(accounts):
        raise BatchError("BATCH_TARGET_MAP_ACCOUNT_MISMATCH")
    for account, target in target_map.items():
        if not isinstance(account, str) or not account.strip() or not isinstance(target, dict):
            raise BatchError("BATCH_TARGET_MAP_SHAPE_INVALID")


def _attachment_bytes(value: bytes | bytearray | Path | str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    path = Path(value)
    return _read_bytes(path, "BATCH_ATTACHMENT_SOURCE_MISSING")


def _existing_manifest(root: Path, week: str, phase: str, generation: int) -> dict[str, Any] | None:
    directory = batch_path(root, week, phase, generation)
    manifest = directory / "manifest.json"
    if not directory.exists():
        return None
    if _is_reparse(directory):
        raise BatchError("BATCH_DIRECTORY_SYMLINK")
    if not manifest.exists():
        raise BatchError("BATCH_INCOMPLETE_REQUIRES_EXPLICIT_RETRY")
    return load_batch(root, week, phase, generation)


def _prior_generations(root: Path, week: str, phase: str) -> list[int]:
    parent = Path(root) / week / phase
    if not parent.exists():
        return []
    values = []
    for path in parent.iterdir():
        match = re.fullmatch(r"g(\d+)", path.name)
        if match and path.is_dir() and not _is_reparse(path):
            values.append(int(match.group(1)))
    return sorted(values)


def prepare_batch(
    root: Path,
    week: str,
    phase: str,
    generation: int,
    *,
    body: str | bytes,
    attachments: Mapping[str, bytes | bytearray | Path | str],
    logical_accounts: list[str],
    target_map: Mapping[str, Any],
    observed_at: str,
    regions: list[str],
    evidence: Mapping[str, Any] | None = None,
    regenerate: bool = False,
    reason: str | None = None,
    retry: bool = False,
    month: str | None = None,
    delivery_generation: int = 0,
) -> dict[str, Any]:
    """Atomically prepare one generation, or reuse its valid sealed manifest."""
    _validate_context(week, phase, generation)
    if not isinstance(logical_accounts, list) or len(set(logical_accounts)) != len(logical_accounts) or any(not isinstance(a, str) or not a.strip() for a in logical_accounts):
        raise BatchError("BATCH_LOGICAL_ACCOUNTS_INVALID")
    _validate_target_map(logical_accounts, target_map)
    if not isinstance(regions, list) or not regions or len(set(regions)) != len(regions) or any(not isinstance(region, str) or not region.strip() for region in regions):
        raise BatchError("BATCH_REGIONS_INVALID")
    try:
        datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BatchError("BATCH_OBSERVED_AT_INVALID") from exc
    if not isinstance(attachments, Mapping) or not attachments:
        raise BatchError("BATCH_ATTACHMENTS_REQUIRED")
    if isinstance(evidence, Mapping) and evidence.get("delivery_state") in {"sent", "provider_accepted", "unknown", "in_flight"}:
        raise BatchError("BATCH_DELIVERY_STATE_REQUIRES_ARCHIVE")
    for name in attachments:
        if not isinstance(name, str) or not _SAFE_FILE.fullmatch(name):
            raise BatchError("BATCH_ATTACHMENT_NAME_INVALID")
    if regenerate and (not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 120):
        raise BatchError("BATCH_REGENERATE_REASON_REQUIRED")
    if month is not None:
        _validate_month(month)
    if type(delivery_generation) is not int or not 0 <= delivery_generation <= 999:
        raise BatchError("BATCH_DELIVERY_GENERATION_INVALID")
    prior_generations = _prior_generations(root, week, phase)
    if prior_generations and generation > max(prior_generations) and not regenerate:
        raise BatchError("BATCH_REGENERATE_REASON_REQUIRED")
    if regenerate and prior_generations and generation <= max(prior_generations):
        raise BatchError("BATCH_GENERATION_MUST_INCREASE")
    directory = batch_path(root, week, phase, generation)
    directory_exists = directory.exists()
    if directory_exists and not (directory / "manifest.json").exists():
        raise BatchError("BATCH_INCOMPLETE_REQUIRES_EXPLICIT_RETRY")
    existing = _existing_manifest(root, week, phase, generation)
    if existing is not None:
        if regenerate:
            raise BatchError("BATCH_REGENERATE_REQUIRES_NEW_GENERATION")
        return {"status": "reused", "manifest": existing}
    if directory.exists():
        raise BatchError("BATCH_DIRECTORY_INCOMPLETE")
    body_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    attachment_values = {name: _attachment_bytes(value) for name, value in attachments.items()}
    directory.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(directory.parent) or not directory.parent.resolve().is_relative_to(Path(root).resolve()):
        raise BatchError("BATCH_DIRECTORY_INVALID")
    staging = directory.parent / f".{directory.name}.tmp-{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    attachments_dir = staging / "attachments"
    attachments_dir.mkdir()
    try:
        _atomic_bytes(staging / "body.txt", body_bytes)
        attachment_records = []
        for name, value in sorted(attachment_values.items()):
            path = attachments_dir / name
            _atomic_bytes(path, value)
            attachment_records.append({"name": name, "path": f"attachments/{name}", "bytes": len(value), "sha256": _bytes_digest(value)})
        manifest = {
            "schema": SCHEMA,
            "status": "prepared",
            "week": week,
            "phase": phase,
            "generation": generation,
            "content_generation": generation,
            "delivery_generation": delivery_generation,
            "month": month,
            "observed_at": str(observed_at),
            "regions": list(regions),
            "logical_accounts": list(logical_accounts),
            "target_map": dict(target_map),
            "target_map_digest": _target_digest(target_map),
            "body": {"path": "body.txt", "bytes": len(body_bytes), "sha256": _bytes_digest(body_bytes)},
            "attachments": attachment_records,
            "evidence": dict(evidence or {}),
        }
        if regenerate:
            manifest["regeneration_reason"] = reason.strip()
        manifest["metadata_seal"] = _manifest_seal(manifest)
        _atomic_json(staging / "manifest.json", manifest)
        if directory.exists() or _is_reparse(directory):
            raise BatchError("BATCH_DIRECTORY_CREATED_CONCURRENTLY")
        staging.replace(directory)
        return {"status": "prepared", "manifest": load_batch(root, week, phase, generation)}
    except Exception:
        # Leave the uniquely named staging directory for operator inspection;
        # another retry writes a different staging directory and never removes
        # evidence from a failed attempt.
        raise


def load_batch(root: Path, week: str, phase: str, generation: int) -> dict[str, Any]:
    """Strictly load a prepared/published batch and verify every sealed byte."""
    directory = batch_path(root, week, phase, generation)
    if directory.is_symlink() or not directory.is_dir():
        raise BatchError("BATCH_DIRECTORY_INVALID")
    manifest = _read_manifest(directory / "manifest.json")
    required = {"schema", "status", "week", "phase", "generation", "content_generation", "delivery_generation", "month", "observed_at", "regions", "logical_accounts", "target_map", "target_map_digest", "body", "attachments", "evidence", "metadata_seal"}
    allowed = required | {"published_at", "regeneration_reason"}
    if set(manifest) - allowed or set(manifest) < required or manifest.get("schema") != SCHEMA or manifest.get("status") not in ("prepared", "published"):
        raise BatchError("BATCH_MANIFEST_SHAPE_INVALID")
    if (manifest.get("week"), manifest.get("phase"), manifest.get("generation")) != (week, phase, generation):
        raise BatchError("BATCH_SCOPE_MISMATCH")
    if manifest.get("content_generation") != generation or type(manifest.get("delivery_generation")) is not int or manifest.get("delivery_generation") < 0:
        raise BatchError("BATCH_GENERATION_SHAPE_INVALID")
    if not isinstance(manifest.get("metadata_seal"), str) or manifest["metadata_seal"] != _manifest_seal(manifest):
        raise BatchError("BATCH_METADATA_SEAL_MISMATCH")
    accounts = manifest.get("logical_accounts")
    if not isinstance(accounts, list) or len(set(accounts)) != len(accounts) or any(not isinstance(account, str) or not account.strip() for account in accounts):
        raise BatchError("BATCH_LOGICAL_ACCOUNTS_INVALID")
    if not isinstance(manifest.get("regions"), list) or not manifest["regions"] or len(set(manifest["regions"])) != len(manifest["regions"]):
        raise BatchError("BATCH_REGIONS_INVALID")
    try:
        datetime.fromisoformat(str(manifest["observed_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BatchError("BATCH_OBSERVED_AT_INVALID") from exc
    _validate_target_map(accounts, manifest.get("target_map"))
    if manifest.get("target_map_digest") != _target_digest(manifest["target_map"]):
        raise BatchError("BATCH_TARGET_MAP_CHANGED")
    body = manifest.get("body")
    if not isinstance(body, dict) or body.get("path") != "body.txt":
        raise BatchError("BATCH_BODY_SHAPE_INVALID")
    body_bytes = _read_bytes(directory / "body.txt", "BATCH_BODY_MISSING")
    if body.get("bytes") != len(body_bytes) or body.get("sha256") != _bytes_digest(body_bytes):
        raise BatchError("BATCH_BODY_HASH_MISMATCH")
    attachments = manifest.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        raise BatchError("BATCH_ATTACHMENTS_INVALID")
    expected_paths = {"manifest.json", "body.txt", "attachments"}
    actual_top = {path.name for path in directory.iterdir()}
    if actual_top != expected_paths:
        raise BatchError("BATCH_UNEXPECTED_PATH")
    attachment_dir = directory / "attachments"
    if attachment_dir.is_symlink() or not attachment_dir.is_dir():
        raise BatchError("BATCH_ATTACHMENT_DIRECTORY_INVALID")
    seen = set()
    for record in attachments:
        if not isinstance(record, dict) or set(record) != {"name", "path", "bytes", "sha256"}:
            raise BatchError("BATCH_ATTACHMENT_RECORD_INVALID")
        name = record["name"]
        if name in seen or not isinstance(name, str) or not _SAFE_FILE.fullmatch(name) or record["path"] != f"attachments/{name}":
            raise BatchError("BATCH_ATTACHMENT_RECORD_INVALID")
        seen.add(name)
        value = _read_bytes(directory / record["path"], "BATCH_ATTACHMENT_MISSING")
        if record["bytes"] != len(value) or record["sha256"] != _bytes_digest(value):
            raise BatchError("BATCH_ATTACHMENT_HASH_MISMATCH")
    if {path.name for path in attachment_dir.iterdir()} != seen:
        raise BatchError("BATCH_ATTACHMENT_SET_MISMATCH")
    return manifest


def publish_batch(root: Path, week: str, phase: str, generation: int, *, published_at: str | None = None) -> dict[str, Any]:
    """Mark a complete prepared batch published; never publish incomplete data."""
    manifest = load_batch(root, week, phase, generation)
    if manifest["status"] == "published":
        return {"status": "already_published", "manifest": manifest}
    if manifest["status"] != "prepared":
        raise BatchError("BATCH_NOT_PREPARED")
    value = dict(manifest)
    value["status"] = "published"
    value["published_at"] = published_at or manifest["observed_at"]
    value["metadata_seal"] = _manifest_seal(value)
    _atomic_json(batch_path(root, week, phase, generation) / "manifest.json", value)
    return {"status": "published", "manifest": load_batch(root, week, phase, generation)}


class BatchStore:
    """Week-scoped head plus immutable weekly/monthly phase batches.

    ``root`` is the existing profile-private slow-report-batches root.  The
    caller owns phase selection and send/complete gates; this class only
    persists the explicit head and delegates byte sealing to the pure helpers.
    """

    def __init__(self, root: Path, week: str):
        self.root = Path(root)
        _validate_week(week)
        if self.root.is_symlink():
            raise BatchError("BATCH_ROOT_INVALID")
        self.week = week
        self.week_root = self.root / week
        self.head_path = self.week_root / "head.json"

    def head(self) -> dict[str, Any] | None:
        if not self.head_path.exists():
            return None
        value = _read_manifest(self.head_path)
        required = {"schema", "week", "month", "content_generation", "delivery_generation", "phases", "head_seal"}
        allowed = required | {"content_generation_reason"}
        if set(value) - allowed or set(value) < required or value.get("schema") != "datasage-slow-report-head/v1" or value.get("week") != self.week:
            raise BatchError("BATCH_HEAD_INVALID")
        if type(value.get("content_generation")) is not int or value["content_generation"] < 0 or type(value.get("delivery_generation")) is not int or value["delivery_generation"] < 0:
            raise BatchError("BATCH_HEAD_GENERATION_INVALID")
        if not isinstance(value.get("phases"), dict):
            raise BatchError("BATCH_HEAD_PHASES_INVALID")
        if value.get("head_seal") != _head_seal(value):
            raise BatchError("BATCH_HEAD_SEAL_MISMATCH")
        return value

    def _save_head(self, value: Mapping[str, Any]) -> None:
        sealed = dict(value)
        sealed["head_seal"] = _head_seal(sealed)
        _atomic_json(self.head_path, sealed)

    def ensure_head(self, *, month: str, observed_at: str, content_generation: int = 0, delivery_generation: int = 0) -> dict[str, Any]:
        _validate_month(month)
        if type(content_generation) is not int or not 0 <= content_generation <= 999:
            raise BatchError("BATCH_GENERATION_INVALID")
        if type(delivery_generation) is not int or delivery_generation < 0:
            raise BatchError("BATCH_DELIVERY_GENERATION_INVALID")
        try:
            datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise BatchError("BATCH_OBSERVED_AT_INVALID") from exc
        current = self.head()
        if current is not None:
            if current["month"] != month:
                raise BatchError("BATCH_HEAD_MONTH_MISMATCH")
            return current
        value = {
            "schema": "datasage-slow-report-head/v1",
            "week": self.week,
            "month": month,
            "content_generation": content_generation,
            "delivery_generation": delivery_generation,
            "phases": {},
        }
        self._save_head(value)
        return self.head() or value

    def new_content_generation(self, *, month: str, reason: str) -> dict[str, Any]:
        current = self.head()
        if current is None:
            raise BatchError("BATCH_HEAD_REQUIRED")
        if current["month"] != month:
            raise BatchError("BATCH_HEAD_MONTH_MISMATCH")
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 120:
            raise BatchError("BATCH_REGENERATE_REASON_REQUIRED")
        value = dict(current)
        value["content_generation"] += 1
        value["content_generation_reason"] = reason.strip()
        value["phases"] = {}
        self._save_head(value)
        return self.head() or value

    def new_delivery_generation(self, *, reason: str) -> dict[str, Any]:
        current = self.head()
        if current is None:
            raise BatchError("BATCH_HEAD_REQUIRED")
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 120:
            raise BatchError("BATCH_RESEND_REASON_REQUIRED")
        value = dict(current)
        value["delivery_generation"] += 1
        self._save_head(value)
        return self.head() or value

    def load_phase(self, phase: str, *, content_generation: int | None = None) -> dict[str, Any]:
        current = self.head()
        if current is None:
            raise BatchError("BATCH_HEAD_REQUIRED")
        generation = current["content_generation"] if content_generation is None else content_generation
        manifest = load_batch(self.root, self.week, phase, generation)
        phase_record = (current.get("phases") or {}).get(phase)
        if phase_record is not None:
            if not isinstance(phase_record, dict) or phase_record.get("content_generation") != generation:
                raise BatchError("BATCH_HEAD_PHASE_BINDING_MISMATCH")
            if phase_record.get("metadata_seal") != manifest.get("metadata_seal"):
                # A publish can seal the manifest and fail on the subsequent
                # head update.  Permit only that exact prepared->published
                # transition; any body/target/evidence mutation remains sealed
                # as a hard mismatch.
                prepared = dict(manifest)
                prepared["status"] = "prepared"
                prepared.pop("published_at", None)
                prepared["metadata_seal"] = _manifest_seal(prepared)
                if not (manifest.get("status") == "published" and phase_record.get("status") == "prepared" and phase_record.get("metadata_seal") == prepared["metadata_seal"]):
                    raise BatchError("BATCH_HEAD_PHASE_BINDING_MISMATCH")
        return manifest

    def _record_phase(self, phase: str, manifest: Mapping[str, Any]) -> None:
        current = self.head()
        if current is None:
            raise BatchError("BATCH_HEAD_REQUIRED")
        value = dict(current)
        phases = dict(value.get("phases") or {})
        phases[phase] = {"content_generation": manifest["content_generation"], "status": manifest["status"], "metadata_seal": manifest["metadata_seal"]}
        value["phases"] = phases
        self._save_head(value)

    def prepare_phase(self, phase: str, *, content_generation: int | None = None, **kwargs: Any) -> dict[str, Any]:
        current = self.head()
        if current is None:
            raise BatchError("BATCH_HEAD_REQUIRED")
        generation = current["content_generation"] if content_generation is None else content_generation
        kwargs.setdefault("month", current["month"])
        kwargs.setdefault("delivery_generation", current["delivery_generation"])
        if generation > 0:
            kwargs.setdefault("regenerate", True)
            if "reason" not in kwargs:
                reason = current.get("content_generation_reason")
                if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 120:
                    raise BatchError("BATCH_REGENERATE_REASON_REQUIRED")
                kwargs["reason"] = reason
        result = prepare_batch(self.root, self.week, phase, generation, **kwargs)
        self._record_phase(phase, result["manifest"])
        return result

    def publish_phase(self, phase: str, *, content_generation: int | None = None, published_at: str | None = None) -> dict[str, Any]:
        current = self.head()
        if current is None:
            raise BatchError("BATCH_HEAD_REQUIRED")
        generation = current["content_generation"] if content_generation is None else content_generation
        result = publish_batch(self.root, self.week, phase, generation, published_at=published_at)
        self._record_phase(phase, result["manifest"])
        return result


__all__ = ["BatchError", "BatchStore", "batch_root", "batch_path", "prepare_batch", "load_batch", "publish_batch"]
