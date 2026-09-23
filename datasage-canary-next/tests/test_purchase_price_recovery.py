"""Independent purchase-price local-reference acceptance.

These tests deliberately enter ``workflow_io.run_bound`` and
``workflow_io._produce_and_execute``.  The source and the target are both
synthetic: the source is a consistent snapshot fixture and the target is an
explicit ``wecom_app_http`` appchat descriptor.  ``FakeTransport`` records
what the real business runner prepared and exercises the shared receipt
adapter without opening a socket.

The test does not replace the continuity planner or the compatibility
renderer.  It only supplies the database boundary, profile binding, clock,
and transport that the production entry expects.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import importlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_business_contracts as base


io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")
runner = importlib.import_module(base.TEST_PACKAGE + ".purchase_price_runner")
content = importlib.import_module(base.TEST_PACKAGE + ".purchase_price_content")
reference = importlib.import_module(base.TEST_PACKAGE + ".price_reference")
operations = importlib.import_module(base.TEST_PACKAGE + ".operations")
wf = importlib.import_module(base.TEST_PACKAGE + ".legacy_workflow")
local_report = importlib.import_module(base.TEST_PACKAGE + ".local_report")
contract_store = importlib.import_module(base.TEST_PACKAGE + ".contract_store")


REGIONS = ["HCM", "HN", "BKK", "IDK"]
GROUP_ACCOUNT = "synthetic-purchase-group"
GROUP_TARGET = {
    "platform": "wecom_app_http",
    "app_name": "synthetic-purchase-app",
    "corp_id": "synthetic-corp",
    "agent_id": "1000043",
    "target_kind": "appchat",
    "target_id": "synthetic-purchase-appchat",
}


def _stamp(value: str | datetime) -> str:
    return value.isoformat(sep=" ") if isinstance(value, datetime) else str(value)


def _row(
    goods_no: str = "PUR-001",
    *,
    color: str = "Red",
    supplier: str = "SUP-A",
    supplier_name: str = "Supplier A",
    inc: str | None = "100.0000",
    exc: str | None = "80.0000",
    currency: str | None = "CNY",
    unit: str | None = "m",
    detail_id: int = 1001,
    row_id: int = 1,
    at: str = "2026-09-19 10:00:00",
    observed_at: str | None = None,
    goods_name: str = "Synthetic Product",
) -> dict:
    """Return one complete purchase quote at the fixture observation clock."""

    observed = observed_at or at
    return {
        "id": row_id,
        "goods_no": goods_no,
        "goods_name": goods_name,
        "color_label": color,
        "supplier_no": supplier,
        "supplier_name": supplier_name,
        "tax_inclue_price": inc,
        "tax_exclue_price": exc,
        "currency_no": currency,
        "unit_cuur": unit,
        "effective_date": "2026-01-01 00:00:00",
        "expiration_date": "2026-12-31 23:59:59",
        "gmt_modified": at,
        "detail_id": detail_id,
        "observed_at": observed,
    }


class SyntheticPurchaseDB:
    """A read-only DB boundary with one authoritative observation clock."""

    marker = "synthetic-purchase-snapshot-v1"

    def __init__(self, rows: list[dict], at: str = "2026-09-19 10:00:00"):
        self.at = at
        self.rows: list[dict] = []
        self.calls: list[tuple[str, tuple, int]] = []
        self.set_rows(rows, at=at)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_rows(self, rows: list[dict], *, at: str | None = None) -> None:
        if at is not None:
            self.at = at
        # The fixture keeps DB.at and every source row's observed_at together;
        # changing only the host clock must never be enough to pass a test.
        self.rows = []
        for row in rows:
            value = deepcopy(row)
            value["observed_at"] = self.at
            value["gmt_modified"] = self.at
            self.rows.append(value)

    def execute(self, sql, params=(), limit=10000, **kwargs):
        del kwargs
        text = str(sql)
        self.calls.append((text, tuple(params or ()), int(limit)))
        upper = text.upper()
        if "SELECT NOW(6)" in upper:
            # Support both source-query aliases used by the two live paths.
            return [{"at": self.at, "observed_at": self.at, "utc_at": self.at}], False, {}
        if "UTC_TIMESTAMP" in upper:
            return [{"observed_utc": self.at}], False, {}
        if "PURCHASE_PRICE_BILL_DETAIL_DWD" in upper or "WITH POOL AS" in upper:
            return deepcopy(self.rows), False, {}
        # A purchase runner must not silently fall back to personnel/customer
        # lookups.  Returning no rows here keeps that accidental path visible.
        return [], False, {}


class _NoReadSnapshot:
    def __enter__(self):
        raise AssertionError("pending purchase recovery must not read source")

    def __exit__(self, *args):
        return False


class FakeTransport:
    """Offline transport using the official receipt shape and appchat target."""

    def __init__(self, *, outcome: str = "success", fail_on_send: int | None = None):
        self.outcome = outcome
        self.fail_on_send = fail_on_send
        self.preflights: list[list[dict]] = []
        self.sends: list[dict] = []
        self.texts: list[str] = []
        self.target_map = {GROUP_ACCOUNT: deepcopy(GROUP_TARGET)}

    def normalize(self, components, progress):
        # Keep the real UTF-8 split behavior so a long notice becomes multiple
        # parts under the same appchat notification.  No network/config loader
        # is involved in this pure preparation step.
        normalizer = object.__new__(
            importlib.import_module(base.TEST_PACKAGE + ".wecom_app_transport").AppTransport
        )
        return normalizer.normalize(components, progress)

    def preflight(self, components):
        captured = [deepcopy(item) for item in components]
        self.preflights.append(captured)
        for item in components:
            self._assert_appchat_target(item)

    def _assert_appchat_target(self, item):
        target = self.target_map.get(item.get("account"), GROUP_TARGET)
        if item.get("account") != GROUP_ACCOUNT:
            raise AssertionError(f"unexpected purchase recipient: {item.get('account')!r}")
        if target["platform"] != "wecom_app_http" or target["target_kind"] != "appchat":
            raise AssertionError("purchase fixture must use the explicit application group route")
        if target["target_id"].startswith("http"):
            raise AssertionError("purchase fixture must not use a webhook URL")

    def delivery_lock(self, _progress):
        return nullcontext()

    def fingerprint(self, item):
        content = item.get("text", "")
        return sha256(content.encode("utf-8")).hexdigest()

    def send(self, item):
        self.sends.append(deepcopy(item))
        self.texts.append(str(item.get("text", "")))
        attempt = len(self.sends)
        if self.outcome == "unknown":
            raise TimeoutError("synthetic purchase timeout")
        if self.outcome == "bare_success":
            # Process exit 0 alone has no provider business evidence.
            return {"success": True}
        if self.outcome == "http_200_no_business":
            return {"success": True, "status_code": 200, "raw_response": {"http_status": 200}}
        if self.outcome == "exit_0_no_business":
            return {"success": True, "raw_response": {"exit_code": 0}}
        if self.outcome == "message_id_only":
            return {"success": True, "message_id": "synthetic-but-no-business-code"}
        if self.outcome == "markdown_mention_40058":
            return {
                "success": True,
                "message_id": "synthetic-markdown",
                "raw_response": {"markdown_errcode": 0, "mention_errcode": 40058},
            }
        if self.outcome == "markdown_all_zero":
            return {
                "success": True,
                "message_id": "synthetic-markdown",
                "raw_response": {"markdown_errcode": 0, "mention_errcode": 0},
            }
        if self.outcome == "errcode_40058":
            # Deliberately model HTTP 200 plus success=True with an API error.
            return {
                "success": True,
                "status_code": 200,
                "message_id": "synthetic-http-200",
                "raw_response": {"errcode": 40058, "errmsg": "invalid appchat"},
            }
        if self.fail_on_send == attempt or self.outcome == "failed":
            return {
                "success": False,
                "status_code": 200,
                "message_id": "synthetic-failed",
                "raw_response": {"errcode": 45009, "errmsg": "synthetic failure"},
            }
        return {
            "success": True,
            "status_code": 200,
            "message_id": f"synthetic-{attempt}",
            "raw_response": {"errcode": 0, "errmsg": "ok"},
        }


def _binding(*, send_enabled: bool = True, target_map: dict | None = None) -> dict:
    return {
        "kind": "legacy_execution",
        "enabled": True,
        "read_enabled": True,
        "customer_mapping_enabled": False,
        "freeze_enabled": False,
        "send_enabled": send_enabled,
        "price_accept_enabled": True,
        "register_schedule_enabled": False,
        "schedule": None,
        "failure_deliver": False,
        "recipients_file": "local/workflow-roles.json",
        "target_map": deepcopy(target_map or {GROUP_ACCOUNT: deepcopy(GROUP_TARGET)}),
        "operation": {
            "kind": "purchase_prices",
            "regions": list(REGIONS),
            "limit": 10000,
            "reference_source": "profile_local",
        },
    }


def _store(profile: Path):
    return reference.PriceReferenceStore(profile, side="purchase")


def _initialize(profile: Path, rows: list[dict], *, at: str = "2026-09-19 09:00:00"):
    store = _store(profile)
    initial = []
    for index, row in enumerate(rows, 1):
        value = deepcopy(row)
        value["id"] = index
        value["snapshot_at"] = at
        initial.append(value)
    plan = store.initialization_plan(
        initial,
        {
            "source": "synthetic_purchase_review",
            "observed_at": at,
            "snapshot_marker": "synthetic-initial-marker",
            "history_unknown": True,
        },
    )
    return store, store.initialize({**plan, "approval_reason": "Synthetic purchase review"})


def _progress(profile: Path):
    return io.Progress(profile, "purchase_price", "price-events")


def _direct(
    profile: Path,
    db,
    transport,
    *,
    progress=None,
    binding=None,
    now: str = "2026-09-19 10:10:00",
    out: Path | None = None,
):
    out = out or (profile / "out")
    out.mkdir(parents=True, exist_ok=True)
    binding = binding or _binding()
    progress = progress or _progress(profile)
    snapshots = (lambda: _NoReadSnapshot()) if db is None else (lambda: db)
    if hasattr(transport, "target_map"):
        transport.target_map = deepcopy(binding.get("target_map") or {})
    # Every temporary profile gets the checked-in contract set and its own
    # contract-store snapshot.  This is the same profile-root isolation used
    # by the real bound entry, while direct seam tests still exercise the
    # actual purchase reader and engine.
    contracts = profile / "plugins" / "datasage-query" / "contracts"
    if not contracts.exists():
        contracts.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base.PLUGIN_ROOT / "contracts", contracts)
    contract_store.reset_contract_snapshot_for_tests()
    with (
        patch.object(contract_store, "profile_root", return_value=profile),
        patch.object(local_report, "configure_runtime"),
        patch.object(runner, "_now_local", return_value=runner._parse_time(now)),
    ):
        return io._produce_and_execute(
            profile,
            "purchase_price",
            binding,
            out,
            "2026-W38",
            "2026-09",
            progress,
            snapshots,
            transport,
            None,
        )


def _bound(profile: Path, db, transport, *, now: str = "2026-09-19 10:10:00"):
    """Call the real bound entry while replacing only runtime/profile seams."""

    contracts = profile / "plugins" / "datasage-query" / "contracts"
    contracts.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base.PLUGIN_ROOT / "contracts", contracts)
    contract_store.reset_contract_snapshot_for_tests()
    binding = _binding()
    if hasattr(transport, "target_map"):
        transport.target_map = deepcopy(binding.get("target_map") or {})
    with (
        patch.object(io, "load_activation", return_value=binding),
        patch.object(local_report, "configure_runtime"),
        patch.object(contract_store, "profile_root", return_value=profile),
        patch.object(runner, "_now_local", return_value=runner._parse_time(now)),
    ):
        return io.run_bound(profile, "purchase_price", transport=transport, snapshot_factory=lambda: db)


def _head_path(profile: Path) -> Path:
    return profile / "report_runs" / "legacy_execution" / "purchase_reference" / "head.json"


def _head(profile: Path) -> dict:
    return json.loads(_head_path(profile).read_text(encoding="utf-8"))


def _rows_from_head(profile: Path) -> list[dict]:
    return _store(profile).load()["reference"]["rows"]


def _one(**kwargs):
    return _row(**kwargs)


class PurchasePriceRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Each case uses a fresh temporary profile.  The contract store keeps
        # a process-level pinned snapshot, so reset it at both boundaries to
        # prevent this suite's profile root from leaking into later suites.
        contract_store.reset_contract_snapshot_for_tests()

    def tearDown(self):
        contract_store.reset_contract_snapshot_for_tests()

    def test_runner_uses_full_purchase_content_module(self):
        self.assertTrue(hasattr(content, "build_purchase_parts"))
        self.assertEqual("purchase_price_content.py", Path(content.__file__).name)
        self.assertNotEqual("legacy_price_compat.py", Path(content.__file__).name)

    def test_real_run_bound_uses_database_clock_and_explicit_appchat(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            initial = _one(inc="100.0000", exc="80.0000", at="2026-09-19 09:00:00")
            _initialize(profile, [initial], at="2026-09-19 09:00:00")
            current = _one(inc="110.0000", exc="88.0000", at="2026-09-19 10:00:00")
            db = SyntheticPurchaseDB([current], at="2026-09-19 10:00:00")
            transport = FakeTransport()
            result = _bound(profile, db, transport, now="2026-09-19 10:10:00")
            self.assertEqual("success", result["status"])
            self.assertTrue(result["head_advanced"])
            self.assertEqual("2026-09-19 10:00:00", result["observed_at"])
            self.assertGreaterEqual(len(transport.sends), 1)
            self.assertEqual("appchat", GROUP_TARGET["target_kind"])
            self.assertTrue(all(item["account"] == GROUP_ACCOUNT for item in transport.sends))
            body = "\n".join(transport.texts)
            self.assertIn("PUR-001", body)
            self.assertIn("Supplier A", body)
            self.assertIn("Red", body)
            self.assertEqual("110.0000", _rows_from_head(profile)[0]["tax_inclue_price"])
            self.assertEqual("88.0000", _rows_from_head(profile)[0]["tax_exclue_price"])
            self.assertTrue(any("SELECT" in sql.upper() for sql, _, _ in db.calls))

    def test_purchase_rejects_personal_or_webhook_fallback_target_before_source(self):
        for target_kind, target_id in (("user", "synthetic-user"), ("appchat", "https://qyapi.weixin.qq.com/webhook/send?key=x")):
            with self.subTest(target_kind=target_kind), TemporaryDirectory() as temp:
                profile = Path(temp)
                _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
                target = {GROUP_ACCOUNT: {**GROUP_TARGET, "target_kind": target_kind, "target_id": target_id}}
                binding = _binding(target_map=target)
                db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
                transport = FakeTransport()
                with self.assertRaisesRegex(Exception, "GROUP_TARGET|appchat|TARGET|webhook"):
                    _direct(profile, db, transport, binding=binding)
                self.assertEqual([], transport.sends)
                self.assertEqual([], db.calls)

    def test_same_direction_tax_sides_and_one_sided_changes_keep_exact_fields(self):
        scenarios = (
            ("up", "110.0000", "88.0000", ("🔺 采购价上调", "含税价：100.00 → 110.00")),
            ("down", "90.0000", "72.0000", ("🔻 采购价下调", "含税价：100.00 → 90.00")),
            ("inc-only", "110.0000", "80.0000", ("🔺 采购价上调", "含税价：100.00 → 110.00")),
            ("exc-only", "100.0000", "72.0000", ("🔻 采购价下调", "不含税价：80.00 → 72.00")),
        )
        for label, inc, exc, expected in scenarios:
            with self.subTest(label=label), TemporaryDirectory() as temp:
                profile = Path(temp)
                _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
                db = SyntheticPurchaseDB([_one(inc=inc, exc=exc)], at="2026-09-19 10:00:00")
                transport = FakeTransport()
                result = _direct(profile, db, transport)
                self.assertEqual("success", result["status"])
                body = "\n".join(transport.texts)
                self.assertIn(expected[0], body)
                self.assertIn(expected[1], body)
                if label == "inc-only":
                    self.assertNotIn("\n不含税价：", body)
                if label == "exc-only":
                    self.assertNotIn("\n含税价：", body)

    def test_opposite_tax_directions_use_tax_excluded_direction_first(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            # Included price rises while excluded price falls.  The legacy
            # purchase rule chooses the non-tax delta before the tax delta.
            db = SyntheticPurchaseDB(
                [_one(inc="110.0000", exc="70.0000")],
                at="2026-09-19 10:00:00",
            )
            transport = FakeTransport()
            _direct(profile, db, transport)
            body = "\n".join(transport.texts)
            self.assertIn("🔻 采购价下调", body)
            self.assertNotIn("🔺 采购价上调", body)
            self.assertIn("含税价：100.00 → 110.00", body)
            self.assertIn("不含税价：80.00 → 70.00", body)

    def test_supplier_and_color_are_independent_price_keys(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            first = _one(at="2026-09-19 09:00:00", color="Red", supplier="SUP-A", row_id=1, detail_id=1001)
            second = _one(at="2026-09-19 09:00:00", color="Blue", supplier="SUP-B", supplier_name="Supplier B", row_id=2, detail_id=1002)
            _initialize(profile, [first, second], at="2026-09-19 09:00:00")
            current = [deepcopy(first), deepcopy(second)]
            current[0].update(tax_inclue_price="101.0000", gmt_modified="2026-09-19 10:00:00")
            db = SyntheticPurchaseDB(current, at="2026-09-19 10:00:00")
            transport = FakeTransport()
            _direct(profile, db, transport)
            body = "\n".join(transport.texts)
            self.assertIn("Supplier A", body)
            self.assertIn("色标：Red", body)
            self.assertIn("PUR-001", body)
            self.assertNotIn("Supplier B", body)
            self.assertNotIn("色标：Blue", body)
            after = _rows_from_head(profile)
            by_key = {(r["goods_no"], r["color_label"], r["supplier_no"]): r for r in after}
            self.assertEqual("101.0000", by_key[("PUR-001", "Red", "SUP-A")]["tax_inclue_price"])
            self.assertEqual("100.0000", by_key[("PUR-001", "Blue", "SUP-B")]["tax_inclue_price"])

    def test_no_change_new_key_and_reentry_are_silent(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            original = _one(at="2026-09-19 09:00:00")
            _initialize(profile, [original], at="2026-09-19 09:00:00")
            same_db = SyntheticPurchaseDB([original], at="2026-09-19 10:00:00")
            same_transport = FakeTransport()
            same = _direct(profile, same_db, same_transport)
            self.assertEqual("silent_no_deliverable_change", same["delivery"])
            self.assertEqual([], same_transport.sends)

            new = _one(goods_no="PUR-NEW", row_id=2, detail_id=2002, inc="50.00", exc="40.00")
            new_db = SyntheticPurchaseDB([original, new], at="2026-09-19 11:00:00")
            new_transport = FakeTransport()
            new_result = _direct(profile, new_db, new_transport, now="2026-09-19 11:10:00")
            self.assertEqual("silent_no_deliverable_change", new_result["delivery"])
            self.assertEqual([], new_transport.sends)
            self.assertIn("PUR-NEW", {r["goods_no"] for r in _rows_from_head(profile)})

            absent_db = SyntheticPurchaseDB([original], at="2026-09-19 12:00:00")
            absent_transport = FakeTransport()
            absent = _direct(profile, absent_db, absent_transport, now="2026-09-19 12:10:00")
            self.assertEqual("silent_no_deliverable_change", absent["delivery"])
            self.assertEqual([], absent_transport.sends)

            returned = deepcopy(new)
            returned["tax_inclue_price"] = "55.00"
            returned_db = SyntheticPurchaseDB([original, returned], at="2026-09-19 13:00:00")
            returned_transport = FakeTransport()
            returned_result = _direct(profile, returned_db, returned_transport, now="2026-09-19 13:10:00")
            self.assertEqual("silent_no_deliverable_change", returned_result["delivery"])
            self.assertEqual([], returned_transport.sends)

    def test_currency_unit_changes_and_missing_quote_keep_old_reference(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            currency = _one(goods_no="PUR-CURRENCY", row_id=1, detail_id=1001, at="2026-09-19 09:00:00")
            unit = _one(goods_no="PUR-UNIT", row_id=2, detail_id=1002, at="2026-09-19 09:00:00")
            missing = _one(goods_no="PUR-MISSING", row_id=3, detail_id=1003, at="2026-09-19 09:00:00")
            _initialize(profile, [currency, unit, missing], at="2026-09-19 09:00:00")
            current = [deepcopy(currency), deepcopy(unit), deepcopy(missing)]
            current[0].update(currency_no="USD", tax_inclue_price="999.00")
            current[1].update(unit_cuur="kg", tax_exclue_price="999.00")
            current[2].update(tax_inclue_price=None, tax_exclue_price="70.0000")
            db = SyntheticPurchaseDB(current, at="2026-09-19 10:00:00")
            transport = FakeTransport()
            result = _direct(profile, db, transport)
            self.assertEqual("silent_no_deliverable_change", result["delivery"])
            self.assertEqual([], transport.sends)
            after = _rows_from_head(profile)
            by_key = {r["goods_no"]: r for r in after}
            self.assertEqual("100.0000", by_key["PUR-CURRENCY"]["tax_inclue_price"])
            self.assertEqual("CNY", by_key["PUR-CURRENCY"]["currency_no"])
            self.assertEqual("80.0000", by_key["PUR-UNIT"]["tax_exclue_price"])
            self.assertEqual("m", by_key["PUR-UNIT"]["unit_cuur"])
            self.assertEqual("100.0000", by_key["PUR-MISSING"]["tax_inclue_price"])

    def test_exact_decimal_strings_survive_reference_and_notice(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            initial = _one(inc="0.000000000000000001", exc="1.000000000000000001", at="2026-09-19 09:00:00")
            _initialize(profile, [initial], at="2026-09-19 09:00:00")
            current = _one(inc="0.000000000000000002", exc="1.000000000000000002")
            db = SyntheticPurchaseDB([current], at="2026-09-19 10:00:00")
            transport = FakeTransport()
            _direct(profile, db, transport)
            stored = _rows_from_head(profile)[0]
            self.assertEqual(Decimal("0.000000000000000002"), Decimal(stored["tax_inclue_price"]))
            self.assertEqual(Decimal("1.000000000000000002"), Decimal(stored["tax_exclue_price"]))
            self.assertIsInstance(stored["tax_inclue_price"], str)
            self.assertIn("0.000000000000000001 → 0.000000000000000002", "\n".join(transport.texts))

    def test_all_parts_must_be_accepted_before_reference_advances_and_recovery_does_not_requery(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            baseline = [
                _one(goods_no=f"PUR-{index:03d}", row_id=index, detail_id=2000 + index, at="2026-09-19 09:00:00")
                for index in range(1, 100)
            ]
            _initialize(profile, baseline, at="2026-09-19 09:00:00")
            changed = [
                {**row, "tax_inclue_price": "101.0000", "tax_exclue_price": "81.0000"}
                for row in baseline
            ]
            db = SyntheticPurchaseDB(changed, at="2026-09-19 10:00:00")
            failed = FakeTransport(fail_on_send=2)
            with self.assertRaisesRegex(Exception, "FAILED|INCOMPLETE"):
                _direct(profile, db, failed)
            self.assertGreaterEqual(len(failed.sends), 2)
            self.assertEqual("100.0000", _rows_from_head(profile)[0]["tax_inclue_price"])
            pending = _store(profile).load()["pending"]
            self.assertIsNotNone(pending)
            self.assertGreater(len(pending["components"]), 1)

            # The source is deliberately changed and then made unreadable.  A
            # recovery must replay the sealed pending parts only.
            db.set_rows([_one(goods_no="PUR-001", inc="999.00", exc="999.00")], at="2026-09-19 11:00:00")
            recovered = FakeTransport()
            result = _direct(profile, None, recovered, now="2026-09-19 11:10:00")
            self.assertTrue(result["head_advanced"])
            self.assertEqual("101.0000", _rows_from_head(profile)[0]["tax_inclue_price"])
            self.assertEqual("81.0000", _rows_from_head(profile)[0]["tax_exclue_price"])
            self.assertEqual(len(pending["components"]) - 1, len(recovered.sends))
            self.assertNotIn(failed.sends[0]["text"], [item["text"] for item in recovered.sends])

    def test_errcode_40058_fails_even_with_http_200_success_true_and_does_not_advance(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
            transport = FakeTransport(outcome="errcode_40058")
            with self.assertRaisesRegex(Exception, "FAILED|40058|INCOMPLETE"):
                _direct(profile, db, transport)
            self.assertEqual(1, len(transport.sends))
            self.assertEqual("100.0000", _rows_from_head(profile)[0]["tax_inclue_price"])
            progress = _progress(profile)
            self.assertIn("failed", {value.get("status") for value in progress.data["components"].values()})

    def test_bare_success_without_business_receipt_is_not_accepted(self):
        for outcome in ("bare_success", "http_200_no_business", "exit_0_no_business", "message_id_only", "markdown_mention_40058"):
            with self.subTest(outcome=outcome), TemporaryDirectory() as temp:
                profile = Path(temp)
                _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
                db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
                transport = FakeTransport(outcome=outcome)
                with self.assertRaisesRegex(Exception, "UNKNOWN|UNVERIFIED|INCOMPLETE|FAILED"):
                    _direct(profile, db, transport)
                self.assertEqual("100.0000", _rows_from_head(profile)[0]["tax_inclue_price"])
                statuses = {value.get("status") for value in _progress(profile).data["components"].values()}
                self.assertTrue(statuses & {"unverified_success", "unknown", "failed"})

    def test_markdown_business_and_mention_codes_are_both_required(self):
        good = runner.PurchaseReceiptGateTransport(FakeTransport(outcome="markdown_all_zero"))
        accepted = good.send({"account": GROUP_ACCOUNT, "kind": "text", "text": "synthetic"})
        self.assertTrue(accepted["success"])
        bad = runner.PurchaseReceiptGateTransport(FakeTransport(outcome="markdown_mention_40058"))
        rejected = bad.send({"account": GROUP_ACCOUNT, "kind": "text", "text": "synthetic"})
        self.assertFalse(rejected["success"])

    def test_timeout_is_unknown_and_subsequent_run_holds_without_duplicate_send(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
            timeout = FakeTransport(outcome="unknown")
            with self.assertRaisesRegex(Exception, "UNKNOWN"):
                _direct(profile, db, timeout)
            before = len(timeout.sends)
            with self.assertRaisesRegex(Exception, "UNKNOWN|REVIEW"):
                _direct(profile, None, FakeTransport(), now="2026-09-19 10:20:00")
            self.assertEqual(before, len(timeout.sends))
            self.assertEqual("100.0000", _rows_from_head(profile)[0]["tax_inclue_price"])

    def test_pending_target_and_body_seal_tampering_are_blocked_before_send(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
            failed = FakeTransport(outcome="failed")
            with self.assertRaises(Exception):
                _direct(profile, db, failed)
            changed_target = {
                GROUP_ACCOUNT: {**GROUP_TARGET, "target_id": "other-appchat"}
            }
            with self.assertRaisesRegex(Exception, "TARGET|BINDING|CONTENT"):
                _direct(profile, None, FakeTransport(), binding=_binding(target_map=changed_target), now="2026-09-19 10:10:00")
            # Restore the target binding and corrupt pending body without
            # forging the seal.  The reference store must reject it before the
            # transport sees a send.
            head_path = _head_path(profile)
            head = json.loads(head_path.read_text(encoding="utf-8"))
            head["pending"]["components"][0]["text"] = "tampered purchase notice"
            head_path.write_text(json.dumps(head, ensure_ascii=False), encoding="utf-8")
            clean = FakeTransport()
            with self.assertRaisesRegex(Exception, "SEAL|HEAD|CONTENT|INVALID"):
                _direct(profile, None, clean, now="2026-09-19 10:20:00")
            self.assertEqual([], clean.sends)

    def test_generation_failure_sends_zero(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
            transport = FakeTransport()
            self.assertTrue(hasattr(runner, "_prepare_components"), "purchase runner must expose preparation seam")
            error = getattr(runner, "PurchasePriceRunnerError", io.IOErrorBoundary)("SYNTHETIC_PREPARE_FAILURE")
            with patch.object(runner, "_prepare_components", side_effect=error), self.assertRaisesRegex(Exception, "PREPARE"):
                _direct(profile, db, transport)
            self.assertEqual([], transport.sends)
            self.assertEqual("100.0000", _rows_from_head(profile)[0]["tax_inclue_price"])

    def test_missing_manifest_or_fingerprint_is_blocked_before_delivery(self):
        for missing in ("manifest", "fingerprint"):
            with self.subTest(missing=missing), TemporaryDirectory() as temp:
                profile = Path(temp)
                rows = [
                    _one(goods_no=f"PUR-{index:03d}", row_id=index, detail_id=3000 + index, at="2026-09-19 09:00:00")
                    for index in range(1, 90)
                ]
                _initialize(profile, rows, at="2026-09-19 09:00:00")
                current_rows = [
                    {**row, "tax_inclue_price": "101.00", "tax_exclue_price": "81.00"}
                    for row in rows
                ]
                failed = FakeTransport(fail_on_send=2)
                with self.assertRaises(Exception):
                    _direct(profile, SyntheticPurchaseDB(current_rows, at="2026-09-19 10:00:00"), failed)
                progress_path = io.Progress(profile, "purchase_price", "price-events").path
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                if missing == "manifest":
                    progress["notification_manifests"] = {}
                    expected = "MANIFEST"
                else:
                    accepted = next(
                        key for key, value in progress["components"].items()
                        if value.get("status") == "provider_accepted"
                    )
                    progress["components"][accepted].pop("fingerprint", None)
                    expected = "FINGERPRINT"
                progress_path.write_text(json.dumps(progress), encoding="utf-8")
                recovery = FakeTransport()
                with self.assertRaisesRegex(Exception, expected):
                    _direct(profile, None, recovery, now="2026-09-19 10:20:00")
                self.assertEqual([], recovery.sends)
                self.assertEqual("100.0000", _rows_from_head(profile)[0]["tax_inclue_price"])

    def test_commit_failure_before_head_write_recovers_without_duplicate_send(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
            first = FakeTransport()
            real_commit = reference.PriceReferenceStore.commit
            failed_once = {"value": True}

            def fail_before_commit(store, batch_id, receipt):
                if failed_once["value"]:
                    failed_once["value"] = False
                    raise reference.PriceReferenceError("SYNTHETIC_COMMIT_BEFORE_HEAD_WRITE")
                return real_commit(store, batch_id, receipt)

            with patch.object(reference.PriceReferenceStore, "commit", fail_before_commit), self.assertRaisesRegex(Exception, "COMMIT|HEAD_WRITE"):
                _direct(profile, db, first)
            self.assertGreater(len(first.sends), 0)
            recovery = FakeTransport()
            result = _direct(profile, None, recovery, now="2026-09-19 10:20:00")
            self.assertTrue(result["head_advanced"])
            self.assertEqual([], recovery.sends)
            self.assertEqual("110.00", _rows_from_head(profile)[0]["tax_inclue_price"])

    def test_manifest_write_failure_after_commit_restarts_same_hour_without_send(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
            first = FakeTransport()
            real_write = runner._write_manifest
            failed_once = {"value": True}

            def fail_after_commit(out, value):
                if failed_once["value"]:
                    failed_once["value"] = False
                    raise RuntimeError("SYNTHETIC_MANIFEST_AFTER_COMMIT")
                return real_write(out, value)

            with patch.object(runner, "_write_manifest", fail_after_commit), self.assertRaisesRegex(RuntimeError, "AFTER_COMMIT"):
                _direct(profile, db, first)
            second = FakeTransport()
            result = _direct(profile, None, second, now="2026-09-19 10:20:00")
            self.assertEqual("same_hour_already_committed", result["execution_state"])
            self.assertEqual([], second.sends)

    def test_source_hour_idempotence_uses_observed_hour_and_purchase_lock_isolated(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            _initialize(profile, [_one(at="2026-09-19 09:00:00")], at="2026-09-19 09:00:00")
            first_db = SyntheticPurchaseDB([_one(inc="110.00", exc="88.00")], at="2026-09-19 10:00:00")
            first = FakeTransport()
            _direct(profile, first_db, first, now="2026-09-19 10:10:00")
            second_db = SyntheticPurchaseDB([_one(inc="120.00", exc="96.00")], at="2026-09-19 10:55:00")
            second = FakeTransport()
            result = _direct(profile, second_db, second, now="2026-09-19 11:05:00")
            self.assertEqual("same_source_hour_already_committed", result["execution_state"])
            self.assertEqual([], second.sends)
            self.assertEqual("110.00", _rows_from_head(profile)[0]["tax_inclue_price"])
            with io.price_run_lock(profile, "purchase_price"):
                with self.assertRaisesRegex(Exception, "BUSY|LOCK|UNAVAILABLE"):
                    with io.price_run_lock(profile, "purchase_price"):
                        pass
            marker = io.private_root(profile) / "purchase_price.lock"
            marker.write_text("synthetic stale marker", encoding="utf-8")
            try:
                with self.assertRaisesRegex(Exception, "BUSY|STALE"):
                    with io.price_run_lock(profile, "purchase_price"):
                        pass
            finally:
                marker.unlink(missing_ok=True)

    def test_legacy_database_purchase_reference_is_read_only(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            called = []
            document = {
                "status": "success",
                "kind": "purchase_prices",
                "baseline_source": "legacy_database",
                "reference": {"rows": 1},
                "events": [],
                "scope_notice": "synthetic read only",
            }
            bridge = importlib.import_module(base.TEST_PACKAGE + ".legacy_price_bridge")
            contracts = profile / "plugins" / "datasage-query" / "contracts"
            contracts.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(base.PLUGIN_ROOT / "contracts", contracts)
            contract_store.reset_contract_snapshot_for_tests()
            with (
                patch.object(contract_store, "profile_root", return_value=profile),
                patch.object(local_report, "_assert_local_context"),
                patch.object(bridge, "observe", side_effect=lambda binding: called.append(binding) or document),
            ):
                result = operations.execute(
                    profile,
                    "purchase_price",
                    {
                        "kind": "purchase_prices",
                        "regions": list(REGIONS),
                        "limit": 10,
                        "reference_source": "legacy_database",
                    },
                )
            self.assertEqual(document, result)
            self.assertTrue(called)
            self.assertFalse(_head_path(profile).exists())

    def test_sales_reference_and_purchase_reference_heads_are_separate(self):
        sales_reference = importlib.import_module(base.TEST_PACKAGE + ".price_reference")
        with TemporaryDirectory() as temp:
            profile = Path(temp)
            purchase_store = sales_reference.PriceReferenceStore(profile, side="purchase")
            sales_store = sales_reference.PriceReferenceStore(profile, side="sales")
            self.assertNotEqual(purchase_store.root, sales_store.root)
            self.assertEqual("purchase", purchase_store.side)
            self.assertEqual("sales", sales_store.side)
            self.assertTrue(io.local_price_reference("purchase_price", _binding()))
            sales_binding = _binding()
            sales_binding["operation"] = {**sales_binding["operation"], "kind": "sales_prices"}
            self.assertTrue(io.local_sales_reference("sales_price", sales_binding))


if __name__ == "__main__":
    unittest.main()
