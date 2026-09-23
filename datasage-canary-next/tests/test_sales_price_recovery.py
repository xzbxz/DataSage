"""Sales-price recovery kernel tests; isolated filesystem only."""

from pathlib import Path
import subprocess
import sys
import copy
import json
from datetime import timedelta
import shutil
from tempfile import TemporaryDirectory
import textwrap
import unittest
from contextlib import contextmanager, nullcontext
from hashlib import sha256
from unittest.mock import patch

import test_business_contracts as base


io = __import__(base.TEST_PACKAGE + ".workflow_io", fromlist=["*"])
sales_runner = __import__(base.TEST_PACKAGE + ".sales_price_runner", fromlist=["*"])
sales_reference = __import__(base.TEST_PACKAGE + ".sales_reference", fromlist=["*"])
operations = __import__(base.TEST_PACKAGE + ".operations", fromlist=["*"])
wf = __import__(base.TEST_PACKAGE + ".legacy_workflow", fromlist=["*"])
local_report = __import__(base.TEST_PACKAGE + ".local_report", fromlist=["*"])
contract_store = __import__(base.TEST_PACKAGE + ".contract_store", fromlist=["*"])


_LOCK_CHILD = r'''
import importlib, sys, types
from pathlib import Path
name = "sales_lock_child"
pkg = types.ModuleType(name)
pkg.__path__ = [sys.argv[2]]
sys.modules[name] = pkg
io = importlib.import_module(name + ".workflow_io")
with io.sales_run_lock(Path(sys.argv[1])):
    print("READY", flush=True)
    sys.stdin.readline()
'''


class SyntheticDB:
    marker = "synthetic-sales-snapshot"

    def __init__(self, price, *, buyers=True, employee_region="HCM", at="2026-09-19 10:00:00"):
        self.buyers = buyers
        self.employee_region = employee_region
        self.at = at
        self.rows = [
            {
                "goods_id": 1, "goods_no": "SYN-1", "dept": "HCM", "goods_name": "Synthetic",
                "customer_grade": "A", "color_label": "Red", "ddp_price": price, "currency_no": "CNY",
                "unit": "m", "unit_cuur": "m", "is_inclue_tax": "n",
                "effective_date": "2026-01-01T00:00:00", "expiration_date": "2026-12-31T00:00:00",
                "gmt_modified": at, "detail_id": 1, "observed_at": at,
            }
        ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_at(self, value):
        self.at = value
        for row in self.rows:
            row["observed_at"] = value
            row["gmt_modified"] = value

    def execute(self, sql, params=(), limit=10000, **kwargs):
        del params, limit, kwargs
        if "SELECT NOW(6)" in sql:
            return [{"at": self.at}], False, {}
        if "FROM vk_dwd.employee_dwd" in sql:
            return [{"region": self.employee_region, "main_dept": "HCM Sales", "is_delete": "n", "wecom_status": "payroll", "wecom_account": "acct-sales", "person_name": "Sales", "position": "Sales"}], False, {}
        if "FROM vk_dwd.delivery_bill_barcode_detail_dwd" in sql:
            return ([{"customer_id": "C1", "goods_no": "SYN-1", "whse_dept": "HCM"}] if self.buyers else []), False, {}
        if "FROM vk_dwd.customer_dwd" in sql:
            return [{"customer_id": "C1", "customer_no": "C-1", "customer_name": "Customer", "sales_name": "Sales"}], False, {}
        return copy.deepcopy(self.rows), False, {}


class _NoReadSnapshot:
    def __enter__(self):
        raise AssertionError("source read must not occur during pending recovery")

    def __exit__(self, *args):
        return False


class SalesTransport:
    def __init__(self, fail_kind=None, outcome="success"):
        self.fail_kind = fail_kind
        self.outcome = outcome
        self.sends = []
        self.kinds = []

    def preflight(self, components):
        self.preflight_components = list(components)

    def delivery_lock(self, _progress):
        return nullcontext()

    def fingerprint(self, item):
        raw = Path(item["path"]).read_bytes() if item["kind"] == "file" else str(item.get("text", "")).encode()
        return sha256(raw).hexdigest()

    def send(self, item):
        self.sends.append(item["stage"]); self.kinds.append(item["kind"])
        if self.outcome == "unknown":
            raise TimeoutError("synthetic price timeout")
        if self.fail_kind == item["kind"]:
            return {"success": False, "raw_response": {"errcode": 1}}
        return {"success": True, "message_id": "synthetic"}


class _NoReadSnapshot:
    def __enter__(self):
        raise AssertionError("source snapshot must not be opened")

    def __exit__(self, *args):
        return False


class SalesPriceRecoveryTests(unittest.TestCase):
    def test_unassigned_customer_stays_manager_only_with_explicit_display_label(self):
        import zipfile
        from xml.etree import ElementTree as ET
        class UnassignedDB(SyntheticDB):
            customer={'customer_id':'C1','customer_no':'C-1','customer_name':'Customer','sales_name':None}
            def execute(self,sql,params=(),limit=10000,**kwargs):
                if 'FROM vk_dwd.customer_dwd' in sql:return [dict(self.customer)],False,{}
                return super().execute(sql,params,limit,**kwargs)
        with TemporaryDirectory() as temp:
            root=Path(temp);out=root/'out';out.mkdir();self._initialize(root,'100.00')
            db=UnassignedDB('95.00');transport=SalesTransport()
            with patch.object(sales_runner,'_now_local',return_value=sales_runner._parse_time('2026-09-19 10:10:00')):
                result=self._run(root,out,db,transport,roles=self.roles(manager=True),target_map=self.targets(manager=True))
            self.assertEqual('success',result['status'])
            self.assertNotIn('sales_file',transport.sends)
            self.assertEqual(1,transport.sends.count('sales_text'));self.assertEqual(1,transport.sends.count('manager_file'))
            store=sales_reference.SalesReferenceStore(root);proof=store._proof_load(store.load()['last_committed']['batch_id'])
            pending=proof['pending'];files=[c for c in pending['components'] if c['kind']=='file']
            self.assertEqual(['acct-manager'],[c['account'] for c in files])
            ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            with zipfile.ZipFile(files[0]['path']) as archive:
                sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            rows=[[''.join(cell.find('s:is',ns).itertext()) for cell in row] for row in sheet.findall('s:sheetData/s:row',ns)]
            self.assertEqual([['Sales','Customer No','Customer'],['Unassigned','C-1','Customer']],rows)
            self.assertIsNone(db.customer['sales_name'])
            self.assertIsNone(pending['observation']['prepared']['managers']['HCM']['buyers']['SYN-1'][0][0])

    def test_only_configured_active_manager_executor_receives_dynamic_summary(self):
        class ExecutorDB(SyntheticDB):
            extra_lookup=False
            def execute(self,sql,params=(),limit=10000,**kwargs):
                if 'FROM vk_dwd.employee_dwd' in sql and 'wecom_account IN' in sql:
                    self.extra_lookup=True
                    return [
                        {'wecom_account':'dynamic-manager','position':'Sales Supervisor','is_delete':'n'},
                        {'wecom_account':'other-executor','position':'Designer','is_delete':'n'},
                        {'wecom_account':'deleted-manager','position':'Sales Manager','is_delete':'y'},
                    ],False,{}
                return super().execute(sql,params,limit,**kwargs)
        class Captured(SalesTransport):
            def __init__(self):super().__init__();self.accounts=[]
            def send(self,item):self.accounts.append(item['account']);return super().send(item)
        with TemporaryDirectory() as temp:
            root=Path(temp);out=root/'out';out.mkdir();self._initialize(root,'100.00')
            roles=self.roles();roles['regions']['HCM']['executors'] += [{'account':a} for a in ('dynamic-manager','other-executor','deleted-manager')]
            targets={a:{'platform':'synthetic','target_ref':a} for a in ('acct-sales','dynamic-manager','other-executor','deleted-manager')}
            db=ExecutorDB('95.00');transport=Captured()
            with patch.object(sales_runner,'_now_local',return_value=sales_runner._parse_time('2026-09-19 10:10:00')):
                result=self._run(root,out,db,transport,roles=roles,target_map=targets)
            self.assertEqual('success',result['status']);self.assertTrue(db.extra_lookup)
            self.assertEqual({'acct-sales','dynamic-manager'},set(transport.accounts))
            self.assertEqual(1,transport.sends.count('manager_text'));self.assertEqual(1,transport.sends.count('manager_file'))

    def test_sales_file_lock_is_mutually_exclusive_and_released_on_child_exit(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", textwrap.dedent(_LOCK_CHILD), str(profile), str(base.PLUGIN_ROOT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual("READY", child.stdout.readline().strip())
                with self.assertRaisesRegex(io.IOErrorBoundary, "BUSY|UNAVAILABLE"):
                    with io.sales_run_lock(profile):
                        pass
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
                if child.stdin is not None:
                    child.stdin.close()
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()
            # The OS-backed kernel lock must be released when this owned child
            # exits; no stale process lock is accepted as a reusable state file.
            with io.sales_run_lock(profile):
                pass

    def test_stale_legacy_lock_marker_is_not_silently_overwritten(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            root = io.private_root(profile)
            marker = root / "sales_price.lock"
            marker.write_text("synthetic stale marker", encoding="utf-8")
            try:
                with self.assertRaisesRegex(io.IOErrorBoundary, "BUSY|STALE"):
                    with io.sales_run_lock(profile):
                        pass
            finally:
                marker.unlink(missing_ok=True)

    def test_local_sales_reference_100_to_95_then_95_to_90_commits_each_hour(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            shutil.copytree(base.PLUGIN_ROOT / "contracts", root / "plugins" / "datasage-query" / "contracts")
            db = SyntheticDB("95.00"); transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", side_effect=[sales_runner._parse_time("2026-09-19 10:10:00"), sales_runner._parse_time("2026-09-19 11:10:00")]), patch.object(io, "read_recipients", return_value=self.roles()):
                first = self._run(root, out, db, transport)
                db.rows[0]["ddp_price"] = "90.00"
                db.set_at("2026-09-19 11:10:00")
                second = self._run(root, out, db, transport, progress=io.Progress(root, "sales_price", "price-events"))
            self.assertEqual("success", first["status"]); self.assertEqual("success", second["status"])
            self.assertGreaterEqual(len(transport.sends), 2)
            self.assertEqual("90.00", sales_reference.SalesReferenceStore(root).load()["reference"]["rows"][0]["ddp_price"])

    def test_failed_file_keeps_95_pending_and_recovery_does_not_read_90(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00"); failed = SalesTransport(fail_kind="file")
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                with self.assertRaisesRegex(Exception, "FAILED|INCOMPLETE"):
                    self._run(root, out, db, failed)
            db.rows[0]["ddp_price"] = "90.00"; recovered = SalesTransport()
            with patch.object(sales_runner, "_source_rows", side_effect=AssertionError("pending recovery must not read source")), patch.object(io, "read_recipients", side_effect=AssertionError("pending recovery must not read roles")):
                result = self._run(root, out, None, recovered, progress=io.Progress(root, "sales_price", "price-events"))
            self.assertTrue(result.get("head_advanced")); self.assertEqual(1, recovered.kinds.count("file")); self.assertEqual(0, recovered.kinds.count("text"))
            self.assertEqual("95.00", sales_reference.SalesReferenceStore(root).load()["reference"]["rows"][0]["ddp_price"])

    def test_no_change_new_key_and_reentry_are_silent(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "95.00")
            db = SyntheticDB("95.00"); transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 11:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                same = self._run(root, out, db, transport)
            self.assertEqual("silent_no_deliverable_change", same["delivery"]); self.assertEqual([], transport.sends)
            db.rows.append({**db.rows[0], "goods_id": 2, "goods_no": "SYN-2", "detail_id": 2, "ddp_price": "10.00"})
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 12:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                new = self._run(root, out, db, transport, progress=io.Progress(root, "sales_price", "price-events"))
            self.assertIn(new["delivery"], ("silent_no_deliverable_change", "not_requested")); self.assertEqual([], transport.sends)

    def test_anomalous_basis_keeps_reference_and_does_not_send(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "95.00")
            db = SyntheticDB("90.00"); db.rows[0]["currency_no"] = None; transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 11:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                result = self._run(root, out, db, transport)
            self.assertEqual([], transport.sends); self.assertEqual("95.00", sales_reference.SalesReferenceStore(root).load()["reference"]["rows"][0]["ddp_price"])

    def test_unknown_pending_blocks_recovery_and_target_or_attachment_tamper_blocks(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00"); unknown = SalesTransport(outcome="unknown")
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                with self.assertRaisesRegex(Exception, "UNKNOWN"):
                    self._run(root, out, db, unknown)
            with self.assertRaisesRegex(Exception, "UNKNOWN|PENDING"):
                self._run(root, out, None, SalesTransport(), progress=io.Progress(root, "sales_price", "price-events"))
            self.assertGreaterEqual(len(unknown.sends), 1)

    def test_prepare_failure_sends_zero_and_legacy_database_remains_read_only(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00"); transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()), patch.object(sales_runner, "_prepare_components", side_effect=sales_runner.SalesPriceRunnerError("SYNTHETIC_PREPARE_FAILURE")):
                with self.assertRaisesRegex(Exception, "PREPARE"):
                    self._run(root, out, db, transport)
            self.assertEqual([], transport.sends); self.assertIsNone(sales_reference.SalesReferenceStore(root).load()["pending"])

    def test_sales_with_no_buyers_sends_text_without_attachment_and_no_manager(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00", buyers=False); transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")):
                self._run(root, out, db, transport, roles=self.roles(manager=True), target_map=self.targets(manager=True))
            self.assertIn("text", transport.kinds); self.assertNotIn("file", transport.kinds)
            self.assertNotIn("manager_text", transport.sends); self.assertNotIn("manager_file", transport.sends)

    def test_manager_notice_and_attachment_exist_only_when_region_has_buyers(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00", buyers=True); transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")):
                self._run(root, out, db, transport, roles=self.roles(manager=True), target_map=self.targets(manager=True))
            self.assertIn("manager_text", transport.sends); self.assertIn("manager_file", transport.sends)

    def test_invalid_sales_qualification_does_not_expand_to_wrong_region(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00", buyers=True, employee_region="HN"); transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")):
                with self.assertRaisesRegex(Exception, "RECIPIENT|QUALIF|TARGET|PLAN|COMPONENT|EMPTY|PRICE_REQUIRED"):
                    self._run(root, out, db, transport)
            self.assertEqual([], transport.sends)

    def test_commit_before_write_failure_recovers_without_duplicate_send(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00"); first = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()), patch.object(sales_reference.SalesReferenceStore, "commit", side_effect=sales_reference.SalesReferenceError("SYNTHETIC_COMMIT_BEFORE_WRITE")):
                with self.assertRaisesRegex(Exception, "COMMIT|WRITE"):
                    self._run(root, out, db, first)
            before = len(first.sends)
            recovered = SalesTransport()
            with patch.object(sales_runner, "_source_rows", side_effect=AssertionError("recovery must not read source")), patch.object(io, "read_recipients", side_effect=AssertionError("recovery must not read roles")):
                result = self._run(root, out, None, recovered, progress=io.Progress(root, "sales_price", "price-events"))
            self.assertTrue(result.get("head_advanced")); self.assertEqual([], recovered.sends); self.assertGreater(before, 0)

    def test_after_commit_manifest_failure_same_hour_restarts_without_read_or_send(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            db = SyntheticDB("95.00"); first = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()), patch.object(sales_runner, "_write_manifest", side_effect=RuntimeError("SYNTHETIC_MANIFEST_ACK_LOST")):
                with self.assertRaisesRegex(RuntimeError, "MANIFEST"):
                    self._run(root, out, db, first)
            second = SalesTransport()
            observed_at = sales_reference.SalesReferenceStore(root).load()["last_committed"]["observed_at"]
            same_hour = sales_runner._parse_time(observed_at) + timedelta(minutes=5)
            with patch.object(sales_runner, "_now_local", return_value=same_hour), patch.object(sales_runner, "_source_rows", side_effect=AssertionError("same-hour recovery must not read source")), patch.object(io, "read_recipients", side_effect=AssertionError("same-hour recovery must not read roles")):
                result = self._run(root, out, None, second, progress=io.Progress(root, "sales_price", "price-events"))
            self.assertEqual("same_hour_already_committed", result["execution_state"]); self.assertEqual([], second.sends)

    def test_pending_attachment_target_and_body_seal_tamper_are_blocked(self):
        for mode in ("attachment", "target", "body"):
            with self.subTest(mode=mode), TemporaryDirectory() as temp:
                root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
                db = SyntheticDB("95.00"); failed = SalesTransport(fail_kind="file")
                with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                    with self.assertRaises(Exception): self._run(root, out, db, failed)
                head_path = root / "report_runs" / "legacy_execution" / "sales_reference" / "head.json"
                head = json.loads(head_path.read_text(encoding="utf-8")); pending = head["pending"]
                if mode == "attachment":
                    path = Path(next(iter(pending["attachments"])))
                    path.write_bytes(b"tampered-attachment")
                    expected = "ATTACHMENT|PENDING"
                    target_map = self.targets()
                elif mode == "target":
                    expected = "TARGET|BINDING"
                    target_map = {"acct-sales": {"platform": "synthetic", "target_ref": "changed"}}
                else:
                    pending["components"][0]["text"] = "tampered body"
                    head_path.write_text(json.dumps(head), encoding="utf-8")
                    expected = "HEAD|SEAL|INVALID"
                    target_map = self.targets()
                with self.assertRaisesRegex(Exception, expected):
                    self._run(root, out, None, SalesTransport(), progress=io.Progress(root, "sales_price", "price-events"), roles=self.roles(), target_map=target_map)

    def test_accepted_missing_fingerprint_or_manifest_cannot_advance(self):
        for mode in ("fingerprint", "manifest"):
            with self.subTest(mode=mode), TemporaryDirectory() as temp:
                root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
                db = SyntheticDB("95.00"); failed = SalesTransport(fail_kind="file")
                with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                    with self.assertRaises(Exception): self._run(root, out, db, failed)
                progress_path = root / "report_runs" / "legacy_execution" / (wf.digest(["sales_price", "price-events"]) + ".json")
                data = json.loads(progress_path.read_text(encoding="utf-8"))
                if mode == "fingerprint":
                    accepted = next(key for key, value in data["components"].items() if value.get("status") == "provider_accepted")
                    data["components"][accepted].pop("fingerprint", None); expected = "LEGACY_RECEIPT|BINDING|FINGERPRINT"
                else:
                    key = next(iter(data.get("notification_manifests", {}))); data["notification_manifests"][key] = "tampered"; expected = "CONTENT_CHANGED|MANIFEST"
                progress_path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(Exception, expected):
                    self._run(root, out, None, SalesTransport(), progress=io.Progress(root, "sales_price", "price-events"))

    def test_no_head_formal_send_fails_before_source_query(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); transport = SalesTransport()
            with patch.object(io, "read_recipients", side_effect=AssertionError("must not read roles")):
                with self.assertRaisesRegex(Exception, "HEAD|REFERENCE|INITIAL"):
                    self._run(root, out, None, transport, progress=io.Progress(root, "sales_price", "price-events"))
            self.assertEqual([], transport.sends)

    def test_reentry_after_absence_is_silent_on_first_return(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            extra = {"id": 2, "goods_id": 2, "goods_no": "SYN-2", "dept": "HCM", "customer_grade": "A", "color_label": "Blue", "ddp_price": "80.00", "currency_no": "CNY", "matched_detail_id": 2, "snapshot_at": "2026-09-19 09:00:00"}
            self._initialize(root, "95.00", [extra])
            db = SyntheticDB("95.00"); db.rows.append({**db.rows[0], **{key: value for key, value in extra.items() if key not in ("id", "snapshot_at")}, "observed_at": "2026-09-19 10:00:00"}); transport = SalesTransport()
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                db.rows = [db.rows[1]]; gone = self._run(root, out, db, transport)
            self.assertEqual([], transport.sends); self.assertEqual("silent_no_deliverable_change", gone["delivery"])
            db.rows = [SyntheticDB("95.00").rows[0], db.rows[0]]
            db.set_at("2026-09-19 11:10:00")
            with patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 11:10:00")), patch.object(io, "read_recipients", return_value=self.roles()):
                returned = self._run(root, out, db, transport, progress=io.Progress(root, "sales_price", "price-events"))
            self.assertEqual([], transport.sends); self.assertEqual("silent_no_deliverable_change", returned["delivery"])

    def test_legacy_database_execute_is_read_only_and_does_not_create_local_head(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); called = []
            doc = {"status": "success", "kind": "sales_prices", "baseline_source": "legacy_database", "reference": {"rows": 0}, "events": [], "scope_notice": "synthetic"}
            bridge = __import__(base.TEST_PACKAGE + ".legacy_price_bridge", fromlist=["*"])
            with patch.object(bridge, "observe", side_effect=lambda binding: called.append(binding) or doc):
                result = operations.execute(root, "sales", {"kind": "sales_prices", "regions": ["HCM", "HN", "BKK", "IDK"], "limit": 10, "reference_source": "legacy_database"})
            self.assertEqual(doc, result); self.assertTrue(called); self.assertFalse((root / "report_runs" / "legacy_execution" / "sales_reference" / "head.json").exists())

    def test_run_bound_local_sales_entry_uses_existing_head_and_fake_snapshot(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir(); self._initialize(root, "100.00")
            shutil.copytree(base.PLUGIN_ROOT / "contracts", root / "plugins" / "datasage-query" / "contracts")
            contract_store.reset_contract_snapshot_for_tests()
            db = SyntheticDB("95.00"); transport = SalesTransport()
            binding = {"kind": "legacy_execution", "enabled": True, "read_enabled": True, "customer_mapping_enabled": True, "freeze_enabled": False, "send_enabled": True, "price_accept_enabled": True, "register_schedule_enabled": False, "schedule": None, "failure_deliver": False, "recipients_file": "local/workflow-roles.json", "target_map": self.targets(), "operation": {"kind": "sales_prices", "regions": ["HCM", "HN", "BKK", "IDK"], "limit": 10000, "reference_source": "profile_local"}}
            with patch.object(io, "load_activation", return_value=binding), patch.object(local_report, "configure_runtime"), patch.object(contract_store, "profile_root", return_value=root), patch.object(io, "read_recipients", return_value=self.roles()), patch.object(sales_runner, "_now_local", return_value=sales_runner._parse_time("2026-09-19 10:10:00")):
                result = io.run_bound(root, "sales_price", transport=transport, snapshot_factory=lambda: db)
            self.assertEqual("success", result["status"]); self.assertGreater(len(transport.sends), 0)
            contract_store.reset_contract_snapshot_for_tests()

    def _initialize(self, root, price, extra_rows=None):
        store = sales_reference.SalesReferenceStore(root)
        rows = [{"id": 1, "goods_id": 1, "goods_no": "SYN-1", "dept": "HCM", "customer_grade": "A", "color_label": "Red", "ddp_price": price, "currency_no": "CNY", "matched_detail_id": 1, "snapshot_at": "2026-09-19 09:00:00"}]
        rows.extend(list(extra_rows or []))
        plan = store.initialization_plan(rows, {"source": "synthetic_review", "observed_at": "2026-09-19 09:00:00", "history_unknown": True})
        store.initialize({**plan, "approval_reason": "Synthetic sales review"})

    def roles(self, *, manager=False):
        return {"price_manager_fixed": ["acct-manager"] if manager else [], "regions": {"HCM": {"executors": [{"account": "acct-sales", "name": "Synthetic Sales"}], "managers": []}, "HN": {"executors": [], "managers": []}, "BKK": {"executors": [], "managers": []}, "IDK": {"executors": [], "managers": []}}}

    def targets(self, *, manager=False):
        value = {"acct-sales": {"platform": "synthetic", "target_ref": "sales"}}
        if manager:
            value["acct-manager"] = {"platform": "synthetic", "target_ref": "manager"}
        return value

    def _run(self, root, out, db, transport, progress=None, *, roles=None, target_map=None):
        snapshots = (lambda: _NoReadSnapshot()) if db is None else (lambda: db)
        binding = {"send_enabled": True, "price_accept_enabled": True, "recipients_file": "local/workflow-roles.json", "target_map": target_map or self.targets(), "operation": {"kind": "sales_prices", "regions": ["HCM", "HN", "BKK", "IDK"], "limit": 10000, "reference_source": "profile_local"}}
        with patch.object(io, "read_recipients", return_value=roles or self.roles()):
            return io._produce_and_execute(root, "sales_price", binding, out, "2026-W38", "2026-09", progress or io.Progress(root, "sales_price", "price-events"), snapshots, transport, None)


if __name__ == "__main__":
    unittest.main()
