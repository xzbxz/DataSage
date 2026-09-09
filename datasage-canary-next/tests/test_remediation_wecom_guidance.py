"""Check effective provider system input, not model compliance.

Reuses the official-host offline transport harness. No live model calls;
passing means rules reach a provider request, never that answers follow them.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_host_compaction_e2e as host


class WeComGuidanceAssemblyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env=dict(os.environ,HERMES_AGENT_ROOT=str(host._host_root()),PYTHONDONTWRITEBYTECODE='1')
        run=subprocess.run([sys.executable,'-B',str(host.PRODUCER_PATH),'--_test-raw-child'],
            cwd=host.PROFILE_ROOT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        payload=json.loads(run.stdout)
        cls.system='\n'.join(host._content_text(m.get('content'))
            for m in payload['captured_request']['messages'] if m.get('role')=='system')

    def test_scope_rules_reach_provider_after_host_assembly(self):
        self.assertIn('Separate identity-bearing entities from population and output constraints',self.system)
        self.assertIn('Confirming a dimension is not confirmation of its literal value',self.system)



if __name__=='__main__':unittest.main()
