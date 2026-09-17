"""Offline checks through current official Hermes APIs, with no host patches."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]


class HostCompatibilityTests(unittest.TestCase):
    def test_curator_is_disabled_through_the_official_reader(self):
        from agent import curator
        with patch.dict(os.environ, {'HERMES_HOME': str(ROOT)}):
            self.assertFalse(curator.is_enabled())

    def test_profile_pyright_override_completes_real_lsp_handshake(self):
        from agent.lsp.servers import SERVERS, ServerContext
        from agent.lsp.client import LSPClient
        config = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
        command = config['lsp']['servers']['pyright']['command']
        try:
            runtime_readable = Path(command[0]).is_file()
        except OSError:
            runtime_readable = False
        if not runtime_readable:
            self.skipTest('Optional installed Pyright runtime is not readable in this isolated environment; no installation performed.')
        server = next(item for item in SERVERS if item.server_id == 'pyright')
        with tempfile.TemporaryDirectory(prefix='datasage-lsp-check-') as temporary:
            context = ServerContext(workspace_root=temporary, install_strategy='manual', binary_overrides={'pyright': command})
            spec = server.build_spawn(temporary, context)
            self.assertIsNotNone(spec)
            self.assertEqual(command, spec.command)
            self.assertTrue(Path(spec.command[0]).is_file())
            async def check():
                client = LSPClient(server_id='pyright', workspace_root=temporary, command=spec.command, cwd=temporary)
                try:
                    await asyncio.wait_for(client.start(), timeout=15)
                    self.assertTrue(client.is_running)
                finally:
                    await asyncio.wait_for(client.shutdown(), timeout=5)
                self.assertFalse(client.is_running)
            with patch.dict(os.environ, {'HERMES_HOME': temporary}):
                asyncio.run(check())


if __name__ == '__main__': unittest.main()
