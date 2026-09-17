"""Independent structural acceptance for the isolated Profile candidate.

This file deliberately owns its fixtures and adapters.  It does not import
another Profile test, use a Codex output fixture, or call a real database,
model, scheduler, or delivery endpoint.  The assertions describe the
acceptance boundary: an omitted package, an unverified/truncated export, or a
cross-target receipt must not be promoted to a complete result.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import zipfile
from unittest.mock import patch

from role_config_fixture import REGIONS, legacy_document, public_document


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

PACKAGE = "profile_convergence_structural_acceptance"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)


def _module(name: str):
    return importlib.import_module(f"{PACKAGE}.{name}")


legacy = _module("legacy_workflow")
operations = _module("operations")
workflow_io = _module("workflow_io")
workflow_cycle = _module("workflow_cycle")
customer_audit = _module("workflow_customer_audit")
delivery_review = _module("workflow_delivery_review")
acceptance_delivery = _module("acceptance_delivery")
fabric_report = _module("fabric_report")
wire = _module("wire")
local_report = _module("local_report")
roles = _module("workflow_roles")


def _baseline() -> list[dict[str, object]]:
    return [
        {
            "whse_dept": "HCM",
            "goods_no": "G-1",
            "attr_val": "Red",
            "total_piece": "10",
        },
        {
            "whse_dept": "HCM",
            "goods_no": "G-2",
            "attr_val": "Blue",
            "total_piece": "20",
        },
    ]


def _mapping(*, missing_customer_number: bool = False) -> dict[str, object]:
    return {
        "productsByCustomer": {
            "customer-a": [{"goods_no": "G-1", "whse_dept": "HCM"}],
            "customer-b": [{"goods_no": "G-2", "whse_dept": "HCM"}],
        },
        "customerInfo": {
            "customer-a": {
                "customer_no": "C-001",
                "name": "同名客户",
                "sales": "销售甲",
            },
            "customer-b": {
                "customer_no": "" if missing_customer_number else "C-002",
                "name": "同名客户",
                "sales": "销售乙",
            },
        },
        "wecomBySales": {"销售甲": "account-a", "销售乙": "account-b"},
        "employeeBySales": {
            "销售甲": {"wecom_account": "account-a", "region": "HCM"},
            "销售乙": {"wecom_account": "account-b", "region": "HCM"},
        },
    }


def _complete_package_plan() -> dict[str, object]:
    return {
        "sales_packages": [
            {
                "account": "account-a",
                "region": "HCM",
                "sales_name": "销售甲",
                "sales_names": ["销售甲"],
                "customers": [
                    {
                        "customer_id": "customer-a",
                        "customer_no": "C-001",
                        "customer_name": "同名客户",
                        "products": [["G-1", "Red", 10]],
                    }
                ],
            },
            {
                "account": "account-b",
                "region": "HCM",
                "sales_name": "销售乙",
                "sales_names": ["销售乙"],
                "customers": [
                    {
                        "customer_id": "customer-b",
                        "customer_no": "C-002",
                        "customer_name": "同名客户",
                        "products": [["G-2", "Blue", 20]],
                    }
                ],
            },
        ],
        # This is independently authored expected evidence for the two
        # baseline-to-customer relations.  The test intentionally does not
        # obtain it from contact_plan(), so verify_plan cannot self-justify.
        "audit_rows": [
            {
                "product_dept": "HCM",
                "goods_no": "G-1",
                "color": "Red",
                "customer_no": "C-001",
                "customer_name": "同名客户",
                "sales_owner": "销售甲",
                "account": "account-a",
                "exception": "",
            },
            {
                "product_dept": "HCM",
                "goods_no": "G-2",
                "color": "Blue",
                "customer_no": "C-002",
                "customer_name": "同名客户",
                "sales_owner": "销售乙",
                "account": "account-b",
                "exception": "",
            },
        ],
    }


class StructuralCustomerPackageTests(unittest.TestCase):
    def test_complete_plan_is_accepted_and_each_identity_is_retained(self):
        result = customer_audit.verify_plan(
            _baseline(), _mapping(), _complete_package_plan()
        )
        self.assertEqual(2, result["customer_cards"])
        self.assertEqual(2, result["owners"])

    def test_omitted_package_is_rejected_by_independent_population_check(self):
        baseline = _baseline()
        mapping = _mapping()
        plan = _complete_package_plan()

        # Derive the expected owner set from the reviewed mapping in this test,
        # independently of verify_plan's iteration over supplied packages.
        expected_accounts = {
            mapping["wecomBySales"][info["sales"]]
            for info in mapping["customerInfo"].values()
        }
        reduced = copy.deepcopy(plan)
        reduced["sales_packages"].pop()
        supplied_accounts = {p["account"] for p in reduced["sales_packages"]}
        self.assertNotEqual(expected_accounts, supplied_accounts)

        # A package audit must fail closed when a mapped eligible owner has no
        # package.  This is the counterexample that the old validator accepted.
        with self.assertRaises(ValueError):
            customer_audit.verify_plan(baseline, mapping, reduced)

    def test_extra_empty_package_is_rejected_by_independent_population_check(self):
        baseline = _baseline()
        mapping = _mapping()
        plan = _complete_package_plan()
        plan["sales_packages"].append(
            {"account": "unmapped-extra", "region": "HCM", "customers": []}
        )
        expected_accounts = set(mapping["wecomBySales"].values())
        self.assertNotIn("unmapped-extra", expected_accounts)

        with self.assertRaises(ValueError):
            customer_audit.verify_plan(baseline, mapping, plan)

    def test_same_name_and_same_amount_keep_distinct_ids_and_missing_number_is_audited(self):
        baseline = [
            {
                "whse_dept": "HCM",
                "goods_no": "G-1",
                "attr_val": "Red",
                "total_piece": "10",
            }
        ]
        mapping = copy.deepcopy(_mapping())
        mapping["productsByCustomer"] = {
            "customer-a": [{"goods_no": "G-1", "whse_dept": "HCM"}],
            "customer-b": [{"goods_no": "G-1", "whse_dept": "HCM"}],
        }
        complete = legacy.contact_plan(baseline, mapping)
        cards = [
            card
            for package in complete["sales_packages"]
            for card in package["customers"]
        ]
        self.assertEqual({"C-001", "C-002"}, {card["customer_no"] for card in cards})
        self.assertEqual({"同名客户"}, {card["customer_name"] for card in cards})
        self.assertEqual({10}, {card["products"][0][2] for card in cards})

        missing_mapping = copy.deepcopy(_mapping(missing_customer_number=True))
        missing_mapping["productsByCustomer"]["customer-b"] = [
            {"goods_no": "G-1", "whse_dept": "HCM"}
        ]
        missing = legacy.contact_plan(baseline, missing_mapping)
        missing_rows = [
            row
            for row in missing["audit_rows"]
            if row.get("sales_owner") == "销售乙"
        ]
        self.assertEqual(1, len(missing_rows))
        self.assertEqual("Missing Customer No", missing_rows[0]["exception"])
        self.assertEqual("", missing_rows[0]["customer_no"])
        self.assertNotIn(
            "customer-b",
            {
                card["customer_id"]
                for package in missing["sales_packages"]
                for card in package["customers"]
            },
        )

    def test_week_and_generation_must_match_before_reusing_a_cached_plan(self):
        existing = {"version": 4, "week": "2026-W38", "baseline_digest": "digest-a"}
        self.assertEqual(
            "reuse_frozen_customer_plan",
            legacy.customer_plan_decision(
                "2026-W38", "digest-a", existing, preview=False
            ),
        )
        self.assertEqual(
            "build_for_preview",
            legacy.customer_plan_decision(
                "2026-W38", "digest-a", existing, preview=True
            ),
        )
        self.assertEqual(
            "rebuild_for_refreeze",
            legacy.customer_plan_decision(
                "2026-W38", "digest-a", existing, rebuild=True, preview=False
            ),
        )
        for wrong in (
            {"version": 3, "week": "2026-W38", "baseline_digest": "digest-a"},
            {"version": 4, "week": "2026-W39", "baseline_digest": "digest-a"},
            {"version": 4, "week": "2026-W38", "baseline_digest": "digest-b"},
        ):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                legacy.customer_plan_decision(
                    "2026-W38", "digest-a", wrong, preview=False
                )

        class Store:
            def rows(self, *_args):
                return [
                    {
                        "cycle_id": "customer-sample-batch-v1",
                        "payload": {"selected": [{"week": "2026-W38"}]},
                    }
                ]

        self.assertIsNotNone(customer_audit.batch_record(Store(), "2026-W38")[0])
        self.assertEqual(
            (None, "customer-sample-2026-w39-v1"),
            customer_audit.batch_record(Store(), "2026-W39"),
        )

        class GenerationStore:
            def rows(self, *_args):
                return [
                    {
                        "cycle_id": "ca-hcm-g1",
                        "payload": {
                            "summary": {
                                "week": "2026-W38",
                                "department": "HCM",
                                "generation": 1,
                            }
                        },
                    },
                    {
                        "cycle_id": "ca-hcm-g2",
                        "payload": {
                            "summary": {
                                "week": "2026-W38",
                                "department": "HCM",
                                "generation": 2,
                            }
                        },
                    },
                ]

        with patch.object(customer_audit.live, "DEPARTMENTS", ("HCM",)):
            current, history = customer_audit._current_audit_records(
                GenerationStore(), "2026-W38"
            )
        self.assertEqual([2], [item["generation"] for item in current])
        self.assertEqual([1], [item["generation"] for item in history])


class StructuralDataQualityTests(unittest.TestCase):
    @staticmethod
    def _purchase_row(**overrides):
        row = {
            "goods_no": "G-1",
            "color_label": "Red",
            "supplier_no": "SUP-1",
            "supplier_name": "Synthetic Supplier",
            "tax_inclue_price": "12.00",
            "tax_exclue_price": "10.00",
            "currency_no": "CNY",
            "unit_cuur": "m",
            "effective_date": "2026-01-01",
            "expiration_date": "2027-01-01",
            "gmt_modified": "2026-09-17 09:00:00",
            "detail_id": 1,
        }
        row.update(overrides)
        return row

    def test_null_and_empty_currency_are_unknown_basis_not_zero(self):
        observed = operations.classify_prices(
            "purchase",
            [self._purchase_row()],
            operations.datetime.fromisoformat("2026-09-17 10:00:00"),
        )
        for value in (None, ""):
            with self.subTest(currency=value):
                row = operations.classify_prices(
                    "purchase",
                    [self._purchase_row(currency_no=value)],
                    operations.datetime.fromisoformat("2026-09-17 10:00:00"),
                )[0]
                self.assertEqual("unknown_basis", row["state"])
                self.assertNotEqual("0", row["prices"]["tax_inclue_price"])
                self.assertEqual(
                    "unresolved",
                    operations.compare(observed, [row])[0]["event"],
                )

    def test_null_and_empty_amount_are_missing_price_not_zero_or_change(self):
        observed = operations.classify_prices(
            "purchase",
            [self._purchase_row()],
            operations.datetime.fromisoformat("2026-09-17 10:00:00"),
        )
        for value in (None, ""):
            with self.subTest(amount=value):
                row = operations.classify_prices(
                    "purchase",
                    [self._purchase_row(tax_inclue_price=value)],
                    operations.datetime.fromisoformat("2026-09-17 10:00:00"),
                )[0]
                self.assertEqual("missing_price", row["state"])
                self.assertIsNone(row["prices"]["tax_inclue_price"])
                self.assertEqual(
                    "unresolved",
                    operations.compare(observed, [row])[0]["event"],
                )


class StructuralPreviewAndConfigurationTests(unittest.TestCase):
    def test_synthetic_preview_is_explicitly_not_sent(self):
        data = {
            "evidence_origin": "synthetic",
            "region": "IDK",
            "observed_at": "2026-09-17T09:00:00+08:00",
            "records": [{"product": "SYN-G1", "color": None}],
        }
        with tempfile.TemporaryDirectory(prefix="profile-convergence-preview-") as raw:
            output = Path(raw)
            result = legacy.build_preview("idk", data, output)
            self.assertEqual("preview_ready", result["status"])
            self.assertFalse(result["enabled"])
            self.assertFalse(result["sent"])
            self.assertFalse(result["frozen"])
            self.assertFalse(result["price_baseline_accepted"])
            self.assertEqual("Hermes official; not invoked", result["transport"])
            self.assertTrue(result["files"])
            self.assertTrue(all((output / name).is_file() for name in result["files"]))
            self.assertTrue(
                any("NOT SENT" in (output / name).read_text(encoding="utf-8") for name in result["files"] if name.endswith(".txt"))
            )

    def test_explicit_private_acceptance_config_missing_and_bad_version_fail_closed(self):
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=11111111-1111-1111-1111-111111111111"
        corp_id = "synthetic-corp"
        config = {
            "version": 1,
            "mode": "acceptance_only",
            "enabled": True,
            "private_target": "synthetic-test-user",
            "application": {
                "name": "synthetic-app",
                "corp_id": corp_id,
                "agent_id": "1000043",
                "corp_secret": "synthetic-secret",
            },
            "test_webhook": url,
            "approved_webhook_sha256": hashlib.sha256(url.encode()).hexdigest(),
            "max_logical_notifications_per_batch": 2,
            "max_messages_per_batch": 12,
        }
        with tempfile.TemporaryDirectory(prefix="profile-convergence-config-") as raw:
            profile = Path(raw)
            with patch.object(
                acceptance_delivery, "TEST_USER", "synthetic-test-user"
            ), patch.object(
                acceptance_delivery,
                "APPROVED_WEBHOOK_SHA256",
                config["approved_webhook_sha256"],
            ), patch.object(
                acceptance_delivery,
                "APPROVED_CORP_SHA256",
                hashlib.sha256(corp_id.encode()).hexdigest(),
            ):
                with self.assertRaises(workflow_io.IOErrorBoundary):
                    acceptance_delivery.load_settings(profile)
                path = profile / acceptance_delivery.SECRET_FILE
                path.write_text(json.dumps({**config, "version": 2}), encoding="utf-8")
                with self.assertRaises(workflow_io.IOErrorBoundary):
                    acceptance_delivery.load_settings(profile)
                path.write_text(json.dumps(config), encoding="utf-8")
                loaded = acceptance_delivery.load_settings(profile)
                self.assertEqual("acceptance_only", loaded["mode"])
                self.assertEqual("synthetic-test-user", loaded["private_target"])

    def test_private_runtime_binding_missing_bad_version_and_valid_recovery(self):
        binding = {
            "kind": "legacy_execution",
            "enabled": True,
            "read_enabled": True,
            "customer_mapping_enabled": False,
            "freeze_enabled": False,
            "send_enabled": False,
            "price_accept_enabled": False,
            "register_schedule_enabled": False,
        }
        with tempfile.TemporaryDirectory(prefix="profile-convergence-binding-") as raw:
            profile = Path(raw)
            with patch.object(local_report, "_assert_local_context", return_value=None):
                with self.assertRaises(workflow_io.IOErrorBoundary):
                    workflow_io.load_activation(profile, "slow_task")

                path = profile / local_report.BINDINGS_FILE
                path.write_text(
                    json.dumps(
                        {
                            "version": 9,
                            "default_report": "legacy-slow-task",
                            "reports": {"legacy-slow-task": binding},
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(workflow_io.IOErrorBoundary):
                    workflow_io.load_activation(profile, "slow_task")

                path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "default_report": "legacy-slow-task",
                            "reports": {"legacy-slow-task": binding},
                        }
                    ),
                    encoding="utf-8",
                )
                loaded = workflow_io.load_activation(profile, "slow_task")
                self.assertEqual(binding, loaded)

    def test_private_role_configuration_missing_bad_then_explicit_import_recovers(self):
        """The active role file is restored through the production importer."""
        with tempfile.TemporaryDirectory(prefix="profile-convergence-roles-") as raw:
            profile = Path(raw)
            contract = profile / roles.LEGACY_RELATIVE_PATH
            contract.parent.mkdir(parents=True, exist_ok=True)
            contract.write_text(
                json.dumps(public_document(), ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaises(roles.RoleConfigurationError) as missing:
                roles.load(profile)
            self.assertEqual("ROLE_CONFIGURATION_REQUIRED", missing.exception.code)

            active = profile / roles.ACTIVE_RELATIVE_PATH
            active.parent.mkdir(parents=True, exist_ok=True)
            active.write_text(
                json.dumps({"schema": roles.SCHEMA, "version": 99}), encoding="utf-8"
            )
            with self.assertRaises(roles.RoleConfigurationError) as bad_version:
                roles.load(profile)
            self.assertEqual("ROLE_CONFIGURATION_INVALID", bad_version.exception.code)

            # Materialize an explicit synthetic source and recover via the same
            # import API used by the CLI.  No production/report_runs fallback is
            # involved, and all eight public regions remain required.
            source = profile / "synthetic-role-source.json"
            source.write_text(
                json.dumps(legacy_document(), ensure_ascii=False), encoding="utf-8"
            )
            active.unlink()
            imported = roles.import_legacy(source, active, profile)
            self.assertEqual("imported", imported["status"])
            loaded = roles.load(profile)
            self.assertEqual(len(REGIONS), len(loaded["regions"]))
            self.assertEqual(["BKK Sales"], loaded["regions"]["BKK"]["dynamic_sales_departments"])
            self.assertTrue(active.is_file())

    @staticmethod
    def _components() -> list[dict[str, object]]:
        result = []
        for stage, kind in (("text", "text"), ("file", "file")):
            result.append(
                {
                    "account": "private:synthetic-target",
                    "kind": kind,
                    "stage": stage,
                    "key": hashlib.sha256(stage.encode()).hexdigest(),
                    "notification_key": "synthetic-notification",
                }
            )
        return result

    class _Transport:
        def __init__(self, *, failed_stage: str | None = None, unknown_stage: str | None = None):
            self.failed_stage = failed_stage
            self.unknown_stage = unknown_stage
            self.send_calls: list[str] = []

        def normalize(self, items, _progress):
            return list(items)

        def fingerprint(self, item):
            return "synthetic-fingerprint-" + item["stage"]

        def preflight(self, _items):
            return None

        @contextmanager
        def delivery_lock(self, _progress):
            yield

        def prepare(self, _items, _progress):
            return None

        def send(self, item):
            self.send_calls.append(item["stage"])
            if item["stage"] == self.unknown_stage:
                raise TimeoutError("synthetic unknown outcome")
            if item["stage"] == self.failed_stage:
                return {"success": False, "raw_response": {"errcode": 45009}}
            return {
                "success": True,
                "message_id": "synthetic-message-" + item["stage"],
                "raw_response": {"errcode": 0},
            }

    def test_failed_generation_recovers_only_missing_component_and_unknown_stays_blocked(self):
        components = self._components()
        with tempfile.TemporaryDirectory(prefix="profile-convergence-recovery-") as raw:
            profile = Path(raw)
            first = workflow_io.Progress(profile, "structural", "2026-W38")
            first.run_id = "generation-one"
            failing = self._Transport(failed_stage="file")
            with self.assertRaisesRegex(
                workflow_io.IOErrorBoundary, "DELIVERY_COMPONENT_FAILED"
            ):
                workflow_io.deliver_components(
                    components, failing, first, enabled=True
                )
            self.assertEqual(["text", "file"], failing.send_calls)
            second = workflow_io.Progress(profile, "structural", "2026-W38")
            second.run_id = "generation-two"
            repaired = self._Transport()
            workflow_io.deliver_components(
                components, repaired, second, enabled=True, force=True
            )
            self.assertEqual(["file"], repaired.send_calls)
            self.assertEqual("provider_accepted", second.status(components[0]["key"]))

            unknown_profile = profile / "unknown"
            unknown = workflow_io.Progress(unknown_profile, "structural", "2026-W38")
            unknown.run_id = "generation-unknown"
            unknown_transport = self._Transport(unknown_stage="file")
            with self.assertRaisesRegex(
                workflow_io.IOErrorBoundary, "WORKFLOW|DELIVERY_UNKNOWN"
            ):
                workflow_io.deliver_components(
                    components, unknown_transport, unknown, enabled=True
                )
            blocked = workflow_io.Progress(unknown_profile, "structural", "2026-W38")
            retry_transport = self._Transport()
            with self.assertRaisesRegex(
                workflow_io.IOErrorBoundary, "DELIVERY_UNKNOWN_REVIEW_REQUIRED"
            ):
                workflow_io.deliver_components(
                    components, retry_transport, blocked, enabled=True, force=True
                )
            self.assertEqual([], retry_transport.send_calls)

    def test_atomic_precommit_interruption_preserves_delivered_state_for_recovery(self):
        class Store:
            def __init__(self):
                self.snapshots = {("sales", "main"): [{"state": "before"}]}
                self.cycles: dict[str, dict[str, object]] = {}

            def rows(self, role, scope=None):
                if role == "sales_snapshot":
                    return copy.deepcopy(self.snapshots[("sales", scope)])
                if role == "cycles":
                    return list(copy.deepcopy(self.cycles).values())
                return []

            @contextmanager
            def transaction(self):
                snapshot = copy.deepcopy(self.snapshots)
                cycles = copy.deepcopy(self.cycles)
                try:
                    yield
                except BaseException:
                    self.snapshots = snapshot
                    self.cycles = cycles
                    raise

            def replace_snapshot(self, side, scope, rows):
                self.snapshots[(side, scope)] = copy.deepcopy(rows)

            def cycle(self, key, scope, status, payload):
                self.cycles[key] = {
                    "cycle_id": key,
                    "test_scope": scope,
                    "status": status,
                    "payload": copy.deepcopy(payload),
                }

        store = Store()
        before = [{"state": "before"}]
        after = [{"state": "after"}]
        data = {
            "document": {"event_counts": {"changed": 1}},
            "before_digest": "before",
            "after_digest": "after",
            "after": after,
            "notices": [{"logical_id": "synthetic"}],
            "real_transport": False,
        }
        dispatch_calls: list[str] = []
        saved: list[dict[str, object]] = []

        def hasher(_side, rows):
            if rows == before:
                return "before"
            if rows == after:
                return "after"
            return "unexpected"

        def dispatcher(key, _items, _state, _send):
            dispatch_calls.append(key)
            return {"status": "provider_accepted", "components": 1}

        def fault(event, _key):
            if event == "before_commit":
                raise RuntimeError("synthetic process interruption before commit")

        with self.assertRaisesRegex(RuntimeError, "before commit"):
            workflow_cycle.complete_plan(
                store,
                "sales",
                "main",
                "sales-main-0",
                data,
                "planned",
                dispatcher=dispatcher,
                fault=fault,
                hasher=hasher,
                saver=lambda _name, value: saved.append(value),
            )
        self.assertEqual(before, store.snapshots[("sales", "main")])
        self.assertEqual("delivered", store.cycles["sales-main-0"]["status"])
        self.assertEqual(["sales-main-0"], dispatch_calls)

        result = workflow_cycle.complete_plan(
            store,
            "sales",
            "main",
            "sales-main-0",
            data,
            "delivered",
            dispatcher=dispatcher,
            hasher=hasher,
            saver=lambda _name, value: saved.append(value),
        )
        self.assertEqual(after, store.snapshots[("sales", "main")])
        self.assertEqual("committed", store.cycles["sales-main-0"]["status"])
        self.assertEqual(["sales-main-0"], dispatch_calls)
        self.assertEqual("provider_accepted", result["delivery"]["status"])


class StructuralExportAndReceiptTests(unittest.TestCase):
    def test_low_limit_truncated_packets_cannot_be_reported_as_complete(self):
        binding = {
            "kind": "fabric_review",
            "limit": 1,
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "inventory_scope": "total",
        }

        def handler(arguments):
            request = arguments["requests"][0]
            return json.dumps(
                {
                    "status": "success",
                    "results": [
                        {
                            "request_id": request["request_id"],
                            "status": "success",
                            "data_state": "truncated",
                            "truncated": True,
                            "requested_limit": 1,
                            "effective_limit": 1,
                            "has_more": True,
                            "rows": [{"group": "synthetic", "facts": {"metric_value": 1}}],
                        }
                    ],
                }
            )

        with tempfile.TemporaryDirectory(prefix="profile-convergence-export-") as raw:
            with patch.object(wire, "bounded_json_handler", return_value=handler):
                result = operations.execute_governed(
                    Path(raw), "legacy-fabric", binding, "synthetic-scope"
                )
        self.assertNotEqual(
            "success",
            result["status"],
            "a truncated governed packet must not become a complete export",
        )

    def test_exported_observation_sheet_retains_its_own_truncation_marker(self):
        document = {
            "status": "success",
            "observations": [
                {
                    "name": "出库总览",
                    "truncated": True,
                    "time": {"read_at": "2026-09-17T09:00:00+08:00"},
                    "rows": [
                        {
                            "group": "总体",
                            "facts": {"metric_value": 7},
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory(prefix="profile-convergence-fabric-") as raw:
            paths = fabric_report.export_report(document, Path(raw))
            # Keep this acceptance test runnable in the Hermes venv without
            # importing a third-party workbook reader.  Resolve the named
            # worksheet through the OOXML relationship and inspect its cells.
            import xml.etree.ElementTree as ET

            main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
            with zipfile.ZipFile(paths[0]) as archive:
                workbook = ET.fromstring(archive.read("xl/workbook.xml"))
                sheet = next(
                    node
                    for node in workbook.findall(f".//{{{main_ns}}}sheet")
                    if node.attrib.get("name") == "出库总览"
                )
                rel_id = sheet.attrib[f"{{{rel_ns}}}id"]
                relationships = ET.fromstring(
                    archive.read("xl/_rels/workbook.xml.rels")
                )
                target = next(
                    node.attrib["Target"]
                    for node in relationships.findall(f".//{{{pkg_rel_ns}}}Relationship")
                    if node.attrib.get("Id") == rel_id
                )
                worksheet_path = "xl/" + target.lstrip("/")
                worksheet = ET.fromstring(archive.read(worksheet_path))
                shared_strings = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in shared.findall(f".//{{{main_ns}}}si"):
                        shared_strings.append(
                            "".join(node.text or "" for node in item.findall(f".//{{{main_ns}}}t"))
                        )
                values = []
                for cell in worksheet.findall(f".//{{{main_ns}}}c"):
                    value = cell.find(f"{{{main_ns}}}v")
                    if value is None or value.text is None:
                        continue
                    text_value = value.text
                    if cell.attrib.get("t") == "s":
                        text_value = shared_strings[int(text_value)]
                    values.append(str(text_value))
        self.assertTrue(
            any("截断" in value or "truncated" in value.casefold() for value in values),
            "the observation sheet must identify that its source was truncated",
        )

    def test_cross_target_accepted_receipt_is_not_reused(self):
        prior_notice = {
            "logical_id": "target-old",
            "channel": "private",
            "role": "synthetic role",
            "body": "same governed body",
            "attachments": [],
            "message_format": "text",
        }
        current_notice = {**prior_notice, "logical_id": "target-new"}
        with tempfile.TemporaryDirectory(prefix="profile-convergence-receipt-") as raw:
            profile = Path(raw)
            with patch.object(delivery_review.base, "profile", return_value=profile):
                allowed = workflow_io.private_root(
                    acceptance_delivery.runtime_home(profile)
                )
                allowed.mkdir(parents=True, exist_ok=True)
                progress_path = allowed / "synthetic-progress.json"
                progress_path.write_text(
                    json.dumps(
                        {
                            "components": {
                                "a" * 64: {"status": "provider_accepted"}
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                prior = {
                    "notices": [prior_notice],
                    "evidence": {"baseline_week": "2026-W38"},
                    "notice_digest": delivery_review.base.digest([prior_notice]),
                }
                (allowed / "prior-manifest.json").write_text(
                    json.dumps(prior), encoding="utf-8"
                )
                (allowed / "prior-receipt.json").write_text(
                    json.dumps(
                        {
                            "status": "provider_accepted_not_human_read",
                            "progress_file": str(progress_path),
                            "components": 1,
                            "case_id": "prior-2026-w38",
                        }
                    ),
                    encoding="utf-8",
                )
                result = delivery_review.review(
                    types.SimpleNamespace(rows=lambda *_args: []),
                    "ls-hcm-2026-w38-g0-current",
                    {"notices": [current_notice]},
                )
        self.assertEqual([], result["prior_matches"])


if __name__ == "__main__":
    unittest.main()
