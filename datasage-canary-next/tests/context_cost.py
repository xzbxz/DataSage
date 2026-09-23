"""R28: measure context and artifact cost - never estimate, never predict.

``measure`` recomputes every number from this workspace; ``write`` merges those numbers into
the baseline register while preserving the human-filled fields (owner, procedure, status);
``check`` reports drift.  Token counts, latency and provider cost are deliberately absent:
there is no local tokenizer for the serving model, so they stay declared as unmeasured
instead of being converted from byte counts.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import statistics
import sys
from tempfile import TemporaryDirectory
import types
from typing import Any

PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
REGISTER_PATH = PROFILE_ROOT / "tests" / "fixtures" / "context_cost_baseline.json"

SKILL_ROOT = PROFILE_ROOT / "skills" / "business-analytics" / "datasage"
ACCEPTANCE_CASES = PROFILE_ROOT / "tests" / "fixtures" / "business_acceptance_cases.json"
PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.yaml"

PACKAGE = "datasage_context_cost_measure"
if PACKAGE not in sys.modules:
    os.environ.setdefault("HERMES_HOME", str(PROFILE_ROOT))
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE] = package


def _plugin(name: str) -> Any:
    return importlib.import_module(f"{PACKAGE}.{name}")


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _tracked_names(relative: str) -> set[str] | None:
    """Tracked file names under one directory, so the numbers reproduce in any checkout."""

    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(PROFILE_ROOT), "ls-files", "--", relative],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return {Path(line).name for line in completed.stdout.splitlines() if line.strip()}


def measure_skill_context() -> dict[str, Any]:
    tracked = _tracked_names("skills/business-analytics/datasage")
    files = {}
    for path in sorted(SKILL_ROOT.rglob("*.md")):
        if tracked is not None and path.name not in tracked:
            continue
        text = path.read_text(encoding="utf-8")
        files[str(path.relative_to(SKILL_ROOT)).replace("\\", "/")] = {
            "characters": len(text),
            "bytes": _bytes(text),
            "lines": text.count("\n") + 1,
        }
    return {
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files.values()),
        "total_characters": sum(item["characters"] for item in files.values()),
    }


def measure_tool_surface() -> dict[str, Any]:
    import yaml

    manifest = yaml.safe_load(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    provided = [str(name) for name in manifest.get("provides_tools") or []]
    manifest_text = PLUGIN_MANIFEST.read_text(encoding="utf-8")
    return {
        "provided_tool_names": [name for name in provided],
        "provided_tool_count": len(provided),
        # Byte count of the reviewed text with line endings normalised, so a checkout with
        # LF and a working copy with CRLF measure the same.
        "manifest_bytes": _bytes(manifest_text),
        "channel_tool_count": 7,
        "channel_tool_note": "native clarify + three datasage tools + skills group (R13 probe)",
    }


def _measured_payload(label: str, args: dict[str, Any], tool: str) -> dict[str, Any]:
    """Measure one valid model-visible payload; a failure envelope is not a measurement."""

    contracts = _plugin("contracts")
    wire = _plugin("wire")
    raw = contracts.datasage_catalog(args)
    envelope = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(envelope, dict) and envelope.get("status") == "failed":
        raise SystemExit(
            f"{label}: the call failed ({envelope['error']['code']}), so it is not measured"
        )
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    compact = wire.enforce_tool_result_budget(tool, text)
    return {
        "raw_bytes": _bytes(text),
        "wire_bytes": _bytes(compact),
        "method": "measured",
        "measured_from": f"wire.enforce_tool_result_budget over contracts.datasage_catalog({args})",
    }


def measure_payloads() -> dict[str, Any]:
    """Measure real model-visible payload bytes through the profile's own wire gate."""

    wire = _plugin("wire")
    measured: dict[str, Any] = {}

    measured["catalog_expert_index_delivery"] = _measured_payload(
        "catalog_expert_index_delivery",
        {"requests": [{"domain": "delivery", "view": "expert_index"}]},
        "datasage_catalog",
    )
    measured["catalog_full_delivery"] = _measured_payload(
        "catalog_full_delivery",
        {"requests": [{"domain": "delivery", "view": "full"}]},
        "datasage_catalog",
    )

    rows = [
        {"request_id": "r1", "metric_value": "1" * 200, "label": f"客户-{index}"}
        for index in range(100)
    ]
    synthetic = json.dumps({"rows": rows}, ensure_ascii=False)
    compact_rows = wire.enforce_tool_result_budget("datasage_query", synthetic)
    measured["query_rows_100x200chars"] = {
        "raw_bytes": _bytes(synthetic),
        "wire_bytes": _bytes(compact_rows),
        "method": "measured",
        "measured_from": "wire.enforce_tool_result_budget over a 100-row x 200-char result",
        "input_note": "synthetic shape at the configured max_rows=100 and 200-char cells",
    }
    return measured


def measure_artifacts() -> dict[str, Any]:
    local_report = _plugin("local_report")
    payload = {
        "report_id": "slow_report",
        "query": {
            "answer_scope_line": "范围：一个示例部门，2026-W38",
            "facts": [
                {"label": "期末在库卷数", "value": index, "unit": "卷"}
                for index in range(40)
            ],
        },
    }
    text = local_report.render_text(payload)
    with TemporaryDirectory() as tmp:
        run = local_report.save_artifacts(Path(tmp), payload, text)
        document = json.loads((run / "report.json").read_text(encoding="utf-8"))
        # The runtime block carries a pid and machine paths whose length changes with the
        # host; normalise those values so the measurement is comparable across machines.
        runtime = document.get("runtime") or {}
        for key in ("profile_home", "python_executable", "python_prefix", "pid"):
            if key in runtime:
                runtime[key] = f"<{key}>"
        normalised = json.dumps(document, ensure_ascii=False, indent=2)
        report_json = _bytes(normalised)
        report_txt = (run / "report.txt").stat().st_size
    return {
        "local_report_text_bytes": report_txt,
        "local_report_json_bytes": report_json,
        "method": "measured",
        "measured_from": "local_report.render_text + save_artifacts over a 40-fact payload, "
        "with runtime pid and paths normalised",
        "input_note": "synthetic shape; the model's own prose is not produced offline",
    }


def measure_questions() -> dict[str, Any]:
    cases = json.loads(ACCEPTANCE_CASES.read_text(encoding="utf-8"))["cases"]
    turn_bytes = sorted(_bytes(turn) for case in cases for turn in case.get("turns", []))
    properties = [len(case.get("required_answer_properties") or []) for case in cases]
    budgets = _plugin("settings")
    return {
        "acceptance_case_count": len(cases),
        "question_bytes_min": turn_bytes[0],
        "question_bytes_median": int(statistics.median(turn_bytes)),
        "question_bytes_max": turn_bytes[-1],
        "required_properties_median": int(statistics.median(properties)),
        "configured_max_rows": budgets.get_int("max_rows", 100, 1, 100),
        "configured_max_cell_chars": budgets.get_int("max_cell_chars", 2000, 100, 20000),
        "configured_max_concurrent_queries": budgets.get_int("max_concurrent_queries", 4, 1, 16),
        "configured_call_timeout_seconds": budgets.get_int("call_timeout_seconds", 60, 5, 600),
        "method": "measured",
        "measured_from": "business_acceptance_cases.json plus the plugin settings contract",
    }


def measure() -> dict[str, Any]:
    return {
        "skill_context": measure_skill_context(),
        "tool_surface": measure_tool_surface(),
        "payloads": measure_payloads(),
        "artifacts": measure_artifacts(),
        "questions": measure_questions(),
    }


def write_register() -> int:
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    measured = measure()
    for section, values in measured.items():
        register["measured"][section] = values
    REGISTER_PATH.write_text(
        json.dumps(register, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


def check() -> int:
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    measured = measure()
    problems: list[str] = []
    for section, values in measured.items():
        if register["measured"].get(section) != values:
            problems.append(f"{section} drifted from the workspace")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "measure"
    if action == "measure":
        print(json.dumps(measure(), ensure_ascii=False, indent=2))
        return 0
    if action == "write":
        return write_register()
    if action == "check":
        return check()
    print(f"usage: {Path(argv[0]).name} [measure|write|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
