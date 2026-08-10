#!/usr/bin/env python3
"""Auditable offline E2E replay for the DataSage WeCom surface.

The default mode reads frozen transcripts only.  It never imports the plugin,
calls a model, opens a network connection, or touches a database.  A future
live adapter must be explicitly selected with ``--mode live-wecom`` and is
intentionally unavailable in this release.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
PROFILE_ROOT = HERE.parents[2]
PUBLIC_DATASAGE_TOOLS = {
    "datasage_catalog",
    "datasage_entity_resolve",
    "datasage_query",
}
FORBIDDEN_TOOL_PREFIXES = (
    "session_search",
    "memory",
    "skills",
    "skill_",
    "cron",
    "terminal",
    "file",
    "code",
)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _wecom_toolsets(config_path: Path) -> list[str]:
    """Read only ``platform_toolsets.wecom`` without a YAML dependency."""
    lines = config_path.read_text(encoding="utf-8").splitlines()
    in_platform = False
    in_wecom = False
    result: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_platform = stripped == "platform_toolsets:"
            in_wecom = False
            continue
        if in_platform and indent == 2:
            in_wecom = stripped == "wecom:"
            continue
        if in_platform and in_wecom and indent >= 4 and stripped.startswith("- "):
            result.append(stripped[2:].strip().strip("'\""))
        elif in_platform and in_wecom and indent <= 2:
            break
    if not result:
        raise ValueError(f"platform_toolsets.wecom not found in {config_path}")
    return result


def _contains_forbidden_tool(name: str) -> bool:
    lowered = name.lower()
    return any(lowered == prefix or lowered.startswith(prefix + "_") for prefix in FORBIDDEN_TOOL_PREFIXES)


def _semantic_check(value: str | None, allowed: list[str], forbidden: list[str], label: str) -> list[str]:
    errors: list[str] = []
    if allowed and value not in allowed:
        errors.append(f"{label}={value!r} not in allowed set {allowed!r}")
    if value in forbidden:
        errors.append(f"{label}={value!r} is forbidden")
    return errors


def _business_errors(case: dict[str, Any], replay: dict[str, Any]) -> list[str]:
    oracle = case["oracle"]
    semantic = replay.get("semantic") or {}
    errors: list[str] = []
    errors += _semantic_check(
        semantic.get("metric"), oracle["allowed_metrics"], oracle["forbidden_metrics"], "metric"
    )
    errors += _semantic_check(
        semantic.get("domain"), oracle["allowed_domains"], oracle["forbidden_domains"], "domain"
    )
    errors += _semantic_check(
        semantic.get("entity"), oracle["allowed_entities"], oracle["forbidden_entities"], "entity"
    )
    errors += _semantic_check(
        semantic.get("period"), oracle["allowed_periods"], oracle["forbidden_periods"], "period"
    )
    if oracle["required_scope"] and semantic.get("scope") != oracle["required_scope"]:
        errors.append(f"scope={semantic.get('scope')!r}, expected {oracle['required_scope']!r}")

    answer = str(replay.get("answer", ""))
    for fragment in oracle["conclusions"]["must_include"]:
        if fragment not in answer:
            errors.append(f"answer missing required conclusion {fragment!r}")
    for fragment in oracle["conclusions"]["must_not_include"]:
        if fragment in answer:
            errors.append(f"answer contains forbidden conclusion {fragment!r}")

    facts = replay.get("numeric_facts") or {}
    for assertion in oracle["numeric_assertions"]:
        if assertion.get("op") == "equals_fact":
            left_name = assertion["left"]
            right_name = assertion["right"]
            if left_name not in facts or right_name not in facts:
                errors.append(
                    f"numeric consistency facts missing: {left_name!r}, {right_name!r}"
                )
                continue
            left = float(facts[left_name])
            right = float(facts[right_name])
            tolerance = float(assertion.get("tolerance", 0))
            if abs(left - right) > tolerance:
                errors.append(
                    f"numeric contradiction: {left_name}={left} != {right_name}={right}"
                )
            continue
        name = assertion["name"]
        if name not in facts:
            errors.append(f"numeric fact {name!r} missing")
            continue
        actual = float(facts[name])
        if "equals" in assertion and abs(actual - float(assertion["equals"])) > float(assertion.get("tolerance", 0)):
            errors.append(f"numeric fact {name!r}={actual} violates equality assertion")
        if "min" in assertion and actual < float(assertion["min"]):
            errors.append(f"numeric fact {name!r}={actual} below minimum")
        if "max" in assertion and actual > float(assertion["max"]):
            errors.append(f"numeric fact {name!r}={actual} above maximum")
    return errors


def _validate_multiturn(cases: list[dict[str, Any]], replays: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {case["id"]: [] for case in cases}
    conversations: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        conversations.setdefault(case["conversation_id"], []).append(case)
    for group in conversations.values():
        group.sort(key=lambda item: item["turn"])
        if len(group) < 2:
            continue
        session_ids = {replays[item["id"]].get("session_id") for item in group}
        if len(session_ids) != 1 or None in session_ids:
            for item in group:
                errors[item["id"]].append("multi-turn conversation did not preserve one session_id")
        previous: dict[str, Any] | None = None
        for item in group:
            semantic = replays[item["id"]].get("semantic") or {}
            transition = item.get("follow_up") or {}
            if previous is not None:
                for field in transition.get("preserve", []):
                    if semantic.get(field) != previous.get(field):
                        errors[item["id"]].append(f"follow-up did not preserve {field}")
                for field in transition.get("replace", []):
                    if semantic.get(field) == previous.get(field):
                        errors[item["id"]].append(f"follow-up did not replace {field}")
            previous = semantic
    return errors


def run(cases_path: Path, replay_path: Path, config_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    suite = _load_json(cases_path)
    replay_doc = _load_json(replay_path)
    cases = suite["cases"]
    replay_rows = replay_doc["cases"]
    replays = {row["id"]: row for row in replay_rows}
    if len(cases) < int(suite.get("minimum_case_count", 26)):
        raise ValueError("suite contains fewer than the required 26 cases")
    if set(replays) != {case["id"] for case in cases}:
        raise ValueError("replay IDs must exactly match case IDs")

    configured_toolsets = _wecom_toolsets(config_path)
    declared_toolsets = replay_doc.get("wecom_surface", {}).get("toolsets")
    surface_errors: list[str] = []
    if declared_toolsets != configured_toolsets:
        surface_errors.append(
            f"replay toolsets {declared_toolsets!r} != config platform_toolsets.wecom {configured_toolsets!r}"
        )
    declared_datasage_tools = set(replay_doc.get("wecom_surface", {}).get("datasage_public_tools", []))
    if declared_datasage_tools != PUBLIC_DATASAGE_TOOLS:
        surface_errors.append("replay DataSage public tool list does not match plugin.yaml")

    multiturn_errors = _validate_multiturn(cases, replays)
    rows: list[dict[str, Any]] = []
    for case in cases:
        replay = replays[case["id"]]
        process = replay.get("process") or {}
        process_errors: list[str] = []
        if process.get("exit_code") != 0:
            process_errors.append(f"exit_code={process.get('exit_code')!r}")
        if process.get("timed_out") is not False:
            process_errors.append("process timed out")
        process_exit = not process_errors

        tool_errors = list(surface_errors)
        calls = replay.get("tool_calls") or []
        for call in calls:
            name = str(call.get("name", ""))
            if _contains_forbidden_tool(name):
                tool_errors.append(f"forbidden WeCom tool called: {name}")
            if name.startswith("datasage_") and name not in PUBLIC_DATASAGE_TOOLS:
                tool_errors.append(f"non-public DataSage tool called: {name}")
            if call.get("status") != "success":
                tool_errors.append(f"tool {name!r} status={call.get('status')!r}")
        tool_success = process_exit and not tool_errors

        business_errors = _business_errors(case, replay)
        business_errors.extend(multiturn_errors[case["id"]])
        business_correct = tool_success and not business_errors
        rows.append(
            {
                "id": case["id"],
                "conversation_id": case["conversation_id"],
                "turn": case["turn"],
                "duration_ms": process.get("duration_ms"),
                "process_exit": process_exit,
                "tool_success": tool_success,
                "business_correct": business_correct,
                "errors": {
                    "process": process_errors,
                    "tool": tool_errors,
                    "business": business_errors,
                },
            }
        )

    total = len(rows)
    durations = [float(row["duration_ms"]) for row in rows if row["duration_ms"] is not None]
    counts = {
        key: sum(1 for row in rows if row[key])
        for key in ("process_exit", "tool_success", "business_correct")
    }
    return {
        "schema": "datasage-e2e-report/v1",
        "mode": "offline-replay",
        "source": {"cases": str(cases_path), "replay": str(replay_path), "config": str(config_path)},
        "wecom_surface": {
            "toolsets": configured_toolsets,
            "datasage_public_tools": sorted(PUBLIC_DATASAGE_TOOLS),
            "forbidden_tool_prefixes": list(FORBIDDEN_TOOL_PREFIXES),
        },
        "summary": {
            "total": total,
            **{key: {"passed": value, "rate": round(value / total, 4)} for key, value in counts.items()},
            "timeouts": sum(1 for row in replay_rows if row.get("process", {}).get("timed_out") is True),
            "duration_ms": {
                "min": min(durations) if durations else None,
                "max": max(durations) if durations else None,
                "mean": round(statistics.fmean(durations), 2) if durations else None,
                "median": statistics.median(durations) if durations else None,
            },
            "runner_elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        },
        "results": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline-replay", "live-wecom"), default="offline-replay")
    parser.add_argument("--cases", type=Path, default=HERE / "cases.json")
    parser.add_argument("--replay", type=Path, default=HERE / "fixtures" / "offline_replay.json")
    parser.add_argument("--config", type=Path, default=PROFILE_ROOT / "config.yaml")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "live-wecom":
        parser.error("live WeCom canary is opt-in but no live adapter is shipped; offline replay is the release gate")
    report = run(args.cases, args.replay, args.config)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if report["summary"]["business_correct"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
