"""Focused tests for the per-metric capability-receipt cache."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Lock
import types
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_receipt_cache_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

capability_contract = importlib.import_module(f"{PACKAGE}.capability_contract")
receipt_cache = importlib.import_module(f"{PACKAGE}.receipt_cache")


class MetricCapabilityReceiptCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        receipt_cache.clear_metric_capability_receipt_cache()
        receipt_cache.contract_store.reset_contract_snapshot_for_tests()
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        relative_paths = {
            "plugins/datasage-query/contracts/datasets.yaml",
            "plugins/datasage-query/contracts/entity-registry.yaml",
            "plugins/datasage-query/contracts/metric-governance.yaml",
            capability_contract.QUERY_POLICY_PATH,
            capability_contract.TARGET_GAP_CONTRACT_PATH,
            *(
                source["semantics"]
                for source in capability_contract.DOMAIN_SOURCES.values()
            ),
        }
        self.paths = {
            relative_path: root / f"contract-{index}.yaml"
            for index, relative_path in enumerate(sorted(relative_paths))
        }
        for relative_path, path in self.paths.items():
            path.write_text(f"path: {relative_path}\n", encoding="utf-8")
        self.trusted_path = mock.patch.object(
            receipt_cache.contract_store,
            "trusted_path",
            side_effect=lambda relative_path: self.paths[relative_path],
        )
        self.trusted_path.start()

    def tearDown(self) -> None:
        self.trusted_path.stop()
        receipt_cache.clear_metric_capability_receipt_cache()
        receipt_cache.contract_store.reset_contract_snapshot_for_tests()
        self.temp_dir.cleanup()

    def test_same_signature_builds_once_and_return_values_are_isolated(self) -> None:
        calls = 0

        def builder(domain: str, metric: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"domain": domain, "metric": metric, "facts": ["current"]}

        first = receipt_cache.get_metric_capability_receipt(
            "delivery", "gross_delivery", builder=builder
        )
        first["facts"].append("caller mutation")
        second = receipt_cache.get_metric_capability_receipt(
            "delivery", "gross_delivery", builder=builder
        )

        self.assertEqual(1, calls)
        self.assertEqual(["current"], second["facts"])
        self.assertIsNot(first, second)

    def test_changed_domain_semantics_latches_contract_drift(self) -> None:
        calls = 0

        def builder(_domain: str, _metric: str) -> str:
            nonlocal calls
            calls += 1
            return f"receipt-{calls}"

        first = receipt_cache.get_metric_capability_receipt(
            "delivery", "gross_delivery", builder=builder
        )
        semantics = self.paths[
            capability_contract.DOMAIN_SOURCES["delivery"]["semantics"]
        ]
        semantics.write_text("changed: true\n", encoding="utf-8")
        self.assertEqual("receipt-1", first)
        with self.assertRaises(receipt_cache.contract_store.ContractStoreError):
            receipt_cache.get_metric_capability_receipt(
                "delivery", "gross_delivery", builder=builder
            )
        self.assertEqual(1, calls)

    def test_same_size_same_mtime_content_change_is_rejected(self) -> None:
        calls = 0

        def builder(_domain: str, _metric: str) -> int:
            nonlocal calls
            calls += 1
            return calls

        self.assertEqual(
            1,
            receipt_cache.get_metric_capability_receipt(
                "delivery", "gross_delivery", builder=builder
            ),
        )
        semantics = self.paths[
            capability_contract.DOMAIN_SOURCES["delivery"]["semantics"]
        ]
        before = semantics.stat()
        original = semantics.read_bytes()
        replacement = bytes([original[0] ^ 1]) + original[1:]
        semantics.write_bytes(replacement)
        os.utime(
            semantics,
            ns=(before.st_atime_ns, before.st_mtime_ns),
        )

        with self.assertRaises(receipt_cache.contract_store.ContractStoreError):
            receipt_cache.get_metric_capability_receipt(
                "delivery", "gross_delivery", builder=builder
            )
        self.assertEqual(1, calls)

    def test_yaml_cache_cannot_hide_contract_drift(self) -> None:
        relative_path = capability_contract.QUERY_POLICY_PATH
        path = self.paths[relative_path]
        path.write_text("value: one\n", encoding="utf-8")
        receipt_cache.contract_store.parse_yaml_cached.cache_clear()
        first = receipt_cache.contract_store.read_yaml(relative_path)
        before = path.stat()
        path.write_text("value: two\n", encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual({"value": "one"}, first)
        with self.assertRaises(receipt_cache.contract_store.ContractStoreError):
            receipt_cache.contract_store.read_yaml(relative_path)

    def test_each_common_contract_change_is_rejected(self) -> None:
        for changed_path in (
            "plugins/datasage-query/contracts/datasets.yaml",
            capability_contract.QUERY_POLICY_PATH,
        ):
            with self.subTest(changed_path=changed_path):
                receipt_cache.clear_metric_capability_receipt_cache()
                receipt_cache.contract_store.reset_contract_snapshot_for_tests()
                calls = 0

                def builder(_domain: str, _metric: str) -> int:
                    nonlocal calls
                    calls += 1
                    return calls

                self.assertEqual(
                    1,
                    receipt_cache.get_metric_capability_receipt(
                        "receivable", "balance", builder=builder
                    ),
                )
                path = self.paths[changed_path]
                path.write_text(
                    path.read_text(encoding="utf-8") + "changed: true\n",
                    encoding="utf-8",
                )
                with self.assertRaises(
                    receipt_cache.contract_store.ContractStoreError
                ):
                    receipt_cache.get_metric_capability_receipt(
                        "receivable", "balance", builder=builder
                    )
                self.assertEqual(1, calls)

    def test_builder_failure_is_not_cached(self) -> None:
        calls = 0

        def builder(_domain: str, _metric: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("catalog unavailable")
            return "recovered"

        with self.assertRaisesRegex(RuntimeError, "catalog unavailable"):
            receipt_cache.get_metric_capability_receipt(
                "receipt", "net_receipt", builder=builder
            )
        self.assertEqual(
            "recovered",
            receipt_cache.get_metric_capability_receipt(
                "receipt", "net_receipt", builder=builder
            ),
        )
        self.assertEqual(2, calls)

    def test_target_gap_change_latches_the_process_snapshot(self) -> None:
        calls: dict[str, int] = {"delivery": 0, "target": 0}

        def builder(domain: str, _metric: str) -> str:
            calls[domain] += 1
            return f"{domain}-{calls[domain]}"

        for domain in ("delivery", "target"):
            receipt_cache.get_metric_capability_receipt(
                domain, "metric", builder=builder
            )
        self.paths[capability_contract.TARGET_GAP_CONTRACT_PATH].write_text(
            "changed: target-gap\n", encoding="utf-8"
        )
        # The changed target-only file is detected when the target path is
        # read; that detection latches the process so unrelated reads then
        # fail closed as well.
        for domain in ("target", "delivery"):
            with self.subTest(domain=domain), self.assertRaises(
                receipt_cache.contract_store.ContractStoreError
            ):
                receipt_cache.get_metric_capability_receipt(
                    domain, "metric", builder=builder
                )
        self.assertEqual({"delivery": 1, "target": 1}, calls)

    def test_clear_forces_rebuild(self) -> None:
        calls = 0

        def builder(_domain: str, _metric: str) -> int:
            nonlocal calls
            calls += 1
            return calls

        self.assertEqual(
            1,
            receipt_cache.get_metric_capability_receipt(
                "inventory", "stock", builder=builder
            ),
        )
        receipt_cache.clear_metric_capability_receipt_cache()
        self.assertEqual(
            2,
            receipt_cache.get_metric_capability_receipt(
                "inventory", "stock", builder=builder
            ),
        )

    def test_concurrent_same_signature_builds_once(self) -> None:
        calls = 0
        calls_lock = Lock()
        started = Event()
        release = Event()

        def builder(_domain: str, _metric: str) -> str:
            nonlocal calls
            with calls_lock:
                calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return "receipt"

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(
                    receipt_cache.get_metric_capability_receipt,
                    "customer_risk",
                    "risk_metric",
                    builder=builder,
                )
                for _ in range(4)
            ]
            self.assertTrue(started.wait(timeout=5))
            release.set()
            self.assertEqual(["receipt"] * 4, [future.result() for future in futures])

        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
