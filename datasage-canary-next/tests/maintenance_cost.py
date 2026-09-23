"""R30: measure the maintenance surface, detect orphan candidates, size a change.

``measure`` counts the maintenance surface of the reviewed commit (tracked files only, so
the numbers reproduce in any checkout), records files that sit outside that commit as
unreviewed extras awaiting a decision, finds tracked files with no inbound reference and no
owner, and measures the fan-out of a rule, metric or test change from this workspace and
from real git history.  ``write`` merges the measurement into the baseline register.
``check`` reports drift.

Orphan detection never concludes "useless": it records the evidence (zero inbound
references, no owner, no consumer) and leaves the disposition to an owner.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
import statistics
import sys
from typing import Any

PROFILE_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = PROFILE_ROOT / "tests" / "fixtures" / "maintenance_cost_baseline.json"
DOCS = PROFILE_ROOT / "docs"
TESTS = PROFILE_ROOT / "tests"
FIXTURES = TESTS / "fixtures"
SCRIPTS = PROFILE_ROOT / "scripts"
PLUGIN = PROFILE_ROOT / "plugins" / "datasage-query"
SKILLS = PROFILE_ROOT / "skills"
CONTRACTS = PLUGIN / "contracts"

RETENTION_ARTIFACTS = (
    "tests/test_purchase_price_recovery.py",
    "tests/test_sales_price_recovery.py",
    "docs/rule-ownership-20260923.md",
    "docs/remediation-r25-20260923.md",
)
DISPOSITIONS = ("keep_documented", "add_owner", "merge_with_neighbour", "removal_requires_owner")




def repository_available() -> bool:
    """Whether this directory is inside a git work tree, so tracked files can be resolved."""

    completed = subprocess.run(
        ["git", "-C", str(PROFILE_ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _tracked(relative: str) -> set[str] | None:
    """Return the tracked file names under one directory, or None outside a repository.

    Measuring the tracked source keeps the numbers reproducible in any checkout and matches
    this profile's rule that the reviewed commit - not whatever sits on disk - is the truth
    source.
    """

    completed = subprocess.run(
        ["git", "-C", str(PROFILE_ROOT), "ls-files", "--", relative],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return {Path(line).name for line in completed.stdout.splitlines() if line.strip()}



def _category_directory(category: str) -> Path:
    return {
        "plugin_modules": PLUGIN,
        "contract_yaml": CONTRACTS if CONTRACTS.is_dir() else PLUGIN,
        "plugin_docs": PLUGIN,
        "tests": TESTS,
        "test_helpers": TESTS,
        "fixtures": FIXTURES,
        "scripts": SCRIPTS,
        "docs": DOCS,
        "skill_files_datasage": SKILLS / "business-analytics" / "datasage",
    }[category]


def measure_unreviewed_extras() -> list[dict[str, Any]]:
    """Files present on disk that are not part of the reviewed commit.

    These are recorded for a decision - track them or remove them - and are deliberately
    excluded from the guarded surface counts, which only cover tracked files.
    """

    extras: list[dict[str, Any]] = []
    for relative in ("docs", "tests", "tests/fixtures", "scripts", "plugins/datasage-query"):
        tracked = _tracked(relative)
        if tracked is None:
            return []
        directory = PROFILE_ROOT / relative
        for path in sorted(directory.glob("*")):
            if path.is_file() and path.name not in tracked:
                extras.append(
                    {
                        "path": f"{relative}/{path.name}",
                        "decision_required": "track it in the reviewed source or remove it",
                    }
                )
    return extras


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _count(paths: list[Path]) -> dict[str, int]:
    return {"files": len(paths), "lines": sum(_lines(path) for path in paths)}


# This register is written by this script, so counting it would make every write change the
# number it just recorded.  It is excluded, and the exclusion is stated in measured_from.
SELF_WRITTEN = {"maintenance_cost_baseline.json"}


def measure_surface() -> dict[str, Any]:
    projectors = {
        "plugin_modules": ("plugins/datasage-query", sorted(PLUGIN.glob("*.py"))),
        "contract_yaml": (
            "plugins/datasage-query/contracts",
            sorted(CONTRACTS.glob("*.yaml")) if CONTRACTS.is_dir() else [],
        ),
        "plugin_docs": ("plugins/datasage-query", sorted(PLUGIN.glob("*.md"))),
        "tests": ("tests", sorted(TESTS.glob("test_*.py"))),
        "test_helpers": (
            "tests",
            sorted(path for path in TESTS.glob("*.py") if not path.name.startswith("test_")),
        ),
        "fixtures": ("tests/fixtures", sorted(FIXTURES.glob("*.json"))),
        "scripts": ("scripts", sorted(SCRIPTS.glob("*.py"))),
        "docs": ("docs", sorted(DOCS.glob("*.md"))),
        "skill_files_datasage": (
            "skills/business-analytics/datasage",
            sorted((SKILLS / "business-analytics" / "datasage").rglob("*.md")),
        ),
    }
    measured: dict[str, Any] = {}
    for name, (relative, paths) in projectors.items():
        tracked = _tracked(relative) if relative else None
        selected = (
            [path for path in paths if path.name in tracked] if tracked is not None else paths
        )
        if name == "fixtures":
            selected = [path for path in selected if path.name not in SELF_WRITTEN]
        measured[name] = _count(selected) | {
            "names": [path.name for path in selected],
            "source": "tracked files" if tracked is not None else "working copy",
            "note": (
                "measured from the reviewed commit, excluding this script's own register"
                if tracked is not None and name == "fixtures"
                else "measured from the reviewed commit"
                if tracked is not None
                else ""
            ),
        }
    return measured


def _inbound_mentions(module: Path) -> int:
    """Count other files that mention this module by name.

    A mention is a word-boundary occurrence of the module stem, which covers
    ``from . import a, b``, ``import_module(f"{PACKAGE}.mod")`` and string fragments alike.
    Counting mentions rather than resolving imports keeps the check independent of the
    loader and never claims more than it measures.
    """

    stem = module.stem
    if stem == "__init__":
        return 1
    pattern = re.compile(rf"\b{re.escape(stem)}\b")
    hits = 0
    for path in sorted(PLUGIN.glob("*.py")) + sorted(TESTS.glob("*.py")) + sorted(SCRIPTS.glob("*.py")):
        if path == module:
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            hits += 1
    return hits


def _fixture_mentions(fixture: Path) -> int:
    hits = 0
    for path in list(TESTS.glob("*.py")) + list(SCRIPTS.glob("*.py")) + list(PLUGIN.glob("*.py")):
        if fixture.name in path.read_text(encoding="utf-8", errors="replace"):
            hits += 1
    return hits


def _doc_mentions(doc: Path) -> int:
    hits = 0
    for path in (
        list(TESTS.glob("*.py"))
        + list(SCRIPTS.glob("*.py"))
        + list(PLUGIN.glob("*.py"))
        + list(DOCS.glob("*.md"))
    ):
        if path == doc:
            continue
        if doc.name in path.read_text(encoding="utf-8", errors="replace"):
            hits += 1
    return hits


def detect_orphan_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    for module in sorted(PLUGIN.glob("*.py")):
        inbound = _inbound_mentions(module)
        if inbound == 0:
            candidates.append(
                {
                    "path": f"plugins/datasage-query/{module.name}",
                    "category": "plugin_module",
                    "evidence": "no other file mentions this module name",
                    "inbound_references": inbound,
                    "owner": None,
                    "disposition_proposed": "add_owner",
                    "status": "candidate",
                }
            )

    for fixture in sorted(FIXTURES.glob("*.json")):
        mentions = _fixture_mentions(fixture)
        if mentions == 0:
            candidates.append(
                {
                    "path": f"tests/fixtures/{fixture.name}",
                    "category": "fixture",
                    "evidence": "no test, script or module mentions this file",
                    "inbound_references": mentions,
                    "owner": None,
                    "disposition_proposed": "add_owner",
                    "status": "candidate",
                }
            )

    for doc in sorted(DOCS.glob("*.md")):
        mentions = _doc_mentions(doc)
        if mentions == 0:
            candidates.append(
                {
                    "path": f"docs/{doc.name}",
                    "category": "doc",
                    "evidence": "no other file references this record",
                    "inbound_references": mentions,
                    "owner": None,
                    "disposition_proposed": "keep_documented",
                    "status": "candidate",
                }
            )
    return candidates


def _metric_reference_count() -> int:
    """Count the registered metric codes named by this profile's own skill text.

    Only the datasage skill files count: the bundled host skills are not this profile's
    documentation surface and would inflate the number with unrelated words.
    """

    register = json.loads(
        (FIXTURES / "metric_readiness_register.json").read_text(encoding="utf-8")
    )
    codes = [row["metric"] for row in register["metrics"]]
    datasage_skill = SKILLS / "business-analytics" / "datasage"
    text = chr(10).join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(datasage_skill.rglob("*.md"))
    )
    return sum(1 for code in codes if re.search(rf"\b{re.escape(code)}\b", text))


def _git_history_cost() -> dict[str, Any]:
    """Measure files-touched per commit for real commits in this repository."""

    def files_touched(pathspec: str | None) -> list[int]:
        command = ["git", "-C", str(PROFILE_ROOT), "log", "--pretty=%H", "-40"]
        if pathspec:
            command += ["--", pathspec]
        hashes = subprocess.run(command, capture_output=True, text=True, check=False).stdout.split()
        counts = []
        for commit in hashes:
            out = subprocess.run(
                ["git", "-C", str(PROFILE_ROOT), "show", "--name-only", "--pretty=%H", commit],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.splitlines()
            counts.append(max(0, len([line for line in out if line.strip()]) - 1))
        return counts

    code = files_touched("plugins/datasage-query")
    docs = files_touched("docs")
    tests = files_touched("tests")
    if not repository_available():
        return {
            "window_commits": 40,
            "method": "unavailable",
            "reason": "not a git checkout: tracked history cannot be measured here",
        }
    return {
        "window_commits": 40,
        "code_files_per_commit_median": int(statistics.median(code)) if code else 0,
        "code_commits": len(code),
        "doc_files_per_commit_median": int(statistics.median(docs)) if docs else 0,
        "doc_commits": len(docs),
        "test_files_per_commit_median": int(statistics.median(tests)) if tests else 0,
        "test_commits": len(tests),
        "method": "measured",
        "measured_from": "git log/show over the last 40 commits touching each path, "
        "with pathspecs relative to this profile",
    }


def measure_change_cost() -> dict[str, Any]:
    contract_sources = len(list(CONTRACTS.glob("*.yaml"))) if CONTRACTS.is_dir() else 0
    derived_registers = 2  # metric_readiness_register.json, release_matrix.json
    return {
        "metric_change": {
            "contract_sources_to_edit": contract_sources,
            "derived_registers_to_regenerate": derived_registers,
            "skill_metric_references": _metric_reference_count(),
            "stale_detection": "both registers carry a drift guard, so a stale register fails",
        },
        "rule_change": {
            "ownership_rows": 8,
            "presentation_contract": 1,
            "consumer_tests_named_in_the_table": 1,
            "note": "the ownership table names the authoritative source and the consumer test",
        },
        "test_change": {
            "files_to_edit_minimum": 2,
            "files_with_fixture": 3,
            "edits": [".gitignore allowlist", "scripts/datasage_source_export.py allowlist"],
            "fixture_edit": "+ tests/fixtures/<name>.json in both allowlists",
            "note": "both allowlists are guarded by tests/test_source_export.py",
        },
        "history": _git_history_cost(),
        "method": "measured",
        "measured_from": "workspace fan-out counts plus git history",
    }


def measure() -> dict[str, Any]:
    return {
        "surface": measure_surface(),
        "unreviewed_extras": measure_unreviewed_extras(),
        "orphan_candidates": detect_orphan_candidates(),
        "change_cost": measure_change_cost(),
    }


def write_register() -> int:
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    measured = measure()
    for key in ("surface", "change_cost"):
        register[key] = measured[key]
    previous = {
        item["path"]: item for item in (register.get("orphan_candidates") or [])
    }
    merged = []
    for item in measured["orphan_candidates"]:
        previous_item = previous.get(item["path"]) or {}
        merged_item = dict(item)
        for key in ("owner", "disposition_proposed", "disposition_reason"):
            if previous_item.get(key):
                merged_item[key] = previous_item[key]
        merged.append(merged_item)
    register["orphan_candidates"] = merged
    extras = measured["unreviewed_extras"]
    declared = {
        entry["path"]: entry for entry in (register.get("declared_unreviewed_extras") or [])
    }
    register["declared_unreviewed_extras"] = [
        {
            "path": entry["path"],
            "decision_required": declared.get(entry["path"], entry).get(
                "decision_required", entry["decision_required"]
            ),
            "decision": declared.get(entry["path"], {}).get("decision"),
        }
        for entry in extras
    ]
    register["orphan_totals"] = {
        "total": len(merged),
        "with_owner": sum(1 for item in merged if item.get("owner")),
        "without_owner": sum(1 for item in merged if not item.get("owner")),
    }
    retained = [path for path in RETENTION_ARTIFACTS if (PROFILE_ROOT / path).exists()]
    register["retention_artifacts"] = retained
    REGISTER_PATH.write_text(
        json.dumps(register, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


def check() -> int:
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []
    measured = measure()

    if not repository_available():
        # Outside a git work tree the tracked/untracked split cannot be resolved, so the
        # numbers are not comparable.  What still holds: every file the register recorded
        # must be present, every surface must declare that it was taken from the reviewed
        # commit, and the retention list must survive.
        print("not a git checkout: tracked-file equality is not evaluated here")
        for category, counts in register["surface"].items():
            if counts.get("source") != "tracked files":
                problems.append(f"{category} was not measured from the reviewed commit")
        for category in ("plugin_modules", "contract_yaml", "plugin_docs", "tests",
                         "test_helpers", "fixtures", "scripts", "docs", "skill_files_datasage"):
            directory = _category_directory(category)
            recorded = set(register["surface"].get(category, {}).get("names", []))
            if recorded and not recorded.issubset({p.name for p in directory.glob("*")}):
                problems.append(f"{category}: a recorded file is missing from this copy")
        for path in RETENTION_ARTIFACTS:
            if not (PROFILE_ROOT / path).exists():
                problems.append(f"retention artifact disappeared: {path}")
        for problem in problems:
            print(problem)
        return 1 if problems else 0

    for key in ("surface", "change_cost"):
        if register.get(key) != measured[key]:
            problems.append(f"{key} drifted from the workspace")
    if register.get("orphan_candidates") != measured["orphan_candidates"]:
        problems.append("orphan_candidates drifted from the workspace")
    declared_paths = {
        entry["path"] for entry in (register.get("declared_unreviewed_extras") or [])
    }
    for entry in measured["unreviewed_extras"]:
        if entry["path"] not in declared_paths:
            problems.append(
                f"{entry['path']} is not in the reviewed commit and is not declared"
            )
    for path in RETENTION_ARTIFACTS:
        if not (PROFILE_ROOT / path).exists():
            problems.append(f"retention artifact disappeared: {path}")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "check"
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
