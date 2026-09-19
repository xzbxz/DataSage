import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_business_contracts as base


batch = __import__(base.TEST_PACKAGE + ".slow_report_batch", fromlist=["*"])


def inputs():
    return {
        "body": "synthetic weekly report body",
        "attachments": {"HCM_weekly.xlsx": b"PK-synthetic-xlsx", "IDK_weekly.xlsx": b"PK-synthetic-idk"},
        "logical_accounts": ["sales-a", "manager-a"],
        "target_map": {
            "sales-a": {"platform": "synthetic", "target_ref": "target-a"},
            "manager-a": {"platform": "synthetic", "target_ref": "target-m"},
        },
        "observed_at": "2026-09-19T10:00:00+08:00",
        "regions": ["HCM", "IDK"],
        "evidence": {"source_snapshot": "synthetic-snapshot", "query_complete": True},
    }


class SlowReportBatchTests(unittest.TestCase):
    def test_prepare_load_publish_and_reuse_without_current_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = inputs()
            prepared = batch.prepare_batch(root, "2026-W38", "weekly", 0, **value)
            self.assertEqual("prepared", prepared["status"])
            loaded = batch.load_batch(root, "2026-W38", "weekly", 0)
            self.assertEqual("prepared", loaded["status"])
            # Reuse reads sealed target_map from the manifest; no current role input exists here.
            reused = batch.prepare_batch(root, "2026-W38", "weekly", 0, **{**value, "target_map": {"sales-a": {"changed": True}, "manager-a": {"changed": True}}})
            self.assertEqual("reused", reused["status"])
            published = batch.publish_batch(root, "2026-W38", "weekly", 0, published_at="2026-09-19T10:05:00+08:00")
            self.assertEqual("published", published["status"])
            self.assertEqual("published", batch.load_batch(root, "2026-W38", "weekly", 0)["status"])

    def test_tamper_and_shape_errors_block_load(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch.prepare_batch(root, "2026-W38", "weekly", 0, **inputs())
            attachment = root / "2026-W38" / "weekly" / "g0" / "attachments" / "HCM_weekly.xlsx"
            attachment.write_bytes(b"tampered")
            with self.assertRaisesRegex(batch.BatchError, "HASH_MISMATCH"):
                batch.load_batch(root, "2026-W38", "weekly", 0)

    def test_invalid_prepare_is_retryable_and_incomplete_cannot_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = inputs()
            with self.assertRaisesRegex(batch.BatchError, "ATTACHMENT_NAME_INVALID"):
                batch.prepare_batch(root, "2026-W38", "weekly", 0, **{**value, "attachments": {"bad/name.xlsx": b"x"}})
            self.assertFalse((root / "2026-W38").exists())
            batch.prepare_batch(root, "2026-W38", "weekly", 0, **value)
            manifest = root / "2026-W38" / "weekly" / "g0" / "manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["status"] = "incomplete"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(batch.BatchError, "MANIFEST_SHAPE_INVALID"):
                batch.publish_batch(root, "2026-W38", "weekly", 0)

    def test_unsealed_directory_is_blocked_without_destroying_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orphan = root / "2026-W38" / "weekly" / "g0"
            orphan.mkdir(parents=True)
            value = inputs()
            with self.assertRaisesRegex(batch.BatchError, "EXPLICIT_RETRY"):
                batch.prepare_batch(root, "2026-W38", "weekly", 0, **value)
            with self.assertRaisesRegex(batch.BatchError, "EXPLICIT_RETRY"):
                batch.prepare_batch(root, "2026-W38", "weekly", 0, **value, retry=True)

    def test_batch_store_head_phase_and_delivery_generations(self):
        with tempfile.TemporaryDirectory() as temp:
            store = batch.BatchStore(Path(temp), "2026-W38")
            head = store.ensure_head(month="2026-09", observed_at="2026-09-19T10:00:00+08:00")
            self.assertEqual(0, head["content_generation"])
            value = inputs()
            prepared = store.prepare_phase("weekly", **value)
            self.assertEqual("prepared", prepared["status"])
            published = store.publish_phase("weekly", published_at="2026-09-19T10:01:00+08:00")
            self.assertEqual("published", published["status"])
            self.assertEqual("published", store.load_phase("weekly")["status"])
            self.assertEqual(1, store.new_delivery_generation(reason="Synthetic resend review")["delivery_generation"])
            self.assertEqual(1, store.new_content_generation(month="2026-09", reason="Synthetic data correction")["content_generation"])
            prepared_new = store.prepare_phase("weekly", **value)
            self.assertEqual("prepared", prepared_new["status"])

    def test_new_generation_requires_explicit_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = inputs()
            batch.prepare_batch(root, "2026-W38", "weekly", 0, **value)
            with self.assertRaisesRegex(batch.BatchError, "REGENERATE_REASON_REQUIRED"):
                batch.prepare_batch(root, "2026-W38", "weekly", 1, **value)
            regenerated = batch.prepare_batch(root, "2026-W38", "weekly", 1, **value, regenerate=True, reason="Synthetic correction")
            self.assertEqual("prepared", regenerated["status"])
            self.assertEqual("Synthetic correction", regenerated["manifest"]["regeneration_reason"])

    def test_sent_or_unknown_without_archive_cannot_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(batch.BatchError, "DELIVERY_STATE_REQUIRES_ARCHIVE"):
                batch.prepare_batch(Path(temp), "2026-W38", "weekly", 0, **{**inputs(), "evidence": {"delivery_state": "unknown"}})

    def test_period_and_phase_shape_are_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = inputs()
            with self.assertRaisesRegex(batch.BatchError, "WEEK_INVALID"):
                batch.prepare_batch(root, "2026-W99", "weekly", 0, **value)
            with self.assertRaisesRegex(batch.BatchError, "PHASE_INVALID"):
                batch.prepare_batch(root, "2026-W38", "daily", 0, **value)
            with self.assertRaisesRegex(batch.BatchError, "MONTH_INVALID"):
                batch.prepare_batch(root, "2026-W38", "weekly", 0, **{**value, "month": "2026-13"})

    def test_publish_head_failure_recovers_without_rewriting_sealed_business_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = batch.BatchStore(root, "2026-W38")
            store.ensure_head(month="2026-09", observed_at="2026-09-19T10:00:00+08:00")
            store.prepare_phase("weekly", **inputs())
            directory = batch.batch_path(root, "2026-W38", "weekly", 0)
            business_paths = [directory / "body.txt", *(directory / "attachments").glob("*.xlsx")]
            original_atomic = batch._atomic_json
            def fail_head(path, value):
                if Path(path) == store.head_path:
                    raise RuntimeError("synthetic head write failure")
                return original_atomic(path, value)
            with patch.object(batch, "_atomic_json", side_effect=fail_head):
                with self.assertRaisesRegex(RuntimeError, "head write"):
                    store.publish_phase("weekly", published_at="2026-09-19T10:01:00+08:00")
            manifest_path = directory / "manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            business_bytes = [path.read_bytes() for path in business_paths]
            self.assertEqual("published", batch.load_batch(root, "2026-W38", "weekly", 0)["status"])
            self.assertEqual("prepared", store.head()["phases"]["weekly"]["status"])
            store.publish_phase("weekly", published_at="2026-09-19T10:01:00+08:00")
            self.assertEqual("published", store.head()["phases"]["weekly"]["status"])
            self.assertEqual(manifest_bytes, manifest_path.read_bytes())
            self.assertEqual(business_bytes, [path.read_bytes() for path in business_paths])


if __name__ == "__main__":
    unittest.main()
