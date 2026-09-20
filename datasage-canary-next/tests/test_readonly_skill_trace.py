"""Offline, opt-in validation of read-only DataSage Skill calls.

These tests synthesize the shape emitted by ``hermes sessions export --format
jsonl``.  They never start Hermes, read a real Home, call a model, open a
database, or send a message.  The normal business replay validator remains
strictly DataSage-only; this file exercises the explicitly opt-in extension.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import copy
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _load_replay():
    path = ROOT / "tests" / "business_replay.py"
    spec = importlib.util.spec_from_file_location("datasage_business_replay", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPLAY = _load_replay()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _export(calls, *, session_id="readonly-session", final="完成"):
    messages = [
        {"id": 1, "session_id": session_id, "active": 1, "role": "system", "content": "system"},
        {"id": 2, "session_id": session_id, "active": 1, "role": "user", "content": "越南今年的经营情况"},
    ]
    message_id = 3
    for index, call in enumerate(calls, 1):
        call_id = call.get("call_id", f"call-{index}")
        name = call["name"]
        arguments = call.get("arguments", {})
        result = call.get("result", {"success": True})
        raw_result = result if isinstance(result, str) else _json(result)
        messages.append({
            "id": message_id,
            "session_id": session_id,
            "active": 1,
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": _json(arguments)},
            }],
        })
        message_id += 1
        messages.append({
            "id": message_id,
            "session_id": session_id,
            "active": 1,
            "role": "tool",
            "content": raw_result,
            "tool_call_id": call_id,
            "tool_name": name,
        })
        message_id += 1
    messages.append({
        "id": message_id,
        "session_id": session_id,
        "active": 1,
        "role": "assistant",
        "content": final,
        "tool_calls": [],
    })
    return (_json({"id": session_id, "messages": messages}) + "\n").encode("utf-8")


class ReadonlySkillTraceTests(unittest.TestCase):
    def test_opt_in_is_required_and_old_validator_stays_strict(self):
        payload = _export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "rules"},
            }
        ])
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "explicit"):
            REPLAY.validate_readonly_skill_trace(payload)
        for value in (1, "true"):
            with self.subTest(opt_in=value):
                with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "explicit"):
                    REPLAY.validate_readonly_skill_trace(
                        payload,
                        allow_readonly_skills=value,
                    )

        _, messages = REPLAY._export_session(payload)
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "unapproved"):
            REPLAY._validate_live_turn_tool_flow(messages[2:4])

        policy = [{
            "case_id": "skill-opt-in",
            "turn": 1,
            "assistant_completion": "ordinary_text",
            "maximum_blocking_clarify_calls": 0,
        }]
        with self.assertRaises(REPLAY._LiveTranscriptPolicyError):
            REPLAY._endpoints(payload_messages := messages, ["越南今年的经营情况"], policy)
        for value in (1, "true"):
            with self.subTest(endpoint_opt_in=value):
                with self.assertRaises(REPLAY._LiveTranscriptPolicyError):
                    REPLAY._endpoints(
                        payload_messages, ["越南今年的经营情况"], policy,
                        allow_readonly_skills=value,
                    )
        endpoints = REPLAY._endpoints(
            payload_messages,
            ["越南今年的经营情况"],
            policy,
            allow_readonly_skills=True,
        )
        self.assertEqual([(2, 5)], [item[:2] for item in endpoints])

    def test_valid_opt_in_trace_keeps_export_and_call_id_evidence(self):
        payload = _export([
            {
                "name": "skills_list",
                "arguments": {"category": "business-analytics"},
                "result": {"success": True, "skills": [{"name": "datasage"}]},
            },
            {
                "name": "skill_view",
                "call_id": "skill-main",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "main"},
            },
            {
                "name": "skill_view",
                "call_id": "skill-rules",
                "arguments": {"name": "datasage", "file_path": "references/query-rules.md"},
                "result": {
                    "success": True,
                    "name": "datasage",
                    "file": "references/query-rules.md",
                    "content": "query rules",
                },
            },
            {
                "name": "datasage_query",
                "call_id": "query-1",
                "arguments": {"requests": []},
                "result": {},
            },
        ])
        original = bytes(payload)
        trace = REPLAY.validate_readonly_skill_trace(
            payload,
            allow_readonly_skills=True,
            case_id="ambiguity_10_rc5_vietnam_scorecard",
            tool_snapshot=[
                "datasage_catalog",
                "datasage_entity_resolve",
                "datasage_query",
                "skills_list",
                "skill_view",
            ],
        )

        self.assertEqual(original, payload)
        self.assertEqual(REPLAY.READONLY_SKILL_TRACE_SCHEMA, trace["schema"])
        self.assertEqual("ambiguity_10_rc5_vietnam_scorecard", trace["case_id"])
        self.assertEqual("caller_label_unbound", trace["case_binding"])
        self.assertEqual("readonly-session", trace["source"]["session_id"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), trace["source"]["session_export_sha256"])
        self.assertEqual(
            "provided_unbound_no_skill_edit_or_exec",
            trace["tool_surface"]["status"],
        )
        self.assertEqual("not_provided", trace["tool_surface"]["source_binding"])
        self.assertTrue(trace["calls_only_allowed"])
        self.assertFalse(trace["skill_method_use_observed"])
        self.assertEqual("not_observed", trace["runtime_side_effects_observed"])
        self.assertTrue(trace["business_replay_required"])
        self.assertEqual(3, trace["skill_call_count"])
        by_id = {call["call_id"]: call for call in trace["calls"]}
        self.assertEqual("listed", by_id["call-1"]["read_status"])
        self.assertEqual("read_success", by_id["skill-main"]["read_status"])
        self.assertEqual("read_success", by_id["skill-rules"]["read_status"])
        self.assertEqual("datasage", by_id["skill-rules"]["skill_name"])
        self.assertEqual("references/query-rules.md", by_id["skill-rules"]["skill_file_path"])
        self.assertEqual(
            hashlib.sha256(b"query rules").hexdigest(),
            by_id["skill-rules"]["content_sha256"],
        )
        self.assertEqual(9, by_id["query-1"]["assistant_message_id"])
        self.assertEqual(10, by_id["query-1"]["tool_message_id"])

    def test_failed_skill_read_is_observed_without_becoming_success(self):
        trace = REPLAY.validate_readonly_skill_trace(
            _export([
                {
                    "name": "skill_view",
                    "arguments": {"name": "datasage", "file_path": "references/entity-guidance.md"},
                    "result": {"success": False, "error": "file unavailable"},
                }
            ]),
            allow_readonly_skills=True,
        )
        self.assertEqual("failed", trace["calls"][0]["read_status"])
        self.assertIsNone(trace["calls"][0]["content_sha256"])
        self.assertFalse(trace["skill_method_use_observed"])

    def test_binary_placeholder_and_dedup_stub_are_not_new_method_reads(self):
        binary = REPLAY.validate_readonly_skill_trace(
            _export([
                {
                    "name": "skill_view",
                    "arguments": {
                        "name": "datasage",
                        "file_path": "references/query-rules.md",
                    },
                    "result": {
                        "success": True,
                        "name": "datasage",
                        "file": "references/query-rules.md",
                        "is_binary": True,
                        "content": "[Binary file: query-rules.md, size: 5 bytes]",
                    },
                }
            ]),
            allow_readonly_skills=True,
        )
        self.assertEqual("binary_placeholder", binary["calls"][0]["read_status"])
        self.assertIsNone(binary["calls"][0]["content_sha256"])
        self.assertFalse(binary["skill_method_use_observed"])

        dedup = REPLAY.validate_readonly_skill_trace(
            _export([
                {
                    "name": "skill_view",
                    "arguments": {
                        "name": "datasage",
                        "file_path": "references/query-rules.md",
                    },
                    "result": {
                        "success": True,
                        "status": "unchanged",
                        "name": "datasage",
                        "file": "references/query-rules.md",
                        "dedup": True,
                        "content_returned": False,
                        "message": "unchanged; refer to earlier result",
                    },
                }
            ]),
            allow_readonly_skills=True,
        )
        self.assertEqual("cached_not_reobserved", dedup["calls"][0]["read_status"])
        self.assertIsNone(dedup["calls"][0]["content_sha256"])
        self.assertFalse(dedup["skill_method_use_observed"])

    def test_empty_skill_list_is_listed_but_does_not_prove_datasage_discovery(self):
        trace = REPLAY.validate_readonly_skill_trace(
            _export([
                {
                    "name": "skills_list",
                    "arguments": {},
                    "result": {"success": True, "skills": []},
                }
            ]),
            allow_readonly_skills=True,
        )
        self.assertEqual("listed", trace["calls"][0]["read_status"])
        self.assertFalse(trace["calls"][0]["datasage_discovered"])
        self.assertFalse(trace["skill_discovery_observed"])

    def test_json_string_tool_calls_are_parsed_on_a_copy(self):
        decoded = json.loads(_export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "rules"},
            }
        ]).decode("utf-8"))
        decoded["messages"][2]["tool_calls"] = _json(decoded["messages"][2]["tool_calls"])
        payload = (_json(decoded) + "\n").encode("utf-8")
        original = bytes(payload)
        trace = REPLAY.validate_readonly_skill_trace(payload, allow_readonly_skills=True)
        self.assertEqual(original, payload)
        self.assertEqual("read_success", trace["calls"][0]["read_status"])

    def test_default_validator_keeps_string_and_empty_tool_call_rejections(self):
        decoded = json.loads(_export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "rules"},
            }
        ]).decode("utf-8"))
        decoded["messages"][2]["tool_calls"] = _json(decoded["messages"][2]["tool_calls"])
        _, messages = REPLAY._export_session((_json(decoded) + "\n").encode("utf-8"))
        with self.assertRaises(REPLAY._LiveTranscriptPolicyError):
            REPLAY._validate_live_turn_tool_flow(messages[2:4])

        decoded["messages"][2]["tool_calls"] = ""
        _, messages = REPLAY._export_session((_json(decoded) + "\n").encode("utf-8"))
        with self.assertRaises(REPLAY._LiveTranscriptPolicyError):
            REPLAY._validate_live_turn_tool_flow(messages[2:4])

    def test_forbidden_or_unknown_calls_fail_even_when_tool_returns_error(self):
        for name in ("skill_manage", "terminal", "execute_code", "clarify", "unknown_tool"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    REPLAY._LiveTranscriptPolicyError,
                    "unapproved|blocking clarify",
                ):
                    REPLAY.validate_readonly_skill_trace(
                        _export([
                            {
                                "name": name,
                                "arguments": {},
                                "result": {"success": False, "error": "denied"},
                            }
                        ]),
                        allow_readonly_skills=True,
                    )

    def test_skill_view_rejects_internal_or_out_of_scope_arguments(self):
        invalid = [
            {"name": "datasage", "preprocess": False},
            {"name": "other-skill"},
            {"name": "datasage", "file_path": "../.env"},
            {"name": "datasage", "file_path": "C:/secret.txt"},
            {"name": "datasage", "file_path": "references/unknown.md"},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "skill_view"):
                    REPLAY.validate_readonly_skill_trace(
                        _export([
                            {
                                "name": "skill_view",
                                "arguments": arguments,
                                "result": {"success": False, "error": "rejected"},
                            }
                        ]),
                        allow_readonly_skills=True,
                    )

    def test_pseudo_success_and_malformed_failure_envelopes_fail_closed(self):
        invalid_results = [
            {"success": True, "name": "datasage"},
            {"success": True, "name": "datasage", "content": 7},
            {"success": False},
        ]
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaises(REPLAY._LiveTranscriptPolicyError):
                    REPLAY.validate_readonly_skill_trace(
                        _export([
                            {
                                "name": "skill_view",
                                "arguments": {"name": "datasage"},
                                "result": result,
                            }
                        ]),
                        allow_readonly_skills=True,
                    )
        with self.assertRaises(REPLAY._LiveTranscriptPolicyError):
            REPLAY.validate_readonly_skill_trace(
                _export([
                    {
                        "name": "skills_list",
                        "arguments": {},
                        "result": {"success": True},
                    }
                ]),
                allow_readonly_skills=True,
            )

    def test_call_result_identity_and_duplicates_are_bound(self):
        payload = _export([
            {
                "name": "skill_view",
                "call_id": "duplicate",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "one"},
            },
            {
                "name": "skill_view",
                "call_id": "duplicate",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "two"},
            },
        ])
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "duplicate"):
            REPLAY.validate_readonly_skill_trace(payload, allow_readonly_skills=True)

        missing = _export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "ok"},
            }
        ])
        decoded = json.loads(missing.decode("utf-8"))
        decoded["messages"][3]["tool_call_id"] = "wrong-result-id"
        with self.assertRaises(REPLAY._LiveTranscriptPolicyError):
            REPLAY.validate_readonly_skill_trace(
                (_json(decoded) + "\n").encode("utf-8"),
                allow_readonly_skills=True,
            )

    def test_full_surface_requires_a_supplied_snapshot_and_rejects_forbidden_names(self):
        payload = _export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "ok"},
            }
        ])
        absent = REPLAY.validate_readonly_skill_trace(payload, allow_readonly_skills=True)
        self.assertEqual("not_observed", absent["tool_surface"]["status"])
        clarify_surface = REPLAY.validate_readonly_skill_trace(
            payload,
            allow_readonly_skills=True,
            tool_snapshot=["skills_list", "skill_view", "clarify"],
        )
        self.assertEqual(
            "provided_unbound_no_skill_edit_or_exec",
            clarify_surface["tool_surface"]["status"],
        )
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "tool_snapshot"):
            REPLAY.validate_readonly_skill_trace(
                payload,
                allow_readonly_skills=True,
                tool_snapshot=["skills_list", "skill_view", "skill_manage"],
            )
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "tool_snapshot"):
            REPLAY.validate_readonly_skill_trace(
                payload,
                allow_readonly_skills=True,
                tool_snapshot=["skills_list", "skill_view", "terminal"],
            )
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "missing observed"):
            REPLAY.validate_readonly_skill_trace(
                payload,
                allow_readonly_skills=True,
                tool_snapshot=["skills_list"],
            )

    def test_existing_wecom_snapshot_audits_execute_exposure_without_readonly_claim(self):
        payload = _export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "ok"},
            }
        ])
        existing_wecom = [
            "clarify",
            "datasage_catalog",
            "datasage_entity_resolve",
            "datasage_query",
            "execute_code",
            "skill_view",
            "skills_list",
        ]
        trace = REPLAY.validate_readonly_skill_trace(
            payload,
            allow_readonly_skills=True,
            tool_snapshot=existing_wecom,
            snapshot_scope="existing_wecom_surface",
        )
        self.assertEqual(
            "provided_unbound_existing_wecom_surface",
            trace["tool_surface"]["status"],
        )
        self.assertEqual(["execute_code"], trace["tool_surface"]["execution_tools_exposed"])
        self.assertEqual("not_certified", trace["tool_surface"]["whole_surface_readonly"])
        self.assertEqual("not_observed", trace["tool_surface"]["execution_isolation"])
        self.assertEqual("existing_wecom_surface", trace["tool_surface"]["snapshot_scope"])

        # The snapshot scope never widens actual transcript calls.  A
        # returned error from execute_code is still an execution attempt and
        # must remain outside this Skill trace policy.
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "unapproved"):
            REPLAY.validate_readonly_skill_trace(
                _export([
                    {
                        "name": "execute_code",
                        "arguments": {"code": "print('blocked')"},
                        "result": {"success": False, "error": "blocked"},
                    }
                ]),
                allow_readonly_skills=True,
                tool_snapshot=existing_wecom,
                snapshot_scope="existing_wecom_surface",
            )

        # Existing strict mode remains fail-closed, and the explicit enum is
        # required rather than being inferred from the supplied names.
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "forbidden"):
            REPLAY.validate_readonly_skill_trace(
                payload,
                allow_readonly_skills=True,
                tool_snapshot=existing_wecom,
            )
        for invalid_scope in (None, "", "wecom", 1):
            with self.subTest(invalid_scope=invalid_scope):
                with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "snapshot_scope"):
                    REPLAY.validate_readonly_skill_trace(
                        payload,
                        allow_readonly_skills=True,
                        snapshot_scope=invalid_scope,
                    )
        for forbidden in ("skill_manage", "terminal", "unknown_tool"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "tool_snapshot"):
                    REPLAY.validate_readonly_skill_trace(
                        payload,
                        allow_readonly_skills=True,
                        snapshot_scope="existing_wecom_surface",
                        tool_snapshot=[*existing_wecom, forbidden],
                    )

    def test_reuses_official_metadata_hygiene_for_hidden_content_and_legacy_calls(self):
        payload = _export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "ok"},
            }
        ])
        decoded = json.loads(payload.decode("utf-8"))
        hidden_meta = {
            "id": 99,
            "session_id": "readonly-session",
            "active": 1,
            "role": "session_meta",
            "content": "hidden tool payload",
        }
        decoded["messages"].append(hidden_meta)
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "session_meta"):
            REPLAY.validate_readonly_skill_trace(
                (_json(decoded) + "\n").encode("utf-8"),
                allow_readonly_skills=True,
            )

        decoded = json.loads(payload.decode("utf-8"))
        decoded["messages"][2]["function_call"] = {"name": "terminal", "arguments": "{}"}
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "function_call"):
            REPLAY.validate_readonly_skill_trace(
                (_json(decoded) + "\n").encode("utf-8"),
                allow_readonly_skills=True,
            )

    def test_rejects_pending_new_assistant_batch_before_tool_result(self):
        decoded = json.loads(_export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "ok"},
            }
        ]).decode("utf-8"))
        messages = decoded["messages"]
        second_assistant = copy.deepcopy(messages[2])
        second_assistant["id"] = 4
        second_assistant["tool_calls"][0]["id"] = "call-2"
        messages[3]["id"] = 5
        messages[4]["id"] = 6
        messages.insert(3, second_assistant)
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "pending"):
            REPLAY.validate_readonly_skill_trace(
                (_json(decoded) + "\n").encode("utf-8"),
                allow_readonly_skills=True,
            )

    def test_rejects_result_before_declaration(self):
        decoded = json.loads(_export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "ok"},
            }
        ]).decode("utf-8"))
        messages = decoded["messages"]
        tool_row = messages.pop(3)
        assistant_row = messages.pop(2)
        tool_row["id"], assistant_row["id"], messages[-1]["id"] = 3, 4, 5
        messages.insert(2, tool_row)
        messages.insert(3, assistant_row)
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "bound|pending|declaration"):
            REPLAY.validate_readonly_skill_trace(
                (_json(decoded) + "\n").encode("utf-8"),
                allow_readonly_skills=True,
            )

    def test_rejects_cross_session_backfilled_tool_result(self):
        decoded = json.loads(_export([
            {
                "name": "skill_view",
                "arguments": {"name": "datasage"},
                "result": {"success": True, "name": "datasage", "content": "ok"},
            }
        ]).decode("utf-8"))
        decoded["messages"][3]["session_id"] = "another-session"
        with self.assertRaisesRegex(REPLAY._LiveTranscriptPolicyError, "cross-session"):
            REPLAY.validate_readonly_skill_trace(
                (_json(decoded) + "\n").encode("utf-8"),
                allow_readonly_skills=True,
            )


if __name__ == "__main__":
    unittest.main()
