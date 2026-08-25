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
