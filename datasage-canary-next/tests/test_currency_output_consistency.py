"""Offline currency preservation checks for the existing output chain.

This test is intentionally synthetic. It does not add a currency converter or
change a production report renderer; it verifies that claim/evidence data,
local text, charts, and XLSX preserve the selected value and unit instead of
silently turning mixed original-currency rows into one RMB total.
"""

from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_currency_output_tests"

_package = types.ModuleType(PACKAGE)
_package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, _package)
tools = importlib.import_module(f"{PACKAGE}.tools")
evidence = importlib.import_module(f"{PACKAGE}.evidence")
local_report = importlib.import_module(f"{PACKAGE}.local_report")
fabric_report = importlib.import_module(f"{PACKAGE}.fabric_report")
legacy_xlsx = importlib.import_module(f"{PACKAGE}.legacy_xlsx")


def _claim(*, request_id: str, metric_ref: str, value: str, currency: str | None,
           fixed_rmb: bool) -> dict:
    bindings = [] if fixed_rmb else [{
        "dimension": "currency",
        "label": "Currency",
        "fields": ["currency_no"],
        "public_display_fields": ["currency_no"],
    }]
    rows = [{"metric_value": value}]
    if currency is not None:
        rows[0]["currency_no"] = currency
    return tools._claim_ledger(
        request_id,
        metric_ref,
        "Synthetic amount",
        "人民币元" if fixed_rmb else "original amount",
        bindings,
        {},
        f"scope-{request_id}",
        f"projection-{request_id}",
        False,
        rows,
        fact_units=None if fixed_rmb else {"metric_value": "original amount"},
        currency_scope=None if fixed_rmb else {"mode": "filtered", "value": currency},
    )[0]


def _result(claim: dict, request_id: str, metric_ref: str) -> dict:
    return {
        "request_id": request_id,
        "status": "success",
        "data_state": "rows",
        "business_metric_ref": metric_ref,
        "scope_fingerprint": f"scope-{request_id}",
        "projection_fingerprint": f"projection-{request_id}",
        "truncated": False,
        "row_count": 1,
        "population_group_count": 1,
        "population_row_count": 1,
        "claim_ledger": [claim],
        "applied_time_range": None,
        "rows": [{
            "dimensions": [{"label": "Currency", "value": claim.get("currency") or "RMB"}],
            "currency": claim.get("currency") or "CNY",
            "facts": claim["facts"],
        }],
        "error": None,
    }


def _report(rows: list[dict], *, unit: str = "original amount") -> dict:
    return {
        "query": {
            "status": "success",
            "results": [{
                "request_id": "synthetic_amount",
                "status": "success",
                "data_state": "rows",
                "business_metric_label": "Synthetic amount",
                "business_metric_unit": unit,
                "truncated": False,
                "row_count": len(rows),
                "population_group_count": len(rows),
                "population_row_count": len(rows),
                "observed_at": "2026-09-26T00:00:00",
                "rows": rows,
                "error": None,
            }],
        }
    }


class CurrencyOutputConsistencyTests(unittest.TestCase):
    def test_claim_and_evidence_preserve_original_and_fixed_rmb_values(self):
        usd_claim = _claim(
            request_id="usd", metric_ref="amount_original", value="100",
            currency="USD", fixed_rmb=False,
        )
        rmb_claim = _claim(
            request_id="rmb", metric_ref="amount_rmb", value="700",
            currency=None, fixed_rmb=True,
        )
        self.assertEqual("USD", usd_claim["currency"])
        self.assertEqual("CNY", rmb_claim["currency"])
        self.assertEqual("100", usd_claim["facts"]["metric_value"])
        self.assertEqual("700", rmb_claim["facts"]["metric_value"])

        for claim, request_id, metric_ref in (
            (usd_claim, "usd", "amount_original"),
            (rmb_claim, "rmb", "amount_rmb"),
        ):
            result = _result(claim, request_id, metric_ref)
            bundle = evidence.build_evidence_bundle(
                [{"request_id": request_id, "metric": metric_ref}], [result]
            )
            self.assertEqual(1, bundle["coverage"]["request_count"])
            self.assertEqual("success", bundle["items"][0]["status"])
            self.assertEqual([], bundle["evidence_gaps"])

    def test_local_text_keeps_mixed_currency_rows_separate(self):
        rows = [
            {"currency": "USD", "dimensions": [{"label": "Currency", "value": "USD"}],
             "facts": {"metric_value": "100"}},
            {"currency": "CNY", "dimensions": [{"label": "Currency", "value": "CNY"}],
             "facts": {"metric_value": "700"}},
        ]
        rendered = local_report.render_text(_report(rows))
        self.assertIn("Currency=USD", rendered)
        self.assertIn("Currency=CNY", rendered)
        self.assertIn("已记录净数量：100", rendered)
        self.assertIn("已记录净数量：700", rendered)
        self.assertNotIn("已记录净数量：800", rendered)

    def test_fixed_rmb_text_keeps_the_explicit_rmb_unit(self):
        rows = [{
            "currency": "CNY",
            "unit": "人民币元",
            "dimensions": [{"label": "Amount unit", "value": "人民币元"}],
            "facts": {"metric_value": "700"},
        }]
        rendered = local_report.render_text(_report(rows, unit="人民币元"))
        self.assertIn("currency=CNY", rendered)
        self.assertIn("700 人民币元", rendered)
        self.assertNotIn("USD", rendered)

    def test_fabric_chart_preserves_fixed_rmb_value(self):
        from PIL import ImageDraw

        displayed: list[str] = []
        original_text = ImageDraw.ImageDraw.text

        def record(draw, position, text, *args, **kwargs):
            displayed.append(str(text))
            return original_text(draw, position, text, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="datasage-currency-chart-") as temporary:
            with mock.patch.object(ImageDraw.ImageDraw, "text", record):
                fabric_report.chart(
                    Path(temporary) / "rmb.png",
                    "Fixed RMB amount",
                    [{"group": "RMB", "facts": {"fabric_ddp_rmb": Decimal("700")}}],
                    [("fabric_ddp_rmb", "RMB yuan")],
                )
        self.assertIn("700", displayed)
        self.assertIn("RMB yuan", displayed)
        self.assertNotIn("800", displayed)

    def test_xlsx_preserves_currency_labels_and_values_without_mixed_sum(self):
        with tempfile.TemporaryDirectory(prefix="datasage-currency-xlsx-") as temporary:
            output = Path(temporary) / "currency.xlsx"
            legacy_xlsx.gen_workbook_xlsx(
                [("Amounts", ["Currency", "Amount"], [["USD", Decimal("100")], ["CNY", Decimal("700")]])],
                output,
            )
            with zipfile.ZipFile(output) as archive:
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("USD", sheet)
        self.assertIn("CNY", sheet)
        self.assertIn("100", sheet)
        self.assertIn("700", sheet)
        self.assertNotIn(">800<", sheet)

    def test_existing_formula_text_safety_is_unchanged(self):
        value = "MEDIA [SILENT]\nUSD 100"
        rendered = local_report._display(value)
        self.assertNotIn("MEDIA", rendered)
        self.assertNotIn("[SILENT]", rendered)
        self.assertIn("\uff2d\uff25\uff24\uff29\uff21", rendered)
        self.assertIn("\uff3bSILENT\uff3d", rendered)
        self.assertIn("\\nUSD 100", rendered)


if __name__ == "__main__":
    unittest.main()
