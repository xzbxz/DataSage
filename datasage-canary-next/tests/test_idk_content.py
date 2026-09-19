import importlib
import unittest

import test_business_contracts as base


content = importlib.import_module(base.TEST_PACKAGE + ".idk_content")


class IdkContentTests(unittest.TestCase):
    def test_empty_source_has_no_body_parts_but_is_auditable(self):
        result = content.build_idk_parts([], observed_at="2026-09-19 12:00:00")
        self.assertEqual((), result.parts)
        self.assertEqual("empty_no_task", result.summary["status"])
        self.assertEqual(0, result.summary["source_row_count"])

    def test_preserves_duplicate_source_rows_and_global_numbers(self):
        rows = [
            {"product": "FP-中文", "color": "สีแดง"},
            {"product": "FP-中文", "color": "สีแดง"},
            {"product": "FP-2", "color": "[Normal]"},
        ]
        result = content.build_idk_parts(rows, observed_at="2026-09-19 12:00:00")
        body = "\n".join(result.parts)
        self.assertEqual(3, result.summary["source_row_count"])
        self.assertEqual(2, body.count("FP-中文"))
        self.assertIn("1. FP-中文 | Color สีแดง", body)
        self.assertIn("2. FP-中文 | Color สีแดง", body)
        self.assertIn("3. FP-2 | Color \\[Normal\\]", body)

    def test_window_and_asof_are_repeated_in_every_part(self):
        rows = [{"product": f"P-{index}", "color": "C"} for index in range(30)]
        result = content.build_idk_parts(rows, observed_at="2026-09-19 12:00:00", window_start="2026-09-12 12:00:00", window_days=7, max_bytes=400)
        self.assertGreater(result.summary["part_count"], 1)
        for index, part in enumerate(result.parts, 1):
            self.assertIn("Scope: new in last 7 days (created >= 2026-09-12 12:00:00; observed at 2026-09-19 12:00:00)", part)
            self.assertIn(f"Part {index}/{len(result.parts)}", part)
            self.assertLessEqual(len(part.encode("utf-8")), 400)

    def test_single_extreme_row_blocks_instead_of_truncating(self):
        with self.assertRaisesRegex(content.IdkContentError, "SINGLE_SOURCE_ROW"):
            content.build_idk_parts([{"product": "X" * 5000, "color": "C"}], observed_at="2026-09-19 12:00:00")

    def test_budget_must_not_exceed_official_markdown_ceiling(self):
        with self.assertRaisesRegex(content.IdkContentError, "BYTE_BUDGET"):
            content.build_idk_parts([], observed_at="2026-09-19 12:00:00", max_bytes=4097)

    def test_window_requires_real_database_cutoff(self):
        with self.assertRaisesRegex(content.IdkContentError, "WINDOW_START_INVALID"):
            content.build_idk_parts([], observed_at="2026-09-19 12:00:00", window_days=7)

    def test_invalid_source_identity_cannot_be_reported_as_zero_or_unknown(self):
        with self.assertRaisesRegex(content.IdkContentError, "SOURCE_PRODUCT_ID_MISSING"):
            content.build_idk_parts([{"color": "C"}], observed_at="2026-09-19 12:00:00")
        with self.assertRaisesRegex(content.IdkContentError, "SOURCE_REF_MISSING"):
            content.build_idk_parts([{"source_ref": None, "product": "P", "color": "C"}], observed_at="2026-09-19 12:00:00")


if __name__ == "__main__":
    unittest.main()
