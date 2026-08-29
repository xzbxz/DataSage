# Hermes host-compaction E2E evidence contract

`fixtures/host_compaction_ordering.json` is a test-only pressure fixture for the
Hermes host message assembler. DataSage does not own compaction and must not add
a prompt hook, message rewrite, runtime plugin, planner, or Profile-local replay
implementation.

The producer is `tests/test_host_compaction_e2e.py`. It imports the pinned
Hermes host and executes the real in-process path:

```text
AIAgent.run_conversation
  -> build_turn_context / preflight compression
  -> AIAgent._compress_context
  -> ContextCompressor.compress
  -> final prompt assembly and transport kwargs
  -> AIAgent._interruptible_api_call (captured)
```

Construction isolation is explicit: the harness patches dotenv loading, config
loading, tool discovery/requirement checks, and the `OpenAI` client constructor;
it supplies an empty tool set and a test-only custom route. During the actual
turn, only two model boundaries are replaced: the auxiliary `call_llm` used to
generate summary text and the final `_interruptible_api_call` provider transport.
The test does not mock `_compress_context`, `ContextCompressor.compress`, prompt
assembly, or transport-kwargs construction. A temporary `HERMES_HOME` is
installed before importing `run_agent`; all socket connection attempts are
blocked. No real model, state database, session, Profile `.env`, database
account, or WeCom configuration is read or used. Progress output from
`run_conversation` is captured so CLI stdout remains pure JSON.

The producer is one-shot and must run in its own process. The set of importable
Hermes top-level names is derived from the pinned checkout itself: root `*.py`
stems plus root directories containing `__init__.py`, excluding `tests` and
`venv`. Startup fails if any module under one of those names is already loaded;
after the host imports, every such ordinary module (including submodules) must
have a verifiable `__file__` inside the pinned checkout. A namespace package
must instead have a non-empty `__path__` whose every entry is inside the pinned
checkout; entries with neither proof fail closed. The producer deliberately does not tear down or restore
Hermes' process-global modules: the formal CLI exits after one report, while
unittest launches the same producer in a hidden test-only child mode. The
parent test compares its complete `sys.modules` key set and every original
object identity before and after the child, and separately validates wrong-path
and namespace `model_tools` entries without importing Hermes in the parent.
Child stdout is JSON-only. Stderr is accepted only when empty or when it exactly
matches the known official Relay destructor failure; any other stderr fails the
test and is never hidden from the result.

This contract is grounded in the pinned official host implementation:

- [`AIAgent` and `run_conversation`](https://github.com/NousResearch/hermes-agent/blob/fcbd1076a93841fa88855acce810e342a5b78101/run_agent.py#L412-L435)
- [turn-start prompt assembly and preflight compression](https://github.com/NousResearch/hermes-agent/blob/fcbd1076a93841fa88855acce810e342a5b78101/agent/turn_context.py#L431-L1017)
- [`ContextCompressor.compress`](https://github.com/NousResearch/hermes-agent/blob/fcbd1076a93841fa88855acce810e342a5b78101/agent/context_compressor.py#L7045)
- [official compaction authority prefix](https://github.com/NousResearch/hermes-agent/blob/fcbd1076a93841fa88855acce810e342a5b78101/agent/context_compressor.py#L114-L148)
- [final non-streaming transport seam](https://github.com/NousResearch/hermes-agent/blob/fcbd1076a93841fa88855acce810e342a5b78101/agent/chat_completion_helpers.py#L3336-L3346)

## Assertion boundary

The four assertion IDs in the fixture are exact and closed. The fourth asserts
only what the official compaction prefix and final request ordering can prove:
the latest user message wins over the compaction summary, stale draft, and prior
correction. A conflict between persistent `MEMORY.md`/`USER.md` content and the
latest user message is deliberately outside this contract. The harness skips
persistent Memory, and neither a passing test nor its evidence may be described
as resolving that separate host-policy question.

## Raw evidence

The producer emits exactly `datasage-host-compaction-evidence/v1`:

```json
{
  "schema": "datasage-host-compaction-evidence/v1",
  "subject": {
    "name": "...",
    "version": "...",
    "content_sha256": "...",
    "profile_git_commit": "..."
  },
  "host": {
    "hermes_version": "...",
    "hermes_git_commit": "..."
  },
  "fixture": {"path": "tests/fixtures/host_compaction_ordering.json", "sha256": "..."},
  "producer": {"path": "tests/test_host_compaction_e2e.py", "sha256": "..."},
  "compression_count": 1,
  "captured_request_sha256": "...",
  "captured_request": {"model": "...", "messages": []}
}
```

It contains the complete JSON-serializable final provider kwargs and raw
compression count only: no assertions, `status`, `passed`, `eligible`, release
decision, or self-hash. `captured_request_sha256` is the canonical-JSON SHA-256
of `captured_request`. The release gate independently derives every assertion
from that request and hashes the raw report separately.

The Profile commit in `subject.profile_git_commit` is also the source of truth
for `fixture.sha256` and `producer.sha256`: both are hashes of that commit's Git
blobs, never hashes of mutable worktree files. Evidence generation requires both
paths to be tracked, the Profile-scoped worktree to be clean, the Hermes
worktree to be clean, and the live host version/commit to match the fixture's
`pinned_host` exactly. The development test may run the raw host chain before
the new producer is committed, but `build_evidence` correctly refuses to mint a
report until a subject-commit blob exists.

Running the test writes nothing:

```powershell
$env:HERMES_AGENT_ROOT = 'C:\Users\10192\AppData\Local\hermes\hermes-agent'
python -B -m unittest discover -s tests -p test_host_compaction_e2e.py -v
```

The same file can be used as a strict CLI. Without `--output` it prints only the
JSON report to stdout. The sole permitted file destination is the current HEAD
name below, and an existing file is never overwritten:

```powershell
python -B tests/test_host_compaction_e2e.py --output pending/evidence/host-compaction-<profile_git_commit>.json
```

Untracked source, a dirty Profile or Hermes worktree, host-pin mismatch,
infrastructure mismatch, import-time isolation failure, an attempted network
connection, zero real compressions, no final transport capture, non-canonical
request data, or an invalid output path fails closed. Fixture assertions remain
local test self-checks and never enter the raw report. The producer does not
copy host logic or downgrade a failure into synthetic evidence.
