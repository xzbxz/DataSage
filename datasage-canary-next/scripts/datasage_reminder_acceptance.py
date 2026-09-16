from pathlib import Path
import sys,types,importlib

def main():
    if not sys.argv[1:]:print('ACCEPTANCE_ACTION_REQUIRED');return 2
    profile=Path(__file__).resolve().parents[1]
    package=types.ModuleType('datasage_reminder_operator');package.__path__=[str(profile/'plugins/datasage-query')]
    sys.modules[package.__name__]=package
    return importlib.import_module(package.__name__+'.reminder_acceptance').main(profile,sys.argv[1:])
if __name__=='__main__':raise SystemExit(main())
