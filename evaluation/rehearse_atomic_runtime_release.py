"""Verify artifact units and rehearse pointer changes without starting Hermes.

This is deliberately not a runtime rollback test.  A runtime rehearsal must
use a CURRENT-aware launcher and an offline health callback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import zipfile

from evaluation import build_atomic_runtime_release as atomic


def _read_manifest(unit: Path) -> list[tuple[str, str]]:
    manifest = unit / ".release" / "MANIFEST.sha256"
    if not manifest.is_file():
        raise RuntimeError("runtime unit manifest is missing")
    records: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise RuntimeError("runtime unit manifest is invalid")
        digest, raw_relative = match.groups()
        relative = atomic._safe_relative(raw_relative)
        normalized = relative.as_posix()
        if normalized in seen:
            raise RuntimeError("runtime unit manifest contains duplicates")
        seen.add(normalized)
        target = unit / relative
        if not target.is_file() or atomic._hash_file(target) != digest:
            raise RuntimeError(f"runtime unit hash mismatch: {normalized}")
        records.append((normalized, digest))
    actual = {
        path.relative_to(unit).as_posix()
        for path in unit.rglob("*")
        if path.is_file()
    }
    allowed = {relative for relative, _ in records} | {
        ".release/MANIFEST.sha256"
    }
    undeclared = sorted(actual - allowed)
    if undeclared:
        raise RuntimeError(
            "runtime unit contains undeclared files: " + ", ".join(undeclared)
        )
    return records


def verify_unit(unit: Path) -> dict[str, Any]:
    unit = unit.resolve()
    records = _read_manifest(unit)
    release_path = unit / ".release" / "RELEASE.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if not isinstance(release, dict):
        raise RuntimeError("runtime unit release metadata is invalid")
    if release.get("release_unit_id") != unit.name:
        raise RuntimeError("runtime unit directory identity does not match")

    payload_records = [
        (relative, digest)
        for relative, digest in records
        if relative.startswith(("health/", "hermes/", "profile/"))
    ]
    if atomic._aggregate_records(payload_records) != release.get("payload_sha256"):
        raise RuntimeError("runtime unit payload identity does not match")

    profile_release, _ = atomic._read_profile_identity(
        unit / "profile",
        require_directory_identity=False,
    )
    expected_profile = release.get("profile")
    if not isinstance(expected_profile, dict):
        raise RuntimeError("runtime unit Profile identity is missing")
    for field in atomic.PROFILE_IDENTITY_FIELDS:
        if expected_profile.get(field) != profile_release.get(field):
            raise RuntimeError(f"runtime unit Profile identity mismatch: {field}")
    hermes = release.get("hermes")
    if not isinstance(hermes, dict):
        raise RuntimeError("runtime unit Hermes identity is missing")
    atomic._bind_profile_to_hermes(profile_release, str(hermes.get("commit") or ""))

    archive = unit / "hermes" / "hermes-source.zip"
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        if "hermes_cli/__init__.py" not in names:
            raise RuntimeError("Hermes source archive is incomplete")
        for raw in names:
            relative = Path(raw)
            if (
                not raw
                or "\\" in raw
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                raise RuntimeError(f"unsafe Hermes archive path: {raw}")
    return release


def _stage(unit: Path, deployments: Path) -> Path:
    release = verify_unit(unit)
    unit_id = str(release["release_unit_id"])
    target = deployments / unit_id
    if target.exists():
        verify_unit(target)
        return target
    staging_root = deployments.parent / ".staging"
    staging_root.mkdir(exist_ok=True)
    temporary = staging_root / unit_id
    if temporary.exists():
        raise RuntimeError(f"stale runtime staging directory exists: {temporary}")
    shutil.copytree(unit, temporary)
    verify_unit(temporary)
    temporary.replace(target)
    try:
        staging_root.rmdir()
    except OSError:
        pass
    return target


def _activate(root: Path, unit: Path) -> None:
    release = verify_unit(unit)
    pointer = root / "CURRENT"
    temporary = root / ".CURRENT.tmp"
    temporary.write_text(
        str(release["release_unit_id"]) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, pointer)


def rehearse(forward: Path, rollback: Path, root: Path) -> Path:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    deployments = root / "deployments"
    deployments.mkdir()
    forward_slot = _stage(forward, deployments)
    rollback_slot = _stage(rollback, deployments)
    if forward_slot.name == rollback_slot.name:
        raise RuntimeError("rollback rehearsal requires two different release units")

    journal: list[dict[str, str]] = []
    _activate(root, forward_slot)
    journal.append({"step": "start", "release_unit_id": forward_slot.name})
    _activate(root, rollback_slot)
    journal.append({"step": "rollback", "release_unit_id": rollback_slot.name})
    _activate(root, forward_slot)
    journal.append({"step": "roll_forward", "release_unit_id": forward_slot.name})
    receipt = root / "REHEARSAL.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "artifact_pointer_passed",
                "scope": "artifact_verification_and_pointer_flip_only",
                "runtime_executed": False,
                "health_checked": False,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "journal": journal,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward", required=True, type=Path)
    parser.add_argument("--rollback", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    print(rehearse(args.forward, args.rollback, args.workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
