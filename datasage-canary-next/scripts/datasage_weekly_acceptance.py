"""Fixed local-only HCM weekly acceptance. No args means no I/O."""
from pathlib import Path
import sys,types,importlib

def main():
    if sys.argv[1:]!=['--read-hcm-2026-w38']:
        print('ACCEPTANCE_READ_NOT_ENABLED');return 2
    profile=Path(__file__).resolve().parents[1]
    package=types.ModuleType('datasage_weekly_local_operator')
    package.__path__=[str(profile/'plugins/datasage-query')]
    sys.modules[package.__name__]=package
    return importlib.import_module(package.__name__+'.weekly_acceptance').main(profile,sys.argv[1:])

if __name__=='__main__':raise SystemExit(main())
