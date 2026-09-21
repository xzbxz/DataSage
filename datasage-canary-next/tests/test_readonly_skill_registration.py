"""Offline evidence for the public plugin lifecycle boundary around read-only Skills.

This test does not load the DataSage plugin into an active Profile, edit config,
or expose tools to WeCom.  It proves what PluginContext can scope and unload,
and why the existing built-in ``skill_view``/``skills_list`` names cannot be
re-exported into a new toolset without the host override capability.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


class ReadonlySkillRegistrationTests(unittest.TestCase):
    def test_official_skills_read_and_stage_writes_with_real_profile_config(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from tools import skills_tool, skill_manager_tool, write_approval
        from toolsets import resolve_toolset

        with TemporaryDirectory(prefix="datasage-native-skills-") as directory:
            home = Path(directory)
            (home / "config.yaml").write_text(
                "skills:\n  write_approval: true\n  inline_shell: false\n", encoding="utf-8")
            skill = home / "skills" / "native-probe"
            skill.mkdir(parents=True)
            body = "---\nname: native-probe\ndescription: Synthetic native read.\n---\nOriginal body\n"
            (skill / "SKILL.md").write_text(body, encoding="utf-8")
            token = set_hermes_home_override(home)
            try:
                with patch.dict(os.environ, {"HERMES_HOME": str(home)}), \
                     patch.object(Path, "home", return_value=home), \
                     patch.object(skills_tool, "SKILLS_DIR", home / "skills"), \
                     patch.object(skill_manager_tool, "SKILLS_DIR", home / "skills"), \
                     patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
                     patch("sqlite3.connect", side_effect=AssertionError("database forbidden")):
                    self.assertEqual({"skills_list", "skill_view", "skill_manage"}, set(resolve_toolset("skills")))
                    read = json.loads(skills_tool.skill_view("native-probe"))
                    self.assertTrue(read["success"])
                    self.assertIn("Original body", read["content"])
                    result = json.loads(skill_manager_tool.skill_manage(
                        action="create", name="pending-probe", content="Synthetic proposed skill"))
                    self.assertTrue(result["staged"])
                    self.assertIsNotNone(write_approval.get_pending(write_approval.SKILLS, result["pending_id"]))
                    self.assertFalse((home / "skills" / "pending-probe").exists())
                    self.assertEqual(body, (skill / "SKILL.md").read_text(encoding="utf-8"))
            finally:
                reset_hermes_home_override(token)

    def test_official_inline_shell_is_disabled_by_profile_configuration(self):
        from agent.skill_preprocessing import preprocess_skill_content
        content = "Inspect this literal: !`echo should-not-execute`"
        with patch("agent.skill_preprocessing.run_inline_shell", side_effect=AssertionError("shell forbidden")):
            self.assertEqual(content, preprocess_skill_content(
                content, None, skills_cfg={"inline_shell": False}))

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
