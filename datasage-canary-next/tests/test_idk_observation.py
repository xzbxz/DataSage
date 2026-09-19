import importlib
from datetime import datetime
from pathlib import Path
import unittest
from unittest import mock

import test_business_contracts as base


PROFILE_ROOT = Path(__file__).resolve().parents[1]
ops = importlib.import_module(base.TEST_PACKAGE + ".operations")
tools = importlib.import_module(base.TEST_PACKAGE + ".tools")
local_report = importlib.import_module(base.TEST_PACKAGE + ".local_report")
workflow = importlib.import_module(base.TEST_PACKAGE + ".legacy_workflow")


class IdkObservationTests(unittest.TestCase):
    def test_window_sql_keeps_idk_non_ht_source_row_predicates_and_limit_plus_one(self):
        sql, params = ops.build_observation({"kind": "idk_unpriced", "limit": 10, "window_days": 7})
        self.assertIn("whse_dept=%s", sql)
        self.assertIn("goods_num>%s", sql)
        self.assertIn("is_whitelist=%s", sql)
        self.assertIn("promotion_price IS NULL OR promotion_price<=0", sql)
        self.assertIn("gmt_create>=DATE_SUB(NOW(6),INTERVAL %s DAY)", sql)
        self.assertIn("ORDER BY gmt_create DESC,goods_no,id LIMIT %s", sql)
        self.assertNotIn("DISTINCT", sql.upper())
        self.assertEqual(["IDK", 10, "n", 7, 11], params)
        self.assertNotIn("IDK-HT", sql)

    def test_execute_and_preview_preserve_window_cutoff_and_actual_clock(self):
        binding = {"kind": "idk_unpriced", "limit": 10, "window_days": 7}
        observed = "2026-03-01 12:00:00"
        rows = [{
            "id": 101,
            "goods_no": "SYN-IDK",
            "goods_name": "Synthetic",
            "attr_val": "Red",
            "color_label": "Red",
            "source_unit": "m",
            "goods_num": "11",
            "piece_num": "1",
            "promotion_price": None,
            "gmt_create": "2026-02-25 10:00:00",
            "gmt_modified": "2026-02-25 10:00:00",
        }]

        profile = PROFILE_ROOT
        rows_with_clock = [{**rows[0], "observed_at": observed}]
        def fake_execute(sql, params, limit):
            self.assertIn("gmt_create>=DATE_SUB(NOW(6),INTERVAL %s DAY)", sql)
            self.assertEqual(11, params[-1])
            return rows_with_clock, False, {"read_only": True}
        with mock.patch.object(local_report, "_assert_local_context"), mock.patch.object(tools, "_execute_with_source", side_effect=fake_execute):
            result = ops.execute(profile, "idk-test", binding)
        self.assertEqual("2026-03-01 12:00:00", result["observed_at"])
        self.assertEqual("2026-02-22 12:00:00", result["window_start"])
        self.assertTrue(result["selection_complete"])
        preview = workflow.operation_preview_input("idk", result)
        self.assertEqual("2026-03-01 12:00:00", preview["observed_at"])
        self.assertEqual(7, preview["window_days"])
        self.assertEqual("2026-02-22 12:00:00", preview["window_start"])

    def test_duplicate_source_ids_are_rejected_before_complete_zero_or_summary(self):
        rows = [{"id": 1, "goods_no": "A", "goods_num": 11, "piece_num": 1, "promotion_price": None}, {"id": 1, "goods_no": "B", "goods_num": 12, "piece_num": 1, "promotion_price": 0}]
        with mock.patch.object(local_report, "_assert_local_context"), mock.patch.object(tools, "_execute_with_source", return_value=(rows, False, {})):
            with self.assertRaises(Exception):
                ops.execute(PROFILE_ROOT, "idk-duplicate", {"kind": "idk_unpriced", "limit": 10})

    def test_truncated_source_is_not_complete(self):
        with mock.patch.object(local_report, "_assert_local_context"), mock.patch.object(tools, "_execute_with_source", return_value=([{"id": i, "goods_no": f"P{i}", "goods_num": 11, "piece_num": 1, "promotion_price": None, "observed_at": "2026-03-01 12:00:00"} for i in range(11)], True, {})):
            with self.assertRaises(Exception):
                ops.execute(PROFILE_ROOT, "idk-truncated", {"kind": "idk_unpriced", "limit": 10})

    def test_zero_rows_require_a_real_observation_clock(self):
        with mock.patch.object(local_report, "_assert_local_context"), mock.patch.object(tools, "_execute_with_source", return_value=([], False, {})):
            with self.assertRaises(Exception):
                ops.execute(PROFILE_ROOT, "idk-no-clock", {"kind": "idk_unpriced", "limit": 10})


if __name__ == "__main__":
    unittest.main()
