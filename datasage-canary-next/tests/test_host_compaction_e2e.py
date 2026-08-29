"""Pinned-host E2E evidence for DataSage compaction ordering.

The module is also a no-write-by-default evidence CLI. It exercises Hermes'
real in-process compression and request assembly and replaces only the two
outbound model boundaries.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PROFILE_REPO = PROFILE_ROOT.parent
PROFILE_REPO_PATH = PROFILE_ROOT.relative_to(PROFILE_REPO).as_posix()
FIXTURE_PATH = PROFILE_ROOT / "tests" / "fixtures" / "host_compaction_ordering.json"
PRODUCER_PATH = PROFILE_ROOT / "tests" / "test_host_compaction_e2e.py"
FIXTURE_RELATIVE_PATH = "tests/fixtures/host_compaction_ordering.json"
PRODUCER_RELATIVE_PATH = "tests/test_host_compaction_e2e.py"
EVIDENCE_SCHEMA = "datasage-host-compaction-evidence/v1"
HOST_TOP_LEVEL_EXCLUDED = {"tests", "venv"}
TEST_CHILD_SCHEMA = "datasage-host-compaction-test-child/v1"
KNOWN_RELAY_STDERR_BLOCK = re.compile(
    r"Hermes Relay orphaned scope drain failed\n"
    r"Traceback \(most recent call last\):\n"
    r".*?RuntimeError: invalid argument: root scope cannot be removed\n"
    r"Hermes Relay session [^\n]+ closed with errors: session scope close "
    r"failed: not found: scope handle not found(?:\n|\Z)",
    re.DOTALL,
)


def _snapshot_modules() -> dict[str, object]:
    return dict(sys.modules)


def _host_top_level_module_names(host_root: Path) -> set[str]:
    names = {
        path.stem
        for path in host_root.glob("*.py")
        if path.name != "__init__.py"
    }
    names.update(
        path.name
        for path in host_root.iterdir()
        if path.is_dir()
        and path.name not in HOST_TOP_LEVEL_EXCLUDED
        and (path / "__init__.py").is_file()
    )
    return names


def _host_module_entries(
    modules: dict[str, object], host_root: Path
) -> dict[str, object]:
    host_top_levels = _host_top_level_module_names(host_root)
    return {
        name: module
        for name, module in modules.items()
        if name.partition(".")[0] in host_top_levels
    }


def _validate_host_module_origins(
    modules: dict[str, object], host_root: Path
) -> None:
    resolved_host = host_root.resolve()
    for name, module in _host_module_entries(modules, resolved_host).items():
        origin = getattr(module, "__file__", None)
        if isinstance(origin, str) and origin:
            origins = [origin]
        else:
            namespace_path = getattr(module, "__path__", None)
            try:
                origins = list(namespace_path) if namespace_path is not None else []
            except TypeError as exc:
                raise RuntimeError(
                    f"loaded Hermes module has no verifiable __file__ or "
                    f"namespace __path__: {name}"
                ) from exc
        if not origins:
            raise RuntimeError(
                f"loaded Hermes module has no verifiable __file__ or "
                f"namespace __path__: {name}"
            )
        for origin_entry in origins:
            resolved_origin = Path(origin_entry).resolve()
            try:
                resolved_origin.relative_to(resolved_host)
            except ValueError as exc:
                raise RuntimeError(
                    f"loaded Hermes module is outside pinned host: "
                    f"{name} -> {resolved_origin}"
                ) from exc


def _require_no_preloaded_host_modules(host_root: Path) -> None:
    preloaded = sorted(_host_module_entries(dict(sys.modules), host_root))
    if preloaded:
        raise RuntimeError(
            "host-compaction producer must start in a fresh process; "
            f"preloaded pinned-host modules: {preloaded!r}"
        )


def _classify_child_stderr(stderr: str) -> str:
    normalized = stderr.replace("\r\n", "\n")
    if not normalized.strip():
        return "empty"
    cursor = 0
    while cursor < len(normalized):
        match = KNOWN_RELAY_STDERR_BLOCK.match(normalized, cursor)
        if match is None:
            raise AssertionError(
                "host-compaction child emitted non-Relay stderr:\n"
                + normalized[cursor:]
            )
        cursor = match.end()
    return "known_official_relay_destructor_noise"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _read_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _git_commit(repo: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "-C",
            str(repo),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    commit = result.stdout.strip().lower()
    if len(commit) < 40 or any(char not in "0123456789abcdef" for char in commit):
        raise RuntimeError(f"invalid Git commit returned for {repo}: {commit!r}")
    return commit


def _git_repo_path(profile_relative_path: str) -> str:
    return f"{PROFILE_REPO_PATH}/{profile_relative_path}"


def _git_is_tracked(repo: Path, repo_relative_path: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "-C",
            str(repo),
            "ls-files",
            "--error-unmatch",
            "--",
            repo_relative_path,
        ],
        capture_output=True,
    )
    return result.returncode == 0


def _require_tracked_source(repo_relative_path: str) -> None:
    if not _git_is_tracked(PROFILE_REPO, repo_relative_path):
        raise RuntimeError(
            f"release evidence source must be tracked by Git: {repo_relative_path}"
        )


def _git_blob_sha256(repo: Path, commit: str, repo_relative_path: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "-C",
            str(repo),
            "show",
            f"{commit}:{repo_relative_path}",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "release evidence source has no blob in subject commit "
            f"{commit}: {repo_relative_path}: {detail}"
        )
    return _sha256_bytes(result.stdout)


def _git_status(repo: Path, pathspec: str | None = None) -> str:
    command = [
        "git",
        "-c",
        "safe.directory=*",
        "-C",
        str(repo),
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ]
    if pathspec is not None:
        command.extend(["--", pathspec])
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _host_root() -> Path:
    configured = os.environ.get("HERMES_AGENT_ROOT")
    candidate = (
        Path(configured).expanduser()
        if configured
        else PROFILE_ROOT.parent.parent / "hermes-agent"
    ).resolve()
    required = (
        candidate / "run_agent.py",
        candidate / "agent" / "context_compressor.py",
        candidate / "pyproject.toml",
        candidate / ".git",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "HERMES_AGENT_ROOT does not identify a complete official checkout: "
            + ", ".join(missing)
        )
    return candidate


def _load_release_subject(profile_commit: str) -> dict[str, object]:
    module_path = PROFILE_ROOT / "build_release_receipt.py"
    spec = importlib.util.spec_from_file_location(
        "datasage_host_compaction_release_subject", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load release subject producer: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = module.build_receipt()
    return {
        "name": receipt["name"],
        "version": receipt["version"],
        "content_sha256": receipt["content_sha256"],
        "profile_git_commit": profile_commit,
    }


def _host_identity(host_root: Path) -> dict[str, str]:
    pyproject = tomllib.loads(
        (host_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    version = pyproject.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("Hermes pyproject.toml has no project.version")
    return {
        "hermes_version": version.strip(),
        "hermes_git_commit": _git_commit(host_root),
    }


def _validate_pinned_host(
    fixture: dict[str, object], host_identity: dict[str, str]
) -> None:
    pinned = fixture.get("pinned_host")
    if pinned != host_identity:
        raise RuntimeError(
            f"Hermes host does not match fixture pinned_host: "
            f"expected={pinned!r}, actual={host_identity!r}"
        )


def _validate_release_preconditions(host_root: Path) -> None:
    profile_status = _git_status(PROFILE_REPO, PROFILE_REPO_PATH)
    if profile_status:
        raise RuntimeError(
            "Profile-scoped worktree must be clean before evidence generation:\n"
            + profile_status
        )
    host_status = _git_status(host_root)
    if host_status:
        raise RuntimeError(
            "Hermes worktree must be clean before evidence generation:\n"
            + host_status
        )


def _fixture_config() -> dict[str, object]:
    return {
        "model": {
            "default": "test/model",
            "provider": "custom",
            "base_url": "https://fixture.invalid/v1",
            "context_length": 65_536,
        },
        "compression": {
            "enabled": True,
            "threshold": 0.50,
            "threshold_tokens": 20_000,
            "target_ratio": 0.20,
            "protect_first_n": 0,
            "protect_last_n": 3,
            "max_attempts": 3,
            "in_place": True,
            "micro_compact": False,
        },
        "context": {"engine": "compressor"},
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
        "auxiliary": {
            "compression": {"context_length": 65_536},
            "background_review": {"enabled": False},
        },
    }


def _pressured_history(fixture: dict[str, object]) -> list[dict[str, object]]:
    source = fixture.get("pre_compaction_messages")
    pressure = fixture.get("pressure")
    if not isinstance(source, list) or not source:
        raise ValueError("fixture pre_compaction_messages must be a non-empty list")
    if not isinstance(pressure, dict):
        raise ValueError("fixture pressure must be an object")
    repeat_count = int(pressure.get("repeat_count", 0))
    padding_chars = int(pressure.get("padding_chars_per_message", 0))
    padding_character = pressure.get("padding_character")
    if repeat_count < 2 or padding_chars < 1:
        raise ValueError("fixture pressure must create an oversized transcript")
    if not isinstance(padding_character, str) or len(padding_character) != 1:
        raise ValueError("fixture pressure.padding_character must be one character")

    history: list[dict[str, object]] = []
    padding = padding_character * padding_chars
    for repetition in range(repeat_count):
        for offset, raw_message in enumerate(source):
            if not isinstance(raw_message, dict):
                raise ValueError("fixture messages must be objects")
            role = raw_message.get("role")
            content = raw_message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError("fixture messages require user/assistant string content")
            history.append(
                {
                    "role": role,
                    "content": f"{content}\n[pressure-copy:{repetition}:{offset}]\n{padding}",
                }
            )
    return history


def _stop_response() -> SimpleNamespace:
    message = SimpleNamespace(
        content="host-compaction fixture complete",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    # No usage object: post-response pricing lookup is an unrelated external
    # metadata boundary and is intentionally not activated by this fixture.
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _summary_response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/summary", usage=None)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _latest_is_final_user_message(
    messages: list[dict[str, object]], latest: str
) -> bool:
    user_indices = [
        index for index, message in enumerate(messages) if message.get("role") == "user"
    ]
    return bool(
        user_indices
        and _content_text(messages[user_indices[-1]].get("content")) == latest
    )


def _observe_assertions(
    fixture: dict[str, object],
    messages: list[dict[str, object]],
    *,
    summary_prefix: str,
) -> list[dict[str, object]]:
    assertion_ids = fixture.get("required_host_assertions")
    expected_ids = [
        "compaction_summary_is_before_latest_user_message",
        "latest_user_message_hash_is_preserved_exactly",
        "no_summary_or_old_task_is_appended_as_a_user_message_after_latest_user_message",
        "model_input_authority_marks_latest_user_message_above_summary_draft_and_prior_correction",
    ]
    if assertion_ids != expected_ids:
        raise ValueError("fixture assertion IDs or ordering drifted from the v1 contract")

    latest = fixture.get("latest_user_message")
    expected_latest = fixture.get("expected_authoritative_latest_user_message")
    stale_fragments = fixture.get("stale_reference_fragments")
    if not isinstance(latest, str) or not isinstance(expected_latest, str):
        raise ValueError("fixture latest user messages must be strings")
    if not isinstance(stale_fragments, list) or not all(
        isinstance(item, str) and item for item in stale_fragments
    ):
        raise ValueError("fixture stale_reference_fragments must be non-empty strings")

    rendered = [_content_text(message.get("content")) for message in messages]
    latest_indices = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and rendered[index] == latest
    ]
    summary_indices = [
        index for index, content in enumerate(rendered) if summary_prefix in content
    ]
    latest_index = latest_indices[-1] if latest_indices else -1
    summary_before_latest = bool(
        len(latest_indices) == 1
        and summary_indices
        and max(summary_indices) < latest_index
    )
    preserved_exactly = bool(
        len(latest_indices) == 1
        and latest == expected_latest
        and _sha256_bytes(rendered[latest_index].encode("utf-8"))
        == _sha256_bytes(expected_latest.encode("utf-8"))
    )

    trailing_user_replay = False
    if latest_index >= 0:
        for index in range(latest_index + 1, len(messages)):
            if messages[index].get("role") != "user":
                continue
            content = rendered[index]
            if summary_prefix in content or any(
                fragment in content for fragment in stale_fragments
            ):
                trailing_user_replay = True
                break

    authority_phrases = (
        "Respond ONLY to the latest user message that appears AFTER this summary",
        "the latest user message WINS",
    )
    stale_indices = [
        index
        for index, content in enumerate(rendered)
        if index != latest_index
        and any(fragment in content for fragment in stale_fragments)
    ]
    authority_ordering = bool(
        summary_before_latest
        and all(
            any(phrase in rendered[index] for index in summary_indices)
            for phrase in authority_phrases
        )
        and all(index < latest_index for index in stale_indices)
        and not trailing_user_replay
    )

    observations = {
        expected_ids[0]: summary_before_latest,
        expected_ids[1]: preserved_exactly,
        expected_ids[2]: (
            latest_index >= 0
            and _latest_is_final_user_message(messages, latest)
            and not trailing_user_replay
        ),
        expected_ids[3]: authority_ordering,
    }
    return [
        {"id": assertion_id, "observed": bool(observations[assertion_id])}
        for assertion_id in expected_ids
    ]


def _run_host_chain(
    fixture: dict[str, object], host_root: Path
) -> tuple[dict[str, object], int, str]:
    """Run once in a fresh one-shot producer process."""

    _require_no_preloaded_host_modules(host_root)
    return _run_host_chain_one_shot(fixture, host_root)


def _run_host_chain_one_shot(
    fixture: dict[str, object], host_root: Path
) -> tuple[dict[str, object], int, str]:
    """Return the captured provider request from the real pinned-host chain."""

    history = _pressured_history(fixture)
    latest = fixture.get("latest_user_message")
    summary_text = fixture.get("compaction_summary")
    if not isinstance(latest, str) or not isinstance(summary_text, str):
        raise ValueError("fixture latest_user_message and compaction_summary must be strings")
    original_sys_path = list(sys.path)
    captured_requests: list[dict[str, object]] = []
    summary_calls: list[dict[str, object]] = []
    network_attempts: list[str] = []

    def blocked_connect(_socket: socket.socket, address: object) -> None:
        network_attempts.append(repr(address))
        raise AssertionError(f"network forbidden in host-compaction E2E: {address!r}")

    def blocked_connect_ex(_socket: socket.socket, address: object) -> int:
        blocked_connect(_socket, address)
        return 1

    def blocked_create_connection(
        address: object, *args: object, **kwargs: object
    ) -> None:
        network_attempts.append(repr(address))
        raise AssertionError(f"network forbidden in host-compaction E2E: {address!r}")

    def fake_summary_call(**kwargs: object) -> SimpleNamespace:
        summary_calls.append(copy.deepcopy(kwargs))
        return _summary_response(summary_text)

    def capture_transport(api_kwargs: dict[str, object]) -> SimpleNamespace:
        captured_requests.append(copy.deepcopy(api_kwargs))
        return _stop_response()

    with tempfile.TemporaryDirectory(prefix="datasage-host-compaction-") as temp_home:
        test_env = {
            "HERMES_HOME": temp_home,
            "HERMES_SKIP_CHMOD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        sys.path.insert(0, str(host_root))
        try:
            with (
                patch.dict(os.environ, test_env, clear=False),
                patch.object(socket.socket, "connect", blocked_connect),
                patch.object(socket.socket, "connect_ex", blocked_connect_ex),
                patch("socket.create_connection", side_effect=blocked_create_connection),
            ):
                env_loader = importlib.import_module("hermes_cli.env_loader")
                config_module = importlib.import_module("hermes_cli.config")
                with (
                    patch.object(env_loader, "load_hermes_dotenv", return_value=[]),
                    patch.object(config_module, "load_config", side_effect=_fixture_config),
                    patch.object(
                        config_module, "load_config_readonly", side_effect=_fixture_config
                    ),
                ):
                    run_agent = importlib.import_module("run_agent")
                    compressor_module = importlib.import_module("agent.context_compressor")
                    hermes_logging = importlib.import_module("hermes_logging")
                    _validate_host_module_origins(dict(sys.modules), host_root)
                    with (
                        contextlib.redirect_stdout(io.StringIO()),
                        patch.object(run_agent, "get_tool_definitions", return_value=[]),
                        patch.object(
                            run_agent, "check_toolset_requirements", return_value={}
                        ),
                        patch.object(run_agent, "OpenAI"),
                    ):
                        agent = run_agent.AIAgent(
                            base_url="https://fixture.invalid/v1",
                            api_key="test-key-not-a-secret",
                            provider="custom",
                            api_mode="chat_completions",
                            model="test/model",
                            max_iterations=4,
                            enabled_toolsets=[],
                            disabled_toolsets=[],
                            save_trajectories=False,
                            quiet_mode=True,
                            skip_context_files=True,
                            skip_memory=True,
                            skip_background_review=True,
                        )

                    agent.client = MagicMock()
                    agent._use_prompt_caching = False
                    agent._disable_streaming = True
                    agent.tool_delay = 0
                    agent.save_trajectories = False
                    agent.context_compressor.threshold_tokens = 20_000
                    agent.context_compressor.tail_token_budget = 2_048
                    starting_compressions = agent.context_compressor.compression_count
                    soul = (PROFILE_ROOT / "SOUL.md").read_text(encoding="utf-8")

                    try:
                        with (
                            contextlib.redirect_stdout(io.StringIO()),
                            patch(
                                "agent.context_compressor.call_llm",
                                side_effect=fake_summary_call,
                            ),
                            patch.object(
                                agent,
                                "_interruptible_api_call",
                                side_effect=capture_transport,
                            ),
                        ):
                            result = agent.run_conversation(
                                latest,
                                system_message=soul,
                                conversation_history=history,
                            )

                        compression_count = (
                            agent.context_compressor.compression_count
                            - starting_compressions
                        )
                        summary_prefix = compressor_module.SUMMARY_PREFIX
                    finally:
                        # Stop the official asynchronous file logger before
                        # TemporaryDirectory removes its Windows lock file.
                        hermes_logging._reset_queued_handlers()
        finally:
            sys.path[:] = original_sys_path

    if network_attempts:
        raise AssertionError(f"network attempts were blocked: {network_attempts!r}")
    if not result.get("completed"):
        raise AssertionError(f"AIAgent.run_conversation did not complete: {result!r}")
    if compression_count < 1:
        raise AssertionError("real ContextCompressor.compress did not commit a compression")
    if not summary_calls:
        raise AssertionError("real compressor never reached its auxiliary summary boundary")
    if len(captured_requests) != 1:
        raise AssertionError(
            f"expected one final provider request, got {len(captured_requests)}"
        )

    captured_request = captured_requests[0]
    messages = captured_request.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(message, dict) for message in messages
    ):
        raise TypeError("captured provider request has no dictionary messages list")

    return captured_request, compression_count, summary_prefix


def build_evidence(*, enforce_clean: bool = True) -> dict[str, object]:
    """Build raw evidence bound only to committed, tracked source blobs."""

    fixture = _read_json_object(FIXTURE_PATH)
    host_root = _host_root()
    host_identity = _host_identity(host_root)
    _validate_pinned_host(fixture, host_identity)

    producer_repo_path = _git_repo_path(PRODUCER_RELATIVE_PATH)
    fixture_repo_path = _git_repo_path(FIXTURE_RELATIVE_PATH)
    _require_tracked_source(producer_repo_path)
    _require_tracked_source(fixture_repo_path)
    if enforce_clean:
        _validate_release_preconditions(host_root)

    profile_commit = _git_commit(PROFILE_REPO)
    producer_sha256 = _git_blob_sha256(
        PROFILE_REPO, profile_commit, producer_repo_path
    )
    fixture_sha256 = _git_blob_sha256(
        PROFILE_REPO, profile_commit, fixture_repo_path
    )
    captured_request, compression_count, _summary_prefix = _run_host_chain(
        fixture, host_root
    )

    return {
        "schema": EVIDENCE_SCHEMA,
        "subject": _load_release_subject(profile_commit),
        "host": host_identity,
        "fixture": {
            "path": FIXTURE_RELATIVE_PATH,
            "sha256": fixture_sha256,
        },
        "producer": {
            "path": PRODUCER_RELATIVE_PATH,
            "sha256": producer_sha256,
        },
        "compression_count": compression_count,
        "captured_request_sha256": _sha256_bytes(
            _canonical_json_bytes(captured_request)
        ),
        "captured_request": captured_request,
    }


def _build_test_child_payload() -> dict[str, object]:
    fixture = _read_json_object(FIXTURE_PATH)
    host_root = _host_root()
    _validate_pinned_host(fixture, _host_identity(host_root))
    captured_request, compression_count, summary_prefix = _run_host_chain(
        fixture, host_root
    )
    messages = captured_request.get("messages")
    if not isinstance(messages, list):
        raise TypeError("captured provider request has no messages list")
    observations = _observe_assertions(
        fixture, messages, summary_prefix=summary_prefix
    )
    latest = fixture.get("latest_user_message")
    if not isinstance(latest, str):
        raise TypeError("fixture latest_user_message must be a string")
    return {
        "schema": TEST_CHILD_SCHEMA,
        "compression_count": compression_count,
        "captured_request": captured_request,
        "captured_request_sha256": _sha256_bytes(
            _canonical_json_bytes(captured_request)
        ),
        "assertions": observations,
        "latest_is_final_user_message": _latest_is_final_user_message(
            messages, latest
        ),
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)


class HostCompactionEvidenceTest(unittest.TestCase):
    def test_build_evidence_rejects_current_untracked_producer(self) -> None:
        producer_repo_path = _git_repo_path(PRODUCER_RELATIVE_PATH)
        if _git_is_tracked(PROFILE_REPO, producer_repo_path):
            self.skipTest("formal evidence is exercised by the committed one-shot CLI")
        with self.assertRaisesRegex(RuntimeError, "must be tracked by Git"):
            build_evidence(enforce_clean=False)

    def test_raw_host_chain_runs_in_one_shot_child(self) -> None:
        child_env = dict(os.environ)
        child_env["HERMES_AGENT_ROOT"] = str(_host_root())
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [sys.executable, "-B", str(PRODUCER_PATH), "--_test-raw-child"]
        original_modules = _snapshot_modules()
        completed = subprocess.run(
            command,
            cwd=PROFILE_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        restored_modules = _snapshot_modules()
        self.assertEqual(set(restored_modules), set(original_modules))
        for name, module in original_modules.items():
            self.assertIs(restored_modules[name], module, name)

        stderr_class = _classify_child_stderr(completed.stderr)
        self.assertIn(
            stderr_class, {"empty", "known_official_relay_destructor_noise"}
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"child stderr class={stderr_class}\n{completed.stderr}",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload.get("schema"), TEST_CHILD_SCHEMA)
        self.assertGreaterEqual(payload.get("compression_count", 0), 1)
        self.assertTrue(payload.get("latest_is_final_user_message"))
        observations = payload.get("assertions")
        self.assertIsInstance(observations, list)
        self.assertTrue(all(item.get("observed") for item in observations))
        captured_request = payload.get("captured_request")
        self.assertIsInstance(captured_request, dict)
        self.assertEqual(
            payload.get("captured_request_sha256"),
            _sha256_bytes(_canonical_json_bytes(captured_request)),
        )

    def test_wrong_model_tools_origin_validator_fails_closed(self) -> None:
        host_root = _host_root()
        wrong_module = ModuleType("model_tools")
        wrong_module.__file__ = str(PROFILE_ROOT / "wrong" / "model_tools.py")
        with self.assertRaisesRegex(RuntimeError, "outside pinned host: model_tools"):
            _validate_host_module_origins({"model_tools": wrong_module}, host_root)

        namespace_module = ModuleType("model_tools")
        with self.assertRaisesRegex(RuntimeError, "no verifiable __file__.*model_tools"):
            _validate_host_module_origins({"model_tools": namespace_module}, host_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the pinned Hermes host-compaction E2E evidence producer."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Explicit raw evidence destination; omitted means stdout only.",
    )
    parser.add_argument(
        "--_test-raw-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args._test_raw_child:
        if args.output is not None:
            parser.error("--_test-raw-child cannot be combined with --output")
        payload = _build_test_child_payload()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if (
            payload["latest_is_final_user_message"]
            and all(item["observed"] for item in payload["assertions"])
        ) else 1

    profile_commit = _git_commit(PROFILE_REPO)
    expected_output = (
        PROFILE_ROOT
        / "pending"
        / "evidence"
        / f"host-compaction-{profile_commit}.json"
    ).resolve()
    if args.output is not None:
        requested_output = args.output.expanduser().resolve()
        if requested_output != expected_output:
            parser.error(f"--output must be exactly: {expected_output}")
        if requested_output.exists():
            parser.error(f"refusing to overwrite existing evidence: {requested_output}")

    report = build_evidence(enforce_clean=True)
    if args.output is None:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _write_report(expected_output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
