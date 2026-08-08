"""Stable CURRENT-aware supervisor for the installed DataSage canary.

The immutable release unit remains the authority for code and configuration.
Hermes still needs a writable HERMES_HOME, so this supervisor hydrates a
manifest-verified working view while carrying forward only explicitly allowed
runtime state.  It also owns RUNNING.json for the entire Gateway lifetime.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping
import uuid


CONTROL_ROOT = Path(__file__).resolve().parent


def _verify_control_package_before_import() -> None:
    if os.environ.get("DATASAGE_REQUIRE_CONTROL_MANIFEST") != "1":
        return
    manifest_path = CONTROL_ROOT / "CONTROL_MANIFEST.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("installed control manifest is unavailable") from exc
    files = value.get("files") if isinstance(value, Mapping) else None
    if (
        not isinstance(files, list)
        or not files
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"path", "sha256"}
            or not isinstance(item.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
            for item in files
        )
    ):
        raise RuntimeError("installed control manifest is invalid")
    for item in files:
        relative = Path(str(item["path"]).replace("\\", "/"))
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or ":" in str(item["path"])
        ):
            raise RuntimeError("installed control manifest path is unsafe")
        target = CONTROL_ROOT / relative
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise RuntimeError(
                f"installed control file hash mismatch: {item['path']}"
            )
    expected_paths = {
        str(item["path"]).replace("\\", "/")
        for item in files
    }
    actual_paths = {
        path.relative_to(CONTROL_ROOT).as_posix()
        for path in CONTROL_ROOT.rglob("*")
        if path.is_file() and path.name != "CONTROL_MANIFEST.json"
    }
    if actual_paths != expected_paths:
        raise RuntimeError("installed control file set is inconsistent")


_verify_control_package_before_import()
sys.path.insert(0, str(CONTROL_ROOT))

from evaluation import current_runtime_launcher as launcher  # noqa: E402


SHA256 = re.compile(r"[0-9a-f]{64}")
MUTABLE_TOP_LEVEL = frozenset(
    {
        ".env",
        ".hermes_history",
        ".update_check",
        "audio_cache",
        "auth.json",
        "auth.lock",
        "cache",
        "channel_directory.json",
        "cron",
        "gateway-service",
        "gateway-starts.log",
        "gateway.lock",
        "gateway.pid",
        "gateway_state.json",
        "home",
        "hooks",
        "image_cache",
        "lsp",
        "logs",
        "memories",
        "models_dev_cache.json",
        "ollama_cloud_models_cache.json",
        "pairing",
        "pastes",
        "pending_messages",
        "plans",
        "platforms",
        "processes.json",
        "provider_models_cache.json",
        "sandboxes",
        "sessions",
        "skins",
        "state",
        "state.db",
        "state.db-shm",
        "state.db-wal",
        "verification_evidence.db",
        "verification_evidence.db-shm",
        "verification_evidence.db-wal",
        "workspace",
    }
)
CONTROL_OWNED_TOP_LEVEL = frozenset({".release", ".runtime"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _safe_relative(raw: str) -> Path:
    normalized = raw.replace("\\", "/")
    value = Path(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or value.is_absolute()
        or any(part in {"", ".", ".."} for part in value.parts)
        or ":" in normalized
    ):
        raise RuntimeError("Profile manifest contains an unsafe path")
    return value


def _manifest(profile: Path) -> dict[Path, str]:
    path = profile / ".release" / "MANIFEST.sha256"
    if _is_reparse(path) or not path.is_file():
        raise RuntimeError("Profile manifest is missing or unsafe")
    records: dict[Path, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "  " not in line:
            raise RuntimeError("Profile manifest record is malformed")
        digest, raw = line.split("  ", 1)
        relative = _safe_relative(raw)
        if not SHA256.fullmatch(digest) or relative in records:
            raise RuntimeError("Profile manifest record is invalid")
        records[relative] = digest
    if not records:
        raise RuntimeError("Profile manifest is empty")
    return records


def _verify_manifest(profile: Path) -> dict[Path, str]:
    if _is_reparse(profile) or not profile.is_dir():
        raise RuntimeError("Profile view is missing or unsafe")
    records = _manifest(profile)
    for relative, expected in records.items():
        path = profile / relative
        if _is_reparse(path) or not path.is_file():
            raise RuntimeError(f"Profile manifest file is missing: {relative}")
        if _hash_file(path) != expected:
            raise RuntimeError(f"Profile manifest hash mismatch: {relative}")
    return records


def _assert_safe_tree(path: Path, label: str) -> None:
    if _is_reparse(path):
        raise RuntimeError(f"{label} is a link or reparse point")
    if path.is_dir():
        for child in path.rglob("*"):
            if _is_reparse(child):
                raise RuntimeError(f"{label} contains a link or reparse point")


def _make_writable(path: Path) -> None:
    if not path.exists():
        return
    for child in [path, *path.rglob("*")]:
        try:
            child.chmod(0o755 if child.is_dir() else 0o600)
        except OSError as exc:
            raise RuntimeError("unable to make runtime view writable") from exc


def _copy_mutable_item(source: Path, target: Path) -> None:
    _assert_safe_tree(source, f"mutable item {source.name}")
    if source.is_dir():
        shutil.copytree(source, target)
    elif source.is_file():
        shutil.copy2(source, target)
    else:
        raise RuntimeError(f"mutable item has unsupported type: {source.name}")
    _make_writable(target)


def _existing_mutable_items(home: Path) -> list[Path]:
    if not home.exists():
        return []
    if _is_reparse(home) or not home.is_dir():
        raise RuntimeError("existing runtime home is unsafe")
    old_manifest = _manifest(home)
    owned_roots = {path.parts[0] for path in old_manifest}
    mutable: list[Path] = []
    for item in home.iterdir():
        if item.name in owned_roots or item.name in CONTROL_OWNED_TOP_LEVEL:
            continue
        if item.name not in MUTABLE_TOP_LEVEL:
            raise RuntimeError(
                f"unclassified top-level runtime item blocks hydration: {item.name}"
            )
        mutable.append(item)
    return sorted(mutable, key=lambda item: item.name.casefold())


def _atomic_copy_file(source: Path, target: Path, operation: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{operation}.tmp"
    if temporary.exists():
        raise RuntimeError("in-place hydration temporary path collision")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def _hydrate_in_place(
    immutable_profile: Path,
    runtime_home: Path,
    backup_root: Path,
    *,
    release_unit_id: str,
    operation: str,
) -> Path:
    """Windows fallback when the profile root is held as another process CWD."""
    backup = backup_root / f"{runtime_home.name}.{operation}"
    if backup.exists():
        raise RuntimeError("in-place hydration backup path collision")
    old_records = _verify_manifest(runtime_home)
    new_records = _verify_manifest(immutable_profile)
    if set(old_records) != set(new_records):
        raise RuntimeError(
            "in-place hydration requires an unchanged release path topology"
        )
    _assert_safe_tree(runtime_home, "existing runtime home")
    shutil.copytree(runtime_home, backup)
    _verify_manifest(backup)
    release_files = (
        Path(".release") / "MANIFEST.sha256",
        Path(".release") / "RELEASE.json",
    )
    try:
        for relative in sorted(new_records, key=lambda item: item.as_posix()):
            _atomic_copy_file(
                immutable_profile / relative,
                runtime_home / relative,
                operation,
            )
        for relative in release_files:
            _atomic_copy_file(
                immutable_profile / relative,
                runtime_home / relative,
                operation,
            )
        marker_dir = runtime_home / ".runtime"
        marker_dir.mkdir(exist_ok=True)
        marker = marker_dir / "RUNTIME_VIEW.json"
        temporary_marker = marker_dir / f".RUNTIME_VIEW.{operation}.tmp"
        temporary_marker.write_text(
            json.dumps(
                {
                    "version": "datasage-runtime-view/v1",
                    "release_unit_id": release_unit_id,
                    "hydrated_at": _utc_now(),
                    "hydration_mode": "atomic_file_replace",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary_marker, marker)
        _verify_manifest(runtime_home)
    except Exception:
        for relative in sorted(old_records, key=lambda item: item.as_posix()):
            _atomic_copy_file(
                backup / relative,
                runtime_home / relative,
                operation + ".restore",
            )
        for relative in release_files:
            _atomic_copy_file(
                backup / relative,
                runtime_home / relative,
                operation + ".restore",
            )
        raise
    return backup


def hydrate_runtime_home(
    immutable_profile: Path,
    runtime_home: Path,
    backup_root: Path,
    *,
    release_unit_id: str,
) -> tuple[Path, Path | None]:
    """Atomically replace the writable view and preserve allowlisted state."""
    _verify_manifest(immutable_profile)
    runtime_home = Path(os.path.abspath(runtime_home))
    if runtime_home.parent.name.casefold() != "profiles":
        raise RuntimeError("runtime home parent must be named profiles")
    runtime_home.parent.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse(runtime_home.parent) or _is_reparse(backup_root):
        raise RuntimeError("runtime home or backup root is unsafe")
    if runtime_home.exists():
        marker = runtime_home / ".runtime" / "RUNTIME_VIEW.json"
        try:
            marker_value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker_value = {}
        if (
            isinstance(marker_value, Mapping)
            and marker_value.get("release_unit_id") == release_unit_id
            and _verify_manifest(runtime_home) == _manifest(immutable_profile)
        ):
            return runtime_home, None

    operation = uuid.uuid4().hex
    stage = runtime_home.parent / f".{runtime_home.name}.stage.{operation}"
    backup = backup_root / f"{runtime_home.name}.{operation}"
    local_backup = (
        runtime_home.parent / f".{runtime_home.name}.backup.{operation}"
    )
    if stage.exists() or backup.exists() or local_backup.exists():
        raise RuntimeError("runtime hydration path collision")
    mutable = _existing_mutable_items(runtime_home)
    old_home_moved = False
    final_backup: Path | None = None
    try:
        shutil.copytree(immutable_profile, stage)
        _make_writable(stage)
        _verify_manifest(stage)
        for source in mutable:
            target = stage / source.name
            if target.exists():
                raise RuntimeError(
                    f"mutable item collides with release content: {source.name}"
                )
            _copy_mutable_item(source, target)
        marker_dir = stage / ".runtime"
        marker_dir.mkdir()
        (marker_dir / "RUNTIME_VIEW.json").write_text(
            json.dumps(
                {
                    "version": "datasage-runtime-view/v1",
                    "release_unit_id": release_unit_id,
                    "hydrated_at": _utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _verify_manifest(stage)
        if runtime_home.exists():
            try:
                runtime_home.replace(local_backup)
                old_home_moved = True
            except PermissionError:
                shutil.rmtree(stage)
                final_backup = _hydrate_in_place(
                    immutable_profile,
                    runtime_home,
                    backup_root,
                    release_unit_id=release_unit_id,
                    operation=operation,
                )
                return runtime_home, final_backup
        stage.replace(runtime_home)
        if old_home_moved:
            try:
                local_backup.replace(backup)
                final_backup = backup
            except OSError:
                # The safety invariant is the completed sibling backup, not
                # its archival parent.  Some Windows security providers deny
                # cross-parent directory renames even on the same volume.
                final_backup = local_backup
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if (
            old_home_moved
            and not runtime_home.exists()
            and local_backup.exists()
        ):
            local_backup.replace(runtime_home)
        raise
    return runtime_home, final_backup


def _resolve_current(root: Path) -> tuple[str, Path, Path, Mapping[str, Any]]:
    root = launcher.initialize_root(root)
    unit_id = launcher._read_current_once(root)
    unit = launcher._safe_unit(root, unit_id)
    trust = launcher._load_trust_root(root)
    release, _contract = launcher.verify_launchable_unit(unit, trust)
    checkout = launcher.materialize_hermes_checkout(root, unit, release)
    profile = launcher.materialize_runtime_profile(root, unit, release)
    return unit_id, checkout, profile, release


def _resolve_current_profile_only(
    root: Path,
) -> tuple[str, Path, Path, Mapping[str, Any]]:
    """Resolve a health-verified CURRENT without rewalking cached Hermes."""
    root = launcher.initialize_root(root)
    unit_id = launcher._read_current_once(root)
    unit = launcher._safe_unit(root, unit_id)
    trust = launcher._load_trust_root(root)
    release, _contract = launcher.verify_launchable_unit(unit, trust)
    profile = launcher.materialize_runtime_profile(root, unit, release)
    slot = hashlib.sha256(unit_id.encode("utf-8")).hexdigest()[:20]
    checkout = root / "state" / "hermes-checkouts" / slot
    launcher._require_plain_directory(
        checkout,
        parent=root / "state" / "hermes-checkouts",
        label="health-verified Hermes checkout slot",
    )
    if (
        _is_reparse(checkout / "hermes_cli" / "__init__.py")
        or not (checkout / "hermes_cli" / "__init__.py").is_file()
        or _is_reparse(checkout / "uv.lock")
        or not (checkout / "uv.lock").is_file()
        or _hash_file(checkout / "uv.lock")
        != release["hermes"]["uv_lock_sha256"]
    ):
        raise RuntimeError("health-verified Hermes checkout anchor is invalid")
    committed = []
    for receipt in (root / "journal").glob("health.*.committed.json"):
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(value, Mapping)
            and value.get("release_unit_id") == unit_id
            and value.get("status") == "committed"
            and value.get("runtime_executed") is True
            and value.get("health_checked") is True
        ):
            committed.append(receipt)
    if not committed:
        raise RuntimeError("CURRENT has no committed offline health evidence")
    return unit_id, checkout, profile, release


def hydrate_current_home(
    root: Path,
    runtime_home: Path,
    backup_root: Path,
) -> dict[str, Any]:
    root = launcher.initialize_root(root)
    with launcher.control_operation(root, "gateway-hydrate") as token:
        with launcher.root_lock(root):
            launcher._require_control_access(
                root,
                token,
                is_alive=launcher._default_liveness,
                process_identity=launcher._process_start_identity,
            )
            if launcher._running_lease(
                root,
                is_alive=launcher._default_liveness,
                process_identity=launcher._process_start_identity,
            ) is not None:
                raise RuntimeError("cannot hydrate while Gateway is running")
        unit_id, checkout, immutable_profile, release = (
            _resolve_current_profile_only(root)
        )
        hydrated, backup = hydrate_runtime_home(
            immutable_profile,
            runtime_home,
            backup_root,
            release_unit_id=unit_id,
        )
    return {
        "status": "hydrated",
        "release_unit_id": unit_id,
        "profile_artifact_id": release["profile"]["artifact_id"],
        "hermes_commit": release["hermes"]["commit"],
        "runtime_home": str(hydrated),
        "hermes_checkout": str(checkout),
        "backup_created": backup is not None,
        "backup_name": backup.name if backup is not None else None,
    }


def prepare(
    root: Path,
    runtime_home: Path,
    backup_root: Path,
) -> dict[str, Any]:
    root = launcher.initialize_root(root)
    with launcher.control_operation(root, "gateway-prepare") as token:
        with launcher.root_lock(root):
            launcher._require_control_access(
                root,
                token,
                is_alive=launcher._default_liveness,
                process_identity=launcher._process_start_identity,
            )
            if launcher._running_lease(
                root,
                is_alive=launcher._default_liveness,
                process_identity=launcher._process_start_identity,
            ) is not None:
                raise RuntimeError("cannot hydrate while Gateway is running")
        unit_id, checkout, immutable_profile, release = _resolve_current(root)
        hydrated, backup = hydrate_runtime_home(
            immutable_profile,
            runtime_home,
            backup_root,
            release_unit_id=unit_id,
        )
    return {
        "status": "prepared",
        "release_unit_id": unit_id,
        "profile_artifact_id": release["profile"]["artifact_id"],
        "hermes_commit": release["hermes"]["commit"],
        "runtime_home": str(hydrated),
        "hermes_checkout": str(checkout),
        "backup_created": backup is not None,
        "backup_name": backup.name if backup is not None else None,
    }


def _runtime_environment(runtime_home: Path, checkout: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONBREAKPOINT",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "HERMES_HOME": str(runtime_home),
            "PYTHONPATH": str(checkout),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "DATASAGE_RUNTIME_STATE": str(runtime_home),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return environment


def probe(
    root: Path,
    runtime_home: Path,
    backup_root: Path,
    python: Path,
) -> dict[str, Any]:
    prepared = prepare(root, runtime_home, backup_root)
    checkout = Path(prepared["hermes_checkout"])
    command = [
        str(python),
        "-B",
        "-c",
        (
            "import json, pathlib, hermes_cli; "
            "print(json.dumps({'hermes_cli': str(pathlib.Path("
            "hermes_cli.__file__).resolve())}, ensure_ascii=False))"
        ),
    ]
    completed = subprocess.run(
        command,
        cwd=checkout,
        env=_runtime_environment(runtime_home, checkout),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        ),
    )
    if completed.returncode != 0:
        raise RuntimeError("exact Hermes import probe failed")
    try:
        identity = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("exact Hermes import probe output is invalid") from exc
    origin = Path(str(identity.get("hermes_cli") or "")).resolve()
    if checkout.resolve() not in origin.parents:
        raise RuntimeError("Hermes import escaped the selected checkout")
    prepared.update(
        status="probed",
        python_sha256=_hash_file(python),
        hermes_cli_origin=str(origin),
    )
    return prepared


def run_gateway(
    root: Path,
    runtime_home: Path,
    backup_root: Path,
    python: Path,
) -> int:
    prepared = prepare(root, runtime_home, backup_root)
    root = launcher.initialize_root(root)
    unit_id = str(prepared["release_unit_id"])
    checkout = Path(prepared["hermes_checkout"])
    operation = uuid.uuid4().hex
    running = root / "state" / "RUNNING.json"
    start_identity = launcher._process_start_identity(os.getpid())
    if start_identity is None:
        raise RuntimeError("supervisor process identity is unavailable")
    lease = {
        "version": "datasage-gateway-lease/v1",
        "pid": os.getpid(),
        "process_start_id": start_identity,
        "started_at": _utc_now(),
        "unit_id": unit_id,
        "token": uuid.uuid4().hex,
        "supervisor_sha256": _hash_file(Path(__file__)),
    }
    with launcher.root_lock(root):
        if launcher._running_lease(
            root,
            is_alive=launcher._default_liveness,
            process_identity=launcher._process_start_identity,
        ) is not None:
            raise RuntimeError("Gateway is already running")
        if launcher._read_current_once(root) != unit_id:
            raise RuntimeError("CURRENT changed before Gateway lease acquisition")
        launcher._atomic_json(running, lease)

    command = [
        str(python),
        "-B",
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
        "--external-supervisor",
    ]
    start_receipt = root / "journal" / f"gateway.{operation}.started.json"
    exit_receipt = root / "journal" / f"gateway.{operation}.exited.json"
    child: subprocess.Popen[str] | None = None
    try:
        child = subprocess.Popen(
            command,
            cwd=checkout,
            env=_runtime_environment(runtime_home, checkout),
            stdin=subprocess.DEVNULL,
            text=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        child_identity = launcher._process_start_identity(child.pid)
        lease["child_pid"] = child.pid
        lease["child_process_start_id"] = child_identity
        launcher._atomic_json(running, lease)
        launcher._atomic_json(
            start_receipt,
            {
                "version": "datasage-gateway-receipt/v1",
                "status": "started",
                "operation_id": operation,
                "release_unit_id": unit_id,
                "profile_artifact_id": prepared["profile_artifact_id"],
                "hermes_commit": prepared["hermes_commit"],
                "supervisor_pid": os.getpid(),
                "child_pid": child.pid,
                "started_at": _utc_now(),
            },
        )
        return_code = child.wait()
        launcher._atomic_json(
            exit_receipt,
            {
                "version": "datasage-gateway-receipt/v1",
                "status": "exited",
                "operation_id": operation,
                "release_unit_id": unit_id,
                "return_code": return_code,
                "exited_at": _utc_now(),
            },
        )
        return return_code
    finally:
        archive = root / "journal" / f"gateway.{operation}.lease-ended.json"
        if running.exists():
            launcher._claim_lease(
                running,
                archive,
                expected_token=lease["token"],
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, probe, or supervise the CURRENT DataSage Gateway."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument(
        "command",
        choices=("hydrate", "prepare", "probe", "run"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "hydrate":
            result = hydrate_current_home(
                args.root,
                args.runtime_home,
                args.backup_root,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "prepare":
            result = prepare(args.root, args.runtime_home, args.backup_root)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.python is None or not args.python.is_file():
            raise RuntimeError("--python must be an existing absolute file")
        if not args.python.is_absolute() or _is_reparse(args.python):
            raise RuntimeError("--python is unsafe")
        if args.command == "probe":
            result = probe(
                args.root,
                args.runtime_home,
                args.backup_root,
                args.python,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        return run_gateway(
            args.root,
            args.runtime_home,
            args.backup_root,
            args.python,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            f"DataSage Gateway bootstrap failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
