import copy
import hashlib
import importlib
import json
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import test_business_contracts as base
from test_legacy_price_bridge import old_sales, sales


runner = importlib.import_module(base.TEST_PACKAGE + ".sales_price_runner")
wf = importlib.import_module(base.TEST_PACKAGE + ".legacy_workflow")
io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")

NOW = datetime(2026, 9, 18, 11, 30, 0)
REGIONS = ["HCM", "HN", "BKK", "IDK"]


def roles():
    result = {region: {"departments": [region], "task_sales_departments": [region + " Sales"], "dynamic_sales_departments": [region + " Sales"], "executors": [], "managers": []} for region in REGIONS}
    result["HCM"]["executors"] = [{"account": "seller", "name": "Seller"}]
    return {"regions": result, "price_manager_fixed": []}


class FakeDB:
    marker = "synthetic-price-snapshot"

    def __init__(self, current):
        self.current = current
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params, limit, **kwargs):
        self.calls.append(sql)
        upper = sql.upper()
        if "DELIVERY_BILL_BARCODE_DETAIL_DWD" in upper:
            return ([{"customer_id": "c1", "goods_no": "SYN-1", "whse_dept": "HCM"}], False, None)
        if "CUSTOMER_DWD" in upper:
            return ([{"customer_id": "c1", "customer_no": "C-1", "customer_name": "Customer 1", "sales_name": "Seller"}], False, None)
        if "FROM VK_DWD.EMPLOYEE_DWD" in upper and "PERSON_NAME" in upper:
            return ([{"person_name": "Seller", "wecom_account": "seller", "region": "HCM", "main_dept": "HCM Sales", "position": "Sales", "is_delete": "n", "wecom_status": "payroll"}], False, None)
        if "SELECT WECOM_ACCOUNT,POSITION,IS_DELETE" in upper:
            return ([{"wecom_account": "seller", "position": "Sales", "is_delete": "n"}], False, None)
        if "SELECT NOW(6) AS AT" in upper:
            return ([{"at": NOW.isoformat(sep=" ")}], False, None)
        if "SELECT NOW(6) AS OBSERVED_AT" in upper:
            return ([{"observed_at": NOW.isoformat(sep=" ")}], False, None)
        if "DYNAMIC" in upper:
            return ([], False, None)
        if "EMPLOYEE_DWD" in upper:
            return ([{"region": "HCM", "main_dept": "HCM Sales", "person_name": "Seller", "wecom_account": "seller", "position": "Sales", "is_delete": "n", "wecom_status": "payroll"}], False, None)
        if "WITH REGIONS" in upper or "SELECT GOODS_ID" in upper:
            return ([dict(self.current)], False, None)
        return ([], False, None)


class FakeProgress:
    def __init__(self):
        self.data = {"components": {}, "notification_manifests": {}}
        self.run_id = "synthetic-run"

    def status(self, key):
        return self.data["components"].get(key, {}).get("status", "not_attempted")


class FakeReferenceStore:
    def __init__(self, head):
        self.head = copy.deepcopy(head)
        self.prepare_calls = []
        self.commit_calls = []

    def load(self):
        return copy.deepcopy(self.head)

    def prepare(self, payload, before_digest):
        self.prepare_calls.append(copy.deepcopy(payload))
        pending_payload = {"batch_id": payload["batch_id"], "before_digest": before_digest, "observed_at": payload["observed_at"], "observation": payload["observation"], "components": payload["components"], "target_map": payload["target_map"], "attachments": payload["attachments"], "after_reference": payload["after_reference"]}
        pending = {**pending_payload, "content_seal": wf.digest(pending_payload)}
        self.head["pending"] = pending
        return {"status": "prepared", "head": self.load()}

    def commit(self, batch_id, receipt):
        self.commit_calls.append(copy.deepcopy(receipt))
        pending = self.head["pending"]
        self.head["reference"] = {"rows": pending["after_reference"], "digest": wf.digest(pending["after_reference"]), "provenance": {"source": "local_pending"}, "initialized_at": pending["observed_at"]}
        self.head["pending"] = None
        self.head["last_committed"] = {"batch_id": batch_id, "observed_at": pending["observed_at"], "committed_at": "2026-09-18 13:00:00", "receipt_evidence": receipt, "before_digest": pending["before_digest"], "after_digest": self.head["reference"]["digest"]}
        return {"status": "committed", "head": self.load()}


def head_with(rows, *, pending=None, last_committed=None):
    return {"schema": "datasage-sales-reference-head/v1", "mode": "local", "reference": {"rows": rows, "digest": wf.digest(rows), "provenance": {"source": "synthetic"}, "initialized_at": "2026-09-18 10:00:00"}, "pending": pending, "last_committed": last_committed, "history_unknown": False, "head_seal": "synthetic"}


@contextmanager
def snapshot_factory(db):
    yield db


class SalesPriceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.current = sales()
        self.current["ddp_price"] = "11.0001"
        self.db = FakeDB(self.current)
        self.profile = Path(tempfile.mkdtemp())
        self.out = self.profile / "report_runs" / "legacy_execution" / "sales-price-test"
        self.out.mkdir(parents=True)
        self.target_map = {"seller": {"platform": "wecom_app_http", "app_name": "synthetic", "corp_id": "1", "agent_id": "2", "target_kind": "user", "target_id": "seller"}}
        self.binding = {"operation": {"kind": "sales_prices", "regions": REGIONS, "limit": 10000, "reference_source": "profile_local"}, "send_enabled": False, "price_accept_enabled": False, "customer_mapping_enabled": True, "target_map": self.target_map, "recipients_file": "local/workflow-roles.json"}

    def _run(self, fake_store, binding=None, *, progress=None, now=None, transport=None):
        binding = binding or self.binding
        progress = progress or FakeProgress()
        with patch.object(runner.sales_reference, "SalesReferenceStore", return_value=fake_store), patch.object(runner.io, "read_recipients", return_value=roles()), patch.object(runner, "_now_local", return_value=now or datetime(2026, 9, 18, 12, 0, 0)):
            return runner.run(self.profile, binding, self.out, "2026-W38", "2026-09", progress, lambda: snapshot_factory(self.db), transport), progress

    def test_preview_prepares_without_advancing_head_and_filters_message_txt(self):
        fake = FakeReferenceStore(head_with([old_sales()]))
        result, _ = self._run(fake)
        self.assertEqual("success", result["status"])
        self.assertEqual("prepared_only", result["execution_state"])
        self.assertFalse(fake.prepare_calls)
        self.assertFalse(fake.commit_calls)
        self.assertGreaterEqual(result["components"], 1)
        self.assertTrue(list(self.out.rglob("*.xlsx")))
        self.assertTrue(list(self.out.rglob("*.txt")))

    def test_send_requires_a_trusted_head(self):
        fake = FakeReferenceStore(None)
        binding = {**self.binding, "send_enabled": True, "price_accept_enabled": True}
        with self.assertRaisesRegex(Exception, "PRICE_LOCAL_REFERENCE_REQUIRED_FOR_SEND"):
            self._run(fake, binding)

    def test_send_seals_and_commits_only_after_shared_delivery_acceptance(self):
        fake = FakeReferenceStore(head_with([old_sales()]))
        binding = {**self.binding, "send_enabled": True, "price_accept_enabled": True}
        progress = FakeProgress()

        def preflight(components, transport, progress_arg, **kwargs):
            return components

        def deliver(components, transport, progress_arg, **kwargs):
            groups = {}
            for item in components:
                fingerprint = "fp-" + item["key"]
                progress_arg.data["components"][item["key"]] = {"status": "provider_accepted", "fingerprint": fingerprint}
                groups.setdefault(item["notification_key"], []).append((item["key"], fingerprint, item["kind"]))
            for notification, values in groups.items():
                ordered = sorted(values, key=lambda value: value[2] != "text")
                progress_arg.data["notification_manifests"][notification] = wf.digest([[key, fingerprint] for key, fingerprint, _ in ordered])

        with patch.object(runner.io, "preflight_components", side_effect=preflight), patch.object(runner.io, "deliver_components", side_effect=deliver):
            result, _ = self._run(fake, binding, progress=progress, transport=object())
        self.assertEqual("success", result["status"])
        self.assertTrue(fake.commit_calls)
        receipt = fake.commit_calls[0]
        self.assertEqual("provider_accepted", receipt["status"])
        self.assertEqual(set(receipt["component_keys"]), set(receipt["accepted_fingerprints"]))

    def test_reference_store_initialization_is_explicit_and_dryrun_has_no_head(self):
        store = runner.sales_reference.SalesReferenceStore(self.profile)
        plan = store.initialization_plan([old_sales()], {"source": "synthetic", "observed_at": "2026-09-18 10:00:00", "history_unknown": True})
        dryrun = store.dryrun_initialize(plan)
        self.assertEqual("dryrun", dryrun["status"])
        self.assertIsNone(store.load())
        plan["approval_reason"] = "synthetic reviewed reference"
        head = store.initialize(plan)
        self.assertEqual(plan["rows_digest"], head["reference"]["digest"])
        self.assertIsNone(head["pending"])

    def test_same_hour_uses_observed_hour_not_commit_hour(self):
        fake = FakeReferenceStore(head_with([old_sales()], last_committed={"batch_id": "x", "observed_at": "2026-09-18 11:15:00", "committed_at": "2026-09-18 13:00:00"}))
        result, _ = self._run(fake, now=datetime(2026, 9, 18, 11, 50, 0))
        self.assertEqual("same_hour_already_committed", result["execution_state"])
        self.assertFalse(self.db.calls)

    def test_same_source_hour_after_new_host_hour_does_not_advance(self):
        fake = FakeReferenceStore(head_with([old_sales()], last_committed={"batch_id": "x", "observed_at": "2026-09-18 15:15:00", "committed_at": "2026-09-18 17:00:00"}))
        self.db.current["observed_at"] = "2026-09-18T15:30:00"
        result, _ = self._run(fake, now=datetime(2026, 9, 18, 16, 5, 0))
        self.assertEqual("same_source_hour_already_committed", result["execution_state"])
        self.assertFalse(fake.prepare_calls)
        self.assertFalse(fake.commit_calls)

    def test_pending_preview_does_not_query_source_or_roles(self):
        component = io.component("seller", "text", "body", "scope", "sales_text")
        pending = {"batch_id": "batch", "before_digest": wf.digest([old_sales()]), "content_seal": "seal", "observed_at": "2026-09-18 11:00:00", "observation": {}, "components": [component], "target_map": self.target_map, "attachments": {}, "after_reference": [old_sales()]}
        fake = FakeReferenceStore(head_with([old_sales()], pending=pending))
        result, _ = self._run(fake)
        self.assertEqual("pending_recovery_preview", result["execution_state"])
        self.assertFalse(self.db.calls)

    def test_changed_pending_target_is_blocked(self):
        component = io.component("seller", "text", "body", "scope", "sales_text")
        pending = {"batch_id": "batch", "before_digest": wf.digest([old_sales()]), "content_seal": "seal", "observed_at": "2026-09-18 11:00:00", "observation": {}, "components": [component], "target_map": self.target_map, "attachments": {}, "after_reference": [old_sales()]}
        fake = FakeReferenceStore(head_with([old_sales()], pending=pending))
        binding = {**self.binding, "target_map": {"seller": {"changed": "binding"}}}
        with self.assertRaisesRegex(Exception, "PRICE_PENDING_TARGET_BINDING_CHANGED"):
            self._run(fake, binding)


if __name__ == "__main__":
    unittest.main()
