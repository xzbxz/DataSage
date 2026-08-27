"""Bind DataSage quality-gate evidence to one reviewed runtime payload.

Git commit/tag and the Hermes Profile Distribution manifest are authoritative
for release and installation identity.  This source-only command computes a
deterministic checksum manifest so DataSage-specific replay, compaction,
delivery, and performance evidence cannot be applied to a different payload.
It is neither an installer nor a replacement for ``hermes profile``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable

import yaml


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "distribution.yaml"
RELEASE_DIR = ROOT / "release"
PENDING_DIR = ROOT / "pending"
GOLDEN_SUITE = ROOT / "plugins" / "datasage-query" / "e2e" / "golden_expert_cases.json"
HOST_COMPACTION_FIXTURE = ROOT / "tests" / "fixtures" / "host_compaction_ordering.json"
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
INSTALLER_MANIFEST_FIELDS = {"name", "source", "installed_at"}
IDENTITY_MISMATCH_EXIT = 2
LIVE_GATE_BLOCKED_EXIT = 3
CANDIDATE_RECEIPT_GLOB = "*-candidate-receipt*.json"
FINAL_RECEIPT_GLOB = "*-release-receipt.json"


def _canonical_manifest_bytes(payload: object) -> bytes:
    """Return the release-owned part of a Hermes distribution manifest.

    Hermes legitimately rewrites ``name`` and adds ``source`` / ``installed_at``
    during installation.  Those fields identify an installation, not the
    reviewed distribution payload, so they cannot participate in the payload
    identity used to compare source and installed profiles.
    """

    if not isinstance(payload, dict):
        raise ValueError("distribution.yaml must contain a mapping")
    release_payload = {
        key: value
        for key, value in payload.items()
        if key not in INSTALLER_MANIFEST_FIELDS
    }
    rendered = yaml.safe_dump(
        release_payload,
        allow_unicode=True,
        sort_keys=True,
        default_flow_style=False,
    )
    return rendered.encode("utf-8")


def _release_content(path: Path) -> tuple[bytes, str | None]:
    if path == MANIFEST:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return _canonical_manifest_bytes(payload), "hermes-install-metadata/v1"
    return path.read_bytes(), None


def _owned_paths() -> list[str]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    owned = manifest.get("distribution_owned")
    if not isinstance(owned, list) or not all(isinstance(item, str) for item in owned):
        raise ValueError("distribution_owned must be a list of relative paths")
    return owned


def _iter_files(entries: Iterable[str]) -> list[Path]:
    files: set[Path] = {MANIFEST}
    for entry in entries:
        candidate = (ROOT / entry).resolve()
        if ROOT not in candidate.parents and candidate != ROOT:
            raise ValueError(f"distribution path escapes root: {entry}")
        if not candidate.exists():
            raise FileNotFoundError(f"distribution-owned path is missing: {entry}")
        discovered = [candidate] if candidate.is_file() else list(candidate.rglob("*"))
        for path in discovered:
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if EXCLUDED_PARTS.intersection(relative.parts) or path.suffix in EXCLUDED_SUFFIXES:
                continue
            files.add(path)
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def build_receipt() -> dict[str, object]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    file_records = []
    aggregate = hashlib.sha256()
    for path in _iter_files(_owned_paths()):
        relative = path.relative_to(ROOT).as_posix()
        content, normalization = _release_content(path)
        digest = hashlib.sha256(content).hexdigest()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
        record = {"path": relative, "sha256": digest, "bytes": len(content)}
        if normalization:
            record["normalization"] = normalization
        file_records.append(record)
    return {
        "schema": "datasage-release-receipt/v2",
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "content_sha256": aggregate.hexdigest(),
        "file_count": len(file_records),
        "files": file_records,
        "identity_scope": "quality-gate-subject/distribution-owned-runtime-content/v1",
        "excludes_runtime_state": True,
    }


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON document must contain an object: {path}")
    return payload


def _receipt_location(path: Path, *, kind: str) -> tuple[Path | None, str | None]:
    """Resolve an explicit receipt and enforce candidate/final separation."""

    if kind not in {"candidate", "final"}:
        raise ValueError(f"unknown receipt kind: {kind}")
    candidate = path if path.is_absolute() else ROOT / path
    candidate = candidate.resolve()
    expected_dir = (PENDING_DIR if kind == "candidate" else RELEASE_DIR).resolve()
    expected_glob = CANDIDATE_RECEIPT_GLOB if kind == "candidate" else FINAL_RECEIPT_GLOB
    if candidate.parent != expected_dir or not candidate.match(expected_glob):
        return None, f"{kind.upper()}_RECEIPT_PATH_INVALID"
    return candidate, None


def _stored_receipt(
    version: str,
    *,
    kind: str,
    receipt_path: Path | None = None,
    expected_content_sha256: str | None = None,
) -> tuple[dict[str, object] | None, Path | None, str | None]:
    """Return one stored receipt without crossing candidate/final namespaces.

    Candidate discovery is content-addressed.  Stale candidate receipts for the
    same version may remain immutable in ``pending/``; only a unique receipt
    whose payload hash matches the current runtime distribution is selected.
    """

    if kind not in {"candidate", "final"}:
        raise ValueError(f"unknown receipt kind: {kind}")
    if receipt_path is not None:
        resolved, location_error = _receipt_location(receipt_path, kind=kind)
        if location_error:
            return None, None, location_error
        candidates = [resolved]
    else:
        receipt_dir = PENDING_DIR if kind == "candidate" else RELEASE_DIR
        receipt_glob = CANDIDATE_RECEIPT_GLOB if kind == "candidate" else FINAL_RECEIPT_GLOB
        candidates = sorted(receipt_dir.glob(receipt_glob))
    matches: list[tuple[dict[str, object], Path]] = []
    for path in candidates:
        if path is None:
            continue
        try:
            payload = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if str(payload.get("version")) == version:
            matches.append((payload, path))
    if not matches:
        return None, None, f"{kind.upper()}_RECEIPT_MISSING"
    if kind == "candidate" and receipt_path is None:
        content_matches = [
            item
            for item in matches
            if item[0].get("content_sha256") == expected_content_sha256
        ]
        if not content_matches:
            return None, None, "CANDIDATE_RECEIPT_FOR_CURRENT_CONTENT_MISSING"
        matches = content_matches
    if len(matches) != 1:
        return None, None, f"{kind.upper()}_RECEIPT_AMBIGUOUS"
    payload, path = matches[0]
    return payload, path, None


def compare_release_identity(
    actual: dict[str, object],
    stored: dict[str, object] | None,
    *,
    receipt_path: Path | None = None,
    discovery_error: str | None = None,
) -> dict[str, object]:
    """Compare a computed receipt with one immutable stored receipt."""

    if stored is None:
        return {
            "status": "mismatch",
            "reason_code": discovery_error or "RELEASE_RECEIPT_MISSING",
            "receipt": None,
            "expected_content_sha256": None,
            "actual_content_sha256": actual.get("content_sha256"),
        }
    compared_fields = ("schema", "name", "version", "content_sha256", "file_count", "files")
    mismatches = [field for field in compared_fields if stored.get(field) != actual.get(field)]
    return {
        "status": "verified" if not mismatches else "mismatch",
        "reason_code": None if not mismatches else "RELEASE_IDENTITY_MISMATCH",
        "receipt": (
            receipt_path.relative_to(ROOT).as_posix()
            if receipt_path is not None and receipt_path.is_relative_to(ROOT)
            else str(receipt_path) if receipt_path is not None else None
        ),
        "mismatched_fields": mismatches,
        "expected_content_sha256": stored.get("content_sha256"),
        "actual_content_sha256": actual.get("content_sha256"),
    }


def check_release_identity(
    *,
    kind: str = "final",
    receipt_path: Path | None = None,
) -> dict[str, object]:
    """Check the quality-gate subject against a candidate or final receipt."""

    actual = build_receipt()
    version = str(actual.get("version"))
    stored, resolved_path, discovery_error = _stored_receipt(
        version,
        kind=kind,
        receipt_path=receipt_path,
        expected_content_sha256=str(actual.get("content_sha256")),
    )
    return compare_release_identity(
        actual,
        stored,
        receipt_path=resolved_path,
        discovery_error=discovery_error,
    )


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def evaluate_release_gates(
    release_validation: dict[str, object],
    host_fixture: dict[str, object],
    *,
    version: str,
) -> dict[str, object]:
    """Evaluate machine-readable live, host, stability, and performance evidence."""

    blockers: list[dict[str, str]] = []
    live_status = str(release_validation.get("live_model_replay_status", "missing"))
    release_gate_status = str(release_validation.get("release_gate_status", "missing"))
    target = str(release_validation.get("release_target", "missing"))
    trusted = release_validation.get("trusted_replay_gate")
    trusted = trusted if isinstance(trusted, dict) else {}
    required_runs = trusted.get("required_replay_runs_per_case")
    case_ids = trusted.get("case_ids")
    completed_runs = trusted.get("completed_replay_runs_per_case")
    case_ids = case_ids if isinstance(case_ids, list) else []
    completed_runs = completed_runs if isinstance(completed_runs, dict) else {}

    if target != version:
        blockers.append(_blocker("LIVE_REPLAY_TARGET_MISMATCH", "Live replay evidence does not target the candidate version."))
    if live_status not in {"passed", "verified"}:
        blockers.append(_blocker("LIVE_MODEL_REPLAY_NOT_VERIFIED", "The candidate has no verified live-model replay."))
    if release_gate_status not in {"passed", "eligible"}:
        blockers.append(_blocker("LIVE_RELEASE_GATE_BLOCKED", "The Golden manifest still blocks release."))
    if trusted.get("outbound_delivery_verification_status") not in {"passed", "verified"}:
        blockers.append(_blocker("OUTBOUND_DELIVERY_NOT_VERIFIED", "Trusted outbound delivery is not verified."))
    if trusted.get("stability_status") != "passed":
        blockers.append(_blocker("STABILITY_NOT_VERIFIED", "Required repeated live runs are not marked stable."))
    if not isinstance(required_runs, int) or required_runs < 1 or not case_ids:
        blockers.append(_blocker("LIVE_REPLAY_RUN_CONTRACT_INVALID", "Trusted replay run requirements are incomplete."))
    else:
        incomplete = [
            case_id
            for case_id in case_ids
            if not isinstance(completed_runs.get(case_id), int)
            or completed_runs.get(case_id, 0) < required_runs
        ]
        if incomplete:
            blockers.append(_blocker("LIVE_REPLAY_RUNS_INCOMPLETE", "One or more Golden cases lack the required repeated live runs."))

    distribution = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    pinned_hermes = str(distribution.get("hermes_requires", "")).removeprefix("==")
    host_verification = host_fixture.get("verification")
    host_verification = host_verification if isinstance(host_verification, dict) else {}
    if (
        host_verification.get("status") != "passed"
        or host_verification.get("required_assertions_passed") is not True
        or str(host_verification.get("hermes_version", "")) != pinned_hermes
    ):
        blockers.append(_blocker("HOST_COMPACTION_NOT_VERIFIED", "Compaction has not passed on the pinned Hermes version."))

    performance = release_validation.get("performance_cost_gate")
    performance = performance if isinstance(performance, dict) else {}
    numeric_measurements = all(
        isinstance(performance.get(field), (int, float)) and performance.get(field, 0) >= 0
        for field in ("sample_count", "p50_ms", "p90_ms")
    )
    if (
        performance.get("status") != "passed"
        or performance.get("latency_status") != "passed"
        or performance.get("cost_status") != "passed"
        or not numeric_measurements
        or performance.get("sample_count", 0) < 1
    ):
        blockers.append(_blocker("PERFORMANCE_COST_NOT_VERIFIED", "P50/P90 and cost acceptance evidence is incomplete."))

    return {
        "live_model_replay": {
            "status": live_status,
            "release_gate_status": release_gate_status,
            "target": target,
        },
        "host_compaction": {
            "status": host_verification.get("status", "missing"),
            "hermes_version": host_verification.get("hermes_version"),
            "required_assertions_passed": host_verification.get("required_assertions_passed", False),
        },
        "performance_cost": {
            "status": performance.get("status", "missing"),
            "sample_count": performance.get("sample_count"),
            "p50_ms": performance.get("p50_ms"),
            "p90_ms": performance.get("p90_ms"),
            "latency_status": performance.get("latency_status", "missing"),
            "cost_status": performance.get("cost_status", "missing"),
        },
        "blockers": blockers,
    }


def _run_offline_tests() -> dict[str, object]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason_code": "OFFLINE_TEST_TIMEOUT", "test_count": None}
    combined = f"{completed.stdout}\n{completed.stderr}"
    matched = re.search(r"Ran\s+(\d+)\s+tests?", combined)
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "reason_code": None if completed.returncode == 0 else "OFFLINE_TEST_FAILED",
        "test_count": int(matched.group(1)) if matched else None,
    }


def verify_candidate(*, receipt_path: Path | None = None, offline_runner=None) -> dict[str, object]:
    """Return one answer about candidate identity and release eligibility."""

    actual = build_receipt()
    identity = check_release_identity(kind="candidate", receipt_path=receipt_path)
    offline = (offline_runner or _run_offline_tests)()
    release_validation = _read_json(GOLDEN_SUITE).get("release_validation")
    if not isinstance(release_validation, dict):
        raise ValueError("golden_expert_cases.json is missing release_validation")
    host_fixture = _read_json(HOST_COMPACTION_FIXTURE)
    gates = evaluate_release_gates(
        release_validation,
        host_fixture,
        version=str(actual.get("version")),
    )
    blockers = list(gates.pop("blockers"))
    if identity["status"] != "verified":
        blockers.insert(
            0,
            _blocker(
                str(identity.get("reason_code") or "RELEASE_IDENTITY_MISMATCH"),
                "The current runtime payload is not bound to one immutable candidate receipt.",
            ),
        )
    if offline.get("status") != "passed":
        blockers.append(_blocker(str(offline.get("reason_code") or "OFFLINE_TEST_FAILED"), "The complete offline test suite did not pass."))
    return {
        "schema": "datasage-release-eligibility/v1",
        "name": actual.get("name"),
        "version": actual.get("version"),
        "eligible": not blockers,
        "identity": identity,
        "offline_tests": offline,
        **gates,
        "blockers": blockers,
        "interpretation": (
            "release_eligible"
            if not blockers
            else "offline tests may pass while live release gates remain blocked"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare current content with the immutable final receipt in release/")
    mode.add_argument("--verify-candidate", action="store_true", help="run the complete release-eligibility gate")
    parser.add_argument(
        "--receipt",
        type=Path,
        help=(
            "explicit receipt; --verify-candidate accepts only pending/"
            "*-candidate-receipt*.json and --check accepts only release/"
            "*-release-receipt.json"
        ),
    )
    args = parser.parse_args()
    if args.output and (args.check or args.verify_candidate or args.receipt):
        parser.error("--output cannot be combined with verification options")
    if args.receipt and not (args.check or args.verify_candidate):
        parser.error("--receipt requires --check or --verify-candidate")
    if args.check:
        result = check_release_identity(kind="final", receipt_path=args.receipt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "verified" else IDENTITY_MISMATCH_EXIT
    if args.verify_candidate:
        result = verify_candidate(receipt_path=args.receipt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["eligible"]:
            return 0
        if result["identity"]["status"] != "verified":
            return IDENTITY_MISMATCH_EXIT
        return LIVE_GATE_BLOCKED_EXIT
    rendered = json.dumps(build_receipt(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output, location_error = _receipt_location(args.output, kind="candidate")
        if location_error or output is None:
            parser.error(
                "--output must be a pending/*-candidate-receipt*.json path; "
                "final receipts are promoted only after all quality gates pass"
            )
        if output.exists():
            parser.error("candidate receipts are immutable; choose a new --output path")
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
