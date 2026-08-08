"""Build one atomic, checksummed Hermes + DataSage runtime release unit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40,64}")
PROFILE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PROFILE_IDENTITY_FIELDS = (
    "artifact_id",
    "distribution_name",
    "distribution_version",
    "payload_sha256",
)
LAUNCHER_PYTHON_SENTINEL = "@launcher-python"
OFFLINE_HEALTH_PROBE_RELATIVE = "evaluation/runtime_offline_health.py"
OFFLINE_HEALTH_EXECUTOR_ID = "datasage-atomic-offline-health/v1"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_offline_health_contract(
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("offline health contract is invalid")
    if value.get("mode") == "fixture_injected":
        if (
            set(value) != {"version", "network", "mode", "executor_id"}
            or value.get("version") != "offline-health/v1"
            or value.get("network") != "forbidden"
            or not isinstance(value.get("executor_id"), str)
            or not value["executor_id"]
        ):
            raise RuntimeError("fixture offline health contract is invalid")
        return dict(value)
    if (
        set(value)
        != {
            "version",
            "network",
            "mode",
            "executor_id",
            "argv",
            "cwd",
            "timeout_seconds",
        }
        or value.get("version") != "offline-health/v1"
        or value.get("network") != "forbidden"
        or value.get("mode") != "unit_command"
        or value.get("executor_id") != OFFLINE_HEALTH_EXECUTOR_ID
        or value.get("argv")
        != [
            LAUNCHER_PYTHON_SENTINEL,
            "health/datasage_offline_health.py",
        ]
        or value.get("cwd") != "."
        or not isinstance(value.get("timeout_seconds"), int)
        or isinstance(value.get("timeout_seconds"), bool)
        or not 1 <= value["timeout_seconds"] <= 60
    ):
        raise RuntimeError("release offline health contract is invalid")
    return dict(value)


def _git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    command = [
        "git",
        "-c",
        f"safe.directory={repo.as_posix()}",
        "-C",
        str(repo),
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=not binary,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"unable to read Git repository: {repo}") from exc
    return completed.stdout


def _resolve_hermes_identity(
    repo: Path,
    requested_commit: str,
    requested_tag: str,
) -> tuple[str, str, str]:
    if not repo.is_dir():
        raise RuntimeError("Hermes repository does not exist")
    if not COMMIT_PATTERN.fullmatch(requested_commit):
        raise RuntimeError("Hermes commit identity is invalid")
    if not requested_tag or re.search(r"[\x00-\x20\x7f]", requested_tag):
        raise RuntimeError("Hermes tag identity is invalid")

    status = str(_git(repo, "status", "--porcelain", "--", ".")).strip()
    if status:
        raise RuntimeError("Hermes repository must be clean")

    resolved_commit = str(
        _git(repo, "rev-parse", "--verify", f"{requested_commit}^{{commit}}")
    ).strip().lower()
    resolved_tag_commit = str(
        _git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/tags/{requested_tag}^{{commit}}",
        )
    ).strip().lower()
    if not COMMIT_PATTERN.fullmatch(resolved_commit):
        raise RuntimeError("resolved Hermes commit identity is invalid")
    if resolved_commit != requested_commit.lower():
        raise RuntimeError("Hermes commit must be specified in full")
    if resolved_tag_commit != resolved_commit:
        raise RuntimeError("Hermes commit and tag do not match")
    tree_oid = str(
        _git(repo, "rev-parse", "--verify", f"{resolved_commit}^{{tree}}")
    ).strip().lower()
    if not COMMIT_PATTERN.fullmatch(tree_oid):
        raise RuntimeError("resolved Hermes tree identity is invalid")
    return resolved_commit, requested_tag, tree_oid


def _safe_relative(raw: str) -> Path:
    if not raw or "\\" in raw:
        raise RuntimeError(f"unsafe release path: {raw}")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe release path: {raw}")
    return relative


def _git_commit_blobs(
    repo: Path,
    commit: str,
) -> list[tuple[str, str, bytes]]:
    """Read exact committed blobs without checkout or archive conversion filters."""
    listing = _git(
        repo,
        "ls-tree",
        "-rz",
        "--full-tree",
        commit,
        binary=True,
    )
    if not isinstance(listing, bytes):
        raise RuntimeError("unable to enumerate exact Hermes commit")
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for raw_record in listing.split(b"\0"):
        if not raw_record:
            continue
        try:
            header, raw_path = raw_record.split(b"\t", 1)
            raw_mode, raw_kind, raw_oid = header.split(b" ", 2)
            relative = raw_path.decode("utf-8")
            mode = raw_mode.decode("ascii")
            oid = raw_oid.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Hermes Git tree entry is invalid") from exc
        if raw_kind != b"blob" or mode not in {"100644", "100755", "120000"}:
            raise RuntimeError(
                f"Hermes Git tree entry type is unsupported: {relative}"
            )
        safe_relative = _safe_relative(relative).as_posix()
        if safe_relative != relative or relative in seen:
            raise RuntimeError(f"Hermes Git tree path is unsafe: {relative}")
        seen.add(relative)
        entries.append((relative, mode, oid))
    if not entries:
        raise RuntimeError("Hermes Git tree contains no files")

    command = [
        "git",
        "-c",
        f"safe.directory={repo.as_posix()}",
        "-C",
        str(repo),
        "cat-file",
        "--batch",
    ]
    try:
        output = subprocess.run(
            command,
            input=b"".join(
                oid.encode("ascii") + b"\n"
                for _relative, _mode, oid in entries
            ),
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("unable to read exact Hermes Git blobs") from exc
    records: list[tuple[str, str, bytes]] = []
    offset = 0
    for relative, mode, oid in entries:
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            raise RuntimeError("Hermes Git batch output is truncated")
        header = output[offset:header_end].split(b" ")
        if (
            len(header) != 3
            or header[0].decode("ascii", errors="ignore") != oid
            or header[1] != b"blob"
        ):
            raise RuntimeError("Hermes Git batch object identity is invalid")
        try:
            size = int(header[2])
        except ValueError as exc:
            raise RuntimeError("Hermes Git batch object size is invalid") from exc
        payload_start = header_end + 1
        payload_end = payload_start + size
        if payload_end >= len(output) or output[payload_end:payload_end + 1] != b"\n":
            raise RuntimeError("Hermes Git batch object payload is truncated")
        records.append((relative, mode, output[payload_start:payload_end]))
        offset = payload_end + 1
    if offset != len(output):
        raise RuntimeError("Hermes Git batch output has trailing data")
    return records


def _export_git_commit(repo: Path, commit: str, archive: Path) -> None:
    """Zip exact Git blobs, bypassing platform EOL and export filters."""
    records = _git_commit_blobs(repo, commit)
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as package:
        for relative, mode, payload in records:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            unix_mode = {
                "100644": stat.S_IFREG | 0o644,
                "100755": stat.S_IFREG | 0o755,
                "120000": stat.S_IFLNK | 0o777,
            }[mode]
            info.external_attr = unix_mode << 16
            package.writestr(info, payload)

    with zipfile.ZipFile(archive) as package:
        actual = {
            info.filename: (
                (
                    "120000"
                    if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)
                    else (
                        "100755"
                        if ((info.external_attr >> 16) & 0xFFFF) & 0o111
                        else "100644"
                    )
                ),
                package.read(info),
            )
            for info in package.infolist()
            if not info.is_dir()
        }
    expected = {
        relative: (mode, payload)
        for relative, mode, payload in records
    }
    if actual != expected:
        raise RuntimeError("Hermes commit archive changed exact Git blobs")


def _git_file_sha256(repo: Path, commit: str, relative: str) -> str:
    try:
        payload = _git(repo, "show", f"{commit}:{relative}", binary=True)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Hermes commit is missing required identity file: {relative}"
        ) from exc
    if not isinstance(payload, bytes):
        raise RuntimeError("unable to hash Hermes identity file")
    return hashlib.sha256(payload).hexdigest()


def _resolve_control_plane_identity(
    repo: Path,
    requested_commit: str,
    requested_tag: str,
) -> tuple[dict[str, str], bytes]:
    commit, tag, tree_oid = _resolve_hermes_identity(
        repo,
        requested_commit,
        requested_tag,
    )
    payload = _git(
        repo,
        "show",
        f"{commit}:{OFFLINE_HEALTH_PROBE_RELATIVE}",
        binary=True,
    )
    if not isinstance(payload, bytes):
        raise RuntimeError("control-plane probe cannot be exported")
    blob_oid = str(
        _git(
            repo,
            "rev-parse",
            "--verify",
            f"{commit}:{OFFLINE_HEALTH_PROBE_RELATIVE}",
        )
    ).strip().lower()
    if not COMMIT_PATTERN.fullmatch(blob_oid):
        raise RuntimeError("control-plane probe blob identity is invalid")
    return (
        {
            "commit": commit,
            "tag": tag,
            "tree_oid": tree_oid,
            "probe_blob_oid": blob_oid,
            "probe_sha256": hashlib.sha256(payload).hexdigest(),
            "executor_id": OFFLINE_HEALTH_EXECUTOR_ID,
        },
        payload,
    )


def _read_profile_identity(
    profile: Path,
    *,
    require_directory_identity: bool = True,
) -> tuple[dict, list[tuple[str, str]]]:
    release_path = profile / ".release" / "RELEASE.json"
    manifest_path = profile / ".release" / "MANIFEST.sha256"
    distribution_path = profile / "distribution.yaml"
    if not all(path.is_file() for path in (release_path, manifest_path, distribution_path)):
        raise RuntimeError("Profile is not a built release artifact")

    release = json.loads(release_path.read_text(encoding="utf-8"))
    distribution = yaml.safe_load(distribution_path.read_text(encoding="utf-8"))
    if not isinstance(release, dict) or not isinstance(distribution, dict):
        raise RuntimeError("Profile release metadata is invalid")
    if not all(release.get(field) for field in PROFILE_IDENTITY_FIELDS):
        raise RuntimeError("Profile release identity is incomplete")
    if not release.get("hermes_requires"):
        raise RuntimeError("Profile Hermes compatibility identity is incomplete")
    if release["distribution_name"] != distribution.get("name"):
        raise RuntimeError("Profile RELEASE distribution name does not match")
    if release["distribution_version"] != distribution.get("version"):
        raise RuntimeError("Profile RELEASE distribution version does not match")
    expected_artifact_id = (
        f"{release['distribution_name']}-{release['distribution_version']}-"
        f"{str(release['payload_sha256'])[:16]}"
    )
    if release["artifact_id"] != expected_artifact_id:
        raise RuntimeError("Profile RELEASE artifact identity does not match")
    if require_directory_identity and profile.name != release["artifact_id"]:
        raise RuntimeError("Profile artifact directory identity does not match RELEASE")

    records: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise RuntimeError("Profile SHA256 manifest is invalid")
        digest, raw_relative = match.groups()
        relative = _safe_relative(raw_relative)
        if raw_relative in seen:
            raise RuntimeError("Profile SHA256 manifest contains duplicates")
        seen.add(raw_relative)
        target = profile / relative
        if not target.is_file() or _hash_file(target) != digest:
            raise RuntimeError(f"Profile artifact hash mismatch: {raw_relative}")
        records.append((relative.as_posix(), digest))

    aggregate = hashlib.sha256()
    for relative, digest in records:
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
    if aggregate.hexdigest() != release["payload_sha256"]:
        raise RuntimeError("Profile payload identity does not match its manifest")
    allowed = {
        *(relative for relative, _ in records),
        ".release/MANIFEST.sha256",
        ".release/RELEASE.json",
    }
    actual = {
        path.relative_to(profile).as_posix()
        for path in profile.rglob("*")
        if path.is_file()
    }
    undeclared = sorted(actual - allowed)
    if undeclared:
        raise RuntimeError(
            "Profile artifact contains undeclared files: "
            + ", ".join(undeclared)
        )
    return release, records


def _bind_profile_to_hermes(profile_release: dict, hermes_commit: str) -> None:
    recorded = profile_release.get("hermes_source")
    if not isinstance(recorded, dict):
        raise RuntimeError("Profile RELEASE has no Hermes source identity")
    if recorded.get("commit") != hermes_commit:
        raise RuntimeError("Profile RELEASE Hermes commit does not match")
    if recorded.get("dirty") is not False:
        raise RuntimeError("Profile RELEASE is bound to a dirty Hermes worktree")


def _tree_records(root: Path) -> list[tuple[str, str]]:
    return [
        (path.relative_to(root).as_posix(), _hash_file(path))
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        )
    ]


def _aggregate_records(records: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for relative, file_digest in records:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build(
    output_root: Path,
    profile_artifact: Path,
    hermes_repo: Path,
    hermes_commit: str,
    hermes_tag: str,
    offline_health: dict[str, Any] | None = None,
    runtime_profile_name: str = "datasage-canary-next",
    control_source_repo: Path | None = None,
    control_source_commit: str | None = None,
    control_source_tag: str | None = None,
) -> Path:
    offline_health = _validated_offline_health_contract(offline_health)
    profile_artifact = profile_artifact.resolve()
    hermes_repo = hermes_repo.resolve()
    if (
        not PROFILE_NAME_PATTERN.fullmatch(runtime_profile_name)
        or runtime_profile_name in {".", ".."}
    ):
        raise RuntimeError("runtime Profile name is invalid")
    resolved_commit, resolved_tag, resolved_tree = _resolve_hermes_identity(
        hermes_repo,
        hermes_commit,
        hermes_tag,
    )
    uv_lock_sha256 = _git_file_sha256(
        hermes_repo,
        resolved_commit,
        "uv.lock",
    )
    profile_release, _ = _read_profile_identity(profile_artifact)
    _bind_profile_to_hermes(profile_release, resolved_commit)
    control_identity: dict[str, str] | None = None
    control_probe: bytes | None = None
    if (
        offline_health is not None
        and offline_health.get("executor_id")
        == OFFLINE_HEALTH_EXECUTOR_ID
    ):
        if (
            control_source_repo is None
            or control_source_commit is None
            or control_source_tag is None
        ):
            raise RuntimeError(
                "release health requires an exact-tag control-plane source"
            )
        control_identity, control_probe = _resolve_control_plane_identity(
            control_source_repo.resolve(),
            control_source_commit,
            control_source_tag,
        )

    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix="datasage-atomic-runtime-stage-", dir=output_root)
    )
    try:
        hermes_target = stage / "hermes"
        profile_target = stage / "profile"
        hermes_target.mkdir()
        _export_git_commit(
            hermes_repo,
            resolved_commit,
            hermes_target / "hermes-source.zip",
        )
        shutil.copytree(profile_artifact, profile_target)
        if (
            offline_health is not None
            and offline_health.get("mode") == "unit_command"
            and offline_health.get("argv", [None])[0]
            == LAUNCHER_PYTHON_SENTINEL
        ):
            if control_probe is None or control_identity is None:
                raise RuntimeError("launcher offline health probe is unavailable")
            argv = offline_health.get("argv")
            if (
                not isinstance(argv, list)
                or len(argv) < 2
                or argv[1] != "health/datasage_offline_health.py"
            ):
                raise RuntimeError("launcher offline health probe path is invalid")
            health_target = stage / "health"
            health_target.mkdir()
            (health_target / "datasage_offline_health.py").write_bytes(
                control_probe
            )

        payload_records = _tree_records(stage)
        payload_sha256 = _aggregate_records(payload_records)
        unit_id = (
            f"datasage-runtime-{profile_release['distribution_version']}-"
            f"{payload_sha256[:16]}"
        )
        unit = output_root / unit_id
        if unit.exists():
            raise RuntimeError(f"immutable runtime release already exists: {unit}")

        release_dir = stage / ".release"
        release_dir.mkdir()
        metadata = {
            "release_unit_id": unit_id,
            "payload_sha256": payload_sha256,
            "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "profile": {
                **{
                    field: profile_release[field]
                    for field in PROFILE_IDENTITY_FIELDS
                },
                "manifest_sha256": _hash_file(
                    profile_artifact / ".release" / "MANIFEST.sha256"
                ),
                "release_metadata_sha256": _hash_file(
                    profile_artifact / ".release" / "RELEASE.json"
                ),
                "runtime_profile_name": runtime_profile_name,
            },
            "hermes": {
                "commit": resolved_commit,
                "tag": resolved_tag,
                "tree_oid": resolved_tree,
                "uv_lock_sha256": uv_lock_sha256,
                "source": "exact_git_tree_archive",
            },
            "compatibility": {
                "hermes_requires": profile_release.get("hermes_requires"),
            },
            **(
                {
                    "control_plane": {
                        **control_identity,
                        "profile_source_commit": profile_release.get(
                            "source_commit"
                        ),
                        "profile_source_tag": profile_release.get(
                            "source_tag"
                        ),
                    }
                }
                if (
                    offline_health is not None
                    and offline_health.get("executor_id")
                    == OFFLINE_HEALTH_EXECUTOR_ID
                )
                else {}
            ),
            **(
                {"runtime": {"offline_health": offline_health}}
                if offline_health is not None
                else {}
            ),
        }
        metadata_path = release_dir / "RELEASE.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        final_records = _tree_records(stage)
        (release_dir / "MANIFEST.sha256").write_text(
            "".join(
                f"{digest}  {relative}\n"
                for relative, digest in final_records
            ),
            encoding="utf-8",
            newline="\n",
        )
        stage.rename(unit)
        return unit
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--profile-artifact", required=True, type=Path)
    parser.add_argument("--hermes-repo", required=True, type=Path)
    parser.add_argument("--hermes-commit", required=True)
    parser.add_argument("--hermes-tag", required=True)
    parser.add_argument("--offline-health-contract", type=Path)
    parser.add_argument(
        "--runtime-profile-name",
        default="datasage-canary-next",
    )
    parser.add_argument("--control-source-repo", type=Path)
    parser.add_argument("--control-source-commit")
    parser.add_argument("--control-source-tag")
    args = parser.parse_args()
    offline_health = (
        json.loads(
            args.offline_health_contract.read_text(encoding="utf-8")
        )
        if args.offline_health_contract is not None
        else None
    )
    print(
        build(
            args.output_root,
            args.profile_artifact,
            args.hermes_repo,
            args.hermes_commit,
            args.hermes_tag,
            offline_health,
            args.runtime_profile_name,
            args.control_source_repo,
            args.control_source_commit,
            args.control_source_tag,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
