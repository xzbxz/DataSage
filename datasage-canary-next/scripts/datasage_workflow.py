"""Run the installed Profile workflow with its adjacent official Hermes runtime."""
from pathlib import Path
import os,sys,types,importlib

def main():
    profile=Path(__file__).resolve().parents[1]
    official=profile.parents[1]/'hermes-agent'
    if not (official/'hermes_constants.py').is_file():raise RuntimeError('ADJACENT_OFFICIAL_HERMES_REQUIRED')
    os.environ['HERMES_HOME']=str(profile)
    sys.path.insert(0,str(official))
    package=types.ModuleType('datasage_installed_workflow');package.__path__=[str(profile/'plugins/datasage-query')]
    sys.modules[package.__name__]=package
    try:return importlib.import_module(package.__name__+'.workflow_runner').main(profile,sys.argv[1:])
    except Exception as exc:
        # Exceptions from adapters can contain connection details; only bounded codes leave the entry.
        code=str(exc) if isinstance(exc,(ValueError,RuntimeError)) and str(exc).replace('_','').isalnum() else type(exc).__name__
        print('WORKFLOW_FAILED:'+code);return 1
if __name__=='__main__':raise SystemExit(main())
