# pytest on Windows with hyphenated plugin packages

## Symptoms

- `pytest plugins/<pkg>/tests` (default prepend mode) → dozens of
  `ERROR ... attempted relative import with no known parent package` pointing at
  the plugin `__init__.py` (`from . import contracts, entities, ...`).
- `python -m unittest discover` fails with `Start directory is not importable`
  for hyphenated dirs like `datasage-query`.
- `PermissionError: [WinError 5] ... Temp\pytest-of-<user>` on Windows — pytest's
  default basetemp path is blocked.

## Working pytest.ini (profile root)

```ini
[pytest]
testpaths = plugins/datasage-query/tests
addopts = --import-mode=importlib -p no:cacheprovider
```

KEY GOTCHA: on pytest 9.0.2, `import-mode = importlib` as an ini KEY is silently
ignored (still 94 errors); the SAME value as a CLI flag (`--import-mode=importlib`)
or inside `addopts` works. Put it in `addopts`.

## Basetemp fix

```bash
python -m pytest -q --basetemp=C:/Users/<user>/AppData/Local/Temp/hermes-pytest
```

## Test/runtime boundary

Use the committed pytest suite for module-level verification. Its internal
loading mechanics are test implementation details and must not be copied into
ad-hoc scripts. Live verification must go through the normal authenticated
Hermes/DataSage facade after runtime readiness reports `ready=True`; if
identity readiness fails, stop and repair the release identity first.

## Known-good baseline

Full suite: `93 passed + 1 skipped + 50 subtests`. The single skip is
`test_playbook_and_evaluation_vocabularies_match_plugin` when
`evaluation/expert-core/cases.yaml` is absent from the installed profile —
the test calls `self.skipTest("evaluation corpus not present in this deployment")`.
Do NOT copy `evaluation/` into the profile to force it green: that flips
`db_security._profile_deployment_role` to "source" and revokes the canary
account exception (see main SKILL.md pitfall #5).
