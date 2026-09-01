"""Bind DataSage quality-gate evidence to one reviewed runtime payload.

Git commit/tag and the Hermes Profile Distribution manifest are authoritative
for release and installation identity.  This source-only command computes a
deterministic checksum manifest so DataSage-specific replay, compaction,
delivery, and performance evidence cannot be applied to a different payload.
It is neither an installer nor a replacement for ``hermes profile``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import subprocess
import sys
import tempfile
import types
from typing import Iterable

import yaml


ROOT = Path(__file__).resolve().parent
DATASAGE_PLUGIN_ROOT = ROOT / "plugins" / "datasage-query"
MANIFEST = ROOT / "distribution.yaml"
RELEASE_DIR = ROOT / "release"
PENDING_DIR = ROOT / "pending"
GOLDEN_SUITE = ROOT / "plugins" / "datasage-query" / "e2e" / "golden_expert_cases.json"
HOST_COMPACTION_FIXTURE = ROOT / "tests" / "fixtures" / "host_compaction_ordering.json"
PERFORMANCE_CONTRACT = ROOT / "tests" / "fixtures" / "performance_non_db_contract.json"
LIVE_RELEASE_CONTRACT = ROOT / "tests" / "fixtures" / "live_release_contract.json"
EVIDENCE_DIR = ROOT / "pending" / "evidence"
HOST_EVIDENCE_SCHEMA = "datasage-host-compaction-evidence/v1"
PERFORMANCE_EVIDENCE_SCHEMA = "datasage-performance-evidence/v1"
LIVE_EVIDENCE_SCHEMA = "datasage-live-release-evidence/v3"
PERFORMANCE_CONTRACT_SCHEMA = "datasage-performance-non-db-contract/v2"
PERFORMANCE_INTEGRITY_POLICY = "datasage-performance-integrity/v1"
LIVE_CONTRACT_SCHEMA = "datasage-live-release-contract/v6"
HOST_PRODUCER_PATH = "tests/test_host_compaction_e2e.py"
PERFORMANCE_PRODUCER_PATH = "tests/run_performance_evidence.py"
LIVE_PRODUCER_PATH = "tests/run_live_release_evidence.py"
GOLDEN_SUITE_PATH = "plugins/datasage-query/e2e/golden_expert_cases.json"
TRANSCRIPT_ADAPTER_PATH = "plugins/datasage-query/e2e/canary_transcript_adapter.py"
GOLDEN_SCORER_PATH = "plugins/datasage-query/e2e/golden_expert_scorer.py"
TRUSTED_LIVE_REVIEWER_SHA256 = [
    "705d13a1ddebaa4c3696ab8212f420df5865c8c6169b38ffbdefa15499192cac",
    "90b873c5e7a787403323752fea9333cecc3f26eb2978307614083fec33478c91",
]
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
FINAL_RECEIPT_SCHEMA = "datasage-final-release-receipt/v1"
LIVE_RUN_IDENTITY_SCHEMA = "datasage-live-run-identity/v1"
LIVE_INTEGRITY_POLICY = "datasage-live-integrity/v1"
REVIEWER_ATTESTATION_SCHEMA = "datasage-reviewer-attestation/v1"
DECISION_QUALITY_DIMENSIONS = frozenset({
    "conclusion_clarity", "decision_relevance", "actionability",
    "evidence_basis", "assumptions", "material_risks", "validation_steps",
})
OFFLINE_ALLOWED_SKIPS = frozenset({
    # This test is intentionally delegated to the one-shot evidence CLI when
    # the producer is tracked.  It is the only expected skip in the current
    # offline suite; all other skips remain release blockers.
    "test_build_evidence_rejects_current_untracked_producer",
})
OFFLINE_ALLOWED_EXPECTED_FAILURES = frozenset()

SESSION_META_CONVERSATIONAL_FIELDS = (
    "content",
    "api_content",
    "tool_calls",
    "tool_call_id",
    "tool_name",
    "function_call",
    "name",
    "effect_disposition",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)
LIVE_ALLOWED_TOOL_NAMES = frozenset({
    "datasage_catalog",
    "datasage_entity_resolve",
    "datasage_query",
})
LIVE_SESSION_META_ALLOWED_FIELDS = frozenset({
    *SESSION_META_CONVERSATIONAL_FIELDS,
    "id",
    "role",
    "tools",
    "model",
    "timestamp",
    "platform",
})

LIVE_BLOCKERS = (
    ("LIVE_MODEL_REPLAY_NOT_VERIFIED", "Raw live-model replay evidence is not available."),
    ("LIVE_RELEASE_GATE_BLOCKED", "The live Golden release gate remains blocked."),
    ("OUTBOUND_DELIVERY_NOT_VERIFIED", "Raw outbound protocol/API ACK evidence is not available."),
    ("STABILITY_NOT_VERIFIED", "Raw repeated-run stability evidence is not available."),
    ("LIVE_REPLAY_RUNS_INCOMPLETE", "Raw repeated Golden runs are not available."),
)


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


def _contains_required_code_token(value: object, code: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(code)}(?![A-Za-z0-9_])"
    return re.search(pattern, value) is not None


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


def _require_model_fingerprint(value: object, label: str) -> str:
    """Accept only a host-issued SHA-256 fingerprint, normalized to lowercase.

    A provider label, placeholder, or arbitrary text must never be hashed here:
    hashing it would turn an untrusted value into apparently valid provenance.
    """

    value = _require_string(value, label)
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError(f"{label} must be a 64-character SHA-256 fingerprint")
    return value.lower()


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


def _canonical_hermes_python() -> Path:
    root = _hermes_source_root()
    expected = (root / "venv" / "Scripts" / "python.exe").resolve()
    if not expected.is_file() or Path(sys.executable).resolve() != expected:
        raise ValueError("release builder must run under the pinned Hermes venv interpreter")
    return expected


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _approved_sys_path_tokens(approval: dict[str, object], entry_kind: str) -> list[str]:
    policy = _require_exact_keys(
        approval.get("sys_path"),
        {"runner_entry", "builder_entry", "ordered_tail", "runner_sha256", "builder_sha256"},
        "approved Python sys.path",
    )
    for key in ("runner_entry", "builder_entry", "runner_sha256", "builder_sha256"):
        _require_string(policy[key], f"approved Python sys.path.{key}")
    _require_sha256(policy["runner_sha256"], "approved Python sys.path.runner_sha256")
    _require_sha256(policy["builder_sha256"], "approved Python sys.path.builder_sha256")
    tail = policy["ordered_tail"]
    if not isinstance(tail, list) or len(tail) != 11 or not all(isinstance(item, str) and item for item in tail):
        raise ValueError("approved Python sys.path tail must contain exactly eleven tokens")
    if policy["runner_entry"] != "profile:tests" or policy["builder_entry"] != "profile:.":
        raise ValueError("approved Python sys.path entry variants are invalid")
    runner = [policy["runner_entry"], *tail]
    builder = [policy["builder_entry"], *tail]
    if (
        _sha256_bytes(_canonical_json_bytes(runner)) != policy["runner_sha256"]
        or _sha256_bytes(_canonical_json_bytes(builder)) != policy["builder_sha256"]
    ):
        raise ValueError("approved Python sys.path ordered summary mismatch")
    if entry_kind not in {"runner", "builder"}:
        raise ValueError("Python provenance entry kind is invalid")
    return runner if entry_kind == "runner" else builder


def _runtime_sys_path_tokens(paths: list[Path], entry_kind: str) -> list[str]:
    if entry_kind not in {"runner", "builder"}:
        raise ValueError("Python provenance entry kind is invalid")
    expected_entry = (ROOT / "tests").resolve() if entry_kind == "runner" else ROOT.resolve()
    if not paths or paths[0] != expected_entry or len(paths) != len(set(paths)):
        raise ValueError("Python runtime path has a wrong entry or duplicate path")
    hermes_root = _hermes_source_root().resolve()
    prefix = _canonical_hermes_python().parent.parent.resolve()
    base_prefix = Path(sys.base_prefix).resolve()
    editable = ROOT.resolve() / "__editable__.hermes_agent-0.20.5.finder.__path_hook__"
    tokens = ["profile:tests" if entry_kind == "runner" else "profile:."]
    for index, path in enumerate(paths):
        if index == 0:
            continue
        if path == editable and not path.exists():
            tokens.append("profile:" + path.name)
        elif path == hermes_root and path.exists():
            tokens.append("hermes:.")
        elif _is_within(path, prefix) and path.exists():
            relative = path.relative_to(prefix).as_posix()
            tokens.append("venv:" + (relative or "."))
        elif _is_within(path, base_prefix) and (path.exists() or path == base_prefix / "python313.zip"):
            relative = path.relative_to(base_prefix).as_posix()
            tokens.append("base:" + (relative or "."))
        else:
            raise ValueError("Python runtime path contains an unapproved or nonexistent entry")
    return tokens


def _validate_runtime_sys_paths(paths: list[Path], entry_kind: str, approval: dict[str, object] | None = None) -> list[str]:
    if approval is None:
        approval = _read_strict_json(LIVE_RELEASE_CONTRACT).get("python_provenance_approval")
    if not isinstance(approval, dict):
        raise ValueError("tracked Python provenance approval is missing")
    tokens = _runtime_sys_path_tokens(paths, entry_kind)
    if tokens != _approved_sys_path_tokens(approval, entry_kind):
        raise ValueError("Python runtime path order differs from the tracked approval")
    return tokens


def _runtime_sys_paths(entry_kind: str, approval: dict[str, object] | None = None) -> tuple[list[Path], list[str]]:
    paths = []
    for value in sys.path:
        if value and re.fullmatch(r"__editable__\.[A-Za-z0-9_.-]+\.finder\.__path_hook__", value):
            paths.append(ROOT.resolve() / value)
        else:
            paths.append(Path(value or ROOT).resolve())
    current_entry = "runner" if paths and paths[0] == (ROOT / "tests").resolve() else "builder" if paths and paths[0] == ROOT.resolve() else None
    if current_entry is None:
        raise ValueError("Python runtime entry is neither the Profile runner nor release builder")
    if current_entry != entry_kind:
        if current_entry != "builder" or entry_kind != "runner":
            raise ValueError("Python runtime entry transition is unsupported")
        paths = [(ROOT / "tests").resolve(), *paths[1:]]
    tokens = _validate_runtime_sys_paths(paths, entry_kind, approval)
    return paths, tokens


def _file_identity(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Python provenance source is not a regular file: {path.name}")
    payload = path.read_bytes()
    return {"sha256": _sha256_bytes(payload), "bytes": len(payload)}


def _python_path_controls(paths: list[Path], prefix: Path) -> list[dict[str, object]]:
    controls = []
    for path in paths:
        pth = []
        if path.is_dir():
            for item in sorted(path.glob("*.pth"), key=lambda value: value.name):
                if not _is_within(item.resolve(), prefix):
                    raise ValueError("effective .pth is outside the pinned Hermes venv")
                pth.append({"path": item.resolve().relative_to(prefix).as_posix(), **_file_identity(item)})
        customization: dict[str, object] = {}
        for name in ("sitecustomize.py", "usercustomize.py"):
            item = path / name
            customization[name] = {"exists": item.exists(), "identity": _file_identity(item) if item.exists() else None}
        controls.append({
            "path_sha256": _sha256_bytes(str(path).encode("utf-8")),
            "pth": pth,
            "customization": customization,
        })
    return controls


def _hermes_import_origins(hermes_root: Path, approval: dict[str, object] | None = None) -> list[dict[str, object]]:
    result = []
    modules = (
        tuple(item["module"] for item in approval.get("imports", []))
        if isinstance(approval, dict)
        else ("hermes_cli.main", "hermes_cli.session_export")
    )
    for module in modules:
        spec = importlib.util.find_spec(module)
        origin = Path(spec.origin).resolve() if spec is not None and spec.origin else None
        if origin is None or hermes_root not in origin.parents:
            raise ValueError(f"Python import origin for {module} is outside the pinned Hermes checkout")
        result.append({
            "module": module,
            "path": origin.relative_to(hermes_root).as_posix(),
            "origin_sha256": _sha256_bytes(str(origin).encode("utf-8")),
            **_file_identity(origin),
        })
    return result


def _pth_import_payloads(prefix: Path) -> list[dict[str, object]]:
    result = []
    for module in (
        "__editable___hermes_agent_0_20_5_finder",
        "_virtualenv",
        "pywin32_bootstrap",
    ):
        spec = importlib.util.find_spec(module)
        origin = Path(spec.origin).resolve() if spec is not None and spec.origin else None
        if origin is None or not _is_within(origin, prefix):
            raise ValueError(f".pth import payload for {module} is outside the pinned Hermes venv")
        result.append({
            "module": module,
            "path": origin.relative_to(prefix).as_posix(),
            **_file_identity(origin),
        })
    return result


def _current_python_provenance(entry_kind: str = "runner", approval: dict[str, object] | None = None) -> dict[str, object]:
    executable = _canonical_hermes_python()
    prefix = executable.parent.parent.resolve()
    base_prefix = Path(sys.base_prefix).resolve()
    effective_paths, path_tokens = _runtime_sys_paths(entry_kind, approval)
    controls = _python_path_controls(effective_paths, prefix)
    if any(item["exists"] for control in controls for item in control["customization"].values()):
        raise ValueError("sitecustomize.py and usercustomize.py are forbidden on the effective runner path")
    path_hashes = [_sha256_bytes(str(path).encode("utf-8")) for path in effective_paths]
    return {
        "schema": "datasage-python-runtime-provenance/v1",
        "entry_kind": entry_kind,
        "executable": _file_identity(executable),
        "sys_executable_sha256": _sha256_bytes(str(executable).encode("utf-8")),
        "sys_prefix_sha256": _sha256_bytes(str(prefix).encode("utf-8")),
        "sys_base_prefix_sha256": _sha256_bytes(str(base_prefix).encode("utf-8")),
        "sys_path_entry_sha256": path_hashes,
        "sys_path_sha256": _sha256_bytes(_canonical_json_bytes(path_hashes)),
        "sys_path_template_sha256": _sha256_bytes(_canonical_json_bytes(path_tokens)),
        "path_controls": controls,
        "pth_import_payloads": _pth_import_payloads(prefix),
        "import_origins": _hermes_import_origins(_hermes_source_root(), approval),
    }


def _validate_python_provenance(value: object, label: str, approval: dict[str, object]) -> dict[str, object]:
    proof = _require_exact_keys(
        value,
        {"schema", "entry_kind", "executable", "sys_executable_sha256", "sys_prefix_sha256", "sys_base_prefix_sha256", "sys_path_entry_sha256", "sys_path_sha256", "sys_path_template_sha256", "path_controls", "pth_import_payloads", "import_origins"},
        label,
    )
    _require_exact_keys(proof["executable"], {"sha256", "bytes"}, f"{label}.executable")
    if proof["entry_kind"] != "runner" or _canonical_json_bytes(proof) != _canonical_json_bytes(_current_python_provenance("runner", approval)):
        raise ValueError(f"{label} differs from the current pinned Python runtime content")
    observed_pth = [item for control in proof["path_controls"] for item in control["pth"]]
    observed_pth_payloads = proof["pth_import_payloads"]
    observed_imports = [{key: item[key] for key in ("module", "path", "sha256", "bytes")} for item in proof["import_origins"]]
    if (
        _canonical_json_bytes(proof["executable"]) != _canonical_json_bytes(approval["executable"])
        or proof["sys_path_template_sha256"] != approval["sys_path"]["runner_sha256"]
        or _canonical_json_bytes(observed_pth) != _canonical_json_bytes(approval["pth"])
        or _canonical_json_bytes(observed_pth_payloads) != _canonical_json_bytes(approval["pth_import_payloads"])
        or _canonical_json_bytes(observed_imports) != _canonical_json_bytes(approval["imports"])
    ):
        raise ValueError(f"{label} is not approved by the tracked Python provenance pins")
    return proof


def _evidence_path(kind: str, profile_git_commit: str) -> Path:
    prefixes = {
        "host": "host-compaction",
        "performance": "performance",
        "live": "live-release",
    }
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


def _configured_model_identity() -> tuple[str, str]:
    """Return the model configured by the reviewed Profile payload."""

    payload = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, dict):
        raise ValueError("config.model must be an object")
    provider = _require_string(model.get("provider"), "config.model.provider")
    configured = model.get("default") or model.get("model")
    if isinstance(configured, dict):
        configured = configured.get("model")
    return provider, _require_string(configured, "config.model.default")


def _validate_live_run_identity(
    value: object,
    *,
    subject: dict[str, str],
    expected_model: tuple[str, str],
) -> dict[str, str]:
    """Validate the host-produced identity bound to one live run.

    The nonce and fingerprints must come from the official session export.  We
    deliberately do not synthesize them from the current configuration: doing
    so would make a replay from another model/profile look current.
    """

    identity = _require_exact_keys(
        value,
        {
            "schema",
            "fresh_run_nonce",
            "provider",
            "model",
            "model_fingerprint_sha256",
            "system_prompt_sha256",
            "profile_content_sha256",
        },
        "live run identity",
    )
    if identity["schema"] != LIVE_RUN_IDENTITY_SCHEMA:
        raise ValueError("live run identity schema is unsupported")
    nonce = _require_string(identity["fresh_run_nonce"], "live run identity.fresh_run_nonce")
    if re.fullmatch(r"[0-9a-f]{32,128}", nonce) is None:
        raise ValueError("live run identity.fresh_run_nonce must be lowercase hex")
    provider = _require_string(identity["provider"], "live run identity.provider")
    model = _require_string(identity["model"], "live run identity.model")
    fingerprint = _require_model_fingerprint(
        identity["model_fingerprint_sha256"],
        "live run identity.model_fingerprint_sha256",
    )
    prompt_sha = _require_sha256(
        identity["system_prompt_sha256"],
        "live run identity.system_prompt_sha256",
    )
    profile_sha = _require_sha256(
        identity["profile_content_sha256"],
        "live run identity.profile_content_sha256",
    )
    if profile_sha != subject["content_sha256"]:
        raise ValueError("live run identity profile content differs from the subject")
    if (provider, model) != expected_model:
        raise ValueError("live run provider/model differs from config.yaml")
    return {
        "schema": LIVE_RUN_IDENTITY_SCHEMA,
        "fresh_run_nonce": nonce,
        "provider": provider,
        "model": model,
        "model_fingerprint_sha256": fingerprint,
        "system_prompt_sha256": prompt_sha,
        "profile_content_sha256": profile_sha,
    }


def _live_run_identity_from_export(
    exported: dict[str, object],
    *,
    subject: dict[str, str],
    expected_model: tuple[str, str],
) -> dict[str, str]:
    """Extract the same identity fields directly from the official export."""

    nonce = exported.get("fresh_run_nonce") or exported.get("run_nonce")
    provider = exported.get("billing_provider") or exported.get("provider")
    model = exported.get("model")
    fingerprint = exported.get("model_fingerprint_sha256") or exported.get("model_fingerprint")
    prompt_sha = exported.get("system_prompt_sha256") or exported.get("system_prompt_hash")
    profile_sha = exported.get("profile_content_sha256") or exported.get("profile_artifact_sha256")
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32,128}", nonce) is None:
        raise ValueError("official export lacks a host fresh-run nonce")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise ValueError("official export lacks provider/model identity")
    if (provider, model) != expected_model:
        raise ValueError("official export provider/model differs from config.yaml")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None:
        raise ValueError("official export lacks model fingerprint")
    fingerprint = fingerprint.lower()
    if not isinstance(prompt_sha, str) or SHA256_RE.fullmatch(prompt_sha) is None:
        raise ValueError("official export lacks system prompt hash")
    if not isinstance(profile_sha, str) or SHA256_RE.fullmatch(profile_sha) is None:
        raise ValueError("official export lacks profile content digest")
    if profile_sha != subject["content_sha256"]:
        raise ValueError("official export profile content digest differs from the subject")
    return {
        "schema": LIVE_RUN_IDENTITY_SCHEMA,
        "fresh_run_nonce": nonce,
        "provider": provider,
        "model": model,
        "model_fingerprint_sha256": fingerprint,
        "system_prompt_sha256": prompt_sha,
        "profile_content_sha256": profile_sha,
    }


def _gate_report_digest(result: dict[str, object]) -> str:
    payload = {key: value for key, value in result.items() if key != "gate_report_sha256"}
    return _sha256_bytes(_canonical_json_bytes(payload))


def _reviewer_attestation_digest(value: object) -> str:
    """Digest only artifact references and reviewer identity hashes."""

    if not isinstance(value, list) or not value:
        raise ValueError("reviewer attestation inputs are missing")
    return _sha256_bytes(_canonical_json_bytes(value))


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
    if not isinstance(contract, dict) or not top.issubset(contract) or set(contract) - (top | {"integrity_policy"}):
        raise ValueError("performance contract keys mismatch")
    if contract["schema"] != PERFORMANCE_CONTRACT_SCHEMA:
        raise ValueError("performance contract schema is unsupported")
    for key in ("scope", "provider", "model"):
        _require_string(contract[key], f"performance contract.{key}")
    strict_integrity = contract.get("integrity_policy") == PERFORMANCE_INTEGRITY_POLICY
    if "integrity_policy" in contract and not strict_integrity:
        raise ValueError("performance contract.integrity_policy is unsupported")
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
    acceptance_keys = {"expected_tool_call_rule", "required_tool_result", "required_final_response_code", "database_runtime_entered", "required_api_calls_per_run", "max_duration_ns", "threshold_basis", "expected_tool_schema_sha256"}
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
    required_final_response_code = _require_string(
        acceptance["required_final_response_code"],
        "acceptance.required_final_response_code",
    )
    if required_final_response_code != required_result["error"]["code"]:
        raise ValueError("acceptance.required_final_response_code must match the fail-closed code")
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
    if strict_integrity:
        if (
            contract["scope"] != "test-only host/plugin/model non-database denial path"
            or contract["provider"] != "deepseek"
            or contract["model"] != "deepseek-v4-flash"
            or acceptance["database_runtime_entered"] is not False
            or acceptance["required_api_calls_per_run"] != 2
            or budget["max_successful_llm_calls"] != 20
        ):
            raise ValueError("performance integrity policy invariants are not satisfied")
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
            if not _contains_required_code_token(
                final_response, acceptance["required_final_response_code"]
            ):
                raise ValueError("final response lacks the standalone fail-closed code")
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


def _validate_live_contract(contract: dict[str, object]) -> dict[str, object]:
    top = {
        "schema", "scope", "subject", "host", "source_paths", "case_plan",
        "turn_completion_policy",
        "execution", "inbound", "review_policy", "capture_integrity_policy",
        "python_provenance_policy", "python_provenance_approval", "outbound", "runtime_readiness_policy",
        "run_identity_policy", "tool_allowlist",
    }
    contract = _require_exact_keys(contract, top, "live contract")
    if contract["schema"] != LIVE_CONTRACT_SCHEMA:
        raise ValueError("live contract schema is unsupported")
    if contract["scope"] != "test_only_official_hermes_wecom_inbound_live_release":
        raise ValueError("live contract scope is unsupported")
    subject = _require_exact_keys(contract["subject"], {"name", "version"}, "live contract.subject")
    host = _require_exact_keys(contract["host"], {"hermes_version", "hermes_git_commit"}, "live contract.host")
    for key, value in subject.items():
        _require_string(value, f"live contract.subject.{key}")
    _require_string(host["hermes_version"], "live contract.host.hermes_version")
    _require_git_commit(host["hermes_git_commit"], "live contract.host.hermes_git_commit")
    paths = _require_exact_keys(
        contract["source_paths"],
        {"golden_suite", "adapter", "scorer", "producer"},
        "live contract.source_paths",
    )
    expected_paths = {
        "golden_suite": GOLDEN_SUITE_PATH,
        "adapter": TRANSCRIPT_ADAPTER_PATH,
        "scorer": GOLDEN_SCORER_PATH,
        "producer": LIVE_PRODUCER_PATH,
    }
    if paths != expected_paths:
        raise ValueError("live contract source paths do not match the approved components")
    plan = _require_exact_keys(
        contract["case_plan"],
        {"conversation_id", "case_ids", "turns_per_session", "runs"},
        "live contract.case_plan",
    )
    _require_string(plan["conversation_id"], "live contract.case_plan.conversation_id")
    case_ids = plan["case_ids"]
    if not isinstance(case_ids, list) or len(case_ids) != 2 or len(set(case_ids)) != 2 or not all(isinstance(item, str) and item for item in case_ids):
        raise ValueError("live contract must pin exactly two unique case IDs")
    if _require_int(plan["turns_per_session"], "live contract.case_plan.turns_per_session") != 2:
        raise ValueError("live contract must require exactly two turns per session")
    if _require_int(plan["runs"], "live contract.case_plan.runs") != 3:
        raise ValueError("live contract must require exactly three runs")
    completion_policies = _live_turn_completion_policies(
        contract["turn_completion_policy"], len(case_ids)
    )
    if [item["case_id"] for item in completion_policies] != case_ids:
        raise ValueError("turn completion policy case order differs from the Golden plan")
    execution = _require_exact_keys(
        contract["execution"],
        {"timeout_seconds", "command_shapes"},
        "live contract.execution",
    )
    _require_int(execution["timeout_seconds"], "live contract.execution.timeout_seconds", minimum=1)
    command_shapes = _require_exact_keys(
        execution["command_shapes"],
        {"session_export", "adapter", "scorer"},
        "live contract.execution.command_shapes",
    )
    for name, argv in command_shapes.items():
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError(f"live contract command shape {name} is invalid")
    forbidden = {"latest", "-c", "--continue", "-z", "--oneshot"}
    if any(item in forbidden for argv in command_shapes.values() for item in argv):
        raise ValueError("live contract command shapes contain a forbidden resume/oneshot form")
    prefix = ["{python}", "-B", "-m", "hermes_cli.main"]
    expected_shapes = {
        "session_export": [*prefix, "sessions", "export", "-", "--session-id", "{exact_session_id}", "--format", "jsonl"],
        "adapter": ["{python}", "-B", "{adapter}", "--session-export", "{session_export}", "--bindings", "{bindings}", "--output", "{candidate}"],
        "scorer": ["{python}", "-B", "{scorer}", "--cases", "{golden_suite}", "--case-id", "{case_1}", "--case-id", "{case_2}", "--candidate", "{candidate}", "--output", "{score_report}"],
    }
    if command_shapes != expected_shapes:
        raise ValueError("live contract command shapes differ from the pinned official CLI/test commands")
    inbound_keys = {"platform", "chat_type", "expected_user_id_sha256", "expected_chat_id_sha256", "identity_storage", "session_input", "title_template", "started_at_policy", "prompt_policy", "endpoint_projection_policy", "terminal_policy"}
    inbound = _require_exact_keys(contract["inbound"], inbound_keys, "live contract.inbound")
    expected_identity = "4d497bc168a1782eeaffb82b3cfa1f9ae212e86d9fa6dea721fe34712a3179e7"
    expected_inbound = {
        "platform": "wecom",
        "chat_type": "dm",
        "identity_storage": "sha256_only",
        "session_input": "exactly_three_ordered_complete_session_ids",
        "title_template": "datasage-live-{commit12}-run-{run_index}",
        "started_at_policy": "not_before_subject_commit_timestamp",
        "prompt_policy": "exactly_two_ordered_tracked_golden_user_prompts",
        "endpoint_projection_policy": "canonical_jsonl_raw_retained_endpoint_ignores_only_carrier_free_exact_session_meta_unknown_roles_fail_closed",
        "terminal_policy": "terminal_nonempty_assistant_and_closed_tool_flow",
    }
    expected_inbound.update({
        "expected_user_id_sha256": expected_identity,
        "expected_chat_id_sha256": expected_identity,
    })
    if inbound != expected_inbound:
        raise ValueError("live inbound contract semantics are unsupported")
    review_policy = _require_exact_keys(
        contract["review_policy"],
        {"reviews_per_run_case", "trusted_reviewer_id_sha256", "evidence_binding", "conclusion_consensus", "semantic_assurance"},
        "live contract.review_policy",
    )
    trusted_reviewers = review_policy["trusted_reviewer_id_sha256"]
    if (
        _require_int(review_policy["reviews_per_run_case"], "live contract.review_policy.reviews_per_run_case", minimum=2) != 2
        or not isinstance(trusted_reviewers, list)
        or trusted_reviewers != TRUSTED_LIVE_REVIEWER_SHA256
        or any(_require_sha256(value, "trusted reviewer identity") != value for value in trusted_reviewers)
        or review_policy["evidence_binding"] != "exact_answer_codepoint_ranges_sha256_per_conclusion"
        or review_policy["conclusion_consensus"] != "both_reviewers_exactly_match_candidate_and_golden"
        or review_policy["semantic_assurance"] != "trusted_human_review_not_mechanically_proven"
    ):
        raise ValueError("live review policy does not define the fixed dual-review trust boundary")
    capture_policy = _require_exact_keys(
        contract["capture_integrity_policy"],
        {"manifest_digest", "retained_process_streams", "filesystem_read_only"},
        "live contract.capture_integrity_policy",
    )
    if capture_policy != {
        "manifest_digest": "sha256_sidecar_bound_to_both_trusted_reviews",
        "retained_process_streams": "exact_private_bytes_sha256_size",
        "filesystem_read_only": "best_effort_tamper_evidence_not_same_user_authorization",
    }:
        raise ValueError("live capture integrity policy is unsupported")
    python_policy = _require_exact_keys(
        contract["python_provenance_policy"],
        {"interpreter", "sys_path", "site_packages", "customization_modules", "import_origins", "checks"},
        "live contract.python_provenance_policy",
    )
    if python_policy != {
        "interpreter": "pinned_venv_executable_sha256_size",
        "sys_path": "fixed_runner_path_sha256",
        "site_packages": "effective_path_pth_and_customization_content_sha256",
        "customization_modules": "sitecustomize_and_usercustomize_forbidden",
        "import_origins": "hermes_cli_main_session_export_from_pinned_checkout",
        "checks": "capture_and_finalize_pre_post",
    }:
        raise ValueError("live Python provenance policy is unsupported")
    approval = _require_exact_keys(
        contract["python_provenance_approval"], {"executable", "sys_path", "pth", "pth_import_payloads", "imports"},
        "live contract.python_provenance_approval",
    )
    _approved_sys_path_tokens(approval, "runner")
    _approved_sys_path_tokens(approval, "builder")
    _require_exact_keys(approval["executable"], {"sha256", "bytes"}, "approved Python executable")
    _require_sha256(approval["executable"]["sha256"], "approved Python executable.sha256")
    _require_int(approval["executable"]["bytes"], "approved Python executable.bytes", minimum=1)
    for label, values, keys in (
        ("approved .pth", approval["pth"], {"path", "sha256", "bytes"}),
        ("approved .pth import payload", approval["pth_import_payloads"], {"module", "path", "sha256", "bytes"}),
        ("approved Hermes import", approval["imports"], {"module", "path", "sha256", "bytes"}),
    ):
        if not isinstance(values, list) or not values:
            raise ValueError(f"{label} list is empty")
        for index, item in enumerate(values):
            item = _require_exact_keys(item, keys, f"{label}[{index}]")
            for key in keys - {"bytes"}:
                _require_string(item[key], f"{label}[{index}].{key}")
            _require_sha256(item["sha256"], f"{label}[{index}].sha256")
            _require_int(item["bytes"], f"{label}[{index}].bytes", minimum=1)
    tracked_approval = _read_strict_json(LIVE_RELEASE_CONTRACT).get("python_provenance_approval")
    if _canonical_json_bytes(approval) != _canonical_json_bytes(tracked_approval):
        raise ValueError("live Python provenance approval differs from the tracked contract")
    outbound = _require_exact_keys(
        contract["outbound"],
        {"collection", "status", "evidence_semantics", "release_blocker"},
        "live contract.outbound",
    )
    if outbound != {
        "collection": "forbidden_in_finalize",
        "status": "not_verified",
        "evidence_semantics": "protocol_or_api_ack_not_user_read",
        "release_blocker": "OUTBOUND_DELIVERY_NOT_VERIFIED",
    }:
        raise ValueError("live outbound contract semantics are unsupported")
    readiness = _require_exact_keys(
        contract["runtime_readiness_policy"],
        {"database_account", "database_tls", "wecom_configuration", "configuration_values_recorded", "trusted_caller_readiness"},
        "live contract.runtime_readiness_policy",
    )
    if readiness != {
        "database_account": "external_gate_not_auto_passed",
        "database_tls": "external_gate_not_auto_passed",
        "wecom_configuration": "required_for_live_execution",
        "configuration_values_recorded": False,
        "trusted_caller_readiness": "official_wecom_inbound_session_origin_verified",
    }:
        raise ValueError("live runtime readiness policy may not auto-pass external gates")
    run_identity_policy = _require_exact_keys(
        contract["run_identity_policy"],
        {"schema", "required_fields", "unique_nonce_per_run", "profile_binding"},
        "live contract.run_identity_policy",
    )
    if run_identity_policy != {
        "schema": LIVE_INTEGRITY_POLICY,
        "required_fields": [
            "fresh_run_nonce",
            "provider",
            "model",
            "model_fingerprint_sha256",
            "system_prompt_sha256",
            "profile_content_sha256",
        ],
        "unique_nonce_per_run": True,
        "profile_binding": "subject.content_sha256",
    }:
        raise ValueError("live run identity policy is unsupported")
    if contract["tool_allowlist"] != sorted(LIVE_ALLOWED_TOOL_NAMES):
        raise ValueError("live tool allowlist is unsupported")
    return contract


def _validate_process_record(
    value: object,
    expected_argv: list[str],
    label: str,
    *,
    expected_bindings: dict[str, str],
    subject_commit: str,
    run_index: int,
    stream_prefix: str,
    require_read_only: bool = False,
) -> dict[str, object]:
    record = _require_exact_keys(
        value,
        {"argv", "argv_sha256", "exit_code", "timed_out", "duration_ns", "stdout", "stderr"},
        label,
    )
    argv = record["argv"]
    if not isinstance(argv, list) or len(argv) != len(expected_argv):
        raise ValueError(f"{label}.argv does not match the tracked command token count")
    for index, (observed, expected) in enumerate(zip(argv, expected_argv)):
        if expected.startswith("{") and expected.endswith("}"):
            binding = expected[1:-1]
            token = _require_exact_keys(observed, {"binding", "value_sha256"}, f"{label}.argv[{index}]")
            if token["binding"] != binding:
                raise ValueError(f"{label}.argv[{index}] has the wrong redacted binding")
            digest = _require_sha256(token["value_sha256"], f"{label}.argv[{index}].value_sha256")
            if binding not in expected_bindings:
                raise ValueError(f"{label}.argv[{index}] has no builder-owned token binding")
            expected_digest = _sha256_bytes(expected_bindings[binding].encode("utf-8"))
            if digest != expected_digest:
                raise ValueError(f"{label}.argv[{index}] is not bound to the retained run artifact")
        elif observed != expected:
            raise ValueError(f"{label}.argv[{index}] differs from the tracked command token")
    if _require_sha256(record["argv_sha256"], f"{label}.argv_sha256") != _sha256_bytes(_canonical_json_bytes(argv)):
        raise ValueError(f"{label}.argv safe projection hash mismatch")
    if _require_int(record["exit_code"], f"{label}.exit_code") != 0:
        raise ValueError(f"{label} did not exit successfully")
    if _require_bool(record["timed_out"], f"{label}.timed_out"):
        raise ValueError(f"{label} timed out")
    _require_int(record["duration_ns"], f"{label}.duration_ns", minimum=1)
    _live_artifact(
        record["stdout"], subject_commit=subject_commit, run_index=run_index,
        filename=f"{stream_prefix}.stdout", label=f"{label}.stdout", require_read_only=require_read_only, allow_empty=True,
    )
    _live_artifact(
        record["stderr"], subject_commit=subject_commit, run_index=run_index,
        filename=f"{stream_prefix}.stderr", label=f"{label}.stderr", require_read_only=require_read_only, allow_empty=True,
    )
    return record


def _is_read_only(path: Path) -> bool:
    mode = path.stat().st_mode
    if os.name == "nt" and hasattr(path.stat(), "st_file_attributes"):
        return bool(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
    return not bool(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _validate_private_evidence_path(path: Path, expected_parent: Path, label: str) -> None:
    lexical_root = EVIDENCE_DIR.absolute()
    lexical_path = path.absolute()
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as error:
        raise ValueError(f"{label} escapes pending/evidence") from error
    current = lexical_root
    if _is_reparse_point(current):
        raise ValueError(f"{label} traverses a symlink/reparse point")
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            raise ValueError(f"{label} traverses a symlink/reparse point")
    if not path.is_file() or path.resolve(strict=True).parent != expected_parent.resolve(strict=True):
        raise ValueError(f"{label} is not a regular file in its exact private evidence directory")


def _live_artifact(
    value: object,
    *,
    subject_commit: str,
    run_index: int,
    filename: str,
    label: str,
    require_read_only: bool = False,
    allow_empty: bool = False,
) -> tuple[Path, bytes]:
    ref = _require_exact_keys(value, {"path", "sha256", "bytes"}, label)
    expected = f"private/{subject_commit}/run-{run_index}/{filename}"
    if ref["path"] != expected:
        raise ValueError(f"{label}.path must be {expected!r}")
    path = EVIDENCE_DIR / expected
    expected_parent = EVIDENCE_DIR / f"private/{subject_commit}/run-{run_index}"
    _validate_private_evidence_path(path, expected_parent, f"{label}.path")
    payload = path.read_bytes()
    if _require_int(ref["bytes"], f"{label}.bytes", minimum=0 if allow_empty else 1) != len(payload):
        raise ValueError(f"{label}.bytes does not match the retained file")
    if _require_sha256(ref["sha256"], f"{label}.sha256") != _sha256_bytes(payload):
        raise ValueError(f"{label}.sha256 does not match the retained file")
    if require_read_only and not _is_read_only(path):
        raise ValueError(f"{label} is not retained read-only after capture")
    return path, payload


def _live_capture_artifact(value: object, digest_value: object, *, subject_commit: str) -> tuple[Path, bytes]:
    ref = _require_exact_keys(value, {"path", "sha256", "bytes"}, "live capture")
    expected = f"private/{subject_commit}/capture.json"
    if ref["path"] != expected:
        raise ValueError("live capture path is not the frozen current-subject capture")
    path = EVIDENCE_DIR / expected
    expected_parent = EVIDENCE_DIR / f"private/{subject_commit}"
    _validate_private_evidence_path(path, expected_parent, "live capture")
    payload = path.read_bytes()
    if _require_int(ref["bytes"], "live capture.bytes", minimum=1) != len(payload) or _require_sha256(ref["sha256"], "live capture.sha256") != _sha256_bytes(payload):
        raise ValueError("live capture hash/size does not match the retained file")
    if not _is_read_only(path):
        raise ValueError("live capture manifest is not retained read-only")
    digest_ref = _require_exact_keys(digest_value, {"path", "sha256", "bytes"}, "live capture digest")
    digest_path = EVIDENCE_DIR / f"private/{subject_commit}/capture.sha256"
    if digest_ref["path"] != f"private/{subject_commit}/capture.sha256":
        raise ValueError("live capture digest path is invalid")
    _validate_private_evidence_path(digest_path, expected_parent, "live capture digest")
    digest_payload = digest_path.read_bytes()
    if (
        _require_int(digest_ref["bytes"], "live capture digest.bytes", minimum=1) != len(digest_payload)
        or _require_sha256(digest_ref["sha256"], "live capture digest.sha256") != _sha256_bytes(digest_payload)
        or digest_payload != (ref["sha256"] + "\n").encode("ascii")
        or not _is_read_only(digest_path)
    ):
        raise ValueError("live capture digest does not bind the retained read-only manifest")
    return path, payload


def _strict_json_payload(payload: bytes, label: str) -> dict[str, object]:
    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


class _LiveTranscriptPolicyError(RuntimeError, ValueError):
    """Fail-closed live policy error shared with the capture runner."""


def _load_e2e_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load release evaluator {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _export_session(
    payload: bytes | str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    adapter = _load_e2e_module(
        "_datasage_official_export_parser",
        ROOT / TRANSCRIPT_ADAPTER_PATH,
    )
    try:
        return adapter.parse_official_session_export(payload)
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise _LiveTranscriptPolicyError(str(error)) from error


def _live_turn_completion_policies(
    completion_policy: object, prompt_count: int
) -> list[dict[str, object]]:
    if not isinstance(completion_policy, list) or len(completion_policy) != prompt_count:
        raise _LiveTranscriptPolicyError(
            "turn completion policy must declare every Golden turn exactly once"
        )
    policies: list[dict[str, object]] = []
    keys = {
        "case_id",
        "turn",
        "assistant_completion",
        "maximum_blocking_clarify_calls",
    }
    for index, value in enumerate(completion_policy, 1):
        if not isinstance(value, dict) or set(value) != keys:
            raise _LiveTranscriptPolicyError(
                "turn completion policy entry has an invalid shape"
            )
        if (
            not isinstance(value["case_id"], str)
            or not value["case_id"]
            or value["turn"] != index
        ):
            raise _LiveTranscriptPolicyError(
                "turn completion policy is missing or out of Golden turn order"
            )
        if value["assistant_completion"] != "ordinary_text":
            raise _LiveTranscriptPolicyError(
                "live turns must end with ordinary assistant text"
            )
        if (
            type(value["maximum_blocking_clarify_calls"]) is not int
            or value["maximum_blocking_clarify_calls"] != 0
        ):
            raise _LiveTranscriptPolicyError(
                "blocking clarify tool calls must be forbidden for every live turn"
            )
        policies.append(value)
    return policies


def _live_without_transport_metadata(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    conversation: list[dict[str, object]] = []
    for item in messages:
        if item.get("function_call") is not None:
            raise _LiveTranscriptPolicyError(
                "legacy function_call is forbidden in live evidence"
            )
        role = item.get("role")
        if role == "session_meta":
            if not set(item).issubset(LIVE_SESSION_META_ALLOWED_FIELDS):
                raise _LiveTranscriptPolicyError(
                    "session_meta contains unknown fields"
                )
            if any(
                item.get(field) not in (None, "", [], {})
                for field in SESSION_META_CONVERSATIONAL_FIELDS
            ):
                raise _LiveTranscriptPolicyError(
                    "session_meta contains conversational or tool-flow payload"
                )
            continue
        if role not in {"system", "user", "assistant", "tool"}:
            raise _LiveTranscriptPolicyError(
                "official export contains an unsupported conversational role"
            )
        conversation.append(item)
    return conversation


def _validate_live_turn_tool_flow(segment: list[dict[str, object]]) -> None:
    pending: list[tuple[str, str, int]] = []
    completed: set[str] = set()
    for relative_index, message in enumerate(segment):
        role = message.get("role")
        tool_calls = message.get("tool_calls")
        if tool_calls not in (None, []):
            if role != "assistant" or not isinstance(tool_calls, list):
                raise _LiveTranscriptPolicyError(
                    "captured tool flow has an invalid tool-call carrier"
                )
            if pending:
                raise _LiveTranscriptPolicyError(
                    "pending tool calls must close before a new assistant tool-call batch"
                )
        if role == "system":
            continue
        if pending:
            call_id, function_name, call_index = pending[0]
            if role != "tool":
                raise _LiveTranscriptPolicyError(
                    "pending tool calls must be closed by the next non-system tool results"
                )
            observed_id = message.get("tool_call_id")
            if observed_id != call_id:
                if any(observed_id == item[0] for item in pending[1:]):
                    raise _LiveTranscriptPolicyError(
                        "tool results must close pending calls in declaration order"
                    )
                raise _LiveTranscriptPolicyError(
                    "captured tool result is not bound to the next pending tool call"
                )
            tool_name = message.get("tool_name")
            if (
                not isinstance(tool_name, str)
                or not tool_name
                or tool_name != function_name
            ):
                raise _LiveTranscriptPolicyError(
                    "captured tool result name does not match its tool call"
                )
            if call_index >= relative_index:
                raise _LiveTranscriptPolicyError(
                    "captured tool result precedes its tool call"
                )
            pending.pop(0)
            completed.add(call_id)
            continue
        if role == "tool":
            raise _LiveTranscriptPolicyError(
                "captured tool result is not bound to a prior tool call"
            )
        if tool_calls in (None, []):
            continue
        batch_ids: set[str] = set()
        for call in tool_calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            function = call.get("function") if isinstance(call, dict) else None
            function_name = function.get("name") if isinstance(function, dict) else None
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in batch_ids
                or call_id in completed
            ):
                raise _LiveTranscriptPolicyError(
                    "captured tool flow has an invalid or duplicate tool-call ID"
                )
            if not isinstance(function_name, str) or not function_name:
                raise _LiveTranscriptPolicyError(
                    "captured tool call has no valid function name"
                )
            if function_name == "datasage_push":
                raise _LiveTranscriptPolicyError(
                    "datasage_push is forbidden in inbound live evidence"
                )
            if function_name == "clarify":
                raise _LiveTranscriptPolicyError(
                    "blocking clarify tool calls are forbidden in live evidence"
                )
            if function_name not in LIVE_ALLOWED_TOOL_NAMES:
                raise _LiveTranscriptPolicyError(
                    "live evidence contains an unapproved tool call"
                )
            batch_ids.add(call_id)
            pending.append((call_id, function_name, relative_index))
    if pending:
        raise _LiveTranscriptPolicyError("captured turn has an unclosed tool flow")


def _live_endpoints(
    messages: list[dict[str, object]],
    prompts: list[str],
    completion_policy: object,
) -> list[tuple[int, int, str]]:
    _live_turn_completion_policies(completion_policy, len(prompts))
    conversation = _live_without_transport_metadata(messages)
    if [
        item.get("content") for item in conversation if item.get("role") == "user"
    ] != prompts:
        raise _LiveTranscriptPolicyError(
            "official export user turns are not exactly the two ordered Golden prompts"
        )
    try:
        first_user = next(
            index
            for index, item in enumerate(conversation)
            if item.get("role") == "user"
        )
    except StopIteration as error:
        raise _LiveTranscriptPolicyError(
            "official export contains no Golden user prompt"
        ) from error
    if any(item.get("role") != "system" for item in conversation[:first_user]):
        raise _LiveTranscriptPolicyError(
            "captured transcript has a non-system message before the first Golden prompt"
        )
    result: list[tuple[int, int, str]] = []
    start = 0
    for prompt in prompts:
        users = [
            index
            for index in range(start, len(conversation))
            if conversation[index].get("role") == "user"
            and conversation[index].get("content") == prompt
        ]
        if len(users) != 1:
            raise _LiveTranscriptPolicyError(
                "captured prompt is not unique in the official export"
            )
        user_index = users[0]
        next_user = next(
            (
                index
                for index in range(user_index + 1, len(conversation))
                if conversation[index].get("role") == "user"
            ),
            len(conversation),
        )
        segment = conversation[user_index + 1 : next_user]
        visible = [item for item in segment if item.get("role") != "system"]
        if not visible:
            raise _LiveTranscriptPolicyError(
                "captured turn has no terminal assistant answer"
            )
        final = visible[-1]
        if (
            final.get("role") != "assistant"
            or not isinstance(final.get("content"), str)
            or not final["content"]
            or final.get("tool_calls") not in (None, [])
        ):
            raise _LiveTranscriptPolicyError(
                "captured turn does not end in a terminal assistant answer"
            )
        _validate_live_turn_tool_flow(segment)
        user_id = conversation[user_index].get("id")
        final_id = final.get("id")
        if type(user_id) is not int or type(final_id) is not int or user_id >= final_id:
            raise _LiveTranscriptPolicyError("captured turn endpoints are invalid")
        result.append(
            (user_id, final_id, _sha256_bytes(final["content"].encode("utf-8")))
        )
        start = next_user
    return result


def _live_timestamp(value: object, label: str) -> datetime:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a timezone-aware timestamp")
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{label} must be a timezone-aware timestamp") from error
    else:
        raise ValueError(f"{label} must be a timezone-aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware timestamp")
    return parsed


def _subject_commit_timestamp(commit: str) -> datetime:
    _require_git_commit(commit, "live subject commit")
    value = str(_git_output(ROOT, "show", "-s", "--format=%cI", commit)).strip()
    return _live_timestamp(value, "subject commit timestamp")


def _validate_wecom_session_origin(
    exported: dict[str, object],
    session: dict[str, object],
    *,
    inbound: dict[str, object],
    subject_commit: str,
    run_index: int,
    subject_commit_timestamp: datetime,
) -> str:
    session_id = exported.get("id") or exported.get("session_id")
    user_id, chat_id = exported.get("user_id"), exported.get("chat_id")
    expected_title = f"datasage-live-{subject_commit[:12]}-run-{run_index}"
    started_at = _live_timestamp(exported.get("started_at"), "exported session.started_at")
    if not isinstance(session_id, str) or not session_id or any(character.isspace() for character in session_id):
        raise ValueError("captured export has no complete exact session ID")
    allowed_chat_types = {"dm"} if inbound.get("chat_type") == "dm" else {"dm", "group"}
    if exported.get("source") != "wecom" or exported.get("chat_type") not in allowed_chat_types:
        raise ValueError("capture is not an official WeCom DM inbound export")
    if not isinstance(user_id, str) or not user_id or not isinstance(chat_id, str) or not chat_id:
        raise ValueError("captured WeCom user/chat identity is incomplete")
    if "expected_user_id_sha256" in inbound:
        if _sha256_bytes(user_id.encode("utf-8")) != inbound["expected_user_id_sha256"]:
            raise ValueError("captured WeCom user identity does not match the anonymous contract pin")
        if _sha256_bytes(chat_id.encode("utf-8")) != inbound["expected_chat_id_sha256"]:
            raise ValueError("captured WeCom chat identity does not match the anonymous contract pin")
    elif exported.get("authenticated") is not True:
        raise ValueError("captured WeCom session lacks host authentication attestation")
    if exported.get("title") != expected_title or started_at < subject_commit_timestamp:
        raise ValueError("captured WeCom title/start time does not bind the subject run")
    expected_record = {
        "source": "wecom",
        "chat_type": exported["chat_type"],
        "user_id_sha256": _sha256_bytes(user_id.encode("utf-8")),
        "chat_id_sha256": _sha256_bytes(chat_id.encode("utf-8")),
        "title": expected_title,
        "started_at": started_at.isoformat(),
    }
    if "expected_user_id_sha256" not in inbound:
        expected_record["authenticated"] = exported["authenticated"]
    if any(session[key] != value for key, value in expected_record.items()):
        raise ValueError("capture session metadata differs from the official export")
    return session_id


def _reject_entitlement_denial(messages: list[dict[str, object]]) -> None:
    public_tools = {"datasage_catalog", "datasage_entity_resolve", "datasage_query", "datasage_push"}
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_name") not in public_tools:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("persisted DataSage tool result is not JSON text")
        payload = json.loads(content, object_pairs_hook=_strict_object, parse_constant=_reject_json_constant)
        if not isinstance(payload, dict):
            raise ValueError("persisted DataSage tool result is not an object")
        codes: set[str] = set()
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            codes.add(error["code"])
        results = payload.get("results")
        for result in results if isinstance(results, list) else []:
            error = result.get("error") if isinstance(result, dict) else None
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                codes.add(error["code"])
        bundle = payload.get("evidence_bundle")
        gaps = bundle.get("evidence_gaps") if isinstance(bundle, dict) else None
        for gap in gaps if isinstance(gaps, list) else []:
            if isinstance(gap, dict) and isinstance(gap.get("reason"), str):
                codes.add(gap["reason"])
        if "DATA_ENTITLEMENT_DENIED" in codes:
            raise ValueError("captured session contains DATA_ENTITLEMENT_DENIED")


def _validate_live_candidate_shape(candidate: dict[str, object]) -> None:
    _require_exact_keys(
        candidate,
        {"schema", "profile_artifact", "session_export_sha256", "cases", "canary_receipt"},
        "live candidate",
    )
    _require_exact_keys(candidate["profile_artifact"], {"profile_id", "artifact_id", "payload_sha256"}, "live candidate.profile_artifact")
    cases = candidate["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("live candidate must contain one or more cases")
    case_keys = {"id", "session_id", "session_lineage", "plan", "conclusions", "conclusion_review", "evidence"}
    plan_keys = {"domains", "metrics", "dimensions", "operations", "time_semantics", "context_action", "context_bindings"}
    evidence_keys = {"receipts", "successful_queries", "failed_queries", "truncated", "reconciled", "query_attempted", "error_codes"}
    review_keys = {"status", "reviewer_id_sha256", "assertion_sha256", "binding_sha256", "plan_trace_sha256"}
    for index, case in enumerate(cases):
        case = _require_exact_keys(
            case,
            case_keys | ({"decision_quality"} if "decision_quality" in case else set()),
            f"live candidate.cases[{index}]",
        )
        plan = _require_exact_keys(
            case["plan"],
            plan_keys | ({"domain_metric_pairs"} if "domain_metric_pairs" in case["plan"] else set()),
            f"live candidate.cases[{index}].plan",
        )
        evidence = _require_exact_keys(case["evidence"], evidence_keys, f"live candidate.cases[{index}].evidence")
        _require_int(evidence["successful_queries"], "candidate successful_queries")
        _require_int(evidence["failed_queries"], "candidate failed_queries")
        for key in ("truncated", "reconciled", "query_attempted"):
            _require_bool(evidence[key], f"candidate evidence.{key}")
        _require_exact_keys(
            case["conclusion_review"],
            review_keys | ({"decision_quality"} if "decision_quality" in case["conclusion_review"] else set()),
            f"live candidate.cases[{index}].conclusion_review",
        )
    receipt = _require_exact_keys(
        candidate["canary_receipt"],
        {"schema", "captured_at", "source", "profile_artifact", "session_export_sha256", "candidate_cases_sha256", "turns", "receipt_sha256"},
        "live candidate.canary_receipt",
    )
    source = _require_exact_keys(
        receipt["source"],
        {"platform", "format", "session_export_sha256"},
        "live candidate receipt source",
    )
    if source["format"] != "hermes_sessions_export_jsonl":
        raise ValueError("live candidate receipt source is not official Hermes JSONL")
    turns = receipt["turns"]
    if not isinstance(turns, list) or len(turns) != len(cases):
        raise ValueError("live candidate receipt turns must match candidate cases")
    turn_keys = {
        "test_id", "conversation_id", "turn", "session_id", "session_export_message_ids",
        "user_message_id", "canonical_prompt_sha256", "session_export_sha256",
        "watermark_sha256", "user_platform_message_id", "final_message_id",
        "final_platform_message_id", "final_answer_sha256", "transcript_sha256",
        "candidate_case_sha256", "conclusion_review", "fixture_attestation_sha256",
        "business_database_ref_sha256",
    }
    for index, turn in enumerate(turns):
        turn = _require_exact_keys(turn, turn_keys, f"live candidate receipt.turns[{index}]")
        _require_int(turn["turn"], "candidate receipt turn", minimum=1)
        _require_int(turn["user_message_id"], "candidate receipt user_message_id", minimum=1)
        _require_int(turn["final_message_id"], "candidate receipt final_message_id", minimum=1)
        ids = turn["session_export_message_ids"]
        if not isinstance(ids, list) or not ids or any(type(item) is not int or item < 1 for item in ids):
            raise ValueError("candidate receipt session_export_message_ids must be strict positive integers")
        _require_exact_keys(
            turn["conclusion_review"],
            review_keys | ({"decision_quality"} if "decision_quality" in turn["conclusion_review"] else set()),
            f"live candidate receipt.turns[{index}].conclusion_review",
        )


def _validate_live_reviews(
    payload: dict[str, object],
    *,
    run_index: int,
    case_ids: list[str],
    review_policy: dict[str, object],
    capture_sha256: str,
) -> dict[str, list[dict[str, object]]]:
    payload = _require_exact_keys(payload, {"schema", "run_index", "reviews"}, "live reviews")
    if payload["schema"] != "datasage-live-review-set/v1" or _require_int(payload["run_index"], "live reviews.run_index", minimum=1) != run_index:
        raise ValueError("live review set identity is invalid")
    reviews = payload["reviews"]
    trusted = review_policy["trusted_reviewer_id_sha256"]
    if not isinstance(reviews, list) or len(reviews) != len(case_ids) * 2:
        raise ValueError("live review set must contain two trusted reviews per case")
    result: dict[str, list[dict[str, object]]] = {case_id: [] for case_id in case_ids}
    keys = {"run_index", "case_id", "session_id_sha256", "final_answer_sha256", "capture_sha256", "reviewer_id", "labels", "evidence", "reviewed_at"}
    for index, value in enumerate(reviews):
        review = _require_exact_keys(
            value,
            keys | ({"decision_quality"} if isinstance(value, dict) and "decision_quality" in value else set()),
            f"live reviews[{index}]",
        )
        if _require_int(review["run_index"], "live review.run_index", minimum=1) != run_index:
            raise ValueError("live review is assigned to the wrong run")
        case_id = _require_string(review["case_id"], "live review.case_id")
        expected_case = case_ids[index // 2]
        expected_reviewer_sha = trusted[index % 2]
        if case_id != expected_case:
            raise ValueError("live reviews must follow exact case/reviewer order")
        _require_sha256(review["session_id_sha256"], "live review.session_id_sha256")
        _require_sha256(review["final_answer_sha256"], "live review.final_answer_sha256")
        if _require_sha256(review["capture_sha256"], "live review.capture_sha256") != capture_sha256:
            raise ValueError("live review is not anchored to the frozen capture manifest")
        reviewer_id = _require_string(review["reviewer_id"], "live review.reviewer_id")
        if _sha256_bytes(reviewer_id.encode("utf-8")) != expected_reviewer_sha:
            raise ValueError("live review is outside the contract-pinned reviewer boundary")
        labels = review["labels"]
        if not isinstance(labels, list) or any(not isinstance(item, str) or not item for item in labels) or len(labels) != len(set(labels)):
            raise ValueError("live review labels are invalid")
        if "decision_quality" in review:
            quality = review["decision_quality"]
            if (
                not isinstance(quality, dict)
                or set(quality) != DECISION_QUALITY_DIMENSIONS
                or any(type(quality[item]) is not int or quality[item] < 0 or quality[item] > 2 for item in quality)
            ):
                raise ValueError("live review decision_quality is invalid")
        evidence = review["evidence"]
        if not isinstance(evidence, list) or len(evidence) != len(labels):
            raise ValueError("live review must bind one answer range per conclusion")
        for evidence_index, item in enumerate(evidence):
            item = _require_exact_keys(item, {"label", "start", "end", "text_sha256"}, f"live review evidence[{evidence_index}]")
            if item["label"] != labels[evidence_index]:
                raise ValueError("live review evidence order differs from its conclusions")
            _require_int(item["start"], "live review evidence.start")
            _require_int(item["end"], "live review evidence.end", minimum=1)
            _require_sha256(item["text_sha256"], "live review evidence.text_sha256")
        reviewed_at = _require_string(review["reviewed_at"], "live review.reviewed_at")
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("live review reviewed_at must include a timezone")
        result[case_id].append(review)
    if any(len(pair) != 2 or pair[0]["reviewer_id"] == pair[1]["reviewer_id"] for pair in result.values()):
        raise ValueError("each live case requires two independent trusted reviewers")
    return result


def _review_consensus_id(reviews: list[dict[str, object]]) -> str:
    return "trusted-dual-review:" + _sha256_bytes(_canonical_json_bytes({
        "reviewer_id_sha256": [_sha256_bytes(str(review["reviewer_id"]).encode("utf-8")) for review in reviews],
        "labels": reviews[0]["labels"],
        "decision_quality": reviews[0].get("decision_quality"),
    }))


def _validate_review_evidence(review: dict[str, object], final_answer: str, required_labels: list[str]) -> None:
    if review["labels"] != required_labels:
        raise ValueError("trusted reviewer conclusions differ from the Golden/candidate conclusions")
    for item in review["evidence"]:
        start = _require_int(item["start"], "review evidence.start")
        end = _require_int(item["end"], "review evidence.end", minimum=1)
        if end <= start or end > len(final_answer):
            raise ValueError("review evidence range is outside the exact final answer")
        excerpt = final_answer[start:end]
        if len(excerpt.strip()) < 8 or _sha256_bytes(excerpt.encode("utf-8")) != item["text_sha256"]:
            raise ValueError("review evidence hash does not bind a substantive exact answer excerpt")


def _reject_internal_conclusion_codes(final_answer: str, golden: dict[str, object]) -> None:
    codes = set()
    for key in ("required_conclusions", "allowed_conclusions", "forbidden_conclusions"):
        values = golden.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"tracked Golden {key} is invalid")
        codes.update(values)
    if any(code in final_answer for code in codes):
        raise ValueError("final answer leaks a tracked internal conclusion code")


def _verify_live_candidate(
    adapter,
    candidate: dict[str, object],
    export_session: dict[str, object],
    messages: list[dict[str, object]],
    session_export_sha256: str,
    suite: dict[str, object],
    case_ids: list[str],
    subject: dict[str, str],
    reviews: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    scorer = _load_e2e_module("_datasage_release_scorer", ROOT / GOLDEN_SCORER_PATH)
    _validate_live_candidate_shape(candidate)
    if adapter.verify_receipt(candidate) is not True:
        raise ValueError("existing transcript adapter rejected the retained candidate receipt")
    receipt = candidate.get("canary_receipt")
    receipt_source = receipt.get("source") if isinstance(receipt, dict) else None
    if (
        _require_sha256(session_export_sha256, "session export identity")
        != session_export_sha256
        or candidate.get("session_export_sha256") != session_export_sha256
        or not isinstance(receipt, dict)
        or receipt.get("session_export_sha256") != session_export_sha256
        or not isinstance(receipt_source, dict)
        or receipt_source.get("session_export_sha256") != session_export_sha256
    ):
        raise ValueError("retained candidate does not bind the exact session export bytes")
    if candidate.get("profile_artifact") != {
        "profile_id": subject["name"],
        "artifact_id": subject["profile_git_commit"],
        "payload_sha256": subject["content_sha256"],
    }:
        raise ValueError("retained candidate is not bound to the release subject")
    session_id = export_session.get("id") or export_session.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("retained official export has no exact session ID")
    cases = candidate.get("cases")
    if not isinstance(receipt_source, dict) or receipt_source.get("platform") != "wecom" or export_session.get("source") != "wecom":
        raise ValueError("retained candidate transcript source is not WeCom inbound")
    turns = receipt.get("turns") if isinstance(receipt, dict) else None
    if not isinstance(cases, list) or not isinstance(turns, list):
        raise ValueError("retained candidate has no adapter cases/turns")
    case_by_id = {item.get("id"): item for item in cases if isinstance(item, dict)}
    turn_by_id = {item.get("test_id"): item for item in turns if isinstance(item, dict)}
    golden_by_id = {item.get("id"): item for item in suite.get("cases", []) if isinstance(item, dict)}
    if any(type(item.get("id")) is not int for item in messages):
        raise ValueError("retained official export message IDs must be strict integers")
    message_by_id = {item["id"]: item for item in messages}
    if len(message_by_id) != len(messages):
        raise ValueError("retained official export contains duplicate message IDs")
    if set(case_by_id) != set(case_ids) or set(turn_by_id) != set(case_ids):
        raise ValueError("retained adapter candidate does not contain the exact live cases")
    for case_id in case_ids:
        case = case_by_id[case_id]
        turn = turn_by_id[case_id]
        golden = golden_by_id.get(case_id)
        ids = turn.get("session_export_message_ids")
        if (
            not isinstance(golden, dict)
            or case.get("session_id") != session_id
            or turn.get("session_id") != session_id
            or turn.get("session_export_sha256") != session_export_sha256
            or not isinstance(ids, list)
            or not ids
            or any(type(item) is not int or item not in message_by_id for item in ids)
        ):
            raise ValueError("adapter turn is not bound to the retained exact session export")
        projection = [
            {
                key: message_by_id[message_id].get(key)
                for key in ("id", "role", "tool_name", "tool_call_id", "tool_calls", "content")
            }
            for message_id in ids
        ]
        user = message_by_id.get(turn.get("user_message_id"))
        final = message_by_id.get(turn.get("final_message_id"))
        prompt = golden.get("prompt")
        review_pair = reviews[case_id]
        if (
            not isinstance(user, dict)
            or user.get("role") != "user"
            or user.get("content") != prompt
            or not isinstance(final, dict)
            or final.get("role") != "assistant"
            or not isinstance(final.get("content"), str)
            or turn.get("canonical_prompt_sha256") != _sha256_bytes(str(prompt).encode("utf-8"))
            or turn.get("final_answer_sha256") != _sha256_bytes(final["content"].encode("utf-8"))
            or turn.get("transcript_sha256") != _sha256_bytes(_canonical_json_bytes(projection))
        ):
            raise ValueError("adapter receipt hashes do not match the retained official transcript")
        session_id_sha = _sha256_bytes(session_id.encode("utf-8"))
        required_labels = golden.get("required_conclusions")
        if not isinstance(required_labels, list) or case.get("conclusions") != required_labels:
            raise ValueError("candidate conclusions differ from the tracked Golden conclusions")
        _reject_internal_conclusion_codes(final["content"], golden)
        for review in review_pair:
            if review["session_id_sha256"] != session_id_sha or review["final_answer_sha256"] != turn["final_answer_sha256"]:
                raise ValueError("external review is not bound to this captured session/final answer")
            _validate_review_evidence(review, final["content"], required_labels)
        if _canonical_json_bytes(review_pair[0]["labels"]) != _canonical_json_bytes(review_pair[1]["labels"]):
            raise ValueError("trusted reviewers did not reach exact conclusion consensus")
        if _canonical_json_bytes(review_pair[0].get("decision_quality")) != _canonical_json_bytes(review_pair[1].get("decision_quality")):
            raise ValueError("trusted reviewers did not reach exact decision-quality consensus")
        consensus_reviewer_id = _review_consensus_id(review_pair)
        assertion = {
            "schema": "datasage-review-assertion/v3",
            "status": "reviewed",
            "test_id": case_id,
            "artifact_id": subject["profile_git_commit"],
            "payload_sha256": subject["content_sha256"],
            "session_id": session_id,
            "user_message_id": turn["user_message_id"],
            "canonical_prompt_sha256": turn["canonical_prompt_sha256"],
            "session_export_sha256": turn["session_export_sha256"],
            "watermark_sha256": turn["watermark_sha256"],
            "final_answer_sha256": turn["final_answer_sha256"],
            "reviewer_id": consensus_reviewer_id,
            "labels": required_labels,
            "fixture_attestation_sha256": turn["fixture_attestation_sha256"],
            "business_database_ref_sha256": turn["business_database_ref_sha256"],
        }
        if "decision_quality" in review_pair[0]:
            assertion["decision_quality"] = review_pair[0]["decision_quality"]
        binding = {
            key: assertion[key]
            for key in (
                "test_id", "artifact_id", "payload_sha256", "session_id",
                "user_message_id", "canonical_prompt_sha256", "session_export_sha256",
                "watermark_sha256", "final_answer_sha256", "fixture_attestation_sha256",
                "business_database_ref_sha256",
            )
        }
        expected_review = {
            "status": "reviewed",
            "reviewer_id_sha256": _sha256_bytes(consensus_reviewer_id.encode("utf-8")),
            "assertion_sha256": _sha256_bytes(_canonical_json_bytes(assertion)),
            "binding_sha256": _sha256_bytes(_canonical_json_bytes(binding)),
            "plan_trace_sha256": None,
        }
        if "decision_quality" in review_pair[0]:
            expected_review["decision_quality"] = review_pair[0]["decision_quality"]
        if _canonical_json_bytes(case["conclusion_review"]) != _canonical_json_bytes(expected_review) or _canonical_json_bytes(turn["conclusion_review"]) != _canonical_json_bytes(expected_review):
            raise ValueError("candidate review was not derived from the external post-capture review")
    selected = scorer.select_suite(suite, case_ids)
    score = scorer.score(selected, candidate)
    summary = score.get("summary")
    results = score.get("results")
    safety_score = score.get("safety_score")
    decision_quality_score = score.get("decision_quality_score")
    gate = score.get("gate")
    if (
        not isinstance(summary, dict)
        or summary.get("total") != len(case_ids)
        or summary.get("passed") != len(case_ids)
        or summary.get("failed") != 0
        or not isinstance(results, list)
        or [item.get("id") for item in results if isinstance(item, dict)] != case_ids
        or any(item.get("passed") is not True or item.get("errors") != [] for item in results if isinstance(item, dict))
        or not isinstance(safety_score, dict) or safety_score.get("passed") is not True
        or not isinstance(decision_quality_score, dict) or decision_quality_score.get("passed") is not True
        or not isinstance(gate, dict) or gate.get("passed") is not True
    ):
        raise ValueError("existing Golden scorer did not pass the retained candidate")
    return {"session_id": session_id, "score": score}


def _live_gate_blockers() -> list[dict[str, str]]:
    return [_blocker(code, message) for code, message in LIVE_BLOCKERS]


def _evaluate_live_evidence(
    evidence: dict[str, object] | None,
    contract: dict[str, object] | None,
    release_validation: dict[str, object],
    *,
    subject: dict[str, str],
    pinned_hermes: str,
    hermes_git_commit: str,
    strict_live: bool = True,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    assurance = {
        "review_assurance": "trusted_human_orchestrated_not_cryptographically_authenticated",
        "capture_integrity": "best_effort_not_same_user_adversarial",
    }
    empty = {
        "live_model_replay": {"status": "unverified_raw_evidence_required", "release_gate_status": "blocked", "case_count": 0, **assurance},
        "outbound_delivery": {"status": "missing", "evidence_semantics": "protocol_or_api_ack_not_user_read"},
        "stability": {"status": "missing", "completed_runs": 0},
        "live_replay_runs": {"status": "missing", "completed_runs": 0, "required_runs": 3},
    }
    if evidence is None:
        return empty, _live_gate_blockers()
    try:
        if contract is None:
            raise ValueError("tracked live release contract is missing")
        contract = _validate_live_contract(contract)
        report_keys = {
                "schema", "captured_at", "subject_commit_timestamp", "subject", "host", "contract", "producer",
                "golden_suite", "adapter", "scorer",
                "runtime_readiness_policy_sha256", "capture", "capture_digest",
                "python_provenance", "runs",
        }
        if strict_live:
            report_keys |= {"integrity_policy", "model_identity"}
        report = _require_exact_keys(evidence, report_keys, "live evidence")
        if report["schema"] != LIVE_EVIDENCE_SCHEMA:
            raise ValueError("live evidence schema is unsupported")
        if strict_live and report["integrity_policy"] != LIVE_INTEGRITY_POLICY:
            raise ValueError("live evidence integrity policy is missing")
        _validate_subject(report["subject"], subject)
        _validate_host(report["host"], pinned_version=pinned_hermes, expected_commit=hermes_git_commit)
        subject_commit = report["subject"]["profile_git_commit"]
        expected_model = _configured_model_identity() if strict_live else None
        common_model_identity = None
        if strict_live:
            common_model_identity = _require_exact_keys(
                report["model_identity"],
                {
                    "schema", "provider", "model", "model_fingerprint_sha256",
                    "system_prompt_sha256", "profile_content_sha256",
                },
                "live evidence.model_identity",
            )
            if common_model_identity.get("schema") != LIVE_RUN_IDENTITY_SCHEMA:
                raise ValueError("live evidence model identity schema is unsupported")
            if (common_model_identity.get("provider"), common_model_identity.get("model")) != expected_model:
                raise ValueError("live evidence model identity differs from config.yaml")
            _require_model_fingerprint(
                common_model_identity["model_fingerprint_sha256"],
                "live evidence.model_identity.model_fingerprint_sha256",
            )
            _require_sha256(
                common_model_identity["system_prompt_sha256"],
                "live evidence.model_identity.system_prompt_sha256",
            )
            if _require_sha256(
                common_model_identity["profile_content_sha256"],
                "live evidence.model_identity.profile_content_sha256",
            ) != subject["content_sha256"]:
                raise ValueError("live evidence model identity is not bound to the subject")
        source_bindings = (
            ("contract", report["contract"], "tests/fixtures/live_release_contract.json"),
            ("producer", report["producer"], LIVE_PRODUCER_PATH),
            ("golden_suite", report["golden_suite"], GOLDEN_SUITE_PATH),
            ("adapter", report["adapter"], TRANSCRIPT_ADAPTER_PATH),
            ("scorer", report["scorer"], GOLDEN_SCORER_PATH),
        )
        for label, value, expected_path in source_bindings:
            _validate_hashed_source(value, label=label, expected_path=expected_path, subject_commit=subject_commit)
        adapter = _load_e2e_module(
            "_datasage_release_adapter", ROOT / TRANSCRIPT_ADAPTER_PATH
        )
        if contract["subject"] != {"name": subject["name"], "version": subject["version"]}:
            raise ValueError("live contract subject does not match the current candidate")
        if contract["host"] != {"hermes_version": pinned_hermes, "hermes_git_commit": hermes_git_commit}:
            raise ValueError("live contract host does not match the pinned Hermes checkout")
        trusted = release_validation.get("trusted_replay_gate")
        trusted = trusted if isinstance(trusted, dict) else {}
        plan = contract["case_plan"]
        if release_validation.get("release_target") != subject["version"]:
            raise ValueError("Golden release target does not match the candidate")
        if trusted.get("binding_schema") != adapter.LIVE_BINDING_SCHEMA:
            raise ValueError("Golden trusted replay binding schema is unsupported")
        if trusted.get("case_ids") != plan["case_ids"] or trusted.get("required_replay_runs_per_case") != plan["runs"]:
            raise ValueError("Golden trusted replay requirements do not match the live contract")
        readiness_sha = _require_sha256(report["runtime_readiness_policy_sha256"], "runtime_readiness_policy_sha256")
        if readiness_sha != _sha256_bytes(_canonical_json_bytes(contract["runtime_readiness_policy"])):
            raise ValueError("runtime readiness policy hash does not match the tracked policy")
        runs = report["runs"]
        if not isinstance(runs, list) or len(runs) != plan["runs"]:
            raise ValueError("live evidence does not contain exactly three finalized runs")
        _, capture_payload = _live_capture_artifact(report["capture"], report["capture_digest"], subject_commit=subject_commit)
        capture_keys = {
            "schema", "captured_at", "subject_commit_timestamp", "subject",
            "host", "contract", "python_provenance", "runs",
        }
        if strict_live:
            capture_keys |= {"integrity_policy", "model_identity"}
        capture = _require_exact_keys(
            _strict_json_payload(capture_payload, "live capture"),
            capture_keys,
            "live capture",
        )
        if (
            capture["schema"] != "datasage-live-capture/v2"
            or capture["captured_at"] != report["captured_at"]
            or capture["subject_commit_timestamp"] != report["subject_commit_timestamp"]
            or _canonical_json_bytes(capture["subject"]) != _canonical_json_bytes(report["subject"])
            or _canonical_json_bytes(capture["host"]) != _canonical_json_bytes(report["host"])
            or _canonical_json_bytes(capture["contract"]) != _canonical_json_bytes(report["contract"])
            or (
                strict_live
                and (
                    capture["integrity_policy"] != LIVE_INTEGRITY_POLICY
                    or _canonical_json_bytes(capture["model_identity"])
                    != _canonical_json_bytes(report["model_identity"])
                )
            )
        ):
            raise ValueError("frozen capture is not bound to this subject/host/contract")
        captured_at = _live_timestamp(capture["captured_at"], "capture.captured_at")
        subject_commit_timestamp = _live_timestamp(
            capture["subject_commit_timestamp"], "capture.subject_commit_timestamp"
        )
        if subject_commit_timestamp != _subject_commit_timestamp(subject_commit) or captured_at < subject_commit_timestamp:
            raise ValueError("capture timestamps do not bind the actual subject commit")
        common_model_identity = None
        expected_model = None
        if strict_live:
            common_model_identity = _require_exact_keys(
                capture["model_identity"],
                {
                    "schema", "provider", "model", "model_fingerprint_sha256",
                    "system_prompt_sha256", "profile_content_sha256",
                },
                "live capture.model_identity",
            )
            if common_model_identity["schema"] != LIVE_RUN_IDENTITY_SCHEMA:
                raise ValueError("live capture model identity schema is unsupported")
            expected_model = _configured_model_identity()
            if (common_model_identity["provider"], common_model_identity["model"]) != expected_model:
                raise ValueError("live capture model identity differs from config.yaml")
            _require_model_fingerprint(
                common_model_identity["model_fingerprint_sha256"],
                "live capture.model_identity.model_fingerprint_sha256",
            )
            _require_sha256(
                common_model_identity["system_prompt_sha256"],
                "live capture.model_identity.system_prompt_sha256",
            )
            if _require_sha256(
                common_model_identity["profile_content_sha256"],
                "live capture.model_identity.profile_content_sha256",
            ) != subject["content_sha256"]:
                raise ValueError("live capture model identity is not bound to the subject")
        capture_python = _require_exact_keys(capture["python_provenance"], {"before", "after"}, "capture Python provenance")
        _validate_python_provenance(capture_python["before"], "capture Python provenance.before", contract["python_provenance_approval"])
        _validate_python_provenance(capture_python["after"], "capture Python provenance.after", contract["python_provenance_approval"])
        finalize_python = _require_exact_keys(report["python_provenance"], {"before", "after"}, "finalize Python provenance")
        _validate_python_provenance(finalize_python["before"], "finalize Python provenance.before", contract["python_provenance_approval"])
        _validate_python_provenance(finalize_python["after"], "finalize Python provenance.after", contract["python_provenance_approval"])
        captures = capture["runs"]
        if not isinstance(captures, list) or len(captures) != plan["runs"]:
            raise ValueError("frozen capture does not contain exactly three runs")
        suite = _read_strict_json(GOLDEN_SUITE)
        golden_by_id = {item.get("id"): item for item in suite.get("cases", []) if isinstance(item, dict)}
        command_shapes = contract["execution"]["command_shapes"]
        python = str(_canonical_hermes_python())
        expected_indices = set(range(1, plan["runs"] + 1))
        seen_indices: set[int] = set()
        lineages: set[str] = set()
        fresh_nonces: set[str] = set()
        capture_by_index = {}
        for position, value in enumerate(captures):
            capture_run_keys = {
                "run_index", "case_ids", "processes", "session", "artifact_set_sha256",
            }
            if strict_live:
                capture_run_keys.add("run_identity")
            captured = _require_exact_keys(value, capture_run_keys, f"capture.runs[{position}]")
            index = _require_int(captured["run_index"], "capture run_index", minimum=1)
            if index in capture_by_index or captured["case_ids"] != plan["case_ids"]:
                raise ValueError("frozen capture has duplicate/mismatched runs")
            capture_by_index[index] = captured
        for position, value in enumerate(runs):
            run = _require_exact_keys(
                value,
                {
                    "run_index", "case_ids", "processes", "bindings", "candidate",
                    "score_report", "reviews",
                    *( {"run_identity"} if strict_live else set() ),
                },
                f"runs[{position}]",
            )
            run_index = _require_int(run["run_index"], f"runs[{position}].run_index", minimum=1)
            if run_index in seen_indices or run_index not in capture_by_index:
                raise ValueError("duplicate or uncaptured finalized run")
            seen_indices.add(run_index)
            if run["case_ids"] != plan["case_ids"]:
                raise ValueError("finalized run case order differs from the contract")
            captured = capture_by_index[run_index]
            if strict_live and run.get("run_identity") != captured.get("run_identity"):
                raise ValueError("finalized run identity differs from its capture")
            session = _require_exact_keys(
                captured["session"],
                {"source", "chat_type", "user_id_sha256", "chat_id_sha256", "title", "started_at", "export_format", "turns", "lineage_sha256", "session_id_sha256", "final_answer_sha256", "export"},
                f"capture.runs[{position}].session",
            )
            if session["export_format"] != "jsonl" or _require_int(session["turns"], "capture turns") != plan["turns_per_session"]:
                raise ValueError("capture turn count does not match the release case plan")
            _, export_payload = _live_artifact(session["export"], subject_commit=subject_commit, run_index=run_index, filename="session.jsonl", label="captured session export", require_read_only=True)
            exported, messages = _export_session(export_payload)
            session_id = _validate_wecom_session_origin(
                exported,
                session,
                inbound=contract["inbound"],
                subject_commit=subject_commit,
                run_index=run_index,
                subject_commit_timestamp=subject_commit_timestamp,
            )
            if strict_live:
                exported_identity = _live_run_identity_from_export(
                    exported,
                    subject=subject,
                    expected_model=expected_model,
                )
                if exported_identity != captured.get("run_identity"):
                    raise ValueError("captured run identity is not present in the official export")
            session_sha = _sha256_bytes(session_id.encode("utf-8"))
            lineage = _sha256_bytes(_canonical_json_bytes([session_id]))
            if session["session_id_sha256"] != session_sha or session["lineage_sha256"] != lineage or lineage in lineages:
                raise ValueError("captured session identity/lineage is invalid or reused")
            lineages.add(lineage)
            if strict_live:
                run_identity = _validate_live_run_identity(
                    captured["run_identity"],
                    subject=subject,
                    expected_model=expected_model,
                )
                if {
                    key: run_identity[key]
                    for key in common_model_identity
                } != common_model_identity:
                    raise ValueError("live run model identity differs from the common identity")
                if run_identity["fresh_run_nonce"] in fresh_nonces:
                    raise ValueError("live runs reuse a fresh-run nonce")
                fresh_nonces.add(run_identity["fresh_run_nonce"])
            expected_user_prompts = [golden_by_id[case_id]["prompt"] for case_id in plan["case_ids"]]
            endpoints = _live_endpoints(
                messages,
                expected_user_prompts,
                contract["turn_completion_policy"],
            )
            _reject_entitlement_denial(messages)
            final_hashes = [item[2] for item in endpoints]
            if session["final_answer_sha256"] != final_hashes:
                raise ValueError("capture final-answer hashes do not match the official export")
            capture_processes = _require_exact_keys(captured["processes"], {"session_export"}, "capture processes")
            capture_bindings = {
                "session_export": {"python": python, "exact_session_id": session_id},
            }
            capture_records = {
                name: _validate_process_record(
                    capture_processes[name], command_shapes[name], f"capture process {name}",
                    expected_bindings=capture_bindings[name], subject_commit=subject_commit,
                    run_index=run_index, stream_prefix=name.replace("_", "-"), require_read_only=True,
                )
                for name in capture_processes
            }
            artifact_refs = [
                capture_records["session_export"]["stdout"],
                capture_records["session_export"]["stderr"],
            ]
            artifact_refs.append(session["export"])
            if strict_live:
                artifact_refs.append(captured["run_identity"])
            if _require_sha256(captured["artifact_set_sha256"], "capture artifact_set_sha256") != _sha256_bytes(_canonical_json_bytes(artifact_refs)):
                raise ValueError("capture artifact-set digest does not close over every retained input/stream/export")
            if capture_records["session_export"]["stdout"]["sha256"] != session["export"]["sha256"]:
                raise ValueError("capture export stdout differs from the retained export")
            _, reviews_payload = _live_artifact(run["reviews"], subject_commit=subject_commit, run_index=run_index, filename="reviews.json", label="external reviews")
            reviews = _validate_live_reviews(
                _strict_json_payload(reviews_payload, "external reviews"), run_index=run_index,
                case_ids=list(plan["case_ids"]), review_policy=contract["review_policy"],
                capture_sha256=report["capture"]["sha256"],
            )
            _, bindings_payload = _live_artifact(
                run["bindings"], subject_commit=subject_commit, run_index=run_index,
                filename="bindings.json", label="adapter bindings",
            )
            bindings = _strict_json_payload(bindings_payload, "adapter bindings")
            if strict_live and (
                bindings.get("integrity_policy") != LIVE_INTEGRITY_POLICY
            ):
                raise ValueError("live adapter bindings lack the strict integrity policy")
            rebuilt_candidate = adapter.adapt(export_payload, bindings)
            _, candidate_payload = _live_artifact(run["candidate"], subject_commit=subject_commit, run_index=run_index, filename="candidate.json", label="adapter candidate")
            candidate = _strict_json_payload(candidate_payload, "adapter candidate")
            if _canonical_json_bytes(candidate) != _canonical_json_bytes(rebuilt_candidate):
                raise ValueError(
                    "retained candidate differs from the exact export/bindings adapter rebuild"
                )
            verified = _verify_live_candidate(
                adapter,
                candidate,
                exported,
                messages,
                session["export"]["sha256"],
                suite,
                list(plan["case_ids"]),
                report["subject"],
                reviews,
            )
            _, score_payload = _live_artifact(run["score_report"], subject_commit=subject_commit, run_index=run_index, filename="score.json", label="score report")
            if _canonical_json_bytes(_strict_json_payload(score_payload, "score report")) != _canonical_json_bytes(verified["score"]):
                raise ValueError("retained scorer report differs from builder-recomputed Golden score")
            processes = _require_exact_keys(run["processes"], {"adapter", "scorer"}, "finalize processes")
            run_dir = EVIDENCE_DIR / f"private/{subject_commit}/run-{run_index}"
            finalize_bindings = {
                "adapter": {"python": python, "adapter": str(ROOT / TRANSCRIPT_ADAPTER_PATH), "session_export": str(run_dir / "session.jsonl"), "bindings": str(run_dir / "bindings.json"), "candidate": str(run_dir / "candidate.json")},
                "scorer": {"python": python, "scorer": str(ROOT / GOLDEN_SCORER_PATH), "golden_suite": str(ROOT / GOLDEN_SUITE_PATH), "case_1": plan["case_ids"][0], "case_2": plan["case_ids"][1], "candidate": str(run_dir / "candidate.json"), "score_report": str(run_dir / "score.json")},
            }
            finalize_records = {
                name: _validate_process_record(
                    processes[name], command_shapes[name], f"finalize process {name}",
                    expected_bindings=finalize_bindings[name], subject_commit=subject_commit,
                    run_index=run_index, stream_prefix=name,
                )
                for name in processes
            }
        if seen_indices != expected_indices or set(capture_by_index) != expected_indices:
            raise ValueError("one or more required capture/finalize runs are missing")
        live_result = {
            "live_model_replay": {
                "status": "passed",
                "release_gate_status": "passed",
                "case_count": len(plan["case_ids"]),
                **assurance,
            },
            "outbound_delivery": {"status": "missing", "evidence_semantics": "protocol_or_api_ack_not_user_read"},
            "stability": {"status": "passed", "completed_runs": plan["runs"]},
            "live_replay_runs": {"status": "complete", "completed_runs": plan["runs"], "required_runs": plan["runs"]},
        }
        if strict_live:
            attestation_input = [
                {"run_index": run["run_index"], "reviews": run["reviews"]}
                for run in runs
            ]
            live_result["live_model_replay"] = {
                **live_result["live_model_replay"],
                "model_identity": common_model_identity,
                "reviewer_attestation": {
                    "schema": REVIEWER_ATTESTATION_SCHEMA,
                    "sha256": _reviewer_attestation_digest(attestation_input),
                },
            }
        return live_result, [_blocker("OUTBOUND_DELIVERY_NOT_VERIFIED", "Raw outbound protocol/API ACK evidence is not available.")]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        invalid = {
            **empty,
            "live_model_replay": {**empty["live_model_replay"], "status": "invalid", "reason": str(error)},
            "outbound_delivery": empty["outbound_delivery"],
            "stability": {**empty["stability"], "status": "invalid"},
            "live_replay_runs": {**empty["live_replay_runs"], "status": "invalid"},
        }
        return invalid, [_blocker("LIVE_RELEASE_EVIDENCE_INVALID", "Raw live release evidence is invalid."), *_live_gate_blockers()]


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
    live_evidence: dict[str, object] | None = None,
    live_contract: dict[str, object] | None = None,
    profile_worktree_clean: bool = True,
    hermes_worktree_clean: bool = True,
    strict_live: bool = True,
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
    adapter = _load_e2e_module(
        "_datasage_release_contract_adapter", ROOT / TRANSCRIPT_ADAPTER_PATH
    )
    valid_live_contract = type(required_runs) is int and required_runs >= 1 and isinstance(case_ids, list) and bool(case_ids)
    valid_live_contract = valid_live_contract and all(isinstance(case_id, str) and case_id for case_id in case_ids) and len(case_ids) == len(set(case_ids))
    valid_live_contract = (
        valid_live_contract
        and trusted.get("binding_schema") == adapter.LIVE_BINDING_SCHEMA
    )
    if target != version:
        blockers.append(_blocker("LIVE_REPLAY_TARGET_MISMATCH", "Live replay requirements do not target the candidate version."))
    if not valid_live_contract:
        blockers.append(_blocker("LIVE_REPLAY_RUN_CONTRACT_INVALID", "Trusted replay run requirements are incomplete."))
    distribution = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    pinned_hermes = str(distribution.get("hermes_requires", "")).removeprefix("==")
    commit = profile_git_commit or (subject or {}).get("profile_git_commit")
    expected_subject = subject or {"name": str(distribution.get("name", "")), "version": version, "content_sha256": "", "profile_git_commit": str(commit or "")}
    actual_hermes_commit = hermes_git_commit or ""
    if (host_evidence is not None or performance_evidence is not None or live_evidence is not None) and not actual_hermes_commit:
        actual_hermes_commit = _git_commit(_hermes_source_root())
    host_result, host_blockers = _evaluate_host_evidence(host_evidence, host_fixture, subject=expected_subject, pinned_hermes=pinned_hermes, hermes_git_commit=actual_hermes_commit)
    performance_result, performance_blockers = _evaluate_performance_evidence(performance_evidence, performance_contract, subject=expected_subject, pinned_hermes=pinned_hermes, hermes_git_commit=actual_hermes_commit)
    live_result, live_blockers = _evaluate_live_evidence(
        live_evidence,
        live_contract,
        release_validation,
        subject=expected_subject,
        pinned_hermes=pinned_hermes,
        hermes_git_commit=actual_hermes_commit,
        strict_live=strict_live,
    )
    blockers.extend(live_blockers)
    blockers.extend(host_blockers)
    blockers.extend(performance_blockers)
    return {
        **live_result,
        "live_model_replay": {**live_result["live_model_replay"], "target": target},
        "host_compaction": host_result,
        "performance_cost": performance_result,
        "blockers": blockers,
    }


def _run_offline_tests() -> dict[str, object]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "reason_code": "OFFLINE_TEST_TIMEOUT",
            "test_count": None,
            "skipped_count": None,
            "expected_failure_count": None,
            "skipped_tests": [],
            "expected_failure_tests": [],
        }
    combined = f"{completed.stdout}\n{completed.stderr}"
    matched = re.search(r"Ran\s+(\d+)\s+tests?", combined)
    skipped_tests = sorted(set(re.findall(
        r"(?m)^\s*(test_[A-Za-z0-9_]+)\s+\([^\r\n]+\)\s+\.\.\.\s+skipped\b",
        combined,
    )))
    expected_failure_tests = sorted(set(re.findall(
        r"(?mi)^\s*(test_[A-Za-z0-9_]+)\s+\([^\r\n]+\)\s+\.\.\.\s+expected failure\b",
        combined,
    )))
    summary_skipped = re.search(r"skipped=(\d+)", combined)
    summary_expected = re.search(r"expected failures=(\d+)", combined)
    test_count = int(matched.group(1)) if matched else None
    skipped_count = int(summary_skipped.group(1)) if summary_skipped else len(skipped_tests)
    expected_failure_count = (
        int(summary_expected.group(1))
        if summary_expected
        else len(expected_failure_tests)
    )
    reason_code = None
    if completed.returncode != 0:
        reason_code = "OFFLINE_TEST_FAILED"
    elif test_count is None:
        reason_code = "OFFLINE_TEST_SUMMARY_MISSING"
    elif test_count < 1:
        reason_code = "OFFLINE_TEST_ZERO"
    elif skipped_count != len(skipped_tests) or (
        skipped_count and not skipped_tests
    ):
        reason_code = "OFFLINE_TEST_SKIP_SUMMARY_UNRESOLVED"
    elif expected_failure_count != len(expected_failure_tests) or (
        set(expected_failure_tests) - OFFLINE_ALLOWED_EXPECTED_FAILURES
    ):
        reason_code = "OFFLINE_TEST_EXPECTED_FAILURE"
    elif set(skipped_tests) - OFFLINE_ALLOWED_SKIPS:
        reason_code = "OFFLINE_TEST_SKIP_NOT_ALLOWLISTED"
    return {
        "status": "passed" if reason_code is None else "failed",
        "reason_code": reason_code,
        "test_count": test_count,
        "skipped_count": skipped_count,
        "expected_failure_count": expected_failure_count,
        "skipped_tests": skipped_tests,
        "expected_failure_tests": expected_failure_tests,
    }


def _load_metric_governance_module():
    """Load the single plugin governance parser without registering tools."""

    package_name = "_datasage_release_metric_governance"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(DATASAGE_PLUGIN_ROOT)]
        sys.modules[package_name] = package
    module = importlib.import_module(f"{package_name}.metric_governance")
    origin = Path(str(getattr(module, "__file__", ""))).resolve(strict=True)
    try:
        origin.relative_to(DATASAGE_PLUGIN_ROOT.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("metric governance parser origin is outside the Profile") from exc
    return module


def _evaluate_metric_governance() -> tuple[dict[str, object], list[dict[str, str]]]:
    """Evaluate release-only governance without changing runtime availability."""

    try:
        module = _load_metric_governance_module()
        contract = module.load_contract()
        summary = contract.get("summary")
        digest = contract.get("contract_sha256")
        if (
            not isinstance(summary, dict)
            or set(summary)
            != {
                "metric_count",
                "availability",
                "lifecycle",
                "review",
                "execution",
                "release",
                "blocker_counts",
            }
            or type(summary.get("metric_count")) is not int
            or summary["metric_count"] < 1
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or any(
                not isinstance(summary.get(key), dict)
                for key in (
                    "availability",
                    "lifecycle",
                    "review",
                    "execution",
                    "release",
                    "blocker_counts",
                )
            )
        ):
            raise ValueError("metric governance summary is invalid")
    except Exception as exc:
        code = getattr(exc, "code", "METRIC_GOVERNANCE_CONTRACT_INVALID")
        blocker_code = (
            "METRIC_GOVERNANCE_COVERAGE_MISMATCH"
            if code == "METRIC_GOVERNANCE_COVERAGE_MISMATCH"
            else "METRIC_GOVERNANCE_CONTRACT_INVALID"
        )
        return (
            {
                "status": "invalid",
                "contract_sha256": None,
                "metric_count": None,
                "availability": {},
                "lifecycle": {},
                "review": {},
                "execution": {},
                "release": {},
                "blocker_counts": {},
                "reason_code": blocker_code,
            },
            [_blocker(blocker_code, "Metric governance contract is invalid.")],
        )

    blocker_counts = dict(summary["blocker_counts"])
    supported_blockers = {
        "METRIC_GOVERNANCE_OWNER_MISSING": "Metric governance owners are incomplete.",
        "METRIC_GOVERNANCE_REVIEW_MISSING": "Metric governance reviews are incomplete.",
        "METRIC_GOVERNANCE_REVIEW_OVERDUE": "Metric governance reviews are overdue.",
        "METRIC_GOVERNANCE_INTERVAL_INVALID": "Metric governance review intervals are incomplete or invalid.",
        "METRIC_GOVERNANCE_REVIEW_INVALID": "Metric governance review state is invalid.",
        "METRIC_GOVERNANCE_VALIDATION_PENDING": "One or more metrics remain pending validation.",
        "METRIC_GOVERNANCE_METRIC_RETIRED": "One or more metrics are retired.",
    }
    blockers = [
        _blocker(code, message)
        for code, message in supported_blockers.items()
        if type(blocker_counts.get(code)) is int and blocker_counts[code] > 0
    ]
    release_counts = summary["release"]
    if (
        type(release_counts.get("pass")) is not int
        or type(release_counts.get("block")) is not int
        or release_counts["pass"] + release_counts["block"]
        != summary["metric_count"]
    ):
        return (
            {
                "status": "invalid",
                "contract_sha256": digest,
                "metric_count": summary["metric_count"],
                "availability": dict(summary["availability"]),
                "lifecycle": dict(summary["lifecycle"]),
                "review": dict(summary["review"]),
                "execution": dict(summary["execution"]),
                "release": dict(release_counts),
                "blocker_counts": blocker_counts,
                "reason_code": "METRIC_GOVERNANCE_CONTRACT_INVALID",
            },
            [
                _blocker(
                    "METRIC_GOVERNANCE_CONTRACT_INVALID",
                    "Metric governance release totals are invalid.",
                )
            ],
        )
    report = {
        "status": "passed" if not blockers else "blocked",
        "contract_sha256": digest,
        "metric_count": summary["metric_count"],
        "availability": dict(summary["availability"]),
        "lifecycle": dict(summary["lifecycle"]),
        "review": dict(summary["review"]),
        "execution": dict(summary["execution"]),
        "release": dict(release_counts),
        "blocker_counts": blocker_counts,
        "reason_code": None if not blockers else "METRIC_GOVERNANCE_INCOMPLETE",
    }
    return report, blockers


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
    try:
        live_evidence = _discover_evidence("live", profile_git_commit)
    except (OSError, ValueError, json.JSONDecodeError):
        live_evidence = {"schema": "invalid-live-evidence"}
    performance_contract = (
        _read_strict_json(PERFORMANCE_CONTRACT)
        if PERFORMANCE_CONTRACT.is_file()
        else None
    )
    live_contract = (
        _read_strict_json(LIVE_RELEASE_CONTRACT)
        if LIVE_RELEASE_CONTRACT.is_file()
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
        live_evidence=live_evidence,
        live_contract=live_contract,
        profile_worktree_clean=profile_worktree_clean,
        hermes_worktree_clean=hermes_worktree_clean,
    )
    blockers = list(gates.pop("blockers"))
    governance_report, governance_blockers = _evaluate_metric_governance()
    blockers.extend(governance_blockers)
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
    result = {
        "schema": "datasage-release-eligibility/v1",
        "name": actual.get("name"),
        "version": actual.get("version"),
        "eligible": not blockers,
        "identity": identity,
        "offline_tests": offline,
        "metric_governance": governance_report,
        **gates,
        "blockers": blockers,
        "interpretation": (
            "release_eligible"
            if not blockers
            else "offline tests may pass while live release gates remain blocked"
        ),
    }
    result["gate_report_sha256"] = _gate_report_digest(result)
    return result


def _candidate_artifact_reference(path: Path) -> dict[str, object]:
    resolved, location_error = _receipt_location(path, kind="candidate")
    if location_error or resolved is None:
        raise ValueError(location_error or "CANDIDATE_RECEIPT_PATH_INVALID")
    payload = resolved.read_bytes()
    _read_strict_json(resolved)
    return {
        "path": resolved.relative_to(ROOT).as_posix(),
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
    }


def _promotion_model_identity(eligibility: dict[str, object]) -> dict[str, str]:
    live = eligibility.get("live_model_replay")
    if not isinstance(live, dict):
        raise ValueError("promotion is missing live model identity")
    identity = _require_exact_keys(
        live.get("model_identity"),
        {
            "schema", "provider", "model", "model_fingerprint_sha256",
            "system_prompt_sha256", "profile_content_sha256",
        },
        "promotion model identity",
    )
    if identity["schema"] != LIVE_RUN_IDENTITY_SCHEMA:
        raise ValueError("promotion model identity schema is unsupported")
    _require_string(identity["provider"], "promotion model identity.provider")
    _require_string(identity["model"], "promotion model identity.model")
    for key in ("model_fingerprint_sha256", "system_prompt_sha256", "profile_content_sha256"):
        if key == "model_fingerprint_sha256":
            _require_model_fingerprint(identity[key], f"promotion model identity.{key}")
        else:
            _require_sha256(identity[key], f"promotion model identity.{key}")
    return {key: str(value) for key, value in identity.items()}


def _promotion_reviewer_attestation(eligibility: dict[str, object]) -> dict[str, str]:
    live = eligibility.get("live_model_replay")
    if not isinstance(live, dict):
        raise ValueError("promotion is missing reviewer attestation")
    attestation = _require_exact_keys(
        live.get("reviewer_attestation"),
        {"schema", "sha256"},
        "promotion reviewer attestation",
    )
    if attestation["schema"] != REVIEWER_ATTESTATION_SCHEMA:
        raise ValueError("promotion reviewer attestation schema is unsupported")
    _require_sha256(attestation["sha256"], "promotion reviewer attestation.sha256")
    return {key: str(value) for key, value in attestation.items()}


def _write_final_receipt_atomic(path: Path, payload: dict[str, object]) -> None:
    path = path.resolve()
    if path.parent != RELEASE_DIR.resolve() or not path.match(FINAL_RECEIPT_GLOB):
        raise ValueError("final receipt path is outside the release namespace")
    if path.exists():
        raise FileExistsError("final receipt already exists")
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=RELEASE_DIR,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError("final receipt appeared during promotion")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def promote_candidate(*, receipt_path: Path, offline_runner=None) -> dict[str, object]:
    """Promote one explicit, fully eligible candidate through an atomic write."""

    resolved, location_error = _receipt_location(receipt_path, kind="candidate")
    if location_error or resolved is None:
        raise ValueError(location_error or "CANDIDATE_RECEIPT_PATH_INVALID")
    eligibility = verify_candidate(receipt_path=resolved, offline_runner=offline_runner)
    if eligibility.get("eligible") is not True:
        raise ValueError("candidate is not eligible for promotion")
    actual = build_receipt()
    if check_release_identity(kind="candidate", receipt_path=resolved).get("status") != "verified":
        raise ValueError("candidate changed during promotion")
    candidate_ref = _candidate_artifact_reference(resolved)
    subject = {
        "name": str(actual["name"]),
        "version": str(actual["version"]),
        "content_sha256": str(actual["content_sha256"]),
        "profile_git_commit": _git_commit(ROOT),
    }
    pinned_hermes = str(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")).get("hermes_requires", "")).removeprefix("==")
    hermes_root = _hermes_source_root()
    hermes = {
        "hermes_version": pinned_hermes,
        "hermes_git_commit": _git_commit(hermes_root),
    }
    model = _promotion_model_identity(eligibility)
    reviewer = _promotion_reviewer_attestation(eligibility)
    gate_digest = _require_sha256(
        eligibility.get("gate_report_sha256"),
        "promotion gate_report_sha256",
    )
    final_name = (
        f"{subject['name']}-{subject['version']}-"
        f"{subject['content_sha256'][:16]}-release-receipt.json"
    )
    if re.fullmatch(r"[A-Za-z0-9_.-]+", final_name) is None:
        raise ValueError("promotion produced an invalid final receipt name")
    final_path = RELEASE_DIR / final_name
    final = {
        "schema": FINAL_RECEIPT_SCHEMA,
        "name": subject["name"],
        "version": subject["version"],
        "subject": subject,
        "candidate": candidate_ref,
        "gate_report_sha256": gate_digest,
        "evaluator": {
            "path": "build_release_receipt.py",
            "sha256": _sha256_path(ROOT / "build_release_receipt.py"),
        },
        "hermes": hermes,
        "model": model,
        "reviewer_attestation": reviewer,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_final_receipt_atomic(final_path, final)
    return {
        "status": "promoted",
        "receipt": final_path.relative_to(ROOT).as_posix(),
        "final": final,
    }


def _check_final_release(*, receipt_path: Path | None = None) -> dict[str, object]:
    actual = build_receipt()
    version = str(actual.get("version"))
    resolved: Path | None = None
    try:
        stored, resolved, error = _stored_receipt(
            version,
            kind="final",
            receipt_path=receipt_path,
        )
        if stored is None or resolved is None:
            return {
                "status": "mismatch",
                "reason_code": error or "FINAL_RECEIPT_MISSING",
                "receipt": None,
            }
        final = _read_strict_json(resolved)
        _require_exact_keys(
            final,
            {
                "schema", "name", "version", "subject", "candidate",
                "gate_report_sha256", "evaluator", "hermes", "model",
                "reviewer_attestation", "promoted_at",
            },
            "final receipt",
        )
        if final["schema"] != FINAL_RECEIPT_SCHEMA:
            raise ValueError("final receipt schema is unsupported")
        subject = {
            "name": str(actual["name"]),
            "version": str(actual["version"]),
            "content_sha256": str(actual["content_sha256"]),
            "profile_git_commit": _git_commit(ROOT),
        }
        if final["subject"] != subject or final["name"] != subject["name"] or final["version"] != subject["version"]:
            raise ValueError("final receipt subject differs from the current payload")
        evaluator = _require_exact_keys(final["evaluator"], {"path", "sha256"}, "final evaluator")
        if evaluator["path"] != "build_release_receipt.py" or evaluator["sha256"] != _sha256_path(ROOT / "build_release_receipt.py"):
            raise ValueError("final evaluator identity differs from the current builder")
        candidate = _require_exact_keys(final["candidate"], {"path", "sha256", "bytes"}, "final candidate")
        _require_sha256(candidate["sha256"], "final candidate.sha256")
        _require_int(candidate["bytes"], "final candidate.bytes", minimum=1)
        candidate_path, candidate_error = _receipt_location(Path(str(candidate["path"])), kind="candidate")
        if candidate_error or candidate_path is None:
            raise ValueError(candidate_error or "final candidate path is invalid")
        if candidate["path"] != candidate_path.relative_to(ROOT).as_posix():
            raise ValueError("final candidate path is not canonical")
        candidate_payload = candidate_path.read_bytes()
        if (
            candidate["sha256"] != _sha256_bytes(candidate_payload)
            or candidate["bytes"] != len(candidate_payload)
        ):
            raise ValueError("final candidate digest differs from the retained candidate")
        identity = compare_release_identity(
            actual,
            _read_strict_json(candidate_path),
            receipt_path=candidate_path,
        )
        if identity["status"] != "verified":
            raise ValueError("final candidate identity is not verified")
        eligibility = verify_candidate(receipt_path=candidate_path)
        if eligibility.get("eligible") is not True:
            raise ValueError("recomputed eligibility is false")
        _require_sha256(final["gate_report_sha256"], "final gate_report_sha256")
        if final["gate_report_sha256"] != eligibility.get("gate_report_sha256"):
            raise ValueError("final gate report digest differs from recomputed eligibility")
        expected_hermes = {
            "hermes_version": str(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")).get("hermes_requires", "")).removeprefix("=="),
            "hermes_git_commit": _git_commit(_hermes_source_root()),
        }
        if final["hermes"] != expected_hermes:
            raise ValueError("final Hermes identity differs from the current host")
        if final["model"] != _promotion_model_identity(eligibility):
            raise ValueError("final model identity differs from recomputed evidence")
        if final["reviewer_attestation"] != _promotion_reviewer_attestation(eligibility):
            raise ValueError("final reviewer attestation differs from recomputed evidence")
        _live_timestamp(final["promoted_at"], "final receipt.promoted_at")
        return {
            "status": "verified",
            "receipt": resolved.relative_to(ROOT).as_posix(),
            "candidate": candidate["path"],
            "gate_report_sha256": final["gate_report_sha256"],
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "status": "mismatch",
            "reason_code": "FINAL_RECEIPT_INVALID",
            "reason": str(error),
            "receipt": str(resolved.relative_to(ROOT).as_posix()) if resolved is not None and resolved.is_relative_to(ROOT) else None,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify the current payload and all bindings in a controlled final receipt")
    mode.add_argument("--verify-candidate", action="store_true", help="run the complete release-eligibility gate")
    mode.add_argument("--promote", action="store_true", help="promote one explicitly selected eligible candidate atomically")
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
    if args.output and (args.check or args.verify_candidate or args.promote or args.receipt):
        parser.error("--output cannot be combined with verification options")
    if args.receipt and not (args.check or args.verify_candidate or args.promote):
        parser.error("--receipt requires --check, --verify-candidate, or --promote")
    if args.promote and not args.receipt:
        parser.error("--promote requires an explicit pending/*-candidate-receipt*.json")
    if args.check:
        result = _check_final_release(receipt_path=args.receipt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "verified" else IDENTITY_MISMATCH_EXIT
    if args.promote:
        try:
            result = promote_candidate(receipt_path=args.receipt)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
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
