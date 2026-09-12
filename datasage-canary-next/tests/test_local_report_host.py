"""Real official no_agent/script subprocesses, temporary Profile, no job store/send."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from cron import scheduler

PROFILE=Path(__file__).resolve().parents[1]


class LocalReportHostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);(self.home/'scripts').mkdir()
        (self.home/'config.yaml').write_text('timezone: Asia/Shanghai\nplugins:\n  entries:\n    datasage-query:\n      settings:\n        max_concurrent_queries: 1\n',encoding='utf-8')
        self.token=set_hermes_home_override(str(self.home));self.addCleanup(reset_hermes_home_override,self.token)
        env=patch.dict(os.environ,{'HERMES_HOME':str(self.home),'PYTHONIOENCODING':'utf-8'});env.start();self.addCleanup(env.stop)

    def run_script(self,name,content):
        (self.home/'scripts'/name).write_text(content,encoding='utf-8')
        job={'id':'offline-synthetic','name':'offline-synthetic','script':name,'no_agent':True,
             'schedule':{'kind':'cron'},'workdir':str(self.home)}
        scope=scheduler._CronRunScope(job,job['id'],None)
        try:
            scope.enter()
            result=scheduler._run_no_agent_job(job,job['id'],job['name'],None)
        finally:scope.exit()
        self.assertEqual([], [p.name for p in (self.home/'cron').rglob('*') if p.is_file()],
                         'No jobs, executions, queue or run state may be written')
        return result

    def test_actual_home_interpreter_dependency_and_empty_session_context(self):
        result=self.run_script('probe.py', '''import os,sys,json
from hermes_constants import get_hermes_home
from gateway.session_context import get_session_env
import yaml
print(json.dumps({'home':str(get_hermes_home()),'python':sys.executable,'prefix':sys.prefix,'cwd':os.getcwd(),
 'yaml':yaml.__version__,'session':[get_session_env('HERMES_SESSION_'+k) for k in ['PLATFORM','SOURCE','USER_ID']]}))
''')
        self.assertTrue(result[0],result)
        proof=json.loads(result[2]);self.assertEqual(self.home.resolve(),Path(proof['home']).resolve())
        self.assertEqual(self.home.resolve(),Path(proof['cwd']).resolve());self.assertEqual(['','',''],proof['session'])
        self.assertTrue(Path(proof['python']).is_file());self.assertTrue(proof['yaml'])

    def test_installed_entry_unconfigured_fails_without_runtime_artifacts(self):
        content=(PROFILE/'scripts/datasage_slow_report.py').read_text(encoding='utf-8')
        result=self.run_script('datasage_slow_report.py',content)
        self.assertFalse(result[0]);self.assertIn('REPORT_NOT_CONFIGURED',result[3])
        self.assertFalse((self.home/'report_runs').exists())

    def test_empty_stdout_and_nonzero_exit_have_official_contract(self):
        result=self.run_script('silent.py','pass\n')
        self.assertTrue(result[0]);self.assertEqual(scheduler.SILENT_MARKER,result[2])
        failed=self.run_script('failed.py','raise SystemExit(9)\n')
        self.assertFalse(failed[0]);self.assertIn('code 9',failed[3])

    def test_synthetic_report_cli_through_official_script_stdout(self):
        # Only the DB/readiness boundary is replaced by the existing network-
        # blocked fixture. The report main config loader, compiler, wire,
        # artifacts and official script subprocess/exit/stdout run for real.
        tests=repr(str(PROFILE/'tests'))
        script=f'''import sys,json
from pathlib import Path
sys.path.insert(0,{tests})
from test_local_report import LocalReportTests,synthetic_bindings,report
from hermes_constants import get_hermes_home
test=LocalReportTests();test.setUp()
try:
 home=Path(get_hermes_home())
 (home/'local-report-bindings.json').write_text(json.dumps(synthetic_bindings(views=['flow_summary'])),encoding='utf-8')
 test.h.outgoing(10);test.h.returning(3)
 code=report.main(home,[])
finally:test.doCleanups()
raise SystemExit(code)
'''
        result=self.run_script('synthetic_report.py',script)
        self.assertTrue(result[0],result);self.assertIn('已记录净数量：7',result[2])
        evidence=list((self.home/'report_runs'/'slow').glob('*/report.json'))
        self.assertEqual(1,len(evidence))
        document=json.loads(evidence[0].read_text(encoding='utf-8'))
        self.assertEqual(str(self.home.resolve()),document['runtime']['profile_home'])
        self.assertEqual('local_os_operator',document['trust'])


if __name__=='__main__':unittest.main()
