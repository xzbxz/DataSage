"""Metric readiness register for the 215 governed metrics (R16).

The contracts say a metric is *available* unless it declares otherwise
(``capability_contract.validate_availability`` returns ``available`` when there is
no ``availability`` block), so most metrics are available by omission and carry no
owner or evidence.  This module turns that implicit state into an explicit,
reviewable register: one row per registered metric, with the contract facts and a
verification section that only a data owner can fill.

Regenerating merges the existing verification records, so a human's evidence is
never overwritten by a rebuild.

Usage (from the profile root):

    python -B tests/metric_readiness.py check       # validate the committed register
    python -B tests/metric_readiness.py generate    # rewrite the contract-derived fields
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import date
from pathlib import Path

SCHEMA = "datasage-metric-readiness/v1"
REGISTER_PATH = "tests/fixtures/metric_readiness_register.json"
PLUGIN_RELATIVE = "plugins/datasage-query"

DECLARED_STATUSES = ("available", "pending_validation", "blocked")
# An independent record has to come from outside the engine under test.
EVIDENCE_KINDS = ("independent_sql", "manual_fixture", "owner_statement")
ROW_KEYS = (
    "domain",
    "metric",
    "label",
    "unit",
    "time_policy",
    "declared_availability",
    "declared_by_contract",
    "owner",
    "activation_gate",
    "remaining_checks",
    "verified",
    "independent_evidence",
)
POLICY = (
    "declared_availability describes the contract, not verification: a metric is "
    "'available' when it declares no unavailable status.  verified=true requires at "
    "least one independent evidence record; pending or blocked metrics stay "
    "unverified however useful they look."
)


def _load_plugin_module(plugin_root: Path, name: str):
    package_name = "_metric_readiness_plugin"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(plugin_root)]
        sys.modules[package_name] = package
    qualified = f"{package_name}.{name}"
    module = sys.modules.get(qualified)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(qualified, plugin_root / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def contract_rows(profile_root: Path) -> list[dict]:
    """One row per registered metric, built from the plugin's own contract loaders."""

    plugin_root = profile_root / PLUGIN_RELATIVE
    capability = _load_plugin_module(plugin_root, "capability_contract")
    contracts = _load_plugin_module(plugin_root, "contracts")

    rows: list[dict] = []
    for domain in capability.SUPPORTED_DOMAINS:
        _datasets, semantics = contracts.execution_contracts(domain)
        for code in sorted((semantics.get("metrics") or {}).keys()):
            definition = semantics["metrics"][code]
            if not isinstance(definition, dict):
                continue
            availability = definition.get("availability")
            declared = "available"
            owner = None
            gate_state = None
            remaining: list[str] = []
            if isinstance(availability, dict):
                declared = str(availability.get("status") or "available")
                owner = _text(availability.get("owner"))
                gate = availability.get("activation_gate")
                if isinstance(gate, dict):
                    gate_state = _text(gate.get("state"))
                    checks = gate.get("remaining_candidate_checks") or []
                    remaining = [str(item) for item in checks]
            rows.append(
                {
                    "domain": domain,
                    "metric": str(code),
                    "label": _text(definition.get("label")) or str(code),
                    "unit": _text(definition.get("unit")),
                    "time_policy": _text(definition.get("time_policy")),
                    "declared_availability": declared,
                    "declared_by_contract": isinstance(availability, dict),
                    "owner": owner,
                    "activation_gate": gate_state,
                    "remaining_checks": remaining,
                    "verified": False,
                    "independent_evidence": [],
                }
            )
    rows.sort(key=lambda row: (row["domain"], row["metric"]))
    return rows


def _existing_verification(register: dict | None) -> dict[tuple[str, str], dict]:
    preserved: dict[tuple[str, str], dict] = {}
    if not isinstance(register, dict):
        return preserved
    for row in register.get("metrics") or []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("domain")), str(row.get("metric")))
        evidence = row.get("independent_evidence") or []
        if evidence or row.get("verified"):
            preserved[key] = {
                "verified": bool(row.get("verified")),
                "independent_evidence": list(evidence),
            }
    return preserved


def build_register(profile_root: Path, existing: dict | None = None) -> dict:
    rows = contract_rows(profile_root)
    preserved = _existing_verification(existing)
    for row in rows:
        kept = preserved.get((row["domain"], row["metric"]))
        if kept:
            row.update(kept)
    return {
        "schema": SCHEMA,
        "generated_from": [
            f"{PLUGIN_RELATIVE}/contracts/{domain_contract}"
            for domain_contract in _contract_files(profile_root)
        ],
        "policy": POLICY,
        "not_a_release_gate": True,
        "metrics": rows,
        "summary": {
            "total": len(rows),
            "declared_available": sum(
                1 for row in rows if row["declared_availability"] == "available"
            ),
            "declared_by_contract": sum(
                1 for row in rows if row["declared_by_contract"]
            ),
            "pending_or_blocked": sum(
                1 for row in rows if row["declared_availability"] != "available"
            ),
            "verified": sum(1 for row in rows if row["verified"]),
            "unverified": sum(1 for row in rows if not row["verified"]),
        },
    }


def _contract_files(profile_root: Path) -> list[str]:
    plugin_root = profile_root / PLUGIN_RELATIVE
    capability = _load_plugin_module(plugin_root, "capability_contract")
    return sorted(
        str(source.get("semantics", "")).split("/")[-1]
        for source in capability.DOMAIN_SOURCES.values()
    )


def _valid_evidence(record) -> str | None:
    """Return a problem string, or None when the record satisfies the shape."""

    if not isinstance(record, dict):
        return "evidence record must be a mapping"
    required = {"kind", "reference", "artifact_sha256", "approved_by", "approved_on"}
    if set(record) != required:
        return f"evidence record must contain exactly {sorted(required)}"
    if record["kind"] not in EVIDENCE_KINDS:
        return f"evidence kind must be one of {list(EVIDENCE_KINDS)}"
    reference = str(record["reference"] or "").strip()
    if not reference:
        return "evidence reference must not be empty"
    digest = str(record["artifact_sha256"] or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return "artifact_sha256 must be a 64-character hex digest"
    if not str(record["approved_by"] or "").strip():
        return "approved_by must name the approver"
    approved_on = str(record["approved_on"] or "").strip()
    try:
        date.fromisoformat(approved_on)
    except ValueError:
        return "approved_on must be an ISO date"
    return None


def validate_register(register: dict) -> list[str]:
    """Return every problem found in a register (empty list means valid)."""

    problems: list[str] = []
    if not isinstance(register, dict):
        return ["register must be a mapping"]
    if register.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    rows = register.get("metrics")
    if not isinstance(rows, list) or not rows:
        return problems + ["metrics must be a non-empty list"]

    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        label = f"metrics[{index}]"
        if not isinstance(row, dict):
            problems.append(f"{label} must be a mapping")
            continue
        missing = [key for key in ROW_KEYS if key not in row]
        if missing:
            problems.append(f"{label} is missing {missing}")
            continue
        key = (str(row["domain"]), str(row["metric"]))
        if key in seen:
            problems.append(f"{label} duplicates {key[0]}/{key[1]}")
        seen.add(key)
        if row["declared_availability"] not in DECLARED_STATUSES:
            problems.append(
                f"{label} has an unsupported status {row['declared_availability']!r}"
            )
        evidence = row["independent_evidence"]
        if not isinstance(evidence, list):
            problems.append(f"{label} independent_evidence must be a list")
            evidence = []
        if row["verified"] and not evidence:
            problems.append(
                f"{label} is verified without an independent evidence record"
            )
        for position, record in enumerate(evidence):
            problem = _valid_evidence(record)
            if problem:
                problems.append(f"{label}.independent_evidence[{position}]: {problem}")
        if row["verified"] and row["declared_availability"] != "available":
            problems.append(
                f"{label} is verified while the contract declares "
                f"{row['declared_availability']!r}"
            )

    summary = register.get("summary")
    if not isinstance(summary, dict):
        problems.append("summary must be a mapping")
    else:
        expected = {
            "total": len(rows),
            "declared_available": sum(
                1 for row in rows if row.get("declared_availability") == "available"
            ),
            "declared_by_contract": sum(
                1 for row in rows if row.get("declared_by_contract")
            ),
            "pending_or_blocked": sum(
                1
                for row in rows
                if row.get("declared_availability") not in (None, "available")
            ),
            "verified": sum(1 for row in rows if row.get("verified")),
            "unverified": sum(1 for row in rows if not row.get("verified")),
        }
        for key, value in expected.items():
            if summary.get(key) != value:
                problems.append(
                    f"summary.{key} is {summary.get(key)!r}, rows say {value!r}"
                )
    return problems


def load_register(profile_root: Path) -> dict:
    path = profile_root / REGISTER_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def write_register(profile_root: Path, register: dict) -> Path:
    path = profile_root / REGISTER_PATH
    path.write_text(
        json.dumps(register, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def _profile_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    profile_root = _profile_root()
    command = argv[1] if len(argv) > 1 else "check"
    if command == "generate":
        existing = None
        if (profile_root / REGISTER_PATH).exists():
            existing = load_register(profile_root)
        register = build_register(profile_root, existing)
        problems = validate_register(register)
        if problems:
            print("register is structurally invalid; not written:")
            for problem in problems[:20]:
                print("  -", problem)
            return 1
        path = write_register(profile_root, register)
        print(f"wrote {path} ({register['summary']['total']} metrics)")
        return 0
    if command == "check":
        register = load_register(profile_root)
        problems = validate_register(register)
        fresh = build_register(profile_root, register)
        if register.get("metrics") != fresh.get("metrics"):
            problems.append(
                "contract-derived fields drifted from the contracts; run generate"
            )
        if problems:
            print(f"{len(problems)} problem(s):")
            for problem in problems[:20]:
                print("  -", problem)
            return 1
        summary = register["summary"]
        print(
            "register ok:",
            f"total={summary['total']}",
            f"declared_available={summary['declared_available']}",
            f"declared_by_contract={summary['declared_by_contract']}",
            f"verified={summary['verified']}",
        )
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
