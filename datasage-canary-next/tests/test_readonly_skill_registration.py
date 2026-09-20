"""Offline evidence for the public plugin lifecycle boundary around read-only Skills.

This test does not load the DataSage plugin into an active Profile, edit config,
or expose tools to WeCom.  It proves what PluginContext can scope and unload,
and why the existing built-in ``skill_view``/``skills_list`` names cannot be
re-exported into a new toolset without the host override capability.
"""

from __future__ import annotations

from pathlib import Path
import unittest


class ReadonlySkillRegistrationTests(unittest.TestCase):
    @staticmethod
    def _context(scope: str):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

        manager = PluginManager(scope_key=scope)
        manifest = PluginManifest(
            name="datasage-readonly-registration-probe",
            key="datasage-readonly-registration-probe",
            source="user",
        )
        return PluginContext(manifest, manager), manager

    @staticmethod
    def _schema(name: str) -> dict:
        return {"name": name, "parameters": {"type": "object", "properties": {}}}

    def test_public_context_registers_arbitrary_toolset_with_scoped_unload(self):
        from tools.registry import registry

        scope = str((Path.cwd() / "readonly-registration-scope").resolve())
        context, manager = self._context(scope)
        name = "datasage_readonly_registration_probe"
        previous = registry.snapshot_registration(name, scope=scope)

        try:
            handle = context.register_tool(
                name=name,
                toolset="datasage-skills-readonly",
                schema=self._schema(name),
                handler=lambda args, **kwargs: "probe",
            )
            self.assertIsNotNone(handle)
            entry = registry.snapshot_registration(name, scope=scope)
            self.assertIsNotNone(entry)
            self.assertEqual("datasage-skills-readonly", entry.toolset)
            self.assertEqual("probe", entry.handler({}))

            handle.dispose()
            self.assertIs(registry.snapshot_registration(name, scope=scope), previous)
            self.assertNotIn(name, manager._plugin_tool_names)
        finally:
            current = registry.snapshot_registration(name, scope=scope)
            if current is not previous:
                registry.restore_registration(name, current, previous, scope=scope)

    def test_scope_controls_visibility_between_profile_registries(self):
        from tools.registry import registry

        scope_a = str((Path.cwd() / "readonly-registration-scope-a").resolve())
        scope_b = str((Path.cwd() / "readonly-registration-scope-b").resolve())
        context, _manager = self._context(scope_a)
        name = "datasage_readonly_scope_probe"
        previous = registry.snapshot_registration(name, scope=scope_a)
        try:
            handle = context.register_tool(
                name=name,
                toolset="datasage-skills-readonly",
                schema=self._schema(name),
                handler=lambda args, **kwargs: "scope-a",
            )
            self.assertIsNotNone(handle)
            self.assertIsNotNone(registry.snapshot_registration(name, scope=scope_a))
            self.assertIsNone(registry.snapshot_registration(name, scope=scope_b))
            handle.dispose()
        finally:
            current = registry.snapshot_registration(name, scope=scope_a)
            if current is not previous:
                registry.restore_registration(name, current, previous, scope=scope_a)

    def test_builtin_skill_names_cannot_be_reexported_without_override(self):
        from hermes_cli.plugins import PluginContext
        from tools import skills_tool  # noqa: F401 — materialize official built-ins
        from tools.registry import registry

        scope = str((Path.cwd() / "readonly-registration-collision").resolve())
        context, manager = self._context(scope)
        for name in ("skills_list", "skill_view"):
            with self.subTest(name=name):
                self.assertIsNotNone(registry.get_entry(name, scope=scope))
                handle = context.register_tool(
                    name=name,
                    toolset="datasage-skills-readonly",
                    schema=self._schema(name),
                    handler=lambda args, **kwargs: "must-not-shadow",
                )
                self.assertIsNone(handle)
                self.assertNotIn(name, manager._plugin_tool_names)
                self.assertIsNone(registry.snapshot_registration(name, scope=scope))

if __name__ == "__main__":
    unittest.main()
