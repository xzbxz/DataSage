"""Operator identity diagnostics must never turn into DB/readiness initialization."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_business_contracts as base


class ProfileDiagnosticsTests(unittest.TestCase):
    def test_registration_log_identifies_emitting_process_and_pinned_contract(self):
        health=base.runtime_health
        snapshot={'current_process_contract_snapshot_loaded':True,'current_process_contract_snapshot_sha256':'pinned',
                  'profile_git_head_on_disk':'disk','running_gateway_loaded_revision':'not_observed','credentials_read':False}
        with patch.object(health,'source_diagnostics',return_value=snapshot),patch.object(health.os,'getpid',return_value=1234),self.assertLogs(health.__name__,level='INFO') as logs:
            health.record_plugin_initialization(self.test_registration_log_identifies_emitting_process_and_pinned_contract.__code__)
        value=json.loads(logs.output[0].split('DATASAGE_PLUGIN_INITIALIZED ',1)[1])
        self.assertEqual(value['pid'],1234)
        self.assertEqual(value['current_process_contract_snapshot_sha256'],'pinned')
        self.assertEqual(value['running_gateway_loaded_revision'],'not_observed')
        self.assertEqual(len(value['registration_code_sha256']),64)

    def test_registration_diagnostic_failure_is_bounded_and_nonfatal(self):
        health=base.runtime_health
        with patch.object(health,'source_diagnostics',side_effect=RuntimeError('SENSITIVE_DO_NOT_LOG')),self.assertLogs(health.__name__,level='WARNING') as logs:
            health.record_plugin_initialization(self.test_registration_diagnostic_failure_is_bounded_and_nonfatal.__code__)
        self.assertNotIn('SENSITIVE_DO_NOT_LOG',''.join(logs.output))
        self.assertIn('error_type=RuntimeError',''.join(logs.output))

    def test_disk_diagnostic_distinguishes_disk_from_gateway_without_secret_reads(self):
        health=base.runtime_health
        with TemporaryDirectory() as directory:
            root=Path(directory)
            plugin=root/'plugins/datasage-query';(plugin/'contracts').mkdir(parents=True)
            for path,content in [(root/'SOUL.md','soul'),(root/'profile.yaml','description: synthetic'),(plugin/'plugin.yaml','name: synthetic'),(plugin/'tools.py','pass'),(plugin/'contracts/a.yaml','version: synthetic'),(root/'.env','SYNTHETIC_NOT_TO_BE_READ=secret')]:
                path.write_text(content,encoding='utf-8')
            seen=[];original_read=Path.read_bytes
            def observe(path):
                seen.append(path)
                if path.name=='.env':raise AssertionError('diagnostic must not read credentials')
                return original_read(path)
            snapshot={'loaded':True,'manifest_digest':'pinned-before-disk-edit'}
            with patch.object(health,'_profile_root',return_value=root),patch.object(health.contract_store,'contract_snapshot_status',return_value=snapshot),patch.object(health.contract_store,'pin_contract_snapshot',side_effect=AssertionError('must not initialize')),patch.object(health,'database_configuration_status',side_effect=AssertionError('must not inspect credentials/DB')),patch.object(health.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')),patch.object(Path,'read_bytes',observe):
                first=health.source_diagnostics()
                (plugin/'contracts/a.yaml').write_text('version: changed',encoding='utf-8')
                second=health.source_diagnostics()
            self.assertEqual(first['running_gateway_loaded_revision'],'not_observed')
            self.assertEqual(first['current_process_contract_snapshot_sha256'],second['current_process_contract_snapshot_sha256'])
            self.assertNotEqual(first['disk_contract_files_sha256'],second['disk_contract_files_sha256'])
            self.assertNotIn(root/'.env',seen)
            self.assertNotIn('secret',json.dumps(second))
            self.assertNotIn(str(root),json.dumps(second))
            self.assertFalse(second['database_connection_attempted'])


if __name__=='__main__':unittest.main()
