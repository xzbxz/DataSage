"""Explicit ownership contract for writable DataSage profile-root state.

The immutable profile manifest owns release content and the DSRT bootstrap owns
``.release``/``.runtime``.  Every other top-level item must be named here or
hydration fails closed.  This contract intentionally has no glob or suffix
rules: adding a new Hermes runtime artifact requires source evidence and a new
control-plane identity.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeRoot:
    name: str
    kind: str
    lifecycle: tuple[str, ...]
    evidence: str


RUNTIME_ROOTS = (
    # Profile-local credentials and initialization state.
    RuntimeRoot(".env", "file", ("bootstrap",), "hermes_constants.py:1175"),
    RuntimeRoot("auth.json", "file", ("bootstrap",), "agent/auxiliary_client.py:767"),
    RuntimeRoot("auth.lock", "file", ("bootstrap",), "hermes_cli/auth.py"),
    RuntimeRoot("audio_cache", "directory", ("bootstrap", "session"), "hermes_cli/config.py"),
    RuntimeRoot("image_cache", "directory", ("bootstrap", "session"), "hermes_cli/config.py"),
    RuntimeRoot("cache", "directory", ("bootstrap", "session"), "agent/model_metadata.py:130"),
    RuntimeRoot("cron", "directory", ("bootstrap", "session"), "cron/scheduler.py:586"),
    RuntimeRoot("hooks", "directory", ("bootstrap", "session"), "gateway/hooks.py:49"),
    RuntimeRoot("memories", "directory", ("bootstrap", "session"), "agent/learning_mutations.py:33"),
    RuntimeRoot("pairing", "directory", ("bootstrap", "session"), "gateway/pairing.py"),
    RuntimeRoot("platforms", "directory", ("bootstrap", "session"), "gateway/pairing.py"),
    RuntimeRoot("skins", "directory", ("bootstrap",), "hermes_cli/config.py:9366"),
    RuntimeRoot("home", "directory", ("bootstrap", "agent_smoke"), "hermes_constants.py profile HOME isolation"),
    # Gateway process lifecycle and its exact restart/drain protocol markers.
    RuntimeRoot("gateway.lock", "file", ("gateway_start", "planned_shutdown"), "hermes_cli/gateway_windows.py:1321"),
    RuntimeRoot("gateway.pid", "file", ("gateway_start", "planned_shutdown"), "hermes_cli/gateway_windows.py:1320"),
    RuntimeRoot("gateway_state.json", "file", ("gateway_start", "session", "planned_shutdown"), "hermes_cli/gateway_windows.py:1322"),
    RuntimeRoot("gateway-starts.log", "file", ("gateway_start", "restart"), "gateway/status.py:67"),
    RuntimeRoot("cron.pid", "file", ("gateway_start", "planned_shutdown"), "hermes_cli/backup.py:_EXCLUDED_NAMES"),
    RuntimeRoot("processes.json", "file", ("gateway_start", "session", "planned_shutdown"), "hermes_cli/container_boot.py:75"),
    RuntimeRoot("state", "directory", ("gateway_start", "session", "planned_shutdown"), "gateway/lifecycle_ledger.py:_LIFECYCLE_RELATIVE"),
    RuntimeRoot("logs", "directory", ("gateway_start", "agent_smoke", "planned_shutdown"), "gateway/lifecycle_ledger.py:_EXIT_DIAG_RELATIVE"),
    RuntimeRoot("gateway", "directory", ("gateway_start", "restart"), "gateway/restart_loop_guard.py:48"),
    RuntimeRoot(".drain_request.json", "file", ("planned_shutdown",), "gateway/drain_control.py:64"),
    RuntimeRoot(".restart_failure_counts", "file", ("restart",), "gateway/run.py:7112"),
    RuntimeRoot(".restart_last_processed.json", "file", ("restart",), "gateway/run.py:15423"),
    RuntimeRoot(".restart_notify.json", "file", ("restart",), "gateway/run.py:17763"),
    RuntimeRoot(".restart_pending.json", "file", ("restart",), "gateway/run.py:1641"),
    RuntimeRoot(".clean_shutdown", "file", ("planned_shutdown", "restart"), "gateway/run.py:10121"),
    # Prompt, session, channel, and agent state exercised by DataSage's
    # gateway + safe-mode agent lifecycle.
    RuntimeRoot(".skills_prompt_snapshot.json", "file", ("agent_smoke", "session", "restart"), "agent/prompt_builder.py:1330"),
    RuntimeRoot(".hermes_history", "file", ("agent_smoke", "session"), "cli.py:4517"),
    RuntimeRoot(".update_check", "file", ("agent_smoke",), "hermes_cli/banner.py:273"),
    RuntimeRoot("channel_directory.json", "file", ("gateway_start", "session", "session_reset"), "gateway/channel_directory.py:21"),
    RuntimeRoot("channel_aliases.json", "file", ("session", "session_reset"), "gateway/channel_directory.py:36"),
    RuntimeRoot("sessions", "directory", ("agent_smoke", "session", "session_reset"), "gateway/config.py:894"),
    RuntimeRoot("pending_messages", "directory", ("session", "planned_shutdown"), "gateway/shutdown_flush.py:38"),
    RuntimeRoot("state.db", "file", ("agent_smoke", "session", "session_reset", "planned_shutdown"), "hermes_state.py:284"),
    RuntimeRoot("state.db-shm", "file", ("session", "planned_shutdown"), "hermes_cli/backup.py:_EXCLUDED_SUFFIXES"),
    RuntimeRoot("state.db-wal", "file", ("session", "planned_shutdown"), "hermes_cli/backup.py:_EXCLUDED_SUFFIXES"),
    RuntimeRoot("state.db-journal", "file", ("session", "planned_shutdown"), "hermes_cli/backup.py:_EXCLUDED_SUFFIXES"),
    RuntimeRoot("models_dev_cache.json", "file", ("agent_smoke",), "agent/models_dev.py:190"),
    RuntimeRoot("ollama_cloud_models_cache.json", "file", ("agent_smoke",), "hermes_cli/models.py"),
    RuntimeRoot("provider_models_cache.json", "file", ("agent_smoke",), "hermes_cli/models.py"),
    RuntimeRoot("pastes", "directory", ("session",), "cli.py:15479"),
    RuntimeRoot("lsp", "directory", ("agent_smoke", "session"), "agent runtime workspace"),
    RuntimeRoot("plans", "directory", ("agent_smoke", "session"), "agent runtime workspace"),
    RuntimeRoot("sandboxes", "directory", ("agent_smoke", "session", "planned_shutdown"), "agent runtime workspace"),
    RuntimeRoot("workspace", "directory", ("agent_smoke", "session"), "config.yaml:terminal.cwd"),
)


CONTROL_OWNED_TOP_LEVEL = frozenset({".release", ".runtime"})
MUTABLE_TOP_LEVEL = frozenset(root.name for root in RUNTIME_ROOTS)

# Exact known contamination encountered in the reviewed profile.  Backup-like
# names not listed here remain unknown and therefore also fail closed.
FORBIDDEN_TOP_LEVEL = frozenset(
    {
        ".pytest_cache",
        "pytest.ini",
        "config.yaml.bak-20260807",
        "gateway-service",
        "verification_evidence.db",
        "verification_evidence.db-shm",
        "verification_evidence.db-wal",
        ".gateway-planned-stop.json",
        ".gateway-takeover.json",
        ".update_pending.json",
        ".update_pending.claimed.json",
        ".update_output.txt",
        ".update_exit_code",
        ".update_prompt.json",
        ".update_response",
    }
)


def roots_for_lifecycle(stage: str) -> frozenset[str]:
    return frozenset(root.name for root in RUNTIME_ROOTS if stage in root.lifecycle)


def classify_top_level(
    names: set[str] | frozenset[str],
    payload_owned: set[str] | frozenset[str],
) -> dict[str, frozenset[str]]:
    """Classify exact top-level names without inferring ownership by suffix."""
    all_names = frozenset(names)
    payload = all_names & frozenset(payload_owned)
    control = all_names & CONTROL_OWNED_TOP_LEVEL
    mutable = all_names & MUTABLE_TOP_LEVEL
    forbidden = all_names & FORBIDDEN_TOP_LEVEL
    classified = payload | control | mutable | forbidden
    return {
        "payload": payload,
        "control": control,
        "mutable": mutable,
        "forbidden": forbidden,
        "unknown": all_names - classified,
    }


def _validate_contract() -> None:
    names = [root.name for root in RUNTIME_ROOTS]
    if len(names) != len(set(names)):
        raise RuntimeError("duplicate runtime-root ownership entry")
    if MUTABLE_TOP_LEVEL & CONTROL_OWNED_TOP_LEVEL:
        raise RuntimeError("mutable and control-owned roots overlap")
    if MUTABLE_TOP_LEVEL & FORBIDDEN_TOP_LEVEL:
        raise RuntimeError("mutable and forbidden roots overlap")
    for root in RUNTIME_ROOTS:
        if (
            not root.name
            or root.name in {".", ".."}
            or "/" in root.name
            or "\\" in root.name
            or "*" in root.name
            or "?" in root.name
            or root.kind not in {"file", "directory"}
            or not root.lifecycle
            or not root.evidence
        ):
            raise RuntimeError(f"invalid runtime-root ownership entry: {root!r}")


_validate_contract()
