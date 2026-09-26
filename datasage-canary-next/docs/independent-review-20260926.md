# Independent source audit and fixes · 2026-09-26

Batch: `independent-review-20260926`. This is an audit batch, not an upstream
Hermes release or a claim of live gateway deployment.

> Source provenance: this is the external V7 author's note. Its referenced
> input archive, external audit evidence and supplied patch were not included
> in the received V7 source ZIP. Local acceptance is recorded separately; the
> author's statements are not substituted for current integration results.

## Scope and retained boundaries

Input: `DataSage-codex-remediation-20260926.zip`, SHA256
`2103d9c5f8de7808ae16f5cef595085e70c0c03e66b3dbf3ae2a3b58b46fcab6`.
Input ZIP source marker: `c05d4c014e5ae43ce6e5544728413e7d33d2b819`.
The source marker is not independent confirmation of remote HEAD or running code.
Hermes continues to own cognition, task planning and final answers. No new
router, planner, reference loader, permissions platform or model-answer filter.

## Confirmed corrections

1. Catalog domain/view and entity domain validate string types before set/map
   membership. JSON containers now yield `INVALID_INPUT`, not `INTERNAL_ERROR`.
   Invalid requests still fail before database I/O; valid and pending requests
   retain their existing behavior. No coercion, privilege or schema expansion.
2. The optional shared method uses the exact metric's currency basis and
   effective grain. Original-currency-only pattern amounts are not turned into
   RMB, and an empty dimensions list need not mean one overall aggregate.
3. Receipt method refers to the public dimension `currency`; the physical
   column name is not a valid caller grouping key. Internal SQL mappings remain.
4. Domain stop conditions apply to the unsupported claim or comparison, not
   all independent branches. Missing credit/cost blocks dependent verdicts,
   not independently supported aging, quantity or roll observations.
   Missing optional expenses block unsupported complete-expense/net-profit
   claims, not independently supported recorded gross-margin observations.
   Missing required numerator/denominator and zero denominators remain undefined.

These method corrections align with the existing machine contracts. They are
not independent proof of live model consumption or improved expert performance.
All existing metric contracts, formulas, SQL construction, SOUL, tool schemas,
configuration, credentials and operational permissions stay unchanged.

## Verification and limitations

`tests/test_independent_contract_boundaries_20260926.py` provides synthetic
request-boundary and actual generated-SQL oracles. Private namespace loading
only isolates pure implementations; it is not fake Hermes integration.
SQLite is used only for compatible SELECTs with placeholder substitution, not
for full MySQL certification. Pattern default grain is checked at the actual
builder and scope output, not by executing its MySQL-specific query in SQLite.
Prose remains subject to semantic review, not exact-wording format blacklists.

The accompanying audit evidence includes baseline/final module results,
independent probes and manifests. Unavailable official modules, Windows fonts,
real DB/model/gateway and private operator bindings are not counted as passed.
An external report records exact counts; this source note does not fabricate
successful test totals before the final candidate is exercised.

## Maintenance and use

The new test and this note are explicitly included in Git/export allowlists.
Static context and current source-surface measurements are updated; recorded
historical Git statistics and pending business verifications are not promoted.
These byte/line measurements are not token, latency or quality gains.

Review and apply the supplied patch in a matching experimental source branch.
Do not overwrite a live Hermes Home, state, sessions, Memory or secrets.
In the supported official Python/host environment, run the new regression
module, all existing tests, then real channel and independent business checks.
Production restart, sending, scheduling and approval changes require separate
authorization. Roll back only this batch's changed source, not user state.
