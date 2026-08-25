"""Build a deterministic identity receipt for distribution-owned content.

The receipt deliberately excludes runtime state and secrets.  It proves which
reviewed files make up a candidate or installed instance.  Hermes-owned
installation metadata is normalized; the receipt is not a deployment
instruction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable

import yaml


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "distribution.yaml"
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
INSTALLER_MANIFEST_FIELDS = {"name", "source", "installed_at"}


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
        "identity_scope": "distribution-owned-content/v1",
        "excludes_runtime_state": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(build_receipt(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
