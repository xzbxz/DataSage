"""Release eligibility, dependency ownership, and inactive-surface gates."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
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


def _live_contract():
    contract = json.loads(
        (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
            encoding="utf-8"
        )
    )
    contract["subject"] = {"name": "datasage-canary-next", "version": "0.15.0-test"}
    contract["host"] = {
        "hermes_version": "0.20.5",
        "hermes_git_commit": TEST_HERMES_COMMIT,
    }
    return contract


def _live_manifest(contract=None):
    contract = contract or _live_contract()
    return {
        "release_target": contract["subject"]["version"],
        "transcript_source": "wecom",
        "trusted_replay_gate": {
            "case_ids": copy.deepcopy(contract["case_plan"]["case_ids"]),
            "required_replay_runs_per_case": contract["case_plan"]["runs"],
        },
    }


def _live_report(builder, evidence_dir, subject=None, contract=None):
    subject = copy.deepcopy(subject or _subject())
    contract = contract or _live_contract()
    case_ids = contract["case_plan"]["case_ids"]
    commands = contract["execution"]["command_shapes"]
    suite = json.loads(builder.GOLDEN_SUITE.read_text(encoding="utf-8"))
    golden = {case["id"]: case for case in suite["cases"]}
    natural_review_evidence = {
        "ambiguity_10_rc5_vietnam_scorecard": {
            "report_metrics_individually": ("各项业务指标会分别呈现", "不会把不同指标合成为未经治理的总分"),
            "report_governed_target_status": ("目标状态只按已经治理的口径报告", "不会自行推导一个总体强弱判断"),
            "report_inventory_or_disclose_missing_capability": ("库存结论只依据实际可用的证据说明", "缺少库存依据时会明确标注无法确认"),
            "disclose_period_flow_and_snapshot_scopes": ("期间流量与时点快照会分开标明", "两类统计范围不会混在同一口径里"),
            "disclose_partial_period_comparison_limit": ("部分期间与完整期间并不完全可比", "局部观察不能被表述成正式趋势"),
        },
        "multiturn_08_rc5_thailand_followup": {
            "ask_entity_clarification": ("泰国可能代表国家市场或具体业务实体", "请先确认需要比较的准确对象"),
            "state_no_unconfirmed_entity_substitution": ("在确认之前不会替换查询实体", "不会自动把泰国当成曼谷或其他候选对象"),
        },
    }

    def sha_bytes(payload):
        return hashlib.sha256(payload).hexdigest()

    def sha(value):
        payload = value.encode("utf-8") if isinstance(value, str) else builder._canonical_json_bytes(value)
        return sha_bytes(payload)

    def artifact(path):
        payload = path.read_bytes()
        return {
            "path": path.relative_to(evidence_dir).as_posix(),
            "sha256": sha_bytes(payload),
            "bytes": len(payload),
        }

    def candidate_and_export(run_index):
        session_id = f"private-live-session-{run_index}"
        messages = []
        candidate_cases = []
        turns = []
        external_reviews = []
        profile = {
            "profile_id": subject["name"],
            "artifact_id": subject["profile_git_commit"],
            "payload_sha256": subject["content_sha256"],
        }
        database_identity = "c" * 64
        for offset, case_id in enumerate(case_ids):
            case = golden[case_id]
            user_id = offset * 2 + 1
            final_id = user_id + 1
            phrases = natural_review_evidence[case_id]
            final_answer = "。".join(
                phrase
                for label in case["required_conclusions"]
                for phrase in phrases[label]
            ) + "。"
            turn_messages = [
                {"id": user_id, "role": "user", "tool_name": None, "tool_call_id": None, "tool_calls": None, "content": case["prompt"]},
                {"id": final_id, "role": "assistant", "tool_name": None, "tool_call_id": None, "tool_calls": None, "content": final_answer},
            ]
            messages.extend(turn_messages)
            prompt_sha = sha_bytes(case["prompt"].encode("utf-8"))
            final_sha = sha_bytes(final_answer.encode("utf-8"))
            fixture_sha = "d" * 64
            database_ref_sha = "e" * 64
            watermark = sha({
                "schema": "datasage-replay-watermark/v2-live-fixture",
                "test_id": case_id,
                "conversation_id": case["conversation_id"],
                "turn": case["turn"],
                "canonical_prompt_sha256": prompt_sha,
                "user_message_id": user_id,
                "database_identity_sha256": database_identity,
                "artifact_id": profile["artifact_id"],
                "payload_sha256": profile["payload_sha256"],
                "fixture_attestation_sha256": fixture_sha,
                "business_database_ref_sha256": database_ref_sha,
            })
            binding = {
                "test_id": case_id,
                "artifact_id": profile["artifact_id"],
                "payload_sha256": profile["payload_sha256"],
                "session_id": session_id,
                "user_message_id": user_id,
                "canonical_prompt_sha256": prompt_sha,
                "database_identity_sha256": database_identity,
                "watermark_sha256": watermark,
                "final_answer_sha256": final_sha,
                "fixture_attestation_sha256": fixture_sha,
                "business_database_ref_sha256": database_ref_sha,
            }
            external_reviews_for_case = []
            for reviewer_index, reviewer_id in enumerate(("datasage-live-reviewer-a", "datasage-live-reviewer-b")):
                evidence = []
                for label in case["required_conclusions"]:
                    excerpt = phrases[label][reviewer_index]
                    start = final_answer.index(excerpt)
                    evidence.append({"label": label, "start": start, "end": start + len(excerpt), "text_sha256": sha(excerpt)})
                external_reviews_for_case.append({
                    "run_index": run_index,
                    "case_id": case_id,
                    "session_id_sha256": sha(session_id),
                    "final_answer_sha256": final_sha,
                    "capture_sha256": "0" * 64,
                    "reviewer_id": reviewer_id,
                    "labels": copy.deepcopy(case["required_conclusions"]),
                    "evidence": evidence,
                    "reviewed_at": "2026-08-29T00:00:00+00:00",
                })
            consensus_reviewer_id = builder._review_consensus_id(external_reviews_for_case)
            assertion = {
                "schema": "datasage-review-assertion/v2",
                "status": "reviewed",
                "test_id": case_id,
                "artifact_id": profile["artifact_id"],
                "payload_sha256": profile["payload_sha256"],
                "session_id": session_id,
                "user_message_id": user_id,
                "canonical_prompt_sha256": prompt_sha,
                "database_identity_sha256": database_identity,
                "watermark_sha256": watermark,
                "final_answer_sha256": final_sha,
                "reviewer_id": consensus_reviewer_id,
                "labels": external_reviews_for_case[0]["labels"],
                "fixture_attestation_sha256": fixture_sha,
                "business_database_ref_sha256": database_ref_sha,
            }
            review = {
                "status": "reviewed",
                "reviewer_id_sha256": sha(consensus_reviewer_id),
                "assertion_sha256": sha(assertion),
                "binding_sha256": sha(binding),
                "plan_trace_sha256": None,
            }
            plan = {
                key: copy.deepcopy(value)
                for key, value in case["plan_constraints"].items()
                if not key.startswith("must_not_")
            }
            plan.setdefault("context_bindings", {"filter_fingerprints": {}})
            observed = {
                "id": case_id,
                "session_id": session_id,
                "session_lineage": [session_id],
                "plan": plan,
                "conclusions": copy.deepcopy(case["required_conclusions"]),
                "conclusion_review": review,
                "evidence": {
                    "receipts": copy.deepcopy(case["evidence_requirements"]["required_receipts"]),
                    "successful_queries": case["evidence_requirements"]["minimum_successful_queries"],
                    "failed_queries": 0,
                    "truncated": False,
                    "reconciled": case["evidence_requirements"]["require_reconciled_decomposition"],
                    "query_attempted": not case["evidence_requirements"]["must_not_query"],
                    "error_codes": copy.deepcopy(case["evidence_requirements"]["required_error_codes"]),
                },
            }
            candidate_cases.append(observed)
            external_reviews.extend(external_reviews_for_case)
            turns.append({
                "test_id": case_id,
                "conversation_id": case["conversation_id"],
                "turn": case["turn"],
                "session_id": session_id,
                "database_message_ids": [user_id, final_id],
                "user_message_id": user_id,
                "canonical_prompt_sha256": prompt_sha,
                "database_identity_sha256": database_identity,
                "watermark_sha256": watermark,
                "user_platform_message_id": None,
                "final_message_id": final_id,
                "final_platform_message_id": None,
                "final_answer_sha256": final_sha,
                "transcript_sha256": sha(turn_messages),
                "candidate_case_sha256": sha(observed),
                "conclusion_review": review,
                "fixture_attestation_sha256": fixture_sha,
                "business_database_ref_sha256": database_ref_sha,
            })
        receipt = {
            "schema": "datasage-canary-receipt/v1",
            "captured_at": "2026-08-29T00:00:00+00:00",
            "source": {"platform": "wecom", "sqlite_mode": "ro", "query_only": True, "state_db_identity_sha256": database_identity},
            "profile_artifact": profile,
            "state_db_identity_sha256": database_identity,
            "candidate_cases_sha256": sha(candidate_cases),
            "turns": turns,
        }
        receipt["receipt_sha256"] = sha(receipt)
        return session_id, {
            "id": session_id,
            "source": "wecom",
            "chat_type": "dm",
            "user_id": "synthetic-test-identity",
            "chat_id": "synthetic-test-identity",
            "title": f"datasage-live-{subject['profile_git_commit'][:12]}-run-{run_index}",
            "started_at": "2026-08-29T00:00:00+00:00",
            "messages": messages,
        }, {
            "schema": "datasage-golden-expert-candidate/v1",
            "profile_artifact": profile,
            "state_db_identity_sha256": database_identity,
            "cases": candidate_cases,
            "canary_receipt": receipt,
        }, external_reviews

    def process(template, seed, run_dir, stream_prefix, bindings=None, stdout=b"", stderr=b""):
        bindings = bindings or {}
        argv = []
        for token in template:
            if token.startswith("{") and token.endswith("}"):
                binding = token[1:-1]
                if binding not in bindings:
                    raise AssertionError(f"missing test binding {binding}")
                argv.append({"binding": binding, "value_sha256": sha(bindings[binding])})
            else:
                argv.append(token)
        stdout_path, stderr_path = run_dir / f"{stream_prefix}.stdout", run_dir / f"{stream_prefix}.stderr"
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(stderr)
        return {
            "argv": argv,
            "argv_sha256": sha(argv),
            "exit_code": 0,
            "timed_out": False,
            "duration_ns": seed * 1_000_000,
            "stdout": artifact(stdout_path),
            "stderr": artifact(stderr_path),
        }

    python = str(builder._canonical_hermes_python())
    state_db = str((builder.ROOT / "state.db").resolve())
    python_proof = {
        "schema": "datasage-python-runtime-provenance/v1",
        "entry_kind": "runner",
        "executable": copy.deepcopy(contract["python_provenance_approval"]["executable"]),
        "sys_executable_sha256": "2" * 64,
        "sys_prefix_sha256": "3" * 64,
        "sys_base_prefix_sha256": "9" * 64,
        "sys_path_entry_sha256": ["4" * 64, "5" * 64],
        "sys_path_sha256": "6" * 64,
        "sys_path_template_sha256": contract["python_provenance_approval"]["sys_path"]["runner_sha256"],
        "path_controls": [{
            "path_sha256": "7" * 64,
            "pth": copy.deepcopy(contract["python_provenance_approval"]["pth"]),
            "customization": {
                "sitecustomize.py": {"exists": False, "identity": None},
                "usercustomize.py": {"exists": False, "identity": None},
            },
        }],
        "pth_import_payloads": copy.deepcopy(contract["python_provenance_approval"]["pth_import_payloads"]),
        "import_origins": [
            {**copy.deepcopy(item), "origin_sha256": "a" * 64}
            for item in contract["python_provenance_approval"]["imports"]
        ],
    }
    captures = []
    runs = []
    review_paths = []
    for run_index in range(1, 4):
        run_dir = evidence_dir / "private" / subject["profile_git_commit"] / f"run-{run_index}"
        run_dir.mkdir(parents=True)
        session_id, export, candidate, external_reviews = candidate_and_export(run_index)
        export_path = run_dir / "session.jsonl"
        export_path.write_text(json.dumps(export, ensure_ascii=False) + "\n", encoding="utf-8")
        candidate_path = run_dir / "candidate.json"
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        reviews_path = run_dir / "reviews.json"
        reviews_path.write_text(json.dumps({"schema": "datasage-live-review-set/v1", "run_index": run_index, "reviews": external_reviews}), encoding="utf-8")
        review_paths.append(reviews_path)
        bindings_path = run_dir / "bindings.json"
        bindings_path.write_text("{}", encoding="utf-8")
        scorer = builder._load_e2e_module(f"_test_live_scorer_{run_index}", builder.ROOT / builder.GOLDEN_SCORER_PATH)
        score = scorer.score(scorer.select_suite(suite, case_ids), candidate)
        score_path = run_dir / "score.json"
        score_path.write_text(json.dumps(score, ensure_ascii=False), encoding="utf-8")
        export_ref = artifact(export_path)
        candidate_ref = artifact(candidate_path)
        reviews_ref = artifact(reviews_path)
        score_ref = artifact(score_path)
        session_sha = sha_bytes(session_id.encode("utf-8"))
        capture_bindings = {
            "session_export": {"python": python, "exact_session_id": session_id},
        }
        capture_processes = {
            name: process(
                commands[name], run_index * 10 + offset, run_dir, name.replace("_", "-"), capture_bindings[name],
                export_path.read_bytes(),
            )
            for offset, name in enumerate(("session_export",), 1)
        }
        capture_artifacts = []
        for name in ("session_export",):
            capture_artifacts.extend((capture_processes[name]["stdout"], capture_processes[name]["stderr"]))
        capture_artifacts.append(export_ref)
        captures.append({
            "run_index": run_index,
            "case_ids": copy.deepcopy(case_ids),
            "processes": capture_processes,
            "session": {
                "source": "wecom", "chat_type": "dm",
                "user_id_sha256": contract["inbound"]["expected_user_id_sha256"],
                "chat_id_sha256": contract["inbound"]["expected_chat_id_sha256"],
                "title": f"datasage-live-{subject['profile_git_commit'][:12]}-run-{run_index}",
                "started_at": "2026-08-29T00:00:00+00:00",
                "export_format": "jsonl", "turns": 2,
                "lineage_sha256": sha([session_id]), "session_id_sha256": session_sha,
                "final_answer_sha256": [external_reviews[0]["final_answer_sha256"], external_reviews[2]["final_answer_sha256"]],
                "export": export_ref,
            },
            "artifact_set_sha256": sha(capture_artifacts),
        })
        finalize_bindings = {
            "adapter": {"python": python, "adapter": str(builder.ROOT / builder.TRANSCRIPT_ADAPTER_PATH), "state_db": state_db, "bindings": str(bindings_path), "candidate": str(candidate_path)},
            "scorer": {"python": python, "scorer": str(builder.ROOT / builder.GOLDEN_SCORER_PATH), "golden_suite": str(builder.ROOT / builder.GOLDEN_SUITE_PATH), "case_1": case_ids[0], "case_2": case_ids[1], "candidate": str(candidate_path), "score_report": str(score_path)},
        }
        runs.append(
            {
                "run_index": run_index,
                "case_ids": copy.deepcopy(case_ids),
                "processes": {
                    name: process(
                        commands[name], run_index * 10 + offset, run_dir, name, finalize_bindings[name],
                    )
                    for offset, name in enumerate(("adapter", "scorer"), start=4)
                },
                "candidate": candidate_ref,
                "score_report": score_ref,
                "reviews": reviews_ref,
            }
        )
    source = lambda path: {"path": path, "sha256": TEST_SHA}
    capture_path = evidence_dir / "private" / subject["profile_git_commit"] / "capture.json"
    capture_path.write_text(json.dumps({
        "schema": "datasage-live-capture/v2",
        "captured_at": "2026-08-29T00:00:00+00:00",
        "subject_commit_timestamp": "2026-08-28T00:00:00+00:00",
        "subject": subject,
        "host": {"hermes_version": "0.20.5", "hermes_git_commit": TEST_HERMES_COMMIT},
        "contract": source("tests/fixtures/live_release_contract.json"),
        "python_provenance": {"before": python_proof, "after": python_proof},
        "runs": captures,
    }, ensure_ascii=False), encoding="utf-8")
    capture_ref = artifact(capture_path)
    capture_digest_path = capture_path.with_name("capture.sha256")
    capture_digest_path.write_text(capture_ref["sha256"] + "\n", encoding="ascii", newline="")
    for run, reviews_path in zip(runs, review_paths):
        value = json.loads(reviews_path.read_text(encoding="utf-8"))
        for review in value["reviews"]:
            review["capture_sha256"] = capture_ref["sha256"]
        reviews_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        run["reviews"] = artifact(reviews_path)
    return {
        "schema": "datasage-live-release-evidence/v2",
        "captured_at": "2026-08-29T00:00:00+00:00",
        "subject_commit_timestamp": "2026-08-28T00:00:00+00:00",
        "subject": subject,
        "host": {
            "hermes_version": "0.20.5",
            "hermes_git_commit": TEST_HERMES_COMMIT,
        },
        "contract": source("tests/fixtures/live_release_contract.json"),
        "producer": source("tests/run_live_release_evidence.py"),
        "golden_suite": source("plugins/datasage-query/e2e/golden_expert_cases.json"),
        "adapter": source("plugins/datasage-query/e2e/canary_transcript_adapter.py"),
        "scorer": source("plugins/datasage-query/e2e/golden_expert_scorer.py"),
        "runtime_readiness_policy_sha256": builder._sha256_bytes(
            builder._canonical_json_bytes(contract["runtime_readiness_policy"])
        ),
        "capture": capture_ref,
        "capture_digest": artifact(capture_digest_path),
        "python_provenance": {"before": python_proof, "after": python_proof},
        "runs": runs,
    }


class ReleaseEligibilityTests(unittest.TestCase):
    def test_builder_binds_tool_names_before_entitlement_and_rejects_push(self):
        builder = _builder()
        prompts = ["first", "second"]
        script = _live_contract()["clarify_reply_script"]

        def transcript(function_name, tool_name, content):
            return [
                {"id": 1, "role": "user", "content": "first"},
                {
                    "id": 2,
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call-1", "function": {"name": function_name}}],
                },
                {"id": 3, "role": "tool", "tool_call_id": "call-1", "tool_name": tool_name, "content": content},
                {"id": 4, "role": "assistant", "content": "final one", "tool_calls": None},
                {"id": 5, "role": "user", "content": "second"},
                {"id": 6, "role": "assistant", "content": "final two", "tool_calls": None},
            ]

        clarify = transcript("clarify", "clarify", "declined")
        clarify[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
            "question": "Pick a governed definition",
            "choices": ["A", "B"],
        })
        clarify[2]["content"] = json.dumps({
            "question": "Pick a governed definition",
            "choices_offered": ["A", "B"],
            "user_response": script[0]["fixed_response"],
        }, ensure_ascii=False)
        self.assertEqual(2, len(builder._live_endpoints(clarify, prompts, script)))
        with self.assertRaisesRegex(ValueError, "datasage_push is forbidden"):
            builder._live_endpoints(
                transcript("datasage_push", "datasage_push", "{}"), prompts, script
            )

        denial = json.dumps({"error": {"code": "DATA_ENTITLEMENT_DENIED"}})
        for tool_name in ("datasage_catalog", ""):
            with self.subTest(tool_name=tool_name), self.assertRaisesRegex(ValueError, "name does not match"):
                builder._live_endpoints(
                    transcript("datasage_query", tool_name, denial), prompts, script
                )
        correctly_bound = transcript("datasage_query", "datasage_query", denial)
        builder._live_endpoints(correctly_bound, prompts, script)
        with self.assertRaisesRegex(ValueError, "DATA_ENTITLEMENT_DENIED"):
            builder._reject_entitlement_denial(correctly_bound)

    def test_builder_clarify_reply_contract_rejects_order_and_transcript_tampering(self):
        builder = _builder()
        contract = _live_contract()
        script = contract["clarify_reply_script"]
        prompts = ["first", "second"]

        def pair(call_id, question, choices, response):
            return [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call_id,
                        "function": {
                            "name": "clarify",
                            "arguments": json.dumps({"question": question, "choices": choices}),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "tool_name": "clarify",
                    "content": json.dumps({
                        "question": question,
                        "choices_offered": choices,
                        "user_response": response,
                    }, ensure_ascii=False),
                },
            ]

        valid = [
            {"id": 1, "role": "user", "content": "first"},
            *pair("c1", "Pick one", ["A", "B"], script[0]["fixed_response"]),
            {"id": 4, "role": "assistant", "content": "final one", "tool_calls": None},
            {"id": 5, "role": "user", "content": "second"},
            *pair("c2", "Which entity?", None, script[1]["fixed_response"]),
            {"id": 8, "role": "assistant", "content": "final two", "tool_calls": None},
        ]
        self.assertEqual(2, len(builder._live_endpoints(valid, prompts, script)))

        with_metadata = [
            valid[0],
            valid[1],
            {
                "id": 20,
                "role": "session_meta",
                "content": None,
                "tool_calls": None,
                "tool_call_id": None,
                "tool_name": None,
                "function_call": None,
                "tools": [{"type": "function", "function": {"name": "official_tool"}}],
                "model": "official-model",
                "timestamp": 1.0,
            },
            valid[2],
            valid[3],
            {"id": 21, "role": "session_meta", "content": None, "platform": "wecom"},
            valid[4],
            valid[5],
            valid[6],
            valid[7],
            {"id": 22, "role": "session_meta", "content": None, "tools": []},
        ]
        retained = copy.deepcopy(with_metadata)
        self.assertEqual(2, len(builder._live_endpoints(with_metadata, prompts, script)))
        self.assertEqual(retained, with_metadata)

        adversarial_carriers = (
            ("nonempty-content", {"content": "hidden conversational text"}),
            ("push-tool-calls", {"tool_calls": [{
                "id": "push-1",
                "function": {"name": "datasage_push", "arguments": "{}"},
            }]}),
            ("fake-tool-result", {"tool_call_id": "push-1", "tool_name": "datasage_push"}),
            ("legacy-function-call", {"function_call": {"name": "datasage_push", "arguments": "{}"}}),
        )
        session_meta_index = next(
            index for index, item in enumerate(with_metadata) if item.get("role") == "session_meta"
        )
        for label, carriers in adversarial_carriers:
            adversarial = copy.deepcopy(with_metadata)
            adversarial[session_meta_index].update(carriers)
            unchanged = copy.deepcopy(adversarial)
            with self.subTest(carrier=label), self.assertRaisesRegex(
                ValueError, "session_meta contains conversational or tool-flow payload"
            ):
                builder._live_endpoints(adversarial, prompts, script)
            self.assertEqual(unchanged, adversarial)

        unsupported = copy.deepcopy(with_metadata)
        unsupported.insert(-1, {"id": 23, "role": "audit_meta", "content": None})
        with self.assertRaisesRegex(ValueError, "unsupported conversational role"):
            builder._live_endpoints(unsupported, prompts, script)

        mutations = []

        def mutation(label, callback, pattern):
            value = copy.deepcopy(valid)
            callback(value)
            mutations.append((label, value, pattern))

        mutation("orphan", lambda value: value[2].__setitem__("tool_call_id", "orphan"), "next non-system")
        mutation("wrong-name", lambda value: value[2].__setitem__("tool_name", "datasage_query"), "name does not match")
        mutation("interposed", lambda value: value.insert(2, {"role": "assistant", "content": "interposed", "tool_calls": None}), "next non-system")
        mutation("question", lambda value: value[2].__setitem__("content", json.dumps({"question": "forged", "choices_offered": ["A", "B"], "user_response": script[0]["fixed_response"]}, ensure_ascii=False)), "does not bind")
        mutation("choices", lambda value: value[2].__setitem__("content", json.dumps({"question": "Pick one", "choices_offered": ["B", "A"], "user_response": script[0]["fixed_response"]}, ensure_ascii=False)), "does not bind")
        mutation("response", lambda value: value[2].__setitem__("content", json.dumps({"question": "Pick one", "choices_offered": ["A", "B"], "user_response": "forged"})), "fixed Golden response")
        mutation("extra-result-key", lambda value: value[2].__setitem__("content", json.dumps({"question": "Pick one", "choices_offered": ["A", "B"], "user_response": script[0]["fixed_response"], "extra": True}, ensure_ascii=False)), "official shape")
        mutation("non-json", lambda value: value[2].__setitem__("content", "not-json"), "invalid JSON")
        mutation("duplicate-result-key", lambda value: value[2].__setitem__("content", '{"question":"Pick one","question":"forged","choices_offered":["A","B"],"user_response":"x"}'), "invalid JSON")
        mutation("batch", lambda value: value[1]["tool_calls"][0]["function"].__setitem__("arguments", json.dumps({"question": "batch", "questions": [{"question": "one"}]})), "single-question shape")
        mutation("multi-select", lambda value: value[1]["tool_calls"][0]["function"].__setitem__("arguments", json.dumps({"question": "Pick one", "choices": ["A", "B"], "multi_select": True})), "scalar single-question")
        mutation("duplicate-argument-key", lambda value: value[1]["tool_calls"][0]["function"].__setitem__("arguments", '{"question":"one","question":"two","choices":["A"]}'), "invalid JSON")
        mutation("extra-argument-key", lambda value: value[1]["tool_calls"][0]["function"].__setitem__("arguments", json.dumps({"question": "Pick one", "choices": ["A"], "extra": True})), "single-question shape")
        mutation("ordinary-user-reply", lambda value: value.insert(2, {"id": 99, "role": "user", "content": script[0]["fixed_response"]}), "exactly the two ordered")
        for label, value, pattern in mutations:
            with self.subTest(mutation=label), self.assertRaisesRegex(ValueError, pattern):
                builder._live_endpoints(value, prompts, script)

        missing = [*valid[:2], *valid[3:]]
        with self.assertRaisesRegex(ValueError, "next non-system|unclosed"):
            builder._live_endpoints(missing, prompts, script)

        result_before = [valid[0], valid[2], valid[1], *valid[3:]]
        with self.assertRaisesRegex(ValueError, "not bound"):
            builder._live_endpoints(result_before, prompts, script)

        parallel = copy.deepcopy(valid)
        parallel[1]["tool_calls"].append({
            "id": "parallel",
            "function": {"name": "datasage_catalog", "arguments": "{}"},
        })
        with self.assertRaisesRegex(ValueError, "only tool call"):
            builder._live_endpoints(parallel, prompts, script)

        prior_pending = copy.deepcopy(valid)
        prior_pending.insert(1, {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "ordinary-pending",
                "function": {"name": "datasage_catalog", "arguments": "{}"},
            }],
        })
        prior_pending.insert(4, {
            "role": "tool",
            "tool_call_id": "ordinary-pending",
            "tool_name": "datasage_catalog",
            "content": "{}",
        })
        with self.assertRaisesRegex(ValueError, "prior tool calls"):
            builder._live_endpoints(prior_pending, prompts, script)

        missing_recommended_choices = copy.deepcopy(valid)
        missing_recommended_choices[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
            "question": "Pick one",
        })
        payload = json.loads(missing_recommended_choices[2]["content"])
        payload["choices_offered"] = None
        missing_recommended_choices[2]["content"] = json.dumps(payload, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "did not offer a recommended choice"):
            builder._live_endpoints(missing_recommended_choices, prompts, script)

        extra = copy.deepcopy(valid)
        extra[3:3] = pair("extra", "Again?", ["A"], script[0]["fixed_response"])
        with self.assertRaisesRegex(ValueError, "extra clarify call"):
            builder._live_endpoints(extra, prompts, script)

        for label, callback in (
            ("case-order", lambda value: value["clarify_reply_script"].reverse()),
            ("turn-order", lambda value: value["clarify_reply_script"][0].__setitem__("turn", 2)),
            ("projection-policy", lambda value: value["inbound"].__setitem__("endpoint_projection_policy", "drop_all_metadata")),
        ):
            forged_contract = copy.deepcopy(contract)
            callback(forged_contract)
            with self.subTest(contract=label), self.assertRaises(ValueError):
                builder._validate_live_contract(forged_contract)

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
        self.assertIn("LIVE_RELEASE_GATE_BLOCKED", codes)
        self.assertIn("OUTBOUND_DELIVERY_NOT_VERIFIED", codes)
        self.assertIn("STABILITY_NOT_VERIFIED", codes)
        self.assertEqual("unverified_raw_evidence_required", result["live_model_replay"]["status"])
        self.assertEqual(
            "trusted_human_orchestrated_not_cryptographically_authenticated",
            result["live_model_replay"]["review_assurance"],
        )
        self.assertEqual(
            "best_effort_not_same_user_adversarial",
            result["live_model_replay"]["capture_integrity"],
        )
        self.assertEqual("missing", result["outbound_delivery"]["status"])
        self.assertEqual("missing", result["stability"]["status"])
        self.assertEqual("missing", result["live_replay_runs"]["status"])
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
        live_contract = builder._validate_live_contract(
            builder._read_strict_json(builder.LIVE_RELEASE_CONTRACT)
        )
        self.assertEqual(
            {"name": receipt["name"], "version": receipt["version"]},
            live_contract["subject"],
        )
        self.assertEqual(contract["host"], live_contract["host"])

    def test_raw_wecom_inbound_evidence_derives_live_gates_but_not_outbound(self):
        builder = _builder()
        subject = _subject()
        contract = _live_contract()
        with tempfile.TemporaryDirectory() as temporary:
            evidence_dir = Path(temporary) / "evidence"
            report = _live_report(builder, evidence_dir, subject, contract)
            with (
                mock.patch.object(builder, "EVIDENCE_DIR", evidence_dir),
                mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")),
                mock.patch.object(builder, "_subject_commit_timestamp", return_value=builder._live_timestamp("2026-08-28T00:00:00+00:00", "test")),
                mock.patch.object(builder, "_validate_wecom_session_origin", side_effect=lambda exported, *_args, **_kwargs: exported["id"]),
                mock.patch.object(builder, "_is_read_only", return_value=True),
                mock.patch.object(builder, "_current_python_provenance", return_value=report["python_provenance"]["before"]),
            ):
                result = builder.evaluate_release_gates(
                    _live_manifest(contract),
                    _host_fixture(),
                    version=subject["version"],
                    subject=subject,
                    profile_git_commit=TEST_PROFILE_COMMIT,
                    hermes_git_commit=TEST_HERMES_COMMIT,
                    live_evidence=report,
                    live_contract=contract,
                )
        codes = {item["code"] for item in result["blockers"]}
        self.assertEqual("passed", result["live_model_replay"]["status"])
        self.assertEqual(
            "trusted_human_orchestrated_not_cryptographically_authenticated",
            result["live_model_replay"]["review_assurance"],
        )
        self.assertEqual(
            "best_effort_not_same_user_adversarial",
            result["live_model_replay"]["capture_integrity"],
        )
        self.assertEqual("missing", result["outbound_delivery"]["status"])
        self.assertEqual("protocol_or_api_ack_not_user_read", result["outbound_delivery"]["evidence_semantics"])
        self.assertEqual("passed", result["stability"]["status"])
        self.assertEqual("complete", result["live_replay_runs"]["status"])
        self.assertEqual(
            {
                "OUTBOUND_DELIVERY_NOT_VERIFIED",
                "HOST_COMPACTION_NOT_VERIFIED",
                "PERFORMANCE_COST_NOT_VERIFIED",
            },
            codes,
        )

    def test_review_excerpt_binding_and_internal_label_laundering_are_rejected(self):
        builder = _builder()
        labels = ["report_metrics_individually"]
        answer = "各项业务指标会分别呈现，而且不会合成为未经治理的总分。"
        excerpt = "各项业务指标会分别呈现"
        start = answer.index(excerpt)
        review = {
            "labels": labels,
            "evidence": [{
                "label": labels[0], "start": start, "end": start + len(excerpt),
                "text_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            }],
        }
        builder._validate_review_evidence(review, answer, labels)
        with self.assertRaises(ValueError):
            builder._validate_review_evidence(review, "dummy answer", labels)
        suite = json.loads(builder.GOLDEN_SUITE.read_text(encoding="utf-8"))
        golden = next(case for case in suite["cases"] if case["id"] == "ambiguity_10_rc5_vietnam_scorecard")
        copied_excerpt = "人工审查提供了如下证据片段"
        copied = copied_excerpt + "：" + "；".join(
            [*golden["required_conclusions"], *golden["allowed_conclusions"], *golden["forbidden_conclusions"]]
        )
        copied_review = {
            "labels": golden["required_conclusions"],
            "evidence": [
                {
                    "label": label,
                    "start": copied.index(copied_excerpt),
                    "end": copied.index(copied_excerpt) + len(copied_excerpt),
                    "text_sha256": hashlib.sha256(copied_excerpt.encode("utf-8")).hexdigest(),
                }
                for label in golden["required_conclusions"]
            ],
        }
        builder._validate_review_evidence(copied_review, copied, golden["required_conclusions"])
        with self.assertRaises(ValueError):
            builder._reject_internal_conclusion_codes(copied, golden)

    def test_python_provenance_detects_replaced_interpreter_pth_and_sitecustomize(self):
        builder = _builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "python.exe"
            executable.write_bytes(b"trusted-python")
            original_python = builder._file_identity(executable)
            executable.write_bytes(b"forged-python!")
            self.assertNotEqual(original_python, builder._file_identity(executable))

            site_dir = root / "site-packages"
            site_dir.mkdir()
            original_controls = builder._python_path_controls([site_dir], root)
            (site_dir / "temporary-injection.pth").write_text(str(root), encoding="utf-8")
            self.assertNotEqual(original_controls, builder._python_path_controls([site_dir], root))
            (site_dir / "sitecustomize.py").write_text("raise RuntimeError('injected')", encoding="utf-8")
            controls = builder._python_path_controls([site_dir], root)
            self.assertTrue(controls[0]["customization"]["sitecustomize.py"]["exists"])
            self.assertRegex(
                controls[0]["customization"]["sitecustomize.py"]["identity"]["sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_real_pinned_venv_provenance_smoke_and_tracked_approval(self):
        builder = _builder()
        contract = builder._validate_live_contract(builder._read_strict_json(builder.LIVE_RELEASE_CONTRACT))
        code = (
            "import importlib.util,pathlib;"
            f"p=pathlib.Path({str(PROFILE_ROOT / 'build_release_receipt.py')!r});"
            "s=importlib.util.spec_from_file_location('live_builder_smoke',p);"
            "b=importlib.util.module_from_spec(s);s.loader.exec_module(b);"
            "c=b._validate_live_contract(b._read_strict_json(b.LIVE_RELEASE_CONTRACT));"
            "x=b._current_python_provenance('runner',c['python_provenance_approval']);"
            "b._validate_python_provenance(x,'real pinned venv',c['python_provenance_approval']);"
            "print(len(x['sys_path_entry_sha256']),len(x['pth_import_payloads']))"
        )
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("PYTHON")
        }
        environment.update({
            "PYTHONPATH": str(builder._hermes_source_root()),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        result = subprocess.run(
            [str(builder._canonical_hermes_python()), "-B", "-c", code],
            cwd=PROFILE_ROOT / "tests", env=environment, capture_output=True, text=True,
            timeout=10, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("12 3", result.stdout.strip())

        with tempfile.TemporaryDirectory() as temporary:
            proof = _live_report(builder, Path(temporary) / "evidence", _subject(), _live_contract())["python_provenance"]["before"]
        forged = copy.deepcopy(proof)
        forged["pth_import_payloads"][0]["sha256"] = "9" * 64
        with mock.patch.object(builder, "_current_python_provenance", return_value=forged):
            with self.assertRaises(ValueError):
                builder._validate_python_provenance(
                    forged, "pre-existing replaced .pth payload", contract["python_provenance_approval"]
                )
        forged = copy.deepcopy(proof)
        forged["path_controls"][0]["pth"].append({"path": "Lib/site-packages/injected.pth", "sha256": "9" * 64, "bytes": 1})
        with mock.patch.object(builder, "_current_python_provenance", return_value=forged):
            with self.assertRaises(ValueError):
                builder._validate_python_provenance(
                    forged, "pre-existing added .pth", contract["python_provenance_approval"]
                )

    def test_runtime_path_and_private_artifact_ancestor_boundaries(self):
        builder = _builder()
        hermes_root = builder._hermes_source_root().resolve()
        prefix = builder._canonical_hermes_python().parent.parent.resolve()
        base = Path(sys.base_prefix).resolve()
        paths = [
            (builder.ROOT / "tests").resolve(),
            hermes_root,
            base / "python313.zip",
            base / "DLLs",
            base / "Lib",
            base,
            prefix,
            prefix / "Lib" / "site-packages",
            builder.ROOT.resolve() / "__editable__.hermes_agent-0.20.5.finder.__path_hook__",
            prefix / "Lib" / "site-packages" / "win32",
            prefix / "Lib" / "site-packages" / "win32" / "lib",
            prefix / "Lib" / "site-packages" / "Pythonwin",
        ]
        paths = [path.resolve() for path in paths]
        builder._validate_runtime_sys_paths(paths, "runner")
        with self.assertRaises(ValueError):
            builder._validate_runtime_sys_paths([*paths, prefix / "unapproved-shadow-path"], "runner")
        with self.assertRaises(ValueError):
            builder._validate_runtime_sys_paths([*paths, prefix / "Lib"], "runner")
        reordered = [*paths]
        reordered[2], reordered[3] = reordered[3], reordered[2]
        with self.assertRaises(ValueError):
            builder._validate_runtime_sys_paths(reordered, "runner")
        with tempfile.TemporaryDirectory() as temporary:
            evidence_dir = Path(temporary) / "evidence"
            expected_parent = evidence_dir / "private" / ("1" * 40) / "run-1"
            expected_parent.mkdir(parents=True)
            artifact = expected_parent / "captured.stdout"
            artifact.write_bytes(b"retained")
            with mock.patch.object(builder, "EVIDENCE_DIR", evidence_dir):
                builder._validate_private_evidence_path(artifact, expected_parent, "test artifact")
                with mock.patch.object(
                    builder, "_is_reparse_point",
                    side_effect=lambda path: path == evidence_dir / "private",
                ):
                    with self.assertRaises(ValueError):
                        builder._validate_private_evidence_path(artifact, expected_parent, "ancestor reparse")
                with self.assertRaises(ValueError):
                    builder._validate_private_evidence_path(artifact, expected_parent.parent, "wrong parent")

    def test_live_evidence_rejects_tampering_and_maps_back_to_five_blockers(self):
        builder = _builder()
        subject = _subject()
        contract = _live_contract()
        def mutate_capture(evidence, root, callback):
            path = root / evidence["capture"]["path"]
            capture = json.loads(path.read_text(encoding="utf-8"))
            callback(capture)
            path.write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")
            payload = path.read_bytes()
            evidence["capture"].update({"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})

        def bool_exit(evidence, root):
            mutate_capture(evidence, root, lambda capture: capture["runs"][0]["processes"]["session_export"].__setitem__("exit_code", True))

        def duplicate_run(evidence, _root):
            evidence["runs"][1]["run_index"] = 1

        def stale_commit(evidence, _root):
            evidence["subject"]["profile_git_commit"] = "9" * 40

        def forged_pass_summary(evidence, _root):
            evidence["runs"][0]["scorer_report"] = {"passed": True}

        def forged_argv(evidence, root):
            def change(capture):
                record = capture["runs"][0]["processes"]["session_export"]
                token = next(item for item in record["argv"] if isinstance(item, dict) and item.get("binding") == "exact_session_id")
                token["value_sha256"] = "9" * 64
                record["argv_sha256"] = builder._sha256_bytes(builder._canonical_json_bytes(record["argv"]))
            mutate_capture(evidence, root, change)

        def forged_export_hash(evidence, root):
            mutate_capture(evidence, root, lambda capture: capture["runs"][0]["session"]["export"].__setitem__("sha256", "9" * 64))

        def mutate_json_ref(evidence, root, ref, callback):
            path = root / ref["path"]
            value = json.loads(path.read_text(encoding="utf-8"))
            callback(value)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            payload = path.read_bytes()
            ref.update({"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})

        def sync_capture_and_reviews(evidence, root, capture):
            for captured in capture["runs"]:
                refs = [
                    captured["processes"]["session_export"]["stdout"],
                    captured["processes"]["session_export"]["stderr"],
                ]
                refs.append(captured["session"]["export"])
                captured["artifact_set_sha256"] = builder._sha256_bytes(builder._canonical_json_bytes(refs))
            capture_path = root / evidence["capture"]["path"]
            capture_path.write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")
            capture_payload = capture_path.read_bytes()
            capture_sha = hashlib.sha256(capture_payload).hexdigest()
            evidence["capture"].update({"sha256": capture_sha, "bytes": len(capture_payload)})
            digest_path = root / evidence["capture_digest"]["path"]
            digest_path.write_text(capture_sha + "\n", encoding="ascii", newline="")
            digest_payload = digest_path.read_bytes()
            evidence["capture_digest"].update({"sha256": hashlib.sha256(digest_payload).hexdigest(), "bytes": len(digest_payload)})
            for run in evidence["runs"]:
                review_path = root / run["reviews"]["path"]
                review_set = json.loads(review_path.read_text(encoding="utf-8"))
                for review in review_set["reviews"]:
                    review["capture_sha256"] = capture_sha
                review_path.write_text(json.dumps(review_set, ensure_ascii=False), encoding="utf-8")
                review_payload = review_path.read_bytes()
                run["reviews"].update({"sha256": hashlib.sha256(review_payload).hexdigest(), "bytes": len(review_payload)})

        def review_wrong_session(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], lambda value: value["reviews"][0].__setitem__("session_id_sha256", "9" * 64))

        def review_wrong_answer(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], lambda value: value["reviews"][0].__setitem__("final_answer_sha256", "9" * 64))

        def review_wrong_run(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], lambda value: value["reviews"][0].__setitem__("run_index", 2))

        def review_plan_trace(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], lambda value: value["reviews"][0].__setitem__("plan_trace", {"status": "passed"}))

        def duplicate_review(evidence, root):
            def change(value):
                value["reviews"][1] = copy.deepcopy(value["reviews"][0])
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], change)

        def review_disagrees(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], lambda value: value["reviews"][1]["labels"].reverse())

        def review_excerpt_hash_forged(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], lambda value: value["reviews"][0]["evidence"][0].__setitem__("text_sha256", "9" * 64))

        def static_case_review(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["reviews"], lambda value: value["reviews"][0].pop("run_index"))

        def candidate_bool_message_id(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["candidate"], lambda value: value["canary_receipt"]["turns"][0].__setitem__("user_message_id", True))

        def candidate_unknown_field(evidence, root):
            mutate_json_ref(evidence, root, evidence["runs"][0]["candidate"], lambda value: value.__setitem__("eligible", True))

        def duplicate_export_key(evidence, root):
            capture_path = root / evidence["capture"]["path"]
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            export_ref = capture["runs"][0]["session"]["export"]
            export_path = root / export_ref["path"]
            session = json.loads(export_path.read_text(encoding="utf-8"))
            raw = '{"id":' + json.dumps(session["id"]) + ',"messages":[],"messages":' + json.dumps(session["messages"], ensure_ascii=False) + '}\n'
            export_path.write_text(raw, encoding="utf-8")
            payload = export_path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            export_ref.update({"sha256": digest, "bytes": len(payload)})
            stdout_ref = capture["runs"][0]["processes"]["session_export"]["stdout"]
            stdout_path = root / stdout_ref["path"]
            stdout_path.write_bytes(payload)
            stdout_ref.update({"sha256": digest, "bytes": len(payload)})
            captured = capture["runs"][0]
            refs = [
                captured["processes"]["session_export"]["stdout"],
                captured["processes"]["session_export"]["stderr"],
            ]
            refs.append(captured["session"]["export"])
            captured["artifact_set_sha256"] = builder._sha256_bytes(builder._canonical_json_bytes(refs))
            capture_path.write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")
            capture_payload = capture_path.read_bytes()
            capture_sha = hashlib.sha256(capture_payload).hexdigest()
            evidence["capture"].update({"sha256": capture_sha, "bytes": len(capture_payload)})
            digest_path = root / evidence["capture_digest"]["path"]
            digest_path.write_text(capture_sha + "\n", encoding="ascii", newline="")
            digest_payload = digest_path.read_bytes()
            evidence["capture_digest"].update({"sha256": hashlib.sha256(digest_payload).hexdigest(), "bytes": len(digest_payload)})
            for run in evidence["runs"]:
                review_path = root / run["reviews"]["path"]
                review_set = json.loads(review_path.read_text(encoding="utf-8"))
                for review in review_set["reviews"]:
                    review["capture_sha256"] = capture_sha
                review_path.write_text(json.dumps(review_set, ensure_ascii=False), encoding="utf-8")
                review_payload = review_path.read_bytes()
                run["reviews"].update({"sha256": hashlib.sha256(review_payload).hexdigest(), "bytes": len(review_payload)})

        def extra_old_user_history(evidence, root):
            capture_path = root / evidence["capture"]["path"]
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            export_ref = capture["runs"][0]["session"]["export"]
            export_path = root / export_ref["path"]
            session = json.loads(export_path.read_text(encoding="utf-8"))
            session["messages"].insert(0, {
                "id": 999,
                "role": "user",
                "tool_name": None,
                "tool_call_id": None,
                "tool_calls": None,
                "content": "older unrelated user history",
            })
            payload = (json.dumps(session, ensure_ascii=False) + "\n").encode("utf-8")
            export_path.write_bytes(payload)
            export_ref.update({"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})
            stdout_ref = capture["runs"][0]["processes"]["session_export"]["stdout"]
            stdout_path = root / stdout_ref["path"]
            stdout_path.write_bytes(payload)
            stdout_ref.update({"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})
            sync_capture_and_reviews(evidence, root, capture)

        def synchronized_capture_rewrite_without_external_review(evidence, root):
            capture_path = root / evidence["capture"]["path"]
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            capture["runs"][0]["processes"]["session_export"]["duration_ns"] += 1
            capture_path.write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")
            payload = capture_path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            evidence["capture"].update({"sha256": digest, "bytes": len(payload)})
            digest_path = root / evidence["capture_digest"]["path"]
            digest_path.write_text(digest + "\n", encoding="ascii", newline="")
            digest_payload = digest_path.read_bytes()
            evidence["capture_digest"].update({"sha256": hashlib.sha256(digest_payload).hexdigest(), "bytes": len(digest_payload)})

        def replaced_python_proof(evidence, _root):
            evidence["python_provenance"]["before"]["executable"]["sha256"] = "9" * 64

        def rescored_candidate_failure(evidence, root):
            ref = evidence["runs"][0]["candidate"]
            path = root / ref["path"]
            candidate = json.loads(path.read_text(encoding="utf-8"))
            candidate["cases"][0]["conclusions"].append("invent_result")
            turn = candidate["canary_receipt"]["turns"][0]
            turn["candidate_case_sha256"] = builder._sha256_bytes(
                builder._canonical_json_bytes(candidate["cases"][0])
            )
            receipt = candidate["canary_receipt"]
            receipt["candidate_cases_sha256"] = builder._sha256_bytes(
                builder._canonical_json_bytes(candidate["cases"])
            )
            receipt_body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
            receipt["receipt_sha256"] = builder._sha256_bytes(builder._canonical_json_bytes(receipt_body))
            path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            ref.update({"sha256": digest, "bytes": len(payload)})

        mutations = (
            bool_exit, duplicate_run, stale_commit, forged_pass_summary,
            forged_argv, forged_export_hash, rescored_candidate_failure,
            review_wrong_session, review_wrong_answer, review_wrong_run,
            review_plan_trace, duplicate_review, review_disagrees, review_excerpt_hash_forged, static_case_review,
            candidate_bool_message_id, candidate_unknown_field, duplicate_export_key, extra_old_user_history,
            synchronized_capture_rewrite_without_external_review, replaced_python_proof,
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate.__name__), tempfile.TemporaryDirectory() as temporary:
                evidence_dir = Path(temporary) / "evidence"
                evidence = _live_report(builder, evidence_dir, subject, contract)
                expected_python = copy.deepcopy(evidence["python_provenance"]["before"])
                mutate(evidence, evidence_dir)
                with (
                    mock.patch.object(builder, "EVIDENCE_DIR", evidence_dir),
                    mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")),
                    mock.patch.object(builder, "_subject_commit_timestamp", return_value=builder._live_timestamp("2026-08-28T00:00:00+00:00", "test")),
                    mock.patch.object(builder, "_validate_wecom_session_origin", side_effect=lambda exported, *_args, **_kwargs: exported["id"]),
                    mock.patch.object(builder, "_is_read_only", return_value=True),
                    mock.patch.object(builder, "_current_python_provenance", return_value=expected_python),
                ):
                    result = builder.evaluate_release_gates(
                        _live_manifest(contract),
                        _host_fixture(),
                        version=subject["version"],
                        subject=subject,
                        profile_git_commit=TEST_PROFILE_COMMIT,
                        hermes_git_commit=TEST_HERMES_COMMIT,
                        live_evidence=evidence,
                        live_contract=contract,
                    )
                codes = {item["code"] for item in result["blockers"]}
                self.assertIn("LIVE_RELEASE_EVIDENCE_INVALID", codes)
                self.assertTrue({code for code, _ in builder.LIVE_BLOCKERS}.issubset(codes))
                self.assertEqual("invalid", result["live_model_replay"]["status"])
                self.assertEqual(
                    "trusted_human_orchestrated_not_cryptographically_authenticated",
                    result["live_model_replay"]["review_assurance"],
                )
                self.assertEqual(
                    "best_effort_not_same_user_adversarial",
                    result["live_model_replay"]["capture_integrity"],
                )

    def test_live_capture_readonly_and_python_customization_fail_closed(self):
        builder = _builder()
        subject, contract = _subject(), _live_contract()
        for readonly, python_error in ((False, None), (True, ValueError("sitecustomize is forbidden"))):
            with self.subTest(readonly=readonly, python_error=python_error), tempfile.TemporaryDirectory() as temporary:
                evidence_dir = Path(temporary) / "evidence"
                evidence = _live_report(builder, evidence_dir, subject, contract)
                python_patch = (
                    mock.patch.object(builder, "_current_python_provenance", side_effect=python_error)
                    if python_error else
                    mock.patch.object(builder, "_current_python_provenance", return_value=evidence["python_provenance"]["before"])
                )
                with (
                    mock.patch.object(builder, "EVIDENCE_DIR", evidence_dir),
                    mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")),
                    mock.patch.object(builder, "_subject_commit_timestamp", return_value=builder._live_timestamp("2026-08-28T00:00:00+00:00", "test")),
                    mock.patch.object(builder, "_validate_wecom_session_origin", side_effect=lambda exported, *_args, **_kwargs: exported["id"]),
                    mock.patch.object(builder, "_is_read_only", return_value=readonly),
                    python_patch,
                ):
                    result = builder.evaluate_release_gates(
                        _live_manifest(contract), _host_fixture(), version=subject["version"],
                        subject=subject, profile_git_commit=TEST_PROFILE_COMMIT,
                        hermes_git_commit=TEST_HERMES_COMMIT, live_evidence=evidence, live_contract=contract,
                    )
                self.assertIn("LIVE_RELEASE_EVIDENCE_INVALID", {item["code"] for item in result["blockers"]})

    def test_wecom_inbound_actual_identity_must_match_anonymous_contract_pin(self):
        builder = _builder()
        subject, contract = _subject(), _live_contract()
        with tempfile.TemporaryDirectory() as temporary:
            evidence_dir = Path(temporary) / "evidence"
            evidence = _live_report(builder, evidence_dir, subject, contract)
            with (
                mock.patch.object(builder, "EVIDENCE_DIR", evidence_dir),
                mock.patch.object(builder, "_validate_hashed_source", return_value=Path("checked")),
                mock.patch.object(builder, "_subject_commit_timestamp", return_value=builder._live_timestamp("2026-08-28T00:00:00+00:00", "test")),
                mock.patch.object(builder, "_is_read_only", return_value=True),
                mock.patch.object(builder, "_current_python_provenance", return_value=evidence["python_provenance"]["before"]),
            ):
                result = builder.evaluate_release_gates(
                    _live_manifest(contract), _host_fixture(), version=subject["version"],
                    subject=subject, profile_git_commit=TEST_PROFILE_COMMIT,
                    hermes_git_commit=TEST_HERMES_COMMIT,
                    live_evidence=evidence, live_contract=contract,
                )
        self.assertIn("LIVE_RELEASE_EVIDENCE_INVALID", {item["code"] for item in result["blockers"]})

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
