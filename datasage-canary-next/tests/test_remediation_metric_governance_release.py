from __future__ import annotations

import importlib.util
from pathlib import Path
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "datasage_metric_governance_release_builder",
    ROOT / "build_release_receipt.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load release builder")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class MetricGovernanceReleaseTests(unittest.TestCase):
    def test_real_governance_truthfully_blocks_release(self):
        report, blockers = builder._evaluate_metric_governance()
        self.assertEqual("blocked", report["status"])
        self.assertEqual(121, report["metric_count"])
        self.assertEqual(
            {"available": 108, "pending_validation": 13},
            report["availability"],
        )
        self.assertEqual({"pass": 0, "block": 121}, report["release"])
        codes = {item["code"] for item in blockers}
        self.assertTrue(
            {
                "METRIC_GOVERNANCE_OWNER_MISSING",
                "METRIC_GOVERNANCE_REVIEW_MISSING",
                "METRIC_GOVERNANCE_INTERVAL_INVALID",
                "METRIC_GOVERNANCE_VALIDATION_PENDING",
            }
            <= codes
        )

    def test_completed_summary_can_pass_without_changing_runtime(self):
        module = types.SimpleNamespace(
            load_contract=lambda: {
                "contract_sha256": "a" * 64,
                "summary": {
                    "metric_count": 2,
                    "availability": {"available": 2},
                    "lifecycle": {"active": 2},
                    "review": {"current": 2},
                    "execution": {"allowed": 2},
                    "release": {"pass": 2, "block": 0},
                    "blocker_counts": {},
                },
            }
        )
        with mock.patch.object(
            builder, "_load_metric_governance_module", return_value=module
        ):
            report, blockers = builder._evaluate_metric_governance()
        self.assertEqual("passed", report["status"])
        self.assertEqual([], blockers)

    def test_contract_and_coverage_errors_map_to_stable_blockers(self):
        class Failure(ValueError):
            def __init__(self, code):
                super().__init__(code)
                self.code = code

        for source_code, expected in (
            (
                "METRIC_GOVERNANCE_COVERAGE_MISMATCH",
                "METRIC_GOVERNANCE_COVERAGE_MISMATCH",
            ),
            ("METRIC_GOVERNANCE_OWNER_INVALID", "METRIC_GOVERNANCE_CONTRACT_INVALID"),
        ):
            with self.subTest(source_code=source_code):
                module = types.SimpleNamespace(
                    load_contract=mock.Mock(side_effect=Failure(source_code))
                )
                with mock.patch.object(
                    builder, "_load_metric_governance_module", return_value=module
                ):
                    report, blockers = builder._evaluate_metric_governance()
                self.assertEqual("invalid", report["status"])
                self.assertEqual(expected, blockers[0]["code"])

    def test_verify_candidate_cannot_drop_governance_blocker(self):
        actual = {
            "name": "datasage-canary-next",
            "version": "0.15.0-rc9",
            "content_sha256": "a" * 64,
            "file_count": 1,
            "files": [],
        }
        gate_result = {
            "live_model_replay": {"status": "passed"},
            "outbound_delivery": {"status": "passed"},
            "stability": {"status": "passed"},
            "live_replay_runs": {"status": "complete"},
            "host_compaction": {"status": "passed"},
            "performance_cost": {"status": "passed"},
            "blockers": [],
        }
        governance_report = {
            "status": "blocked",
            "contract_sha256": "b" * 64,
            "metric_count": 1,
            "availability": {"available": 1},
            "lifecycle": {"active": 1},
            "review": {"missing": 1},
            "execution": {"allowed": 1},
            "release": {"pass": 0, "block": 1},
            "blocker_counts": {"METRIC_GOVERNANCE_REVIEW_MISSING": 1},
            "reason_code": "METRIC_GOVERNANCE_INCOMPLETE",
        }
        governance_blocker = builder._blocker(
            "METRIC_GOVERNANCE_REVIEW_MISSING", "review missing"
        )
        with mock.patch.object(builder, "build_receipt", return_value=actual), mock.patch.object(
            builder,
            "check_release_identity",
            return_value={"status": "verified", "reason_code": None},
        ), mock.patch.object(
            builder, "_read_json", return_value={"release_validation": {}}
        ), mock.patch.object(
            builder, "_read_strict_json", return_value={}
        ), mock.patch.object(
            builder, "_git_commit", return_value="c" * 40
        ), mock.patch.object(
            builder, "_discover_evidence", return_value=None
        ), mock.patch.object(
            builder, "_git_worktree_clean", return_value=True
        ), mock.patch.object(
            builder, "_hermes_source_root", return_value=ROOT
        ), mock.patch.object(
            builder, "evaluate_release_gates", return_value=gate_result
        ), mock.patch.object(
            builder, "_host_pins_match", return_value=True
        ), mock.patch.object(
            builder,
            "_evaluate_metric_governance",
            return_value=(governance_report, [governance_blocker]),
        ):
            result = builder.verify_candidate(
                offline_runner=lambda: {"status": "passed"}
            )
        self.assertFalse(result["eligible"])
        self.assertEqual(governance_report, result["metric_governance"])
        self.assertIn(governance_blocker, result["blockers"])

    def test_release_receipt_owns_both_governance_sources(self):
        receipt = builder.build_receipt()
        paths = {item["path"] for item in receipt["files"]}
        self.assertIn(
            "plugins/datasage-query/contracts/metric-governance.yaml", paths
        )
        self.assertIn("plugins/datasage-query/metric_governance.py", paths)


if __name__ == "__main__":
    unittest.main()
