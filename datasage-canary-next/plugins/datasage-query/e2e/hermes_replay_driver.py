#!/usr/bin/env python3
"""Drive DataSage golden cases through Hermes CLI and score persisted evidence.

Commands are argv templates and always execute with ``shell=False``. Captured
stdout/stderr, prompts, tool rows, and answer text are never written to the
report. The durable output contains only identifiers, status, and hashes.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import stat
import types
from collections import Counter
from pathlib import Path
from typing import Any, Callable

if os.name == "nt":
    import msvcrt


HERE = Path(__file__).resolve().parent
CONFIG_SCHEMA = "datasage-hermes-replay-config/v2"
REVIEW_SCHEMA = "datasage-review-labels/v2"
REPORT_SCHEMA = "datasage-hermes-replay-report/v1"
BUNDLE_SCHEMA = "datasage-hermes-replay-bundle/v1"
REPLAY_CONTRACT_ENV = "DATASAGE_REPLAY_EXECUTION_CONTRACT_SHA256"
REQUIRED_CREDENTIAL_KEYS = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "DATA_QUERY_MYSQL_HOST",
        "DATA_QUERY_MYSQL_PORT",
        "DATA_QUERY_MYSQL_DATABASE",
        "DATA_QUERY_MYSQL_USER",
        "DATA_QUERY_MYSQL_PASSWORD",
    }
)
OPTIONAL_CREDENTIAL_KEYS = frozenset({"DATA_QUERY_MYSQL_SSL_CA"})
PROJECTED_CREDENTIAL_KEYS = REQUIRED_CREDENTIAL_KEYS | OPTIONAL_CREDENTIAL_KEYS
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WINDOWS_ALLOWED_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})


def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(
        f"datasage_replay_{name}",
        HERE / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


ADAPTER = _sibling("canary_transcript_adapter")
SCORER = _sibling("golden_expert_scorer")
LIVE_FIXTURE = _sibling("live_fixture_materializer")
MAX_EXCHANGE_BYTES = 2_000_000


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _stage_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return Path(handle.name)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _sha(value: Any) -> str:
    return ADAPTER._sha256(value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(
        int(getattr(details, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _windows_acl(path: Path) -> dict[str, Any]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file() or _is_reparse(executable):
        raise ValueError("Windows ACL API is unavailable")
    script = r"""
$ErrorActionPreference='Stop'
$target=$env:DATASAGE_REPLAY_ACL_TARGET
$current=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=Get-Acl -LiteralPath $target
$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {
  [ordered]@{sid=$_.IdentityReference.Value;type=$_.AccessControlType.ToString();inherited=$_.IsInherited;rights=[int64]$_.FileSystemRights}
})
[ordered]@{current_sid=$current.Value;owner_sid=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;protected=$acl.AreAccessRulesProtected;rules=$rules} | ConvertTo-Json -Compress -Depth 4
"""
    environment = {
        key: os.environ[key]
        for key in (
            "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PSModulePath",
        )
        if key in os.environ
    }
    environment["DATASAGE_REPLAY_ACL_TARGET"] = str(path)
    try:
        completed = subprocess.run(
            [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
            check=False,
            env=environment,
        )
        value = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError("Windows ACL inspection failed") from exc
    if not isinstance(value, dict):
        raise ValueError("Windows ACL inspection failed")
    return value


def _assert_restricted_acl(path: Path, *, require_protected: bool) -> None:
    if os.name == "nt":
        value = _windows_acl(path)
        current = value.get("current_sid")
        allowed = _WINDOWS_ALLOWED_SIDS | ({current} if isinstance(current, str) else set())
        rules = value.get("rules")
        if isinstance(rules, dict):
            rules = [rules]
        if (
            (require_protected and value.get("protected") is not True)
            or value.get("owner_sid") not in allowed
            or not isinstance(rules, list)
            or not rules
            or any(
                not isinstance(rule, dict)
                or rule.get("sid") not in allowed
                or rule.get("type") != "Allow"
                for rule in rules
            )
            or current not in {rule.get("sid") for rule in rules if isinstance(rule, dict)}
        ):
            raise ValueError("runtime ACL is not restricted")
        return
    details = path.stat(follow_symlinks=False)
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("runtime ACL is not restricted")


class _CredentialLock:
    """Hold the approved credential file immutable for one complete replay.

    On Windows the handle deliberately shares reads only.  The launcher may
    independently attest the file, while writers and delete/replace operations
    remain denied until every replay process has exited.
    """

    def __init__(self, path: Path, *, max_bytes: int = 1024 * 1024) -> None:
        self.path = Path(os.path.abspath(path))
        self.max_bytes = max_bytes
        self._stream: Any = None
        self._initial = bytearray()
        self._path_identity: tuple[int, ...] = ()
        self._handle_identity: tuple[int, ...] = ()
        self._open()

    @staticmethod
    def _path_token(path: Path) -> tuple[int, ...]:
        details = path.lstat()
        return (
            int(details.st_dev), int(details.st_ino), int(details.st_nlink),
            int(details.st_size), int(details.st_mtime_ns), int(details.st_ctime_ns),
        )

    def _validate_path(self) -> tuple[int, ...]:
        try:
            details = self.path.lstat()
        except OSError as exc:
            raise ValueError("credential source is missing or unsafe") from exc
        if (
            _is_reparse(self.path)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_size > self.max_bytes
            or os.path.normcase(str(self.path.resolve(strict=True)))
            != os.path.normcase(str(self.path))
        ):
            raise ValueError("credential source is missing or unsafe")
        _assert_restricted_acl(self.path, require_protected=True)
        return self._path_token(self.path)

    def _windows_handle_token(self) -> tuple[int, ...]:
        from ctypes import wintypes

        class _Info(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation_time_low", wintypes.DWORD), ("creation_time_high", wintypes.DWORD),
                ("access_time_low", wintypes.DWORD), ("access_time_high", wintypes.DWORD),
                ("write_time_low", wintypes.DWORD), ("write_time_high", wintypes.DWORD),
                ("volume_serial", wintypes.DWORD),
                ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD), ("file_index_low", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        info = _Info()
        handle = msvcrt.get_osfhandle(self._stream.fileno())
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ValueError("credential source identity is unreadable")
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        final_buffer = ctypes.create_unicode_buffer(32768)
        final_size = kernel32.GetFinalPathNameByHandleW(
            handle, final_buffer, len(final_buffer), 0
        )
        if not final_size or final_size >= len(final_buffer):
            raise ValueError("credential source final path is unreadable")
        final_path = final_buffer.value
        if final_path.startswith("\\\\?\\UNC\\"):
            final_path = "\\\\" + final_path[8:]
        elif final_path.startswith("\\\\?\\"):
            final_path = final_path[4:]
        if os.path.normcase(os.path.abspath(final_path)) != os.path.normcase(
            str(self.path)
        ):
            raise ValueError("credential source final path changed")
        token = (
            int(info.volume_serial),
            (int(info.file_index_high) << 32) | int(info.file_index_low),
            int(info.links),
            (int(info.size_high) << 32) | int(info.size_low),
        )
        if info.attributes & 0x400 or info.links != 1 or token[3] > self.max_bytes:
            raise ValueError("credential source is missing or unsafe")
        return token

    def _handle_token(self) -> tuple[int, ...]:
        if os.name == "nt":
            return self._windows_handle_token()
        details = os.fstat(self._stream.fileno())
        if details.st_nlink != 1 or details.st_size > self.max_bytes:
            raise ValueError("credential source is missing or unsafe")
        return (
            int(details.st_dev), int(details.st_ino),
            int(details.st_nlink), int(details.st_size),
        )

    def _read(self) -> bytes:
        self._stream.seek(0)
        payload = self._stream.read(self.max_bytes + 1)
        if len(payload) > self.max_bytes:
            raise ValueError("credential source is missing or unsafe")
        return payload

    def _open(self) -> None:
        self._path_identity = self._validate_path()
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            ]
            create_file.restype = wintypes.HANDLE
            handle = create_file(
                str(self.path),
                0x80000000,  # GENERIC_READ
                0x00000001,  # FILE_SHARE_READ only: deny write and delete
                None,
                3,  # OPEN_EXISTING
                0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
                None,
            )
            if handle == wintypes.HANDLE(-1).value:
                raise ValueError("credential source cannot be opened safely")
            try:
                descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
                handle = None
                self._stream = os.fdopen(descriptor, "rb", closefd=True)
            finally:
                if handle is not None:
                    kernel32.CloseHandle(handle)
        else:
            descriptor = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            self._stream = os.fdopen(descriptor, "rb", closefd=True)
        try:
            self._handle_identity = self._handle_token()
            payload = self._read()
            if (
                self._path_identity != self._validate_path()
                or self._handle_identity != self._handle_token()
                or len(payload) != self._handle_identity[3]
            ):
                raise ValueError("credential source changed during validation")
            initial_values = _parse_credentials(payload)
            initial_values.clear()
            self._initial.extend(payload)
        except BaseException:
            self.close()
            raise

    def values(self, config: dict[str, Any], evidence: dict[str, Any]) -> dict[str, str]:
        if self._stream is None or self.path != _credential_source(config):
            raise ValueError("credential source lock is invalid")
        payload = self._read()
        if (
            self._handle_identity != self._handle_token()
            or self._path_identity != self._validate_path()
            or payload != self._initial
        ):
            raise ValueError("credential source changed during replay")
        values = _parse_credentials(payload)
        expected = {
            "schema": "datasage-replay-credential-source/v1",
            "command_config_sha256": _execution_contract(config),
            "projected_key_names": sorted(values),
            "acl_restricted": True,
            "owner_approved": True,
            "file_identity_stable": True,
            "disk_materialized": False,
            "ssl_ca_configured": False,
        }
        if evidence != expected:
            values.clear()
            raise ValueError("credential source evidence is invalid")
        return values

    def close(self) -> None:
        for index in range(len(self._initial)):
            self._initial[index] = 0
        self._initial.clear()
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "_CredentialLock":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _parse_credentials(payload: bytes) -> dict[str, str]:
    if payload.startswith(b"\xef\xbb\xbf") or b"\x00" in payload:
        raise ValueError("credential document is malformed")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("credential document is malformed") from exc
    values: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw or raw.startswith("#"):
            continue
        if raw != raw.strip() or raw.startswith("export ") or "=" not in raw:
            raise ValueError("credential document is malformed")
        key, value = raw.split("=", 1)
        if (
            ENV_KEY.fullmatch(key) is None
            or key in values
            or value != value.strip()
            or value.endswith("\\")
            or value[:1] in {"'", '"'}
            or value[-1:] in {"'", '"'}
        ):
            raise ValueError("credential document is malformed")
        values[key] = value
    projected = {key: values[key] for key in PROJECTED_CREDENTIAL_KEYS if values.get(key)}
    if not REQUIRED_CREDENTIAL_KEYS.issubset(projected):
        raise ValueError("credential document lacks required keys")
    if "DATA_QUERY_MYSQL_SSL_CA" in projected:
        raise ValueError("CREDENTIAL_SSL_CA_UNSUPPORTED")
    return projected


def _credential_source(config: dict[str, Any]) -> Path:
    command = config["attestation_command"]
    if command.count("--credential-source") != 1:
        raise ValueError("attestation command lacks credential source binding")
    index = command.index("--credential-source")
    if index + 1 >= len(command) or not Path(command[index + 1]).is_absolute():
        raise ValueError("attestation credential source binding is invalid")
    return Path(os.path.abspath(command[index + 1]))


def _credential_values(
    config: dict[str, Any], evidence: dict[str, Any], credential_lock: _CredentialLock
) -> dict[str, str]:
    return credential_lock.values(config, evidence)


def _runtime_guard(config: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    profile = Path(config["profile_root"])
    replay_root = profile.parent.parent
    paths = {
        "replay_root": replay_root,
        "marker": replay_root / "REPLAY_ISOLATION.json",
        "state_db": Path(config["state_db"]),
    }
    identities: dict[str, tuple[int, int, int]] = {}
    for name, path in paths.items():
        if _is_reparse(path) or not path.exists():
            raise ValueError("isolated runtime guard path is unsafe")
        details = path.lstat()
        if name != "replay_root" and (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1):
            raise ValueError("isolated runtime guard file is unsafe")
        _assert_restricted_acl(path, require_protected=name == "replay_root")
        identities[name] = (int(details.st_dev), int(details.st_ino), int(details.st_nlink))
    if (profile / ".env").exists() or os.path.lexists(profile / ".env"):
        raise ValueError("isolated runtime unexpectedly materialized credentials")
    return identities


def _run_bounded_process(
    command: list[str],
    *,
    timeout: int,
    cwd: str,
    env: dict[str, str],
    inherited_descriptors: tuple[int, ...] = (),
    inherited_handles: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run one replay turn and kill its complete descendant tree on every exit."""

    if os.name != "nt":
        if inherited_handles:
            raise ValueError("native inherited handles are Windows-only")
        process = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            start_new_session=True,
            pass_fds=inherited_descriptors,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as exc:
            try:
                os.killpg(process.pid, 9)
            except OSError:
                process.kill()
            stdout, stderr = process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(
                    command, timeout, output=stdout, stderr=stderr
                ) from None
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD

    def resume_initial_threads(pid: int) -> None:
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
        if snapshot == wintypes.HANDLE(-1).value:
            raise RuntimeError("replay process containment is unavailable")
        resumed = 0
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if int(entry.th32OwnerProcessID) == pid:
                    thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise RuntimeError("replay process containment is unavailable")
                    try:
                        if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise RuntimeError("replay process containment is unavailable")
                        resumed += 1
                    finally:
                        kernel32.CloseHandle(thread)
                entry.dwSize = ctypes.sizeof(entry)
                present = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        if resumed < 1:
            raise RuntimeError("replay process containment is unavailable")

    def bounded_direct_kill(process: subprocess.Popen[str]) -> tuple[str, str]:
        if process.poll() is None:
            process.kill()
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("JOB_TERMINATION_FAILED") from exc

    def terminate_job_and_drain(
        job_handle: Any, process: subprocess.Popen[str]
    ) -> tuple[str, str]:
        if process.poll() is None and not kernel32.TerminateJobObject(job_handle, 1):
            bounded_direct_kill(process)
            raise RuntimeError("JOB_TERMINATION_FAILED")
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            bounded_direct_kill(process)
            raise RuntimeError("JOB_TERMINATION_FAILED") from None

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise RuntimeError("replay process containment is unavailable")
    limits = _ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(job)
        raise RuntimeError("replay process containment is unavailable")
    process: subprocess.Popen[str] | None = None
    child_handles = list(inherited_handles) or [
        msvcrt.get_osfhandle(fd) for fd in inherited_descriptors
    ]
    startupinfo = None
    if child_handles:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": child_handles}
        for handle in child_handles:
            os.set_handle_inheritable(handle, True)
    try:
        try:
            process = subprocess.Popen(
                command,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                env=env,
                creationflags=(
                    getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
                    | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
                ),
                close_fds=True,
                startupinfo=startupinfo,
            )
        finally:
            for handle in child_handles:
                os.set_handle_inheritable(handle, False)
        if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
            bounded_direct_kill(process)
            raise RuntimeError("replay process containment is unavailable")
        try:
            resume_initial_threads(process.pid)
        except BaseException:
            terminate_job_and_drain(job, process)
            raise
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as exc:
            stdout, stderr = terminate_job_and_drain(job, process)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(
                    command, timeout, output=stdout, stderr=stderr
                ) from None
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process is not None and process.poll() is None:
            terminate_job_and_drain(job, process)
        kernel32.CloseHandle(job)


def _config(value: Any) -> dict[str, Any]:
    required = {
        "schema", "attestation_command", "start_command", "resume_command",
        "session_id_regex", "profile_root", "state_db", "working_directory",
    }
    optional = {
        "timeout_seconds", "environment", "attestation_working_directory",
        "control_package_root",
    }
    if not isinstance(value, dict) or set(value) - (required | optional) or not required.issubset(value):
        raise ValueError("command config has invalid keys")
    if value["schema"] != CONFIG_SCHEMA:
        raise ValueError(f"command config schema must be {CONFIG_SCHEMA}")
    for key in ("attestation_command", "start_command", "resume_command"):
        command = value[key]
        if not isinstance(command, list) or not command or any(not isinstance(token, str) or not token for token in command):
            raise ValueError(f"{key} must be a non-empty argv string list")
        if not Path(command[0]).is_absolute():
            raise ValueError(f"{key} executable must be an absolute path")
    if not any("{prompt}" in token for token in value["start_command"]):
        raise ValueError("start_command must include {prompt}")
    if not any("{prompt}" in token for token in value["resume_command"]) or not any(
        "{session_id}" in token for token in value["resume_command"]
    ):
        raise ValueError("resume_command must include {prompt} and {session_id}")
    try:
        re.compile(value["session_id_regex"])
    except (TypeError, re.error) as exc:
        raise ValueError("session_id_regex is invalid") from exc
    timeout = value.get("timeout_seconds", 180)
    if not isinstance(timeout, int) or not 1 <= timeout <= 1800:
        raise ValueError("timeout_seconds must be between 1 and 1800")
    for key in ("profile_root", "state_db", "working_directory"):
        path = Path(value[key]) if isinstance(value[key], str) else Path()
        if not path.is_absolute():
            raise ValueError(f"{key} must be an absolute path")
    control_package_root = value.get("control_package_root")
    if not isinstance(control_package_root, str) or not Path(control_package_root).is_absolute():
        raise ValueError("control_package_root must be an absolute path")
    if "attestation_working_directory" in value:
        attestation_cwd = value["attestation_working_directory"]
        if not isinstance(attestation_cwd, str) or not Path(attestation_cwd).is_absolute():
            raise ValueError("attestation_working_directory must be an absolute path")
    if Path(value["state_db"]).resolve(strict=False) != (
        Path(value["profile_root"]).resolve(strict=False) / "state.db"
    ):
        raise ValueError("state_db must be the attested Profile root state.db")
    environment = value.get("environment", {})
    allowed = {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    if (
        not isinstance(environment, dict)
        or any(key not in allowed for key in environment)
        or any(not isinstance(item, str) for item in environment.values())
    ):
        raise ValueError("environment contains a non-allowlisted variable")
    normalized = dict(value)
    _credential_source(normalized)
    return normalized


def _reviews(value: Any, case_ids: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("schema") != REVIEW_SCHEMA or set(value) != {"schema", "reviews"}:
        raise ValueError(f"review labels schema must be {REVIEW_SCHEMA}")
    reviews = value.get("reviews")
    if not isinstance(reviews, dict) or any(key not in case_ids for key in reviews):
        raise ValueError("review labels contain invalid or unknown test IDs")
    if any(not isinstance(review, dict) for review in reviews.values()):
        raise ValueError("review labels must be assertion objects")
    return dict(reviews)


def _isolated_environment(
    config: dict[str, Any], extra: dict[str, str] | None = None
) -> dict[str, str]:
    inherited = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP")
        if key in os.environ
    }
    inherited.update(config.get("environment", {}))
    inherited.update(extra or {})
    profile_root = Path(config["profile_root"]).resolve(strict=False)
    profile_value = str(profile_root)
    # Python's Windows expanduser/Path.home requires USERPROFILE or the
    # HOMEDRIVE+HOMEPATH pair.  Never inherit those ambient identities: they
    # would either make a minimal replay process fail at startup or let it
    # escape the attested Profile into the operator's real home directory.
    inherited["HOME"] = profile_value
    inherited["USERPROFILE"] = profile_value
    inherited["APPDATA"] = str(profile_root / ".appdata" / "Roaming")
    inherited["LOCALAPPDATA"] = str(profile_root / ".appdata" / "Local")
    if os.name == "nt":
        inherited["HOMEDRIVE"] = profile_root.drive
        inherited["HOMEPATH"] = profile_value[len(profile_root.drive):]
    inherited["HERMES_HOME"] = profile_value
    inherited["PYTHONDONTWRITEBYTECODE"] = "1"
    return inherited


def _protect_attestation_home(path: Path) -> None:
    """Restrict the disposable home used only while the launcher attests."""

    if os.name == "nt":
        value = _windows_acl(path)
        current = value.get("current_sid")
        if not isinstance(current, str) or not current.startswith("S-1-"):
            raise ValueError("attestation home ACL cannot be restricted")
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        executable = system_root / "System32" / "icacls.exe"
        if not executable.is_file() or _is_reparse(executable):
            raise ValueError("attestation home ACL cannot be restricted")
        completed = subprocess.run(
            [
                str(executable), str(path), "/inheritance:r", "/grant:r",
                f"*{current}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
                "/Q",
            ],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("attestation home ACL cannot be restricted")
        after = _windows_acl(path)
        allowed = _WINDOWS_ALLOWED_SIDS | {current}
        rules = after.get("rules")
        if isinstance(rules, dict):
            rules = [rules]
        unwanted = sorted(
            {
                rule.get("sid")
                for rule in (rules if isinstance(rules, list) else [])
                if isinstance(rule, dict)
                and isinstance(rule.get("sid"), str)
                and rule["sid"] not in allowed
            }
        )
        for sid in unwanted:
            removed = subprocess.run(
                [
                    str(executable), str(path), "/remove:g", f"*{sid}",
                    "/remove:d", f"*{sid}", "/Q",
                ],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if removed.returncode != 0:
                raise ValueError("attestation home ACL cannot be restricted")
    else:
        path.chmod(0o700)
    _assert_restricted_acl(path, require_protected=os.name == "nt")


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(parent)))
        ) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _paths_overlap(left: Path, right: Path) -> bool:
    return _path_is_within(left, right) or _path_is_within(right, left)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return types.MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _review_contracts(suite: dict[str, Any], case_ids: set[str]) -> list[dict[str, Any]]:
    fields = (
        "domains", "metrics", "dimensions", "operations",
        "time_semantics", "context_action", "context_bindings",
    )
    return [
        {
            "id": case["id"],
            "expected_plan": {
                key: copy.deepcopy(case["expected_plan"].get(key, {}))
                for key in fields
            },
            "required_conclusions": copy.deepcopy(case["required_conclusions"]),
            "allowed_conclusions": copy.deepcopy(case["allowed_conclusions"]),
            "forbidden_conclusions": copy.deepcopy(case["forbidden_conclusions"]),
        }
        for case in suite["cases"]
        if case["id"] in case_ids
    ]


def _review_state_snapshot(state_db: Path) -> str:
    """Hash the complete replay transcript state without exposing its values."""

    connection = ADAPTER._open_read_only(state_db)
    try:
        sessions = connection.execute(
            "SELECT * FROM sessions ORDER BY id"
        ).fetchall()
        messages = connection.execute(
            "SELECT * FROM messages ORDER BY id"
        ).fetchall()
        return _sha(
            {
                "database_identity_sha256": _database_identity(state_db),
                "sessions": [
                    [_sha(value) for value in tuple(row)] for row in sessions
                ],
                "messages": [
                    [_sha(value) for value in tuple(row)] for row in messages
                ],
            }
        )
    finally:
        connection.close()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def _immutable_review_snapshot(config: dict[str, Any], state_db: Path):
    """Back up live state once, then retain one read transaction for both adaptations."""

    replay_root = Path(config["profile_root"]).resolve(strict=True).parent.parent
    directory = Path(tempfile.mkdtemp(prefix=".review-snapshot-", dir=replay_root)).resolve(strict=True)
    snapshot = directory / "state.db"
    connection: sqlite3.Connection | None = None
    try:
        if directory.parent != replay_root or _is_reparse(directory):
            raise ValueError("review snapshot is outside replay root")
        _protect_attestation_home(directory)
        descriptor = os.open(
            snapshot,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)
        source = ADAPTER._open_read_only(state_db)
        try:
            destination = sqlite3.connect(snapshot)
            try:
                source.backup(destination)
                destination.commit()
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("review snapshot integrity check failed")
            finally:
                destination.close()
        finally:
            source.close()
        snapshot.chmod(0o600)
        _assert_restricted_acl(snapshot, require_protected=False)
        details = snapshot.lstat()
        if _is_reparse(snapshot) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError("review snapshot is unsafe")
        identity = (int(details.st_dev), int(details.st_ino), int(details.st_nlink))
        content_sha256 = _file_sha256(snapshot)
        connection = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("review snapshot integrity check failed")

        def validate() -> None:
            current = snapshot.lstat()
            if (
                _is_reparse(snapshot)
                or (int(current.st_dev), int(current.st_ino), int(current.st_nlink)) != identity
                or _file_sha256(snapshot) != content_sha256
            ):
                raise ValueError("review snapshot identity changed")

        validate()
        yield snapshot, connection, validate
        validate()
    finally:
        if connection is not None:
            connection.close()
        _confined_remove_tree(directory, replay_root)


def _capture_projection(candidate: dict[str, Any]) -> dict[str, Any]:
    """Exclude only fields derived from reviewer assertions and their aggregate hashes."""

    projected = copy.deepcopy(candidate)
    for case in projected.get("cases", []):
        case.pop("conclusions", None)
        case.pop("conclusion_review", None)
    receipt = projected.get("canary_receipt", {})
    receipt.pop("receipt_sha256", None)
    receipt.pop("candidate_cases_sha256", None)
    for turn in receipt.get("turns", []):
        turn.pop("candidate_case_sha256", None)
        turn.pop("conclusion_review", None)
    return projected


def _assert_exchange_handle_path(descriptor: int, expected: Path) -> None:
    if os.name != "nt":
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = msvcrt.get_osfhandle(descriptor)
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD
    ]
    buffer = ctypes.create_unicode_buffer(32768)
    size = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not size or size >= len(buffer):
        raise ValueError("live fixture exchange final path is unreadable")
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        final = "\\\\" + final[8:]
    elif final.startswith("\\\\?\\"):
        final = final[4:]
    if os.path.normcase(os.path.abspath(final)) != os.path.normcase(str(expected)):
        raise ValueError("live fixture exchange final path changed")


class _HeldExchange:
    """Two pre-created files whose descriptors, not paths, cross the process boundary."""

    def __init__(self, directory: Path, request: Path, response: Path, descriptors: tuple[int, int]):
        self.directory = directory
        self.request_path = request
        self.response_path = response
        self.descriptors = descriptors
        self.identities = tuple(self._identity(path, fd) for path, fd in zip((request, response), descriptors))

    @staticmethod
    def _identity(path: Path, descriptor: int) -> tuple[int, int, int]:
        _assert_restricted_acl(path, require_protected=False)
        details = path.lstat()
        held = os.fstat(descriptor)
        if (
            _is_reparse(path)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or (details.st_dev, details.st_ino) != (held.st_dev, held.st_ino)
        ):
            raise ValueError("live fixture exchange identity is unsafe")
        _assert_exchange_handle_path(descriptor, path)
        return int(held.st_dev), int(held.st_ino), int(held.st_nlink)

    def validate(self) -> None:
        current = tuple(
            self._identity(path, fd)
            for path, fd in zip((self.request_path, self.response_path), self.descriptors)
        )
        if current != self.identities:
            raise ValueError("live fixture exchange identity changed")

    @contextlib.contextmanager
    def child_handles(self):
        """Create least-privilege child handles while retaining parent handles."""

        if os.name != "nt":
            yield tuple(str(fd) for fd in self.descriptors), (), self.descriptors
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        duplicated: list[int] = []
        try:
            for descriptor, access in zip(self.descriptors, (0x80000000, 0x40000000)):
                target = wintypes.HANDLE()
                if not kernel32.DuplicateHandle(
                    process,
                    wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
                    process,
                    ctypes.byref(target),
                    access,
                    False,
                    0,
                ):
                    raise ValueError("live fixture child handle restriction failed")
                duplicated.append(int(target.value))
            yield tuple(str(handle) for handle in duplicated), tuple(duplicated), ()
        finally:
            for handle in duplicated:
                kernel32.CloseHandle(wintypes.HANDLE(handle))

    def write_request(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > MAX_EXCHANGE_BYTES:
            raise ValueError("live fixture exchange request is oversized")
        self.validate()
        descriptor = self.descriptors[0]
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        self.validate()

    def read_response(self) -> dict[str, Any]:
        self.validate()
        descriptor = self.descriptors[1]
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, MAX_EXCHANGE_BYTES + 1)
        self.validate()
        if len(raw) > MAX_EXCHANGE_BYTES:
            raise ValueError("live fixture exchange response is oversized")
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("live fixture exchange response is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("live fixture exchange response is invalid")
        return value


def _confined_remove_tree(exchange: Path, replay_root: Path) -> None:
    if not (_path_is_within(exchange, replay_root) and exchange.parent == replay_root):
        return
    if not os.path.lexists(exchange):
        return
    if _is_reparse(exchange):
        exchange.rmdir()
        return
    for target in list(exchange.iterdir()):
        if target.parent != exchange:
            raise ValueError("live fixture exchange cleanup escaped")
        if target.is_dir() and not _is_reparse(target):
            _confined_remove_tree(target, exchange)
        elif target.is_dir():
            target.rmdir()
        else:
            target.unlink(missing_ok=True)
    exchange.rmdir()


@contextlib.contextmanager
def _secure_exchange(config: dict[str, Any], request_payload: dict[str, Any]):
    replay_root = Path(config["profile_root"]).resolve(strict=True).parent.parent
    _assert_restricted_acl(replay_root, require_protected=os.name == "nt")
    exchange = Path(
        tempfile.mkdtemp(prefix=".live-fixture-exchange-", dir=replay_root)
    ).resolve(strict=True)
    request = exchange / "request.bin"
    response = exchange / "response.bin"
    descriptors: list[int] = []
    try:
        if exchange.parent != replay_root or _is_reparse(exchange):
            raise ValueError("live fixture exchange is outside replay root")
        _protect_attestation_home(exchange)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptors = [os.open(path, flags, 0o600) for path in (request, response)]
        held = _HeldExchange(exchange, request, response, tuple(descriptors))
        held.write_request(request_payload)
        yield held
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _confined_remove_tree(exchange, replay_root)


@contextlib.contextmanager
def _temporary_attestation_home(config: dict[str, Any]):
    """Create a restricted launcher home that cannot pre-create replay_root."""

    profile_root = Path(config["profile_root"]).resolve(strict=False)
    replay_root = profile_root.parent.parent
    bases = (Path(tempfile.gettempdir()), Path.cwd())
    for base in dict.fromkeys(
        candidate.resolve(strict=True) for candidate in bases if candidate.is_dir()
    ):
        home = Path(
            tempfile.mkdtemp(prefix="datasage-attestation-home-", dir=base)
        ).resolve(strict=True)
        if _is_reparse(home):
            shutil.rmtree(home, ignore_errors=False)
            raise ValueError("attestation home overlaps the secure replay root")
        if _paths_overlap(home, replay_root) or _paths_overlap(home, profile_root):
            shutil.rmtree(home, ignore_errors=False)
            continue
        try:
            _protect_attestation_home(home)
            yield home
            return
        finally:
            shutil.rmtree(home, ignore_errors=False)
    raise ValueError("attestation home overlaps the secure replay root")


def _attestation_environment(
    config: dict[str, Any], home: Path, extra: dict[str, str]
) -> dict[str, str]:
    environment = _isolated_environment(config, extra)
    home_value = str(home)
    environment["HOME"] = home_value
    environment["USERPROFILE"] = home_value
    environment["APPDATA"] = str(home / "AppData" / "Roaming")
    environment["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    environment["HERMES_HOME"] = home_value
    if os.name == "nt":
        environment["HOMEDRIVE"] = home.drive
        environment["HOMEPATH"] = home_value[len(home.drive):]
    return environment


def _execution_contract(config: dict[str, Any]) -> str:
    return _sha(
        {
            "attestation_command": config["attestation_command"],
            "attestation_working_directory": str(
                Path(
                    config.get("attestation_working_directory", config["working_directory"])
                ).resolve(strict=False)
            ),
            "start_command": config["start_command"],
            "resume_command": config["resume_command"],
            "profile_root": str(Path(config["profile_root"]).resolve(strict=False)),
            "state_db": str(Path(config["state_db"]).resolve(strict=False)),
            "control_package_root": str(
                Path(config["control_package_root"]).resolve(strict=False)
            ),
            "working_directory": str(Path(config["working_directory"]).resolve(strict=False)),
            "environment": config.get("environment", {}),
        }
    )


def _final_execution_contract(
    provisioning_contract_sha256: str,
    overlay: dict[str, Any],
    runner: dict[str, Any],
    credential_projection: dict[str, Any],
) -> str:
    return _sha(
        {
            "provisioning_contract_sha256": provisioning_contract_sha256,
            "replay_policy_overlay": overlay,
            "trusted_replay_runner": runner,
            "credential_projection": credential_projection,
        }
    )


def _validate_replay_evidence(
    config: dict[str, Any],
    attestation: dict[str, Any],
    provisioning_contract: str,
    credential_lock: _CredentialLock,
) -> None:
    isolation = attestation["replay_isolation"]
    overlay = isolation.get("replay_policy_overlay")
    runner = isolation.get("trusted_replay_runner")
    credential = isolation.get("credential_projection")
    if (
        not isinstance(overlay, dict)
        or not isinstance(runner, dict)
        or not isinstance(credential, dict)
    ):
        raise ValueError("runtime attestation lacks replay overlay evidence")
    required_overlay = {
        "schema", "base_config_sha256", "canonical_patch",
        "canonical_patch_sha256", "final_config_sha256",
    }
    if set(overlay) != required_overlay or overlay.get("schema") != "datasage-replay-policy-overlay/v1":
        raise ValueError("runtime replay overlay evidence has invalid fields")
    patch = overlay.get("canonical_patch")
    rule = patch.get("value") if isinstance(patch, dict) else None
    if (
        not isinstance(rule, dict)
        or patch.get("op") != "add"
        or patch.get("path")
        != "/plugins/entries/datasage-query/settings/data_entitlements/principals/-"
        or rule.get("platform") != "replay"
        or rule.get("source") != "datasage-trusted-replay"
        or rule.get("user_id") != "datasage-ephemeral-replay"
        or _sha(patch) != overlay.get("canonical_patch_sha256")
    ):
        raise ValueError("runtime replay overlay patch is not canonical")
    profile = Path(config["profile_root"]).resolve(strict=False)
    replay_root = profile.parent.parent
    unit_id = attestation.get("release_unit_id")
    base_config = (
        replay_root / "dsrt" / "runtime" / "deployments" / str(unit_id)
        / "profile" / "config.yaml"
    )
    unit_profile = base_config.parent
    final_config = profile / "config.yaml"
    runner_path = profile / str(runner.get("relative_path", ""))
    expected_runner = Path(str(config["start_command"][1])).resolve(strict=False)
    try:
        import yaml

        base_object = yaml.safe_load(base_config.read_bytes())
        final_object = yaml.safe_load(final_config.read_bytes())
        base_principals = base_object["plugins"]["entries"]["datasage-query"][
            "settings"
        ]["data_entitlements"]["principals"]
        approved = [
            item
            for item in base_principals
            if isinstance(item, dict)
            and item.get("platform") == "wecom"
            and item.get("user_id") == "*"
        ]
        expected_rule = copy.deepcopy(approved[0]) if len(approved) == 1 else None
        if expected_rule is not None:
            expected_rule.update(
                {
                    "platform": "replay",
                    "source": "datasage-trusted-replay",
                    "user_id": "datasage-ephemeral-replay",
                }
            )
        expected_final = copy.deepcopy(base_object)
        expected_final["plugins"]["entries"]["datasage-query"]["settings"][
            "data_entitlements"
        ]["principals"].append(copy.deepcopy(rule))
        manifest_matches = []
        for line in (unit_profile / ".release" / "MANIFEST.sha256").read_text(
            encoding="utf-8"
        ).splitlines():
            digest, separator, relative = line.partition("  ")
            if separator and relative == runner.get("relative_path"):
                manifest_matches.append(digest)
    except Exception as exc:
        raise ValueError("runtime replay configs are not independently auditable") from exc
    if (
        not base_config.is_file()
        or _file_sha256(base_config) != overlay.get("base_config_sha256")
        or not final_config.is_file()
        or _file_sha256(final_config) != overlay.get("final_config_sha256")
        or rule != expected_rule
        or final_object != expected_final
        or runner.get("runtime_path") != str(expected_runner)
        or manifest_matches != [runner.get("sha256")]
        or runner_path.resolve(strict=False) != expected_runner
        or not runner_path.is_file()
        or _file_sha256(runner_path) != runner.get("sha256")
        or runner.get("profile_artifact_id") != attestation.get("profile_artifact_id")
        or runner.get("profile_payload_sha256") != attestation.get("profile_payload_sha256")
        or isolation.get("provisioning_contract_sha256") != provisioning_contract
        or isolation.get("execution_contract_sha256")
        != _final_execution_contract(provisioning_contract, overlay, runner, credential)
    ):
        raise ValueError("runtime replay overlay or runner binding is invalid")
    marker_path = replay_root / "REPLAY_ISOLATION.json"
    try:
        marker = _load(marker_path)
    except (OSError, ValueError) as exc:
        raise ValueError("runtime replay evidence marker is unreadable") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != "datasage-ephemeral-replay-runtime/v5"
        or marker.get("replay_policy_overlay") != overlay
        or marker.get("trusted_replay_runner") != runner
        or marker.get("credential_projection") != credential
        or marker.get("provisioning_contract_sha256") != provisioning_contract
        or marker.get("execution_contract_sha256")
        != isolation.get("execution_contract_sha256")
    ):
        raise ValueError("runtime replay evidence marker differs from attestation")
    values = _credential_values(config, credential, credential_lock)
    values.clear()


def _attest(
    config: dict[str, Any],
    timeout: int,
    execution_contract_sha256: str,
    credential_lock: _CredentialLock,
) -> tuple[dict[str, str], dict[str, Any]]:
    with _temporary_attestation_home(config) as attestation_home:
        completed = subprocess.run(
            config["attestation_command"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=config.get("attestation_working_directory", config["working_directory"]),
            env=_attestation_environment(
                config,
                attestation_home,
                {REPLAY_CONTRACT_ENV: execution_contract_sha256},
            ),
        )
    if completed.returncode != 0:
        raise ValueError("isolated runtime attestation command failed")
    try:
        attestation = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("isolated runtime attestation is not JSON") from exc
    required = {
        "version", "ready", "release_unit_id", "profile_artifact_id",
        "profile_payload_sha256", "runtime_home", "hermes", "control",
        "replay_isolation",
    }
    if (
        not isinstance(attestation, dict)
        or not required.issubset(attestation)
        or attestation.get("version") != "datasage-runtime-attestation/v1"
        or attestation.get("ready") is not True
        or Path(str(attestation.get("runtime_home"))).resolve(strict=False)
        != Path(config["profile_root"]).resolve(strict=False)
    ):
        raise ValueError("isolated runtime attestation does not bind the configured Profile")
    isolation = attestation.get("replay_isolation")
    if (
        not isinstance(isolation, dict)
        or isolation.get("mode") != "ephemeral-replay"
        or isolation.get("gateway_attached") is not False
        or not isinstance(isolation.get("isolation_id"), str)
        or not isolation["isolation_id"]
        or isolation.get("provisioning_contract_sha256")
        != execution_contract_sha256
        or Path(str(isolation.get("state_db"))).resolve(strict=False)
        != Path(config["state_db"]).resolve(strict=False)
    ):
        raise ValueError("runtime attestation has no isolated replay state proof")
    _validate_replay_evidence(
        config, attestation, execution_contract_sha256, credential_lock
    )
    payload = attestation.get("profile_payload_sha256")
    if not isinstance(payload, str) or not re.fullmatch(r"[0-9a-f]{64}", payload):
        raise ValueError("isolated runtime attestation payload digest is invalid")
    if (
        not isinstance(attestation.get("release_unit_id"), str)
        or not attestation["release_unit_id"]
        or not isinstance(attestation.get("profile_artifact_id"), str)
        or not attestation["profile_artifact_id"]
        or not isinstance(attestation.get("hermes"), dict)
        or not attestation["hermes"]
        or not isinstance(attestation.get("control"), dict)
        or not attestation["control"]
    ):
        raise ValueError("isolated runtime attestation identity fields are invalid")
    artifact = {
        "profile_id": Path(config["profile_root"]).name,
        "artifact_id": attestation["profile_artifact_id"],
        "payload_sha256": payload,
    }
    ADAPTER._validate_profile_binding(artifact)
    return artifact, attestation


def _select_cases(
    suite: dict[str, Any],
    selected_ids: set[str],
    selected_categories: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    source_cases = suite["cases"]
    conversation_order: dict[str, int] = {}
    for case in source_cases:
        conversation_order.setdefault(case["conversation_id"], len(conversation_order))
    cases = sorted(
        source_cases,
        key=lambda case: (conversation_order[case["conversation_id"]], case["turn"]),
    )
    all_ids = {case["id"] for case in cases}
    unknown = selected_ids.difference(all_ids)
    if unknown:
        raise ValueError(f"unknown selected case IDs: {sorted(unknown)!r}")
    if not selected_ids and not selected_categories:
        return list(cases), []
    requested = [
        case
        for case in cases
        if case["id"] in selected_ids or case["category"] in selected_categories
    ]
    if not requested:
        raise ValueError("case selection is empty")
    required_turns = {
        (case["conversation_id"], turn)
        for case in requested
        for turn in range(1, case["turn"] + 1)
    }
    expanded = [
        case
        for case in cases
        if (case["conversation_id"], case["turn"]) in required_turns
    ]
    requested_ids = {case["id"] for case in requested}
    added = [case["id"] for case in expanded if case["id"] not in requested_ids]
    return expanded, added


def build_prompt(
    case: dict[str, Any],
    *,
    fixture_attestation_sha256: str | None = None,
    business_database_ref_sha256: str | None = None,
) -> str:
    """Build a test-bound prompt. It is hashed, executed, and never reported."""

    fixture_lines = ""
    if fixture_attestation_sha256 is not None:
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (
                fixture_attestation_sha256,
                business_database_ref_sha256,
            )
        ):
            raise ValueError("live fixture prompt binding hashes are invalid")
        fixture_lines = (
            f"fixture_attestation_sha256: {fixture_attestation_sha256}\n"
            f"business_database_ref_sha256: {business_database_ref_sha256}\n"
        )
    return (
        "[DataSage golden model replay]\n"
        f"test_id: {case['id']}\n"
        f"conversation_id: {case['conversation_id']}\n"
        f"turn: {case['turn']}\n"
        f"{fixture_lines}"
        "Treat the following as the user request. Preserve normal DataSage safety "
        "and evidence boundaries. Do not invent a result.\n"
        f"user_request: {case['prompt']}"
    )


def _live_fixture_preflight(
    suite: dict[str, Any],
    *,
    config: dict[str, Any],
    timeout: int,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Materialize selectors before the first model call via the trusted runner."""

    runner = str(Path(config["start_command"][1]).resolve(strict=False))
    executable = str(Path(config["start_command"][0]).resolve(strict=False))

    def invoke_facade(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            with _secure_exchange(
                config,
                {"tool_name": tool_name, "arguments": arguments},
            ) as exchange:
                with exchange.child_handles() as (
                    handle_arguments,
                    native_handles,
                    descriptors,
                ):
                    command = [
                        executable,
                        runner,
                        "--live-fixture-preflight",
                        *handle_arguments,
                    ]
                    completed = _run_bounded_process(
                        command,
                        timeout=timeout,
                        cwd=config["working_directory"],
                        env=environment,
                        inherited_descriptors=descriptors,
                        inherited_handles=native_handles,
                    )
                if (
                    completed.returncode != 0
                    or completed.stdout != "LIVE_FIXTURE_PREFLIGHT_OK\n"
                    or completed.stderr
                ):
                    raise LIVE_FIXTURE.LiveFixtureUnavailable(
                        "LIVE_FIXTURE_UNAVAILABLE"
                    )
                return exchange.read_response()
        except LIVE_FIXTURE.LiveFixtureUnavailable:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise LIVE_FIXTURE.LiveFixtureUnavailable(
                "LIVE_FIXTURE_UNAVAILABLE"
            ) from None

    salt = os.urandom(32)
    return LIVE_FIXTURE.materialize_suite(
        suite,
        invoke_facade=invoke_facade,
        salt=salt,
    )


def _expand_command(
    template: list[str],
    *,
    prompt: str,
    session_id: str,
    state_db: Path,
    profile_id: str,
    test_id: str,
) -> list[str]:
    values = {
        "prompt": prompt,
        "session_id": session_id,
        "state_db": str(state_db.resolve()),
        "profile_id": profile_id,
        "test_id": test_id,
        "profile_root": str(state_db.resolve().parent),
    }
    try:
        return [token.format_map(values) for token in template]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown or malformed command placeholder: {exc}") from exc


def _max_message_id(state_db: Path) -> int:
    connection = ADAPTER._open_read_only(state_db)
    try:
        row = connection.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()
        return int(row[0])
    finally:
        connection.close()


def _database_identity(state_db: Path) -> str:
    return ADAPTER._database_identity(state_db)


def _message_binding(
    state_db: Path,
    session_id: str,
    after_id: int,
    through_id: int,
    prompt_sha256: str,
) -> tuple[int, int]:
    connection = ADAPTER._open_read_only(state_db)
    try:
        rows = connection.execute(
            "SELECT id,role,content,tool_calls FROM messages "
            "WHERE session_id=? AND active=1 AND id>? AND id<=? ORDER BY id",
            (session_id, after_id, through_id),
        ).fetchall()
    finally:
        connection.close()
    users = [row for row in rows if row["role"] == "user"]
    finals = [
        row
        for row in rows
        if row["role"] == "assistant"
        and isinstance(row["content"], str)
        and row["content"]
        and not row["tool_calls"]
    ]
    if len(users) != 1 or not finals or finals[-1]["id"] <= users[0]["id"]:
        raise ValueError("persisted replay turn has no unique user/final assistant boundary")
    if _sha(users[0]["content"] if isinstance(users[0]["content"], str) else "") != prompt_sha256:
        raise ValueError("persisted replay user message does not match the canonical prompt")
    return int(users[0]["id"]), int(finals[-1]["id"])


def _compression_lineage(state_db: Path, ancestor: str, descendant: str) -> list[str]:
    if ancestor == descendant:
        return []
    connection = ADAPTER._open_read_only(state_db)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        required = {"parent_session_id", "end_reason", "model_config", "source"}
        if not required.issubset(columns):
            return []
        rows = connection.execute(
            "WITH RECURSIVE chain(id,parent_session_id,depth) AS ("
            " SELECT id,parent_session_id,0 FROM sessions WHERE id=?"
            " UNION ALL SELECT p.id,p.parent_session_id,chain.depth+1"
            " FROM chain JOIN sessions p ON p.id=chain.parent_session_id WHERE chain.depth<100"
            ") SELECT id,parent_session_id FROM chain ORDER BY depth",
            (descendant,),
        ).fetchall()
        ids = [row[0] for row in rows]
        if ancestor not in ids:
            return []
        edge_rows = connection.execute(
            "SELECT child.id,parent.id,parent.end_reason,child.model_config,child.source "
            "FROM sessions child JOIN sessions parent ON parent.id=child.parent_session_id "
            f"WHERE child.id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        edge_by_child = {row[0]: row for row in edge_rows}
        path = ids[: ids.index(ancestor) + 1]
        for child_id in path[:-1]:
            edge = edge_by_child.get(child_id)
            if edge is None or edge[2] != "compression" or edge[4] == "tool":
                return []
            try:
                model_config = json.loads(edge[3] or "{}")
            except json.JSONDecodeError:
                return []
            if model_config.get("_branched_from") is not None or model_config.get("_delegate_from") is not None:
                return []
        return list(reversed(path))
    finally:
        connection.close()


def _session_id(output: str, pattern: str) -> str:
    matches = list(re.finditer(pattern, output))
    if not matches:
        raise ValueError("Hermes output did not contain a session ID")
    values = []
    for match in matches:
        value = match.groupdict().get("session_id") or (
            match.group(1) if match.lastindex else match.group(0)
        )
        if value not in values:
            values.append(value)
    if len(values) != 1 or not values[0].strip():
        raise ValueError("Hermes output contains ambiguous session IDs")
    return values[0].strip()


def _subset_suite(suite: dict[str, Any], case_ids: set[str]) -> dict[str, Any]:
    source = [copy.deepcopy(case) for case in suite["cases"] if case["id"] in case_ids]
    conversation_order: dict[str, int] = {}
    for case in source:
        conversation_order.setdefault(case["conversation_id"], len(conversation_order))
    cases = sorted(
        source,
        key=lambda case: (conversation_order[case["conversation_id"]], case["turn"]),
    )
    conversation_turns: dict[str, int] = {}
    for case in cases:
        conversation = case["conversation_id"]
        conversation_turns[conversation] = conversation_turns.get(conversation, 0) + 1
        case["turn"] = conversation_turns[conversation]
    counts = Counter(case["category"] for case in cases)
    return {
        **{key: value for key, value in suite.items() if key not in {"cases", "minimum_case_count", "required_category_minimums"}},
        "minimum_case_count": len(cases),
        "required_category_minimums": dict(counts),
        "cases": cases,
    }


def _run_replay_locked(
    *,
    suite: dict[str, Any],
    command_config: dict[str, Any],
    state_db: Path,
    captured_at: str,
    reviews: dict[str, Any] | None = None,
    selected_ids: set[str] | None = None,
    selected_categories: set[str] | None = None,
    timeout_seconds: int | None = None,
    dry_run: bool = False,
    credential_lock: _CredentialLock,
    _internal_owner_reviewer: Callable[[Any], Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    suite_errors = SCORER.validate_suite(suite)
    if suite_errors:
        raise ValueError("invalid golden suite: " + "; ".join(suite_errors))
    config = _config(command_config)
    configured_state = Path(config["state_db"]).resolve(strict=False)
    if state_db.resolve(strict=False) != configured_state:
        raise ValueError("state_db does not match the isolated launcher contract")
    if not isinstance(captured_at, str) or not captured_at:
        raise ValueError("captured_at must be explicitly supplied")
    selected_suite = _subset_suite(
        suite,
        {
            case["id"]
            for case in _select_cases(
                suite,
                selected_ids or set(),
                selected_categories or set(),
            )[0]
        },
    )
    cases, dependency_cases = _select_cases(
        selected_suite,
        selected_ids or set(),
        selected_categories or set(),
    )
    review_map = _reviews(
        {"schema": REVIEW_SCHEMA, "reviews": reviews or {}},
        {case["id"] for case in suite["cases"]},
    )
    if _internal_owner_reviewer is not None and review_map:
        raise ValueError("internal owner reviewer cannot be combined with supplied reviews")
    timeout = timeout_seconds if timeout_seconds is not None else config["timeout_seconds"]
    if not isinstance(timeout, int) or not 1 <= timeout <= 1800:
        raise ValueError("timeout_seconds must be between 1 and 1800")
    execution_contract_sha256 = _execution_contract(config)
    artifact, attestation = _attest(
        config, timeout, execution_contract_sha256, credential_lock
    )
    runtime_guard = _runtime_guard(config)
    database_identity = _database_identity(state_db)
    fixture_binding: dict[str, Any] | None = None
    if not dry_run:
        credential_values: dict[str, str] = {}
        process_environment: dict[str, str] = {}
        try:
            credential_values = _credential_values(
                config,
                attestation["replay_isolation"]["credential_projection"],
                credential_lock,
            )
            process_environment = _isolated_environment(config, credential_values)
            materialized = _live_fixture_preflight(
                selected_suite,
                config=config,
                timeout=timeout,
                environment=process_environment,
            )
        finally:
            for key in PROJECTED_CREDENTIAL_KEYS:
                process_environment.pop(key, None)
            credential_values.clear()
        selected_suite = materialized["suite"]
        cases, _materialized_dependencies = _select_cases(
            selected_suite,
            selected_ids or set(),
            selected_categories or set(),
        )
        proposed_binding = materialized["durable_binding"]
        if proposed_binding.get("business_database_ref") is not None:
            fixture_binding = proposed_binding
            fixture_attestation_sha256 = _sha(fixture_binding)
            business_database_ref_sha256 = _sha(
                fixture_binding["business_database_ref"]
            )
        else:
            fixture_attestation_sha256 = None
            business_database_ref_sha256 = None
    else:
        fixture_attestation_sha256 = None
        business_database_ref_sha256 = None
    template_sha = _sha(
        {
            "attestation_command": config["attestation_command"],
            "start_command": config["start_command"],
            "resume_command": config["resume_command"],
            "session_id_regex": config["session_id_regex"],
            "working_directory": str(Path(config["working_directory"]).resolve()),
            "environment": config.get("environment", {}),
        }
    )
    runs: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    conversation_sessions: dict[str, str] = {}
    conversation_failed: set[str] = set()

    for case in cases:
        conversation = case["conversation_id"]
        context_action = case["expected_plan"]["context_action"]
        existing_session = conversation_sessions.get(conversation, "")
        use_resume = bool(existing_session and context_action != "reset")
        prompt = build_prompt(
            case,
            fixture_attestation_sha256=fixture_attestation_sha256,
            business_database_ref_sha256=business_database_ref_sha256,
        )
        base_row = {
            "test_id": case["id"],
            "conversation_id": conversation,
            "turn": case["turn"],
            "invocation": "resume" if use_resume else "start",
            "prompt_sha256": _sha(prompt),
            "command_template_sha256": template_sha,
            **(
                {
                    "fixture_attestation_sha256": fixture_attestation_sha256,
                    "business_database_ref_sha256": business_database_ref_sha256,
                }
                if fixture_binding is not None
                else {}
            ),
        }
        if dry_run:
            runs.append({**base_row, "status": "planned"})
            if context_action == "reset" or not existing_session:
                conversation_sessions[conversation] = f"dry-run:{conversation}:{case['turn']}"
            continue
        if conversation in conversation_failed and context_action != "reset":
            runs.append(
                {
                    **base_row,
                    "status": "blocked_by_prior_failure",
                    "error_code": "PRIOR_TURN_FAILED",
                }
            )
            continue

        if _database_identity(state_db) != database_identity:
            raise ValueError("state database identity changed during replay")
        if _runtime_guard(config) != runtime_guard:
            raise ValueError("isolated runtime identity changed during replay")
        before_id = _max_message_id(state_db)
        command = _expand_command(
            config["resume_command"] if use_resume else config["start_command"],
            prompt=prompt,
            session_id=existing_session,
            state_db=state_db,
            profile_id=artifact["profile_id"],
            test_id=case["id"],
        )
        _validate_replay_evidence(
            config, attestation, execution_contract_sha256, credential_lock
        )
        if Path(command[1]).resolve(strict=False) != Path(
            config["start_command"][1]
        ).resolve(strict=False):
            raise ValueError("replay command runner changed after attestation")
        started = time.perf_counter()
        credential_values: dict[str, str] = {}
        process_environment: dict[str, str] = {}
        try:
            credential_values = _credential_values(
                config,
                attestation["replay_isolation"]["credential_projection"],
                credential_lock,
            )
            process_environment = _isolated_environment(config, credential_values)
            completed = _run_bounded_process(
                command,
                timeout=timeout,
                cwd=config["working_directory"],
                env=process_environment,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            runs.append(
                {
                    **base_row,
                    "status": "timeout",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "stdout_sha256": _sha(stdout),
                    "stderr_sha256": _sha(stderr),
                    "error_code": "HERMES_TIMEOUT",
                }
            )
            conversation_failed.add(conversation)
            continue
        finally:
            for key in PROJECTED_CREDENTIAL_KEYS:
                process_environment.pop(key, None)
            credential_values.clear()
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        execution_row = {
            **base_row,
            "exit_code": completed.returncode,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "stdout_sha256": _sha(stdout),
            "stderr_sha256": _sha(stderr),
        }
        if completed.returncode != 0:
            runs.append(
                {
                    **execution_row,
                    "status": "failed",
                    "error_code": "HERMES_NONZERO_EXIT",
                }
            )
            conversation_failed.add(conversation)
            continue
        try:
            session_id = _session_id(stdout + "\n" + stderr, config["session_id_regex"])
            session_lineage: list[str] = []
            if use_resume and session_id != existing_session:
                session_lineage = _compression_lineage(
                    state_db, existing_session, session_id
                )
                if not session_lineage:
                    raise ValueError(
                        "resume invocation changed session without a proven compression lineage"
                    )
            after_id = _max_message_id(state_db)
            user_message_id, final_message_id = _message_binding(
                state_db,
                session_id,
                before_id,
                after_id,
                base_row["prompt_sha256"],
            )
        except ValueError as exc:
            runs.append(
                {
                    **execution_row,
                    "status": "failed",
                    "error_code": "TRANSCRIPT_BINDING_FAILED",
                    "error_detail_sha256": _sha(str(exc)),
                }
            )
            conversation_failed.add(conversation)
            continue
        conversation_sessions[conversation] = session_id
        conversation_failed.discard(conversation)
        binding = {
            "test_id": case["id"],
            "conversation_id": conversation,
            "turn": case["turn"],
            "session_id": session_id,
            "user_message_id": user_message_id,
            "final_message_id": final_message_id,
            "canonical_prompt_sha256": base_row["prompt_sha256"],
            "watermark_sha256": ADAPTER._watermark(
                test_id=case["id"],
                conversation_id=conversation,
                turn=case["turn"],
                canonical_prompt_sha256=base_row["prompt_sha256"],
                user_message_id=user_message_id,
                database_identity_sha256=database_identity,
                profile=artifact,
                fixture_attestation_sha256=fixture_attestation_sha256,
                business_database_ref_sha256=business_database_ref_sha256,
            ),
            **(
                {
                    "fixture_attestation_sha256": fixture_attestation_sha256,
                    "business_database_ref_sha256": business_database_ref_sha256,
                }
                if fixture_binding is not None
                else {}
            ),
        }
        if session_lineage:
            binding["session_lineage"] = session_lineage
        if case["id"] in review_map:
            binding["review"] = review_map[case["id"]]
        bindings.append(binding)
        runs.append(
            {
                **execution_row,
                "status": "completed",
                "session_id": session_id,
                "user_message_id": user_message_id,
                "final_message_id": final_message_id,
            }
        )

    # Scoring/review assembly never needs provider or database credentials.
    # Release the retained source handle as soon as the last execution ends.
    credential_lock.close()

    if dry_run:
        report = {
            "schema": REPORT_SCHEMA,
            "mode": "dry-run",
            "status": "dry_run",
            "profile_artifact": artifact,
            "runtime_attestation_sha256": _sha(attestation),
            "state_db_identity_sha256": database_identity,
            "selected_case_ids": [case["id"] for case in cases],
            "selection_dependencies_added": dependency_cases,
            "runs": runs,
            "summary": {
                "total": len(runs),
                "planned": len(runs),
                "completed": 0,
                "execution_failed": 0,
                "scored_failed": 0,
            },
            "scorer": None,
        }
        return report, None

    candidate: dict[str, Any] | None = None
    scorer_report: dict[str, Any] | None = None
    scorer_error_code: str | None = None
    capture_bindings: dict[str, Any] | None = None
    reviewer_status = (
        "internal_test_reviewer_pending"
        if _internal_owner_reviewer is not None
        else "not_configured"
    )
    if bindings:
        adapter_bindings = {
            "schema": (
                ADAPTER.LIVE_BINDING_SCHEMA
                if fixture_binding is not None
                else ADAPTER.BINDING_SCHEMA
            ),
            "captured_at": captured_at,
            "transcript_source": "datasage-trusted-replay",
            "profile_artifact": artifact,
            "state_db_identity_sha256": database_identity,
            "turns": bindings,
        }
        capture_bindings = {
            **adapter_bindings,
            "turns": [
                {key: value for key, value in binding.items() if key != "review"}
                for binding in bindings
            ],
        }
        completed_ids = {binding["test_id"] for binding in bindings}
        if _internal_owner_reviewer is not None:
            state_snapshot_sha256 = _review_state_snapshot(state_db)
            try:
                with _immutable_review_snapshot(config, state_db) as (
                    snapshot_path,
                    snapshot_connection,
                    validate_snapshot,
                ):
                    candidate = ADAPTER.adapt(
                        snapshot_path,
                        capture_bindings,
                        _connection=snapshot_connection,
                        _database_identity_override=database_identity,
                    )
                    capture_projection = _capture_projection(candidate)
                    reviewer_snapshot = _deep_freeze(
                        {
                            "schema": "datasage-internal-review-snapshot/v1",
                            "capture_bindings": copy.deepcopy(capture_bindings),
                            "candidate": copy.deepcopy(candidate),
                            "review_contracts": _review_contracts(
                                selected_suite, completed_ids
                            ),
                        }
                    )
                    review_document = _internal_owner_reviewer(reviewer_snapshot)
                    if (
                        not isinstance(review_document, dict)
                        or review_document.get("schema") != REVIEW_SCHEMA
                        or set(review_document) != {"schema", "reviews"}
                        or not isinstance(review_document.get("reviews"), dict)
                        or set(review_document["reviews"]) != completed_ids
                        or any(
                            not isinstance(assertion, dict)
                            for assertion in review_document["reviews"].values()
                        )
                    ):
                        raise ValueError("internal reviewer returned an invalid assertion set")
                    validate_snapshot()
                    post_capture = ADAPTER.adapt(
                        snapshot_path,
                        capture_bindings,
                        _connection=snapshot_connection,
                        _database_identity_override=database_identity,
                    )
                    if (
                        _review_state_snapshot(state_db) != state_snapshot_sha256
                        or _capture_projection(post_capture) != capture_projection
                    ):
                        reviewer_status = "capture_only_transcript_drift"
                    else:
                        reviewed_bindings = {
                            **capture_bindings,
                            "turns": [
                                {
                                    **turn,
                                    "review": review_document["reviews"][turn["test_id"]],
                                }
                                for turn in capture_bindings["turns"]
                            ],
                        }
                        reviewed_candidate = ADAPTER.adapt(
                            snapshot_path,
                            reviewed_bindings,
                            _connection=snapshot_connection,
                            _database_identity_override=database_identity,
                        )
                        validate_snapshot()
                        if _review_state_snapshot(state_db) != state_snapshot_sha256:
                            candidate = post_capture
                            reviewer_status = "capture_only_transcript_drift"
                        elif _capture_projection(reviewed_candidate) != capture_projection:
                            raise ValueError("reviewed candidate changed captured evidence")
                        else:
                            candidate = reviewed_candidate
                            reviewer_status = "reviewed"
            except Exception:
                if reviewer_status != "capture_only_transcript_drift":
                    reviewer_status = "capture_only_reviewer_failed"
        else:
            candidate = ADAPTER.adapt(state_db, adapter_bindings)
        try:
            scorer_report = SCORER.score(
                _subset_suite(selected_suite, completed_ids),
                candidate,
            )
        except ValueError:
            scorer_error_code = "CANDIDATE_REVIEW_OR_RECEIPT_INVALID"
    execution_failed = sum(row["status"] != "completed" for row in runs)
    scored_failed = (
        scorer_report["summary"]["failed"]
        if scorer_report
        else len(bindings)
        if scorer_error_code
        else 0
    )
    status = (
        "passed"
        if execution_failed == 0 and scored_failed == 0
        else "partial"
        if bindings
        else "failed"
    )
    report = {
        "schema": REPORT_SCHEMA,
        "mode": "hermes-cli-replay",
        "status": status,
        "profile_artifact": artifact,
        "runtime_attestation_sha256": _sha(attestation),
        "state_db_identity_sha256": database_identity,
        "selected_case_ids": [case["id"] for case in cases],
        "selection_dependencies_added": dependency_cases,
        "runs": runs,
        "summary": {
            "total": len(runs),
            "planned": 0,
            "completed": len(bindings),
            "execution_failed": execution_failed,
            "scored_failed": scored_failed,
        },
        "candidate_sha256": _sha(candidate) if candidate else None,
        "canary_receipt_sha256": (
            candidate["canary_receipt"]["receipt_sha256"] if candidate else None
        ),
        "scorer": scorer_report,
        "scorer_error_code": scorer_error_code,
        "capture_bindings": capture_bindings,
        "fixture_binding": fixture_binding,
        "reviewer_status": reviewer_status,
    }
    return report, candidate


def run_replay(
    *,
    suite: dict[str, Any],
    command_config: dict[str, Any],
    state_db: Path,
    captured_at: str,
    reviews: dict[str, Any] | None = None,
    selected_ids: set[str] | None = None,
    selected_categories: set[str] | None = None,
    timeout_seconds: int | None = None,
    dry_run: bool = False,
    _internal_owner_reviewer: Callable[[Any], Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    config = _config(command_config)
    # The lock is acquired before the launcher attests the source and remains
    # held until every child process and all transcript work have completed.
    with _CredentialLock(_credential_source(config)) as credential_lock:
        return _run_replay_locked(
            suite=suite,
            command_config=command_config,
            state_db=state_db,
            captured_at=captured_at,
            reviews=reviews,
            selected_ids=selected_ids,
            selected_categories=selected_categories,
            timeout_seconds=timeout_seconds,
            dry_run=dry_run,
            credential_lock=credential_lock,
            _internal_owner_reviewer=_internal_owner_reviewer,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=HERE / "golden_expert_cases.json")
    parser.add_argument("--command-config", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bundle-output", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--report-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.candidate_output is not None or args.report_output is not None:
        parser.error("separate candidate/report outputs are unsafe; use --bundle-output")
    reviews_doc = _load(args.reviews) if args.reviews else None
    reviews = _reviews(
        reviews_doc,
        {case["id"] for case in _load(args.cases)["cases"]},
    ) if reviews_doc is not None else {}
    report, candidate = run_replay(
        suite=_load(args.cases),
        command_config=_load(args.command_config),
        state_db=args.state_db,
        captured_at=args.captured_at,
        reviews=reviews,
        selected_ids=set(args.case),
        selected_categories=set(args.category),
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
    )
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "report": report,
        "candidate": candidate,
    }
    bundle_stage = _stage_json(args.bundle_output, bundle)
    try:
        bundle_stage.replace(args.bundle_output)
    finally:
        bundle_stage.unlink(missing_ok=True)
    return 0 if report["status"] in {"passed", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
