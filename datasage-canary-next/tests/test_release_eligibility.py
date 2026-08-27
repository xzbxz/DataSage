"""Release eligibility, dependency ownership, and inactive-surface gates."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from plugin_registration_probe import probe_registration


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"


def _builder():
    path = PROFILE_ROOT / "build_release_receipt.py"
    spec = importlib.util.spec_from_file_location("datasage_release_eligibility", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load release eligibility command")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseEligibilityTests(unittest.TestCase):
    def test_receipt_identity_comparison_is_exact(self):
        builder = _builder()
        actual = {
            "schema": "datasage-release-receipt/v2",
            "name": "datasage-canary-next",
            "version": "0.15.0-test",
            "content_sha256": "a" * 64,
            "file_count": 1,
            "files": [{"path": "one", "sha256": "b" * 64, "bytes": 1}],
        }
        verified = builder.compare_release_identity(actual, copy.deepcopy(actual))
        self.assertEqual("verified", verified["status"])

        changed = copy.deepcopy(actual)
        changed["files"][0]["bytes"] = 2
        mismatch = builder.compare_release_identity(actual, changed)
        self.assertEqual("mismatch", mismatch["status"])
        self.assertIn("files", mismatch["mismatched_fields"])

    def test_candidate_discovery_selects_one_current_payload_not_a_stale_same_version(self):
        builder = _builder()
        actual = {
            "schema": "datasage-release-receipt/v2",
            "name": "datasage-canary-next",
            "version": "0.15.0-test",
            "content_sha256": "a" * 64,
            "file_count": 1,
            "files": [{"path": "one", "sha256": "b" * 64, "bytes": 1}],
        }
        stale = copy.deepcopy(actual)
        stale["content_sha256"] = "c" * 64
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending"
            pending.mkdir()
            (pending / "old-candidate-receipt.json").write_text(
                json.dumps(stale), encoding="utf-8"
            )
            selected = pending / "current-candidate-receipt-post-replay.json"
            selected.write_text(json.dumps(actual), encoding="utf-8")
            with (
                mock.patch.object(builder, "PENDING_DIR", pending),
                mock.patch.object(builder, "build_receipt", return_value=actual),
            ):
                result = builder.check_release_identity(kind="candidate")
        self.assertEqual("verified", result["status"])
        self.assertEqual(str(selected), result["receipt"])

    def test_candidate_and_final_receipt_namespaces_cannot_be_mixed(self):
        builder = _builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "pending"
            release = root / "release"
            pending.mkdir()
            release.mkdir()
            candidate = pending / "one-candidate-receipt.json"
            final = release / "one-release-receipt.json"
            candidate.write_text("{}", encoding="utf-8")
            final.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(builder, "PENDING_DIR", pending),
                mock.patch.object(builder, "RELEASE_DIR", release),
            ):
                self.assertEqual(
                    "CANDIDATE_RECEIPT_PATH_INVALID",
                    builder._receipt_location(final, kind="candidate")[1],
                )
                self.assertEqual(
                    "FINAL_RECEIPT_PATH_INVALID",
                    builder._receipt_location(candidate, kind="final")[1],
                )

    def test_current_live_host_and_performance_evidence_remains_blocked(self):
        builder = _builder()
        suite = json.loads(builder.GOLDEN_SUITE.read_text(encoding="utf-8"))
        fixture = json.loads(builder.HOST_COMPACTION_FIXTURE.read_text(encoding="utf-8"))
        distribution = yaml.safe_load(builder.MANIFEST.read_text(encoding="utf-8"))
        result = builder.evaluate_release_gates(
            suite["release_validation"],
            fixture,
            version=str(distribution["version"]),
        )
        codes = {blocker["code"] for blocker in result["blockers"]}
        self.assertIn("LIVE_MODEL_REPLAY_NOT_VERIFIED", codes)
        self.assertIn("LIVE_REPLAY_RUNS_INCOMPLETE", codes)
        self.assertIn("HOST_COMPACTION_NOT_VERIFIED", codes)
        self.assertIn("PERFORMANCE_COST_NOT_VERIFIED", codes)

    def test_complete_machine_evidence_can_pass_without_weakening_gates(self):
        builder = _builder()
        release_validation = {
            "release_target": "0.15.0-test",
            "live_model_replay_status": "verified",
            "release_gate_status": "eligible",
            "trusted_replay_gate": {
                "case_ids": ["case-a", "case-b"],
                "required_replay_runs_per_case": 3,
                "completed_replay_runs_per_case": {"case-a": 3, "case-b": 4},
                "outbound_delivery_verification_status": "verified",
                "stability_status": "passed",
            },
            "performance_cost_gate": {
                "status": "passed",
                "sample_count": 6,
                "p50_ms": 1200,
                "p90_ms": 2400,
                "latency_status": "passed",
                "cost_status": "passed",
            },
        }
        fixture = {
            "verification": {
                "status": "passed",
                "hermes_version": "0.20.5",
                "required_assertions_passed": True,
            }
        }
        result = builder.evaluate_release_gates(
            release_validation,
            fixture,
            version="0.15.0-test",
        )
        self.assertEqual([], result["blockers"])

    def test_identity_and_live_gate_blockers_are_reported_separately(self):
        builder = _builder()
        actual = {
            "name": "datasage-canary-next",
            "version": "0.15.0-test",
        }
        current_suite = json.loads(builder.GOLDEN_SUITE.read_text(encoding="utf-8"))
        current_fixture = json.loads(
            builder.HOST_COMPACTION_FIXTURE.read_text(encoding="utf-8")
        )
        with (
            mock.patch.object(builder, "build_receipt", return_value=actual),
            mock.patch.object(
                builder,
                "check_release_identity",
                return_value={"status": "mismatch", "reason_code": "RELEASE_IDENTITY_MISMATCH"},
            ),
            mock.patch.object(
                builder,
                "_read_json",
                side_effect=[current_suite, current_fixture],
            ),
        ):
            result = builder.verify_candidate(
                offline_runner=lambda: {
                    "status": "passed",
                    "reason_code": None,
                    "test_count": 128,
                }
            )
        codes = [blocker["code"] for blocker in result["blockers"]]
        self.assertEqual("mismatch", result["identity"]["status"])
        self.assertIn("RELEASE_IDENTITY_MISMATCH", codes)
        self.assertIn("LIVE_RELEASE_GATE_BLOCKED", codes)
        self.assertEqual("passed", result["offline_tests"]["status"])
        self.assertFalse(result["eligible"])

        base = {
            "eligible": False,
            "identity": {"status": "verified"},
            "blockers": [{"code": "LIVE_RELEASE_GATE_BLOCKED"}],
        }
        with (
            mock.patch.object(sys, "argv", ["build_release_receipt.py", "--verify-candidate"]),
            mock.patch.object(builder, "verify_candidate", return_value=base),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(builder.LIVE_GATE_BLOCKED_EXIT, builder.main())

        mismatch = {**base, "identity": {"status": "mismatch"}}
        with (
            mock.patch.object(sys, "argv", ["build_release_receipt.py", "--verify-candidate"]),
            mock.patch.object(builder, "verify_candidate", return_value=mismatch),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(builder.IDENTITY_MISMATCH_EXIT, builder.main())


class SingleOwnershipTests(unittest.TestCase):
    def test_pymysql_has_one_vendored_runtime_owner(self):
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("python_dependencies", manifest)
        self.assertFalse((PLUGIN_ROOT / "requirements.txt").exists())
        self.assertTrue((PLUGIN_ROOT / "vendor" / "pymysql" / "__init__.py").is_file())
        metadata = (
            PLUGIN_ROOT / "vendor" / "pymysql-1.2.0.dist-info" / "METADATA"
        ).read_text(encoding="utf-8")
        self.assertIn("Version: 1.2.0", metadata)
        module, _ = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_release_dependency_registration",
        )
        driver = module.db_runtime.load_pymysql()
        self.assertTrue(
            Path(driver.__file__).resolve().is_relative_to(
                (PLUGIN_ROOT / "vendor").resolve()
            )
        )
        self.assertEqual((1, 2, 0), tuple(driver.VERSION[:3]))
        self.assertFalse(hasattr(module.tools, "_load_pymysql"))

    def test_datasage_is_not_exposed_to_ownerless_cron(self):
        config = yaml.safe_load((PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("cron", config["platform_toolsets"])

    def test_runtime_identity_declares_prestart_not_per_query_verification(self):
        module, _ = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_release_identity_registration",
        )
        runtime_health = importlib.import_module(f"{module.__name__}.runtime_health")
        status = runtime_health.runtime_identity_status(
            profile_root=PROFILE_ROOT
        )
        release_binding = status["release_binding"]
        self.assertEqual("prestart_release_gate", release_binding["verification_scope"])
        self.assertFalse(release_binding["enforced_per_query"])
        self.assertIn(
            "build_release_receipt.py --verify-candidate",
            release_binding["verification_command"],
        )


if __name__ == "__main__":
    unittest.main()
