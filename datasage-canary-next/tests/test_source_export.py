"""Tests for the explicit, offline DataSage source review exporter."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
import uuid
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import datasage_source_export as source_export


class SourceExportTests(unittest.TestCase):
    def _copy_candidate(self, destination: Path) -> Path:
        target = destination / source_export.PACKAGE_NAME
        # Other integration tests may have created runtime files in the test
        # Home. Never copy them (or a real Home's credentials) into this fixture.
        for relative in source_export.SOURCE_ALLOWLIST:
            source = source_export._source_file(ROOT, relative)
            copied = target / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copied)
        return target

    def test_unknown_runtime_files_are_not_exported_and_config_is_not_mutated(self) -> None:
        before = hashlib.sha256((ROOT / "config.yaml").read_bytes()).hexdigest()
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            workspace = Path(temporary)
            candidate = self._copy_candidate(workspace)
            (candidate / "unreviewed-secret.py").write_text(
                'API_KEY="synthetic-secret-value"\n', encoding="utf-8"
            )
            (candidate / ".env").write_text(
                "DATA_QUERY_MYSQL_PASSWORD=synthetic-secret-value\n", encoding="utf-8"
            )
            (candidate / "state.db").write_bytes(b"synthetic state")
            (candidate / "report_runs").mkdir()
            (candidate / "report_runs" / "real-report.json").write_text(
                '{"customer":"synthetic"}', encoding="utf-8"
            )
            archive = workspace / "review.tar.gz"
            result = source_export.export_source(candidate, archive)
            self.assertTrue(result.archive)
            self.assertEqual(before, hashlib.sha256((ROOT / "config.yaml").read_bytes()).hexdigest())
            with tarfile.open(archive, "r:gz") as handle:
                names = {member.name for member in handle.getmembers()}
            self.assertIn(f"{source_export.PACKAGE_NAME}/config.yaml", names)
            self.assertIn(f"{source_export.PACKAGE_NAME}/EXPORT_MANIFEST.json", names)
            self.assertNotIn(f"{source_export.PACKAGE_NAME}/unreviewed-secret.py", names)
            self.assertNotIn(f"{source_export.PACKAGE_NAME}/.env", names)
            self.assertNotIn(f"{source_export.PACKAGE_NAME}/state.db", names)
            self.assertFalse(any("report_runs" in name for name in names))

    def test_config_template_preserves_switches_and_replaces_identity_and_path(self) -> None:
        original = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        original_channel_id = "synthetic-private-export-channel"
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            workspace = Path(temporary)
            candidate = self._copy_candidate(workspace)
            # The installed config may already be portable. Supply a synthetic
            # identity/path so redaction is tested independently of local values.
            candidate_config = yaml.safe_load((candidate / "config.yaml").read_text(encoding="utf-8"))
            candidate_config["platforms"]["wecom"]["home_channel"].update(
                chat_id=original_channel_id, name="Synthetic channel", user_id="synthetic-owner")
            candidate_config["lsp"]["servers"]["pyright"]["command"][0] = "C:/synthetic-tools/pyright.cmd"
            candidate_config["platforms"]["wecom"]["extra"].update(
                allow_admin_from=["synthetic-approver"],
                group_allow_admin_from=["synthetic-approver"],
            )
            (candidate / "config.yaml").write_text(yaml.safe_dump(candidate_config), encoding="utf-8")
            output = workspace / "package"
            source_export.export_source(candidate, output)
            exported = yaml.safe_load((output / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(original["agent"], exported["agent"])
            self.assertEqual(original["tools"], exported["tools"])
            self.assertEqual(original["plugins"], exported["plugins"])
            self.assertEqual(original["memory"], exported["memory"])
            channel = exported["platforms"]["wecom"]["home_channel"]
            self.assertEqual("${WECOM_HOME_CHANNEL}", channel["chat_id"])
            self.assertEqual("${WECOM_HOME_CHANNEL}", channel["name"])
            self.assertEqual("${WECOM_HOME_CHANNEL}", channel["user_id"])
            self.assertEqual(
                "${PYRIGHT_LANGSERVER_COMMAND}",
                exported["lsp"]["servers"]["pyright"]["command"][0],
            )
            env_example = (output / ".env.EXAMPLE").read_text(encoding="utf-8")
            self.assertIn("PYRIGHT_LANGSERVER_COMMAND=pyright-langserver", env_example)
            assignments = {
                line.split("=", 1)[0]: line.split("=", 1)[1]
                for line in env_example.splitlines()
                if line and not line.lstrip().startswith("#") and "=" in line
            }
            for key, value in assignments.items():
                if key != "PYRIGHT_LANGSERVER_COMMAND":
                    self.assertEqual("", value, key)
            self.assertNotIn("C:/Users/", (output / "config.yaml").read_text(encoding="utf-8"))
            self.assertNotIn(original_channel_id, (output / "config.yaml").read_text(encoding="utf-8"))
            # Approver ids are identities too: the installed profile keeps them,
            # the shareable template must not.
            exported_extra = exported["platforms"]["wecom"]["extra"]
            self.assertEqual(
                ["${WECOM_APPROVER_USER_ID}"], exported_extra["allow_admin_from"]
            )
            self.assertEqual(
                ["${WECOM_APPROVER_USER_ID}"], exported_extra["group_allow_admin_from"]
            )
            exported_text = (output / "config.yaml").read_text(encoding="utf-8")
            self.assertNotIn("synthetic-approver", exported_text)
            for approver in (
                original["platforms"]["wecom"]["extra"].get("allow_admin_from") or []
            ):
                self.assertNotIn(str(approver), exported_text)

    def test_every_remediation_record_and_fixture_is_in_the_export_allowlist(self) -> None:
        """A new record must not be silently left out of the shareable source."""

        allowed = {str(item) for item in source_export.SOURCE_ALLOWLIST}
        missing: list[str] = []
        for path in sorted((ROOT / "docs").glob("remediation-*.md")):
            if f"docs/{path.name}" not in allowed:
                missing.append(f"docs/{path.name}")
        fixtures = sorted((ROOT / "tests" / "fixtures").glob("*.json"))
        self.assertTrue(fixtures, "expected fixtures under tests/fixtures")
        for path in fixtures:
            if f"tests/fixtures/{path.name}" not in allowed:
                missing.append(f"tests/fixtures/{path.name}")
        self.assertEqual([], missing, "add these to the export allowlist")

    def test_missing_contract_fails_closed(self) -> None:
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            candidate = self._copy_candidate(Path(temporary))
            (candidate / "plugins/datasage-query/contracts/query-policy.yaml").unlink()
            with self.assertRaises(source_export.MissingResourceError):
                source_export.export_source(candidate, Path(temporary) / "review.tar.gz")

    def test_exported_configuration_resolves_in_a_different_home(self) -> None:
        """The native config reader must consume deployment templates as-is."""
        with TemporaryDirectory(prefix="datasage-portable-config-") as temporary:
            workspace = Path(temporary)
            candidate = self._copy_candidate(workspace)
            home = workspace / "a different home"
            source_export.export_source(candidate, home)
            before = (home / "config.yaml").read_bytes()
            launcher = workspace / "deployment tools" / "language-server.cmd"
            launcher.parent.mkdir()
            launcher.write_text("@exit /b 0\n", encoding="utf-8")
            env = dict(os.environ)
            env.pop("HERMES_PROFILE", None)
            env.update({
                "HERMES_HOME": str(home),
                "PYRIGHT_LANGSERVER_COMMAND": str(launcher),
                "WECOM_HOME_CHANNEL": "synthetic-review-channel",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            })
            probe = """
import json
from hermes_cli.config import get_config_path, load_config_readonly
from agent.lsp.servers import SERVERS, ServerContext
config = load_config_readonly()
command = config['lsp']['servers']['pyright']['command']
server = next(item for item in SERVERS if item.server_id == 'pyright')
spec = server.build_spawn('.', ServerContext(workspace_root='.', install_strategy='manual', binary_overrides={'pyright': command}))
print(json.dumps({'config_path': str(get_config_path()), 'command': spec.command, 'channel': config['platforms']['wecom']['home_channel']}))
"""
            result = subprocess.run(
                [sys.executable, "-B", "-c", probe], cwd=home, env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual((home / "config.yaml").resolve(), Path(observed["config_path"]).resolve())
            self.assertEqual([str(launcher), "--stdio"], observed["command"])
            for field in ("chat_id", "name", "user_id"):
                self.assertEqual("synthetic-review-channel", observed["channel"][field])
            self.assertEqual(before, (home / "config.yaml").read_bytes())

    def test_flow_style_identity_and_inline_credential_are_rejected_or_redacted(self) -> None:
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            candidate = self._copy_candidate(Path(temporary))
            config_path = candidate / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["platforms"]["wecom"]["home_channel"] = {
                "platform": "wecom",
                "chat_id": "REAL_CHANNEL_ID",
                "name": "REAL_CHANNEL_ID",
                "user_id": "REAL_CHANNEL_ID",
            }
            config["platforms"]["wecom"]["extra"]["bot_id"] = 123456
            config["plugins"]["entries"]["datasage-query"]["settings"]["inline_password"] = 123456
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            with self.assertRaises(source_export.SensitiveContentError):
                source_export.export_source(candidate, Path(temporary) / "review.tar.gz")

    def test_windows_drive_relative_manifest_path_is_rejected(self) -> None:
        with self.assertRaises(source_export.UnsafePathError):
            source_export._relative_path("C:relative/file.py")

    def test_destination_inside_source_is_rejected_without_symlink_privileges(self) -> None:
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            candidate = self._copy_candidate(Path(temporary))
            with self.assertRaises(source_export.UnsafePathError):
                source_export.export_source(candidate, candidate / "inside.tar.gz")
            self.assertFalse((candidate / "inside.tar.gz").exists())

    def test_symlink_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            workspace = Path(temporary)
            candidate = self._copy_candidate(workspace)
            link = candidate / "README.md"
            original = candidate / "SOUL.md"
            link.unlink()
            try:
                link.symlink_to(original)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises(source_export.UnsafePathError):
                source_export.export_source(candidate, workspace / "symlink.tar.gz")

    def test_extracted_package_can_register_plugin_in_a_second_home(self) -> None:
        """Exercise the plugin import/contract path without DB/API/tool calls."""

        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            workspace = Path(temporary)
            candidate = self._copy_candidate(workspace)
            archive = workspace / "review.tar.gz"
            source_export.export_source(candidate, archive)
            extracted = workspace / "extracted"
            extracted.mkdir()
            with tarfile.open(archive, "r:gz") as handle:
                handle.extractall(extracted, filter="data")
            home = extracted / source_export.PACKAGE_NAME

            # The official Hermes venv is the supported test runtime.  The
            # fallback keeps this test truthful when a generic Python is used:
            # it reports a blocked dependency rather than faking registration.
            host_root = Path(sys.executable).resolve().parents[2]
            if (host_root / "agent").is_dir() and str(host_root) not in sys.path:
                sys.path.insert(0, str(host_root))
            try:
                import agent  # noqa: F401
            except ModuleNotFoundError:
                self.skipTest("official Hermes host package is not on this test Python path")

            plugin_root = home / "plugins/datasage-query"
            package_name = f"datasage_export_{uuid.uuid4().hex}"
            spec = importlib.util.spec_from_file_location(
                package_name,
                plugin_root / "__init__.py",
                submodule_search_locations=[str(plugin_root)],
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[package_name] = module

            class Context:
                def get_config(self, key, default=None):
                    return default

                def register_tool(self, **kwargs):
                    del kwargs

            try:
                with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                    spec.loader.exec_module(module)  # type: ignore[union-attr]
                    module.register(Context())
            finally:
                for name in tuple(sys.modules):
                    if name == package_name or name.startswith(package_name + "."):
                        sys.modules.pop(name, None)

    # -- R03: the exported package must close over its own imports ----------
    #
    # F01 reproduced a package whose promised price/report entry points still
    # referenced six modules the whitelist never copied.  Registration alone
    # cannot see that: the imports sit inside functions, so the artifact loads
    # and only fails when the operator path is used.  These tests enumerate the
    # produced artifact's own relative imports instead of trusting the list.

    PRICE_ENTRY_MODULES = (
        "price_reference.py",
        "price_workflow.py",
        "purchase_price_content.py",
        "purchase_price_runner.py",
        "sales_price_runner.py",
        "sales_reference.py",
    )
    PRICE_ENTRY_TESTS = (
        "test_price_reference.py",
        "test_price_workflow.py",
        "test_purchase_price_content.py",
        "test_purchase_price_recovery.py",
        "test_purchase_price_target_schema.py",
        "test_purchase_reference_entry.py",
        "test_purchase_webhook_transport.py",
        "test_sales_price_recovery.py",
        "test_sales_price_runner.py",
        "test_sales_reference.py",
        "test_sales_reference_entry.py",
    )

    @staticmethod
    def _init_bound_names(source: str) -> set[str]:
        """Names a package ``__init__`` binds, for ``from . import name``."""

        names: set[str] = set()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return names
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.Import):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names)
        return names

    def _unresolved_relative_imports(self, package_root: Path) -> list[str]:
        """Every internal relative import in the artifact must resolve."""

        unresolved: list[str] = []
        for path in sorted(package_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                if not base.is_dir() or package_root not in base.parents and base != package_root:
                    # A relative import that leaves the artifact is a different
                    # concern; this test only judges in-package closure.
                    continue
                if node.module:
                    module = base.joinpath(*node.module.split("."))
                    if module.with_suffix(".py").exists() or (module / "__init__.py").exists():
                        continue
                    unresolved.append(
                        f"{path.relative_to(package_root)}:{node.lineno} from .{node.module}"
                    )
                    continue
                init = base / "__init__.py"
                # ``from . import converters`` may name a module or a name the
                # package __init__ itself binds (e.g. pymysql.VERSION_STRING).
                allowed = self._init_bound_names(init.read_text(encoding="utf-8")) if init.exists() else set()
                for alias in node.names:
                    if alias.name in allowed:
                        continue
                    if (base / f"{alias.name}.py").exists() or (base / alias.name / "__init__.py").exists():
                        continue
                    unresolved.append(
                        f"{path.relative_to(package_root)}:{node.lineno} from . import {alias.name}"
                    )
        return unresolved

    def test_exported_package_has_no_broken_internal_relative_imports(self) -> None:
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            workspace = Path(temporary)
            candidate = self._copy_candidate(workspace)
            archive = workspace / "review.tar.gz"
            source_export.export_source(candidate, archive)
            extracted = workspace / "extracted"
            extracted.mkdir()
            with tarfile.open(archive, "r:gz") as handle:
                handle.extractall(extracted, filter="data")
            home = extracted / source_export.PACKAGE_NAME
            unresolved = self._unresolved_relative_imports(home / "plugins/datasage-query")
            self.assertEqual(unresolved, [])
            # The four edges F01 reproduced must be present in the artifact.
            for module in self.PRICE_ENTRY_MODULES:
                self.assertTrue((home / "plugins/datasage-query" / module).is_file())

    def test_price_entry_modules_and_their_tests_are_exported(self) -> None:
        with TemporaryDirectory(prefix="datasage-source-export-test-") as temporary:
            workspace = Path(temporary)
            candidate = self._copy_candidate(workspace)
            archive = workspace / "review.tar.gz"
            source_export.export_source(candidate, archive)
            with tarfile.open(archive, "r:gz") as handle:
                names = {member.name for member in handle.getmembers()}
            prefix = source_export.PACKAGE_NAME
            for module in self.PRICE_ENTRY_MODULES:
                self.assertIn(f"{prefix}/plugins/datasage-query/{module}", names)
            for test in self.PRICE_ENTRY_TESTS:
                self.assertIn(f"{prefix}/tests/{test}", names)


    # -- R29: the bundled driver must stay traceable and unmodified -----------

    def test_vendored_pymysql_matches_the_recorded_provenance(self):
        vendor_root = ROOT / "plugins/datasage-query/vendor"
        files = sorted(
            path
            for path in vendor_root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        digest = hashlib.sha256(
            "\n".join(
                f"{path.relative_to(vendor_root).as_posix()}\0"
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}"
                for path in files
            ).encode()
        ).hexdigest()
        recorded = (ROOT / "docs/vendor-provenance-20260923.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(25, len(files))
        self.assertIn(f"`{digest}`", recorded)
        self.assertIn("licenses/LICENSE", recorded)
        metadata = (vendor_root / "pymysql-1.2.0.dist-info/METADATA").read_text(
            encoding="utf-8"
        )
        self.assertIn("Name: PyMySQL", metadata)
        self.assertIn("Version: 1.2.0", metadata)
        self.assertGreater(
            (vendor_root / "pymysql-1.2.0.dist-info/licenses/LICENSE").stat().st_size,
            500,
        )


if __name__ == "__main__":
    unittest.main()
