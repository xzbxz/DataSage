import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_business_contracts as base


batch = __import__(base.TEST_PACKAGE + ".idk_batch", fromlist=["*"])


def content(week="2026-W38", zero=False):
    return json.dumps({
        "week": week,
        "observation": {"observed_at": "2026-09-19T11:00:00+08:00", "source_rows": 0 if zero else 2},
        "parts": [] if zero else [{"region": "IDK", "text": "synthetic IDK body", "observed_to": "2026-09-19T10:59:00+08:00"}],
        "accounts": ["idk-a"],
        "target_map": {"idk-a": {"platform": "synthetic", "target_ref": "idk-a"}},
        "summary": {"zero": zero},
    }, ensure_ascii=False)


class IdkBatchTests(unittest.TestCase):
    def args(self, zero=False):
        return {
            "content": content(zero=zero),
            "observed_at": "2026-09-19T11:00:00+08:00",
            "logical_accounts": ["idk-a"],
            "target_map": {"idk-a": {"platform": "synthetic", "target_ref": "idk-a"}},
            "evidence": {"source_snapshot": "synthetic-idk-snapshot", "complete": True},
            "zero": zero,
        }

    def test_first_seal_load_reuse_and_zero_week(self):
        with tempfile.TemporaryDirectory() as temp:
            store = batch.IdkBatchStore(Path(temp), "2026-W38")
            first = store.prepare(**self.args())
            self.assertEqual("sealed", first["status"])
            self.assertEqual("sealed", store.load()["status"])
            reused = store.prepare(**{**self.args(), "target_map": {"idk-a": {"changed": True}}})
            self.assertEqual("reused", reused["status"])
            with tempfile.TemporaryDirectory() as zero_temp:
                zero_store = batch.IdkBatchStore(Path(zero_temp), "2026-W39")
                zero = zero_store.prepare(**self.args(zero=True))
                self.assertTrue(zero["manifest"]["zero"])

    def test_content_or_manifest_tamper_blocks_load(self):
        with tempfile.TemporaryDirectory() as temp:
            store = batch.IdkBatchStore(Path(temp), "2026-W38")
            store.prepare(**self.args())
            (store.directory / "content.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(batch.IdkBatchError, "HASH_MISMATCH"):
                store.load()

    def test_load_envelope_rechecks_content_after_manifest_load(self):
        with tempfile.TemporaryDirectory() as temp:
            store = batch.IdkBatchStore(Path(temp), "2026-W38")
            store.prepare(**self.args())
            original = batch._read
            content_reads = 0
            def changed_second_read(path, code):
                nonlocal content_reads
                value = original(path, code)
                if path == store.content_path:
                    content_reads += 1
                    if content_reads == 2:
                        return b"{}"
                return value
            with patch.object(batch, "_read", side_effect=changed_second_read):
                with self.assertRaisesRegex(batch.IdkBatchError, "HASH_MISMATCH"):
                    store.load_envelope()

    def test_manifest_bool_bytes_hash_and_content_shape_are_strict(self):
        cases = [
            ("zero", 1, "SCOPE_INVALID"),
            ("content_bytes", True, "CONTENT_RECORD_INVALID"),
            ("content_hash", "bad", "CONTENT_RECORD_INVALID"),
            ("content_shape", [], "CONTENT_RECORD_INVALID"),
        ]
        for field, value, error in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                store = batch.IdkBatchStore(Path(temp), "2026-W38")
                store.prepare(**self.args())
                manifest_path = store.manifest_path
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if field == "content_shape":
                    manifest["content"] = []
                elif field == "content_bytes":
                    manifest["content"]["bytes"] = value
                elif field == "content_hash":
                    manifest["content"]["sha256"] = value
                else:
                    manifest[field] = value
                manifest["metadata_seal"] = batch._seal(manifest)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(batch.IdkBatchError, error):
                    store.load()

    def test_old_progress_without_batch_is_blocked_and_no_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            store = batch.IdkBatchStore(Path(temp), "2026-W38")
            progress = {"data": {"components": {"key": {"status": "provider_accepted"}}}}
            with self.assertRaisesRegex(batch.IdkBatchError, "PROGRESS_WITHOUT_BATCH"):
                store.prepare(**{**self.args(), "progress": progress})
            self.assertFalse(store.exists())

    def test_missing_or_incomplete_directory_blocks_without_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            store = batch.IdkBatchStore(Path(temp), "2026-W38")
            store.directory.mkdir(parents=True)
            with self.assertRaisesRegex(batch.IdkBatchError, "INCOMPLETE_BATCH_BLOCKED"):
                store.prepare(**self.args())

    def test_bad_scope_targets_and_observation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = batch.IdkBatchStore(Path(temp), "2026-W38")
            with self.assertRaisesRegex(batch.IdkBatchError, "TARGET_MAP_ACCOUNT_MISMATCH"):
                store.prepare(**{**self.args(), "target_map": {"other": {"platform": "synthetic"}}})
            with self.assertRaisesRegex(batch.IdkBatchError, "OBSERVED_AT_INVALID"):
                store.prepare(**{**self.args(), "observed_at": "not-a-time"})


if __name__ == "__main__":
    unittest.main()
