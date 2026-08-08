from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from evaluation import current_runtime_launcher as launcher


@unittest.skipUnless(os.name == "nt", "Windows extended paths only")
class WindowsLongPathStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dsrt-long-path-"))

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(
                launcher._extended_path_within(self.root, self.root),
                ignore_errors=True,
            )

    def _deep_file(self, root: Path) -> Path:
        path = root
        for index in range(12):
            path /= f"schema-segment-{index:02d}-abcdefgh"
        return path / "shared-documentPropertiesVariantTypes.xsd"

    def test_stage_copy_succeeds_for_path_over_260_characters(self) -> None:
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        deep = self._deep_file(source)
        os.makedirs(
            launcher._extended_path_within(deep.parent, source),
            exist_ok=True,
        )
        with open(
            launcher._extended_path_within(deep, source),
            "wb",
        ) as handle:
            handle.write(b"schema")
        self.assertGreater(len(str(deep)), 260)
        launcher._copytree_within_roots(
            source,
            target,
            source_root=source,
            target_root=self.root,
        )
        copied = self._deep_file(target)
        with open(
            launcher._extended_path_within(copied, target),
            "rb",
        ) as handle:
            self.assertEqual(handle.read(), b"schema")

    def test_extended_path_rejects_escape(self) -> None:
        allowed = self.root / "allowed"
        allowed.mkdir()
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            launcher._extended_path_within(
                allowed / ".." / "outside" / "payload",
                allowed,
            )

    def test_failed_stage_cleanup_removes_deep_tree(self) -> None:
        staging = self.root / "staging"
        staging.mkdir()
        operation = staging / "operation"
        deep = self._deep_file(operation)
        os.makedirs(
            launcher._extended_path_within(deep.parent, staging),
            exist_ok=True,
        )
        with open(
            launcher._extended_path_within(deep, staging),
            "wb",
        ) as handle:
            handle.write(b"partial")
        launcher._remove_tree_within_root(operation, staging)
        self.assertFalse(operation.exists())


if __name__ == "__main__":
    unittest.main()
