"""IDK public entry recovery must not depend on another business query."""
from datetime import datetime,timezone,timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import importlib,unittest
import test_business_contracts as base
from test_idk_delivery import FakeTransport,_roles,_target_map,_document
io=importlib.import_module(base.TEST_PACKAGE+'.workflow_io')
local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
contract=importlib.import_module(base.TEST_PACKAGE+'.contract_store')

class IdkEntryTests(unittest.TestCase):
    def test_sealed_current_week_recovers_without_database_or_role_read(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);out=root/'initial';out.mkdir()
            now=datetime.now(timezone(timedelta(hours=8)));y,w,_=now.isocalendar();week=f'{y}-W{w:02d}'
            binding={'enabled':True,'read_enabled':True,'send_enabled':True,'target_map':_target_map(),'recipients_file':'local/workflow-roles.json'}
            transport=FakeTransport()
            with patch.object(io,'read_recipients',return_value=_roles()),patch.object(io.operations,'execute',return_value=_document()):
                io._produce_and_execute(root,'idk',binding,out,week,now.strftime('%Y-%m'),io.Progress(root,'idk',week),None,transport,None)
            sent=len(transport.sends)
            def forbidden():raise AssertionError('sealed retry contacted source')
            with patch.object(contract,'profile_root',return_value=root),patch.object(io,'load_activation',return_value=binding),patch.object(local,'configure_runtime'),patch.object(io,'read_recipients',side_effect=AssertionError('reread roles')),patch.object(io.operations,'execute',side_effect=AssertionError('reobserved')):
                result=io.run_bound(root,'idk',transport=transport,snapshot_factory=forbidden)
            self.assertEqual('previous_provider_acceptance_reused',result['delivery_state'])
            self.assertEqual(sent,len(transport.sends))

    def test_schedule_is_user_approved_local_contract(self):
        self.assertEqual('0 12 * * 1',io.wf.policy()['jobs']['idk']['schedule'])

if __name__=='__main__':unittest.main()
