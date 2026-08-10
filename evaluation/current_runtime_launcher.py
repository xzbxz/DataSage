"""CURRENT-aware offline launcher for immutable DataSage runtime units."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from typing import Any, Callable, Iterator, Mapping
import uuid
import zipfile

from evaluation import build_atomic_runtime_release as atomic
from evaluation import rehearse_atomic_runtime_release as artifact


UNIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
SHA256 = re.compile(r"[0-9a-f]{64}")
HealthExecutor = Callable[
    [Path, Mapping[str, Any], Mapping[str, str]],
    Mapping[str, Any],
]
Liveness = Callable[[int], bool]
ProcessIdentity = Callable[[int], str | None]
SENSITIVE_ENVIRONMENT_PREFIXES = (
    "DATA_QUERY_",
    "WECOM_",
    "DEEPSEEK_",
)
OFFLINE_ENVIRONMENT_ALLOWLIST = {
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}


def launcher_python_runtime_identity() -> dict[str, str]:
    executable = Path(sys.executable)
    base_executable = Path(
        str(getattr(sys, "_base_executable", "") or sys.executable)
    )
    if (
        not executable.is_absolute()
        or not base_executable.is_absolute()
        or _is_link_or_reparse(executable)
        or _is_link_or_reparse(base_executable)
        or not executable.is_file()
        or not base_executable.is_file()
    ):
        raise RuntimeError("launcher Python runtime path is unsafe")
    if os.name == "nt":
        runtime_library = (
            Path(sys.base_prefix)
            / f"python{sys.version_info.major}{sys.version_info.minor}.dll"
        )
    else:
        library_name = sysconfig.get_config_var("LDLIBRARY")
        library_root = sysconfig.get_config_var("LIBDIR")
        runtime_library = (
            Path(str(library_root)) / str(library_name)
            if library_name and library_root
            else base_executable
        )
    if (
        _is_link_or_reparse(runtime_library)
        or not runtime_library.is_file()
    ):
        raise RuntimeError("launcher Python runtime library is unsafe")
    return {
        "implementation": str(sys.implementation.name),
        "version": platform.python_version(),
        "executable_sha256": atomic._hash_file(executable),
        "base_executable_sha256": atomic._hash_file(base_executable),
        "runtime_library_sha256": atomic._hash_file(runtime_library),
    }


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _plain_path(path: Path) -> Path:
    raw = str(path)
    if os.name == "nt" and raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return Path(raw)


def _extended_path_within(path: Path, allowed_root: Path) -> str:
    """Return an I/O path only after proving it remains below a plain root."""
    candidate = Path(os.path.abspath(_plain_path(path)))
    root = Path(os.path.abspath(_plain_path(allowed_root)))
    try:
        root_resolved = root.resolve(strict=True)
        candidate_resolved = candidate.resolve(strict=False)
        candidate.relative_to(root)
        candidate_resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise RuntimeError("extended path escapes its allowed root") from exc
    cursor = candidate if _lexists(candidate) else candidate.parent
    while True:
        if _lexists(cursor) and _is_link_or_reparse(cursor):
            raise RuntimeError("extended path crosses an unsafe alias")
        if cursor == root:
            break
        if cursor == cursor.parent:
            raise RuntimeError("extended path escapes its allowed root")
        cursor = cursor.parent
    rendered = str(candidate_resolved)
    return f"\\\\?\\{rendered}" if os.name == "nt" else rendered


def _copytree_within_roots(
    source: Path,
    target: Path,
    *,
    source_root: Path,
    target_root: Path,
) -> None:
    shutil.copytree(
        _extended_path_within(source, source_root),
        _extended_path_within(target, target_root),
    )


def _remove_tree_within_root(path: Path, allowed_root: Path) -> None:
    if not _lexists(_plain_path(path)):
        return
    raw = _extended_path_within(path, allowed_root)

    def make_writable(function, raw_path, _error):
        candidate = Path(raw_path)
        candidate.chmod(stat.S_IRWXU)
        function(raw_path)

    shutil.rmtree(raw, onerror=make_writable)


def _require_plain_directory(
    path: Path,
    *,
    parent: Path | None = None,
    label: str,
) -> Path:
    was_extended = os.name == "nt" and str(path).startswith("\\\\?\\")
    path = _plain_path(path)
    if not _lexists(path) or _is_link_or_reparse(path) or not path.is_dir():
        raise RuntimeError(f"{label} is missing or unsafe")
    absolute = Path(os.path.abspath(path))
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} identity is unreadable") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
        raise RuntimeError(f"{label} escapes its declared path")
    if parent is not None:
        parent = _plain_path(parent)
        parent_absolute = Path(os.path.abspath(parent))
        if (
            os.path.normcase(str(absolute.parent))
            != os.path.normcase(str(parent_absolute))
        ):
            raise RuntimeError(f"{label} escapes its declared parent")
        _require_plain_directory(parent, label=f"{label} parent")
    if was_extended and os.name == "nt":
        return Path(f"\\\\?\\{resolved}")
    return resolved


def _materialization_parent(root: Path, name: str) -> Path:
    state = root / "state"
    _require_plain_directory(state, parent=root, label="runtime state directory")
    parent = state / name
    try:
        parent.mkdir(exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"runtime {name} parent cannot be created") from exc
    _require_plain_directory(
        parent,
        parent=state,
        label=f"runtime {name} parent",
    )
    return parent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize_root(root: Path) -> Path:
    root = Path(os.path.abspath(root))
    if _lexists(root) and _is_link_or_reparse(root):
        raise RuntimeError("runtime root is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    _require_plain_directory(root, label="runtime root")
    for name in ("deployments", "state", "journal", "lock"):
        path = root / name
        if _lexists(path) and (
            not path.is_dir() or _is_link_or_reparse(path)
        ):
            raise RuntimeError(f"runtime root component is unsafe: {name}")
        path.mkdir(exist_ok=True)
        _require_plain_directory(
            path,
            parent=root,
            label=f"runtime root component {name}",
        )
    return root


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _default_liveness(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        # CPython distributions on Windows can surface ERROR_INVALID_PARAMETER
        # from os.kill(pid, 0) as an uncatchable-looking SystemError for a dead
        # PID.  The control plane already has an exact, handle-based process
        # identity probe, so use it for Windows liveness as well.
        return _process_start_identity(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_start_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not ctypes.windll.kernel32.GetProcessTimes(
                    process,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows-filetime:{value}"
        except (AttributeError, OSError):
            return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8"
        ).split()
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
        return f"proc:{boot_id}:{fields[21]}"
    except (OSError, IndexError):
        return None


def _read_lease(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"runtime lease is invalid: {path.name}") from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("pid"), int)
        or not isinstance(value.get("process_start_id"), str)
        or not value["process_start_id"]
        or not isinstance(value.get("started_at"), str)
        or not UNIT_ID.fullmatch(str(value.get("unit_id") or ""))
        or not isinstance(value.get("token"), str)
        or not value["token"]
    ):
        raise RuntimeError(f"runtime lease is incomplete: {path.name}")
    return value


def _lease_is_live(
    lease: Mapping[str, Any],
    *,
    is_alive: Liveness,
    process_identity: ProcessIdentity,
) -> bool:
    pid = int(lease["pid"])
    if not is_alive(pid):
        return False
    current = process_identity(pid)
    if current is None:
        # A live PID with unverifiable start identity is not safe to reap.
        return True
    return current == lease["process_start_id"]


def _claim_lease(
    source: Path,
    destination: Path,
    *,
    expected_token: str,
) -> dict[str, Any]:
    current = _read_lease(source)
    if current.get("token") != expected_token:
        raise RuntimeError("runtime lease token changed before claim")
    os.replace(source, destination)
    claimed = _read_lease(destination)
    if claimed.get("token") != expected_token:
        raise RuntimeError("runtime lease token changed during claim")
    return claimed


def _archive_stale(
    root: Path,
    source: Path,
    kind: str,
    value: Mapping[str, Any],
) -> None:
    destination = (
        root
        / "journal"
        / f"{kind}.stale.{uuid.uuid4().hex}.json"
    )
    claimed = _claim_lease(
        source,
        destination,
        expected_token=str(value["token"]),
    )
    _atomic_json(
        destination,
        {
            **claimed,
            "status": "stale_archived",
            "archived_at": _utc_now(),
        },
    )


@contextmanager
def root_lock(
    root: Path,
    *,
    is_alive: Liveness = _default_liveness,
    process_identity: ProcessIdentity = _process_start_identity,
) -> Iterator[None]:
    root = initialize_root(root)
    held = root / "lock" / "held.json"
    while True:
        try:
            descriptor = os.open(
                held,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            lease = _read_lease(held)
            if _lease_is_live(
                lease,
                is_alive=is_alive,
                process_identity=process_identity,
            ):
                raise RuntimeError("runtime root lock is held by a live process")
            _archive_stale(root, held, "lock", lease)
            continue
        try:
            start_identity = process_identity(os.getpid())
            if start_identity is None:
                raise RuntimeError("current process start identity is unavailable")
            payload = {
                "pid": os.getpid(),
                "process_start_id": start_identity,
                "started_at": _utc_now(),
                "unit_id": "lock-holder",
                "token": uuid.uuid4().hex,
            }
            os.write(
                descriptor,
                (
                    json.dumps(payload, ensure_ascii=False) + "\n"
                ).encode("utf-8"),
            )
        finally:
            os.close(descriptor)
        break
    try:
        yield
    finally:
        try:
            released = (
                root
                / "journal"
                / f"lock.released.{payload['token']}.json"
            )
            _claim_lease(
                held,
                released,
                expected_token=payload["token"],
            )
        except FileNotFoundError:
            pass


def _require_control_access(
    root: Path,
    control_token: str | None,
    *,
    is_alive: Liveness,
    process_identity: ProcessIdentity,
) -> None:
    control = root / "state" / "CONTROL.json"
    if not control.exists():
        if control_token is not None:
            raise RuntimeError("runtime control lease is unavailable")
        return
    lease = _read_lease(control)
    if not _lease_is_live(
        lease,
        is_alive=is_alive,
        process_identity=process_identity,
    ):
        _archive_stale(root, control, "control", lease)
        if control_token is not None:
            raise RuntimeError("runtime control lease became stale")
        return
    if control_token != lease["token"]:
        raise RuntimeError("runtime root is reserved by a live control operation")


@contextmanager
def control_operation(
    root: Path,
    operation: str,
    *,
    is_alive: Liveness = _default_liveness,
    process_identity: ProcessIdentity = _process_start_identity,
) -> Iterator[str]:
    root = initialize_root(root)
    if not UNIT_ID.fullmatch(operation):
        raise RuntimeError("runtime control operation identity is unsafe")
    control = root / "state" / "CONTROL.json"
    with root_lock(
        root,
        is_alive=is_alive,
        process_identity=process_identity,
    ):
        _require_control_access(
            root,
            None,
            is_alive=is_alive,
            process_identity=process_identity,
        )
        start_identity = process_identity(os.getpid())
        if start_identity is None:
            raise RuntimeError("current process start identity is unavailable")
        lease = {
            "pid": os.getpid(),
            "process_start_id": start_identity,
            "started_at": _utc_now(),
            "unit_id": operation,
            "token": uuid.uuid4().hex,
        }
        descriptor = os.open(
            control,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        try:
            os.write(
                descriptor,
                (json.dumps(lease, ensure_ascii=False) + "\n").encode("utf-8"),
            )
        finally:
            os.close(descriptor)
    try:
        yield str(lease["token"])
    finally:
        try:
            with root_lock(
                root,
                is_alive=is_alive,
                process_identity=process_identity,
            ):
                released = (
                    root
                    / "journal"
                    / f"control.released.{lease['token']}.json"
                )
                _claim_lease(
                    control,
                    released,
                    expected_token=str(lease["token"]),
                )
        except FileNotFoundError:
            pass


def _safe_unit(root: Path, unit_id: str) -> Path:
    if not UNIT_ID.fullmatch(unit_id) or unit_id in {".", ".."}:
        raise RuntimeError("CURRENT release unit identity is unsafe")
    deployments = root / "deployments"
    _require_plain_directory(
        deployments,
        parent=root,
        label="runtime deployments directory",
    )
    candidate = deployments / unit_id
    if _is_link_or_reparse(candidate):
        raise RuntimeError(
            "CURRENT release unit cannot be a symlink or reparse alias"
        )
    absolute = Path(os.path.abspath(candidate))
    resolved = candidate.resolve()
    if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
        raise RuntimeError("CURRENT release unit cannot be a sibling alias")
    if resolved.parent != deployments or not resolved.is_dir():
        raise RuntimeError("CURRENT release unit escapes deployments")
    return Path(_extended_path_within(resolved, deployments))


def _read_current_once(root: Path) -> str:
    pointer = root / "CURRENT"
    if _is_link_or_reparse(pointer) or not pointer.is_file():
        raise RuntimeError("CURRENT pointer is missing or unsafe")
    lines = pointer.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0]:
        raise RuntimeError("CURRENT pointer must contain exactly one unit ID")
    return lines[0]


def _offline_health_contract(release: Mapping[str, Any]) -> dict[str, Any]:
    runtime = release.get("runtime")
    contract = (
        runtime.get("offline_health")
        if isinstance(runtime, Mapping)
        else None
    )
    try:
        validated = atomic._validated_offline_health_contract(contract)
    except RuntimeError as exc:
        raise RuntimeError(
            "runtime unit has no executable offline health contract"
        ) from exc
    if validated is None:
        raise RuntimeError("runtime unit has no executable offline health contract")
    return validated


def _validate_trust_root(value: Mapping[str, Any]) -> dict[str, Any]:
    identities = value.get("identities")
    fields = ("commit", "tag", "tree_oid", "uv_lock_sha256")
    if (
        not {"version", "identities"}.issubset(value)
        or set(value)
        - {
            "version",
            "identities",
            "launcher_python_sha256",
            "launcher_python_runtime",
            "control_plane_identities",
            "release_unit_identities",
        }
        or value.get("version") != "trusted-hermes/v1"
        or not isinstance(identities, list)
        or not identities
        or any(
            not isinstance(item, Mapping)
            or set(item) != set(fields)
            or not atomic.COMMIT_PATTERN.fullmatch(
                str(item.get("commit") or "")
            )
            or not atomic.COMMIT_PATTERN.fullmatch(
                str(item.get("tree_oid") or "")
            )
            or not isinstance(item.get("tag"), str)
            or not item["tag"]
            or re.search(r"[\x00-\x20\x7f]", str(item["tag"]))
            or not SHA256.fullmatch(str(item.get("uv_lock_sha256") or ""))
            for item in identities
        )
    ):
        raise RuntimeError("trusted Hermes identity root is invalid")
    python_sha256 = value.get("launcher_python_sha256")
    if python_sha256 is not None and not SHA256.fullmatch(
        str(python_sha256)
    ):
        raise RuntimeError("trusted launcher Python identity is invalid")
    runtime_identity = value.get("launcher_python_runtime")
    runtime_fields = (
        "implementation",
        "version",
        "executable_sha256",
        "base_executable_sha256",
        "runtime_library_sha256",
    )
    if runtime_identity is not None and (
        not isinstance(runtime_identity, Mapping)
        or set(runtime_identity) != set(runtime_fields)
        or not isinstance(runtime_identity.get("implementation"), str)
        or not runtime_identity["implementation"]
        or not isinstance(runtime_identity.get("version"), str)
        or not runtime_identity["version"]
        or any(
            not SHA256.fullmatch(str(runtime_identity.get(field) or ""))
            for field in (
                "executable_sha256",
                "base_executable_sha256",
                "runtime_library_sha256",
            )
        )
    ):
        raise RuntimeError("trusted launcher Python runtime is invalid")
    normalized = [
        {field: str(item[field]) for field in fields}
        for item in identities
    ]
    identity_keys = {
        tuple(item[field] for field in fields)
        for item in normalized
    }
    if len(identity_keys) != len(normalized):
        raise RuntimeError("trusted Hermes identity root contains duplicates")
    control_fields = (
        "commit",
        "tag",
        "tree_oid",
        "probe_blob_oid",
        "probe_sha256",
        "executor_id",
    )
    raw_controls = value.get("control_plane_identities")
    controls: list[dict[str, str]] = []
    if raw_controls is not None:
        if (
            not isinstance(raw_controls, list)
            or not raw_controls
            or any(
                not isinstance(item, Mapping)
                or set(item) != set(control_fields)
                or not atomic.COMMIT_PATTERN.fullmatch(
                    str(item.get("commit") or "")
                )
                or not atomic.COMMIT_PATTERN.fullmatch(
                    str(item.get("tree_oid") or "")
                )
                or not atomic.COMMIT_PATTERN.fullmatch(
                    str(item.get("probe_blob_oid") or "")
                )
                or not SHA256.fullmatch(
                    str(item.get("probe_sha256") or "")
                )
                or not isinstance(item.get("tag"), str)
                or not item["tag"]
                or item.get("executor_id")
                != atomic.OFFLINE_HEALTH_EXECUTOR_ID
                for item in raw_controls
            )
        ):
            raise RuntimeError("trusted control-plane identity root is invalid")
        controls = [
            {field: str(item[field]) for field in control_fields}
            for item in raw_controls
        ]
        if len(
            {
                tuple(item[field] for field in control_fields)
                for item in controls
            }
        ) != len(controls):
            raise RuntimeError(
                "trusted control-plane identity root contains duplicates"
            )
    unit_fields = (
        "release_unit_id",
        "payload_sha256",
        "profile_artifact_id",
        "profile_payload_sha256",
        "profile_manifest_sha256",
        "profile_release_metadata_sha256",
        "hermes_commit",
        "control_probe_sha256",
    )
    raw_units = value.get("release_unit_identities")
    units: list[dict[str, str]] = []
    if raw_units is not None:
        if (
            not isinstance(raw_units, list)
            or not raw_units
            or any(
                not isinstance(item, Mapping)
                or set(item) != set(unit_fields)
                or not UNIT_ID.fullmatch(
                    str(item.get("release_unit_id") or "")
                )
                or any(
                    not SHA256.fullmatch(str(item.get(field) or ""))
                    for field in (
                        "payload_sha256",
                        "profile_payload_sha256",
                        "profile_manifest_sha256",
                        "profile_release_metadata_sha256",
                        "control_probe_sha256",
                    )
                )
                or not atomic.COMMIT_PATTERN.fullmatch(
                    str(item.get("hermes_commit") or "")
                )
                or not isinstance(item.get("profile_artifact_id"), str)
                or not item["profile_artifact_id"]
                for item in raw_units
            )
        ):
            raise RuntimeError("trusted release-unit identity root is invalid")
        units = [
            {field: str(item[field]) for field in unit_fields}
            for item in raw_units
        ]
        if len(
            {
                tuple(item[field] for field in unit_fields)
                for item in units
            }
        ) != len(units):
            raise RuntimeError(
                "trusted release-unit identity root contains duplicates"
            )
    return {
        "version": "trusted-hermes/v1",
        "identities": normalized,
        **(
            {"launcher_python_sha256": str(python_sha256)}
            if python_sha256 is not None
            else {}
        ),
        **(
            {
                "launcher_python_runtime": {
                    field: str(runtime_identity[field])
                    for field in runtime_fields
                }
            }
            if runtime_identity is not None
            else {}
        ),
        **(
            {"control_plane_identities": controls}
            if raw_controls is not None
            else {}
        ),
        **(
            {"release_unit_identities": units}
            if raw_units is not None
            else {}
        ),
    }


def _load_trust_root(root: Path) -> dict[str, Any]:
    path = root / "state" / "TRUSTED_HERMES.json"
    if _is_link_or_reparse(path):
        raise RuntimeError("trusted Hermes identity root is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("trusted Hermes identity root is unavailable") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("trusted Hermes identity root is invalid")
    return _validate_trust_root(value)


def provision_trust_root(
    root: Path,
    value: Mapping[str, Any],
    *,
    is_alive: Liveness = _default_liveness,
    process_identity: ProcessIdentity = _process_start_identity,
) -> Path:
    root = initialize_root(root)
    trusted = _validate_trust_root(value)
    path = root / "state" / "TRUSTED_HERMES.json"
    with root_lock(
        root,
        is_alive=is_alive,
        process_identity=process_identity,
    ):
        if _lexists(path):
            existing = _load_trust_root(root)
            if existing != trusted:
                raise RuntimeError(
                    "trusted Hermes identity root already exists with different content"
                )
            return path
        _atomic_json(path, trusted)
        if _load_trust_root(root) != trusted:
            raise RuntimeError("trusted Hermes identity root verification failed")
    return path


def _require_trusted_hermes(
    hermes: Mapping[str, Any],
    trust_root: Mapping[str, Any] | None,
) -> None:
    identities = (
        trust_root.get("identities")
        if isinstance(trust_root, Mapping)
        else None
    )
    if not isinstance(identities, list):
        raise RuntimeError("launch blocked: no trusted Hermes identity root")
    fields = ("commit", "tag", "tree_oid", "uv_lock_sha256")
    if not any(
        isinstance(item, Mapping)
        and all(item.get(field) == hermes.get(field) for field in fields)
        for item in identities
    ):
        raise RuntimeError("launch blocked: Hermes identity is not trusted")


def _require_trusted_control_plane(
    control: Mapping[str, Any],
    trust_root: Mapping[str, Any] | None,
) -> None:
    identities = (
        trust_root.get("control_plane_identities")
        if isinstance(trust_root, Mapping)
        else None
    )
    fields = (
        "commit",
        "tag",
        "tree_oid",
        "probe_blob_oid",
        "probe_sha256",
        "executor_id",
    )
    if not isinstance(identities, list) or not any(
        isinstance(item, Mapping)
        and all(item.get(field) == control.get(field) for field in fields)
        for item in identities
    ):
        raise RuntimeError("launch blocked: control-plane identity is not trusted")


def _require_trusted_release_unit(
    release: Mapping[str, Any],
    control: Mapping[str, Any],
    trust_root: Mapping[str, Any] | None,
) -> None:
    profile = release.get("profile")
    identities = (
        trust_root.get("release_unit_identities")
        if isinstance(trust_root, Mapping)
        else None
    )
    if not isinstance(profile, Mapping) or not isinstance(identities, list):
        raise RuntimeError("launch blocked: release-unit identity is not trusted")
    expected = {
        "release_unit_id": release.get("release_unit_id"),
        "payload_sha256": release.get("payload_sha256"),
        "profile_artifact_id": profile.get("artifact_id"),
        "profile_payload_sha256": profile.get("payload_sha256"),
        "profile_manifest_sha256": profile.get("manifest_sha256"),
        "profile_release_metadata_sha256": profile.get(
            "release_metadata_sha256"
        ),
        "hermes_commit": release.get("hermes", {}).get("commit"),
        "control_probe_sha256": control.get("probe_sha256"),
    }
    if not any(
        isinstance(item, Mapping)
        and all(item.get(field) == value for field, value in expected.items())
        for item in identities
    ):
        raise RuntimeError("launch blocked: release-unit identity is not trusted")


def _git_object_id(kind: str, payload: bytes, oid_length: int) -> bytes:
    framed = (
        kind.encode("ascii")
        + b" "
        + str(len(payload)).encode("ascii")
        + b"\0"
        + payload
    )
    if oid_length == 40:
        return hashlib.sha1(framed).digest()
    if oid_length == 64:
        return hashlib.sha256(framed).digest()
    raise RuntimeError("unsupported Git object identity length")


def _archive_tree_oid(archive_path: Path, oid_length: int) -> str:
    tree: dict[str, Any] = {}
    with zipfile.ZipFile(archive_path) as package:
        for info in package.infolist():
            if info.is_dir():
                continue
            raw = info.filename
            relative = atomic._safe_relative(raw)
            parts = relative.parts
            if not parts:
                raise RuntimeError("Hermes archive path is empty")
            node = tree
            for part in parts[:-1]:
                existing = node.setdefault(part, {})
                if not isinstance(existing, dict):
                    raise RuntimeError("Hermes archive file/tree collision")
                node = existing
            name = parts[-1]
            if name in node:
                raise RuntimeError("Hermes archive contains duplicate paths")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                mode = "120000"
            elif unix_mode & 0o111:
                mode = "100755"
            else:
                mode = "100644"
            node[name] = (mode, package.read(info))

    def hash_tree(node: Mapping[str, Any]) -> bytes:
        body = bytearray()
        ordered = sorted(
            node.items(),
            key=lambda item: (
                item[0] + "/" if isinstance(item[1], dict) else item[0]
            ).encode("utf-8"),
        )
        for name, value in ordered:
            if isinstance(value, dict):
                mode = "40000"
                oid = hash_tree(value)
            else:
                mode, payload = value
                oid = _git_object_id("blob", payload, oid_length)
            body.extend(mode.encode("ascii"))
            body.extend(b" ")
            body.extend(name.encode("utf-8"))
            body.extend(b"\0")
            body.extend(oid)
        return _git_object_id("tree", bytes(body), oid_length)

    return hash_tree(tree).hex()


def verify_launchable_unit(
    unit: Path,
    trust_root: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    release = artifact.verify_unit(unit)
    profile = release.get("profile")
    hermes = release.get("hermes")
    if not isinstance(profile, Mapping) or not isinstance(hermes, Mapping):
        raise RuntimeError("runtime unit identity is incomplete")
    profile_root = unit / "profile" / ".release"
    expected_profile_hashes = {
        "manifest_sha256": atomic._hash_file(
            profile_root / "MANIFEST.sha256"
        ),
        "release_metadata_sha256": atomic._hash_file(
            profile_root / "RELEASE.json"
        ),
    }
    for field, actual in expected_profile_hashes.items():
        if profile.get(field) != actual:
            raise RuntimeError(f"runtime Profile identity mismatch: {field}")
    runtime_profile_name = profile.get("runtime_profile_name")
    if (
        not isinstance(runtime_profile_name, str)
        or not atomic.PROFILE_NAME_PATTERN.fullmatch(runtime_profile_name)
        or runtime_profile_name in {".", ".."}
    ):
        raise RuntimeError("runtime Profile deployment name is invalid")
    if (
        not atomic.COMMIT_PATTERN.fullmatch(str(hermes.get("commit") or ""))
        or not atomic.COMMIT_PATTERN.fullmatch(
            str(hermes.get("tree_oid") or "")
        )
        or not isinstance(hermes.get("tag"), str)
        or not hermes["tag"]
        or re.search(r"[\x00-\x20\x7f]", hermes["tag"])
        or hermes.get("source") != "exact_git_tree_archive"
        or not SHA256.fullmatch(str(hermes.get("uv_lock_sha256") or ""))
    ):
        raise RuntimeError("runtime Hermes identity is incomplete")
    with zipfile.ZipFile(unit / "hermes" / "hermes-source.zip") as package:
        try:
            uv_lock = package.read("uv.lock")
        except KeyError as exc:
            raise RuntimeError("Hermes archive has no uv.lock") from exc
    if hashlib.sha256(uv_lock).hexdigest() != hermes["uv_lock_sha256"]:
        raise RuntimeError("Hermes uv.lock identity mismatch")
    archive_tree = _archive_tree_oid(
        unit / "hermes" / "hermes-source.zip",
        len(str(hermes["tree_oid"])),
    )
    if archive_tree != hermes["tree_oid"]:
        raise RuntimeError("Hermes archive does not match declared Git tree")
    _require_trusted_hermes(hermes, trust_root)
    contract = _offline_health_contract(release)
    if contract.get("executor_id") == atomic.OFFLINE_HEALTH_EXECUTOR_ID:
        control = release.get("control_plane")
        profile_release, _records = atomic._read_profile_identity(
            unit / "profile",
            require_directory_identity=False,
        )
        if (
            not isinstance(control, Mapping)
            or set(control)
            != {
                "commit",
                "tag",
                "tree_oid",
                "probe_blob_oid",
                "executor_id",
                "probe_sha256",
                "profile_source_commit",
                "profile_source_tag",
            }
            or control.get("executor_id")
            != atomic.OFFLINE_HEALTH_EXECUTOR_ID
            or control.get("probe_sha256")
            != atomic._hash_file(
                unit / "health" / "datasage_offline_health.py"
            )
            or control.get("profile_source_commit")
            != profile_release.get("source_commit")
            or control.get("profile_source_tag")
            != profile_release.get("source_tag")
        ):
            raise RuntimeError("runtime control-plane identity is incomplete")
        _require_trusted_control_plane(control, trust_root)
        _require_trusted_release_unit(release, control, trust_root)
    return release, contract


def stage_launchable_unit(
    root: Path,
    source: Path,
    *,
    control_token: str | None = None,
    is_alive: Liveness = _default_liveness,
    process_identity: ProcessIdentity = _process_start_identity,
) -> Path:
    root = initialize_root(root)
    source = _require_plain_directory(
        Path(os.path.abspath(source)),
        label="runtime source unit",
    )
    source_io = Path(_extended_path_within(source, source))
    if any(_is_link_or_reparse(path) for path in source_io.rglob("*")):
        raise RuntimeError("runtime source unit contains unsafe paths")
    trust_root = _load_trust_root(root)
    release, _contract = verify_launchable_unit(source_io, trust_root)
    unit_id = str(release["release_unit_id"])
    with root_lock(
        root,
        is_alive=is_alive,
        process_identity=process_identity,
    ):
        _require_control_access(
            root,
            control_token,
            is_alive=is_alive,
            process_identity=process_identity,
        )
        deployments = root / "deployments"
        _require_plain_directory(
            deployments,
            parent=root,
            label="runtime deployments directory",
        )
        target = deployments / unit_id
        if _lexists(target):
            deployed = _safe_unit(root, unit_id)
            deployed_release, _deployed_contract = verify_launchable_unit(
                deployed,
                trust_root,
            )
            if (
                deployed_release.get("payload_sha256")
                != release.get("payload_sha256")
            ):
                raise RuntimeError(
                    "existing runtime deployment payload does not match source"
                )
            return deployed
        staging = root / ".staging"
        if _lexists(staging):
            _require_plain_directory(
                staging,
                parent=root,
                label="runtime staging directory",
            )
            if any(staging.iterdir()):
                raise RuntimeError("stale runtime staging content exists")
        else:
            staging.mkdir()
            _require_plain_directory(
                staging,
                parent=root,
                label="runtime staging directory",
            )
        staging_operation = staging / uuid.uuid4().hex
        staging_operation.mkdir()
        _require_plain_directory(
            staging_operation,
            parent=staging,
            label="runtime staging operation",
        )
        temporary = staging_operation / unit_id
        try:
            _copytree_within_roots(
                source_io,
                temporary,
                source_root=source,
                target_root=root,
            )
            temporary_io = Path(_extended_path_within(temporary, root))
            _require_plain_directory(
                temporary_io,
                parent=staging_operation,
                label="staged runtime unit",
            )
            if any(
                _is_link_or_reparse(path)
                for path in temporary_io.rglob("*")
            ):
                raise RuntimeError("staged runtime unit contains unsafe paths")
            verify_launchable_unit(temporary_io, trust_root)
            if _lexists(target):
                raise RuntimeError("runtime deployment target raced")
            os.replace(
                _extended_path_within(temporary, root),
                _extended_path_within(target, root),
            )
            staging_operation.rmdir()
            deployed = _safe_unit(root, unit_id)
            verify_launchable_unit(deployed, trust_root)
            _make_checkout_read_only(deployed)
            verify_launchable_unit(deployed, trust_root)
        except Exception:
            _remove_tree_within_root(staging_operation, staging)
            raise
        try:
            staging.rmdir()
        except OSError:
            pass
        return deployed


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    return environment


def _checkout_git(checkout: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={checkout.as_posix()}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=",
                "-C",
                str(checkout),
                *args,
            ],
            check=True,
            capture_output=True,
            text=text,
            env=_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("materialized Hermes checkout identity is unreadable") from exc
    return completed.stdout


def _remove_tree(path: Path) -> None:
    def make_writable(function, raw_path, _error):
        candidate = Path(raw_path)
        candidate.chmod(stat.S_IRWXU)
        function(raw_path)

    if _is_link_or_reparse(path):
        try:
            path.unlink()
        except OSError:
            path.rmdir()
    elif path.exists():
        shutil.rmtree(path, onerror=make_writable)


def _verify_checkout_worktree(
    checkout: Path,
    archive: Path,
) -> None:
    expected: dict[str, tuple[int, bytes]] = {}
    try:
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                if info.is_dir():
                    continue
                relative = atomic._safe_relative(info.filename).as_posix()
                if relative == ".git" or relative.startswith(".git/"):
                    raise RuntimeError("Hermes archive contains Git control paths")
                expected[relative] = (
                    (info.external_attr >> 16) & 0xFFFF,
                    package.read(info),
                )
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("Hermes source archive is unreadable") from exc

    actual: set[str] = set()
    for candidate in checkout.rglob("*"):
        relative = candidate.relative_to(checkout).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if candidate.is_dir() and not _is_link_or_reparse(candidate):
            continue
        actual.add(relative)
    if actual != set(expected):
        raise RuntimeError("materialized Hermes checkout has extra or missing paths")

    for relative, (unix_mode, payload) in expected.items():
        candidate = checkout / Path(relative)
        archived_symlink = stat.S_ISLNK(unix_mode)
        if archived_symlink and candidate.is_symlink():
            actual_payload = os.readlink(candidate).encode("utf-8")
        else:
            if (
                _is_link_or_reparse(candidate)
                or not candidate.is_file()
            ):
                raise RuntimeError(
                    "materialized Hermes checkout contains unsafe paths"
                )
            actual_payload = candidate.read_bytes()
        if actual_payload != payload:
            raise RuntimeError("materialized Hermes checkout content mismatch")


def _verify_git_control_plane(checkout: Path) -> None:
    git_dir = checkout / ".git"
    _require_plain_directory(
        git_dir,
        parent=checkout,
        label="materialized Hermes Git directory",
    )
    if any(_is_link_or_reparse(path) for path in git_dir.rglob("*")):
        raise RuntimeError(
            "materialized Hermes Git control plane contains unsafe paths"
        )
    reported_git_dir = Path(
        str(_checkout_git(checkout, "rev-parse", "--absolute-git-dir")).strip()
    )
    reported_root = Path(
        str(_checkout_git(checkout, "rev-parse", "--show-toplevel")).strip()
    )
    if (
        os.path.normcase(str(reported_git_dir.resolve()))
        != os.path.normcase(str(git_dir.resolve()))
        or os.path.normcase(str(reported_root.resolve()))
        != os.path.normcase(str(checkout.resolve()))
    ):
        raise RuntimeError("materialized Hermes Git control path mismatch")
    forbidden = (
        git_dir / "objects" / "info" / "alternates",
        git_dir / "objects" / "info" / "http-alternates",
        git_dir / "info" / "grafts",
        git_dir / "shallow",
    )
    if any(_lexists(path) for path in forbidden):
        raise RuntimeError("materialized Hermes Git control plane is unsafe")
    replacements = str(
        _checkout_git(checkout, "for-each-ref", "--format=%(refname)", "refs/replace")
    ).strip()
    if replacements:
        raise RuntimeError("materialized Hermes Git replacement refs are forbidden")
    status = str(
        _checkout_git(
            checkout,
            "-c",
            "core.excludesFile=",
            "-c",
            "status.showUntrackedFiles=all",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            ".",
        )
    ).strip()
    if status:
        raise RuntimeError("materialized Hermes checkout is not exact and clean")


def _verify_materialized_checkout(
    checkout: Path,
    hermes: Mapping[str, Any],
    archive: Path,
) -> None:
    _require_plain_directory(
        checkout,
        label="materialized Hermes checkout",
    )
    if _lexists(checkout / ".git"):
        raise RuntimeError("materialized Hermes checkout contains Git control paths")
    if any(_is_link_or_reparse(path) for path in checkout.rglob("*")):
        raise RuntimeError("materialized Hermes checkout contains unsafe paths")
    tree_oid = _archive_tree_oid(archive, len(str(hermes["tree_oid"])))
    uv_lock_path = checkout / "uv.lock"
    if (
        tree_oid != hermes["tree_oid"]
        or not uv_lock_path.is_file()
        or hashlib.sha256(uv_lock_path.read_bytes()).hexdigest()
        != hermes["uv_lock_sha256"]
    ):
        raise RuntimeError("materialized Hermes checkout identity mismatch")
    _verify_checkout_worktree(checkout, archive)


def _make_checkout_read_only(checkout: Path) -> None:
    for path in sorted(
        checkout.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.chmod(0o555 if path.is_dir() else 0o444)
        except OSError as exc:
            raise RuntimeError("unable to protect materialized Hermes checkout") from exc
    try:
        checkout.chmod(0o555)
    except OSError as exc:
        raise RuntimeError("unable to protect materialized Hermes checkout") from exc


def materialize_hermes_checkout(
    root: Path,
    unit: Path,
    release: Mapping[str, Any],
) -> Path:
    hermes = release.get("hermes")
    if not isinstance(hermes, Mapping):
        raise RuntimeError("runtime Hermes identity is incomplete")
    parent = _materialization_parent(root, "hermes-checkouts")
    slot = hashlib.sha256(
        str(release["release_unit_id"]).encode("utf-8")
    ).hexdigest()[:20]
    target = parent / slot
    archive = unit / "hermes" / "hermes-source.zip"
    if _lexists(target):
        _require_plain_directory(
            target,
            parent=parent,
            label="materialized Hermes checkout slot",
        )
        target_io = Path(_extended_path_within(target, parent))
        _verify_materialized_checkout(target_io, hermes, archive)
        return target_io
    temporary = parent / f".tmp.{uuid.uuid4().hex[:12]}"
    if _lexists(temporary):
        raise RuntimeError("temporary Hermes checkout path is unsafe")
    try:
        temporary.mkdir()
        _require_plain_directory(
            temporary,
            parent=parent,
            label="temporary Hermes checkout",
        )
        seen: set[str] = set()
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                if info.is_dir():
                    continue
                relative = atomic._safe_relative(info.filename)
                relative_text = relative.as_posix()
                if (
                    relative_text in seen
                    or relative_text == ".git"
                    or relative_text.startswith(".git/")
                ):
                    raise RuntimeError("Hermes archive contains unsafe paths")
                seen.add(relative_text)
                destination = temporary / relative
                destination_io = Path(
                    _extended_path_within(destination, temporary)
                )
                destination_parent_io = Path(
                    _extended_path_within(destination.parent, temporary)
                )
                destination_parent_io.mkdir(parents=True, exist_ok=True)
                if _lexists(destination_io) or _is_link_or_reparse(
                    destination_parent_io
                ):
                    raise RuntimeError("Hermes archive extraction path is unsafe")
                destination_io.write_bytes(package.read(info))
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                destination_io.chmod(0o755 if unix_mode & 0o111 else 0o644)
        temporary_io = Path(_extended_path_within(temporary, parent))
        _verify_materialized_checkout(temporary_io, hermes, archive)
        _make_checkout_read_only(temporary_io)
        if _lexists(target):
            raise RuntimeError("materialized Hermes checkout slot raced")
        os.replace(
            _extended_path_within(temporary, parent),
            _extended_path_within(target, parent),
        )
        _require_plain_directory(
            target,
            parent=parent,
            label="materialized Hermes checkout slot",
        )
        _verify_materialized_checkout(
            Path(_extended_path_within(target, parent)), hermes, archive
        )
    except (OSError, zipfile.BadZipFile) as exc:
        _remove_tree_within_root(temporary, parent)
        raise RuntimeError("unable to materialize exact Hermes checkout") from exc
    except Exception:
        _remove_tree_within_root(temporary, parent)
        raise
    return Path(_extended_path_within(target, parent))


def _verify_materialized_profile(
    profile: Path,
    expected: Mapping[str, Any],
) -> None:
    _require_plain_directory(
        profile,
        label="materialized Profile",
    )
    if any(_is_link_or_reparse(path) for path in profile.rglob("*")):
        raise RuntimeError("materialized Profile contains unsafe paths")
    release, _records = atomic._read_profile_identity(
        profile,
        require_directory_identity=False,
    )
    for field in atomic.PROFILE_IDENTITY_FIELDS:
        if release.get(field) != expected.get(field):
            raise RuntimeError(f"materialized Profile identity mismatch: {field}")
    expected_hashes = {
        "manifest_sha256": atomic._hash_file(
            profile / ".release" / "MANIFEST.sha256"
        ),
        "release_metadata_sha256": atomic._hash_file(
            profile / ".release" / "RELEASE.json"
        ),
    }
    for field, actual in expected_hashes.items():
        if expected.get(field) != actual:
            raise RuntimeError(f"materialized Profile identity mismatch: {field}")
    if profile.name != expected.get("runtime_profile_name"):
        raise RuntimeError("materialized Profile deployment name mismatch")


def materialize_runtime_profile(
    root: Path,
    unit: Path,
    release: Mapping[str, Any],
) -> Path:
    expected = release.get("profile")
    if not isinstance(expected, Mapping):
        raise RuntimeError("runtime Profile identity is incomplete")
    parent = _materialization_parent(root, "runtime-profiles")
    slot = hashlib.sha256(
        str(release["release_unit_id"]).encode("utf-8")
    ).hexdigest()[:20]
    target_root = parent / slot
    target = target_root / str(expected["runtime_profile_name"])
    if _lexists(target_root):
        if (
            _is_link_or_reparse(target_root)
            or not target_root.is_dir()
            or {item.name for item in target_root.iterdir()} != {target.name}
        ):
            raise RuntimeError("materialized Profile slot is unsafe")
        _require_plain_directory(
            target_root,
            parent=parent,
            label="materialized Profile slot",
        )
        _require_plain_directory(
            target,
            parent=target_root,
            label="materialized Profile deployment",
        )
        target_io = Path(_extended_path_within(target, parent))
        _verify_materialized_profile(target_io, expected)
        return target_io
    temporary = parent / f".tmp.{uuid.uuid4().hex[:12]}"
    if _lexists(temporary):
        raise RuntimeError("temporary Profile path is unsafe")
    temporary_profile = temporary / target.name
    try:
        temporary.mkdir()
        _require_plain_directory(
            temporary,
            parent=parent,
            label="temporary Profile slot",
        )
        _copytree_within_roots(
            unit / "profile",
            temporary_profile,
            source_root=unit,
            target_root=parent,
        )
        _require_plain_directory(
            temporary_profile,
            parent=temporary,
            label="temporary Profile deployment",
        )
        temporary_profile_io = Path(
            _extended_path_within(temporary_profile, parent)
        )
        _verify_materialized_profile(temporary_profile_io, expected)
        _make_checkout_read_only(
            Path(_extended_path_within(temporary, parent))
        )
        if _lexists(target_root):
            raise RuntimeError("materialized Profile slot raced")
        os.replace(
            _extended_path_within(temporary, parent),
            _extended_path_within(target_root, parent),
        )
        _require_plain_directory(
            target_root,
            parent=parent,
            label="materialized Profile slot",
        )
        _require_plain_directory(
            target,
            parent=target_root,
            label="materialized Profile deployment",
        )
        _verify_materialized_profile(
            Path(_extended_path_within(target, parent)), expected
        )
    except Exception:
        _remove_tree_within_root(temporary, parent)
        raise
    return Path(_extended_path_within(target, parent))


def _running_lease(
    root: Path,
    *,
    is_alive: Liveness,
    process_identity: ProcessIdentity,
) -> dict[str, Any] | None:
    running = root / "state" / "RUNNING.json"
    if not running.exists():
        return None
    lease = _read_lease(running)
    if _lease_is_live(
        lease,
        is_alive=is_alive,
        process_identity=process_identity,
    ):
        return lease
    _archive_stale(root, running, "running", lease)
    return None


def activate(
    root: Path,
    unit_id: str,
    *,
    control_token: str | None = None,
    is_alive: Liveness = _default_liveness,
    process_identity: ProcessIdentity = _process_start_identity,
) -> Path:
    root = initialize_root(root)
    operation = uuid.uuid4().hex
    pending = root / "journal" / f"activate.{operation}.pending.json"
    with root_lock(
        root,
        is_alive=is_alive,
        process_identity=process_identity,
    ):
        _require_control_access(
            root,
            control_token,
            is_alive=is_alive,
            process_identity=process_identity,
        )
        if _running_lease(
            root,
            is_alive=is_alive,
            process_identity=process_identity,
        ) is not None:
            raise RuntimeError("cannot switch CURRENT while runtime is live")
        unit = _safe_unit(root, unit_id)
        trust_root = _load_trust_root(root)
        verify_launchable_unit(unit, trust_root)
        record = {
            "operation": "activate",
            "operation_id": operation,
            "release_unit_id": unit_id,
            "status": "pending",
            "started_at": _utc_now(),
        }
        _atomic_json(pending, record)
        try:
            temporary = root / f".CURRENT.{operation}.tmp"
            temporary.write_text(
                unit_id + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, root / "CURRENT")
            record.update(status="committed", committed_at=_utc_now())
            suffix = "committed"
        except Exception as exc:
            record.update(
                status="failed",
                failed_at=_utc_now(),
                error_type=type(exc).__name__,
            )
            suffix = "failed"
            failure = exc
        _atomic_json(pending, record)
        receipt = root / "journal" / f"activate.{operation}.{suffix}.json"
        os.replace(pending, receipt)
        if suffix == "failed":
            raise RuntimeError(
                f"CURRENT activation failed; receipt={receipt.name}"
            ) from failure
        return receipt


def _unit_command_executor(
    unit: Path,
    contract: Mapping[str, Any],
    env: Mapping[str, str],
) -> Mapping[str, Any]:
    if contract.get("mode") != "unit_command":
        raise RuntimeError("fixture health contract requires an injected executor")
    argv = contract.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise RuntimeError("offline health argv is invalid")
    launcher_python = argv[0] == atomic.LAUNCHER_PYTHON_SENTINEL
    if launcher_python:
        if (
            argv
            != [
                atomic.LAUNCHER_PYTHON_SENTINEL,
                "health/datasage_offline_health.py",
            ]
            or contract.get("executor_id")
            != atomic.OFFLINE_HEALTH_EXECUTOR_ID
        ):
            raise RuntimeError("launcher Python health command is invalid")
        executable = (unit / atomic._safe_relative(argv[1])).resolve()
        command = [sys.executable, "-I", "-B", str(executable)]
    else:
        executable = (unit / atomic._safe_relative(argv[0])).resolve()
        command = [str(executable), *argv[1:]]
    if (
        executable.parent != unit.resolve()
        and unit.resolve() not in executable.parents
    ):
        raise RuntimeError("offline health executable escapes runtime unit")
    if _is_link_or_reparse(executable) or not executable.is_file():
        raise RuntimeError("offline health executable is missing or unsafe")
    cwd_raw = str(contract.get("cwd") or ".")
    cwd = (unit / atomic._safe_relative(cwd_raw)).resolve()
    if cwd != unit.resolve() and unit.resolve() not in cwd.parents:
        raise RuntimeError("offline health cwd escapes runtime unit")
    timeout = contract.get("timeout_seconds")
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 60
    ):
        raise RuntimeError("offline health timeout is invalid")
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=dict(env),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        ),
    )
    if completed.returncode != 0:
        raise RuntimeError("offline health command failed")
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("offline health output is invalid") from exc
    if not isinstance(result, Mapping):
        raise RuntimeError("offline health output is invalid")
    return result


def launch_current_health(
    root: Path,
    *,
    executor: HealthExecutor | None = None,
    control_token: str | None = None,
    is_alive: Liveness = _default_liveness,
    process_identity: ProcessIdentity = _process_start_identity,
) -> Path:
    root = initialize_root(root)
    operation = uuid.uuid4().hex
    pending = root / "journal" / f"health.{operation}.pending.json"
    running = root / "state" / "RUNNING.json"
    with root_lock(
        root,
        is_alive=is_alive,
        process_identity=process_identity,
    ):
        _require_control_access(
            root,
            control_token,
            is_alive=is_alive,
            process_identity=process_identity,
        )
        if _running_lease(
            root,
            is_alive=is_alive,
            process_identity=process_identity,
        ) is not None:
            raise RuntimeError("runtime health is already executing")
        unit_id = _read_current_once(root)
        unit = _safe_unit(root, unit_id)
        trust_root = _load_trust_root(root)
        release, contract = verify_launchable_unit(unit, trust_root)
        if (
            contract.get("executor_id")
            == atomic.OFFLINE_HEALTH_EXECUTOR_ID
            and (
                trust_root.get("launcher_python_sha256")
                != atomic._hash_file(Path(sys.executable))
                or trust_root.get("launcher_python_runtime")
                != launcher_python_runtime_identity()
            )
        ):
            raise RuntimeError("launcher Python identity is not trusted")
        start_identity = process_identity(os.getpid())
        if start_identity is None:
            raise RuntimeError("current process start identity is unavailable")
        lease = {
            "pid": os.getpid(),
            "process_start_id": start_identity,
            "started_at": _utc_now(),
            "unit_id": unit_id,
            "token": uuid.uuid4().hex,
        }
        _atomic_json(running, lease)
        record: dict[str, Any] = {
            "operation": "offline_health",
            "operation_id": operation,
            "release_unit_id": unit_id,
            "profile_artifact_id": release["profile"]["artifact_id"],
            "hermes_commit": release["hermes"]["commit"],
            "status": "pending",
            "runtime_executed": False,
            "health_checked": False,
            "started_at": _utc_now(),
        }
        _atomic_json(pending, record)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in OFFLINE_ENVIRONMENT_ALLOWLIST
        }
        selected_executor = executor or _unit_command_executor
        failure: Exception | None = None
        try:
            hermes_checkout = materialize_hermes_checkout(root, unit, release)
            runtime_profile = materialize_runtime_profile(root, unit, release)
            environment["HERMES_HOME"] = str(runtime_profile)
            environment["HERMES_AGENT_ROOT"] = str(hermes_checkout)
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONSTARTUP", None)
            environment.pop("PYTHONINSPECT", None)
            environment["PYTHONPATH"] = str(hermes_checkout)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONNOUSERSITE"] = "1"
            environment["PYTHONSAFEPATH"] = "1"
            environment["GIT_OPTIONAL_LOCKS"] = "0"
            environment["DATASAGE_RUNTIME_STATE"] = str(root / "state")
            environment["DATASAGE_OFFLINE_HEALTH"] = "true"
            environment["DATASAGE_RELEASE_UNIT_ID"] = unit_id
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONHASHSEED"] = "0"
            environment["NO_PROXY"] = "*"
            environment["no_proxy"] = "*"
            record["launcher_python_sha256"] = atomic._hash_file(
                Path(sys.executable)
            )
            record["runtime_executed"] = True
            outcome = selected_executor(unit, contract, environment)
            record["health_checked"] = True
            if (
                not isinstance(outcome, Mapping)
                or outcome.get("status") != "healthy"
                or outcome.get("release_unit_id") != unit_id
            ):
                raise RuntimeError(
                    "offline health result did not bind selected unit"
                )
            if contract.get("executor_id") == atomic.OFFLINE_HEALTH_EXECUTOR_ID:
                checks = outcome.get("checks")
                if (
                    outcome.get("executor_id")
                    != atomic.OFFLINE_HEALTH_EXECUTOR_ID
                    or not isinstance(checks, Mapping)
                    or checks
                    != {
                        "hermes_import": True,
                        "plugin_loaded": True,
                        "tool_surface": True,
                        "six_domain_contract": True,
                        "network_used": False,
                        "database_used": False,
                        "wecom_loaded": False,
                    }
                ):
                    raise RuntimeError(
                        "offline health checks are incomplete"
                    )
            _verify_materialized_checkout(
                hermes_checkout,
                release["hermes"],
                unit / "hermes" / "hermes-source.zip",
            )
            _verify_materialized_profile(
                runtime_profile,
                release["profile"],
            )
            post_release, _post_contract = verify_launchable_unit(
                unit,
                trust_root,
            )
            if (
                post_release.get("release_unit_id") != unit_id
                or post_release.get("payload_sha256")
                != release.get("payload_sha256")
            ):
                raise RuntimeError("runtime unit identity changed during health")
            record.update(
                status="committed",
                committed_at=_utc_now(),
                health=dict(outcome),
            )
            suffix = "committed"
        except Exception as exc:
            record.update(
                status="failed",
                failed_at=_utc_now(),
                error_type=type(exc).__name__,
            )
            suffix = "failed"
            failure = exc
        finally:
            lease_archive = (
                root / "journal" / f"health.{operation}.lease-ended.json"
            )
            _claim_lease(
                running,
                lease_archive,
                expected_token=lease["token"],
            )
            _atomic_json(pending, record)
            receipt = (
                root
                / "journal"
                / f"health.{operation}.{suffix}.json"
            )
            os.replace(pending, receipt)
    if suffix == "failed":
        raise RuntimeError(f"offline health failed; receipt={receipt.name}") from failure
    return receipt


def rollback_to(
    root: Path,
    unit_id: str,
    *,
    control_token: str | None = None,
    is_alive: Liveness = _default_liveness,
    process_identity: ProcessIdentity = _process_start_identity,
) -> Path:
    root = initialize_root(root)
    if control_token is None:
        with control_operation(
            root,
            "runtime-rollback",
            is_alive=is_alive,
            process_identity=process_identity,
        ) as owned_token:
            return rollback_to(
                root,
                unit_id,
                control_token=owned_token,
                is_alive=is_alive,
                process_identity=process_identity,
            )
    operation = uuid.uuid4().hex
    pending = root / "journal" / f"rollback.{operation}.pending.json"
    record: dict[str, Any] = {
        "version": "runtime-rollback-receipt/v1",
        "operation": "rollback",
        "operation_id": operation,
        "release_unit_id": unit_id,
        "status": "pending",
        "started_at": _utc_now(),
    }
    _atomic_json(pending, record)
    try:
        activation = activate(
            root,
            unit_id,
            control_token=control_token,
            is_alive=is_alive,
            process_identity=process_identity,
        )
        health = launch_current_health(
            root,
            control_token=control_token,
            is_alive=is_alive,
            process_identity=process_identity,
        )
        health_record = json.loads(health.read_text(encoding="utf-8"))
        if (
            health_record.get("status") != "committed"
            or health_record.get("release_unit_id") != unit_id
            or health_record.get("runtime_executed") is not True
            or health_record.get("health_checked") is not True
            or _read_current_once(root) != unit_id
        ):
            raise RuntimeError("rollback health identity is inconsistent")
        record.update(
            status="committed",
            committed_at=_utc_now(),
            activation_receipt=activation.name,
            activation_receipt_sha256=atomic._hash_file(activation),
            health_receipt=health.name,
            health_receipt_sha256=atomic._hash_file(health),
            profile_artifact_id=health_record["profile_artifact_id"],
            hermes_commit=health_record["hermes_commit"],
            runtime_executed=True,
            health_checked=True,
        )
        suffix = "committed"
    except BaseException as exc:
        record.update(
            status="failed",
            failed_at=_utc_now(),
            error_type=type(exc).__name__,
            current_release_unit_id=(
                _read_current_once(root)
                if (root / "CURRENT").is_file()
                else None
            ),
        )
        suffix = "failed"
        failure = exc
    _atomic_json(pending, record)
    receipt = root / "journal" / f"rollback.{operation}.{suffix}.json"
    os.replace(pending, receipt)
    if suffix == "failed":
        raise RuntimeError(
            f"runtime rollback failed; receipt={receipt.name}"
        ) from failure
    return receipt


def _status(root: Path) -> dict[str, Any]:
    root = initialize_root(root)
    current = (
        _read_current_once(root)
        if (root / "CURRENT").is_file()
        else None
    )
    deployments = sorted(
        path.name
        for path in (root / "deployments").iterdir()
        if path.is_dir() and not _is_link_or_reparse(path)
    )
    return {
        "version": "runtime-control-status/v1",
        "current_release_unit_id": current,
        "deployments": deployments,
        "running_lease": (root / "state" / "RUNNING.json").exists(),
        "control_lease": (root / "state" / "CONTROL.json").exists(),
        "root_lock": (root / "lock" / "held.json").exists(),
        "pending_receipts": len(
            list((root / "journal").glob("*.pending.json"))
        ),
        "failed_receipts": len(
            list((root / "journal").glob("*.failed.json"))
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control immutable CURRENT-aware DataSage runtime units."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status"):
        command = subcommands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
    trust = subcommands.add_parser("provision-trust")
    trust.add_argument("--root", required=True, type=Path)
    trust.add_argument("--identity-file", required=True, type=Path)
    stage = subcommands.add_parser("stage")
    stage.add_argument("--root", required=True, type=Path)
    stage.add_argument("--unit", required=True, type=Path)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--unit-id", required=True)
    activate_command = subcommands.add_parser("activate")
    activate_command.add_argument("--root", required=True, type=Path)
    activate_command.add_argument("--unit-id", required=True)
    health = subcommands.add_parser("health")
    health.add_argument("--root", required=True, type=Path)
    rollback = subcommands.add_parser("rollback")
    rollback.add_argument("--root", required=True, type=Path)
    rollback.add_argument("--unit-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            print(initialize_root(args.root))
        elif args.command == "status":
            print(json.dumps(_status(args.root), ensure_ascii=False))
        elif args.command == "provision-trust":
            value = json.loads(
                args.identity_file.read_text(encoding="utf-8")
            )
            print(provision_trust_root(args.root, value))
        elif args.command == "stage":
            print(stage_launchable_unit(args.root, args.unit))
        elif args.command == "verify":
            root = initialize_root(args.root)
            unit = _safe_unit(root, args.unit_id)
            release, contract = verify_launchable_unit(
                unit,
                _load_trust_root(root),
            )
            print(
                json.dumps(
                    {
                        "status": "verified",
                        "release_unit_id": release["release_unit_id"],
                        "profile_artifact_id": release["profile"]["artifact_id"],
                        "hermes_commit": release["hermes"]["commit"],
                        "health_executor_id": contract["executor_id"],
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "activate":
            print(activate(args.root, args.unit_id))
        elif args.command == "health":
            print(launch_current_health(args.root))
        elif args.command == "rollback":
            print(rollback_to(args.root, args.unit_id))
        else:
            raise RuntimeError("runtime control command is unavailable")
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            f"runtime control failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
