"""Official cron-compatible entrypoint. Unconfigured by default; no sending."""
from pathlib import Path
import importlib.util
import sys

PROFILE=Path(__file__).resolve().parents[1]


def main():
    # No sample bindings, environment overrides, synthetic identities or DB
    # initialization on the installed entrypoint's unconfigured path.
    if not (PROFILE/'local-report-bindings.json').is_file():
        print('REPORT_NOT_CONFIGURED',file=sys.stderr)
        return 2
    root=PROFILE/'plugins/datasage-query'
    name='datasage_local_operator'
    spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
    package=importlib.util.module_from_spec(spec);sys.modules[name]=package;spec.loader.exec_module(package)
    from importlib import import_module
    return import_module(name+'.local_report').main(PROFILE)


if __name__=='__main__':raise SystemExit(main())
