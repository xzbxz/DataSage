"""Regression checks for lossless source XML and supported workbook styles."""
from decimal import Decimal, localcontext
import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import unicodedata
from xml.etree import ElementTree as ET
import zipfile

import test_business_contracts as base

xlsx = importlib.import_module(base.TEST_PACKAGE + '.legacy_xlsx')
workflow = importlib.import_module(base.TEST_PACKAGE + '.legacy_workflow')
templates = importlib.import_module(base.TEST_PACKAGE + '.legacy_message_templates')
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


class RepairWorkbookPrecisionTests(unittest.TestCase):
    def test_legacy_unit_display_preserves_nonzero_small_quantities(self):
        self.assertEqual('0.001 pcs', templates.quantity({'Pcs': Decimal('0.001')}))
        self.assertEqual('Unknown Unknown', templates.quantity({None: None}))

    def test_notes_are_wide_enough_for_their_visible_text(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'synthetic.xlsx'
            xlsx.gen_workbook_xlsx([workflow.REPORT_NOTES_SHEET], output, legacy_layout=True)
            with zipfile.ZipFile(output) as archive:
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            width = float(sheet.find('s:cols/s:col', NS).attrib['width'])
            for row in workflow.REPORT_NOTES_SHEET[2]:
                visible_width = sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in row[0])
                self.assertGreaterEqual(width, visible_width)

    def test_legacy_format_does_not_round_decimal_to_context_precision(self):
        value = Decimal('12345678901234567890.123456789')
        with TemporaryDirectory() as tmp, localcontext() as context:
            context.prec = 12
            output = Path(tmp) / 'synthetic.xlsx'
            xlsx.gen_workbook_xlsx([('Sheet', ['Quantity'], [[value]])], output, legacy_layout=True)
            with zipfile.ZipFile(output) as archive:
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                workbook = ET.fromstring(archive.read('xl/workbook.xml'))
                sheet_names = [node.attrib['name'] for node in workbook.findall('s:sheets/s:sheet', NS)]
                precision_sheet = archive.read('xl/worksheets/sheet2.xml').decode('utf-8')
            cell = sheet.find(".//s:c[@r='A2']", NS)
            rendered = cell.find('s:is/s:t', NS).text
            self.assertEqual('inlineStr', cell.attrib['t'])
            self.assertEqual(value, Decimal(rendered))
            self.assertIn('数值精度说明', sheet_names)
            self.assertIn('不参与Excel数值计算', precision_sheet)

    def test_precision_sheet_name_conflict_gets_unique_suffix(self):
        value = Decimal('12345678901234567890')
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'synthetic.xlsx'
            xlsx.gen_workbook_xlsx(
                [('数值精度说明', ['Value'], [[value]])],
                output,
            )
            with zipfile.ZipFile(output) as archive:
                workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            sheet_names = [node.attrib['name'] for node in workbook.findall('s:sheets/s:sheet', NS)]
            self.assertEqual(['数值精度说明', '数值精度说明_2'], sheet_names)

    def test_tiny_nonzero_decimal_is_not_published_as_numeric_zero(self):
        values = [Decimal('1E-400'), Decimal('-1E-400'), Decimal('1E-308'), Decimal('1E-307'), Decimal('0')]
        for legacy in (False, True):
            with TemporaryDirectory() as tmp:
                output = Path(tmp) / 'tiny.xlsx'
                xlsx.gen_workbook_xlsx([('Values', ['Value'], [[value] for value in values])], output, legacy_layout=legacy)
                with zipfile.ZipFile(output) as archive:
                    sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                for index, value in enumerate(values, 2):
                    cell = sheet.find(f".//s:c[@r='A{index}']", NS)
                    if index < 5:
                        self.assertEqual('inlineStr', cell.attrib.get('t'))
                        self.assertEqual(value, Decimal(cell.find('s:is/s:t', NS).text))
                    else:
                        self.assertIsNone(cell.attrib.get('t'))
                        self.assertEqual(value, Decimal(cell.find('s:v', NS).text))

    def test_business_text_cannot_suppress_precision_disclosure(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'labels.xlsx'
            xlsx.gen_workbook_xlsx([('Business', ['Label', 'Value'], [
                ['Excel数值精度', Decimal('123456789012345.67')]
            ])], output)
            with zipfile.ZipFile(output) as archive:
                workbook = ET.fromstring(archive.read('xl/workbook.xml'))
                names = [node.attrib['name'] for node in workbook.findall('s:sheets/s:sheet', NS)]
            self.assertIn('数值精度说明', names)

    def test_legacy_percent_is_rejected_before_artifact_is_published(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'synthetic.xlsx'
            with self.assertRaisesRegex(ValueError, 'LEGACY_LAYOUT_PERCENT_FORMAT_UNSUPPORTED'):
                xlsx.gen_workbook_xlsx([('Sheet', ['Rate'], [[Decimal('0.125')]], {0: 'percent'})], output, legacy_layout=True)
            self.assertFalse(output.exists())

    def test_default_percent_references_a_defined_number_format(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'synthetic.xlsx'
            xlsx.gen_workbook_xlsx([('Sheet', ['Rate'], [[Decimal('0.125')]], {0: 'percent'})], output)
            with zipfile.ZipFile(output) as archive:
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                styles = ET.fromstring(archive.read('xl/styles.xml'))
            cell = sheet.find(".//s:c[@r='A2']", NS)
            xf = list(styles.find('s:cellXfs', NS))[int(cell.attrib['s'])]
            ids = {item.attrib['numFmtId'] for item in styles.find('s:numFmts', NS)}
            self.assertIn(xf.attrib['numFmtId'], ids)
            self.assertEqual(Decimal('0.125'), Decimal(cell.find('s:v', NS).text))
