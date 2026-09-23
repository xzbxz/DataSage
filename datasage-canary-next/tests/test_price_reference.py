import copy
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_business_contracts as base


price = importlib.import_module(base.TEST_PACKAGE + ".price_reference")
sales_entry = importlib.import_module(base.TEST_PACKAGE + ".sales_reference")


def purchase_rows(*, tax_inclue_price="12.3400", tax_exclue_price="10.1200", snapshot_at="2026-09-19T10:00:00"):
    return [
        {
            "id": 1,
            "goods_no": "SYN-1",
            "color_label": "",
            "supplier_no": "SYN-S",
            "supplier_name": "Synthetic supplier",
            "goods_name": "Synthetic",
            "tax_inclue_price": tax_inclue_price,
            "tax_exclue_price": tax_exclue_price,
            "currency_no": "USD",
            "unit_cuur": "m",
            "snapshot_at": snapshot_at,
        }
    ]


def purchase_plan(store, rows=None):
    return store.initialization_plan(
        rows or purchase_rows(),
        {
            "source": "synthetic_purchase_review",
            "observed_at": "2026-09-19T10:00:00+08:00",
            "history_unknown": True,
        },
    )


def purchase_payload(store, *, batch_id="purchase-b1", after=None):
    head = store.load()
    return {
        "batch_id": batch_id,
        "observed_at": "2026-09-19T11:00:00+08:00",
        "observation": {
            "before": {"tax_inclue_price": "12.3400", "tax_exclue_price": "10.1200"},
            "after": {"tax_inclue_price": "12.3401", "tax_exclue_price": "10.1201"},
        },
        "components": [{"key": "purchase-component"}],
        "target_map": {"purchase-component": {"platform": "synthetic"}},
        "attachments": {},
        "after_reference": after or purchase_rows(
            tax_inclue_price="12.3401",
            tax_exclue_price="10.1201",
            snapshot_at="2026-09-19T11:00:00",
        ),
    }, head["reference"]["digest"]


class PriceReferenceTests(unittest.TestCase):
    def test_side_paths_and_schemas_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            sales = price.PriceReferenceStore(profile)
            purchase = price.PriceReferenceStore(profile, side="purchase")
            sales_head = sales.initialize(
                {
                    **sales.initialization_plan(
                        [
                            {
                                "id": 1,
                                "goods_id": 1,
                                "dept": "HCM",
                                "customer_grade": "A",
                                "color_label": "",
                                "ddp_price": "1.2300",
                                "currency_no": "USD",
                                "matched_detail_id": 1,
                                "snapshot_at": "2026-09-19T10:00:00",
                            }
                        ],
                        {
                            "source": "synthetic_sales_review",
                            "observed_at": "2026-09-19T10:00:00+08:00",
                            "history_unknown": True,
                        },
                    ),
                    "approval_reason": "Synthetic explicit review",
                }
            )
            purchase_head = purchase.initialize(
                {**purchase_plan(purchase), "approval_reason": "Synthetic explicit review"}
            )
            self.assertEqual("datasage-sales-reference-head/v1", sales_head["schema"])
            self.assertEqual("datasage-purchase-reference-head/v1", purchase_head["schema"])
            self.assertEqual(sales.root.name, "sales_reference")
            self.assertEqual(purchase.root.name, "purchase_reference")
            self.assertNotEqual(sales.path, purchase.path)
            self.assertEqual(sales.load()["reference"]["rows"][0]["ddp_price"], "1.2300")
            self.assertEqual(purchase.load()["reference"]["rows"][0]["tax_inclue_price"], "12.3400")

    def test_purchase_decimal_values_are_exact_strings_without_rounding(self):
        with tempfile.TemporaryDirectory() as temp:
            store = price.PriceReferenceStore(Path(temp), side="purchase")
            plan = purchase_plan(store)
            initialized = store.initialize({**plan, "approval_reason": "Synthetic explicit review"})
            self.assertEqual("12.3400", initialized["reference"]["rows"][0]["tax_inclue_price"])
            self.assertEqual("10.1200", initialized["reference"]["rows"][0]["tax_exclue_price"])
            for field in ("tax_inclue_price", "tax_exclue_price"):
                invalid = purchase_rows()
                invalid[0][field] = 12.34
                with self.assertRaisesRegex(price.PriceReferenceError, "DECIMAL_STRING_REQUIRED"):
                    store.initialization_plan(
                        invalid,
                        {
                            "source": "synthetic_purchase_review",
                            "observed_at": "2026-09-19T10:00:00+08:00",
                            "history_unknown": True,
                        },
                    )

    def test_purchase_cas_and_bad_receipt_do_not_advance_head(self):
        with tempfile.TemporaryDirectory() as temp:
            store = price.PriceReferenceStore(Path(temp), side="purchase")
            store.initialize({**purchase_plan(store), "approval_reason": "Synthetic explicit review"})
            payload, before = purchase_payload(store)
            with self.assertRaisesRegex(price.PriceReferenceError, "CAS_MISMATCH"):
                store.prepare(payload, "0" * 64)
            self.assertIsNone(store.load()["pending"])
            store.prepare(payload, before)
            pending = store.load()["pending"]
            bad_receipt = {
                "batch_id": payload["batch_id"],
                "content_seal": pending["content_seal"],
                "status": "provider_error_40058",
                "component_keys": ["purchase-component"],
                "accepted_fingerprints": {"purchase-component": "rejected"},
            }
            with self.assertRaisesRegex(price.PriceReferenceError, "RECEIPT_NOT_ACCEPTED"):
                store.commit(payload["batch_id"], bad_receipt)
            after_bad = store.load()
            self.assertIsNotNone(after_bad["pending"])
            self.assertIsNone(after_bad["last_committed"])

    def test_receipt_bound_proof_cannot_be_replaced_after_head_write_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            store = price.PriceReferenceStore(Path(temp), side="purchase")
            store.initialize({**purchase_plan(store), "approval_reason": "Synthetic explicit review"})
            payload, before = purchase_payload(store, batch_id="proof-purchase")
            store.prepare(payload, before)
            pending = store.load()["pending"]
            receipt = {
                "batch_id": payload["batch_id"],
                "content_seal": pending["content_seal"],
                "status": "provider_accepted",
                "component_keys": ["purchase-component"],
                "accepted_fingerprints": {"purchase-component": "accepted-fingerprint"},
            }
            operations = importlib.import_module(base.TEST_PACKAGE + ".operations")
            original_atomic = operations._atomic

            def fail_head(path, value):
                if path == store.path and value.get("pending") is None:
                    raise OSError("synthetic purchase head write failure")
                return original_atomic(path, value)

            with patch.object(operations, "_atomic", side_effect=fail_head):
                with self.assertRaisesRegex(OSError, "synthetic purchase"):
                    store.commit(payload["batch_id"], receipt)
            self.assertIsNotNone(store.load()["pending"])
            with self.assertRaisesRegex(price.PriceReferenceError, "PROOF_RECEIPT_CHANGED"):
                store.commit(
                    payload["batch_id"],
                    {**receipt, "accepted_fingerprints": {"purchase-component": "changed"}},
                )
            committed = store.commit(payload["batch_id"], receipt)
            self.assertEqual("committed", committed["status"])
            self.assertEqual(
                "12.3401",
                committed["head"]["reference"]["rows"][0]["tax_inclue_price"],
            )

    def test_initialization_preview_date_and_path_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            store = price.PriceReferenceStore(Path(temp), side="purchase")
            plan = purchase_plan(store)
            preview = store.dryrun_initialize(plan)
            self.assertEqual("dryrun", preview["status"])
            self.assertFalse(preview["would_write"])
            self.assertFalse(store.path.exists())
            future = purchase_rows(snapshot_at="2026-09-20T10:00:00")
            with self.assertRaisesRegex(price.PriceReferenceError, "REFERENCE_VALIDATION_FAILED"):
                store.initialization_plan(
                    future,
                    {
                        "source": "synthetic_purchase_review",
                        "observed_at": "2026-09-19T10:00:00+08:00",
                        "history_unknown": True,
                    },
                )
            store.initialize({**plan, "approval_reason": "Synthetic explicit review"})
            store.proof_root = store.root / ".." / "outside"
            with self.assertRaisesRegex(price.PriceReferenceError, "PROOF_PATH_INVALID"):
                store._proof_path("guarded")

    def test_sales_compatibility_entry_reads_shared_implementation(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            store = sales_entry.SalesReferenceStore(profile)
            rows = [
                {
                    "id": 1,
                    "goods_id": 1,
                    "dept": "HCM",
                    "customer_grade": "A",
                    "color_label": "",
                    "ddp_price": "7.0000",
                    "currency_no": "USD",
                    "matched_detail_id": 1,
                    "snapshot_at": "2026-09-19T10:00:00",
                }
            ]
            plan = store.initialization_plan(
                rows,
                {
                    "source": "legacy_sales_head_fixture",
                    "observed_at": "2026-09-19T10:00:00+08:00",
                    "history_unknown": True,
                },
            )
            store.initialize({**plan, "approval_reason": "Synthetic explicit review"})
            restored = price.PriceReferenceStore(profile)
            self.assertIs(restored.__class__, price.PriceReferenceStore)
            self.assertEqual(rows, restored.load()["reference"]["rows"])
            self.assertEqual(store.path, restored.path)
            self.assertIs(sales_entry.SalesReferenceStore, price.PriceReferenceStore)


if __name__ == "__main__":
    unittest.main()

