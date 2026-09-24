"""Independent synthetic regression oracles; no live host, DB, model or sending.

Namespace loading tests pure implementation, not Hermes plugin registration.
The SQLite oracle executes the compatible SELECT subset with parameter syntax
adapted; it does not certify MySQL dialect or company business truth.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "datasage_independent_review"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins/datasage-query")]
    sys.modules[PACKAGE] = package

def load(name):
    return importlib.import_module(PACKAGE + "." + name)

wire = load("wire")
xlsx = load("legacy_xlsx")
transport = load("wecom_app_transport")
workflow = load("workflow_io")

class StrictWireTests(unittest.TestCase):
    def result(self, payload):
        text = wire.enforce_tool_result_budget("datasage_entity_resolve", payload)
        text.encode("utf-8")
        return json.loads(text, parse_constant=lambda token: self.fail(token))

    def test_normal_values_and_literal_words_unchanged(self):
        payload = {"status": "success", "value": 1.25, "count": 2**64,
                   "text": "中文 NaN Infinity", "none": None, "flag": True,
                   "items": [{"value": -0.5}]}
        self.assertEqual(payload, self.result(payload))
        self.assertEqual(payload, self.result(json.dumps(payload)))

    def test_invalid_numeric_values_do_not_become_success_null_or_zero(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=str(value)):
                result = self.result({"status": "success", "facts": [{"value": value}]})
                self.assertEqual("failed", result["status"])
                self.assertEqual("INVALID_TOOL_RESULT", result["error"]["code"])
        for text in ('{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}',
                     '{"value":1e400}', '{"value":-1e400}'):
            with self.subTest(text=text):
                self.assertEqual("failed", self.result(text)["status"])

    def test_repeated_keys_at_any_depth_rejected(self):
        for text in ('{"status":"failed","status":"success"}',
                     '{"nested":{"value":1,"value":2}}'):
            with self.subTest(text=text):
                self.assertEqual("failed", self.result(text)["status"])

    def test_malformed_or_unserializable_payload_becomes_typed_failure(self):
        cycle = {}; cycle["self"] = cycle
        for value in ([], '[]', '{bad', {"bad": object()}, cycle, {"s": "\ud800"}):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual("failed", self.result(value)["status"])

class WorkbookAndAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.directory = self.home / "report_runs" / "legacy_execution"
        self.directory.mkdir(parents=True)

    def make_book(self, filename="普通.xlsx", sheets=None):
        path = self.directory / filename
        xlsx.gen_workbook_xlsx(sheets or [("正常", ["名称", "金额"], [["客户甲", Decimal("100.15")]])], path)
        return path

    def snapshot(self, path):
        return transport.file_snapshot(path, self.home)

    def rewrite(self, path, changes):
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries.update(changes)
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                if data is not None:
                    archive.writestr(name, data)

    def test_xml_disallowed_characters_replace_not_corrupt_workbook(self):
        value = '金额\x00\x01\ud800\udfff\ufffe\uffff正常\t\n🙂<&'
        path = self.make_book(sheets=[("表\x01\ufffe\t标题", ["测试"], [[value]])])
        roots = {}
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    roots[name] = ET.fromstring(archive.read(name))
        texts = ''.join(roots['xl/worksheets/sheet1.xml'].itertext())
        self.assertIn('金额' + '\ufffd'*6 + '正常\t\n🙂<&', texts)
        title = next(roots['xl/workbook.xml'].iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet')).get('name')
        self.assertEqual('表\ufffd\ufffd 标题', title)
        self.snapshot(path)

    def test_sheet_name_collisions_after_cleaning_stay_unique(self):
        path = self.make_book(sheets=[('A\x01', ['x'], [[1]]), ('A\ufffd', ['x'], [[2]]), ('a\ufffd', ['x'], [[3]])])
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read('xl/workbook.xml'))
        titles = [element.get('name') for element in root.iter() if element.tag.endswith('}sheet')]
        self.assertEqual(len(titles), len({name.casefold() for name in titles}))
        self.snapshot(path)

    def test_formula_like_text_and_exact_decimals_are_not_reinterpreted(self):
        text = '=HYPERLINK("https://example.invalid","text")'
        value = Decimal('123456789012345.67')
        path = self.make_book(sheets=[('数据', ['文本','金额'], [[text, value]])])
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            cells = list(root.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'))
        self.assertFalse(any(e.tag.endswith('}f') for e in root.iter()))
        self.assertTrue(any(text in ''.join(c.itertext()) and c.get('t') == 'inlineStr' for c in cells))
        self.assertTrue(any(str(value) in ''.join(c.itertext()) and c.get('t') == 'inlineStr' for c in cells))
        self.snapshot(path)

    def test_valid_png_decodes_and_digest_matches_existing_algorithm(self):
        from PIL import Image
        path = self.directory / '正常.png'
        Image.new('RGB', (5, 7)).save(path)
        name, raw, mime, digest = self.snapshot(path)
        self.assertEqual('image/png', mime)
        self.assertEqual(hashlib.sha256(name.encode() + raw).hexdigest(), digest)

    def test_png_header_only_truncated_and_disguised_image_rejected(self):
        from PIL import Image
        path = self.directory / '错误.png'
        buffer = io.BytesIO(); Image.new('RGB', (10, 10)).save(buffer, format='PNG')
        jpeg = io.BytesIO(); Image.new('RGB', (10, 10)).save(jpeg, format='JPEG')
        for data in (b'\x89PNG\r\n\x1a\n', buffer.getvalue()[:45], jpeg.getvalue()):
            with self.subTest(length=len(data)):
                path.write_bytes(data)
                with self.assertRaisesRegex(workflow.IOErrorBoundary, 'PNG_INVALID'):
                    self.snapshot(path)

    def test_normal_zip_directories_chinese_names_and_digest_survive_regeneration(self):
        path = self.directory / '客户包.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('客户甲/', '')
            archive.writestr('客户甲/说明.txt', '测试内容')
        first = self.snapshot(path)[3]
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(zipfile.ZipInfo('客户甲/', (2020,1,1,0,0,0)), '')
            archive.writestr(zipfile.ZipInfo('客户甲/说明.txt', (2020,1,1,0,0,0)), '测试内容')
        self.assertEqual(first, self.snapshot(path)[3])

    def test_unsafe_archive_names_and_symlinks_rejected_without_extraction(self):
        path = self.directory / '不安全.zip'
        # The backslash spelling is deliberately absent here: on Windows the stdlib rewrites
        # it to "/" while writing the archive, so the entry would arrive safe and the
        # assertion could never hold on this platform.  The rule itself is covered by
        # test_archive_member_rule_rejects_backslash_and_control_names below.
        for name in ('../outside.txt', '/absolute.txt', 'x/../b.txt', 'C:/x.txt', 'name. ', 'a//b'):
            with self.subTest(name=name):
                with zipfile.ZipFile(path, 'w') as archive: archive.writestr(name, 'test')
                with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'): self.snapshot(path)
        with zipfile.ZipFile(path, 'w') as archive:
            entry = zipfile.ZipInfo('link'); entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, '../outside')
        with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'): self.snapshot(path)
        self.assertFalse((self.home / 'outside.txt').exists())

    def test_archive_member_rule_rejects_backslash_and_control_names(self):
        """Exercise the member-name rule directly, where the stdlib cannot help us.

        ``zipfile`` rewrites a backslash to "/" on Windows while writing, so an end-to-end
        case cannot carry that spelling here; the rule is therefore checked against the
        entries themselves, with a safe nested name as a positive control.
        """

        class Entry:
            def __init__(self, name):
                self.filename = name
                self.external_attr = 0

        for hostile in ('a\\b.txt', 'a\x08b.txt', '../outside.txt', '/absolute.txt', 'a//b', 'name. '):
            with self.subTest(name=hostile):
                with self.assertRaises(ValueError):
                    transport._archive_member_names([Entry(hostile)])
        transport._archive_member_names([Entry('客户甲/说明.txt'), Entry('xl/workbook.xml')])

    def test_case_colliding_names_rejected(self):
        path = self.directory / 'collision.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('Case.txt', 'one'); archive.writestr('case.txt', 'two')
        with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'): self.snapshot(path)

    def test_crc_corruption_is_not_accepted(self):
        path = self.directory / 'crc.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('safe.txt', b'unique-data-98765')
        path.write_bytes(path.read_bytes().replace(b'unique-data-98765', b'broken-data-98765'))
        with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'): self.snapshot(path)

    def test_named_but_invalid_ooxml_parts_rejected(self):
        path = self.directory / '假.xlsx'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('[Content_Types].xml', 'not xml')
            archive.writestr('xl/workbook.xml', 'not xml')
        with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'): self.snapshot(path)

    def test_missing_sheet_and_external_sheet_relationship_rejected(self):
        for kind in ('missing', 'external'):
            with self.subTest(kind=kind):
                path = self.make_book()
                if kind == 'missing': self.rewrite(path, {'xl/worksheets/sheet1.xml': None})
                else:
                    with zipfile.ZipFile(path) as archive:
                        rel = archive.read('xl/_rels/workbook.xml.rels')
                    self.rewrite(path, {'xl/_rels/workbook.xml.rels': rel.replace(b'Target="worksheets/sheet1.xml"', b'Target="https://example.invalid/sheet.xml" TargetMode="External"')})
                with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'): self.snapshot(path)

    def test_missing_package_relationship_or_styles_rejected(self):
        for member in ('_rels/.rels', 'xl/styles.xml'):
            with self.subTest(member=member):
                path = self.make_book()
                self.rewrite(path, {member: None})
                with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'):
                    self.snapshot(path)

    def test_xml_dtd_rejected_but_normal_workbook_accepted(self):
        path = self.make_book(); self.snapshot(path)
        with zipfile.ZipFile(path) as archive: content = archive.read('xl/workbook.xml')
        content = content.replace(b'?>', b'?><!DOCTYPE workbook [<!ENTITY test "value">]>', 1)
        self.rewrite(path, {'xl/workbook.xml': content})
        with self.assertRaisesRegex(workflow.IOErrorBoundary, 'ARCHIVE_INVALID'): self.snapshot(path)

    def test_openpyxl_normal_workbook_accepted_when_available(self):
        try: import openpyxl
        except ImportError: self.skipTest('optional independent OOXML implementation unavailable')
        path = self.directory / 'independent.xlsx'
        book = openpyxl.Workbook(); book.active['A1'] = '正常'; book.active['B1'] = 123.45
        book.save(path); self.snapshot(path)

    def test_invalid_attachment_blocks_before_any_http_client(self):
        path = self.directory / '错误.png'; path.write_bytes(b'\x89PNG\r\n\x1a\n')
        target = {'platform':'wecom_app_http','app_name':'business','corp_id':'test-corp',
                  'agent_id':'1','target_kind':'user','target_id':'synthetic-user'}
        binding = {'send_enabled': True, 'target_map': {'test': target}}
        client = Mock(side_effect=AssertionError('HTTP must not start'))
        config = [{'name':'business','corp_id':'test-corp','agent_id':'1','corp_secret':'SYNTHETIC'}]
        with patch.object(workflow, 'require_action', return_value=binding):
            delivery = transport.AppTransport('synthetic', self.home, app_loader=lambda: config, client_factory=client)
            progress = workflow.Progress(self.home, 'synthetic', 'period')
            parts = [workflow.component('test','text','合成测试','scope','text'),
                     workflow.component('test','file',path,'scope','file')]
            with self.assertRaisesRegex(workflow.IOErrorBoundary, 'PNG_INVALID'):
                workflow.deliver_components(parts, delivery, progress, enabled=True)
        client.assert_not_called()

class CleanCopyMaintenanceTests(unittest.TestCase):
    def test_every_owned_reference_is_explicitly_trackable(self):
        text = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        for reference in (ROOT / 'skills/business-analytics/datasage/references').glob('*.md'):
            with self.subTest(reference=reference.name):
                self.assertIn('!' + reference.relative_to(ROOT).as_posix(), text)

    def test_offgit_check_uses_recursive_category_inventory(self):
        import maintenance_cost
        with patch.object(maintenance_cost, 'repository_available', return_value=False), \
             patch.object(maintenance_cost, '_tracked', return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(0, maintenance_cost.check())

    def test_offgit_check_still_rejects_really_missing_reference(self):
        import maintenance_cost
        with patch.object(maintenance_cost, 'repository_available', return_value=False), \
             patch.object(maintenance_cost, '_tracked', return_value=None):
            measured = maintenance_cost.measure()
        measured['surface']['skill_files_datasage']['names'] = ['SKILL.md']
        with patch.object(maintenance_cost, 'repository_available', return_value=False), \
             patch.object(maintenance_cost, 'measure', return_value=measured), redirect_stdout(io.StringIO()):
            self.assertEqual(1, maintenance_cost.check())

class IndependentQueryOracleTests(unittest.TestCase):
    def test_actual_gross_query_filters_dates_and_preserves_unknown(self):
        tools = load('tools')
        request = tools._validate_request({'request_id':'synthetic-oracle', 'domain':'delivery',
            'metric':'gross_delivery_amount','dimensions':[],
            'time_range': {'start':'2026-07-01','end':'2026-08-01'}})
        dataset, semantic = tools._contracts('delivery')
        request = tools._validate_metric_contract(request, semantic)
        statement, parameters, _ = tools._build_metric_query(request, dataset, semantic, 100, observed_on=date(2026,9,1))
        connection = sqlite3.connect(':memory:'); self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        connection.execute('CREATE TABLE vk_dwd.sale_bill_goods_detail_dwd (delivery_amount_rmb REAL, bill_status INTEGER, is_inner_cus TEXT, delivery_time TEXT)')
        connection.executemany('INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES (?,?,?,?)', [
            (100.25,6,'n','2026-07-01'), (49.75,6,'n','2026-07-31'),
            (900,6,'n','2026-08-01'), (800,6,'n','2026-06-30'),
            (700,5,'n','2026-07-15'), (600,6,'y','2026-07-15')])
        # Only change parameter placeholders; execute the actual generated SQL.
        sql = statement.replace('%s', '?')
        result = dict(connection.execute(sql, parameters).fetchone())
        self.assertEqual(150, result['metric_value']); self.assertEqual(2, result['__matched_row_count'])
        self.assertEqual('complete', result['metric_data_state'])
        connection.execute("INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES (NULL,6,'n','2026-07-20')")
        result = dict(connection.execute(sql, parameters).fetchone())
        self.assertIsNone(result['metric_value']); self.assertEqual(1, result['missing_value_count'])
        self.assertEqual('incomplete', result['metric_data_state'])

    def test_net_flow_oracle_keeps_return_period_sign_and_unknown(self):
        tools = load('tools')
        request = tools._validate_request({'request_id':'synthetic-net', 'domain':'delivery',
            'metric':'delivery_amount','dimensions':[],
            'time_range': {'start':'2026-07-01','end':'2026-08-01'}})
        dataset, semantic = tools._contracts('delivery')
        request = tools._validate_metric_contract(request, semantic)
        statement, parameters, _ = tools._build_metric_query(request, dataset, semantic, 100, observed_on=date(2026,9,1))
        connection = sqlite3.connect(':memory:'); self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        connection.execute('CREATE TABLE vk_dwd.sale_bill_goods_detail_dwd (delivery_amount_rmb REAL, bill_status INTEGER, is_inner_cus TEXT, delivery_time TEXT)')
        connection.execute('CREATE TABLE vk_dwd.delivery_return_detail_dwd (return_amount_rmb REAL, status INTEGER, complnt_type INTEGER, channel_type INTEGER, is_inner_cus TEXT, statement_time TEXT)')
        connection.executemany('INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES (?,?,?,?)', [
            (100,6,'n','2026-07-01'), (50,6,'n','2026-07-31'),
            (900,6,'n','2026-08-01')])
        connection.executemany('INSERT INTO vk_dwd.delivery_return_detail_dwd VALUES (?,?,?,?,?,?)', [
            (80,4,1,1,'n','2026-07-15'), (999,4,1,1,'n','2026-08-01'),
            (888,3,1,1,'n','2026-07-15'), (777,4,1,1,'y','2026-07-15')])
        sql = statement.replace('%s', '?')
        result = dict(connection.execute(sql, parameters).fetchone())
        self.assertEqual(70, result['metric_value'])  # independent 100+50-80
        self.assertEqual('complete', result['metric_data_state'])
        # Test the oracle itself: a changed sign or inclusive end cannot pass.
        wrong_sign = sql.replace('ELSE -(COALESCE', 'ELSE (COALESCE')
        self.assertNotEqual(sql, wrong_sign)
        self.assertNotEqual(70, connection.execute(wrong_sign, parameters).fetchone()['metric_value'])
        wrong_end = sql.replace(' < ?', ' <= ?')
        self.assertNotEqual(sql, wrong_end)
        self.assertNotEqual(70, connection.execute(wrong_end, parameters).fetchone()['metric_value'])
        connection.execute("INSERT INTO vk_dwd.delivery_return_detail_dwd VALUES (100,4,1,1,'n','2026-07-20')")
        self.assertEqual(-30, connection.execute(sql, parameters).fetchone()['metric_value'])
        connection.execute("INSERT INTO vk_dwd.delivery_return_detail_dwd VALUES (NULL,4,1,1,'n','2026-07-20')")
        result = dict(connection.execute(sql, parameters).fetchone())
        self.assertIsNone(result['metric_value'])
        self.assertEqual('incomplete', result['metric_data_state'])

    def test_receipt_basis_difference_does_not_require_different_times(self):
        tools = load('tools')
        connection = sqlite3.connect(':memory:'); self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        connection.execute('CREATE TABLE vk_dwd.receive_bill_detail_dwd (detail_receive_rmb REAL, detail_deal_amount REAL, exchange_rate REAL, bill_status TEXT, bill_time TEXT)')
        connection.executemany('INSERT INTO vk_dwd.receive_bill_detail_dwd VALUES (?,?,?,?,?)', [
            (100,50,2,'B','2026-07-15'), (70,50,1.5,'B','2026-07-15'),
            (900,900,1,'A','2026-07-15'), (800,800,1,'B','2026-08-01')])
        dataset, semantic = tools._contracts('receipt')
        self.assertEqual(semantic['metrics']['receipt_amount']['time_field'],
                         semantic['metrics']['actual_receipt_amount']['time_field'])
        for metric, expected in (('receipt_amount',170), ('actual_receipt_amount',175)):
            with self.subTest(metric=metric):
                request = tools._validate_request({'request_id':'synthetic-receipt','domain':'receipt',
                    'metric':metric,'dimensions':[],
                    'time_range':{'start':'2026-07-01','end':'2026-08-01'}})
                request = tools._validate_metric_contract(request, semantic)
                sql, parameters, _ = tools._build_metric_query(request, dataset, semantic, 100, observed_on=date(2026,9,1))
                result = dict(connection.execute(sql.replace('%s','?'), parameters).fetchone())
                self.assertEqual(expected, result['metric_value'])
                self.assertEqual('complete', result['metric_data_state'])

if __name__ == '__main__':
    unittest.main()
