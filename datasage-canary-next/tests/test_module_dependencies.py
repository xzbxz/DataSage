from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

from agent.secret_scope import (  # noqa: E402
    UnscopedSecretError,
    is_multiplex_active,
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)
from hermes_constants import (  # noqa: E402
    reset_hermes_home_override,
    set_hermes_home_override,
)

TEST_PACKAGE = "datasage_query_dependency_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

contract_store = importlib.import_module(f"{TEST_PACKAGE}.contract_store")
contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
db_executor = importlib.import_module(f"{TEST_PACKAGE}.db_executor")
db_runtime = importlib.import_module(f"{TEST_PACKAGE}.db_runtime")
db_security = importlib.import_module(f"{TEST_PACKAGE}.db_security")
entities = importlib.import_module(f"{TEST_PACKAGE}.entities")
runtime_health = importlib.import_module(f"{TEST_PACKAGE}.runtime_health")
receipt_cache = importlib.import_module(f"{TEST_PACKAGE}.receipt_cache")
sql_identifiers = importlib.import_module(f"{TEST_PACKAGE}.sql_identifiers")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")


class ModuleDependencyTests(unittest.TestCase):
    @staticmethod
    def _canonical_pymysql_family() -> dict[str, object]:
        return {
            name: module
            for name, module in tuple(sys.modules.items())
            if name == "pymysql" or name.startswith("pymysql.")
        }

    @staticmethod
    def _replace_canonical_pymysql_family(modules: dict[str, object]) -> None:
        for name in ModuleDependencyTests._canonical_pymysql_family():
            sys.modules.pop(name, None)
        sys.modules.update(modules)

    @staticmethod
    def _alias_family(alias: str) -> dict[str, object]:
        return {
            name: module
            for name, module in tuple(sys.modules.items())
            if name == alias or name.startswith(f"{alias}.")
        }

    @staticmethod
    def _write_synthetic_pymysql(
        profile_root: Path,
        marker: str,
        *,
        fail_after_submodule: bool = False,
    ) -> Path:
        plugin_root = profile_root / "plugins" / "datasage-query"
        package_root = plugin_root / "vendor" / "pymysql"
        package_root.mkdir(parents=True)
        (package_root / "cursors.py").write_text(
            "class SSDictCursor:\n    pass\n",
            encoding="utf-8",
        )
        (package_root / "profile_marker.py").write_text(
            f"PROFILE_MARKER = {marker!r}\n",
            encoding="utf-8",
        )
        failure = (
            "raise RuntimeError('synthetic import failure')\n"
            if fail_after_submodule
            else ""
        )
        (package_root / "__init__.py").write_text(
            "import _imp\n"
            "IMPORT_LOCK_HELD = _imp.lock_held()\n"
            "VERSION = (1, 2, 0)\n"
            "from . import cursors\n"
            "from .profile_marker import PROFILE_MARKER\n"
            "def connect(**kwargs):\n"
            "    return kwargs\n"
            f"{failure}",
            encoding="utf-8",
        )
        return plugin_root

    def test_pymysql_loader_keeps_profile_aliases_coexisting_and_reuses_each_profile(
        self,
    ) -> None:
        original_canonical = self._canonical_pymysql_family()
        original_sys_path = list(sys.path)
        original_file = db_runtime.__file__
        aliases: list[str] = []
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                profile_a = temporary_root / "profile-a"
                profile_b = temporary_root / "profile-b"
                plugin_a = self._write_synthetic_pymysql(profile_a, "profile-a")
                plugin_b = self._write_synthetic_pymysql(profile_b, "profile-b")
                alias_a = db_runtime._pymysql_alias((plugin_a / "vendor").resolve())
                alias_b = db_runtime._pymysql_alias((plugin_b / "vendor").resolve())
                aliases.extend((alias_a, alias_b))

                home_token = set_hermes_home_override(profile_a)
                try:
                    db_runtime.__file__ = str(plugin_a / "db_runtime.py")
                    module_a = db_runtime.load_pymysql()
                    self.assertIs(module_a, db_runtime.load_pymysql())
                    self.assertTrue(module_a.IMPORT_LOCK_HELD)
                finally:
                    reset_hermes_home_override(home_token)

                home_token = set_hermes_home_override(profile_b)
                try:
                    db_runtime.__file__ = str(plugin_b / "db_runtime.py")
                    module_b = db_runtime.load_pymysql()
                    self.assertIs(module_b, db_runtime.load_pymysql())
                    self.assertTrue(module_b.IMPORT_LOCK_HELD)
                finally:
                    reset_hermes_home_override(home_token)

                self.assertIsNot(module_a, module_b)
                self.assertEqual(alias_a, module_a.__name__)
                self.assertEqual(alias_b, module_b.__name__)
                self.assertEqual("profile-a", module_a.PROFILE_MARKER)
                self.assertEqual("profile-b", module_b.PROFILE_MARKER)
                home_token = set_hermes_home_override(profile_a)
                try:
                    db_runtime.__file__ = str(plugin_a / "db_runtime.py")
                    self.assertIs(module_a, db_runtime.load_pymysql())
                finally:
                    reset_hermes_home_override(home_token)
                for alias, package_root in (
                    (alias_a, (plugin_a / "vendor" / "pymysql").resolve()),
                    (alias_b, (plugin_b / "vendor" / "pymysql").resolve()),
                ):
                    family = self._alias_family(alias)
                    self.assertGreaterEqual(len(family), 3)
                    for module in family.values():
                        self.assertTrue(
                            Path(module.__file__).resolve().is_relative_to(package_root)
                        )
                self.assertEqual(original_canonical, self._canonical_pymysql_family())
                self.assertEqual(original_sys_path, sys.path)
        finally:
            db_runtime.__file__ = original_file
            for alias in aliases:
                db_runtime._clear_pymysql_modules(alias)
            self._replace_canonical_pymysql_family(original_canonical)
            sys.path[:] = original_sys_path

    def test_pymysql_loader_leaves_external_canonical_package_untouched(
        self,
    ) -> None:
        original_modules = self._canonical_pymysql_family()
        original_sys_path = list(sys.path)
        original_file = db_runtime.__file__
        alias = ""
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                profile = temporary_root / "profile"
                plugin_root = self._write_synthetic_pymysql(profile, "profile")
                external_root = temporary_root / "external" / "pymysql"
                external_root.mkdir(parents=True)
                external_module = types.ModuleType("pymysql")
                external_module.__file__ = str(external_root / "__init__.py")
                external_module.__path__ = [str(external_root)]
                external_module.VERSION = (1, 2, 0)
                external_module.connect = lambda **kwargs: kwargs
                external_module.cursors = types.SimpleNamespace(SSDictCursor=object())
                external_child = types.ModuleType("pymysql.external")
                external_child.__file__ = str(external_root / "external.py")
                external_modules = {
                    "pymysql": external_module,
                    "pymysql.external": external_child,
                }
                self._replace_canonical_pymysql_family(external_modules)
                db_runtime.__file__ = str(plugin_root / "db_runtime.py")
                alias = db_runtime._pymysql_alias(
                    (plugin_root / "vendor").resolve()
                )

                loaded = db_runtime.load_pymysql()

                self.assertIsNot(external_module, loaded)
                self.assertEqual(alias, loaded.__name__)
                self.assertEqual(external_modules, self._canonical_pymysql_family())
                self.assertEqual(original_sys_path, sys.path)
        finally:
            db_runtime.__file__ = original_file
            if alias:
                db_runtime._clear_pymysql_modules(alias)
            self._replace_canonical_pymysql_family(original_modules)
            sys.path[:] = original_sys_path

    def test_pymysql_loader_failure_cleans_only_the_failing_profile_alias(
        self,
    ) -> None:
        original_canonical = self._canonical_pymysql_family()
        original_sys_path = list(sys.path)
        original_file = db_runtime.__file__
        aliases: list[str] = []
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                profile_a = temporary_root / "profile-a"
                profile_b = temporary_root / "profile-b"
                plugin_a = self._write_synthetic_pymysql(profile_a, "profile-a")
                plugin_b = self._write_synthetic_pymysql(
                    profile_b,
                    "profile-b",
                    fail_after_submodule=True,
                )
                alias_a = db_runtime._pymysql_alias((plugin_a / "vendor").resolve())
                alias_b = db_runtime._pymysql_alias((plugin_b / "vendor").resolve())
                aliases.extend((alias_a, alias_b))
                db_runtime.__file__ = str(plugin_a / "db_runtime.py")
                module_a = db_runtime.load_pymysql()
                family_a = self._alias_family(alias_a)

                db_runtime.__file__ = str(plugin_b / "db_runtime.py")
                with self.assertRaisesRegex(RuntimeError, "synthetic import failure"):
                    db_runtime.load_pymysql()

                self.assertIs(module_a, sys.modules[alias_a])
                self.assertEqual(family_a, self._alias_family(alias_a))
                self.assertEqual({}, self._alias_family(alias_b))
                self.assertEqual(original_canonical, self._canonical_pymysql_family())
                self.assertEqual(original_sys_path, sys.path)
        finally:
            db_runtime.__file__ = original_file
            for alias in aliases:
                db_runtime._clear_pymysql_modules(alias)
            self._replace_canonical_pymysql_family(original_canonical)
            sys.path[:] = original_sys_path

    def test_profile_roots_and_yaml_cache_follow_the_context_override(self) -> None:
        relative_path = "plugins/datasage-query/contracts/query-policy.yaml"
        content = b"value: synthetic\n"
        digest = "a" * 64

        def contract_bytes(path: str):
            return contract_store.profile_root() / path, content, digest

        homes = (
            PROFILE_ROOT / "synthetic-profile-a",
            PROFILE_ROOT / "synthetic-profile-b",
        )
        roots = []
        signatures = []
        parsed = []
        contract_store.parse_yaml_cached.cache_clear()
        try:
            with mock.patch.object(
                contract_store, "_contract_bytes", side_effect=contract_bytes
            ):
                for home in homes:
                    token = set_hermes_home_override(home)
                    try:
                        roots.append(contract_store.profile_root())
                        self.assertEqual(
                            contract_store.profile_root(), runtime_health._profile_root()
                        )
                        signatures.append(contract_store.content_signature(relative_path))
                        parsed.append(contract_store.read_yaml(relative_path))
                    finally:
                        reset_hermes_home_override(token)

            self.assertEqual(
                [home.resolve(strict=False) for home in homes],
                roots,
            )
            self.assertNotEqual(signatures[0][0], signatures[1][0])
            self.assertEqual([digest, digest], [item[1] for item in signatures])
            self.assertEqual([{"value": "synthetic"}] * 2, parsed)
            cache_info = contract_store.parse_yaml_cached.cache_info()
            self.assertEqual(2, cache_info.misses)
            self.assertEqual(2, cache_info.currsize)
        finally:
            contract_store.parse_yaml_cached.cache_clear()

    def test_db_and_tls_configuration_use_the_scoped_profile_secrets(self) -> None:
        process_values = {
            "DATA_QUERY_MYSQL_HOST": "process-db.invalid",
            "DATA_QUERY_MYSQL_PORT": "3307",
            "DATA_QUERY_MYSQL_DATABASE": "process_schema",
            "DATA_QUERY_MYSQL_USER": "process_user",
            "DATA_QUERY_MYSQL_PASSWORD": "process_password",
            "DATA_QUERY_MYSQL_SSL_CA": "C:\\synthetic\\process-ca.pem",
        }
        scoped_ca = "C:\\synthetic\\scoped-ca.pem"
        scoped_values = {
            "DATA_QUERY_MYSQL_HOST": "scoped-db.invalid",
            "DATA_QUERY_MYSQL_PORT": "4407",
            "DATA_QUERY_MYSQL_DATABASE": "scoped_schema",
            "DATA_QUERY_MYSQL_USER": "scoped_user",
            "DATA_QUERY_MYSQL_PASSWORD": "scoped_password",
            "DATA_QUERY_MYSQL_SSL_CA": scoped_ca,
        }
        connection = mock.MagicMock()
        pymysql = types.SimpleNamespace(
            connect=mock.Mock(return_value=connection),
            cursors=types.SimpleNamespace(SSDictCursor=object()),
        )
        tls_evidence = {
            "transport_mode": "tls",
            "tls_required": True,
            "tls_configured": True,
            "tls_verified": True,
            "tls_protocol": "TLSv1.3",
            "tls_cipher": "synthetic-cipher",
        }
        grant_evidence = {
            "grants_verified": True,
            "grant_policy": "strict_object_read_only",
            "read_only_privileges": ["SELECT"],
            "observed_privileges": ["SELECT"],
        }

        previous_multiplex = is_multiplex_active()
        set_multiplex_active(True)
        secret_token = set_secret_scope(scoped_values)
        try:
            with mock.patch.dict(os.environ, process_values, clear=False), mock.patch.object(
                db_runtime, "load_pymysql", return_value=pymysql
            ), mock.patch.object(
                db_runtime.settings,
                "get_int",
                side_effect=lambda _name, default, _minimum, _maximum: default,
            ), mock.patch.object(
                db_runtime, "mysql_tls_kwargs", return_value={}
            ), mock.patch.object(
                db_runtime, "verify_mysql_tls", return_value=tls_evidence
            ), mock.patch.object(
                db_runtime,
                "verify_mysql_read_only_grants",
                return_value=grant_evidence,
            ), mock.patch.object(
                db_runtime, "verify_mysql_source_identity"
            ) as source_identity:
                self.assertIs(connection, db_runtime.connect())

            connect_kwargs = pymysql.connect.call_args.kwargs
            self.assertEqual("scoped-db.invalid", connect_kwargs["host"])
            self.assertEqual(4407, connect_kwargs["port"])
            self.assertEqual("scoped_schema", connect_kwargs["database"])
            self.assertEqual("scoped_user", connect_kwargs["user"])
            self.assertEqual("scoped_password", connect_kwargs["password"])
            source_identity.assert_called_once()

            with mock.patch.object(
                db_security,
                "_database_security_policy",
                return_value={
                    "production_mode": True,
                    "require_tls": True,
                    "canary_accept_existing_account": False,
                },
            ), mock.patch.object(Path, "is_file", return_value=True):
                tls = db_security.mysql_tls_kwargs()
            self.assertEqual(
                str(Path(scoped_ca).expanduser().resolve()),
                tls["ssl_ca"],
            )
        finally:
            reset_secret_scope(secret_token)
            set_multiplex_active(previous_multiplex)

    def test_secret_scope_preserves_single_profile_and_fails_closed_unscoped(self) -> None:
        previous_multiplex = is_multiplex_active()
        try:
            with mock.patch.dict(
                os.environ, {"DATA_QUERY_MYSQL_PORT": "3307"}, clear=False
            ):
                set_multiplex_active(False)
                self.assertEqual(3307, db_runtime.connection_port())

                empty_scope = set_secret_scope(None)
                set_multiplex_active(True)
                try:
                    with self.assertRaises(UnscopedSecretError):
                        db_runtime.connection_port()
                finally:
                    reset_secret_scope(empty_scope)
        finally:
            set_multiplex_active(previous_multiplex)

    def test_live_readiness_cache_key_includes_profile_and_scoped_secrets(self) -> None:
        scoped_values = {
            "DATA_QUERY_MYSQL_HOST": "scoped-db.invalid",
            "DATA_QUERY_MYSQL_PORT": "4407",
            "DATA_QUERY_MYSQL_DATABASE": "scoped_schema",
            "DATA_QUERY_MYSQL_USER": "scoped_user",
            "DATA_QUERY_MYSQL_PASSWORD": "scoped_password",
            "DATA_QUERY_MYSQL_SSL_CA": "C:\\synthetic\\scoped-ca.pem",
        }
        homes = (
            PROFILE_ROOT / "synthetic-profile-a",
            PROFILE_ROOT / "synthetic-profile-b",
        )
        keys = []
        same_profile_keys = []
        previous_multiplex = is_multiplex_active()
        set_multiplex_active(True)
        secret_token = set_secret_scope(scoped_values)
        try:
            with mock.patch.object(
                runtime_health.settings, "get", return_value=None
            ), mock.patch.object(
                runtime_health.settings, "get_list", return_value=[]
            ), mock.patch.object(
                runtime_health.settings,
                "get_int",
                side_effect=lambda _name, default, _minimum, _maximum: default,
            ):
                for home in homes:
                    home_token = set_hermes_home_override(home)
                    try:
                        keys.append(runtime_health._live_cache_key())
                    finally:
                        reset_hermes_home_override(home_token)
                same_profile_keys.append(keys[0])
                alternate_scope = set_secret_scope(
                    {**scoped_values, "DATA_QUERY_MYSQL_PASSWORD": "alternate_password"}
                )
                try:
                    home_token = set_hermes_home_override(homes[0])
                    try:
                        same_profile_keys.append(runtime_health._live_cache_key())
                    finally:
                        reset_hermes_home_override(home_token)
                finally:
                    reset_secret_scope(alternate_scope)
        finally:
            reset_secret_scope(secret_token)
            set_multiplex_active(previous_multiplex)

        self.assertNotEqual(keys[0], keys[1])
        self.assertNotEqual(same_profile_keys[0], same_profile_keys[1])

    def test_entity_registry_cache_uses_profile_bound_content_signatures(self) -> None:
        signatures = {
            path: (f"synthetic-profile::{path}", str(index) * 64)
            for index, path in enumerate(entities._REGISTRY_DEPENDENCIES, 1)
        }
        sentinel = {"registry": "synthetic"}
        with mock.patch.object(
            contract_store,
            "content_signature",
            side_effect=lambda path: signatures[path],
        ) as content_signature, mock.patch.object(
            entities, "_registry_cached", return_value=sentinel
        ) as registry_cached:
            self.assertIs(sentinel, entities._registry())

        self.assertEqual(
            list(entities._REGISTRY_DEPENDENCIES),
            [call.args[0] for call in content_signature.call_args_list],
        )
        registry_cached.assert_called_once_with(
            tuple(signatures[path] for path in entities._REGISTRY_DEPENDENCIES)
        )

    def test_shared_yaml_cache_is_the_only_yaml_parser_owner(self) -> None:
        contract_store.parse_yaml_cached.cache_clear()
        path = "plugins/datasage-query/contracts/query-policy.yaml"
        via_tools = tools._read_yaml(path)
        via_contracts = contracts._read_yaml(path)
        self.assertIs(via_tools, via_contracts)
        self.assertFalse(hasattr(tools, "_parse_yaml_cached"))
        self.assertFalse(hasattr(contracts, "_parse_yaml_cached"))
        self.assertFalse(hasattr(tools, "_profile_root"))
        self.assertFalse(hasattr(contracts, "_profile_root"))

    def test_identifier_compatibility_facade_preserves_results_and_codes(self) -> None:
        self.assertEqual(
            sql_identifiers.quote_table("vk_dw.sample"),
            tools._quote_table("vk_dw.sample"),
        )
        self.assertEqual("`alias`.`column_1`", tools._qualified_identifier("alias", "column_1"))
        for invalid in ("", "bad-name", "schema.table.extra"):
            with self.subTest(invalid=invalid), self.assertRaises(tools.QueryFailure) as caught:
                tools._quote_table(invalid)
            self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_database_connection_compatibility_facade_delegates(self) -> None:
        sentinel = object()
        with mock.patch.object(db_runtime, "connect", return_value=sentinel) as connect:
            self.assertIs(sentinel, tools._connect(timeout_seconds=3))
        connect.assert_called_once_with(
            connect_timeout_seconds=None,
            read_timeout_seconds=None,
            timeout_seconds=3,
        )

        with mock.patch.object(
            db_runtime,
            "connect",
            side_effect=db_runtime.DatabaseRuntimeError(
                "CONFIGURATION_MISSING", "missing"
            ),
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._connect()
        self.assertEqual("CONFIGURATION_MISSING", caught.exception.code)

    def test_entity_executor_bridge_keeps_existing_monkeypatch_seam(self) -> None:
        with mock.patch.object(tools, "_execute", return_value=([], False)) as execute:
            payload = json.loads(entities.datasage_entity_resolve({"token": "未登记地域"}))
        self.assertEqual("not_found", payload["status"])
        execute.assert_called_once()

    def test_runtime_health_uses_shared_database_adapter(self) -> None:
        class Connection:
            _datasage_security_evidence = {
                "live_connection_verified": True,
                "grants_verified": True,
                "transport_mode": "tls",
            }

            def close(self) -> None:
                return None

        scoped_values = {
            "DATA_QUERY_MYSQL_HOST": "scoped-db.invalid",
            "DATA_QUERY_MYSQL_PORT": "4407",
            "DATA_QUERY_MYSQL_DATABASE": "scoped_schema",
            "DATA_QUERY_MYSQL_USER": "scoped_user",
            "DATA_QUERY_MYSQL_PASSWORD": "scoped_password",
            "DATA_QUERY_MYSQL_SSL_CA": "C:\\synthetic\\scoped-ca.pem",
        }
        previous_cache = runtime_health._LIVE_CACHE
        previous_multiplex = is_multiplex_active()
        set_multiplex_active(True)
        secret_token = set_secret_scope(scoped_values)
        try:
            runtime_health._LIVE_CACHE = None
            with mock.patch.object(
                runtime_health.settings, "get", return_value=None
            ), mock.patch.object(
                runtime_health.settings, "get_list", return_value=[]
            ), mock.patch.object(
                runtime_health.settings,
                "get_int",
                side_effect=lambda _name, default, _minimum, _maximum: default,
            ), mock.patch.object(
                db_runtime, "connect", return_value=Connection()
            ) as connect:
                status = runtime_health.live_database_security_status()
        finally:
            runtime_health._LIVE_CACHE = previous_cache
            reset_secret_scope(secret_token)
            set_multiplex_active(previous_multiplex)
        self.assertTrue(status["ready"], status)
        connect.assert_called_once()

    def test_receipt_facade_uses_the_signature_aware_cache(self) -> None:
        with mock.patch.object(
            receipt_cache,
            "get_metric_capability_receipt",
            return_value="a" * 64,
        ) as cached:
            receipt = tools._current_metric_detail_receipt(
                "delivery", "delivery_amount"
            )
        self.assertEqual("a" * 64, receipt)
        cached.assert_called_once_with(
            "delivery",
            "delivery_amount",
            builder=tools._build_current_metric_detail_receipt,
        )

        with mock.patch.object(
            receipt_cache,
            "get_metric_capability_receipt",
            side_effect=contract_store.ContractStoreError(
                "CONTRACT_UNAVAILABLE", "missing"
            ),
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._current_metric_detail_receipt("delivery", "delivery_amount")
        self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)
        self.assertEqual("contract_load", caught.exception.stage)

    def test_single_statement_facade_preserves_public_error_taxonomy(self) -> None:
        source = {"source_identity": "warehouse-a"}
        cases = (
            (
                db_executor.DeadlineExceeded("late"),
                "BATCH_DEADLINE_EXCEEDED",
                True,
            ),
            (TimeoutError("socket timed out"), "QUERY_TIMEOUT", True),
            (RuntimeError("driver failure"), "QUERY_FAILED", False),
        )
        for error, expected_code, expected_timeout in cases:
            with self.subTest(expected_code=expected_code):
                delegate = mock.Mock()
                delegate.execute.side_effect = error
                delegate.source_evidence_ref = source
                with mock.patch.object(
                    db_executor,
                    "ReadOnlyDbExecutor",
                    return_value=delegate,
                ), self.assertRaises(tools.QueryFailure) as caught:
                    tools._execute_with_source("SELECT 1", (), 1)
                self.assertEqual(expected_code, caught.exception.code)
                self.assertEqual(expected_timeout, caught.exception.timeout)
                self.assertEqual(source, caught.exception.source_evidence_ref)

        delegate = mock.Mock()
        delegate.execute.side_effect = tools.DatabaseSecurityError(
            "SOURCE_IDENTITY_CHANGED", "changed"
        )
        delegate.source_evidence_ref = source
        with mock.patch.object(
            db_executor,
            "ReadOnlyDbExecutor",
            return_value=delegate,
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._execute_with_source("SELECT 1", (), 1)
        self.assertEqual("DATABASE_IDENTITY_CHANGED", caught.exception.code)
        self.assertEqual("database_security", caught.exception.stage)
        self.assertEqual(source, caught.exception.source_evidence_ref)

    def test_snapshot_facade_runs_through_shared_executor_end_to_end(self) -> None:
        class Cursor:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, params=None):
                self.connection.executions.append((sql, params))

            def fetchmany(self, count):
                self.connection.fetch_sizes.append(count)
                return [{"metric_value": 7}]

        class Connection:
            def __init__(self):
                self.executions = []
                self.fetch_sizes = []
                self.rollback_count = 0
                self.close_count = 0

            def cursor(self):
                return Cursor(self)

            def rollback(self):
                self.rollback_count += 1

            def close(self):
                self.close_count += 1

        connection = Connection()
        source = {"source_identity": "warehouse-a"}
        with mock.patch.object(
            tools, "_connect", return_value=connection
        ) as connect, mock.patch.object(
            tools,
            "confirm_mysql_read_only_transaction",
            return_value=source,
        ) as confirm:
            with tools._consistent_snapshot_executor() as snapshot:
                rows, truncated, observed_source = snapshot.execute(
                    "SELECT metric_value", (), 2
                )
                self.assertIsNotNone(snapshot.marker)

        self.assertEqual([{"metric_value": 7}], rows)
        self.assertFalse(truncated)
        self.assertEqual(source, observed_source)
        self.assertEqual([3], connection.fetch_sizes)
        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)
        connect.assert_called_once()
        confirm.assert_called_once_with(connection)
        expected_sql = [
            "SET SESSION time_zone = '+08:00'",
            "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
            "SET SESSION MAX_EXECUTION_TIME = %s",
            "SELECT metric_value",
        ]
        observed_sql = [sql for sql, _params in connection.executions]
        self.assertEqual(expected_sql, observed_sql)
        for statement in expected_sql:
            self.assertEqual(1, observed_sql.count(statement), statement)
        self.assertEqual((), connection.executions[-1][1])

    def test_snapshot_facade_maps_public_errors_and_reuses_poison(self) -> None:
        source = {"source_identity": "warehouse-a"}
        driver_timeout = RuntimeError(
            3024, "Maximum statement execution time exceeded"
        )
        for raw_error, expected_code in (
            (db_executor.DeadlineExceeded("late"), "BATCH_DEADLINE_EXCEEDED"),
            (driver_timeout, "SERVER_STATEMENT_TIMEOUT"),
        ):
            with self.subTest(expected_code=expected_code):
                delegate = mock.MagicMock()
                delegate.marker = "snapshot_group_test"
                delegate.source_evidence_ref = source
                delegate.execute.side_effect = raw_error
                with mock.patch.object(
                    db_executor,
                    "ReadOnlyDbExecutor",
                    return_value=delegate,
                ):
                    with tools._consistent_snapshot_executor() as snapshot:
                        with self.assertRaises(tools.QueryFailure) as first:
                            snapshot.execute("SELECT broken", (), 1)
                        with self.assertRaises(tools.QueryFailure) as second:
                            snapshot.execute("SELECT later", (), 1)
                self.assertEqual(expected_code, first.exception.code)
                self.assertEqual(expected_code, second.exception.code)
                self.assertTrue(first.exception.timeout)
                self.assertEqual(source, first.exception.source_evidence_ref)
                self.assertEqual(source, second.exception.source_evidence_ref)
                delegate.execute.assert_called_once()
                delegate.close.assert_called_once()

        security_delegate = mock.MagicMock()
        security_delegate.source_evidence_ref = None
        security_delegate.__enter__.side_effect = tools.DatabaseSecurityError(
            "SOURCE_IDENTITY_CHANGED", "changed"
        )
        with mock.patch.object(
            db_executor,
            "ReadOnlyDbExecutor",
            return_value=security_delegate,
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._consistent_snapshot_executor().__enter__()
        self.assertEqual("DATABASE_IDENTITY_CHANGED", caught.exception.code)
        self.assertEqual("database_security", caught.exception.stage)
        security_delegate.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
