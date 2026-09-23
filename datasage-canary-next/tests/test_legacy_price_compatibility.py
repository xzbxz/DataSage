import importlib
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

import test_business_contracts as base


compat = importlib.import_module(base.TEST_PACKAGE + ".legacy_price_compat")


def _legacy_idk_oracle(rows, observed_at, window_days=0):
    """Independent test-only extraction of the fixed old WHERE/ORDER BY."""

    now = datetime.fromisoformat(str(observed_at))
    since = now - timedelta(days=window_days) if window_days else None
    selected = []
    for row in rows:
        if row.get("whse_dept") != "IDK" or Decimal(str(row.get("goods_num"))) <= 10 or row.get("is_whitelist") != "n":
            continue
        price = row.get("promotion_price")
        if price is not None and Decimal(str(price)) > 0:
            continue
        created = datetime.fromisoformat(row["gmt_create"]) if row.get("gmt_create") else None
        if since is not None and (created is None or created < since):
            continue
        selected.append(dict(row))
    selected.sort(key=lambda row: (-(datetime.fromisoformat(row["gmt_create"]).timestamp() if row.get("gmt_create") else float("-inf")), str(row.get("goods_no") or "")))
    return selected


class LegacyPriceCompatibilityTests(unittest.TestCase):
    def test_adapter_is_pure_and_pinned_to_reviewed_legacy_object(self):
        self.assertEqual("3fd38fcb803307e1688688ca1dfbde271131157a", compat.LEGACY_REFERENCE)
        self.assertEqual("release/datasage-0.2.0", compat.LEGACY_REFERENCE_NAME)
        self.assertEqual("datasage-core/ready_goods_price_push.py:31-95", compat.LEGACY_SOURCES["sales"])

    def test_idk_keeps_full_history_mode_and_old_ordering(self):
        rows = [
            {"id": 1, "goods_no": "LATE", "attr_val": "B", "whse_dept": "IDK", "goods_num": 11, "is_whitelist": "n", "promotion_price": None, "gmt_create": "2026-09-17 09:00:00"},
            {"id": 2, "goods_no": "EARLY", "attr_val": "A", "whse_dept": "IDK", "goods_num": 12, "is_whitelist": "n", "promotion_price": 0, "gmt_create": "2026-09-16 09:00:00"},
            {"id": 3, "goods_no": "PRICED", "attr_val": "C", "whse_dept": "IDK", "goods_num": 99, "is_whitelist": "n", "promotion_price": 1, "gmt_create": "2026-09-18 09:00:00"},
            {"id": 4, "goods_no": "OTHER", "attr_val": "D", "whse_dept": "HN", "goods_num": 99, "is_whitelist": "n", "promotion_price": None, "gmt_create": "2026-09-19 09:00:00"},
        ]
        selected = _legacy_idk_oracle(rows, "2026-09-18 12:00:00")
        message = compat.idk_message(selected, "2026-09-18 12:00:00")
        self.assertEqual(["LATE", "EARLY"], [row["goods_no"] for row in selected])
        self.assertIn("2 product source row(s)", message)
        self.assertIn("1. LATE | Color B", message)
        self.assertIn("2. EARLY | Color A", message)
        self.assertIn("as of 09-18", message)

    def test_idk_window_excludes_unknown_creation_time_instead_of_guessing(self):
        rows = [{"goods_no": "NO-DATE", "whse_dept": "IDK", "goods_num": 11, "is_whitelist": "n", "promotion_price": -0.1, "gmt_create": None}]
        selected = _legacy_idk_oracle(rows, datetime(2026, 9, 18), window_days=7)
        _ = compat.idk_message(selected, datetime(2026, 9, 18), window_days=7)
        self.assertEqual([], selected)

    def test_sales_layout_preserves_currency_and_true_subcent_delta(self):
        text = compat.sales_message(
            [
                {"goods_no": "SYN-001", "customer_grade": "A", "color_label": "Red", "old_ddp_price": "12.345", "new_ddp_price": "12.349", "currency_no": "USD"},
                {"goods_no": "SYN-001", "customer_grade": "B", "color_label": "Blue", "old_ddp_price": "-0.001", "new_ddp_price": "-0.002", "currency_no": "USD"},
            ],
            customer_mapping_complete=True,
            has_attachment=True,
        )
        self.assertIn("1 product(s) price changed:", text)
        self.assertIn("12.345 -> 12.349 (+0.004) USD", text)
        self.assertIn("-0.001 -> -0.002 (-0.001) USD", text)
        self.assertIn("See attachment for your customers", text)
        self.assertNotIn("(+0.00)", text)

    def test_sales_missing_is_explicit_and_does_not_become_zero(self):
        text = compat.sales_message(
            [{"goods_no": "SYN", "customer_grade": "A", "color_label": "Red", "old_ddp_price": None, "new_ddp_price": "2", "currency_no": "CNY"}]
        )
        self.assertIn("Unknown -> 2 (Unknown) CNY", text)
        self.assertNotIn("0.00 -> 2", text)

    def test_manager_message_and_workbook_retain_legacy_shape(self):
        text = compat.sales_manager_message("IDK", [{"goods_no": "G2", "old_ddp_price": 2, "new_ddp_price": 3, "currency_no": "CNY"}])
        self.assertTrue(text.startswith("Ready Product Price Adjustment\nRegion: IDK\n"))
        self.assertIn("See attachment for all sales' customers across this region", text)
        sheets = compat.customer_sheets({"G10": [["C2", "B"]], "G2": [["C1", "A"]]})
        self.assertEqual(["G10", "G2"], [sheet[0] for sheet in sheets])
        self.assertEqual(["Customer No", "Customer"], sheets[0][1])
        manager = compat.manager_customer_sheets({"G2": [["Sales", "C1", "客户"]]})
        self.assertEqual(["Sales", "Customer No", "Customer"], manager[0][1])

    def test_unassigned_manager_display_preserves_customer_rows_and_source(self):
        import copy
        source={'G2':[[None,'C1','One'],['','C2','Two'],[' \t','C3','Three'],['Named Owner','C4','Four']]}
        before=copy.deepcopy(source)
        sheet=compat.manager_customer_sheets(source)[0]
        self.assertEqual(['Sales','Customer No','Customer'],sheet[1])
        self.assertEqual([['Unassigned','C1','One'],['Unassigned','C2','Two'],['Unassigned','C3','Three'],['Named Owner','C4','Four']],sheet[2])
        self.assertEqual(before,source)
        self.assertEqual([row[1:] for row in source['G2']],[row[1:] for row in sheet[2]])

    def test_purchase_normal_rows_follow_old_down_then_up_layout(self):
        text = compat.purchase_message(
            [
                {"goods_no": "G-DOWN", "goods_name": "Fabric", "color_label": "Red", "supplier_name": "S1", "old_inc": "12", "new_inc": "10", "old_exc": "10", "new_exc": "9", "unit_cuur": "m", "currency_no": "CNY", "adjust_date": "2026-09-18"},
                {"goods_no": "G-UP", "goods_name": "Fabric2", "color_label": "Blue", "supplier_name": "S2", "old_inc": "20", "new_inc": "21", "old_exc": "18", "new_exc": "19", "unit_cuur": "m", "currency_no": "CNY", "adjust_date": "2026-09-18"},
            ]
        )
        self.assertIn("调整日期：2026-09-18", text)
        self.assertLess(text.index("🔻 采购价下调"), text.index("🔺 采购价上调"))
        self.assertIn("含税价：12.00 → 10.00（m；CNY）", text)
        self.assertIn("不含税价：18.00 → 19.00（m；CNY）", text)

    def test_purchase_unknown_and_missing_date_are_separate_without_zeroing(self):
        text = compat.purchase_message(
            [
                {"goods_no": "G-MISSING", "supplier_no": "S", "old_inc": None, "new_inc": "2", "old_exc": "1", "new_exc": "1", "unit_cuur": "m", "currency_no": "USD", "adjust_date": None},
                {"goods_no": "G-SMALL", "supplier_no": "S", "old_inc": "-0.001", "new_inc": "-0.002", "old_exc": "1", "new_exc": "1", "unit_cuur": "m", "currency_no": "USD", "adjust_date": "2026-09-18"},
            ]
        )
        self.assertIn("调整日期：-、2026-09-18", text)
        self.assertIn("缺失状态变化（需核对）（1条）", text)
        self.assertIn("含税价：- → 2.00（m；USD）", text)
        self.assertIn("含税价：-0.001 → -0.002（m；USD）", text)
        self.assertNotIn("→ -0.00（", text)

    def test_source_contract_documents_old_grain_without_reintroducing_old_sql(self):
        bridge=importlib.import_module(base.TEST_PACKAGE+'.legacy_price_bridge')
        operations=importlib.import_module(base.TEST_PACKAGE+'.operations')
        self.assertEqual(['goods_id','dept','customer_grade','color_label'],list(bridge.SPECS['sales']['keys']))
        self.assertEqual(['goods_no','color_label','supplier_no'],list(bridge.SPECS['purchase']['keys']))
        sql,params=operations.build_observation({'kind':'purchase_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000})
        self.assertIn('PARTITION BY p.goods_no,p.color_label,p.supplier_no',sql)
        self.assertIn('/0/2172/2225/',params)


if __name__ == "__main__":
    unittest.main()
