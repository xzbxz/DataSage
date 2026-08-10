# DataSage WeCom-equivalent offline E2E

This release gate replays 26 frozen, structured transcripts without a model,
network, or database. It validates three independent layers:

1. `process_exit`: exit code and timeout only;
2. `tool_success`: the configured `platform_toolsets.wecom` surface, public
   DataSage facade tools, and successful tool events;
3. `business_correct`: structured metric/domain/entity/period/scope oracles,
   answer-boundary assertions, numeric assertions, and same-session follow-up
   transitions.

An exit code of zero is never counted as business correctness. The two known
regressions are company-wide formal DSO (`7_ar_dso`) and the `hcm呢` follow-up
that must keep the inventory/slow-moving intent instead of switching to a
receipt target (`18_mt_region2`).

Run from the profile root:

```text
python plugins/datasage-query/e2e/runner.py --output workspace/e2e_offline_report.json
python -m unittest plugins/datasage-query/tests/test_offline_e2e.py
```

`--mode live-wecom` is an explicit opt-in placeholder and deliberately fails
closed because no live adapter is shipped. The offline release gate never
accesses the real database and never imports plugin internals.
