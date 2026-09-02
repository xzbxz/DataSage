"""Independent regression tests for the release-integrity remediation.

These tests use only synthetic metadata and never read or write the Profile's
pending/release evidence instances.  They deliberately exercise the narrow
release/evidence helpers rather than the production query implementation.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load(PROFILE_ROOT / "build_release_receipt.py", "remediation_builder")
adapter = _load(
    PROFILE_ROOT / "plugins" / "datasage-query" / "e2e" / "canary_transcript_adapter.py",
    "remediation_adapter",
)
performance = _load(
    PROFILE_ROOT / "tests" / "run_performance_evidence.py",
    "remediation_performance",
)
live = _load(
    PROFILE_ROOT / "tests" / "run_live_release_evidence.py",
    "remediation_live",
)


class ReleaseIntegrityRemediationTests(unittest.TestCase):
    def test_promote_requires_eligibility_and_writes_unique_bound_final_atomically(self):
        actual = {
            "schema": "datasage-release-receipt/v2",
            "name": "datasage-canary-next",
            "version": "0.15.0-test",
            "content_sha256": "a" * 64,
            "file_count": 0,
            "files": [],
            "identity_scope": "quality-gate-subject/distribution-owned-runtime-content/v1",
            "excludes_runtime_state": True,
        }
        model = {
            "schema": builder.LIVE_RUN_IDENTITY_SCHEMA,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "model_fingerprint_sha256": "b" * 64,
            "system_prompt_sha256": "c" * 64,
            "profile_content_sha256": "a" * 64,
        }
        eligibility = {
            "eligible": True,
            "gate_report_sha256": "d" * 64,
            "identity": {"status": "verified"},
            "live_model_replay": {
                "model_identity": model,
                "reviewer_attestation": {
                    "schema": builder.REVIEWER_ATTESTATION_SCHEMA,
                    "sha256": "e" * 64,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending, release = root / "pending", root / "release"
            pending.mkdir()
            (root / "build_release_receipt.py").write_bytes(b"builder")
            candidate = pending / "test-candidate-receipt.json"
            candidate.write_text(json.dumps(actual), encoding="utf-8")
            with (
                mock.patch.object(builder, "ROOT", root),
                mock.patch.object(builder, "PENDING_DIR", pending),
                mock.patch.object(builder, "RELEASE_DIR", release),
                mock.patch.object(builder, "build_receipt", return_value=actual),
                mock.patch.object(builder, "verify_candidate", return_value=eligibility),
                mock.patch.object(builder, "check_release_identity", return_value={"status": "verified"}),
                mock.patch.object(builder, "_git_commit", return_value="f" * 40),
                mock.patch.object(builder, "_hermes_source_root", return_value=root),
                mock.patch.object(builder, "yaml") as yaml_module,
            ):
                yaml_module.safe_load.return_value = {"hermes_requires": "==0.20.5"}
                result = builder.promote_candidate(receipt_path=candidate)
            final_path = root / result["receipt"]
            self.assertTrue(final_path.is_file())
            with (
                mock.patch.object(builder, "ROOT", root),
                mock.patch.object(builder, "RELEASE_DIR", release),
                mock.patch.object(builder, "PENDING_DIR", pending),
                mock.patch.object(builder, "build_receipt", return_value=actual),
                mock.patch.object(builder, "verify_candidate", return_value=eligibility),
                mock.patch.object(builder, "_git_commit", return_value="f" * 40),
                mock.patch.object(builder, "_hermes_source_root", return_value=root),
                mock.patch.object(builder, "yaml") as yaml_module,
            ):
                yaml_module.safe_load.return_value = {"hermes_requires": "==0.20.5"}
                self.assertEqual("verified", builder._check_final_release(receipt_path=final_path)["status"])
            final = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(builder.FINAL_RECEIPT_SCHEMA, final["schema"])
            self.assertEqual("d" * 64, final["gate_report_sha256"])
            self.assertEqual("e" * 64, final["reviewer_attestation"]["sha256"])
            self.assertEqual("a" * 64, final["subject"]["content_sha256"])
            self.assertEqual(candidate.relative_to(root).as_posix(), final["candidate"]["path"])
            self.assertFalse(list(release.glob("*.tmp")))

    def test_promote_refuses_false_eligibility_without_touching_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending, release = root / "pending", root / "release"
            pending.mkdir()
            candidate = pending / "test-candidate-receipt.json"
            candidate.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(builder, "ROOT", root),
                mock.patch.object(builder, "PENDING_DIR", pending),
                mock.patch.object(builder, "RELEASE_DIR", release),
                mock.patch.object(builder, "verify_candidate", return_value={"eligible": False}),
            ):
                with self.assertRaisesRegex(ValueError, "not eligible"):
                    builder.promote_candidate(receipt_path=candidate)
            self.assertFalse(release.exists())

    def test_check_rejects_legacy_identity_only_final_receipt(self):
        actual = {
            "schema": "datasage-release-receipt/v2",
            "name": "datasage-canary-next",
            "version": "0.15.0-test",
            "content_sha256": "a" * 64,
            "file_count": 0,
            "files": [],
            "identity_scope": "quality-gate-subject/distribution-owned-runtime-content/v1",
            "excludes_runtime_state": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            release.mkdir()
            path = release / "legacy-release-receipt.json"
            path.write_text(json.dumps(actual), encoding="utf-8")
            with (
                mock.patch.object(builder, "ROOT", root),
                mock.patch.object(builder, "RELEASE_DIR", release),
                mock.patch.object(builder, "build_receipt", return_value=actual),
            ):
                result = builder._check_final_release(receipt_path=path)
            self.assertEqual("mismatch", result["status"])
            self.assertEqual("FINAL_RECEIPT_INVALID", result["reason_code"])

    def test_offline_gate_rejects_zero_unknown_skip_and_expected_failure(self):
        class Completed:
            returncode = 0
            stdout = "Ran 0 tests in 0.01s\nOK\n"
            stderr = ""

        with mock.patch.object(builder.subprocess, "run", return_value=Completed()):
            self.assertEqual("OFFLINE_TEST_ZERO", builder._run_offline_tests()["reason_code"])

        class Skipped:
            returncode = 0
            stdout = "test_new_gap (suite.Tests) ... skipped 'missing'\nRan 1 tests in 0.01s\nOK (skipped=1)\n"
            stderr = ""

        with mock.patch.object(builder.subprocess, "run", return_value=Skipped()):
            self.assertEqual(
                "OFFLINE_TEST_SKIP_NOT_ALLOWLISTED",
                builder._run_offline_tests()["reason_code"],
            )

        class AllowedSkip:
            returncode = 0
            stdout = "test_build_evidence_rejects_current_untracked_producer (suite.Tests) ... skipped 'delegated'\nRan 1 tests in 0.01s\nOK (skipped=1)\n"
            stderr = ""

        with mock.patch.object(builder.subprocess, "run", return_value=AllowedSkip()):
            self.assertEqual("passed", builder._run_offline_tests()["status"])

        class Expected:
            returncode = 0
            stdout = "test_old (suite.Tests) ... expected failure\nRan 1 tests in 0.01s\nOK (expected failures=1)\n"
            stderr = ""

        with mock.patch.object(builder.subprocess, "run", return_value=Expected()):
            self.assertEqual(
                "OFFLINE_TEST_EXPECTED_FAILURE",
                builder._run_offline_tests()["reason_code"],
            )

    def test_nested_adapter_json_is_strict(self):
        duplicate = "{" + '"status":"failed","status":"success"' + "}"
        with self.assertRaises(ValueError):
            adapter._json_value(duplicate, "nested")
        with self.assertRaises(ValueError):
            adapter._json_value('{"value":NaN}', "nested")

    def test_live_endpoint_rejects_unknown_tools_and_session_meta_fields(self):
        contract = builder._read_strict_json(builder.LIVE_RELEASE_CONTRACT)
        messages = [
            {"id": 1, "role": "user", "content": "first"},
            {"id": 2, "role": "assistant", "content": None, "tool_calls": [{"id": "x", "function": {"name": "unknown_tool", "arguments": "{}"}}]},
            {"id": 3, "role": "tool", "tool_call_id": "x", "tool_name": "unknown_tool", "content": "{}"},
            {"id": 4, "role": "assistant", "content": "final", "tool_calls": None},
            {"id": 5, "role": "user", "content": "second"},
            {"id": 6, "role": "assistant", "content": "final", "tool_calls": None},
        ]
        with self.assertRaisesRegex(ValueError, "unapproved tool"):
            builder._live_endpoints(messages, ["first", "second"], contract["turn_completion_policy"])

        metadata = dict(messages[0], role="session_meta", events=[{"role": "tool"}])
        messages[1] = {"id": 2, "role": "user", "content": "first"}
        messages.insert(0, metadata)
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            builder._live_endpoints(messages, ["first", "second"], contract["turn_completion_policy"])

    def test_live_query_integrity_requires_complete_state_and_sealed_claims(self):
        request = {
            "request_id": "r1",
            "domain": "delivery",
            "metric": "delivery_amount",
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
        }
        metric_ref = "metric_" + __import__("hashlib").sha256(
            ("delivery" + chr(0) + "delivery_amount").encode()
        ).hexdigest()[:16]
        claim_id = "claim_" + __import__("hashlib").sha256(b"r1").hexdigest()[:20]
        row = {
            "claim_id": claim_id,
            "dimensions": [],
            "facts": {"metric_value": "1"},
            "states": {"metric_data_state": "complete"},
            "allowed_relations": ["observation"],
            "unit": "CNY",
            "currency": "CNY",
        }
        claim = {
            **row,
            "claim_seal": "sha256_" + "1" * 64,
            "request_id": "r1",
            "metric_ref": metric_ref,
            "scope_fingerprint": "scope",
            "projection_fingerprint": "projection",
            "period": {"start": "2026-01-01", "end": "2026-09-01"},
            "source_truncated": False,
        }
        result = {
            "request_id": "r1",
            "status": "success",
            "data_state": "rows",
            "business_metric_ref": metric_ref,
            "business_metric_label": "Delivery amount",
            "row_count": 1,
            "rows": [row],
            "claim_ledger": [claim],
            "truncated": False,
            "applied_time_range": {
                **request["time_range"],
                "source": "explicit",
                "calendar_evidence": {
                    "version": "calendar-period-evidence/v2",
                    "observation_basis": "business_clock_query_observation",
                    "observed_on": "2026-08-30",
                    "period_state": "in_progress",
                    "source_freshness": "not_proven",
                },
            },
            "error": None,
        }
        payload = {
            "status": "success",
            "model_wire_version": "datasage-query-model-wire/v3",
            "source_evidence_ref": {
                "schema": adapter.MODEL_SOURCE_REFERENCE_SCHEMA,
                "source_ref_sha256": "a" * 64,
            },
            "evidence_bundle": {"items": [{"status": "success", "request_id": "r1"}]},
            "results": [result],
        }
        call = {"name": "datasage_query", "arguments": {"requests": [request]}, "result": payload}
        _plan, evidence = adapter._normalize(
            [call],
            expected_business_database_ref_sha256="a" * 64,
            expected_observed_on=__import__("datetime").date(2026, 8, 30),
            require_sealed_claims=True,
            require_result_status_consistency=True,
        )
        self.assertEqual(1, evidence["successful_queries"])

        missing_claims = dict(result, claim_ledger=[])
        missing = dict(payload, results=[missing_claims])
        _plan, missing_evidence = adapter._normalize(
            [{"name": "datasage_query", "arguments": {"requests": [request]}, "result": missing}],
            expected_business_database_ref_sha256="a" * 64,
            expected_observed_on=__import__("datetime").date(2026, 8, 30),
            require_sealed_claims=True,
        )
        self.assertEqual(0, missing_evidence["successful_queries"])

        incomplete = dict(result, data_state="incomplete")
        incomplete_payload = dict(payload, results=[incomplete])
        _plan, incomplete_evidence = adapter._normalize(
            [{"name": "datasage_query", "arguments": {"requests": [request]}, "result": incomplete_payload}],
            expected_business_database_ref_sha256="a" * 64,
            expected_observed_on=__import__("datetime").date(2026, 8, 30),
            require_sealed_claims=True,
        )
        self.assertEqual(0, incomplete_evidence["successful_queries"])

        failed_envelope = dict(payload, status="failed", error={"code": "TOP_LEVEL_FAILURE"})
        _plan, failed_evidence = adapter._normalize(
            [{"name": "datasage_query", "arguments": {"requests": [request]}, "result": failed_envelope}],
            expected_business_database_ref_sha256="a" * 64,
            expected_observed_on=__import__("datetime").date(2026, 8, 30),
            require_sealed_claims=True,
            require_result_status_consistency=True,
        )
        self.assertEqual(0, failed_evidence["successful_queries"])


    def test_performance_tool_result_must_match_call_identity(self):
        contract = performance._read_json(performance.CONTRACT_PATH)
        case = contract["cases"][0]
        call = types.SimpleNamespace(
            id="expected-call",
            function=types.SimpleNamespace(
                name=case["expected_tool"],
                arguments=json.dumps(case["expected_arguments"]),
            ),
        )
        result = {
            "provider": contract["provider"],
            "model": contract["model"],
            "completed": True,
            "messages": [
                types.SimpleNamespace(role="assistant", tool_calls=[call]),
                {"role": "tool", "tool_call_id": "wrong", "tool_name": case["expected_tool"], "content": json.dumps(contract["acceptance"]["required_tool_result"], ensure_ascii=False)},
                {"role": "assistant", "content": "DATA_ENTITLEMENT_DENIED"},
            ],
            "final_response": "DATA_ENTITLEMENT_DENIED",
            "input_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "total_tokens": 2,
            "api_calls": 2,
        }
        with self.assertRaisesRegex(performance.EvidenceError, "not bound"):
            performance._observe_run(
                case,
                contract,
                lambda _case: (result, False, contract["acceptance"]["expected_tool_schema_sha256"]),
                iter(range(2)).__next__,
                1,
            )

    def test_live_run_identity_requires_host_fields_and_binds_profile(self):
        with self.assertRaisesRegex(RuntimeError, "fresh-run nonce"):
            live._session_run_identity({}, profile_content_sha256="a" * 64)
        session = {
            "fresh_run_nonce": "1" * 32,
            "billing_provider": "deepseek",
            "model": "deepseek-v4-flash",
            "model_fingerprint": "ABCDEF0123456789" * 4,
            "system_prompt_hash": "b" * 64,
            "profile_content_sha256": "a" * 64,
        }
        identity = live._session_run_identity(session, profile_content_sha256="a" * 64)
        self.assertEqual("1" * 32, identity["fresh_run_nonce"])
        self.assertEqual(("ABCDEF0123456789" * 4).lower(), identity["model_fingerprint_sha256"])
        self.assertEqual("a" * 64, identity["profile_content_sha256"])

    def test_model_fingerprint_rejects_placeholders_and_arbitrary_text(self):
        invalid_values = (None, "", "unknown", "n/a", "provider-fingerprint", "0" * 63, "0" * 65, "g" * 64)
        for invalid in invalid_values:
            with self.subTest(value=invalid):
                session = {
                    "fresh_run_nonce": "1" * 32,
                    "billing_provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "model_fingerprint": invalid,
                    "system_prompt_hash": "b" * 64,
                    "profile_content_sha256": "a" * 64,
                }
                with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                    live._session_run_identity(session, profile_content_sha256="a" * 64)

                exported = {
                    "fresh_run_nonce": "1" * 32,
                    "billing_provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "model_fingerprint": invalid,
                    "system_prompt_hash": "b" * 64,
                    "profile_content_sha256": "a" * 64,
                }
                with self.assertRaisesRegex(ValueError, "fingerprint"):
                    builder._live_run_identity_from_export(
                        exported,
                        subject={"content_sha256": "a" * 64},
                        expected_model=("deepseek", "deepseek-v4-flash"),
                    )

        exported = {
            "fresh_run_nonce": "2" * 32,
            "billing_provider": "deepseek",
            "model": "deepseek-v4-flash",
            "model_fingerprint": "ABCDEF0123456789" * 4,
            "system_prompt_hash": "b" * 64,
            "profile_content_sha256": "a" * 64,
        }
        identity = builder._live_run_identity_from_export(
            exported,
            subject={"content_sha256": "a" * 64},
            expected_model=("deepseek", "deepseek-v4-flash"),
        )
        self.assertEqual(("ABCDEF0123456789" * 4).lower(), identity["model_fingerprint_sha256"])


if __name__ == "__main__":
    unittest.main()
