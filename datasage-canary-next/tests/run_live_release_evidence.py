#!/usr/bin/env python3
"""Capture then finalize test-only WeCom inbound evidence via official session export."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "fixtures" / "live_release_contract.json"
GOLDEN_PATH = ROOT / "plugins" / "datasage-query" / "e2e" / "golden_expert_cases.json"
ADAPTER_PATH = ROOT / "plugins" / "datasage-query" / "e2e" / "canary_transcript_adapter.py"
SCORER_PATH = ROOT / "plugins" / "datasage-query" / "e2e" / "golden_expert_scorer.py"
BUILDER_PATH = ROOT / "build_release_receipt.py"
EVIDENCE_DIR = ROOT / "pending" / "evidence"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: object) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8") if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(payload).hexdigest()


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BUILDER = _load_module("datasage_live_builder", BUILDER_PATH)
_export = _BUILDER._export_session
_turn_completion_policies = _BUILDER._live_turn_completion_policies
_endpoints = _BUILDER._live_endpoints


def _git(root: Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.resolve().as_posix()}", "-C", str(root), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=not binary, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError("Git provenance check failed")
    return result.stdout


def _commit(root: Path) -> str:
    value = str(_git(root, "rev-parse", "HEAD")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise RuntimeError("Git HEAD is not a full commit")
    return value


def _clean(root: Path, scope: Path | None = None) -> bool:
    args = ["status", "--porcelain=v1", "--untracked-files=all"]
    if scope is not None:
        args.extend(["--", scope.resolve().relative_to(root.resolve()).as_posix()])
    return not str(_git(root, *args)).strip()


def _source(commit: str, path: str) -> dict[str, str]:
    git_root = Path(str(_git(ROOT, "rev-parse", "--show-toplevel")).strip()).resolve()
    repository_path = (ROOT.relative_to(git_root) / path).as_posix()
    blob = _git(git_root, "show", f"{commit}:{repository_path}", binary=True)
    worktree = (ROOT / path).read_bytes()
    if not isinstance(blob, bytes) or blob != worktree:
        raise RuntimeError(f"tracked source differs from HEAD: {path}")
    return {"path": path, "sha256": _sha(blob)}


def _artifact(path: Path, *, logical_path: Path | None = None) -> dict[str, object]:
    payload = path.read_bytes()
    reference_path = logical_path or path
    return {"path": reference_path.relative_to(EVIDENCE_DIR).as_posix(), "sha256": _sha(payload), "bytes": len(payload)}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require_capture_caller_readiness(contract: dict[str, object]) -> None:
    inbound = contract.get("inbound")
    readiness = contract.get("runtime_readiness_policy")
    if (
        isinstance(inbound, dict)
        and inbound.get("platform") == "wecom"
        and inbound.get("chat_type") == "dm"
        and isinstance(readiness, dict)
        and readiness.get("trusted_caller_readiness")
        == "official_wecom_inbound_session_origin_verified"
    ):
        return
    raise RuntimeError("live capture has no trusted caller-readiness attestation")


def _runtime(*, hermes_root: Path, hermes_python: Path, contract: dict[str, object]) -> tuple[Path, Path, str, str]:
    hermes_root = hermes_root.resolve(strict=True)
    expected_python = (hermes_root / "venv" / "Scripts" / "python.exe").resolve(strict=True)
    if hermes_python.resolve(strict=True) != expected_python:
        raise RuntimeError("--hermes-python must be the pinned Hermes checkout venv interpreter")
    profile_git_root = Path(str(_git(ROOT, "rev-parse", "--show-toplevel")).strip()).resolve()
    if not _clean(profile_git_root, ROOT) or not _clean(hermes_root):
        raise RuntimeError("Profile scope and Hermes source must both be clean")
    profile_commit, hermes_commit = _commit(ROOT), _commit(hermes_root)
    if contract["host"]["hermes_git_commit"] != hermes_commit:
        raise RuntimeError("Hermes HEAD differs from the tracked live contract")
    for path in ("build_release_receipt.py", "tests/fixtures/live_release_contract.json", *contract["source_paths"].values()):
        _source(profile_commit, path)
    return hermes_root, expected_python, profile_commit, hermes_commit


def _environment(hermes_root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    env.update({"HERMES_HOME": str(ROOT), "PYTHONPATH": str(hermes_root), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _expand(template: list[str], replacements: dict[str, str]) -> list[str]:
    command = []
    for token in template:
        if token.startswith("{") and token.endswith("}"):
            name = token[1:-1]
            if name not in replacements:
                raise RuntimeError(f"missing command binding: {name}")
            command.append(replacements[name])
        else:
            command.append(token)
    return command


def _run(command: list[str], template: list[str], *, cwd: Path, env: dict[str, str], timeout: int, stream_dir: Path, stream_prefix: str, logical_stream_dir: Path | None = None) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
    if len(command) != len(template):
        raise RuntimeError("subprocess argv length differs from tracked template")
    projection: list[object] = []
    for actual, expected in zip(command, template):
        if expected.startswith("{") and expected.endswith("}"):
            projection.append({"binding": expected[1:-1], "value_sha256": _sha(actual)})
        elif actual == expected:
            projection.append(actual)
        else:
            raise RuntimeError("subprocess literal argv differs from tracked template")
    started = time.perf_counter_ns()
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=False, timeout=timeout, check=False)
        timed_out = False
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, bytes) else (error.stdout or "").encode("utf-8")
        stderr = error.stderr if isinstance(error.stderr, bytes) else (error.stderr or "").encode("utf-8")
        result, timed_out = subprocess.CompletedProcess(command, 124, stdout, stderr), True
    stdout_path, stderr_path = stream_dir / f"{stream_prefix}.stdout", stream_dir / f"{stream_prefix}.stderr"
    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)
    record = {
        "argv": projection, "argv_sha256": _sha(projection), "exit_code": result.returncode,
        "timed_out": timed_out, "duration_ns": max(1, time.perf_counter_ns() - started),
        "stdout": _artifact(
            stdout_path,
            logical_path=(logical_stream_dir / stdout_path.name if logical_stream_dir else None),
        ),
        "stderr": _artifact(
            stderr_path,
            logical_path=(logical_stream_dir / stderr_path.name if logical_stream_dir else None),
        ),
    }
    if timed_out or result.returncode:
        raise RuntimeError("live subprocess failed")
    return result, record


def _python_provenance(builder: Any, hermes_root: Path, hermes_python: Path, contract: dict[str, object]) -> dict[str, object]:
    if Path(sys.executable).resolve() != hermes_python.resolve() or Path(sys.prefix).resolve() != hermes_python.parent.parent.resolve():
        raise RuntimeError("runner itself must execute under the pinned Hermes venv Python")
    proof = builder._current_python_provenance("runner", contract["python_provenance_approval"])
    return builder._validate_python_provenance(proof, "runner Python provenance", contract["python_provenance_approval"])


def _set_read_only(paths: list[Path]) -> None:
    for path in paths:
        path.chmod(stat.S_IREAD)


def _fail_on_nonretryable_tool_result(messages: list[dict[str, object]]) -> None:
    public_tools = {
        "datasage_catalog",
        "datasage_entity_resolve",
        "datasage_query",
        "datasage_push",
    }
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_name") not in public_tools:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError("persisted DataSage tool result is not JSON text")
        try:
            payload = json.loads(
                content,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("persisted DataSage tool result is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("persisted DataSage tool result is not an object")
        codes: set[str] = set()
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            codes.add(error["code"])
        for result in payload.get("results") or []:
            if isinstance(result, dict):
                error = result.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    codes.add(error["code"])
        bundle = payload.get("evidence_bundle")
        for gap in bundle.get("evidence_gaps") or [] if isinstance(bundle, dict) else []:
            if isinstance(gap, dict) and isinstance(gap.get("reason"), str):
                codes.add(gap["reason"])
        if "DATA_ENTITLEMENT_DENIED" in codes:
            raise RuntimeError(
                "nonretryable DATA_ENTITLEMENT_DENIED in persisted tool result"
            )


def _aware_datetime(value: object, label: str) -> datetime:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be a timezone-aware timestamp")
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(f"{label} must be a timezone-aware timestamp") from exc
    else:
        raise RuntimeError(f"{label} must be a timezone-aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{label} must be a timezone-aware timestamp")
    return parsed


def _commit_timestamp(commit: str) -> datetime:
    value = str(_git(ROOT, "show", "-s", "--format=%cI", commit)).strip()
    return _aware_datetime(value, "subject commit timestamp")


def _session_origin(
    session: dict[str, object],
    *,
    contract: dict[str, object],
    commit: str,
    run_index: int,
    commit_timestamp: datetime,
) -> dict[str, object]:
    inbound = contract["inbound"]
    session_id = session.get("id") or session.get("session_id")
    user_id, chat_id = session.get("user_id"), session.get("chat_id")
    expected_title = f"datasage-live-{commit[:12]}-run-{run_index}"
    started_at = _aware_datetime(session.get("started_at"), "session.started_at")
    if not isinstance(session_id, str) or not session_id or any(character.isspace() for character in session_id):
        raise RuntimeError("official export has no complete exact session ID")
    if session.get("source") != inbound["platform"] or session.get("chat_type") != inbound["chat_type"]:
        raise RuntimeError("official export is not a WeCom DM inbound session")
    if (
        not isinstance(user_id, str)
        or not user_id
        or _sha(user_id) != inbound["expected_user_id_sha256"]
        or not isinstance(chat_id, str)
        or not chat_id
        or _sha(chat_id) != inbound["expected_chat_id_sha256"]
    ):
        raise RuntimeError("official export caller/chat identity does not match the anonymous contract pins")
    if session.get("title") != expected_title:
        raise RuntimeError("official export title does not match the exact run title")
    if started_at < commit_timestamp:
        raise RuntimeError("official export session predates the subject commit")
    return {
        "source": inbound["platform"],
        "chat_type": inbound["chat_type"],
        "user_id_sha256": _sha(user_id),
        "chat_id_sha256": _sha(chat_id),
        "title": expected_title,
        "started_at": started_at.isoformat(),
        "session_id": session_id,
    }


def _cleanup_capture_stage(stage: Path, commit: str) -> None:
    private_root = (EVIDENCE_DIR / "private").resolve()
    resolved = stage.resolve()
    prefix = f".capture-stage-{commit}-"
    suffix = resolved.name.removeprefix(prefix)
    if (
        resolved.parent != private_root
        or not resolved.name.startswith(prefix)
        or re.fullmatch(r"[0-9a-f]{32}", suffix) is None
    ):
        raise RuntimeError("refusing to clean an unscoped capture staging path")
    if not resolved.exists():
        return
    for path in sorted(resolved.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(resolved)


def _review_assertion(review: dict[str, object], *, adapter: Any, profile: dict[str, str], case_id: str, session_id: str, user_id: int, prompt_sha: str, session_export_sha256: str, watermark: str, final_sha: str, fixture_sha: str, database_ref_sha: str) -> dict[str, object]:
    return {
        "schema": adapter.REVIEW_SCHEMA, "status": "reviewed", "test_id": case_id,
        "artifact_id": profile["artifact_id"], "payload_sha256": profile["payload_sha256"],
        "session_id": session_id, "user_message_id": user_id, "canonical_prompt_sha256": prompt_sha,
        "session_export_sha256": session_export_sha256, "watermark_sha256": watermark,
        "final_answer_sha256": final_sha, "reviewer_id": review["reviewer_id"], "labels": review["labels"],
        "fixture_attestation_sha256": fixture_sha, "business_database_ref_sha256": database_ref_sha,
    }


def _capture(args: argparse.Namespace, contract: dict[str, object], golden: dict[str, object], builder: Any) -> int:
    _require_capture_caller_readiness(contract)
    session_ids = args.session_ids
    if (
        not isinstance(session_ids, list)
        or len(session_ids) != 3
        or len(set(session_ids)) != 3
        or any(not isinstance(value, str) or not value or len(value) > 256 or any(character.isspace() for character in value) for value in session_ids)
    ):
        raise RuntimeError("capture requires exactly three unique ordered complete session IDs")
    hermes_root, hermes_python, commit, hermes_commit = _runtime(hermes_root=args.hermes_root, hermes_python=args.hermes_python, contract=contract)
    python_before = _python_provenance(builder, hermes_root, hermes_python, contract)
    receipt = builder.build_receipt()
    case_ids = contract["case_plan"]["case_ids"]
    by_id = {item["id"]: item for item in golden["cases"]}
    cases = [by_id[case_id] for case_id in case_ids]
    if [case["turn"] for case in cases] != [1, 2] or {case["conversation_id"] for case in cases} != {contract["case_plan"]["conversation_id"]}:
        raise RuntimeError("contract cases are not the exact tracked two-turn conversation")
    root = EVIDENCE_DIR / "private" / commit
    if root.exists():
        raise RuntimeError("private capture path already exists; retained capture remains fail-closed")
    stage = EVIDENCE_DIR / "private" / f".capture-stage-{commit}-{uuid.uuid4().hex}"
    stage.mkdir(parents=True)
    try:
        env = _environment(hermes_root)
        shapes = contract["execution"]["command_shapes"]
        timeout = contract["execution"]["timeout_seconds"]
        captured_at = datetime.now().astimezone()
        if captured_at.utcoffset() is None:
            raise RuntimeError("capture clock did not produce a timezone-aware timestamp")
        commit_timestamp = _commit_timestamp(commit)
        runs = []
        for run_index, requested_session_id in enumerate(session_ids, 1):
            run_dir = stage / f"run-{run_index}"
            logical_run_dir = root / f"run-{run_index}"
            run_dir.mkdir()
            export_command = _expand(
                shapes["session_export"],
                {"python": str(hermes_python), "exact_session_id": requested_session_id},
            )
            exported, export_record = _run(
                export_command,
                shapes["session_export"],
                cwd=ROOT,
                env=env,
                timeout=timeout,
                stream_dir=run_dir,
                stream_prefix="session-export",
                logical_stream_dir=logical_run_dir,
            )
            export_path = run_dir / "session.jsonl"
            export_path.write_bytes(exported.stdout)
            session, messages = _export(exported.stdout.decode("utf-8", "strict"))
            origin = _session_origin(
                session,
                contract=contract,
                commit=commit,
                run_index=run_index,
                commit_timestamp=commit_timestamp,
            )
            session_id = origin.pop("session_id")
            if session_id != requested_session_id:
                raise RuntimeError("official export does not match the exact requested session")
            endpoints = _endpoints(
                messages,
                [case["prompt"] for case in cases],
                contract["turn_completion_policy"],
            )
            _fail_on_nonretryable_tool_result(messages)
            export_ref = _artifact(export_path, logical_path=logical_run_dir / export_path.name)
            artifact_refs = [export_record["stdout"], export_record["stderr"], export_ref]
            runs.append({
                "run_index": run_index,
                "case_ids": case_ids,
                "processes": {"session_export": export_record},
                "session": {
                    **origin,
                    "export_format": "jsonl",
                    "turns": 2,
                    "lineage_sha256": _sha([session_id]),
                    "session_id_sha256": _sha(session_id),
                    "final_answer_sha256": [item[2] for item in endpoints],
                    "export": export_ref,
                },
                "artifact_set_sha256": _sha(artifact_refs),
            })
        _runtime(hermes_root=hermes_root, hermes_python=hermes_python, contract=contract)
        python_after = _python_provenance(builder, hermes_root, hermes_python, contract)
        capture = {
            "schema": "datasage-live-capture/v2",
            "captured_at": captured_at.isoformat(),
            "subject_commit_timestamp": commit_timestamp.isoformat(),
            "subject": {"name": receipt["name"], "version": receipt["version"], "content_sha256": receipt["content_sha256"], "profile_git_commit": commit},
            "host": {"hermes_version": contract["host"]["hermes_version"], "hermes_git_commit": hermes_commit},
            "contract": _source(commit, "tests/fixtures/live_release_contract.json"),
            "python_provenance": {"before": python_before, "after": python_after},
            "runs": runs,
        }
        temporary = stage / ".capture.json.tmp"
        _write_json(temporary, capture)
        capture_path = stage / "capture.json"
        temporary.replace(capture_path)
        digest_path = stage / "capture.sha256"
        digest_path.write_text(_sha(capture_path.read_bytes()) + "\n", encoding="ascii", newline="")
        _set_read_only([path for path in stage.rglob("*") if path.is_file()])
        stage.replace(root)
        return 0
    except BaseException:
        _cleanup_capture_stage(stage, commit)
        raise


def _finalize(args: argparse.Namespace, contract: dict[str, object], golden: dict[str, object], builder: Any) -> int:
    hermes_root, hermes_python, commit, hermes_commit = _runtime(hermes_root=args.hermes_root, hermes_python=args.hermes_python, contract=contract)
    python_before = _python_provenance(builder, hermes_root, hermes_python, contract)
    receipt = builder.build_receipt()
    root = EVIDENCE_DIR / "private" / commit
    capture_path, capture_digest_path = root / "capture.json", root / "capture.sha256"
    capture_ref, capture_digest_ref = _artifact(capture_path), _artifact(capture_digest_path)
    builder._live_capture_artifact(capture_ref, capture_digest_ref, subject_commit=commit)
    capture = _read_json(capture_path)
    expected_subject = {"name": receipt["name"], "version": receipt["version"], "content_sha256": receipt["content_sha256"], "profile_git_commit": commit}
    expected_host = {"hermes_version": contract["host"]["hermes_version"], "hermes_git_commit": hermes_commit}
    expected_contract = _source(commit, "tests/fixtures/live_release_contract.json")
    if (
        set(capture) != {"schema", "captured_at", "subject_commit_timestamp", "subject", "host", "contract", "python_provenance", "runs"}
        or capture["schema"] != "datasage-live-capture/v2"
        or _canonical(capture["subject"]) != _canonical(expected_subject)
        or _canonical(capture["host"]) != _canonical(expected_host)
        or _canonical(capture["contract"]) != _canonical(expected_contract)
        or not isinstance(capture["runs"], list)
        or len(capture["runs"]) != 3
    ):
        raise RuntimeError("no complete current-subject capture is available")
    captured_at = _aware_datetime(capture["captured_at"], "capture.captured_at")
    subject_commit_timestamp = _aware_datetime(
        capture["subject_commit_timestamp"], "capture.subject_commit_timestamp"
    )
    if subject_commit_timestamp != _commit_timestamp(commit) or captured_at < subject_commit_timestamp:
        raise RuntimeError("frozen capture timestamps do not bind the subject commit")
    capture_python = capture["python_provenance"]
    if not isinstance(capture_python, dict) or set(capture_python) != {"before", "after"}:
        raise RuntimeError("frozen capture has no closed Python provenance")
    builder._validate_python_provenance(capture_python["before"], "capture Python before", contract["python_provenance_approval"])
    builder._validate_python_provenance(capture_python["after"], "capture Python after", contract["python_provenance_approval"])
    case_ids = contract["case_plan"]["case_ids"]
    by_id = {item["id"]: item for item in golden["cases"]}
    cases = [by_id[case_id] for case_id in case_ids]
    shapes = contract["execution"]["command_shapes"]
    captured_contexts = []
    captured_session_ids: set[str] = set()
    for position, captured in enumerate(capture["runs"], 1):
        if (
            not isinstance(captured, dict)
            or set(captured) != {"run_index", "case_ids", "processes", "session", "artifact_set_sha256"}
            or type(captured["run_index"]) is not int
            or captured["run_index"] != position
            or captured["case_ids"] != case_ids
            or not isinstance(captured["processes"], dict)
            or set(captured["processes"]) != {"session_export"}
            or not isinstance(captured["session"], dict)
            or set(captured["session"]) != {"source", "chat_type", "user_id_sha256", "chat_id_sha256", "title", "started_at", "export_format", "turns", "lineage_sha256", "session_id_sha256", "final_answer_sha256", "export"}
        ):
            raise RuntimeError("frozen capture run shape/order differs from the tracked contract")
        run_dir = root / f"run-{position}"
        export_path = run_dir / "session.jsonl"
        if captured["session"]["export"] != _artifact(export_path):
            raise RuntimeError("frozen capture export is not bound to the retained official export")
        builder._live_artifact(
            captured["session"]["export"], subject_commit=commit, run_index=position,
            filename="session.jsonl", label="captured official export", require_read_only=True,
        )
        session, messages = _export(export_path.read_text(encoding="utf-8"))
        session_id = session.get("id") or session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("captured session ID is unavailable")
        if session_id in captured_session_ids:
            raise RuntimeError("frozen capture reuses a session across runs")
        captured_session_ids.add(session_id)
        origin = _session_origin(
            session,
            contract=contract,
            commit=commit,
            run_index=position,
            commit_timestamp=subject_commit_timestamp,
        )
        if origin.pop("session_id") != session_id:
            raise RuntimeError("frozen session origin does not bind the exported session")
        endpoints = _endpoints(
            messages,
            [case["prompt"] for case in cases],
            contract["turn_completion_policy"],
        )
        _fail_on_nonretryable_tool_result(messages)
        session_record = captured["session"]
        if (
            any(session_record[key] != origin[key] for key in origin)
            or session_record["export_format"] != "jsonl"
            or type(session_record["turns"]) is not int
            or session_record["turns"] != 2
            or session_record["lineage_sha256"] != _sha([session_id])
            or session_record["session_id_sha256"] != _sha(session_id)
            or session_record["final_answer_sha256"] != [item[2] for item in endpoints]
        ):
            raise RuntimeError("frozen capture session lineage/answers do not match the retained export")
        builder._validate_process_record(
            captured["processes"]["session_export"], shapes["session_export"], f"capture run {position} export",
            expected_bindings={"python": str(hermes_python), "exact_session_id": session_id},
            subject_commit=commit, run_index=position, stream_prefix="session-export", require_read_only=True,
        )
        if captured["processes"]["session_export"]["stdout"]["sha256"] != captured["session"]["export"]["sha256"]:
            raise RuntimeError("captured export process stdout differs from the retained official export")
        artifact_refs = [
            captured["processes"]["session_export"]["stdout"],
            captured["processes"]["session_export"]["stderr"],
        ]
        artifact_refs.append(captured["session"]["export"])
        if captured["artifact_set_sha256"] != _sha(artifact_refs):
            raise RuntimeError("frozen capture artifact-set digest is invalid")
        captured_contexts.append((captured, run_dir, export_path, session_id, endpoints, messages))
    report_path = EVIDENCE_DIR / f"live-release-{commit}.json"
    if report_path.exists():
        raise RuntimeError("final live evidence already exists and is immutable")
    external = _read_json(args.reviews)
    if set(external) != {"schema", "reviews"} or external["schema"] != "datasage-live-review-batch/v1" or not isinstance(external["reviews"], list) or len(external["reviews"]) != 12:
        raise RuntimeError("external review batch must contain two trusted reviewers x 3 runs x 2 cases")
    review_keys = {"run_index", "case_id", "session_id_sha256", "final_answer_sha256", "capture_sha256", "reviewer_id", "labels", "evidence", "reviewed_at"}
    review_map: dict[tuple[int, str], list[dict[str, object]]] = {}
    for review in external["reviews"]:
        if not isinstance(review, dict) or set(review) != review_keys:
            raise RuntimeError("external reviews have unknown/missing fields; plan_trace is forbidden")
        key = (review.get("run_index"), review.get("case_id"))
        if type(key[0]) is not int or not isinstance(key[1], str):
            raise RuntimeError("external reviews contain a wrong run/case")
        review_map.setdefault(key, []).append(review)
    expected_review_keys = {(run, case) for run in range(1, 4) for case in case_ids}
    trusted = contract["review_policy"]["trusted_reviewer_id_sha256"]
    if set(review_map) != expected_review_keys or any(len(pair) != 2 or {_sha(item["reviewer_id"]) for item in pair} != set(trusted) for pair in review_map.values()):
        raise RuntimeError("external reviews do not exactly cover two independent trusted reviewers per run/case")
    adapter = _load_module("datasage_live_adapter", ADAPTER_PATH)
    profile = {"profile_id": receipt["name"], "artifact_id": commit, "payload_sha256": receipt["content_sha256"]}
    env, timeout = _environment(hermes_root), contract["execution"]["timeout_seconds"]
    finalized = []
    for captured, run_dir, export_path, session_id, endpoints, messages in captured_contexts:
        run_index = captured["run_index"]
        session_export_sha256 = captured["session"]["export"]["sha256"]
        generated = [run_dir / name for name in (
            "reviews.json", "bindings.json", "candidate.json", "score.json",
            "adapter.stdout", "adapter.stderr", "scorer.stdout", "scorer.stderr",
        )]
        if any(path.exists() for path in generated):
            raise RuntimeError("partial finalize artifacts exist; refusing to repeat adapter/scorer work")
        ordered_reviews = []
        for case_id in case_ids:
            pair = sorted(review_map[(run_index, case_id)], key=lambda item: trusted.index(_sha(item["reviewer_id"])))
            ordered_reviews.extend(pair)
        review_set = {"schema": "datasage-live-review-set/v1", "run_index": run_index, "reviews": ordered_reviews}
        reviews = builder._validate_live_reviews(
            review_set, run_index=run_index, case_ids=case_ids,
            review_policy=contract["review_policy"], capture_sha256=capture_ref["sha256"],
        )
        consensus_reviews = []
        for case, endpoint in zip(cases, endpoints):
            pair = reviews[case["id"]]
            final_answer = next(item["content"] for item in messages if item.get("id") == endpoint[1])
            builder._reject_internal_conclusion_codes(final_answer, case)
            for review in pair:
                if review["session_id_sha256"] != _sha(session_id) or review["final_answer_sha256"] != endpoint[2]:
                    raise RuntimeError("external review is not bound to the captured run/answer")
                builder._validate_review_evidence(review, final_answer, case["required_conclusions"])
            if pair[0]["labels"] != pair[1]["labels"]:
                raise RuntimeError("trusted reviewers did not reach exact conclusion consensus")
            consensus_reviews.append({"reviewer_id": builder._review_consensus_id(pair), "labels": pair[0]["labels"]})
        reviews_path = run_dir / "reviews.json"
        _write_json(reviews_path, review_set)
        fixture_sha, database_ref_sha = args.fixture_attestation_sha256, args.business_database_ref_sha256
        turns = []
        for case, review, (user_id, final_id, final_sha) in zip(cases, consensus_reviews, endpoints):
            prompt_sha = _sha(case["prompt"])
            watermark = adapter._watermark(test_id=case["id"], conversation_id=case["conversation_id"], turn=case["turn"], canonical_prompt_sha256=prompt_sha, user_message_id=user_id, session_export_sha256=session_export_sha256, profile=profile, fixture_attestation_sha256=fixture_sha, business_database_ref_sha256=database_ref_sha)
            turns.append({
                "test_id": case["id"], "conversation_id": case["conversation_id"], "turn": case["turn"], "session_id": session_id,
                "user_message_id": user_id, "final_message_id": final_id, "canonical_prompt_sha256": prompt_sha, "watermark_sha256": watermark,
                "fixture_attestation_sha256": fixture_sha, "business_database_ref_sha256": database_ref_sha,
                "review": _review_assertion(review, adapter=adapter, profile=profile, case_id=case["id"], session_id=session_id, user_id=user_id, prompt_sha=prompt_sha, session_export_sha256=session_export_sha256, watermark=watermark, final_sha=final_sha, fixture_sha=fixture_sha, database_ref_sha=database_ref_sha),
            })
        bindings = {"schema": adapter.LIVE_BINDING_SCHEMA, "captured_at": capture["captured_at"], "transcript_source": "wecom", "profile_artifact": profile, "session_export_sha256": session_export_sha256, "turns": turns}
        bindings_path, candidate_path, score_path = run_dir / "bindings.json", run_dir / "candidate.json", run_dir / "score.json"
        _write_json(bindings_path, bindings)
        common = {"python": str(hermes_python), "adapter": str(ADAPTER_PATH), "scorer": str(SCORER_PATH), "session_export": str(export_path), "bindings": str(bindings_path), "candidate": str(candidate_path), "golden_suite": str(GOLDEN_PATH), "case_1": case_ids[0], "case_2": case_ids[1], "score_report": str(score_path)}
        _, adapter_record = _run(_expand(shapes["adapter"], common), shapes["adapter"], cwd=ROOT, env=env, timeout=timeout, stream_dir=run_dir, stream_prefix="adapter")
        _, scorer_record = _run(_expand(shapes["scorer"], common), shapes["scorer"], cwd=ROOT, env=env, timeout=timeout, stream_dir=run_dir, stream_prefix="scorer")
        finalized.append({
            "run_index": run_index, "case_ids": case_ids, "processes": {"adapter": adapter_record, "scorer": scorer_record},
            "bindings": _artifact(bindings_path), "candidate": _artifact(candidate_path),
            "score_report": _artifact(score_path), "reviews": _artifact(reviews_path),
        })
    _runtime(hermes_root=hermes_root, hermes_python=hermes_python, contract=contract)
    python_after = _python_provenance(builder, hermes_root, hermes_python, contract)
    sources = contract["source_paths"]
    evidence = {
        "schema": "datasage-live-release-evidence/v3",
        "captured_at": capture["captured_at"],
        "subject_commit_timestamp": capture["subject_commit_timestamp"],
        "subject": {"name": receipt["name"], "version": receipt["version"], "content_sha256": receipt["content_sha256"], "profile_git_commit": commit},
        "host": {"hermes_version": contract["host"]["hermes_version"], "hermes_git_commit": hermes_commit},
        "contract": _source(commit, "tests/fixtures/live_release_contract.json"), "producer": _source(commit, sources["producer"]),
        "golden_suite": _source(commit, sources["golden_suite"]), "adapter": _source(commit, sources["adapter"]), "scorer": _source(commit, sources["scorer"]),
        "runtime_readiness_policy_sha256": _sha(contract["runtime_readiness_policy"]),
        "capture": capture_ref, "capture_digest": capture_digest_ref,
        "python_provenance": {"before": python_before, "after": python_after}, "runs": finalized,
    }
    temporary = EVIDENCE_DIR / f".live-release-{commit}.tmp"
    _write_json(temporary, evidence)
    temporary.replace(report_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("capture", "finalize"))
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--session-id", dest="session_ids", action="append", default=[])
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--fixture-attestation-sha256")
    parser.add_argument("--business-database-ref-sha256")
    args = parser.parse_args(argv)
    contract, golden = _read_json(CONTRACT_PATH), _read_json(GOLDEN_PATH)
    builder = _BUILDER
    builder._validate_live_contract(contract)
    if args.phase == "capture":
        if args.reviews or args.fixture_attestation_sha256 or args.business_database_ref_sha256:
            parser.error("capture accepts no review/finalize inputs")
        if len(args.session_ids) != 3:
            parser.error("capture requires exactly three ordered --session-id values")
        return _capture(args, contract, golden, builder)
    if args.session_ids:
        parser.error("finalize accepts no --session-id values")
    if args.reviews is None:
        parser.error("finalize requires --reviews")
    for name in ("fixture_attestation_sha256", "business_database_ref_sha256"):
        if SHA256_RE.fullmatch(getattr(args, name) or "") is None:
            parser.error(f"finalize requires --{name.replace('_', '-')} as SHA-256")
    return _finalize(args, contract, golden, builder)


if __name__ == "__main__":
    raise SystemExit(main())
