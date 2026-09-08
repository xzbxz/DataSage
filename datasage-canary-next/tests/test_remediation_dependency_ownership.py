from pathlib import Path
import unittest
import yaml
from plugin_registration_probe import probe_registration
PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"

class SingleOwnershipTests(unittest.TestCase):
    def test_pymysql_has_one_vendored_runtime_owner(self):
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("python_dependencies", manifest)
        self.assertFalse((PLUGIN_ROOT / "requirements.txt").exists())
        self.assertTrue((PLUGIN_ROOT / "vendor" / "pymysql" / "__init__.py").is_file())
        metadata = (
            PLUGIN_ROOT / "vendor" / "pymysql-1.2.0.dist-info" / "METADATA"
        ).read_text(encoding="utf-8")
        self.assertIn("Version: 1.2.0", metadata)
        module, _ = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_release_dependency_registration",
        )
        driver = module.db_runtime.load_pymysql()
        self.assertTrue(
            Path(driver.__file__).resolve().is_relative_to(
                (PLUGIN_ROOT / "vendor").resolve()
            )
        )
        self.assertEqual((1, 2, 0), tuple(driver.VERSION[:3]))
        self.assertFalse(hasattr(module.tools, "_load_pymysql"))

    def test_datasage_is_not_exposed_to_ownerless_cron(self):
        config = yaml.safe_load((PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("cron", config["platform_toolsets"])
