#!/usr/bin/env python3
"""Score normalized DataSage expert-kernel results against golden contracts.

This evaluator is intentionally model-, database-, and network-free.  A replay
adapter owns natural-language interpretation and emits normalized plans,
conclusion labels, and evidence receipts; this module only scores those facts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
SCHEMA = "datasage-golden-expert-cases/v3"
CANDIDATE_SCHEMA = "datasage-golden-expert-candidate/v1"
REPORT_SCHEMA = "datasage-golden-expert-report/v1"
PLAN_LIST_FIELDS = ("domains", "metrics", "dimensions", "operations")
RECEIPT_SCHEMA = "datasage-canary-receipt/v1"
WATERMARK_SCHEMA = "datasage-replay-watermark/v1"
LIVE_WATERMARK_SCHEMA = "datasage-replay-watermark/v2-live-fixture"
CONTEXT_FINGERPRINT_SCHEMA = "datasage-context-binding-fingerprint/v1"


def _is_typed_context_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"schema", "sha256"}
        and value.get("schema") == CONTEXT_FINGERPRINT_SCHEMA
        and isinstance(value.get("sha256"), str)
        and len(value["sha256"]) == 64
        and all(char in "0123456789abcdef" for char in value["sha256"])
    )


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(handle.name).replace(path)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _sha256(value: Any) -> str:
    raw = (
        value.encode("utf-8")
        if isinstance(value, str)
        else json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    return hashlib.sha256(raw).hexdigest()


def _validate_candidate_receipt(candidate: dict[str, Any]) -> None:
    receipt = candidate.get("canary_receipt")
    cases = candidate.get("cases")
    profile = candidate.get("profile_artifact")
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("candidate has no valid canary receipt")
    if receipt.get("profile_artifact") != profile:
        raise ValueError("candidate/receipt Profile artifact binding differs")
    database_identity = candidate.get("state_db_identity_sha256")
    if (
        not isinstance(database_identity, str)
        or len(database_identity) != 64
        or receipt.get("state_db_identity_sha256") != database_identity
        or not isinstance(receipt.get("source"), dict)
        or receipt["source"].get("state_db_identity_sha256") != database_identity
    ):
        raise ValueError("candidate/receipt state database identity binding differs")
    if receipt.get("candidate_cases_sha256") != _sha256(cases):
        raise ValueError("candidate cases do not match canary receipt")
    expected = receipt.get("receipt_sha256")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not isinstance(expected, str) or expected != _sha256(body):
        raise ValueError("candidate canary receipt digest is invalid")
    turns = receipt.get("turns")
    if not isinstance(turns, list) or len(turns) != len(cases):
        raise ValueError("candidate receipt turns do not match candidate cases")
    turn_by_id = {turn.get("test_id"): turn for turn in turns if isinstance(turn, dict)}
    if len(turn_by_id) != len(turns):
        raise ValueError("candidate receipt test IDs are invalid")
    for case in cases:
        review = case.get("conclusion_review")
        if (
            not isinstance(review, dict)
            or review.get("status") != "reviewed"
            or not isinstance(review.get("assertion_sha256"), str)
            or len(review["assertion_sha256"]) != 64
        ):
            raise ValueError(f"candidate case {case.get('id')!r} is unreviewed")
        turn = turn_by_id.get(case.get("id"))
        live_fields = (
            "fixture_attestation_sha256",
            "business_database_ref_sha256",
        )
        live_fixture = isinstance(turn, dict) and any(
            field in turn for field in live_fields
        )
        if live_fixture and not all(
            isinstance(turn.get(field), str) and len(turn[field]) == 64
            for field in live_fields
        ):
            raise ValueError(
                f"candidate case {case.get('id')!r} has invalid live fixture binding"
            )
        binding = (
            {
                "test_id": turn.get("test_id"),
                "artifact_id": profile.get("artifact_id"),
                "payload_sha256": profile.get("payload_sha256"),
                "session_id": turn.get("session_id"),
                "user_message_id": turn.get("user_message_id"),
                "canonical_prompt_sha256": turn.get("canonical_prompt_sha256"),
                "database_identity_sha256": turn.get("database_identity_sha256"),
                "watermark_sha256": turn.get("watermark_sha256"),
                "final_answer_sha256": turn.get("final_answer_sha256"),
                **(
                    {field: turn.get(field) for field in live_fields}
                    if live_fixture
                    else {}
                ),
            }
            if isinstance(turn, dict)
            else None
        )
        expected_watermark = (
            _sha256(
                {
                    "schema": (
                        LIVE_WATERMARK_SCHEMA if live_fixture else WATERMARK_SCHEMA
                    ),
                    "test_id": turn.get("test_id"),
                    "conversation_id": turn.get("conversation_id"),
                    "turn": turn.get("turn"),
                    "canonical_prompt_sha256": turn.get("canonical_prompt_sha256"),
                    "user_message_id": turn.get("user_message_id"),
                    "database_identity_sha256": database_identity,
                    "artifact_id": profile.get("artifact_id"),
                    "payload_sha256": profile.get("payload_sha256"),
                    **(
                        {field: turn.get(field) for field in live_fields}
                        if live_fixture
                        else {}
                    ),
                }
            )
            if isinstance(turn, dict) and isinstance(profile, dict)
            else None
        )
        if (
            not isinstance(turn, dict)
            or turn.get("candidate_case_sha256") != _sha256(case)
            or turn.get("conclusion_review") != review
            or turn.get("database_identity_sha256") != database_identity
            or not isinstance(turn.get("user_message_id"), int)
            or not all(
                isinstance(turn.get(field), str) and len(turn[field]) == 64
                for field in (
                    "canonical_prompt_sha256", "watermark_sha256",
                    "final_answer_sha256",
                )
            )
            or review.get("binding_sha256") != _sha256(binding)
            or turn.get("watermark_sha256") != expected_watermark
        ):
            raise ValueError(f"candidate case {case.get('id')!r} is not receipt-bound")


def validate_suite(suite: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(suite, dict) or suite.get("schema") != SCHEMA:
        return [f"suite schema must be {SCHEMA}"]
    if suite.get("plan_constraint_semantics") != "required-and-forbidden-subsets/v1":
        errors.append(
            "suite plan_constraint_semantics must be required-and-forbidden-subsets/v1"
        )
    cases = suite.get("cases")
    if not isinstance(cases, list):
        return ["suite cases must be a list"]
    minimum = suite.get("minimum_case_count")
    if not isinstance(minimum, int) or minimum < 1 or len(cases) < minimum:
        errors.append("suite does not meet minimum_case_count")
    ids: list[str] = []
    categories: Counter[str] = Counter()
    conversations: dict[str, list[int]] = {}
    required_keys = {
        "id", "category", "conversation_id", "turn", "prompt",
        "plan_constraints", "required_conclusions", "allowed_conclusions",
        "forbidden_conclusions", "evidence_requirements",
    }
    optional_keys: set[str] = set()
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if (
            not isinstance(case, dict)
            or not required_keys.issubset(case)
            or set(case).difference(required_keys | optional_keys)
        ):
            errors.append(f"{where} has invalid keys")
            continue
        case_id = case.get("id")
        category = case.get("category")
        conversation = case.get("conversation_id")
        turn = case.get("turn")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{where}.id is invalid")
        else:
            ids.append(case_id)
        if not isinstance(category, str) or not category:
            errors.append(f"{where}.category is invalid")
        else:
            categories[category] += 1
        if not isinstance(conversation, str) or not conversation:
            errors.append(f"{where}.conversation_id is invalid")
        elif not isinstance(turn, int) or turn < 1:
            errors.append(f"{where}.turn is invalid")
        else:
            conversations.setdefault(conversation, []).append(turn)
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            errors.append(f"{where}.prompt is empty")
        plan = case.get("plan_constraints")
        if not isinstance(plan, dict):
            errors.append(f"{where}.plan_constraints is invalid")
        else:
            for field in PLAN_LIST_FIELDS + ("must_not_metrics",):
                if not isinstance(plan.get(field), list):
                    errors.append(f"{where}.plan_constraints.{field} must be a list")
            for field in PLAN_LIST_FIELDS:
                forbidden_field = f"must_not_{field}"
                if forbidden_field in plan and not isinstance(
                    plan[forbidden_field], list
                ):
                    errors.append(
                        f"{where}.plan_constraints.{forbidden_field} must be a list"
                    )
            if plan.get("context_action") not in {"new", "preserve", "replace", "reset"}:
                errors.append(f"{where}.plan_constraints.context_action is invalid")
            if not isinstance(plan.get("time_semantics"), str):
                errors.append(f"{where}.plan_constraints.time_semantics is invalid")
            if "context_bindings" in plan and not isinstance(plan["context_bindings"], dict):
                errors.append(f"{where}.plan_constraints.context_bindings is invalid")
            elif isinstance(plan.get("context_bindings"), dict):
                for name, binding in plan["context_bindings"].items():
                    if isinstance(binding, dict) and not _is_typed_context_fingerprint(binding):
                        errors.append(
                            f"{where}.plan_constraints.context_bindings.{name} "
                            "has an invalid typed fingerprint"
                        )
        required = case.get("required_conclusions")
        allowed = case.get("allowed_conclusions")
        forbidden = case.get("forbidden_conclusions")
        if not all(isinstance(value, list) for value in (required, allowed, forbidden)):
            errors.append(f"{where} conclusion fields must be lists")
        elif not set(required).issubset(set(allowed)):
            errors.append(f"{where} required conclusions are not allowed")
        elif set(allowed).intersection(forbidden):
            errors.append(f"{where} allowed and forbidden conclusions overlap")
        evidence = case.get("evidence_requirements")
        evidence_keys = {
            "required_receipts", "minimum_successful_queries", "allow_partial_failure",
            "require_untruncated", "require_reconciled_decomposition", "must_not_query",
            "required_error_codes",
        }
        if not isinstance(evidence, dict) or set(evidence) != evidence_keys:
            errors.append(f"{where}.evidence_requirements has invalid keys")
    if len(ids) != len(set(ids)):
        errors.append("case IDs must be unique")
    for conversation, turns in conversations.items():
        if sorted(turns) != list(range(1, len(turns) + 1)):
            errors.append(f"conversation {conversation!r} turns are not contiguous")
    minimums = suite.get("required_category_minimums")
    if not isinstance(minimums, dict):
        errors.append("required_category_minimums must be an object")
    else:
        for category, count in minimums.items():
            if not isinstance(count, int) or count < 1 or categories[category] < count:
                errors.append(f"category {category!r} does not meet minimum {count!r}")
    return errors


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _binding_shape_valid(value: Any, *, live_fixture: bool, name: str) -> bool:
    if name == "limit" and isinstance(value, int):
        return True
    if name == "filter_fingerprints" and isinstance(value, dict):
        return all(
            _is_typed_context_fingerprint(item) if live_fixture else isinstance(item, str)
            for item in value.values()
        )
    return _is_typed_context_fingerprint(value) if live_fixture else not isinstance(value, dict)


def _score_case(
    case: dict[str, Any],
    observed: Any,
    *,
    live_fixture: bool = False,
) -> list[str]:
    if not isinstance(observed, dict):
        return ["candidate case must be an object"]
    errors: list[str] = []
    constraints = case["plan_constraints"]
    plan = observed.get("plan")
    if not isinstance(plan, dict):
        return ["plan is missing"]
    for field in PLAN_LIST_FIELDS:
        observed_values = set(_list(plan.get(field)))
        required_values = set(constraints[field])
        missing_values = required_values.difference(observed_values)
        if missing_values:
            errors.append(
                f"plan.{field} is missing required values "
                f"{sorted(missing_values)!r}"
            )
        forbidden_values = set(
            constraints.get(
                f"must_not_{field}",
                constraints.get("must_not_metrics", []) if field == "metrics" else [],
            )
        )
        if "*" in forbidden_values and observed_values:
            errors.append(f"plan contains {field} although none are authorized")
        else:
            overlap = observed_values.intersection(forbidden_values)
            if overlap:
                errors.append(
                    f"plan contains forbidden {field} {sorted(overlap)!r}"
                )
    for field in ("time_semantics", "context_action"):
        if plan.get(field) != constraints[field]:
            errors.append(
                f"plan.{field}={plan.get(field)!r}, expected {constraints[field]!r}"
            )
    expected_bindings = constraints.get("context_bindings", {})
    observed_bindings = plan.get("context_bindings", {})
    if not isinstance(observed_bindings, dict):
        errors.append("plan.context_bindings must be an object")
    else:
        for name, value in expected_bindings.items():
            observed_value = observed_bindings.get(name)
            if not _binding_shape_valid(value, live_fixture=live_fixture, name=name):
                errors.append(f"expected context binding {name!r} has wrong schema")
                continue
            if not _binding_shape_valid(observed_value, live_fixture=live_fixture, name=name):
                errors.append(f"observed context binding {name!r} has wrong schema")
                continue
            if observed_value != value:
                errors.append(
                    f"plan.context_bindings.{name}={observed_value!r}, "
                    f"expected {value!r}"
                )

    conclusions = _list(observed.get("conclusions"))
    missing_conclusions = set(case["required_conclusions"]).difference(conclusions)
    if missing_conclusions:
        errors.append(f"required conclusions missing {sorted(missing_conclusions)!r}")
    unknown = set(conclusions).difference(case["allowed_conclusions"])
    if unknown:
        errors.append(f"conclusions are not allowed {sorted(unknown)!r}")
    forbidden = set(conclusions).intersection(case["forbidden_conclusions"])
    if forbidden:
        errors.append(f"forbidden conclusions present {sorted(forbidden)!r}")
    requirement = case["evidence_requirements"]
    evidence = observed.get("evidence")
    if not isinstance(evidence, dict):
        return errors + ["evidence is missing"]
    receipts = set(_list(evidence.get("receipts")))
    missing_receipts = set(requirement["required_receipts"]).difference(receipts)
    if missing_receipts:
        errors.append(f"required receipts missing {sorted(missing_receipts)!r}")
    successful = evidence.get("successful_queries")
    failed = evidence.get("failed_queries")
    if not isinstance(successful, int) or successful < requirement["minimum_successful_queries"]:
        errors.append("successful query count is below requirement")
    if not isinstance(failed, int) or failed < 0:
        errors.append("failed query count is invalid")
    elif failed and not requirement["allow_partial_failure"]:
        errors.append("partial query failure is not allowed")
    if requirement["require_untruncated"] and evidence.get("truncated") is not False:
        errors.append("evidence is truncated or truncation status is missing")
    if requirement["require_reconciled_decomposition"] and evidence.get("reconciled") is not True:
        errors.append("change decomposition is not reconciled")
    if requirement["must_not_query"] and evidence.get("query_attempted") is not False:
        errors.append("a query was attempted although the case must fail before data access")
    if case.get("category") == "capability_boundary" and evidence.get("error_codes") != []:
        errors.append("capability boundary error codes must be exactly []")
    missing_codes = set(requirement["required_error_codes"]).difference(
        _list(evidence.get("error_codes"))
    )
    if missing_codes:
        errors.append(f"required error codes missing {sorted(missing_codes)!r}")
    return errors


def score(suite: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    suite_errors = validate_suite(suite)
    if suite_errors:
        raise ValueError("invalid golden suite: " + "; ".join(suite_errors))
    if not isinstance(candidate, dict) or candidate.get("schema") != CANDIDATE_SCHEMA:
        raise ValueError(f"candidate schema must be {CANDIDATE_SCHEMA}")
    rows = candidate.get("cases")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("candidate cases must be a list of objects")
    _validate_candidate_receipt(candidate)
    by_id = {row.get("id"): row for row in rows}
    expected_ids = {case["id"] for case in suite["cases"]}
    if len(by_id) != len(rows) or set(by_id) != expected_ids:
        raise ValueError("candidate IDs must exactly match golden case IDs")
    context_errors: dict[str, list[str]] = {case["id"]: [] for case in suite["cases"]}
    previous_session: dict[str, str] = {}
    for case in suite["cases"]:
        observed = by_id[case["id"]]
        session_id = observed.get("session_id")
        conversation = case["conversation_id"]
        action = case["plan_constraints"]["context_action"]
        if not isinstance(session_id, str) or not session_id:
            context_errors[case["id"]].append("session_id is missing")
            continue
        prior = previous_session.get(conversation)
        if prior is not None and action == "reset" and session_id == prior:
            context_errors[case["id"]].append("reset did not create a new session")
        if prior is not None and action != "reset" and session_id != prior:
            lineage = observed.get("session_lineage")
            if (
                not isinstance(lineage, list)
                or len(lineage) < 2
                or lineage[0] != prior
                or lineage[-1] != session_id
                or len(lineage) != len(set(lineage))
            ):
                context_errors[case["id"]].append(
                    "multi-turn context has no proven compression continuation"
                )
        previous_session[conversation] = session_id

    receipt_turns = candidate["canary_receipt"]["turns"]
    live_ids = {
        turn["test_id"]
        for turn in receipt_turns
        if "fixture_attestation_sha256" in turn
        or "business_database_ref_sha256" in turn
    }
    results = []
    category_counts: dict[str, list[bool]] = {}
    for case in suite["cases"]:
        errors = _score_case(
            case,
            by_id[case["id"]],
            live_fixture=case["id"] in live_ids,
        )
        errors.extend(context_errors[case["id"]])
        passed = not errors
        results.append(
            {"id": case["id"], "category": case["category"], "passed": passed, "errors": errors}
        )
        category_counts.setdefault(case["category"], []).append(passed)
    passed = sum(row["passed"] for row in results)
    return {
        "schema": REPORT_SCHEMA,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4) if results else 0,
        },
        "categories": {
            name: {
                "total": len(values),
                "passed": sum(values),
                "pass_rate": round(sum(values) / len(values), 4),
            }
            for name, values in sorted(category_counts.items())
        },
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=HERE / "golden_expert_cases.json")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = score(_load(args.cases), _load(args.candidate))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _write_text_atomic(args.output, rendered)
    else:
        sys.stdout.write(rendered)
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
