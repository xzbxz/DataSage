"""R24: what an external owner must confirm, and what blocks a full rollout.

The register holds the questions only an owner can answer.  Two things keep it honest: an
accepted item needs an independent record, and every configuration claim it quotes must
still match this profile's live configuration - flipping the transport switch, the
production switch or the model provider fails the test until the register is updated.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
import unittest

import yaml

PROFILE_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = PROFILE_ROOT / "tests" / "fixtures" / "external_verification_register.json"
SCHEMA = "datasage-external-verification/v1"
STATUSES = ("pending", "accepted", "rejected")
EVIDENCE_KINDS = ("dba_report", "platform_attestation", "owner_statement", "provider_terms")
ITEM_KEYS = (
    "id",
    "area",
    "owner_role",
    "request",
    "evidence_format",
    "config_claim",
    "status",
    "independent_evidence",
)
REQUIRED_ITEMS = (
    "db_account_grants",
    "tls_transport",
    "server_identity_allowlist",
    "gateway_visibility_scope",
    "data_refresh_times",
    "log_and_artifact_retention",
    "model_provider_data_scope",
    "authorized_data_scope",
)
VALID_EVIDENCE = {
    "kind": "dba_report",
    "reference": "grants report 2026-09-23",
    "artifact_sha256": "b" * 64,
    "approved_by": "dba-on-call",
    "approved_on": "2026-09-23",
}


def _config_value(config: dict, dotted: str):
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return "<missing>"
        node = node[part]
    return node


def _evidence_problems(record) -> list[str]:
    if not isinstance(record, dict):
        return ["evidence record must be a mapping"]
    expected = {"kind", "reference", "artifact_sha256", "approved_by", "approved_on"}
    if set(record) != expected:
        return [f"evidence record must contain exactly {sorted(expected)}"]
    problems = []
    if record["kind"] not in EVIDENCE_KINDS:
        problems.append(f"evidence kind must be one of {list(EVIDENCE_KINDS)}")
    if not str(record["reference"] or "").strip():
        problems.append("evidence reference must not be empty")
    digest = str(record["artifact_sha256"] or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        problems.append("artifact_sha256 must be a 64-character hex digest")
    if not str(record["approved_by"] or "").strip():
        problems.append("approved_by must name the approver")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(record["approved_on"] or "")):
        problems.append("approved_on must be an ISO date")
    return problems


def _validate(register: dict) -> list[str]:
    problems: list[str] = []
    if not isinstance(register, dict) or register.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"]
    items = register.get("items")
    if not isinstance(items, list) or not items:
        return ["items must be a non-empty list"]
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict) or set(item) != set(ITEM_KEYS):
            problems.append(f"{label} must contain exactly {sorted(ITEM_KEYS)}")
            continue
        if item["id"] in seen:
            problems.append(f"{label} duplicates id {item['id']!r}")
        seen.add(item["id"])
        for key in ("area", "owner_role", "request", "evidence_format"):
            if not str(item[key] or "").strip():
                problems.append(f"{label}.{key} must not be empty")
        if item["status"] not in STATUSES:
            problems.append(f"{label}.status is unsupported: {item['status']!r}")
        evidence = item["independent_evidence"]
        if not isinstance(evidence, list):
            problems.append(f"{label}.independent_evidence must be a list")
            evidence = []
        if item["status"] == "accepted" and not evidence:
            problems.append(f"{label} is accepted without an independent record")
        if item["status"] == "pending" and evidence:
            problems.append(f"{label} carries evidence but is still pending")
        for position, record in enumerate(evidence):
            for problem in _evidence_problems(record):
                problems.append(f"{label}.independent_evidence[{position}]: {problem}")
        claim = item["config_claim"]
        if claim is not None and (
            not isinstance(claim, dict)
            or set(claim) != {"path", "value"}
            or not str(claim.get("path") or "").strip()
        ):
            problems.append(f"{label}.config_claim must be null or {{path, value}}")
    summary = register.get("summary")
    if not isinstance(summary, dict):
        problems.append("summary must be a mapping")
    else:
        expected = {
            "total": len(items),
            "pending": sum(1 for item in items if item.get("status") == "pending"),
            "accepted": sum(1 for item in items if item.get("status") == "accepted"),
            "rejected": sum(1 for item in items if item.get("status") == "rejected"),
        }
        for key, value in expected.items():
            if summary.get(key) != value:
                problems.append(
                    f"summary.{key} is {summary.get(key)!r}, items say {value!r}"
                )
    return problems


class ExternalVerificationRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
        self.config = yaml.safe_load(
            (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        self.items = {item["id"]: item for item in self.register["items"]}

    def test_every_external_question_is_present_and_owned(self) -> None:
        self.assertEqual([], _validate(self.register))
        self.assertEqual(len(REQUIRED_ITEMS), len(self.items))
        self.assertEqual(set(REQUIRED_ITEMS), set(self.items))
        for item in self.items.values():
            with self.subTest(item=item["id"]):
                self.assertTrue(item["owner_role"].strip())
                self.assertTrue(item["evidence_format"].strip())
                self.assertIn(item["status"], ("pending", "accepted", "rejected"))

    def test_configuration_claims_match_the_live_configuration(self) -> None:
        """Flipping a switch must fail here until the register says so."""

        for item in self.items.values():
            claim = item["config_claim"]
            if claim is None:
                continue
            with self.subTest(item=item["id"], path=claim["path"]):
                self.assertEqual(
                    claim["value"],
                    _config_value(self.config, claim["path"]),
                    f"{item['id']} quotes a configuration value that no longer holds",
                )

    def test_the_canary_transport_state_is_recorded_not_assumed(self) -> None:
        settings = self.config["plugins"]["entries"]["datasage-query"]["settings"]
        self.assertIs(False, settings["production_mode"])
        self.assertIs(False, settings["require_tls"])
        tls_item = self.items["tls_transport"]
        self.assertIn("canary", tls_item["request"])
        self.assertIn("明文传输被允许", tls_item["request"])
        self.assertIn(
            "pending item blocks a full rollout", self.register["policy"]
        )

    def test_accepted_items_need_an_independent_record(self) -> None:
        bare = copy.deepcopy(self.register)
        bare["items"][0]["status"] = "accepted"
        bare["summary"]["accepted"] = 1
        bare["summary"]["pending"] -= 1
        self.assertTrue(
            any(
                "without an independent record" in problem
                for problem in _validate(bare)
            )
        )

        wrong_kind = copy.deepcopy(bare)
        wrong_kind["items"][0]["independent_evidence"] = [
            {**VALID_EVIDENCE, "kind": "self_attestation"}
        ]
        self.assertTrue(
            any("evidence kind must be" in problem for problem in _validate(wrong_kind))
        )

        complete = copy.deepcopy(bare)
        complete["items"][0]["independent_evidence"] = [dict(VALID_EVIDENCE)]
        self.assertEqual([], _validate(complete))

    def test_pending_items_may_not_carry_evidence(self) -> None:
        drifted = copy.deepcopy(self.register)
        drifted["items"][0]["independent_evidence"] = [dict(VALID_EVIDENCE)]
        self.assertTrue(
            any("still pending" in problem for problem in _validate(drifted))
        )

    def test_register_carries_no_secret_values(self) -> None:
        text = REGISTER_PATH.read_text(encoding="utf-8")
        for forbidden in ("qyapi.weixin.qq.com", "WECOM_SECRET", "password=", "BEGIN PRIVATE KEY"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        self.assertNotRegex(text, r"\b[A-Za-z0-9+/]{40,}={0,2}\b")


if __name__ == "__main__":
    unittest.main()
