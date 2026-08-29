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


TEST_PROFILE_COMMIT = "1" * 40
TEST_HERMES_COMMIT = "2" * 40
TEST_SHA = "3" * 64


def _subject(version="0.15.0-test"):
    return {
        "name": "datasage-canary-next",
        "version": version,
        "content_sha256": "a" * 64,
        "profile_git_commit": TEST_PROFILE_COMMIT,
    }


def _host_fixture():
    return {
        "pinned_host": {
            "hermes_version": "0.20.5",
            "hermes_git_commit": TEST_HERMES_COMMIT,
        },
        "latest_user_message": "new authoritative request",
        "expected_authoritative_latest_user_message": "new authoritative request",
        "stale_reference_fragments": ["old task", "prior correction"],
        "required_host_assertions": [
            "compaction_summary_is_before_latest_user_message",
            "latest_user_message_hash_is_preserved_exactly",
            "no_summary_or_old_task_is_appended_as_a_user_message_after_latest_user_message",
            "model_input_authority_marks_latest_user_message_above_summary_draft_and_prior_correction",
        ],
    }


def _host_report(subject=None):
    captured_request = {
        "messages": [
            {
                "role": "assistant",
                "content": (
                    "[CONTEXT COMPACTION — REFERENCE ONLY] old task and prior correction. "
                    "Respond ONLY to the latest user message that appears AFTER this summary; "
                    "the latest user message WINS"
                ),
            },
            {"role": "user", "content": "new authoritative request"},
        ]
    }
    report = {
        "schema": "datasage-host-compaction-evidence/v1",
        "subject": copy.deepcopy(subject or _subject()),
        "host": {
            "hermes_version": "0.20.5",
            "hermes_git_commit": TEST_HERMES_COMMIT,
        },
        "fixture": {
            "path": "tests/fixtures/host_compaction_ordering.json",
            "sha256": TEST_SHA,
        },
        "producer": {
            "path": "tests/test_host_compaction_e2e.py",
            "sha256": TEST_SHA,
        },
        "compression_count": 1,
        "captured_request": captured_request,
        "captured_request_sha256": "",
    }
    builder = _builder()
    report["captured_request_sha256"] = builder._sha256_bytes(
        builder._canonical_json_bytes(captured_request)
    )
    return report


def _performance_contract(version="0.15.0-test"):
    return {
        "schema": "datasage-performance-non-db-contract/v1",
        "scope": "non_db_fail_closed",
        "subject": {"name": "datasage-canary-next", "version": version},
        "host": {
            "hermes_version": "0.20.5",
            "hermes_git_commit": TEST_HERMES_COMMIT,
        },
        "provider": "deepseek",
        "model": "deepseek-chat",
        "sample_plan": {
            "warmup_case_id": "case-a",
            "warmup_runs": 1,
            "measured_runs_per_case": 3,
            "measured_sample_count": 9,
        },
        "execution": {
            "timeout_seconds": 120,
            "max_iterations_per_run": 2,
            "max_output_tokens_per_call": 2048,
        },
        "safety_budget": {
            "max_successful_llm_calls": 20,
            "max_total_peak_estimated_cost_usd": "2.30",
            "budget_semantics": "warmup_plus_measured_cumulative_peak_pricing_test_safety_not_business_sla",
        },
        "pricing_snapshot": {
            "schema": "datasage-pricing-snapshot/v1",
            "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
            "accessed_on": "2026-08-29",
            "billing_period": "per_million_tokens",
            "currency": "USD",
            "unit_tokens": 1000000,
            "model": "deepseek-chat",
            "input_cache_hit_per_million": "0.014",
            "input_cache_miss_per_million": "0.14",
            "output_per_million": "0.28",
        },
        "measurement": {
            "clock": "time.perf_counter_ns",
            "duration_unit": "ns",
            "p50_algorithm": "statistics.median",
            "p90_algorithm": "statistics.quantiles(n=10,method='inclusive')[8]",
            "raw_samples_hash_algorithm": "sha256(canonical_json({warmup,samples}))",
            "raw_samples_hash_owner": "release_receipt_builder",
        },
        "accounting": {
            "usage_source": "Hermes AIAgent.run_conversation result totals",
            "total_tokens_equation": "input_tokens + cache_read_tokens + cache_write_tokens + output_tokens",
            "reasoning_tokens_semantics": "subset_of_output_tokens",
            "cost_derivation": "Decimal cumulative warmup plus measured peak snapshot: input_tokens*cache_miss + cache_read_tokens*cache_hit + output_tokens*output; cache_write_tokens must equal 0",
        },
        "acceptance": {
            "expected_tool_call_rule": "exactly_one_and_equal",
            "required_tool_result": {
                "status": "failed",
                "error": {
                    "code": "DATA_ENTITLEMENT_DENIED",
                    "message": "当前请求未获授权，业务查询未执行。",
                    "retryable": False,
                },
            },
            "required_final_response": "DATA_ENTITLEMENT_DENIED",
            "database_runtime_entered": False,
            "required_api_calls_per_run": 2,
            "max_duration_ns": 120000000000,
            "threshold_basis": "test_execution_safety_not_business_sla",
            "expected_tool_schema_sha256": "5" * 64,
        },
        "deferred_scopes": ["database", "outbound_delivery"],
        "cases": [
            {
                "id": case_id,
                "expected_tool": "datasage_query",
                "expected_arguments": (
                    {"limit": 1} if case_id == "case-b" else {"case": case_id}
                ),
                "prompt": f"prompt {case_id}",
            }
            for case_id in ("case-a", "case-b", "case-c")
        ],
    }


def _performance_report(builder, subject=None, contract=None):
    contract = contract or _performance_contract()
    def sample(case, run_index, duration_ns):
        return {
            "case_id": case["id"],
            "run_index": run_index,
            "duration_ns": duration_ns,
            "expected_tool": case["expected_tool"],
            "observed_tool_calls": [
                {
                    "name": case["expected_tool"],
                    "arguments": copy.deepcopy(case["expected_arguments"]),
                }
            ],
            "tool_result": copy.deepcopy(contract["acceptance"]["required_tool_result"]),
            "database_runtime_entered": False,
            "final_response": "DATA_ENTITLEMENT_DENIED",
            "usage": {
                "input_tokens": 100,
                "cache_read_tokens": 10,
                "cache_write_tokens": 0,
                "output_tokens": 50,
                "reasoning_tokens": 5,
                "total_tokens": 160,
                "api_calls": 2,
            },
        }

    samples = []
    for case_index, case in enumerate(contract["cases"]):
        for run_index in range(1, 4):
            samples.append(sample(case, run_index, (10 + case_index * 3 + run_index) * 1000000))
    return {
        "schema": "datasage-performance-evidence/v1",
        "subject": copy.deepcopy(subject or _subject()),
        "host": {
            "hermes_version": "0.20.5",
            "hermes_git_commit": TEST_HERMES_COMMIT,
        },
        "provider": contract["provider"],
        "model": contract["model"],
        "contract": {
            "path": "tests/fixtures/performance_non_db_contract.json",
            "sha256": TEST_SHA,
        },
        "producer": {
            "path": "tests/run_performance_evidence.py",
            "sha256": TEST_SHA,
        },
        "system_prompt": {"path": "SOUL.md", "sha256": TEST_SHA},
        "tool_schema_sha256": contract["acceptance"]["expected_tool_schema_sha256"],
        "pricing_snapshot_sha256": builder._sha256_bytes(
            builder._canonical_json_bytes(contract["pricing_snapshot"])
        ),
        "warmup": sample(contract["cases"][0], 0, 9000000),
        "samples": samples,
    }


def _self_attested_live(version="0.15.0-test"):
    return {
        "release_target": version,
        "live_model_replay_status": "verified",
        "release_gate_status": "eligible",
        "trusted_replay_gate": {
            "case_ids": ["fabricated-case"],
            "required_replay_runs_per_case": 1,
            "completed_replay_runs_per_case": {"fabricated-case": 99},
            "outbound_delivery_verification_status": "verified",
            "stability_status": "passed",
        },
        "performance_cost_gate": {
            "status": "passed",
            "sample_count": True,
            "p50_ms": True,
            "p90_ms": True,
            "latency_status": "passed",
            "cost_status": "passed",
        },
    }


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

    def test_raw_host_and_performance_evidence_are_derived_but_legacy_live_fields_never_pass(self):
        builder = _builder()
        subject = _subject()
        contract = _performance_contract()
        with mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")):
            result = builder.evaluate_release_gates(
                _self_attested_live(),
                _host_fixture(),
                version=subject["version"],
                subject=subject,
                profile_git_commit=TEST_PROFILE_COMMIT,
                hermes_git_commit=TEST_HERMES_COMMIT,
                host_evidence=_host_report(subject),
                performance_evidence=_performance_report(builder, subject, contract),
                performance_contract=contract,
            )
        codes = {item["code"] for item in result["blockers"]}
        self.assertEqual("passed", result["host_compaction"]["status"])
        self.assertEqual("passed", result["performance_cost"]["status"])
        self.assertEqual(9, result["performance_cost"]["sample_count"])
        self.assertLessEqual(
            result["performance_cost"]["p50_ms"],
            result["performance_cost"]["p90_ms"],
        )
        self.assertRegex(result["performance_cost"]["raw_samples_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(20, result["performance_cost"]["successful_llm_calls"])
        self.assertIsNotNone(result["performance_cost"]["total_peak_estimated_cost_usd"])
        self.assertEqual(
            {
                "LIVE_MODEL_REPLAY_NOT_VERIFIED",
                "LIVE_RELEASE_GATE_BLOCKED",
                "OUTBOUND_DELIVERY_NOT_VERIFIED",
                "STABILITY_NOT_VERIFIED",
                "LIVE_REPLAY_RUNS_INCOMPLETE",
            },
            codes,
        )

    def test_real_contract_and_receipt_subjects_are_compatible(self):
        builder = _builder()
        receipt = builder.build_receipt()
        contract = builder._validate_performance_contract(
            builder._read_strict_json(builder.PERFORMANCE_CONTRACT)
        )
        fixture = builder._read_strict_json(builder.HOST_COMPACTION_FIXTURE)
        self.assertEqual(
            {"name": receipt["name"], "version": receipt["version"]},
            contract["subject"],
        )
        self.assertEqual(fixture["pinned_host"], contract["host"])
        pinned_version = str(yaml.safe_load(builder.MANIFEST.read_text(encoding="utf-8"))["hermes_requires"]).removeprefix("==")
        self.assertTrue(
            builder._host_pins_match(
                fixture,
                contract,
                version=pinned_version,
                commit=builder._git_commit(builder._hermes_source_root()),
            )
        )
        self.assertEqual(5, len(contract["deferred_scopes"]))

    def test_self_attested_pass_fields_and_bool_numbers_cannot_clear_any_gate(self):
        builder = _builder()
        result = builder.evaluate_release_gates(
            _self_attested_live(),
            _host_fixture(),
            version="0.15.0-test",
        )
        codes = {item["code"] for item in result["blockers"]}
        self.assertEqual("unverified_raw_evidence_required", result["live_model_replay"]["status"])
        self.assertEqual("missing", result["host_compaction"]["status"])
        self.assertEqual("missing", result["performance_cost"]["status"])
        self.assertIn("LIVE_MODEL_REPLAY_NOT_VERIFIED", codes)
        self.assertIn("HOST_COMPACTION_NOT_VERIFIED", codes)
        self.assertIn("PERFORMANCE_COST_NOT_VERIFIED", codes)

    def test_dirty_profile_or_host_is_an_explicit_fail_closed_blocker(self):
        builder = _builder()
        result = builder.evaluate_release_gates(
            _self_attested_live(),
            _host_fixture(),
            version="0.15.0-test",
            profile_worktree_clean=False,
            hermes_worktree_clean=False,
        )
        codes = {item["code"] for item in result["blockers"]}
        self.assertIn("PROFILE_WORKTREE_DIRTY", codes)
        self.assertIn("HERMES_WORKTREE_DIRTY", codes)

    def test_current_host_head_must_match_both_tracked_pins(self):
        builder = _builder()
        fixture = _host_fixture()
        contract = _performance_contract()
        self.assertTrue(
            builder._host_pins_match(
                fixture, contract, version="0.20.5", commit=TEST_HERMES_COMMIT
            )
        )
        contract["host"]["hermes_git_commit"] = "9" * 40
        self.assertFalse(
            builder._host_pins_match(
                fixture, contract, version="0.20.5", commit=TEST_HERMES_COMMIT
            )
        )

    def test_performance_raw_evidence_rejects_bool_duplicate_stale_and_over_limit_samples(self):
        builder = _builder()
        subject = _subject()
        contract = _performance_contract()

        def status(report, contract_override=None):
            with mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")):
                result, blockers = builder._evaluate_performance_evidence(
                    report,
                    contract_override or contract,
                    subject=subject,
                    pinned_hermes="0.20.5",
                    hermes_git_commit=TEST_HERMES_COMMIT,
                )
            self.assertEqual("PERFORMANCE_EVIDENCE_INVALID", blockers[0]["code"])
            return result

        bool_number = _performance_report(builder, subject, contract)
        bool_number["samples"][0]["usage"]["input_tokens"] = True
        self.assertEqual("invalid", status(bool_number)["status"])

        duplicate = _performance_report(builder, subject, contract)
        duplicate["samples"][-1] = copy.deepcopy(duplicate["samples"][0])
        self.assertEqual("invalid", status(duplicate)["status"])

        stale = _performance_report(builder, subject, contract)
        stale["subject"]["profile_git_commit"] = "f" * 40
        self.assertEqual("invalid", status(stale)["status"])

        over_limit = _performance_report(builder, subject, contract)
        over_limit["samples"][-1]["duration_ns"] = 120000000001
        self.assertEqual("invalid", status(over_limit)["status"])

        self_attested_quantile = _performance_report(builder, subject, contract)
        self_attested_quantile["p90_ms"] = 1
        self.assertEqual("invalid", status(self_attested_quantile)["status"])

        wrong_arguments = _performance_report(builder, subject, contract)
        wrong_arguments["samples"][0]["observed_tool_calls"][0]["arguments"] = {"forged": True}
        self.assertEqual("invalid", status(wrong_arguments)["status"])

        bool_as_integer_argument = _performance_report(builder, subject, contract)
        bool_as_integer_argument["samples"][3]["observed_tool_calls"][0]["arguments"]["limit"] = True
        self.assertEqual("invalid", status(bool_as_integer_argument)["status"])

        leaked_success = _performance_report(builder, subject, contract)
        leaked_success["samples"][0]["tool_result"] = {
            "status": "success",
            "error": copy.deepcopy(contract["acceptance"]["required_tool_result"]["error"]),
            "data": [{"secret": "must-not-pass"}],
        }
        self.assertEqual("invalid", status(leaked_success)["status"])

        false_final = _performance_report(builder, subject, contract)
        false_final["samples"][0]["final_response"] = "query succeeded"
        self.assertEqual("invalid", status(false_final)["status"])

        missing_warmup = _performance_report(builder, subject, contract)
        del missing_warmup["warmup"]
        self.assertEqual("invalid", status(missing_warmup)["status"])

        excessive_total_cost = _performance_report(builder, subject, contract)
        excessive_total_cost["warmup"]["usage"].update(
            input_tokens=100_000_000,
            total_tokens=100_000_060,
        )
        self.assertEqual("invalid", status(excessive_total_cost)["status"])

        wrong_host_pin = _performance_report(builder, subject, contract)
        wrong_contract = copy.deepcopy(contract)
        wrong_contract["host"]["hermes_git_commit"] = "9" * 40
        self.assertEqual("invalid", status(wrong_host_pin, wrong_contract)["status"])

    def test_host_raw_evidence_rejects_tampered_or_semantically_forged_request(self):
        builder = _builder()
        subject = _subject()
        tampered = _host_report(subject)
        tampered["captured_request_sha256"] = "0" * 64
        with mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")):
            result, blockers = builder._evaluate_host_evidence(
                tampered,
                _host_fixture(),
                subject=subject,
                pinned_hermes="0.20.5",
                hermes_git_commit=TEST_HERMES_COMMIT,
            )
        self.assertEqual("invalid", result["status"])
        self.assertEqual("HOST_COMPACTION_EVIDENCE_INVALID", blockers[0]["code"])

        forged = _host_report(subject)
        forged["captured_request"]["messages"][-1]["content"] = "old task"
        forged["captured_request_sha256"] = builder._sha256_bytes(
            builder._canonical_json_bytes(forged["captured_request"])
        )
        with mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")):
            result, blockers = builder._evaluate_host_evidence(
                forged,
                _host_fixture(),
                subject=subject,
                pinned_hermes="0.20.5",
                hermes_git_commit=TEST_HERMES_COMMIT,
            )
        self.assertEqual("invalid", result["status"])
        self.assertEqual("HOST_COMPACTION_EVIDENCE_INVALID", blockers[0]["code"])

        self_attested = _host_report(subject)
        self_attested["passed"] = True
        with mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")):
            result, _ = builder._evaluate_host_evidence(
                self_attested,
                _host_fixture(),
                subject=subject,
                pinned_hermes="0.20.5",
                hermes_git_commit=TEST_HERMES_COMMIT,
            )
        self.assertEqual("invalid", result["status"])

    def test_tracked_source_is_bound_to_subject_commit_blob_and_worktree(self):
        builder = _builder()
        commit = builder._git_commit(builder.ROOT)
        source = builder.MANIFEST
        digest = builder._sha256_path(source)
        checked = builder._validate_hashed_source(
            {"path": "distribution.yaml", "sha256": digest},
            label="manifest",
            expected_path="distribution.yaml",
            subject_commit=commit,
        )
        self.assertEqual(source, checked)

        with mock.patch.object(builder, "_git_output", side_effect=ValueError("untracked")):
            with self.assertRaisesRegex(ValueError, "untracked"):
                builder._validate_hashed_source(
                    {"path": "distribution.yaml", "sha256": digest},
                    label="manifest",
                    expected_path="distribution.yaml",
                    subject_commit=commit,
                )

        different = b"tracked but different"
        with (
            mock.patch.object(builder, "_profile_git_root", return_value=builder.ROOT.parent),
            mock.patch.object(builder, "_git_output", return_value=different),
        ):
            with self.assertRaisesRegex(ValueError, "worktree content differs"):
                builder._validate_hashed_source(
                    {"path": "distribution.yaml", "sha256": builder._sha256_bytes(different)},
                    label="manifest",
                    expected_path="distribution.yaml",
                    subject_commit=commit,
                )

    def test_strict_json_rejects_duplicate_properties_and_nonfinite_numbers(self):
        builder = _builder()
        with tempfile.TemporaryDirectory() as temporary:
            duplicate = Path(temporary) / "duplicate.json"
            duplicate.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON property"):
                builder._read_strict_json(duplicate)
            nonfinite = Path(temporary) / "nonfinite.json"
            nonfinite.write_text('{"duration":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
                builder._read_strict_json(nonfinite)

    def test_evidence_schema_is_closed_and_has_no_self_attested_gate_fields(self):
        schema = json.loads(
            (PROFILE_ROOT / "tests" / "contracts" / "release_evidence.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
        rendered = json.dumps(schema, sort_keys=True)
        for forbidden in (
            '"status"',
            '"passed"',
            '"eligible"',
            '"p50_ms"',
            '"p90_ms"',
            '"cost_status"',
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertTrue(schema["$defs"]["hostCompaction"]["unevaluatedProperties"] is False)
        self.assertTrue(schema["$defs"]["performance"]["unevaluatedProperties"] is False)

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
