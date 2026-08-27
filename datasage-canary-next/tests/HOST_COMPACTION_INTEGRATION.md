# Hermes host compaction integration boundary

`fixtures/host_compaction_ordering.json` is a regression fixture for the Hermes
host message assembler.  The DataSage Profile does not own compaction and must
not add a prompt hook, message rewrite, or Profile-local workaround.

The host integration test must force compaction, inspect the final model input,
and assert every item in `required_host_assertions`.  Before the corresponding
Hermes host fix, that host test is expected to fail.  The local Profile test
only validates that this contract is explicit and that DataSage has not claimed
ownership of the fix.  Release requires a pinned Hermes version for which the
host integration test passes.

## Machine-readable release evidence

`build_release_receipt.py --verify-candidate` treats the fixture as a release
gate, not as proof that the host test ran. The fixture must eventually contain
a `verification` object produced from the real pinned-host E2E run:

```json
{
  "verification": {
    "status": "passed",
    "hermes_version": "0.20.5",
    "required_assertions_passed": true
  }
}
```

Absent, failed, or differently versioned evidence keeps the candidate blocked.
The Profile must not synthesize this object from its local fixture test, because
that would turn an ownership boundary into a false host-integration pass.
