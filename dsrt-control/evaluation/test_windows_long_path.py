from __future__ import annotations

import os
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from evaluation import current_runtime_launcher as launcher
from evaluation import build_atomic_runtime_release as builder
import runtime_gateway_bootstrap as bootstrap
from runtime_profile_ownership import (
    CONTROL_OWNED_TOP_LEVEL,
    FORBIDDEN_TOP_LEVEL,
    MUTABLE_TOP_LEVEL,
    RUNTIME_ROOTS,
    classify_top_level,
    roots_for_lifecycle,
)


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


@unittest.skipUnless(os.name == "nt", "Windows process identity only")
class WindowsLeaseLivenessTests(unittest.TestCase):
    def test_dead_pid_uses_handle_identity_without_os_kill(self) -> None:
        with (
            mock.patch.object(
                launcher, "_process_start_identity", return_value=None
            ) as identity,
            mock.patch.object(
                launcher.os,
                "kill",
                side_effect=AssertionError("os.kill must not be used"),
            ),
        ):
            self.assertFalse(launcher._default_liveness(424242))
        identity.assert_called_once_with(424242)

    def test_live_pid_uses_exact_handle_identity(self) -> None:
        with mock.patch.object(
            launcher,
            "_process_start_identity",
            return_value="windows-filetime:123",
        ) as identity:
            self.assertTrue(launcher._default_liveness(424242))
        identity.assert_called_once_with(424242)


class RuntimeViewClassificationTests(unittest.TestCase):
    LIVE_PROFILE = Path(
        r"C:\Users\10192\AppData\Local\hermes\profiles\datasage-canary-next"
    )
    LIFECYCLE = (
        "bootstrap",
        "gateway_start",
        "agent_smoke",
        "session",
        "session_reset",
        "planned_shutdown",
        "restart",
    )

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dsrt-runtime-view-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_immutable_profile(self, root: Path) -> None:
        release = root / ".release"
        release.mkdir(parents=True)
        payload = root / "config.yaml"
        payload.write_text("version: lifecycle-test\n", encoding="utf-8")
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        (release / "MANIFEST.sha256").write_text(
            f"{digest}  config.yaml\n", encoding="utf-8", newline="\n"
        )
        (release / "RELEASE.json").write_text(
            "{}\n", encoding="utf-8", newline="\n"
        )

    def _materialize(self, home: Path, name: str, marker: str) -> None:
        contract = next(root for root in RUNTIME_ROOTS if root.name == name)
        path = home / name
        if contract.kind == "directory":
            path.mkdir()
            (path / "lifecycle.marker").write_text(marker, encoding="utf-8")
        else:
            path.write_text(marker, encoding="utf-8")

    def test_contract_is_exact_disjoint_and_evidenced(self) -> None:
        names = [root.name for root in RUNTIME_ROOTS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(root.evidence for root in RUNTIME_ROOTS))
        self.assertTrue(all(root.lifecycle for root in RUNTIME_ROOTS))
        self.assertFalse(MUTABLE_TOP_LEVEL & CONTROL_OWNED_TOP_LEVEL)
        self.assertFalse(MUTABLE_TOP_LEVEL & FORBIDDEN_TOP_LEVEL)
        self.assertEqual(bootstrap.MUTABLE_TOP_LEVEL, MUTABLE_TOP_LEVEL)

    @unittest.skipUnless(LIVE_PROFILE.is_dir(), "reviewed live profile absent")
    def test_current_live_has_no_unknown_roots(self) -> None:
        manifest = bootstrap._manifest(self.LIVE_PROFILE)
        payload_roots = {path.parts[0] for path in manifest}
        result = classify_top_level(
            {path.name for path in self.LIVE_PROFILE.iterdir()}, payload_roots
        )
        self.assertEqual(result["unknown"], frozenset())

    def test_complete_gateway_agent_lifecycle_survives_hydration(self) -> None:
        immutable = self.root / "immutable"
        profiles = self.root / "profiles"
        runtime = profiles / "live"
        backup = self.root / "backups"
        self._write_immutable_profile(immutable)
        profiles.mkdir()
        shutil.copytree(immutable, runtime)
        created: dict[str, str] = {}
        for index, stage in enumerate(self.LIFECYCLE):
            for name in sorted(roots_for_lifecycle(stage) - created.keys()):
                marker = f"{stage}:{name}"
                self._materialize(runtime, name, marker)
                created[name] = marker
            bootstrap.hydrate_runtime_home(
                immutable,
                runtime,
                backup,
                release_unit_id=f"lifecycle-unit-{index}",
            )
            for name, marker in created.items():
                contract = next(root for root in RUNTIME_ROOTS if root.name == name)
                path = runtime / name
                actual = (
                    (path / "lifecycle.marker").read_text(encoding="utf-8")
                    if contract.kind == "directory"
                    else path.read_text(encoding="utf-8")
                )
                self.assertEqual(actual, marker, name)
        self.assertEqual(set(created), set(MUTABLE_TOP_LEVEL))

    def test_each_forbidden_item_is_rejected_before_staging(self) -> None:
        immutable = self.root / "immutable"
        profiles = self.root / "profiles"
        backup = self.root / "backups"
        self._write_immutable_profile(immutable)
        profiles.mkdir()
        for forbidden in sorted(FORBIDDEN_TOP_LEVEL):
            with self.subTest(forbidden=forbidden):
                runtime = profiles / f"live-{len(list(profiles.iterdir()))}"
                shutil.copytree(immutable, runtime)
                path = runtime / forbidden
                if forbidden in {".pytest_cache", "gateway-service"}:
                    path.mkdir()
                else:
                    path.write_text("pollution", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "forbidden"):
                    bootstrap.hydrate_runtime_home(
                        immutable,
                        runtime,
                        backup,
                        release_unit_id="forbidden-unit",
                    )
                self.assertFalse(any(profiles.glob(f".{runtime.name}.stage.*")))

    def test_unknown_item_is_rejected_even_on_current_view(self) -> None:
        immutable = self.root / "immutable"
        profiles = self.root / "profiles"
        runtime = profiles / "live"
        backup = self.root / "backups"
        self._write_immutable_profile(immutable)
        profiles.mkdir()
        shutil.copytree(immutable, runtime)
        bootstrap.hydrate_runtime_home(
            immutable,
            runtime,
            backup,
            release_unit_id="current-unit",
        )
        (runtime / "unowned-runtime.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unclassified"):
            bootstrap.hydrate_runtime_home(
                immutable,
                runtime,
                backup,
                release_unit_id="current-unit",
            )

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
