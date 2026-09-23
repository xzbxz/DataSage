"""R26: what a report artifact may contain, and what it must refuse.

Injects the R26 list - formula-shaped text, extreme decimals and exponents, invisible
formatting characters, hostile artifact names, oversized and non-finite values - and checks
the artifact stays readable, exact, contained and honest about its own identity basis.
"""

from __future__ import annotations

import importlib
import json
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unicodedata
import unittest
import zipfile

PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_r26_artifact_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

legacy_xlsx = importlib.import_module(f"{PACKAGE}.legacy_xlsx")
local_report = importlib.import_module(f"{PACKAGE}.local_report")
query_execution = importlib.import_module(f"{PACKAGE}.query_execution")
db_security = importlib.import_module(f"{PACKAGE}.db_security")

FORMULA_SHAPED = (
    "=1+1",
    "=cmd|'/C calc'!A0",
    "@SUM(A1:A9)",
    "+1+1",
    "-1-1",
    "\t=1+1",
    "=HYPERLINK(\"http://example.invalid\")",
)
INVISIBLE = ("\u200b", "\u202e", "\u2060", "\u200e", "\u2066")


def _sheet_xml(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read("xl/worksheets/sheet1.xml")


class ArtifactIntegrityTests(unittest.TestCase):
    def test_formula_shaped_text_never_becomes_a_spreadsheet_formula(self) -> None:
        with TemporaryDirectory() as tmp:
            book = Path(tmp) / "formula.xlsx"
            rows = [[value, index] for index, value in enumerate(FORMULA_SHAPED)]
            legacy_xlsx.gen_workbook_xlsx(
                [("Detail", ["Label", "Index"], rows)], book
            )
            xml = _sheet_xml(book)
            self.assertNotIn(b"<f>", xml)
            self.assertNotIn(b"<f ", xml)
            for value in FORMULA_SHAPED:
                with self.subTest(value=value):
                    fragment = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    self.assertIn(f"<t>{fragment}</t>".encode("utf-8"), xml)

    def test_extreme_decimals_keep_their_exact_value_and_amounts_stay_numeric(self) -> None:
        extremes = (Decimal("1E-320"), Decimal("123456789012345.67"), 2**63 + 1)
        for value in extremes:
            with self.subTest(value=str(value)):
                self.assertTrue(
                    legacy_xlsx.requires_exact_text(value),
                    "an exactness-critical value must not go through a float",
                )
        with TemporaryDirectory() as tmp:
            book = Path(tmp) / "decimals.xlsx"
            rows = [[value] for value in (*extremes, Decimal("100.15"))]
            legacy_xlsx.gen_workbook_xlsx([("Detail", ["Value"], rows)], book)
            xml = _sheet_xml(book)
            for value in extremes:
                with self.subTest(value=str(value)):
                    exact = legacy_xlsx._exact_text(value)
                    self.assertIn(
                        f"<t>{exact}</t>".encode("utf-8"),
                        xml,
                        "the exact decimal text must survive into the cell",
                    )
            self.assertIn(b"<v>100.15</v>", xml, "an ordinary amount stays numeric")
            self.assertIn('t="inlineStr"'.encode("utf-8"), xml)

    def test_invisible_formatting_characters_never_reach_a_cell_or_a_message(self) -> None:
        hostile = "客户\u202eA\u200bB\u2060C\u200e-备注"
        cleaned = legacy_xlsx._xml_text(hostile)
        self.assertFalse(
            any(unicodedata.category(character) == "Cf" for character in cleaned)
        )
        self.assertIn("客户ABC-备注", cleaned)

        # A zero-width or bidi control must not survive as an invalid XML byte either.
        self.assertNotIn("\x00", legacy_xlsx._xml_text("a\x00b"))
        self.assertIn("\ufffd", legacy_xlsx._xml_text("a\x00b"))

        title = legacy_xlsx._safe_workbook_sheet_title("A\u202eB\u200bC", set())
        self.assertFalse(
            any(unicodedata.category(character) == "Cf" for character in title)
        )
        with TemporaryDirectory() as tmp:
            book = Path(tmp) / "invisible.xlsx"
            legacy_xlsx.gen_workbook_xlsx(
                [("S\u202eheet", ["Value"], [[hostile]])], book
            )
            xml = _sheet_xml(book)
            self.assertFalse(
                any(unicodedata.category(character) == "Cf" for character in xml.decode("utf-8"))
            )

        message = local_report._display(hostile)
        self.assertFalse(
            any(unicodedata.category(character) == "Cf" for character in message)
        )
        self.assertIn("客户ABC-备注", message)

    def test_oversized_or_non_finite_values_stop_loudly_instead_of_truncating(self) -> None:
        self.assertEqual("ok", query_execution._json_value("ok"))

        with self.assertRaises(query_execution.QueryFailure) as oversized:
            query_execution._json_value("x" * 5000)
        self.assertEqual("OUTPUT_TOO_LARGE", oversized.exception.code)
        self.assertIn("停止", str(oversized.exception))

        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(query_execution.QueryFailure) as bad:
                    query_execution._json_value(value)
                self.assertEqual("CONTRACT_UNAVAILABLE", bad.exception.code)

    def test_artifact_names_cannot_escape_the_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            profile.mkdir()
            report = {"name": "../../evil.html", "report_id": "slow_report"}
            run = local_report.save_artifacts(profile, report, "text body")
            self.assertEqual(
                {"report.json", "report.txt"},
                {path.name for path in run.iterdir()},
            )
            self.assertRegex(run.name, r"^[0-9a-f]{32}$")
            self.assertEqual((profile / "report_runs" / "slow").resolve(), run.parent.resolve())
            self.assertEqual([], list(Path(tmp).glob("**/evil*")))

            outside = Path(tmp) / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            with self.assertRaises(db_security.DatabaseSecurityError):
                db_security._safe_path_in_approved_root(
                    "relative/path.txt", profile_root=profile
                )
            with self.assertRaises(db_security.DatabaseSecurityError):
                db_security._safe_path_in_approved_root(
                    outside, profile_root=profile
                )

    def test_artifacts_carry_only_reviewed_keys_and_declare_their_identity_basis(self) -> None:
        with TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            profile.mkdir()
            report = {"report_id": "slow_report", "query": {"answer_scope_line": "范围"}}
            run = local_report.save_artifacts(profile, report, "text body")
            document = json.loads((run / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(set(report) | {"runtime"}, set(document))
            runtime = document["runtime"]
            self.assertEqual(
                {
                    "profile_home",
                    "python_executable",
                    "python_prefix",
                    "pid",
                    "identity_basis",
                },
                set(runtime),
            )
            self.assertIn("not WeCom authorization", runtime["identity_basis"])
            self.assertEqual("text body", (run / "report.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
