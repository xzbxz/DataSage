"""Focused tests for the process-lifetime contract identity guard."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_runtime_immutability_tests"
_spec = importlib.util.spec_from_file_location(
    PACKAGE,
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("unable to load DataSage test package")
plugin = importlib.util.module_from_spec(_spec)
sys.modules[PACKAGE] = plugin
_spec.loader.exec_module(plugin)
contract_store = importlib.import_module(f"{PACKAGE}.contract_store")
runtime_health = importlib.import_module(f"{PACKAGE}.runtime_health")


class _Context:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def get_config(self, key: str, default: object = None) -> object:
        del key
        return default

    def register_tool(self, **kwargs: object) -> None:
        self.registered.append(str(kwargs["name"]))


class RuntimeContractImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        for index, relative_path in enumerate(contract_store.PINNED_CONTRACT_PATHS):
            path = self.root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"version: test-{index}\nvalue: {index}\n",
                encoding="utf-8",
            )
        contract_store.reset_contract_snapshot_for_tests()
        self.root_patch = mock.patch.object(
            contract_store,
            "profile_root",
            return_value=self.root,
        )
        self.root_patch.start()
        self.home_patch = mock.patch.dict(
            os.environ,
            {"HERMES_HOME": str(self.root)},
            clear=False,
        )
        self.home_patch.start()

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.root_patch.stop()
        contract_store.reset_contract_snapshot_for_tests()
        self.temp_dir.cleanup()

    @property
    def query_policy_path(self) -> Path:
        return self.root / "plugins/datasage-query/contracts/query-policy.yaml"

    def test_register_pins_once_before_tool_registration(self) -> None:
        context = _Context()
        with mock.patch.object(
            plugin.contract_store,
            "pin_contract_snapshot",
            wraps=contract_store.pin_contract_snapshot,
        ) as pin:
            plugin.register(context)
        self.assertEqual(1, pin.call_count)
        self.assertEqual(
            [
                "datasage_catalog",
                "datasage_entity_resolve",
                "datasage_query",
            ],
            context.registered,
        )
        self.assertEqual(
            len(contract_store.PINNED_CONTRACT_PATHS),
            contract_store.contract_snapshot_status()["file_count"],
        )

    def test_repeated_reads_use_one_fixed_path_and_digest_snapshot(self) -> None:
        first = contract_store.read_yaml(
            "plugins/datasage-query/contracts/query-policy.yaml"
        )
        second = contract_store.read_yaml(
            "plugins/datasage-query/contracts/query-policy.yaml"
        )
        path, digest = contract_store.content_signature(
            "plugins/datasage-query/contracts/query-policy.yaml"
        )
        status = contract_store.contract_snapshot_status()
        self.assertEqual(first, second)
        self.assertEqual(str(self.query_policy_path.resolve()), path)
        self.assertEqual(
            hashlib.sha256(self.query_policy_path.read_bytes()).hexdigest(),
            digest,
        )
        self.assertTrue(status["fixed"])
        self.assertTrue(status["enforced_per_read"])
        self.assertFalse(status["drifted"])
        self.assertEqual(len(contract_store.PINNED_CONTRACT_PATHS), status["file_count"])
        self.assertEqual(1, contract_store._SNAPSHOT_BOOTSTRAP_COUNT)

    def test_content_change_is_latched_and_restoration_cannot_reenable_reads(self) -> None:
        original = self.query_policy_path.read_bytes()
        contract_store.read_yaml(
            "plugins/datasage-query/contracts/query-policy.yaml"
        )
        self.query_policy_path.write_bytes(b"version: changed\nvalue: changed\n")
        with self.assertRaises(contract_store.ContractStoreError) as changed:
            contract_store.read_yaml(
                "plugins/datasage-query/contracts/query-policy.yaml"
            )
        self.assertEqual("CONTRACT_UNAVAILABLE", changed.exception.code)
        self.assertEqual("contract_content_changed", changed.exception.reason_code)
        self.query_policy_path.write_bytes(original)
        with self.assertRaises(contract_store.ContractStoreError) as restored:
            contract_store.content_signature(
                "plugins/datasage-query/contracts/query-policy.yaml"
            )
        self.assertEqual("contract_content_changed", restored.exception.reason_code)
        status = contract_store.contract_snapshot_status()
        self.assertTrue(status["fixed"])
        self.assertTrue(status["drifted"])
        self.assertEqual("contract_content_changed", status["drift_reason"])

    def test_root_switch_is_rejected_even_when_the_file_matches(self) -> None:
        contract_store.read_yaml(
            "plugins/datasage-query/contracts/query-policy.yaml"
        )
        other = Path(self.temp_dir.name) / "other-profile"
        other_path = other / "plugins/datasage-query/contracts/query-policy.yaml"
        other_path.parent.mkdir(parents=True, exist_ok=True)
        other_path.write_bytes(self.query_policy_path.read_bytes())
        with mock.patch.object(contract_store, "profile_root", return_value=other):
            with self.assertRaises(contract_store.ContractStoreError) as switched:
                contract_store.read_yaml(
                    "plugins/datasage-query/contracts/query-policy.yaml"
                )
        self.assertEqual("CONTRACT_UNAVAILABLE", switched.exception.code)
        self.assertEqual("profile_root_changed", switched.exception.reason_code)

    def test_yaml_cache_cannot_bypass_per_read_verification(self) -> None:
        relative_path = "plugins/datasage-query/contracts/query-policy.yaml"
        first = contract_store.read_yaml(relative_path)
        original_text = self.query_policy_path.read_text(encoding="utf-8")
        original_digest = hashlib.sha256(
            original_text.encode("utf-8")
        ).hexdigest()
        self.query_policy_path.write_text(
            "version: changed\nvalue: changed\n",
            encoding="utf-8",
        )
        # The parser cache can still return an old value when called directly;
        # the file-facing API must not trust that cache entry.
        cached = contract_store.parse_yaml_cached(
            str(self.query_policy_path.resolve()),
            original_digest,
            original_text,
        )
        self.assertEqual(first, cached)
        with self.assertRaises(contract_store.ContractStoreError):
            contract_store.read_yaml(relative_path)

    def test_runtime_identity_status_reports_snapshot_truthfully(self) -> None:
        with mock.patch.object(runtime_health.settings, "get_list", return_value=[]):
            cold = runtime_health.runtime_identity_status(profile_root=self.root)
        self.assertFalse(cold["ready"])
        self.assertFalse(cold["contract_snapshot_fixed"])
        self.assertFalse(cold["contract_snapshot_enforced_per_read"])
        self.assertEqual("CONTRACT_SNAPSHOT_UNAVAILABLE", cold["reason_code"])

        contract_store.pin_contract_snapshot()
        with mock.patch.object(runtime_health.settings, "get_list", return_value=[]):
            clean = runtime_health.runtime_identity_status(profile_root=self.root)
        self.assertTrue(clean["ready"])
        self.assertTrue(clean["contract_snapshot_fixed"])
        self.assertTrue(clean["contract_snapshot_enforced_per_read"])
        self.assertIsInstance(clean["contract_manifest_digest"], str)
        self.assertIsNone(clean["contract_snapshot_drift_reason"])
        self.assertFalse(clean["git_binding"]["available"])
        self.assertFalse(clean["release_binding"]["enforced_per_query"])

        self.query_policy_path.write_text(
            "version: changed\nvalue: changed\n",
            encoding="utf-8",
        )
        with self.assertRaises(contract_store.ContractStoreError):
            contract_store.read_yaml(
                "plugins/datasage-query/contracts/query-policy.yaml"
            )
        with mock.patch.object(runtime_health.settings, "get_list", return_value=[]):
            drifted = runtime_health.runtime_identity_status(profile_root=self.root)
        self.assertFalse(drifted["ready"])
        self.assertEqual("contract_snapshot_integrity", drifted["state"])
        self.assertEqual("CONTRACT_SNAPSHOT_DRIFT", drifted["reason_code"])
        self.assertEqual("contract_content_changed", drifted["contract_snapshot_drift_reason"])

    def test_concurrent_initialization_publishes_one_snapshot(self) -> None:
        relative_path = "plugins/datasage-query/contracts/query-policy.yaml"
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _item: contract_store.read_yaml(relative_path), range(32)))
        self.assertEqual([values[0]] * len(values), values)
        self.assertEqual(1, contract_store._SNAPSHOT_BOOTSTRAP_COUNT)


if __name__ == "__main__":
    unittest.main()
