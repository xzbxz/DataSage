#!/usr/bin/env python3
"""Regenerate .release/RELEASE.json for a governed Hermes profile.

Run after ANY change to hermes-agent source files or to profile files listed in
distribution_owned (SOUL.md, config.yaml, ...). Failure to regenerate leaves the
identity gate closed: all datasage_query calls return HERMES_IDENTITY_UNVERIFIED.

Prereqs:
  - hermes-agent working tree is CLEAN (commit/stash changes first)
  - a git tag points at HEAD (create one first if not)
  - a copy of the distribution_owned files exists in the profile root

Usage:
  python gen-release-json.py --hermes C:/.../hermes-agent \
      --profile C:/.../profiles/<name> [--version 0.12.0-dev1]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import importlib.util
import yaml


def _load_payload_manifest(profile: str):
    module_path = (
        Path(profile) / "plugins" / "datasage-query" / "payload_manifest.py"
    )
    spec = importlib.util.spec_from_file_location(
        "datasage_payload_manifest", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load payload manifest implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(hermes: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", hermes, *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes", required=True, help="hermes-agent checkout dir")
    ap.add_argument("--profile", required=True, help="profile root dir")
    ap.add_argument("--version", default="0.12.0-dev1")
    args = ap.parse_args()

    # Fail fast: dirty checkout would just recreate CHECKOUT_DIRTY.
    dirty = git(args.hermes, "status", "--porcelain", "--", ".")
    if dirty.strip():
        print("ERROR: hermes-agent checkout is dirty. Commit or stash first.")
        print(dirty.strip()[:500])
        return 1

    commit = git(args.hermes, "rev-parse", "HEAD")
    tree_oid = git(args.hermes, "rev-parse", "HEAD^{tree}")
    tags = git(args.hermes, "tag", "--points-at", "HEAD").splitlines()
    if not tags:
        print("ERROR: no git tag points at HEAD. Create one first.")
        return 1
    tag = tags[0]

    uv = subprocess.run(
        ["git", "-C", args.hermes, "show", f"{commit}:uv.lock"],
        capture_output=True, check=True,
    ).stdout
    if not uv:
        print("ERROR: git show HEAD:uv.lock returned empty.")
        return 1

    payload_manifest = _load_payload_manifest(args.profile)
    payload = payload_manifest.build_payload_manifest(Path(args.profile))
    distribution = yaml.safe_load(
        (Path(args.profile) / "distribution.yaml").read_text(encoding="utf-8")
    )
    distribution_name = str(distribution["name"])
    hermes_requires = str(distribution["hermes_requires"])
    artifact_id = (
        f"{distribution_name}-{args.version}-{payload['payload_sha256'][:16]}"
    )

    release = {
        "release_version": args.version,
        "distribution_version": args.version,
        "artifact_id": artifact_id,
        "distribution_name": distribution_name,
        "source_commit": commit,
        "source_tag": tag,
        "hermes_requires": hermes_requires,
        **payload,
        "hermes_source": {
            "commit": commit,
            "tag": tag,
            "tree_oid": tree_oid,
            "uv_lock_sha256": hashlib.sha256(uv).hexdigest(),
            "dirty": False,
        },
        "created_at": "2026-08-07T00:00:00Z",
    }

    out_dir = os.path.join(args.profile, ".release")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "RELEASE.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(release, f, ensure_ascii=False, indent=2)
        f.write("\n")
    manifest_path = os.path.join(out_dir, "MANIFEST.sha256")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        for entry in payload["payload_files"]:
            f.write(f"{entry['sha256']}  {entry['path']}\n")

    print("RELEASE.json written:", out_path)
    print("commit:", commit)
    print("tag:", tag)
    print("tree_oid:", tree_oid)
    print("payload_sha256:", release["payload_sha256"])
    print("\nNext: hermes gateway restart, then check the startup log for")
    print("ready=True / identity_state=installed / expected==actual.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
