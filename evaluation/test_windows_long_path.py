from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from evaluation import current_runtime_launcher as launcher
from evaluation import build_atomic_runtime_release as builder


@unittest.skipUnless(os.name == "nt", "Windows extended paths only")
class WindowsLongPathStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dsrt-long-path-"))

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(
                launcher._extended_path_within(self.root, self.root),
                ignore_errors=True,
            )

    def _deep_file(self, root: Path) -> Path:
        path = root
        for index in range(12):
            path /= f"schema-segment-{index:02d}-abcdefgh"
        return path / "shared-documentPropertiesVariantTypes.xsd"

    def test_stage_copy_succeeds_for_path_over_260_characters(self) -> None:
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        deep = self._deep_file(source)
        os.makedirs(
            launcher._extended_path_within(deep.parent, source),
            exist_ok=True,
        )
        with open(
            launcher._extended_path_within(deep, source),
            "wb",
        ) as handle:
            handle.write(b"schema")
        self.assertGreater(len(str(deep)), 260)
        launcher._copytree_within_roots(
            source,
            target,
            source_root=source,
            target_root=self.root,
        )
        copied = self._deep_file(target)
        with open(
            launcher._extended_path_within(copied, target),
            "rb",
        ) as handle:
            self.assertEqual(handle.read(), b"schema")

    def test_extended_path_rejects_escape(self) -> None:
        allowed = self.root / "allowed"
        allowed.mkdir()
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            launcher._extended_path_within(
                allowed / ".." / "outside" / "payload",
                allowed,
            )

    def test_failed_stage_cleanup_removes_deep_tree(self) -> None:
        staging = self.root / "staging"
        staging.mkdir()
        operation = staging / "operation"
        deep = self._deep_file(operation)
        os.makedirs(
            launcher._extended_path_within(deep.parent, staging),
            exist_ok=True,
        )
        with open(
            launcher._extended_path_within(deep, staging),
            "wb",
        ) as handle:
            handle.write(b"partial")
        launcher._remove_tree_within_root(operation, staging)
        self.assertFalse(operation.exists())


class CanonicalUnitIdentityTests(unittest.TestCase):
    def test_each_control_identity_field_changes_unit_identity(self) -> None:
        release = {
            "payload_sha256": "1" * 64,
            "profile": {
                "artifact_id": "profile",
                "distribution_name": "datasage-canary-next",
                "distribution_version": "0.12.0-dev1",
                "payload_sha256": "2" * 64,
                "manifest_sha256": "3" * 64,
                "release_metadata_sha256": "4" * 64,
                "runtime_profile_name": "datasage-canary-next",
            },
            "hermes": {
                "commit": "5" * 40,
                "tag": "hermes-tag",
                "tree_oid": "6" * 40,
                "uv_lock_sha256": "7" * 64,
                "source": "exact_git_tree_archive",
            },
            "compatibility": {"hermes_requires": "==0.19.0"},
            "control_plane": {
                "commit": "8" * 40,
                "tag": "control-tag",
                "tree_oid": "9" * 40,
                "probe_blob_oid": "a" * 40,
                "executor_id": "datasage-atomic-offline-health/v1",
                "probe_sha256": "b" * 64,
                "profile_source_commit": "c" * 40,
                "profile_source_tag": "profile-tag",
            },
            "runtime": {"offline_health": {"version": "offline-health/v1"}},
        }
        baseline = builder._unit_identity_sha256(release)
        alternatives = {
            "commit": "d" * 40,
            "tag": "control-tag-2",
            "tree_oid": "e" * 40,
            "probe_blob_oid": "f" * 40,
            "executor_id": "datasage-atomic-offline-health/v2",
            "probe_sha256": "0" * 64,
            "profile_source_commit": "1" * 40,
            "profile_source_tag": "profile-tag-2",
        }
        for field, replacement in alternatives.items():
            changed = __import__("copy").deepcopy(release)
            changed["control_plane"][field] = replacement
            with self.subTest(field=field):
                self.assertNotEqual(
                    baseline, builder._unit_identity_sha256(changed)
                )


if __name__ == "__main__":
    unittest.main()
