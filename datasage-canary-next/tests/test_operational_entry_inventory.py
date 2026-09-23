"""R25 guards: the operational entries, and what they are allowed to do.

The inventory in docs/remediation-r25-20260923.md claims three things a test can hold it
to: every entry is accounted for, no job is registered and no send switch is on without
being recorded there, and the query path never reads an operator-only file.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
SCRIPTS_ROOT = PROFILE_ROOT / "scripts"
INVENTORY = PROFILE_ROOT / "docs" / "remediation-r25-20260923.md"
BINDINGS = PROFILE_ROOT / "local-report-bindings.json"
OPERATOR_ONLY_FILES = ("local-report-bindings.json",)
OPERATOR_MODULES = ("local_report.py",)
REPORT_IDS = (
    "slow_task",
    "slow_report",
    "idk",
    "sales_price",
    "purchase_price",
    "fabric",
)


def _inventory_rows() -> dict[str, list[list[str]]]:
    """Map each documented script to the cells of every table row naming it."""

    rows: dict[str, list[list[str]]] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*`(datasage_[a-z_0-9]+\.py)`\s*\|(.*)\|\s*$", line)
        if not match:
            continue
        cells = [cell.strip() for cell in match.group(2).split("|")]
        rows.setdefault(match.group(1), []).append(cells)
    return rows


class OperationalEntryInventoryTests(unittest.TestCase):
    def test_every_operational_script_is_accounted_for(self) -> None:
        on_disk = sorted(path.name for path in SCRIPTS_ROOT.glob("datasage_*.py"))
        self.assertTrue(on_disk, "expected operational scripts under scripts/")
        rows = _inventory_rows()
        missing = [name for name in on_disk if name not in rows]
        self.assertEqual([], missing, "document these entries in the R25 inventory")

    def test_each_entry_states_its_side_effects_and_lifecycle(self) -> None:
        rows = _inventory_rows()
        self.assertTrue(rows)
        for name, table_rows in rows.items():
            with self.subTest(entry=name):
                # Table A: owner, path, label, default behaviour
                first = table_rows[0]
                self.assertGreaterEqual(len(first), 4)
                for cell in first[:4]:
                    self.assertTrue(cell, f"{name} has an empty table A cell")
                # Table B: write, send, expiry, audit
                if len(table_rows) > 1:
                    second = table_rows[1]
                    self.assertGreaterEqual(len(second), 4)
                    for cell in second[:4]:
                        self.assertTrue(cell, f"{name} has an empty table B cell")

    def test_no_schedule_is_registered_for_this_profile(self) -> None:
        jobs_file = PROFILE_ROOT / "cron" / "jobs.json"
        if jobs_file.is_file():
            payload = json.loads(jobs_file.read_text(encoding="utf-8"))
            jobs = payload if isinstance(payload, list) else payload.get("jobs", [])
            self.assertEqual(
                [],
                jobs,
                "a registered job must be recorded in the R25 inventory first",
            )
        self.assertIn("没有注册任何调度作业", INVENTORY.read_text(encoding="utf-8"))

    def test_no_send_or_registration_switch_is_on_without_a_record(self) -> None:
        text = INVENTORY.read_text(encoding="utf-8")
        if not BINDINGS.is_file():
            self.skipTest("operator-local bindings are not present in this checkout")
        payload = json.loads(BINDINGS.read_text(encoding="utf-8"))
        reports = payload.get("reports") or {}
        self.assertTrue(reports, "bindings must name at least one report")
        for report_id, settings in reports.items():
            with self.subTest(report=report_id):
                self.assertIsInstance(settings, dict)
                for switch in ("send_enabled", "register_schedule_enabled"):
                    self.assertIn(switch, settings)
                    if settings[switch]:
                        self.assertIn(
                            report_id,
                            text,
                            f"{report_id} has {switch}=true but is not in the inventory",
                        )
                if settings.get("register_schedule_enabled"):
                    self.assertIsNotNone(settings.get("schedule"))
                else:
                    self.assertIsNone(
                        settings.get("schedule"),
                        "a schedule without registration permission is ambiguous",
                    )

    def test_the_query_path_never_reads_an_operator_only_file(self) -> None:
        offenders: list[str] = []
        for path in sorted(PLUGIN_ROOT.glob("*.py")) + sorted(SCRIPTS_ROOT.glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in OPERATOR_ONLY_FILES:
                if marker in text and path.name not in OPERATOR_MODULES + (
                    "datasage_slow_report.py",
                ):
                    offenders.append(f"{path.name} reads {marker}")
        self.assertEqual([], offenders)

    def test_preview_and_run_labels_stay_the_only_side_effect_labels(self) -> None:
        module = (PLUGIN_ROOT / "local_report.py").read_text(encoding="utf-8")
        for flag in ("--legacy-preview", "--legacy-run"):
            with self.subTest(flag=flag):
                self.assertIn(flag, module)
        preview = re.search(r"--legacy-preview'?,\s*choices=\[([^\]]+)\]", module)
        run = re.search(r"--legacy-run'?,\s*choices=\[([^\]]+)\]", module)
        if preview is None or run is None:
            self.fail("both labels must list their explicit report ids")
        for match in (preview, run):
            ids = [item.strip().strip("'\"") for item in match.group(1).split(",")]
            self.assertEqual(sorted(REPORT_IDS), sorted(ids))
        doc = INVENTORY.read_text(encoding="utf-8")
        self.assertIn("`--legacy-preview`（不发送）", doc)
        self.assertIn("`--legacy-run`（显式受门）", doc)

    def test_an_unconfigured_profile_refuses_to_run_a_report(self) -> None:
        entry = (SCRIPTS_ROOT / "datasage_slow_report.py").read_text(encoding="utf-8")
        self.assertIn("REPORT_NOT_CONFIGURED", entry)
        self.assertIn("WORKFLOW_EXECUTION_NOT_ENABLED", entry)


if __name__ == "__main__":
    unittest.main()
