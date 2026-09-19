"""Offline replay of the pinned legacy Products and report presentation."""

from __future__ import annotations

from decimal import Decimal
import importlib
import json
from pathlib import Path
import unittest

import test_business_contracts as base


wf = importlib.import_module(base.TEST_PACKAGE + ".legacy_workflow")
templates = importlib.import_module(base.TEST_PACKAGE + ".legacy_message_templates")
FIXTURE = Path(__file__).parent / "fixtures" / "legacy_report_golden.json"


def _plain(value):
    """Normalize Decimal values for comparison with JSON golden numbers."""

    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if key not in {"hi_qty_rows"}}
    return value


class LegacyReportCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.inputs = cls.fixture["inputs"]
        cls.old = cls.fixture["old"]

    def test_fixture_records_pinned_old_source_without_loading_old_code(self):
        self.assertEqual("3fd38fcb803307e1688688ca1dfbde271131157a", self.fixture["source"]["commit"])
        self.assertEqual("datasage-core/send_slow_report.py", self.fixture["source"]["path"])
        self.assertRegex(self.fixture["source"]["sha256"], r"^[0-9a-f]{64}$")

    def test_products_rows_match_old_ast_golden_for_complete_values(self):
        _message, sheet = wf.task_draft("HCM", "2026-W38", "", "", self.inputs["items"])
        actual = sheet[2]
        self.assertEqual(self.old["task_headers"], wf.TASK_HEADERS)
        self.assertEqual(self.old["products_rows"], _plain(actual))

    def test_products_message_keeps_old_layout_and_unknown_rolls(self):
        text, _sheet = wf.task_draft(
            self.inputs["region"], self.inputs["week"], self.inputs["start"], self.inputs["end"], self.inputs["items"]
        )
        self.assertEqual(
            "Slow sales-stock Products\nDept: HCM\nPeriod: 2026/09/14 ~ 2026/09/19\n"
            "Total SKUs: 2\nTotal Rolls: 3.5",
            text,
        )
        missing = [dict(self.inputs["items"][0], total_piece=None)]
        missing_text, _sheet = wf.task_draft("HCM", "2026-W38", "a", "b", missing)
        self.assertIn("Total Rolls: Unknown", missing_text)

    def test_products_promotion_blank_and_missing_display_fields_are_explicit(self):
        blank = dict(self.inputs["items"][0], promotion_price=" ", promotion_currency_no=" ", promotion_unit="\t")
        _message, sheet = wf.task_draft("HCM", "2026-W38", "", "", [blank])
        self.assertEqual("Not Set", sheet[2][0][8])
        priced = dict(blank, promotion_price="1234.50")
        _message, sheet = wf.task_draft("HCM", "2026-W38", "", "", [priced])
        self.assertEqual("1,234.5 [Currency Missing] / [Unit Missing]", sheet[2][0][8])
        self.assertEqual("Sheet1", sheet[0])

    def test_report_header_matches_old_ast_golden(self):
        self.assertEqual(self.old["report_headers"], wf.REPORT_HEADERS)

    def test_weekly_and_monthly_text_match_old_ast_golden_for_complete_values(self):
        weekly = templates.report(
            self.inputs["region"], self.inputs["week"], self.fixture["canonical"]["summary"],
            self.fixture["canonical"]["sales_rows"], monthly=False,
        )
        monthly = templates.report(
            self.inputs["region"], self.inputs["month"], self.fixture["canonical"]["summary"],
            self.fixture["canonical"]["sales_rows"], monthly=True,
        )
        self.assertEqual(self.old["weekly"], weekly)
        self.assertEqual(self.old["monthly"], monthly)

    def test_fractional_negative_high_discount_is_not_old_zero_rounding(self):
        summary = {
            "opening_skus": 1, "closing_skus": 1, "opening_rolls": 1, "closing_rolls": 1,
            "new": 0, "exited": 0, "net_outbound_rolls": 0,
            "high_net_rolls": Decimal("-0.25"),
        }
        row = [{"sales_id": "s", "sales_name": "A", "net_piece": 1,
                "hi_disc_piece": Decimal("-0.25"), "return_piece": 0}]
        text = templates.report("HCM", "2026-W38", summary, [{"sales_name": "A", "net_rolls": 1}], monthly=False)
        self.assertIn("High-Discount: **-0.25 rolls**", text)
        # The pinned old function uses int(round(sum(hi_disc))), which would
        # display zero for this exact synthetic case.  That fallback is not
        # imported or restored here because the governed metric preserves sign
        # and source precision.
        self.assertNotIn("High-Discount: **0 rolls**", text)
        ht={**summary,'net_outbound_qty_by_unit':{'kg':0},'high_net_qty_by_unit':{'kg':Decimal('-0.005')}}
        ht_text=templates.report('HCM-HT','2026-W38',ht,[])
        self.assertIn('High-Discount: **-0.005 kg**',ht_text)

    def test_missing_numbers_and_names_are_not_coerced_to_zero_or_internal_id(self):
        item = dict(self.inputs["items"][0])
        item.update(baseline_piece=None, current_slow_piece=None, net_outbound_piece=None)
        summary = {"opening_skus": 1, "closing_skus": 1, "opening_rolls": None,
                   "closing_rolls": None, "new": 0, "exited": 0,
                   "net_outbound_rolls": None, "high_net_rolls": None}
        sales = [{"sales_name": None, "net_rolls": 1}]
        text = templates.report("HCM", "2026-W38", summary, sales, monthly=False)
        self.assertIn("**Unknown**", text)
        self.assertNotIn("internal-42", text)

    def test_ht_quantities_remain_unit_scoped_and_preserve_unknown_high_discount(self):
        summary = {
            "opening_skus": 1, "closing_skus": 1, "opening_rolls": 2, "closing_rolls": 2,
            "new": 0, "exited": 0, "net_outbound_rolls": 0,
            "net_outbound_qty_by_unit": {"kg": Decimal("2"), "m": Decimal("10")},
            "high_net_qty_by_unit": {"kg": None, "m": Decimal("1.5")},
        }
        row = [{"sales_id": "s", "sales_name": "A", "net_piece": 0,
                "hi_disc_piece": 0, "return_piece": 0,
                "units": [{"unit": "m", "net_qty": 10, "hi_disc_qty": None, "return_qty": 0},
                          {"unit": "kg", "net_qty": 2, "hi_disc_qty": 1, "return_qty": 1}]}]
        text = templates.report("HCM-HT", "2026-W38", summary, [{"sales_name": "A", "net_rolls": 0, "net_quantity_by_unit": {"m": 10, "kg": 2}}], monthly=False)
        self.assertIn("Total: **2 kg; 10 M**", text)
        self.assertIn("High-Discount: **Unknown kg; 1.5 M**", text)
        self.assertNotIn("0 kg", text)

    def test_workbook_helpers_expose_logical_sheet_and_cells(self):
        from tempfile import TemporaryDirectory
        from zipfile import ZipFile

        with TemporaryDirectory() as tmp:
            products = Path(tmp) / "Products.xlsx"
            _message, sheet = wf.task_draft("HCM", "2026-W38", "", "", self.inputs["items"])
            wf.gen_workbook_xlsx([sheet], products)
            detail = Path(tmp) / "Detail.xlsx"
            wf.gen_workbook_xlsx([("Detail", wf.REPORT_HEADERS, self.old["detail_rows"])], detail, borders=True, landscape=True)
            with ZipFile(products) as archive:
                workbook = archive.read("xl/workbook.xml").decode("utf-8")
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn('name="Sheet1"', workbook)
                self.assertIn("SYN-1001", sheet)
                self.assertIn("12.5 CNY / m", sheet)
            with ZipFile(detail) as archive:
                workbook = archive.read("xl/workbook.xml").decode("utf-8")
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn('name="Detail"', workbook)
                self.assertIn("Net Outbound Rolls", sheet)
                self.assertIn("Alice: 2 rolls", sheet)

    def test_legacy_layout_rejects_undefined_percent_style_xfs(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'LEGACY_LAYOUT_PERCENT_FORMAT_UNSUPPORTED'):
                wf.gen_workbook_xlsx([('Percent',['Value'],[[0.5],],{0:'percent'})],Path(tmp)/'percent.xlsx',legacy_layout=True)

    def test_current_detail_sold_by_uses_product_sales_rolls_not_scope_sales_rolls(self):
        contracts=importlib.import_module(base.TEST_PACKAGE+'.contracts');capability=importlib.import_module(base.TEST_PACKAGE+'.capability_contract');inputs=importlib.import_module(base.TEST_PACKAGE+'.workflow_inputs')
        _,sem=contracts.execution_contracts('inventory');metric='registered_slow_pool_baseline_net_outbound';labels={code:value['label'] for code,value in capability.effective_dimension_definitions(sem,metric).items()}
        def dims(sales):return [{'label':labels['pool_sku'],'value':'101'},{'label':labels['warehouse_department'],'value':'HCM'},{'label':labels['unit'],'value':'m'},{'label':labels['salesperson'],'value':sales}]
        def flow(sales,rolls,quantity):return {'dimensions':dims(sales),'facts':{'sales_identity_ref':'id-'+sales,'sales_net_rolls':5,'net_rolls':rolls,'metric_value':quantity,'unit_net_quantity':50,'scope_net_rolls':5,'scope_high_net_rolls':0}}
        pool={'results':[{'rows':[{'dimensions':[{'label':labels['product'],'value':'P'},{'label':labels['pool_sku'],'value':'101'},{'label':labels['warehouse_department'],'value':'HCM'},{'label':labels['unit'],'value':'m'}],'facts':{'opening_rolls':2,'closing_rolls':2,'opening_quantity':10,'closing_quantity':10},'states':{'pool_movement_state':'No Change'}}]}]}
        packet=inputs.legacy_report_packet(pool,{'results':[{'rows':[flow('A',2,20),flow('B',3,30)]}]},[], 'HCM','2026-W38')
        sold=packet['detail_rows'][0][-1]
        self.assertIn('A: 2 rolls',sold);self.assertIn('B: 3 rolls',sold);self.assertNotIn('A: 5 rolls',sold);self.assertNotIn('B: 5 rolls',sold)


if __name__ == "__main__":
    unittest.main()
