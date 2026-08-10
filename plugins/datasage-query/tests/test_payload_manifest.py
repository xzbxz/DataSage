import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "payload_manifest_test_package"


def _load(name):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


payload_manifest = _load("payload_manifest")
runtime_health = _load("runtime_health")


class PayloadManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "plugins" / "datasage-query").mkdir(parents=True)
        (self.root / "skills" / "datasage" / "example").mkdir(parents=True)
        (self.root / ".release").mkdir()
        (self.root / "distribution.yaml").write_text(
            "distribution_owned:\n"
            "- distribution.yaml\n"
            "- plugins/datasage-query\n"
            "- skills\n"
            "- .release\n",
            encoding="utf-8",
        )
        (self.root / "plugins" / "datasage-query" / "plugin.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.root / "skills" / "datasage" / "example" / "SKILL.md").write_text(
            "# Example\n", encoding="utf-8"
        )
        self.release = {
            "hermes_source": {
                "commit": "a" * 40,
                "tag": "test-tag",
                "tree_oid": "b" * 40,
                "uv_lock_sha256": "c" * 64,
            },
            **payload_manifest.build_payload_manifest(self.root),
        }
        (self.root / ".release" / "RELEASE.json").write_text(
            json.dumps(self.release), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def _status(self):
        actual_identity = {
            **self.release["hermes_source"],
            "_dirty": False,
            "_expected_tag_present": True,
        }
        with mock.patch.object(
            runtime_health, "_actual_hermes_identity", return_value=actual_identity
        ):
            return runtime_health.runtime_identity_status(profile_root=self.root)

    def test_plugin_and_skill_changes_fail_closed(self):
        self.assertTrue(self._status()["ready"])
        for relative in (
            Path("plugins/datasage-query/plugin.py"),
            Path("skills/datasage/example/SKILL.md"),
        ):
            with self.subTest(path=str(relative)):
                path = self.root / relative
                original = path.read_text(encoding="utf-8")
                path.write_text(original + "changed\n", encoding="utf-8")
                self.assertEqual(
                    self._status()["reason_code"],
                    "HERMES_IDENTITY_PAYLOAD_MISMATCH",
                )
                path.write_text(original, encoding="utf-8")

    def test_missing_owned_file_fails_closed(self):
        (self.root / "plugins" / "datasage-query" / "plugin.py").unlink()
        status = self._status()
        self.assertEqual(status["reason_code"], "HERMES_IDENTITY_PAYLOAD_MISMATCH")
        self.assertEqual(status["payload_missing_count"], 1)

    def test_new_owned_file_fails_closed(self):
        (self.root / "plugins" / "datasage-query" / "new.py").write_text(
            "NEW = True\n", encoding="utf-8"
        )
        status = self._status()
        self.assertEqual(status["reason_code"], "HERMES_IDENTITY_PAYLOAD_MISMATCH")
        self.assertEqual(status["payload_unexpected_count"], 1)

    def test_manifest_never_hashes_itself(self):
        first = payload_manifest.build_payload_manifest(self.root)
        (self.root / ".release" / "RELEASE.json").write_text(
            json.dumps({"changed": True}), encoding="utf-8"
        )
        second = payload_manifest.build_payload_manifest(self.root)
        self.assertEqual(first, second)
        self.assertNotIn(
            payload_manifest.MANIFEST_PATH,
            [entry["path"] for entry in second["payload_files"]],
        )
        self.assertNotIn(
            ".release/MANIFEST.sha256",
            [entry["path"] for entry in second["payload_files"]],
        )

    def test_runtime_skill_state_is_excluded(self):
        state_files = (
            Path("skills/.curator_state"),
            Path("skills/.usage.json"),
            Path("skills/.usage.json.lock"),
            Path("skills/.hub/audit.log"),
        )
        for relative in state_files:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime state\n", encoding="utf-8")
        self.assertEqual(
            payload_manifest.build_payload_manifest(self.root),
            {
                key: self.release[key]
                for key in (
                    "payload_hash_algorithm",
                    "payload_file_count",
                    "payload_files",
                    "payload_sha256",
                )
            },
        )


if __name__ == "__main__":
    unittest.main()
