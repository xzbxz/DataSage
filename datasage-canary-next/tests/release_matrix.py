"""R27: independent truth, holdout separation and the release matrix.

``generate`` rebuilds the sample and pass counts from the profile's own registers - the
metric readiness register, the acceptance cases and the operational entry inventory - and
merges them into the owner-facing record without touching the fields only a human may fill
(independent truth, reviewer, adjudication, release decision, sign-off).  ``check`` reports
drift.  The assistant never writes a release decision or a signature.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

PROFILE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROFILE_ROOT / "tests" / "fixtures"
MATRIX_PATH = FIXTURES / "release_matrix.json"
CASES_PATH = FIXTURES / "business_acceptance_cases.json"
METRICS_PATH = FIXTURES / "metric_readiness_register.json"

DOMAIN_SURFACES = (
    "domain.delivery",
    "domain.receipt",
    "domain.receivable",
    "domain.target",
    "domain.inventory",
    "domain.pattern_matching",
    "domain.profit",
)
OTHER_SURFACES = (
    "channel.wecom_surface",
    "reports.operational_entries",
    "answers.business_presentation",
    "analysis.domain_diagnostic_paths",
    "analysis.cross_domain_reasoning",
    "artifacts.integrity_and_recipients",
    "security.data_source_and_privacy",
    "operations.authorisation_and_lifecycle",
)
HOLDOUT_CANDIDATES = tuple(f"B{index:02d}" for index in range(19, 25))


def _cases() -> list[dict[str, Any]]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _metrics() -> dict[str, Any]:
    return json.loads(METRICS_PATH.read_text(encoding="utf-8"))


def _domain_rows() -> dict[str, dict[str, Any]]:
    register = _metrics()
    rows: dict[str, dict[str, Any]] = {}
    for surface in DOMAIN_SURFACES:
        domain = surface.split(".", 1)[1]
        metrics = [m for m in register["metrics"] if m["domain"] == domain]
        verified = sum(1 for m in metrics if m["verified"])
        rows[surface] = {
            "surface": surface,
            "sample": len(metrics),
            "pass": verified,
            "sample_source": "tests/fixtures/metric_readiness_register.json",
            "reproduction_path": "tests/test_metric_readiness_register.py",
            "reproducible": True,
            "failure_reason": (
                f"{len(metrics)} 个指标中 {verified} 个有独立证据（R16 登记册）"
                if verified < len(metrics)
                else ""
            ),
        }
    return rows


def _fixed_rows() -> dict[str, dict[str, Any]]:
    cases = _cases()
    reviewed = sum(
        1 for case in cases if case["independent_business_review_status"] != "pending"
    )
    executed = sum(1 for case in cases if case["execution_status"] != "not_run")
    return {
        "channel.wecom_surface": {
            "surface": "channel.wecom_surface",
            "reproduction_path": "tests/test_integration_boundaries.py::test_wecom_surface_matches_the_documented_capability_boundary",
            "sample": 7,
            "pass": 7,
            "sample_source": "R13 channel probe (7 tools visible)",
            "reproducible": True,
            "failure_reason": "真实渠道回执未到：办公 Skill 隐藏与工具边界只在本机验证",
            "status": "partial",
        },
        "reports.operational_entries": {
            "surface": "reports.operational_entries",
            "reproduction_path": "tests/test_operational_entry_inventory.py",
            "sample": 16,
            "pass": 16,
            "sample_source": "scripts/datasage_*.py inventory (16 entries)",
            "reproducible": True,
            "failure_reason": "真实调度与真实发送未演练；全部入口默认关闭",
            "status": "partial",
        },
        "answers.business_presentation": {
            "surface": "answers.business_presentation",
            "reproduction_path": "tests/test_answer_presentation.py",
            "sample": 4,
            "pass": 4,
            "sample_source": "four answer types in references/answer-boundary.md",
            "reproducible": True,
            "failure_reason": "真实渠道的答案评审未做",
            "status": "partial",
        },
        "analysis.domain_diagnostic_paths": {
            "surface": "analysis.domain_diagnostic_paths",
            "reproduction_path": "tests/test_domain_diagnostic_paths.py",
            "sample": 8,
            "pass": 8,
            "sample_source": "8 domain method files with a Diagnostic path section",
            "reproducible": True,
            "failure_reason": "业务 owner 未评审诊断链与重要性基准",
            "status": "partial",
        },
        "analysis.cross_domain_reasoning": {
            "surface": "analysis.cross_domain_reasoning",
            "reproduction_path": "tests/test_remediation_business_acceptance.py",
            "sample": len(cases),
            "pass": reviewed,
            "sample_source": "tests/fixtures/business_acceptance_cases.json",
            "reproducible": True,
            "failure_reason": f"24 案中 {reviewed} 案有独立业务评审，{executed} 案实际执行过",
            "status": "unverified" if reviewed == 0 else "partial",
        },
        "artifacts.integrity_and_recipients": {
            "surface": "artifacts.integrity_and_recipients",
            "reproduction_path": "tests/test_artifact_integrity.py",
            "sample": 8,
            "pass": 8,
            "sample_source": "8 injected artifact items in the R26 record",
            "reproducible": True,
            "failure_reason": "真实客户端打开与真实接收人复核未做",
            "status": "partial",
        },
        "security.data_source_and_privacy": {
            "surface": "security.data_source_and_privacy",
            "reproduction_path": "tests/test_external_verification_register.py",
            "sample": 8,
            "pass": 0,
            "sample_source": "8 external verification items in the R24 register",
            "reproducible": True,
            "failure_reason": "canary 模式：production_mode 与 require_tls 均为 false，明文传输被允许；R24 登记册 8 项全部 pending",
            "status": "blocked_external",
        },
        "operations.authorisation_and_lifecycle": {
            "surface": "operations.authorisation_and_lifecycle",
            "reproduction_path": "tests/test_operational_entry_inventory.py",
            "sample": 16,
            "pass": 16,
            "sample_source": "16 operational entries with default-off gates",
            "reproducible": True,
            "failure_reason": "调度未注册、发送开关全关；真实周期续期与投递演练待 owner 授权",
            "status": "partial",
        },
    }


def build() -> dict[str, Any]:
    rows = _domain_rows()
    for surface, row in _fixed_rows().items():
        rows[surface] = row

    ordered = {surface: rows[surface] for surface in DOMAIN_SURFACES + OTHER_SURFACES}
    for row in ordered.values():
        row.setdefault("status", "unverified")
        row.setdefault("failure_reason", "")
        row.setdefault("released", False)
        row.setdefault("release_decision", None)
        row.setdefault("release_decision_owner", None)
    return ordered


def _derived() -> dict[str, Any]:
    cases = _cases()
    register = _metrics()
    return {
        "capability_counts": {
            "surfaces": len(DOMAIN_SURFACES) + len(OTHER_SURFACES),
            "domains": len(DOMAIN_SURFACES),
            "metrics_total": register["summary"]["total"],
            "metrics_verified": register["summary"]["verified"],
            "cases_total": len(cases),
            "cases_reviewed": sum(
                1
                for case in cases
                if case["independent_business_review_status"] != "pending"
            ),
        },
        "capabilities": build(),
        "truth": [
            {
                "case_id": case["id"],
                "title": case["title"],
                "domains": case["domains"],
                "holdout": case["id"] in HOLDOUT_CANDIDATES,
                "independent_truth": None,
                "truth_status": "pending",
                "reviewer_role": None,
                "reviewed_on": None,
                "evidence": [],
                "disagreement": None,
                "adjudication": None,
            }
            for case in cases
        ],
    }


def generate() -> int:
    record = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    derived = _derived()
    record["capability_counts"] = derived["capability_counts"]
    existing = record.get("capabilities") or {}
    merged: dict[str, Any] = {}
    for surface, row in derived["capabilities"].items():
        previous = existing.get(surface) or {}
        merged[surface] = {
            **row,
            "release_decision": previous.get("release_decision"),
            "release_decision_owner": previous.get("release_decision_owner"),
        }
        if surface in existing:
            for key in ("failure_reason", "status"):
                if previous.get(key):
                    merged[surface][key] = previous[key]
    record["capabilities"] = merged
    previous_truth = {
        row["case_id"]: row for row in (record.get("truth") or [])
    }
    merged_truth = []
    for row in derived["truth"]:
        previous = previous_truth.get(row["case_id"]) or {}
        merged_row = dict(row)
        for key in (
            "independent_truth",
            "truth_status",
            "reviewer_role",
            "reviewed_on",
            "evidence",
            "disagreement",
            "adjudication",
        ):
            if key in previous and previous[key] not in (None, [], ""):
                merged_row[key] = previous[key]
        merged_truth.append(merged_row)
    record["truth"] = merged_truth
    MATRIX_PATH.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


def check() -> int:
    record = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    derived = _derived()
    problems: list[str] = []
    if record.get("capability_counts") != derived["capability_counts"]:
        problems.append("capability_counts drifted from the registers")
    for surface, row in derived["capabilities"].items():
        stored = record["capabilities"].get(surface)
        if not stored:
            problems.append(f"{surface} is missing from the matrix")
            continue
        for key in ("sample", "pass", "sample_source", "reproducible", "reproduction_path"):
            if stored.get(key) != row[key]:
                problems.append(f"{surface}.{key} drifted")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def unreproducible_problems(record: dict) -> list[str]:
    """Data that cannot be reproduced may not be presented as a measurement."""

    problems: list[str] = []
    for surface, row in record.get("capabilities", {}).items():
        if not row.get("reproducible", False) and (
            row.get("pass") is not None or row.get("sample") is not None
        ):
            problems.append(
                f"{surface}: unreproducible data must not carry measured numbers"
            )
    return problems


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "check"
    if action == "generate":
        return generate()
    if action == "check":
        return check()
    if action == "show":
        print(json.dumps(_derived(), ensure_ascii=False, indent=2))
        return 0
    print(f"usage: {Path(argv[0]).name} [generate|check|show]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
