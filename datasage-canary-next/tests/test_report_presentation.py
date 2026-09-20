"""Offline presentation regressions for the existing fabric report renderer."""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_report_presentation_tests"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_package = types.ModuleType(PACKAGE)
_package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, _package)
legacy_xlsx = importlib.import_module(f"{PACKAGE}.legacy_xlsx")
fabric_report = importlib.import_module(f"{PACKAGE}.fabric_report")


def _sheet_cells(path: Path, sheet_name: str) -> dict[str, dict[str, str | None]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.get("Id"): "xl/" + rel.get("Target").lstrip("/")
            for rel in rels
            if rel.get("Type", "").endswith("/worksheet")
        }
        target = None
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            if sheet.get("name") == sheet_name:
                target = targets[sheet.get(f"{{{REL_NS}}}id")]
                break
        if target is None:
            raise AssertionError(f"sheet not found: {sheet_name}")
        root = ET.fromstring(archive.read(target))
        result = {}
        for cell in root.findall("s:sheetData/s:row/s:c", NS):
            value = cell.find("s:is/s:t", NS)
            if value is None:
                value = cell.find("s:v", NS)
            result[cell.get("r")] = {
                "type": cell.get("t"),
                "style": cell.get("s"),
                "value": value.text if value is not None else None,
            }
        return result


def _fixture() -> dict:
    return {
        "status": "saved_synthetic_observation",
        "observations": [
            {
                "name": "出库总览",
                "status": "success",
                "data_state": "rows",
                "truncated": False,
                "row_count": 1,
                "population_group_count": 1,
                "population_row_count": 1,
                "observed_at": "2026-09-19T09:00:00+08:00",
                "rows": [
                    {
                        "dimensions": [{"name": "单位", "value": "卷"}],
                        "facts": {
                            "fabric_rolls": Decimal("12.5"),
                            "fabric_ddp_rmb": Decimal("123456789012345.67"),
                            "fabric_tag_rate": Decimal("0"),
                            "fabric_tag_contribution": Decimal("1"),
                        },
                    }
                ],
            },
            {
                "name": "出库渠道",
                "status": "success",
                "data_state": "rows",
                "truncated": True,
                "row_count": 1,
                "population_group_count": 2,
                "population_row_count": 2,
                "observed_at": "2026-09-19T09:00:00+08:00",
                "rows": [
                    {
                        "dimensions": [
                            {"name": "单位", "value": "卷"},
                            {
                                "name": "渠道",
                                "value": "中文渠道A｜商品ID 12345678901234567890｜超长分组名称用于溢出检查",
                            },
                        ],
                        "facts": {
                            "fabric_known_tagged_rolls": Decimal("0"),
                            "fabric_tag_rate": Decimal("0"),
                            "fabric_tag_contribution": Decimal("0.123456"),
                            "fabric_ddp_rmb": Decimal("123456789012345.67"),
                        },
                    }
                ],
            },
        ],
    }


class ReportPresentationTests(unittest.TestCase):
    def test_rate_chart_keeps_percent_units_for_negative_values(self):
        from PIL import ImageDraw

        rendered_text = []
        original = ImageDraw.ImageDraw.text

        def record(draw, position, text, *args, **kwargs):
            rendered_text.append(str(text))
            return original(draw, position, text, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ImageDraw.ImageDraw, "text", record):
            fabric_report.chart(
                Path(tmp) / "rates.png", "合成发生率",
                [{"group": "合成渠道", "facts": {
                    "fabric_tag_rate": Decimal("-0.5"),
                    "fabric_tag_contribution": Decimal("0.125"),
                }}],
                [("fabric_tag_rate", "发生率"), ("fabric_tag_contribution", "贡献率")],
            )
        self.assertIn("负值需核对：-50.0%", rendered_text)
        self.assertIn("12.5%", rendered_text)

    def test_fabric_extreme_amount_is_exact_text_and_disclosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fabric_report.export_report(_fixture(), Path(tmp))
            cells = _sheet_cells(Path(paths[0]), "出库总览")
            self.assertEqual("inlineStr", cells["C2"]["type"])
            self.assertEqual("9", cells["C2"]["style"])
            self.assertEqual("123456789012345.67", cells["C2"]["value"])
            overview = _sheet_cells(Path(paths[0]), "管理层总览")
            self.assertTrue(any(
                overview.get(f"B{row}", {}).get("value") == "Excel数值精度"
                and "不参与Excel数值计算" in overview.get(f"C{row}", {}).get("value", "")
                for row in range(1, 80)
            ))
            with zipfile.ZipFile(paths[0]) as archive:
                styles = archive.read("xl/styles.xml").decode("utf-8")
            self.assertIn('formatCode="#,##0.00"', styles)

    def test_numeric_boundary_preserves_ordinary_amount_and_percent_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "precision.xlsx"
            legacy_xlsx.gen_workbook_xlsx(
                [("Sheet", ["Extreme amount", "Ordinary amount", "Ordinary ratio", "Extreme ratio"], [[
                    Decimal("123456789012345.67"), Decimal("100.15"), Decimal("0.123456"), Decimal("0.12345678901234567")
                ]], {0: "amount", 1: "amount", 2: "percent", 3: "percent"})],
                path,
                borders=True,
            )
            cells = _sheet_cells(path, "Sheet")
            self.assertEqual("inlineStr", cells["A2"]["type"])
            self.assertEqual("123456789012345.67", cells["A2"]["value"])
            self.assertIsNone(cells["B2"]["type"])
            self.assertEqual("100.15", cells["B2"]["value"])
            self.assertIsNone(cells["C2"]["type"])
            self.assertEqual("0.123456", cells["C2"]["value"])
            self.assertEqual("5", cells["C2"]["style"])
            self.assertEqual("inlineStr", cells["D2"]["type"])
            self.assertEqual("0.12345678901234567", cells["D2"]["value"])
            self.assertEqual("9", cells["D2"]["style"])
            precision = _sheet_cells(path, "数值精度说明")
            self.assertTrue(any(
                "比例原值未乘100；0.1表示10%" in cell.get("value", "")
                for cell in precision.values()
            ))

    def test_exact_text_width_uses_expanded_value_and_wraps_long_exponents(self):
        value = Decimal("1.2345678901234567E-100")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "long-precision.xlsx"
            legacy_xlsx.gen_workbook_xlsx(
                [("Sheet", ["Value"], [[value]], {0: "amount"})],
                path,
                borders=True,
            )
            cells = _sheet_cells(path, "Sheet")
            self.assertEqual("inlineStr", cells["A2"]["type"])
            self.assertEqual(legacy_xlsx._exact_text(value), cells["A2"]["value"])
            with zipfile.ZipFile(path) as archive:
                sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                styles = archive.read("xl/styles.xml").decode("utf-8")
            width = float(sheet.find("s:cols/s:col", NS).attrib["width"])
            row = sheet.find("s:sheetData/s:row[@r='2']", NS)
            self.assertEqual(42, width)
            self.assertGreater(float(row.attrib["ht"]), 15)
            self.assertIn('numFmtId="166"', styles)
            self.assertIn('numFmtId="166" fontId="0" fillId="0" borderId="1"', styles)
            self.assertIn('alignment wrapText="1" vertical="center"', styles)

    def test_legacy_layout_keeps_existing_numeric_style_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.xlsx"
            legacy_xlsx.gen_workbook_xlsx(
                [("Sheet", ["Amount"], [[Decimal("123.45")]])],
                path,
                borders=True,
                legacy_layout=True,
            )
            cells = _sheet_cells(path, "Sheet")
            self.assertEqual("2", cells["A2"]["style"])
            with zipfile.ZipFile(path) as archive:
                styles = archive.read("xl/styles.xml").decode("utf-8")
            self.assertNotIn('formatCode="#,##0.00"', styles)

    def test_long_chart_labels_wrap_without_loss_and_gain_row_height(self):
        label = "卷 / 中文渠道A｜商品ID 12345678901234567890｜超长分组名称用于溢出检查"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chart.png"
            fabric_report.chart(
                path,
                "合成渠道",
                [
                    {
                        "group": label,
                        "facts": {
                            "fabric_tag_rate": Decimal("0.123456"),
                            "fabric_tag_contribution": Decimal("0"),
                        },
                    }
                ],
                [
                    ("fabric_tag_rate", "渠道自身发生率"),
                    ("fabric_tag_contribution", "标签问题贡献率"),
                ],
            )
            from PIL import Image

            with Image.open(path) as image:
                self.assertGreater(image.height, 180)
            font_path = Path("C:/Windows/Fonts/msyh.ttc")
            from PIL import ImageFont

            font = ImageFont.truetype(str(font_path), 15)
            lines = fabric_report._wrap_chart_label(label, font, 190)
            self.assertGreater(len(lines), 1)
            self.assertEqual(label, "".join(lines))
            self.assertTrue(all(font.getlength(line) <= 190 for line in lines))
            identifier = "12345678901234567890"
            self.assertEqual(1, sum(identifier in line for line in lines))


if __name__ == "__main__":
    unittest.main()
