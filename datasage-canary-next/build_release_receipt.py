"""Bind DataSage quality-gate evidence to one reviewed runtime payload.

Git commit/tag and the Hermes Profile Distribution manifest are authoritative
for release and installation identity.  This source-only command computes a
deterministic checksum manifest so DataSage-specific replay, compaction,
delivery, and performance evidence cannot be applied to a different payload.
It is neither an installer nor a replacement for ``hermes profile``.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
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
PERFORMANCE_CONTRACT = ROOT / "tests" / "fixtures" / "performance_non_db_contract.json"
EVIDENCE_SCHEMA = ROOT / "tests" / "contracts" / "release_evidence.schema.json"
EVIDENCE_DIR = ROOT / "pending" / "evidence"
HOST_EVIDENCE_SCHEMA = "datasage-host-compaction-evidence/v1"
PERFORMANCE_EVIDENCE_SCHEMA = "datasage-performance-evidence/v1"
PERFORMANCE_CONTRACT_SCHEMA = "datasage-performance-non-db-contract/v1"
HOST_PRODUCER_PATH = "tests/test_host_compaction_e2e.py"
PERFORMANCE_PRODUCER_PATH = "tests/run_performance_evidence.py"
SYSTEM_PROMPT_PATH = "SOUL.md"
HOST_FIXTURE_PATH = "tests/fixtures/host_compaction_ordering.json"
PERFORMANCE_CONTRACT_PATH = "tests/fixtures/performance_non_db_contract.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON property: {key}")
        payload[key] = value
    return payload


def _read_strict_json(path: Path) -> dict[str, object]:
    """Read evidence JSON without accepting duplicate keys or NaN/Infinity."""
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_json_constant)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON document must contain an object: {path}")
    return payload


def _canonical_json_bytes(payload: object) -> bytes:
    """Canonical bytes for hashes already defined by the local evidence contract."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _require_exact_keys(payload: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    actual = set(payload)
    if actual != keys:
        raise ValueError(f"{label} keys mismatch; missing={sorted(keys - actual)!r}; unknown={sorted(actual - keys)!r}")
    return payload


def _require_string(value: object, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _require_sha256(value: object, label: str) -> str:
    value = _require_string(value, label)
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_git_commit(value: object, label: str) -> str:
    value = _require_string(value, label)
    if not GIT_COMMIT_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase full Git commit")
    return value


def _profile_path(relative: object, label: str) -> Path:
    text = _require_string(relative, label)
    if "\\" in text or Path(text).is_absolute():
        raise ValueError(f"{label} must be a Profile-relative POSIX path")
    unresolved = ROOT / text
    if unresolved.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    candidate = unresolved.resolve()
    root = ROOT.resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise ValueError(f"{label} does not exist")
    return candidate


def _git_output(root: Path, *args: str, binary: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={root.resolve().as_posix()}", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=not binary,
        timeout=10,
        check=False,
    )
    if completed.returncode:
        raise ValueError(f"Git attribution failed for {root}: {' '.join(args)}")
    return completed.stdout


def _profile_git_root() -> Path:
    return Path(str(_git_output(ROOT, "rev-parse", "--show-toplevel")).strip()).resolve()


def _validate_hashed_source(
    payload: object,
    *,
    label: str,
    expected_path: str,
    subject_commit: str,
) -> Path:
    """Bind a source hash to a tracked blob at the evidence subject commit."""

    source = _require_exact_keys(payload, {"path", "sha256"}, label)
    if source["path"] != expected_path:
        raise ValueError(f"{label}.path must be {expected_path!r}")
    path = _profile_path(source["path"], f"{label}.path")
    expected_sha = _require_sha256(source["sha256"], f"{label}.sha256")
    commit = _require_git_commit(subject_commit, "subject.profile_git_commit")
    git_root = _profile_git_root()
    repository_path = (ROOT.relative_to(git_root) / expected_path).as_posix()
    blob = _git_output(git_root, "show", f"{commit}:{repository_path}", binary=True)
    if not isinstance(blob, bytes) or _sha256_bytes(blob) != expected_sha:
        raise ValueError(f"{label}.sha256 does not match the tracked subject-commit blob")
    if path.read_bytes() != blob:
        raise ValueError(f"{label} worktree content differs from the subject-commit blob")
    return path


def _git_commit(path: Path) -> str:
    commit = str(_git_output(path, "rev-parse", "HEAD")).strip().lower()
    if not GIT_COMMIT_RE.fullmatch(commit):
        raise ValueError(f"cannot resolve Git commit for {path}")
    return commit


def _git_worktree_clean(root: Path, scope: Path | None = None) -> bool:
    args = ["status", "--porcelain=v1", "--untracked-files=all"]
    if scope is not None:
        args.extend(["--", scope.resolve().relative_to(root.resolve()).as_posix()])
    return not str(_git_output(root, *args)).strip()


def _hermes_source_root() -> Path:
    override = os.environ.get("HERMES_AGENT_ROOT")
    if override:
        return Path(override).resolve()
    executable = Path(sys.executable).resolve()
    for candidate in executable.parents:
        if (candidate / "agent" / "context_compressor.py").is_file():
            return candidate
    raise ValueError("cannot locate the pinned Hermes source checkout")


def _evidence_path(kind: str, profile_git_commit: str) -> Path:
    prefixes = {"host": "host-compaction", "performance": "performance"}
    if kind not in prefixes:
        raise ValueError(f"unknown evidence kind: {kind}")
    return EVIDENCE_DIR / f"{prefixes[kind]}-{profile_git_commit}.json"


def _discover_evidence(kind: str, profile_git_commit: str) -> dict[str, object] | None:
    path = _evidence_path(kind, profile_git_commit)
    if not path.exists():
        return None
    if path.resolve().parent != EVIDENCE_DIR.resolve() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{kind} evidence path is not a regular direct child of pending/evidence")
    return _read_strict_json(path)


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


def _validate_subject(payload: object, expected: dict[str, str]) -> None:
    subject = _require_exact_keys(
        payload,
        {"name", "version", "content_sha256", "profile_git_commit"},
        "subject",
    )
    _require_sha256(subject["content_sha256"], "subject.content_sha256")
    _require_git_commit(subject["profile_git_commit"], "subject.profile_git_commit")
    for key, value in expected.items():
        if subject.get(key) != value:
            raise ValueError(f"subject.{key} does not match the current candidate")


def _validate_host(payload: object, *, pinned_version: str, expected_commit: str) -> None:
    host = _require_exact_keys(payload, {"hermes_version", "hermes_git_commit"}, "host")
    _require_git_commit(host["hermes_git_commit"], "host.hermes_git_commit")
    if host["hermes_version"] != pinned_version:
        raise ValueError("host.hermes_version does not match distribution.yaml")
    if host["hermes_git_commit"] != expected_commit:
        raise ValueError("host.hermes_git_commit does not match the official host under test")


def _host_pins_match(
    fixture: dict[str, object],
    contract: dict[str, object] | None,
    *,
    version: str,
    commit: str,
) -> bool:
    expected = {"hermes_version": version, "hermes_git_commit": commit}
    return fixture.get("pinned_host") == expected and isinstance(contract, dict) and contract.get("host") == expected


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "".join(_content_text(item) for item in content)
    return ""


def _derive_host_assertions(fixture: dict[str, object], captured_request: object) -> None:
    request = captured_request if isinstance(captured_request, dict) else None
    messages = request.get("messages") if request else None
    if not isinstance(messages, list) or not messages or not all(isinstance(item, dict) for item in messages):
        raise ValueError("captured_request.messages must be a non-empty object list")
    latest = _require_string(fixture.get("latest_user_message"), "fixture.latest_user_message")
    expected_latest = _require_string(fixture.get("expected_authoritative_latest_user_message"), "fixture.expected_authoritative_latest_user_message")
    stale = fixture.get("stale_reference_fragments")
    if not isinstance(stale, list) or not stale or not all(isinstance(item, str) and item for item in stale):
        raise ValueError("fixture.stale_reference_fragments is invalid")
    required = fixture.get("required_host_assertions")
    expected_ids = [
        "compaction_summary_is_before_latest_user_message",
        "latest_user_message_hash_is_preserved_exactly",
        "no_summary_or_old_task_is_appended_as_a_user_message_after_latest_user_message",
        "model_input_authority_marks_latest_user_message_above_summary_draft_and_prior_correction",
    ]
    if required != expected_ids:
        raise ValueError("fixture.required_host_assertions does not match the pinned contract")

    rendered = [_content_text(message.get("content")) for message in messages]
    user_indices = [index for index, message in enumerate(messages) if message.get("role") == "user"]
    latest_indices = [index for index in user_indices if rendered[index] == latest]
    summary_marker = "[CONTEXT COMPACTION — REFERENCE ONLY]"
    summaries = [index for index, content in enumerate(rendered) if summary_marker in content]
    if len(latest_indices) != 1 or not summaries:
        raise ValueError("captured request lacks one exact latest user message or compaction summary")
    latest_index = latest_indices[0]
    if latest_index != max(user_indices) or any(index >= latest_index for index in summaries):
        raise ValueError("compaction summary/latest-user ordering is invalid")
    if latest != expected_latest or _sha256_bytes(rendered[latest_index].encode("utf-8")) != _sha256_bytes(expected_latest.encode("utf-8")):
        raise ValueError("latest user message was not preserved exactly")
    trailing_users = [index for index in user_indices if index > latest_index]
    if trailing_users:
        raise ValueError("a user message was appended after the authoritative latest user message")
    stale_indices = [
        index for index, content in enumerate(rendered)
        if index != latest_index and any(fragment in content for fragment in stale)
    ]
    authority_phrases = (
        "Respond ONLY to the latest user message that appears AFTER this summary",
        "the latest user message WINS",
    )
    if not all(all(phrase in rendered[index] for phrase in authority_phrases) for index in summaries):
        raise ValueError("compaction summary lacks the pinned latest-user authority language")
    if any(index >= latest_index for index in stale_indices):
        raise ValueError("stale task or prior correction appears after the latest user message")


def _evaluate_host_evidence(
    evidence: dict[str, object] | None,
    fixture: dict[str, object],
    *,
    subject: dict[str, str],
    pinned_hermes: str,
    hermes_git_commit: str,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    empty = {"status": "missing", "hermes_version": None, "hermes_git_commit": None, "compression_count": None, "captured_request_sha256": None, "required_assertions_passed": False}
    if evidence is None:
        return empty, [_blocker("HOST_COMPACTION_NOT_VERIFIED", "Raw host-compaction evidence is missing.")]
    try:
        keys = {"schema", "subject", "host", "fixture", "producer", "compression_count", "captured_request", "captured_request_sha256"}
        report = _require_exact_keys(evidence, keys, "host evidence")
        if report["schema"] != HOST_EVIDENCE_SCHEMA:
            raise ValueError("host evidence schema is unsupported")
        _validate_subject(report["subject"], subject)
        _validate_host(report["host"], pinned_version=pinned_hermes, expected_commit=hermes_git_commit)
        pin = _require_exact_keys(fixture.get("pinned_host"), {"hermes_version", "hermes_git_commit"}, "fixture.pinned_host")
        _require_git_commit(pin["hermes_git_commit"], "fixture.pinned_host.hermes_git_commit")
        if pin != {"hermes_version": pinned_hermes, "hermes_git_commit": hermes_git_commit}:
            raise ValueError("fixture.pinned_host does not match the current official host")
        subject_commit = report["subject"]["profile_git_commit"]
        _validate_hashed_source(report["fixture"], label="fixture", expected_path=HOST_FIXTURE_PATH, subject_commit=subject_commit)
        _validate_hashed_source(report["producer"], label="producer", expected_path=HOST_PRODUCER_PATH, subject_commit=subject_commit)
        count = _require_int(report["compression_count"], "compression_count", minimum=1)
        request_sha = _require_sha256(report["captured_request_sha256"], "captured_request_sha256")
        if request_sha != _sha256_bytes(_canonical_json_bytes(report["captured_request"])):
            raise ValueError("captured_request_sha256 does not match captured_request")
        _derive_host_assertions(fixture, report["captured_request"])
        host = report["host"]
        return {
            "status": "passed",
            "hermes_version": host["hermes_version"],
            "hermes_git_commit": host["hermes_git_commit"],
            "compression_count": count,
            "captured_request_sha256": request_sha,
            "required_assertions_passed": True,
        }, []
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {**empty, "status": "invalid", "reason": str(error)}, [_blocker("HOST_COMPACTION_EVIDENCE_INVALID", "Raw host-compaction evidence is invalid.")]


def _decimal(value: object, label: str) -> Decimal:
    if type(value) not in {str, int, float} or type(value) is bool:
        raise ValueError(f"{label} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a finite decimal") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be a finite decimal >= 0")
    return result


def _validate_performance_contract(contract: dict[str, object]) -> dict[str, object]:
    top = {"schema", "scope", "subject", "host", "provider", "model", "sample_plan", "execution", "safety_budget", "pricing_snapshot", "measurement", "accounting", "acceptance", "deferred_scopes", "cases"}
    contract = _require_exact_keys(contract, top, "performance contract")
    if contract["schema"] != PERFORMANCE_CONTRACT_SCHEMA:
        raise ValueError("performance contract schema is unsupported")
    for key in ("scope", "provider", "model"):
        _require_string(contract[key], f"performance contract.{key}")
    subject = _require_exact_keys(contract["subject"], {"name", "version"}, "contract.subject")
    host = _require_exact_keys(contract["host"], {"hermes_version", "hermes_git_commit"}, "contract.host")
    for key, value in subject.items():
        _require_string(value, f"contract.subject.{key}")
    _require_string(host["hermes_version"], "contract.host.hermes_version")
    _require_git_commit(host["hermes_git_commit"], "contract.host.hermes_git_commit")
    plan = _require_exact_keys(contract["sample_plan"], {"warmup_case_id", "warmup_runs", "measured_runs_per_case", "measured_sample_count"}, "contract.sample_plan")
    _require_string(plan["warmup_case_id"], "sample_plan.warmup_case_id")
    if _require_int(plan["warmup_runs"], "sample_plan.warmup_runs") != 1:
        raise ValueError("sample_plan.warmup_runs must be one")
    runs = _require_int(plan["measured_runs_per_case"], "sample_plan.measured_runs_per_case", minimum=1)
    count = _require_int(plan["measured_sample_count"], "sample_plan.measured_sample_count", minimum=1)
    execution = _require_exact_keys(contract["execution"], {"timeout_seconds", "max_iterations_per_run", "max_output_tokens_per_call"}, "contract.execution")
    for key in execution:
        _require_int(execution[key], f"execution.{key}", minimum=1)
    budget = _require_exact_keys(contract["safety_budget"], {"max_successful_llm_calls", "max_total_peak_estimated_cost_usd", "budget_semantics"}, "contract.safety_budget")
    _require_int(budget["max_successful_llm_calls"], "safety_budget.max_successful_llm_calls", minimum=1)
    _decimal(budget["max_total_peak_estimated_cost_usd"], "safety_budget.max_total_peak_estimated_cost_usd")
    if budget["budget_semantics"] != "warmup_plus_measured_cumulative_peak_pricing_test_safety_not_business_sla":
        raise ValueError("unsupported safety_budget.budget_semantics")
    pricing_keys = {"schema", "source_url", "accessed_on", "billing_period", "currency", "unit_tokens", "model", "input_cache_hit_per_million", "input_cache_miss_per_million", "output_per_million"}
    pricing = _require_exact_keys(contract["pricing_snapshot"], pricing_keys, "contract.pricing_snapshot")
    _require_int(pricing["unit_tokens"], "pricing_snapshot.unit_tokens", minimum=1)
    for key in pricing_keys - {"unit_tokens", "input_cache_hit_per_million", "input_cache_miss_per_million", "output_per_million"}:
        _require_string(pricing[key], f"pricing_snapshot.{key}")
    for key in ("input_cache_hit_per_million", "input_cache_miss_per_million", "output_per_million"):
        _decimal(pricing[key], f"pricing_snapshot.{key}")
    expected_measurement = {
        "clock": "time.perf_counter_ns",
        "duration_unit": "ns",
        "p50_algorithm": "statistics.median",
        "p90_algorithm": "statistics.quantiles(n=10,method='inclusive')[8]",
        "raw_samples_hash_algorithm": "sha256(canonical_json({warmup,samples}))",
        "raw_samples_hash_owner": "release_receipt_builder",
    }
    if contract["measurement"] != expected_measurement:
        raise ValueError("performance contract.measurement uses unsupported semantics")
    expected_accounting = {
        "usage_source": "Hermes AIAgent.run_conversation result totals",
        "total_tokens_equation": "input_tokens + cache_read_tokens + cache_write_tokens + output_tokens",
        "reasoning_tokens_semantics": "subset_of_output_tokens",
        "cost_derivation": "Decimal cumulative warmup plus measured peak snapshot: input_tokens*cache_miss + cache_read_tokens*cache_hit + output_tokens*output; cache_write_tokens must equal 0",
    }
    if contract["accounting"] != expected_accounting:
        raise ValueError("performance contract.accounting uses unsupported semantics")
    acceptance_keys = {"expected_tool_call_rule", "required_tool_result", "required_final_response", "database_runtime_entered", "required_api_calls_per_run", "max_duration_ns", "threshold_basis", "expected_tool_schema_sha256"}
    acceptance = _require_exact_keys(contract["acceptance"], acceptance_keys, "contract.acceptance")
    if acceptance["expected_tool_call_rule"] != "exactly_one_and_equal":
        raise ValueError("unsupported expected_tool_call_rule")
    required_result = {
        "status": "failed",
        "error": {
            "code": "DATA_ENTITLEMENT_DENIED",
            "message": "当前请求未获授权，业务查询未执行。",
            "retryable": False,
        },
    }
    if _canonical_json_bytes(acceptance["required_tool_result"]) != _canonical_json_bytes(required_result):
        raise ValueError("acceptance.required_tool_result must be the exact fail-closed object")
    _require_string(acceptance["required_final_response"], "acceptance.required_final_response")
    _require_sha256(acceptance["expected_tool_schema_sha256"], "acceptance.expected_tool_schema_sha256")
    _require_bool(acceptance["database_runtime_entered"], "acceptance.database_runtime_entered")
    _require_int(acceptance["required_api_calls_per_run"], "acceptance.required_api_calls_per_run", minimum=1)
    max_duration_ns = _require_int(acceptance["max_duration_ns"], "acceptance.max_duration_ns", minimum=1)
    if max_duration_ns != execution["timeout_seconds"] * 1_000_000_000:
        raise ValueError("acceptance.max_duration_ns must equal the execution timeout")
    if acceptance["threshold_basis"] != "test_execution_safety_not_business_sla":
        raise ValueError("unsupported acceptance.threshold_basis")
    deferred = contract["deferred_scopes"]
    if not isinstance(deferred, list) or not all(isinstance(item, str) and item for item in deferred) or len(deferred) != len(set(deferred)):
        raise ValueError("performance contract.deferred_scopes must be unique strings")
    cases = contract["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("performance contract.cases must be non-empty")
    case_ids: set[str] = set()
    for index, item in enumerate(cases):
        case = _require_exact_keys(item, {"id", "expected_tool", "expected_arguments", "prompt"}, f"cases[{index}]")
        identifier = _require_string(case["id"], f"cases[{index}].id")
        for key in ("expected_tool", "prompt"):
            _require_string(case[key], f"cases[{index}].{key}")
        if not isinstance(case["expected_arguments"], dict):
            raise ValueError(f"cases[{index}].expected_arguments must be an object")
        if identifier in case_ids:
            raise ValueError("duplicate performance case id")
        case_ids.add(identifier)
    if plan["warmup_case_id"] not in case_ids:
        raise ValueError("sample_plan.warmup_case_id is not a configured case")
    if count != len(cases) * runs:
        raise ValueError("sample_plan.measured_sample_count is inconsistent")
    return contract


def _evaluate_performance_evidence(
    evidence: dict[str, object] | None,
    contract_payload: dict[str, object] | None,
    *,
    subject: dict[str, str],
    pinned_hermes: str,
    hermes_git_commit: str,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    empty = {"status": "missing", "sample_count": None, "p50_ms": None, "p90_ms": None, "total_tokens": None, "successful_llm_calls": None, "total_peak_estimated_cost_usd": None, "latency_status": "missing", "cost_status": "missing", "raw_samples_sha256": None}
    if evidence is None:
        return empty, [_blocker("PERFORMANCE_COST_NOT_VERIFIED", "Raw performance/cost evidence is missing.")]
    try:
        if contract_payload is None:
            raise ValueError("tracked performance contract is missing")
        contract = _validate_performance_contract(contract_payload)
        report_keys = {"schema", "subject", "host", "provider", "model", "contract", "producer", "system_prompt", "tool_schema_sha256", "pricing_snapshot_sha256", "warmup", "samples"}
        report = _require_exact_keys(evidence, report_keys, "performance evidence")
        if report["schema"] != PERFORMANCE_EVIDENCE_SCHEMA:
            raise ValueError("performance evidence schema is unsupported")
        _validate_subject(report["subject"], subject)
        _validate_host(report["host"], pinned_version=pinned_hermes, expected_commit=hermes_git_commit)
        subject_commit = report["subject"]["profile_git_commit"]
        _validate_hashed_source(report["contract"], label="contract", expected_path=PERFORMANCE_CONTRACT_PATH, subject_commit=subject_commit)
        _validate_hashed_source(report["producer"], label="producer", expected_path=PERFORMANCE_PRODUCER_PATH, subject_commit=subject_commit)
        _validate_hashed_source(report["system_prompt"], label="system_prompt", expected_path=SYSTEM_PROMPT_PATH, subject_commit=subject_commit)
        if report["provider"] != contract["provider"] or report["model"] != contract["model"]:
            raise ValueError("provider/model do not match the tracked performance contract")
        contract_subject = contract["subject"]
        contract_host = contract["host"]
        if contract_subject["name"] != subject["name"] or contract_subject["version"] != subject["version"]:
            raise ValueError("performance contract subject does not match the candidate")
        if contract_host != {"hermes_version": pinned_hermes, "hermes_git_commit": hermes_git_commit}:
            raise ValueError("performance contract host does not match the Hermes pin")
        pricing = contract["pricing_snapshot"]
        pricing_sha = _require_sha256(report["pricing_snapshot_sha256"], "pricing_snapshot_sha256")
        if pricing_sha != _sha256_bytes(_canonical_json_bytes(pricing)):
            raise ValueError("pricing_snapshot_sha256 does not match the tracked pricing snapshot")

        acceptance = contract["acceptance"]
        tool_schema_sha = _require_sha256(report["tool_schema_sha256"], "tool_schema_sha256")
        if tool_schema_sha != acceptance["expected_tool_schema_sha256"]:
            raise ValueError("tool_schema_sha256 does not match the tracked contract pin")
        cases = {case["id"]: case for case in contract["cases"]}
        plan = contract["sample_plan"]
        runs_per_case = plan["measured_runs_per_case"]
        expected_keys = {(case_id, run_index) for case_id in cases for run_index in range(1, runs_per_case + 1)}
        samples = report["samples"]
        if not isinstance(samples, list) or len(samples) != plan["measured_sample_count"]:
            raise ValueError("samples do not match measured_sample_count")
        warmup = report["warmup"]
        seen: set[tuple[str, int]] = set()
        durations: list[int] = []
        total_tokens = 0
        total_api_calls = 0
        total_cost = Decimal(0)
        timeout_ns = acceptance["max_duration_ns"]
        unit_tokens = Decimal(pricing["unit_tokens"])
        hit_price = _decimal(pricing["input_cache_hit_per_million"], "cache-hit price")
        miss_price = _decimal(pricing["input_cache_miss_per_million"], "cache-miss price")
        output_price = _decimal(pricing["output_per_million"], "output price")
        all_samples = [("warmup", warmup, 0)] + [(f"samples[{index}]", item, 1) for index, item in enumerate(samples)]
        for label, item, minimum_run in all_samples:
            sample_keys = {"case_id", "run_index", "duration_ns", "expected_tool", "observed_tool_calls", "tool_result", "database_runtime_entered", "final_response", "usage"}
            sample = _require_exact_keys(item, sample_keys, label)
            case_id = _require_string(sample["case_id"], f"{label}.case_id")
            run_index = _require_int(sample["run_index"], f"{label}.run_index", minimum=minimum_run)
            key = (case_id, run_index)
            is_warmup = label == "warmup"
            if is_warmup:
                if case_id != plan["warmup_case_id"] or run_index != 0:
                    raise ValueError("warmup does not match the tracked warmup plan")
            else:
                if key in seen:
                    raise ValueError("duplicate performance sample")
                seen.add(key)
                if key not in expected_keys:
                    raise ValueError("unexpected performance case or run index")
            expected_tool = cases[case_id]["expected_tool"]
            if sample["expected_tool"] != expected_tool:
                raise ValueError("sample expected_tool does not match the tracked case")
            observed_tools = sample["observed_tool_calls"]
            expected_call = {"name": expected_tool, "arguments": cases[case_id]["expected_arguments"]}
            if _canonical_json_bytes(observed_tools) != _canonical_json_bytes([expected_call]):
                raise ValueError("sample must call exactly the tracked tool and arguments once")
            tool_result = sample["tool_result"]
            if not isinstance(tool_result, dict):
                raise ValueError("sample.tool_result must be the raw result object")
            if _canonical_json_bytes(tool_result) != _canonical_json_bytes(acceptance["required_tool_result"]):
                raise ValueError("sample tool result must equal the complete tracked fail-closed object")
            if _require_bool(sample["database_runtime_entered"], "database_runtime_entered") is not acceptance["database_runtime_entered"]:
                raise ValueError("database_runtime_entered does not match the tracked acceptance rule")
            final_response = _require_string(sample["final_response"], f"{label}.final_response")
            if final_response.strip() != acceptance["required_final_response"]:
                raise ValueError("final response does not exactly report the fail-closed code")
            duration = _require_int(sample["duration_ns"], f"{label}.duration_ns")
            if duration > timeout_ns:
                raise ValueError("performance sample exceeds the tracked per-run timeout")
            if not is_warmup:
                durations.append(duration)
            usage_keys = {"input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "api_calls"}
            usage = _require_exact_keys(sample["usage"], usage_keys, f"{label}.usage")
            values = {
                name: _require_int(usage[name], f"{label}.usage.{name}")
                for name in usage
            }
            if values["cache_write_tokens"] != 0:
                raise ValueError("cache-write pricing is unavailable, so cache writes must be zero")
            if values["reasoning_tokens"] > values["output_tokens"]:
                raise ValueError("reasoning_tokens must be a subset of output_tokens")
            expected_total = values["input_tokens"] + values["cache_read_tokens"] + values["cache_write_tokens"] + values["output_tokens"]
            if values["total_tokens"] != expected_total:
                raise ValueError("total_tokens does not satisfy the Hermes CanonicalUsage equation")
            if values["api_calls"] != acceptance["required_api_calls_per_run"]:
                raise ValueError("api_calls does not match the tracked per-run requirement")
            total_tokens += values["total_tokens"]
            total_api_calls += values["api_calls"]
            numerator = Decimal(values["input_tokens"]) * miss_price + Decimal(values["cache_read_tokens"]) * hit_price + Decimal(values["output_tokens"]) * output_price
            total_cost += numerator / unit_tokens
        if seen != expected_keys:
            raise ValueError("one or more required performance samples are missing")
        max_calls = contract["safety_budget"]["max_successful_llm_calls"]
        if total_api_calls != max_calls:
            raise ValueError("cumulative successful LLM calls do not exactly consume the tracked plan")
        max_total_cost = _decimal(contract["safety_budget"]["max_total_peak_estimated_cost_usd"], "safety_budget.max_total_peak_estimated_cost_usd")
        if total_cost > max_total_cost:
            raise ValueError("total peak-priced estimated cost exceeds the tracked safety budget")
        p50_ms = statistics.median(durations) / 1_000_000
        p90_ms = statistics.quantiles(durations, n=10, method="inclusive")[8] / 1_000_000
        if not (math.isfinite(p50_ms) and math.isfinite(p90_ms) and p50_ms <= p90_ms):
            raise ValueError("derived latency quantiles are invalid")
        return {
            "status": "passed",
            "sample_count": len(samples),
            "p50_ms": p50_ms,
            "p90_ms": p90_ms,
            "total_tokens": total_tokens,
            "successful_llm_calls": total_api_calls,
            "total_peak_estimated_cost_usd": format(total_cost, "f"),
            "latency_status": "passed",
            "cost_status": "passed",
            "raw_samples_sha256": _sha256_bytes(_canonical_json_bytes({"warmup": warmup, "samples": samples})),
        }, []
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError, statistics.StatisticsError) as error:
        invalid = {**empty, "status": "invalid", "latency_status": "invalid", "cost_status": "invalid", "reason": str(error)}
        return invalid, [_blocker("PERFORMANCE_EVIDENCE_INVALID", "Raw performance/cost evidence is invalid.")]


def evaluate_release_gates(
    release_validation: dict[str, object],
    host_fixture: dict[str, object],
    *,
    version: str,
    subject: dict[str, str] | None = None,
    profile_git_commit: str | None = None,
    hermes_git_commit: str | None = None,
    host_evidence: dict[str, object] | None = None,
    performance_evidence: dict[str, object] | None = None,
    performance_contract: dict[str, object] | None = None,
    profile_worktree_clean: bool = True,
    hermes_worktree_clean: bool = True,
) -> dict[str, object]:
    """Derive release state from raw evidence; source manifests never self-attest."""

    blockers: list[dict[str, str]] = []
    if not profile_worktree_clean:
        blockers.append(_blocker("PROFILE_WORKTREE_DIRTY", "The Profile-scoped Git worktree is not clean."))
    if not hermes_worktree_clean:
        blockers.append(_blocker("HERMES_WORKTREE_DIRTY", "The pinned Hermes source worktree is not clean."))
    target = str(release_validation.get("release_target", "missing"))
    trusted = release_validation.get("trusted_replay_gate")
    trusted = trusted if isinstance(trusted, dict) else {}
    required_runs = trusted.get("required_replay_runs_per_case")
    case_ids = trusted.get("case_ids")
    valid_live_contract = type(required_runs) is int and required_runs >= 1 and isinstance(case_ids, list) and bool(case_ids)
    valid_live_contract = valid_live_contract and all(isinstance(case_id, str) and case_id for case_id in case_ids) and len(case_ids) == len(set(case_ids))
    if target != version:
        blockers.append(_blocker("LIVE_REPLAY_TARGET_MISMATCH", "Live replay requirements do not target the candidate version."))
    if not valid_live_contract:
        blockers.append(_blocker("LIVE_REPLAY_RUN_CONTRACT_INVALID", "Trusted replay run requirements are incomplete."))
    # Legacy status strings and completed-run counters are declarations only.
    blockers.extend([
        _blocker("LIVE_MODEL_REPLAY_NOT_VERIFIED", "Raw live-model replay evidence is not available."),
        _blocker("LIVE_RELEASE_GATE_BLOCKED", "The live Golden release gate remains blocked."),
        _blocker("OUTBOUND_DELIVERY_NOT_VERIFIED", "Raw outbound-delivery evidence is not available."),
        _blocker("STABILITY_NOT_VERIFIED", "Raw repeated-run stability evidence is not available."),
        _blocker("LIVE_REPLAY_RUNS_INCOMPLETE", "Raw repeated Golden runs are not available."),
    ])

    distribution = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    pinned_hermes = str(distribution.get("hermes_requires", "")).removeprefix("==")
    commit = profile_git_commit or (subject or {}).get("profile_git_commit")
    expected_subject = subject or {"name": str(distribution.get("name", "")), "version": version, "content_sha256": "", "profile_git_commit": str(commit or "")}
    actual_hermes_commit = hermes_git_commit or ""
    if (host_evidence is not None or performance_evidence is not None) and not actual_hermes_commit:
        actual_hermes_commit = _git_commit(_hermes_source_root())
    host_result, host_blockers = _evaluate_host_evidence(host_evidence, host_fixture, subject=expected_subject, pinned_hermes=pinned_hermes, hermes_git_commit=actual_hermes_commit)
    performance_result, performance_blockers = _evaluate_performance_evidence(performance_evidence, performance_contract, subject=expected_subject, pinned_hermes=pinned_hermes, hermes_git_commit=actual_hermes_commit)
    blockers.extend(host_blockers)
    blockers.extend(performance_blockers)
    return {
        "live_model_replay": {"status": "unverified_raw_evidence_required", "release_gate_status": "blocked", "target": target},
        "host_compaction": host_result,
        "performance_cost": performance_result,
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
    host_fixture = _read_strict_json(HOST_COMPACTION_FIXTURE)
    profile_git_commit = _git_commit(ROOT)
    subject = {
        "name": str(actual.get("name", "")),
        "version": str(actual.get("version", "")),
        "content_sha256": str(actual.get("content_sha256", "")),
        "profile_git_commit": profile_git_commit,
    }
    try:
        host_evidence = _discover_evidence("host", profile_git_commit)
    except (OSError, ValueError, json.JSONDecodeError):
        host_evidence = {"schema": "invalid-host-evidence"}
    try:
        performance_evidence = _discover_evidence("performance", profile_git_commit)
    except (OSError, ValueError, json.JSONDecodeError):
        performance_evidence = {"schema": "invalid-performance-evidence"}
    performance_contract = (
        _read_strict_json(PERFORMANCE_CONTRACT)
        if PERFORMANCE_CONTRACT.is_file()
        else None
    )
    try:
        profile_worktree_clean = _git_worktree_clean(_profile_git_root(), ROOT)
    except ValueError:
        profile_worktree_clean = False
    try:
        hermes_root = _hermes_source_root()
        hermes_git_commit = _git_commit(hermes_root)
        hermes_worktree_clean = _git_worktree_clean(hermes_root)
    except ValueError:
        # A non-empty sentinel makes both strict host validators fail closed.
        hermes_git_commit = "unresolved"
        hermes_worktree_clean = False
    gates = evaluate_release_gates(
        release_validation,
        host_fixture,
        version=str(actual.get("version")),
        subject=subject,
        profile_git_commit=profile_git_commit,
        hermes_git_commit=hermes_git_commit,
        host_evidence=host_evidence,
        performance_evidence=performance_evidence,
        performance_contract=performance_contract,
        profile_worktree_clean=profile_worktree_clean,
        hermes_worktree_clean=hermes_worktree_clean,
    )
    blockers = list(gates.pop("blockers"))
    pinned_hermes = str(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")).get("hermes_requires", "")).removeprefix("==")
    if not _host_pins_match(host_fixture, performance_contract, version=pinned_hermes, commit=hermes_git_commit):
        blockers.append(_blocker("HERMES_PIN_MISMATCH", "Current Hermes HEAD does not match both tracked test-contract pins."))
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
