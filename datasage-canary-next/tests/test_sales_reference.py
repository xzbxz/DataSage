import copy
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import test_business_contracts as base


reference = __import__(base.TEST_PACKAGE + ".sales_reference", fromlist=["*"])
continuity = __import__(base.TEST_PACKAGE + ".workflow_price_continuity", fromlist=["*"])


def rows():
    return [{"id": 1, "goods_id": 1, "dept": "HCM", "customer_grade": "A", "color_label": "Normal", "ddp_price": "12.3456", "currency_no": "VND", "matched_detail_id": 1, "snapshot_at": "2026-09-19T10:00:00"}]


def plan(store):
    return store.initialization_plan(rows(), {"source": "synthetic_review", "history_unknown": True, "observed_at": "2026-09-19T10:00:00+08:00"})


class SalesReferenceTests(unittest.TestCase):
    def test_price_reference_cannot_claim_verified_historical_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            store=reference.SalesReferenceStore(Path(temp))
            with self.assertRaisesRegex(reference.SalesReferenceError,'HISTORICAL_DELIVERY_NOT_PROVEN'):
                store.initialization_plan(rows(),{'source':'synthetic','observed_at':'2026-09-19T10:00:00','history_unknown':False})
            self.assertIsNone(store.load())

    def test_commit_intent_cannot_be_replaced_after_head_write_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            store=reference.SalesReferenceStore(Path(temp));store.initialize({**plan(store),'approval_reason':'Synthetic explicit review'})
            head=store.load();payload={'batch_id':'receipt-proof','observed_at':'2026-09-19T11:00:00+08:00','observation':{},'components':[{'key':'c1'}],'target_map':{},'attachments':{},'after_reference':rows()}
            store.prepare(payload,head['reference']['digest'])
            receipt={'batch_id':'receipt-proof','content_seal':store.load()['pending']['content_seal'],'status':'provider_accepted','component_keys':['c1'],'accepted_fingerprints':{'c1':'a'*64}}
            ops=__import__(base.TEST_PACKAGE+'.operations',fromlist=['*']);original=ops._atomic
            def fail_head(path,value):
                if path==store.path and value.get('pending') is None:raise OSError('synthetic head write failed')
                return original(path,value)
            with patch.object(ops,'_atomic',side_effect=fail_head),self.assertRaisesRegex(OSError,'synthetic'):
                store.commit('receipt-proof',receipt)
            self.assertEqual(head['reference']['digest'],store.load()['reference']['digest'])
            with self.assertRaisesRegex(reference.SalesReferenceError,'PROOF_RECEIPT_CHANGED'):
                store.commit('receipt-proof',{**receipt,'accepted_fingerprints':{'c1':'b'*64}})
            result=store.commit('receipt-proof',receipt)
            self.assertEqual('batches/receipt-proof.json',result['head']['last_committed']['proof_path'])
            # Even a resealed head cannot attach a different reference to the
            # already sealed delivery/commit evidence.
            changed=copy.deepcopy(result['head']);changed['reference']['rows'][0]['ddp_price']='99.00'
            changed['reference']['digest']=reference._digest(changed['reference']['rows'])
            changed['last_committed']['after_digest']=changed['reference']['digest'];changed['head_seal']=reference._seal(changed)
            ops._atomic(store.path,changed)
            with self.assertRaisesRegex(reference.SalesReferenceError,'COMMITTED_PLAN_MISMATCH'):store.load()

    def test_invalid_after_reference_is_rejected_before_batch_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            store=reference.SalesReferenceStore(Path(temp));store.initialize({**plan(store),'approval_reason':'Synthetic explicit review'})
            head=store.load();future=[{**rows()[0],'snapshot_at':'2026-09-20T12:00:00'}]
            payload={'batch_id':'invalid-after','observed_at':'2026-09-19T11:00:00+08:00','observation':{},'components':[],'target_map':{},'attachments':{},'after_reference':future}
            with self.assertRaisesRegex(reference.SalesReferenceError,'REFERENCE_VALIDATION_FAILED'):
                store.prepare(payload,head['reference']['digest'])
            self.assertIsNone(store.load()['pending']);self.assertFalse((store.root/'batches/invalid-after.json').exists())

    def test_explicit_initialize_dryrun_and_head_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            store = reference.SalesReferenceStore(Path(temp))
            proposed = plan(store)
            self.assertFalse((Path(temp) / "sales_reference" / "head.json").exists())
            self.assertEqual("dryrun", store.dryrun_initialize({**proposed, "approval_reason": "Synthetic explicit review"})["status"])
            with self.assertRaisesRegex(reference.SalesReferenceError, "REVIEW_REQUIRED"):
                store.initialize(proposed)
            initialized = store.initialize({**proposed, "approval_reason": "Synthetic explicit review"})
            self.assertTrue(initialized["history_unknown"])
            self.assertEqual(rows(), initialized["reference"]["rows"])

    def test_prepare_commit_cas_receipt_and_exact_decimal_strings(self):
        with tempfile.TemporaryDirectory() as temp:
            store = reference.SalesReferenceStore(Path(temp)); proposed = plan(store)
            store.initialize({**proposed, "approval_reason": "Synthetic explicit review"})
            head = store.load(); before = head["reference"]["digest"]
            payload = {"batch_id": "b1", "observed_at": "2026-09-19T11:00:00+08:00", "observation": {"source": "synthetic", "before": {"ddp_price": "12.3456"}, "after": {"ddp_price": "13.4567"}}, "components": [{"key": "component-a", "account": "a", "text": "synthetic", "hash": "a"}], "target_map": {"a": {"platform": "synthetic"}}, "attachments": {"HCM.xlsx": {"sha256": "a"}}, "after_reference": [{"id": 2, "goods_id": 1, "dept": "HCM", "customer_grade": "A", "color_label": "Normal", "ddp_price": "13.4567", "currency_no": "VND", "matched_detail_id": 1, "snapshot_at": "2026-09-19T11:00:00"}]}
            store.prepare(payload, before)
            receipt = {"batch_id": "b1", "content_seal": store.load()["pending"]["content_seal"], "status": "provider_accepted", "component_keys": ["component-a"], "accepted_fingerprints": {"component-a": "fingerprint-a"}}
            with self.assertRaisesRegex(reference.SalesReferenceError, "RECEIPT_NOT_ACCEPTED"):
                store.commit("b1", {**receipt, "status": "rejected"})
            committed = store.commit("b1", receipt)
            self.assertEqual("committed", committed["status"])
            self.assertEqual("13.4567", committed["head"]["reference"]["rows"][0]["ddp_price"])
            self.assertEqual("2026-09-19T11:00:00+08:00", committed["head"]["last_committed"]["observed_at"])
            self.assertNotEqual(committed["head"]["last_committed"]["observed_at"], committed["head"]["last_committed"]["committed_at"])
            self.assertEqual("source_observed_at", committed["head"]["reference"]["initialized_at_kind"])

    def test_pending_cas_and_unknown_history_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = reference.SalesReferenceStore(Path(temp)); proposed = plan(store)
            store.initialize({**proposed, "approval_reason": "Synthetic explicit review"})
            payload = {"batch_id": "b1", "observed_at": "2026-09-19T11:00:00+08:00", "observation": {}, "components": [], "target_map": {}, "attachments": {}, "after_reference": [], "changes": []}
            with self.assertRaisesRegex(reference.SalesReferenceError, "CAS_MISMATCH"):
                store.prepare(payload, "0" * 64)
            with self.assertRaisesRegex(reference.SalesReferenceError, "PENDING_BATCH_MISMATCH"):
                store.commit("unknown", {"status": "accepted", "verified": True})

    def test_same_batch_changed_payload_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            store = reference.SalesReferenceStore(Path(temp)); proposed = plan(store)
            store.initialize({**proposed, "approval_reason": "Synthetic explicit review"})
            head = store.load(); payload = {"batch_id": "b1", "observed_at": "2026-09-19T11:00:00+08:00", "observation": {"event_counts": {"legacy_nominal_price_changed": 1}, "ddp_price": "13.4567"}, "components": [{"key": "c1"}], "target_map": {"a": {"platform": "synthetic"}}, "attachments": {}, "after_reference": []}
            store.prepare(payload, head["reference"]["digest"])
            changed = dict(payload); changed["observation"] = {"event_counts": {"legacy_nominal_price_changed": 2}, "ddp_price": "13.4567"}
            with self.assertRaisesRegex(reference.SalesReferenceError, "PENDING_CONTENT_CHANGED"):
                store.prepare(changed, head["reference"]["digest"])

    def test_repeated_commits_keep_provenance_depth_constant(self):
        with tempfile.TemporaryDirectory() as temp:
            store = reference.SalesReferenceStore(Path(temp)); proposed = plan(store)
            store.initialize({**proposed, "approval_reason": "Synthetic explicit review"})
            for index in range(1, 6):
                head = store.load()
                stamp = f"2026-09-19T{10 + index:02d}:00:00+08:00"
                payload = {"batch_id": f"b{index}", "observed_at": stamp, "observation": {"before": {"ddp_price": "12.3456"}, "after": {"ddp_price": f"{12 + index}.3456"}}, "components": [{"key": f"component-{index}"}], "target_map": {"a": {"platform": "synthetic"}}, "attachments": {}, "after_reference": [{"id": index + 1, "goods_id": 1, "dept": "HCM", "customer_grade": "A", "color_label": "Normal", "ddp_price": f"{12 + index}.3456", "currency_no": "VND", "matched_detail_id": 1, "snapshot_at": f"2026-09-19T{10 + index:02d}:00:00"}]}
                store.prepare(payload, head["reference"]["digest"])
                pending = store.load()["pending"]
                receipt = {"batch_id": f"b{index}", "content_seal": pending["content_seal"], "status": "provider_accepted", "component_keys": [f"component-{index}"], "accepted_fingerprints": {f"component-{index}": f"fingerprint-{index}"}}
                store.commit(f"b{index}", receipt)
                provenance = store.load()["reference"]["provenance"]
                self.assertNotIn("initial", provenance.get("initial", {}))

    def test_continuity_default_precision_and_local_exact_precision(self):
        before = [{"id": 1, "goods_id": 1, "dept": "HCM", "customer_grade": "A", "color_label": "Normal", "ddp_price": "12.34", "currency_no": "VND", "matched_detail_id": 1, "snapshot_at": "2026-09-18T10:00:00"}]
        current = [{"goods_id": 1, "goods_no": "G", "dept": "HCM", "goods_name": "G", "customer_grade": "A", "color_label": "Normal", "ddp_price": "12.3456", "currency_no": "VND", "unit": "m", "unit_cuur": "VND/m", "detail_id": 1, "observed_at": "2026-09-19 10:00:00"}]
        default = continuity.plan("sales", before, current, __import__("datetime").datetime.fromisoformat("2026-09-19 10:00:00"))
        self.assertGreater(default["document"]["continuation"]["anomaly_count"], 0)
        exact = continuity.plan("sales", before, current, __import__("datetime").datetime.fromisoformat("2026-09-19 10:00:00"), exact_prices=True)
        self.assertEqual(0, exact["document"]["continuation"]["anomaly_count"])
        self.assertEqual("exact_json", exact["document"]["storage_precision_mode"])
        with self.assertRaisesRegex(ValueError, "SALES_ONLY"):
            continuity.plan("purchase", before, current, __import__("datetime").datetime.fromisoformat("2026-09-19 10:00:00"), exact_prices=True)


if __name__ == "__main__":
    unittest.main()
