# Changelog

## 0.12.0-dev1 - 2026-07-31

- Reset the Profile onto the native Hermes 0.19 agent lifecycle. Hermes now
  owns intent understanding, multi-turn history, planning, ordinary
  conversation, tool selection, and final-answer composition.
- Reduced the DataSage plugin to three explicit, stateless evidence tools:
  `datasage_catalog`, `datasage_entity_resolve`, and `datasage_query`.
  Removed plugin-owned conversation hooks, routing middleware, turn scope,
  grounding gates, hidden dialogue state, and mandatory first-turn calls.
- Restored Hermes bundled Skills and the CLI `hermes-cli` composite so the
  Profile retains native conversation, Skill, memory, delegation, browsing,
  file, terminal, and code-execution capabilities where their runtime
  prerequisites are available.
- Made platform boundaries explicit: CLI combines the native Hermes toolset
  with DataSage, while WeCom keeps conversational, browsing, memory, Skill,
  delegation, and DataSage capabilities without host terminal, file, code, or
  computer-control tools.
- Moved all non-secret DataSage behavior into `config.yaml`; `.env` now carries
  only provider credentials and deployment connection coordinates. Enabled
  30-day Hermes session retention with automatic pruning and kept application
  log rotation size-based.
- Replaced legacy conversation-control documentation and contracts with one
  Hermes-native architecture, one semantic authority chain, and local
  tool-failure handling. No compatibility mode or legacy control-plane path is
  retained.
- Removed skipped final-renderer test tombstones and their dead helper path;
  disclosure contracts remain covered at the governed query layer while final
  language rendering is tested through Hermes-native behavior gates.

## 0.11.0-dev56 - 2026-07-31

- Rebuilt the conversation control plane around canonical Hermes 0.19 history
  frames. Complete assistant tool-call and linked tool-result chains no longer
  poison the next ordinary turn; malformed, orphaned, interrupted, duplicated,
  or cross-turn chains remain fail-closed.
- Requires supplied Hermes history to end with the exact current user frame,
  requires linked tool results to name their tool, and accumulates an
  irreversible prior-data semantic bit before canonical display projection.
  Long valid tool chains can no longer erase a data follow-up obligation.
- Replaced model-authored conversation prose with closed positive request
  classes and deterministic trusted rendering. Unknown and mixed wording
  fails closed to the governed route; identity, current model/platform and
  capability claims cannot come from model memory.
- Closed conversation clauses may compose across reviewed punctuation and
  connectors only when every clause is independently classified; the most
  specific typed action wins, while any unknown or business clause fails
  closed for the whole turn.
- Added a versioned Profile capability contract and ephemeral conversation-only
  runtime context; capability IDs render through a positive allowlist, and the
  contract is checked against the packaged plugin tools and skills. Governed
  and mixed turns receive no capability sidecar.
- Made the turn-scope JSON Schema and handler share route constants and the
  same route-dependent obligation cardinality.
- Reworded the terminal integrity fallback so a conversation protocol failure
  is no longer mislabeled as a data-consistency failure.
- Added real Hermes multi-turn protocol regressions covering linked and
  malformed tool frames, model identity, differentiated guidance, data
  follow-ups, mixed requests and history projection.

## 0.11.0-dev55 - 2026-07-31

- Resolve the single active turn by session when Hermes 0.19 plugin dispatch
  omits `turn_id` from handler kwargs while hooks and middleware retain it.
  Multiple matching states remain fail-closed.
- Add the live identity prompt as a dispatch-shape regression and record the
  internal scope-validation reason in operator logs.

## 0.11.0-dev54 - 2026-07-31

- Disable DeepSeek V4 thinking for the tool-using DataSage profile because the
  live provider rejects thinking mode together with the mandatory first-turn
  `tool_choice`. Preserve deterministic scope enforcement rather than making
  the safety-critical scope tool optional.
- Add a regression that pins the live provider/model, explicit non-thinking
  mode and required first-turn scope-tool invariant.

## 0.11.0-dev53 - 2026-07-30

- Restore ordinary conversation as a first-class, deterministic path: every
  turn is scoped before other tools, reviewed renderers answer social,
  identity, capability, guidance and clarification turns, and model-authored
  conversation text cannot inject internal operating claims.
- Keep `datasage_query` visible in the official Hermes registry and quiet-tool
  cache even when database readiness is missing. Runtime readiness now fails
  per request with stable public error codes before any business SQL.
- Bind the exact user input to versioned domain requirement slots, contract
  coverage, one obligation per evidence request and final dispositions.
  Missing sub-questions, multi-obligation query claims, successful-contract
  early exits and factual resolved-entity early exits fail closed.
- Add official Hermes 0.19 registry/cache verification, semantic adversarial
  cases, readiness normalization checks and full turn-scope regressions.

## 0.11.0-dev52 - 2026-07-30

- Project goal-specific evidence requirements into the trusted planning
  contract and enforce them before query admission: comparison and diagnosis
  require a dimensionless overall change, linked decompositions remain in the
  same batch, decisions and judgments reject rank-only or single-plain-fact
  evidence, and strict rankings retain their exact bounded shape.
- Bind every governed request ID to a semantic fingerprint, reject duplicate
  intent even when callers change IDs, and separate the ten-request primary
  work budget from one exact retry of explicitly retryable failures. A pending
  retry must be immediate and the retry is terminal, keeping admission and
  final evidence resolution on one state-machine invariant.
- Move capability-audit success logging after the final output-size delivery
  gate so rejected oversized responses cannot be recorded as delivered
  capability.
- Correct governed return-rate units to permit values above one and align the
  public percentage disclosure with the executable metric semantics.
- Mark eight source-unit-unsafe quantity metrics and their derived quantity
  return rate pending validation until executable unit filtering or grouping
  exists; keep amount and roll-count overview routes available.
- Remove only the cross-ledger salesperson dimension from delivery-versus-
  receipt comparison until transaction and receipt ownership roles reconcile;
  customer, department and organization comparisons remain available.
- Move DataSage identity fields into standards-compliant Skill metadata while
  preserving the signed version and content-hash projection.
- Add natural-language and runtime regressions for evidence shape, semantic
  deduplication, full-budget retry, audit timing and rate-unit consistency.

## 0.11.0-dev51 - 2026-07-30

- Replaced history-dependent Hermes bundles with a trust-bound exact Git-tree
  archive after the real dev50 rehearsal proved the tagged Hermes source is a
  shallow repository whose bundle cannot be fetched into a clean bare repo.
- Materialize the verified tree directly into a read-only, Git-metadata-free
  runtime directory; activation recomputes the Git tree OID and lockfile hash
  and rejects extra files, reparse points and post-execution mutations.
- Batched exact Git blob reads so a 7,854-file Hermes tree no longer requires
  one Git process per file.
- Added shallow-source, exact-tree, archive-tamper and materialized-tree
  regressions. The rejected dev50 units remain preserved as failure evidence.

## 0.11.0-dev50 - 2026-07-30

- Replaced platform-sensitive `git archive` export with deterministic
  per-blob ZIP construction so Windows EOL/export filters cannot change the
  committed Hermes bytes after their identity has been recorded.
- Added a regression that applies a CRLF archive filter to `uv.lock` and
  proves the runtime unit still contains the exact LF Git blob and matching
  SHA-256 identity.
- Kept the dev49 runtime units as rejected evidence: their declared lockfile
  hash was correctly blocked before staging because the ZIP payload had been
  converted to CRLF.

## 0.11.0-dev49 - 2026-07-30

- Serialized immutable-build test shards after the dev48 four-worker gate
  still produced Windows `0xC000013A` interruptions in concurrent Git
  subprocesses; the same complete suite is stable when executed serially.
- Tightened the release-gate regression to require one worker on this release
  line.

## 0.11.0-dev48 - 2026-07-30

- Bounded immutable-build release-gate concurrency to four workers after the
  dev47 candidate was correctly rejected when unrestricted parallel Git and
  runtime materialization tests exceeded stable Windows process capacity.
- Added a regression that keeps release-gate parallelism resource bounded.

## 0.11.0-dev47 - 2026-07-30

- Added a manifest-bound, network-denying atomic-unit health probe that loads
  the exact materialized Hermes checkout, registers the DataSage plugin and
  validates all six governed planner contracts without database, model or
  WeCom access.
- Added explicit trusted-root provisioning, hardened atomic staging,
  CURRENT-aware control leases and executable init/status/stage/verify/
  activate/health/rollback commands.
- Upgraded runtime rehearsal evidence to bind four activation and health
  steps (`baseline -> forward -> rollback -> roll_forward`) to exact Profile
  and Hermes identities, hash every child receipt and explicitly restore the
  known-good unit after a failed forward health check.
- Restricted offline-health subprocesses to the launcher interpreter with
  `-I -B`, a fixed unit-contained probe, a sensitive-environment denylist and
  an operator-trusted interpreter hash.

## 0.11.0-dev46 - 2026-07-29

- Fixed cross-request disclosure merging so one governed conditional clause may
  apply to one inventory scope and not another without invalidating the sealed
  answer; the clause is emitted once when any independently sealed request
  requires it, while mode or text conflicts still fail closed.
- Added a multi-scope natural-language regression and order-independent typed
  disclosure coverage for total and available inventory in one answer.
- Promoted all 18 remaining business-significant legacy answer notes to safe,
  mandatory disclosures while retaining physical fields and formulas only in
  private contract clauses; tests now reject any available metric that has an
  answer note without a public required disclosure.

## 0.11.0-dev45 - 2026-07-29

- Extended runtime deployment-unit and CURRENT-pointer checks from symbolic
  links to all Windows reparse aliases.
- Corrected the no-symlink-privilege test path to simulate the same hardened
  reparse detector used by production code.

## 0.11.0-dev44 - 2026-07-29

- Added an exact-tag Hermes Git bundle to each atomic runtime unit and verify
  its commit, tree and lockfile before activation.
- Materialize a clean, read-only Hermes checkout for the selected CURRENT
  unit and force child imports through that checkout without identity
  overrides.
- Materialize the immutable Profile under its exact
  `datasage-canary-next` deployment name so Canary database policy remains
  fail-closed rather than being weakened for fixture paths.
- Added tamper, missing-tag, Windows cleanup and real child-import coverage;
  formal promotion still requires an installed Gateway rollback rehearsal.

## 0.11.0-dev43 - 2026-07-29

- Replaced file-level release sharding with isolated unittest case-ID
  discovery and thirty-two shards, eliminating single-file latency tails.
- Release discovery now rejects empty, malformed or duplicate case IDs before
  execution, and coverage tests prove every discovered case is assigned once.

## 0.11.0-dev42 - 2026-07-29

- Increased isolated release-test partitioning from four to eight processes
  after the deployment runner's measured 20-second lifecycle limit, without
  changing the test inventory or fail-closed behavior.

## 0.11.0-dev41 - 2026-07-29

- Preserved the complete fail-closed release test set while partitioning every
  checked-in unittest module exactly once across four isolated processes.
- Added coverage proving the parallel release gate neither omits nor
  duplicates test modules. Any shard failure still blocks the immutable build.

## 0.11.0-dev40 - 2026-07-29

- Replaced model-visible query rows and physical execution context with a
  minimal typed wire backed by sealed claims, fingerprints and public
  disclosures; private fields remain available only to execution audit.
- Added safe, deterministic entity display names to every multi-entity claim
  while excluding IDs, codes, filter values and physical dimension fields.
- Corrected the excess-credit metric name and definition, and made
  receivable, receipt and inventory scope disclosures mandatory from the
  executed contract rather than optional model wording.
- Added an external HMAC-SHA256 trust root for behavioral artifacts, binding
  the exact Profile, Hermes, catalog and evidence identities and rejecting
  unsigned, re-signed, tampered or mismatched artifacts.
- Narrowed the Canary Hermes identity override, removed the orphan dataset,
  aligned environment manifests and governed time-range policy, and closed
  credential ACL inheritance on the active Profile and retained rollbacks.
- Kept unavailable target-allocation details private even when a caller
  guesses a pending metric, and updated source documentation to reflect the
  fail-closed reconciliation state.
- Recorded the dev39 third-party data-minimization incident and froze further
  real-model E2E pending explicit authorization; production promotion remains
  blocked on signed real-model evidence, WeCom canary and installed-runtime
  rollback/roll-forward rehearsal.

## 0.11.0-dev39 - 2026-07-29

- Replaced keyword-based simple-ranking routing with a typed goal contract so
  judgment and intervention questions retain evidence-planning behavior.
- Made the governed time-range policy a single versioned source consumed by
  both contracts and query execution, including analytical request validation.
- Projected only executable metric and dimension capabilities; pending
  salesperson target paths can no longer be advertised to the model.
- Added sealed, deterministic metric disclosures and preserved mandatory
  scope, exclusion, netting, currency and coverage notes independently of
  model wording.
- Bound post-tool evidence to the exact decision, tool, arguments, session and
  turn, with conflicting duplicates and tampering failing closed.
- Bounded complex-query and entity-resolution calls, disabled unnecessary tool
  discovery for the three fixed governed tools, and compressed semantic
  contracts with shared dimension sets.
- Added strict behavioral-artifact validation with recalculated tool, SQL,
  latency, API, token and contract-size gates; the dev38 Top-10 artifact remains
  explicit failed evidence rather than being treated as skipped.
- Added a CURRENT-aware atomic runtime launcher, external mutable state,
  signed offline health, executable fixture rollback receipts and exact Hermes
  commit, tag, tree and lockfile startup identity verification.
- Kept formal production promotion blocked pending authorized real-model and
  WeCom evidence plus a real installed-runtime rollback rehearsal.

## 0.11.0-dev38 - 2026-07-29

- Restored the global twelve-turn ceiling so causal, complex and cross-domain
  questions retain their planning space; simple-ranking efficiency continues
  to come from the runtime final-only and tool-budget boundaries.
- Removed generic `QUERY_FAILED` from retry eligibility. A retry now requires
  both an explicit tool `retryable=true` marker and one of the narrowly
  governed timeout/deadline error codes.
- Added negative tests proving missing, false or forged retry metadata cannot
  reopen the simple-ranking query budget.

## 0.11.0-dev37 - 2026-07-29

- Added a conservative signed-route middleware that removes an unnecessary
  `customer_risk` planner contract only for unambiguous receivable ranking
  questions; causal, comparative, DSO, risk and cross-domain questions retain
  their full planning context.
- Added runtime-enforced contract and simple-ranking query budgets, a
  same-intent retry boundary for explicit transient query failures, and a
  final-only provider request after authoritative evidence is complete.
- Reduced the agent turn ceiling from twelve to four while keeping complex
  evidence planning outside the strict simple-ranking query budget.
- Added artifact-level rollback/roll-forward verification without claiming it
  as a live runtime rollback. A CURRENT-aware launcher and externalized mutable
  state remain formal-release prerequisites.

## 0.11.0-dev36 - 2026-07-29

- Made the Hermes boundary gate verify the version of the exact source module
  loaded from the tagged export, matching Hermes distribution compatibility
  checks.
- Kept the stale editable-package metadata visible as an environment
  maintenance issue instead of terminating the user's active `datasage`
  session to replace its locked executable.
- Retained dev35 as a failed, uninstalled build attempt and did not move its
  immutable tag.

## 0.11.0-dev35 - 2026-07-29

- Rebased the exact Hermes output-boundary and plugin-discovery fixes onto the
  locally repaired Hermes 0.19.0 runtime instead of performing a destructive
  downgrade.
- Pinned the Profile, release gates and atomic runtime unit to the verified
  Hermes 0.19.0 commit and tag.
- Kept the unrelated `package-lock.json` worktree change outside the Hermes
  release commit and source archive.

## 0.11.0-dev34 - 2026-07-29

- Replaced per-file Hermes export with one exact Git archive, preserving the
  tagged source tree while avoiding Windows path-length failures.
- Stored the exact Hermes source archive inside the atomic runtime unit and
  covered archive contents, tree identity and unit manifests with release
  tests.
- Retained dev33 as a failed, uninstalled build attempt instead of moving its
  immutable tag.

## 0.11.0-dev33 - 2026-07-29

- Replaced dirty-Hermes provenance with an explicit tagged Hermes commit and
  ran release gates against an export of that exact commit.
- Kept unrelated Hermes worktree changes outside the release identity instead
  of incorporating or silently discarding them.
- Added the foundation for a single checksummed Profile-plus-Hermes runtime
  release unit and offline rollback/roll-forward rehearsal.

## 0.11.0-dev32 - 2026-07-29

- Bound the candidate to a Hermes patchset that defers classic CLI assistant
  streaming whenever a final-output transform is registered, preventing raw
  typed envelopes from appearing before deterministic rendering.
- Declared WeCom callback messages non-editable so Gateway streaming cannot
  create an immutable raw preview.
- Kept normal final delivery enabled when a streamed-message edit is rejected,
  ensuring transformed output is never suppressed by a failed edit.

## 0.11.0-dev31 - 2026-07-29

- Added an explicit user-accepted existing-database-account policy for the
  exact non-production `datasage-canary-next` Profile.
- Kept the exception impossible on production names, production mode or a
  production-stamped artifact.
- Removed object-scope configuration from the query tool's unconditional
  environment prerequisites when the Canary exception is active.
- Preserved live `SHOW GRANTS` collection and exposed the active grant policy
  and observed privilege classes as audit evidence.

## 0.11.0-dev30 - 2026-07-29

- Accepted a unique controlled `datasage_answer` embedded in model prose while
  discarding every surrounding character before deterministic rendering.
- Kept the canonical boundary fail-closed for multiple JSON documents,
  malformed or wrapped JSON, stray braces, duplicate keys, non-finite numbers,
  unknown fields and invalid evidence claims.
- Bounded canonical model-document parsing to prevent oversized or
  ambiguity-heavy responses from consuming unbounded validation work.
- Added Round 16 real-model-shape regression coverage through the complete
  transform and renderer boundary.

## 0.11.0-dev29 - 2026-07-29

- Bound each requested logical dimension to a safe business display field,
  prioritizing names and codes while excluding internal identifiers.
- Rebuilt customer-risk exact recipes as executable typed cross-domain plans
  with governed metric, dimension, filter-role and time-default bindings.
- Added controlled query-backed entity clarification, unavailable and
  unsupported dispositions without leaking raw tool errors or identifiers.
- Locked timeout retries to one semantically identical attempt and rejected
  request-ID, metric, dimension, filter, time-window and comparison changes.
- Separated database health and grant-audit timeouts from business statement
  timeouts and reduced expected timeout cleanup logging to one bounded warning.
- Removed the duplicate Skill body from the model contract payload; SOUL remains
  the single prompt source while the Skill identity is sealed by version/hash.
- Added Round 15 extended regression coverage for dimension evidence, customer
  risk execution, entity clarification, retry integrity and health isolation.

## 0.11.0-dev28 - 2026-07-28

- Connected `decomposition_of_request_id` from the public metric schema through
  request validation so governed same-batch why decompositions can execute.
- Added per-tool-call query attempt lineage. Final grounding consumes only the
  latest evidence for each request, retains prior attempts until the turn is
  consumed, and fails closed when successful retries conflict.
- Canonicalized model output without passing model prose through: exact JSON and
  one standalone JSON fence are accepted for answers; no-query outcomes may
  discard prose only when exactly one controlled JSON object exists.
- Moved success logging after deterministic rendering so safe refusals are
  recorded as blocked rather than rendered.
- Replaced user-visible `[查询N]` trace labels with bounded business metric labels
  and deterministic suffixes for repeated labels.
- Added Round 15 regression coverage for why linkage, retry authority and
  conflicts, JSON canonicalization, ambiguous outcomes, and truthful logging.

## 0.11.0-dev27 - 2026-07-28

- Made release payloads deterministic by exporting committed Git blobs into an
  isolated build tree and declaring LF text normalization in `.gitattributes`.
- Isolated release gates from source-worktree caches and line-ending settings.
- Restricted plaintext MySQL to source validation and explicitly named Canary
  Profiles; installed production Profiles now require `.production-release`
  before database readiness can pass.
- Added a production stamping command and bound the marker to the installed
  artifact ID, version and payload hash.
- Synchronized README, architecture, runtime identity, manifests and example
  environment with the conditional Canary/production transport policy.
- Clarified that database-account origin is unrestricted while actual grants
  must still satisfy the SELECT-only explicit-object policy.
- Fully consumed unbuffered `SHOW STATUS` results before issuing the next
  transport query.

## 0.11.0-dev26 - 2026-07-28

- Marked `DATA_QUERY_MYSQL_SSL_CA` optional in the distribution manifest so
  Hermes installation metadata matches the conditional runtime TLS policy.

## 0.11.0-dev25 - 2026-07-28

- Aligned database transport policy with the existing `datasagecore` deployment:
  non-production profiles may explicitly use plaintext MySQL without a CA file.
- Kept verified TLS mandatory for explicit production mode and installations
  carrying a `.production-release` marker; production cannot be downgraded by
  `DATA_QUERY_REQUIRE_TLS=false`.
- Made plaintext transport explicit with PyMySQL `ssl_disabled=True`, verified
  the live connection reports no TLS, and exposed truthful transport evidence.
- Kept live `SHOW GRANTS`, object-level allowlists, read-only execution,
  statement timeouts, result bounds, rollback and close protections mandatory
  in both transport modes.

## 0.11.0-dev24 - 2026-07-28

- Preserve every canonical `answer_scope_line` verbatim in the
  deterministic renderer. A protected scope that cannot fit the configured
  output budget now causes a safe refusal instead of a silent prefix clip.
- Protect each reconciled change-decomposition judgment boundary in the output
  budget and bind it to the overall result's stable query key. At tight
  budgets, fewer driver rows are shown while the mandatory statement that
  additive evidence does not prove causality remains protected.
- Added exact-scope coverage at 239, 240, 241 and 244 characters, distinct
  multi-payload scopes, an over-budget fail-closed case, and decomposition
  checks at 1000, 1001, 3900 and 4000 characters.
- Reject duplicate change-decomposition sections and reused driver claim sets
  so repeated model structure cannot duplicate evidence or consume the answer
  budget.

## 0.11.0-dev23 - 2026-07-28

- Decoupled fact-status coverage from the three-headline display limit.
  Status classification now evaluates every selected canonical claim and
  renders one deduplicated boundary after all selected facts.
- Added deterministic 4-, 5- and 10-metric cases plus mixed comparison,
  target and plain-fact coverage, including a plain fact outside the headline
  set.
- Reserved output budget for the mandatory status boundary, truncation and
  omitted-evidence disclosures, and scope line. Plain results are described
  by count so same-label comparison and current-only results cannot produce a
  contradictory status label; long disclosure labels are deterministically
  bounded, and every fact/disclosure pair shares a short unique query key so
  same-label and same-prefix requests remain unambiguously linked.

## 0.11.0-dev22 - 2026-07-28

- Closed the Round10 nested causal-authority bypass by validating observation
  kind, request policy, comparison, two-sided ordering, limits, dimensions and
  metric references as exact structured values.
- Removed free-text `explain_change.output` from the model-visible receipt
  contract and added the five Round10 causal mutation probes.
- Added a shared fact-status policy and deterministic renderer behavior:
  target and complete comparison evidence keep their governed status, while a
  successful non-truncated finite plain fact gets one short aggregated
  boundary that says trend or quality remains unknown.
- Added explicit zero-target handling and rejected non-finite values from
  decimal interpretation.
- Required comparison and target claims to carry their explicit relation and
  satisfy producer-, canonical-claim-, and renderer-level arithmetic/state
  closure. Re-sealed inconsistent claims and result/claim truncation drift now
  fail closed.
- Marked legacy E2E catalogs as unbound and promotion-blocked. Synthetic
  fixtures are not model replay or production evidence.

## 0.11.0-dev21 - 2026-07-28

- Removed the label-and-number heuristic that allowed qualitative LLM business
  claims to pass when no trusted DataSage outcome existed. Missing turn state,
  missing query evidence, and free-form smalltalk now fail closed.
- Registered profile-owned safety wrappers for all three lifecycle hooks.
  Pre-LLM and post-tool failures poison the turn; transform failures and empty
  transforms always return the fixed safe refusal.
- Added real Hermes 0.18 plugin-manager fault injection for pre-LLM,
  post-tool, and final-transform hooks, plus qualitative no-tool regression
  cases covering synonyms, omitted subjects, and pure why questions.
- Extended the runtime change contract invariant to validate target cross-domain
  routes against live downstream metric and recipe capabilities, reject unknown
  causal authority on observations, and require a structured explain policy.
- Added fail-closed mutation coverage for missing target recipes or metrics,
  empty handoff routes, causal observation relations, asserted causal status,
  and removed explain policies.

## 0.11.0-dev20 - 2026-07-28

- Reclassified receivable and inventory Top-N period comparisons as
  `change_observations`, explicitly labelled them
  `partial_observations_only`, and prohibited unsupported causal claims.
- Replaced the target domain's retired change-driver handoff with a structured,
  same-period route to the actual delivery and receipt capabilities:
  governed decomposition first and partial observations as fallback.
- Added a runtime fail-closed invariant that binds model-visible change recipes
  to metric-owned additive decomposition capability, validates bounded
  observations, and rejects missing local recipe references.
- Expanded the model-facing consistency gate across all six domains and added
  mutation coverage for retired names, missing labels, unsupported governed
  decomposition, and broken recipe references.

## 0.11.0-dev19 - 2026-07-28

- Replaced conflicting Top-N driver guidance with one governed same-batch,
  linked, complete decomposition recipe; retained Top-N only as explicitly
  partial change observations.
- Moved reasoning topics from domain-wide code menus into versioned metric
  contracts for the four governed additive-change metrics.
- Required every reasoning claim to carry a complete, non-truncated, nonzero,
  same-metric and same-scope period comparison, enforced by both producer and
  deterministic finalizer.
- Added negative tests for static, zero, incomplete, and truncated evidence,
  plus real model-contract tests that reject conflicting driver recipes.
- Made the real Hermes prompt-boundary release gate warnings-as-errors with one
  explicit third-party dependency-warning allowlist and deterministic temp
  cleanup.
- Required an exact source release tag and stamped the Hermes source commit,
  dirty flag, and Git-status digest into release metadata.

## 0.11.0-dev18 - 2026-07-28

- Replaced field-presence driver inference with contract-declared additive
  partitions, compiler-owned population and projection fingerprints bound to
  the actual source, physical user-filter, system-filter, and join plans,
  exact three-column
  current/comparison/delta reconciliation, and complete partition binding.
- Kept zero-change rows in the sealed reconciliation partition while exposing
  only the complete ordered nonzero set as authorized change drivers.
- Added producer and finalizer integrity seals for canonical claims and
  reconciliations; zero, missing, unknown-state, non-additive, truncated,
  cross-scope, partial-set, and non-reconciled paths fail closed.
- Required a conclusion headline for every selected successful result and
  added bounded typed hypothesis, unknown, and next-evidence items that can
  reference only returned claims and domain-authorized business topics.
- Added a real Hermes 0.18 prompt-boundary release gate proving the generic
  Skills index and management instructions stay out of the DataSage runtime
  tool surface.

## 0.11.0-dev17 - 2026-07-28

- Replaced opaque row handles with canonical public evidence claims that bind
  metric, governed dimensions, period, unit, fact roles, states, and allowed
  analytical relations.
- Added typed answer v2 headline and bounded driver composition without
  reopening model-authored factual prose or causal claims.
- Added explicit missing, incomplete, future-period, dual-turnover, untrusted
  dimension, and deterministic final-message length handling.
- Added typed no-query outcomes for contract clarification, unsupported
  requests, and entity-only resolution.
- Hard-disabled the generic Skills toolset and projected the fixed DataSage
  Skill read-only through `datasage_contract`.
- Synchronized production-disabled salesperson allocation acceptance cases and
  runtime environment declarations.

## 0.11.0-dev16 - 2026-07-27

- Replaced free-text final-answer acceptance with a typed claim-ledger
  disposition protocol and deterministic business rendering.
- Removed ambiguous shortened metric tokens from the delivery boundary.
- Hid and execution-blocked salesperson-allocation metrics and paths pending
  production read-only coverage, uniqueness, orphan, and amount reconciliation.
- Unified target analytical dimensions with each metric path contract instead
  of a hidden two-dimension executor cap.
- Restricted live database proof to TLS 1.2 or TLS 1.3.
- Kept TLS CA fixtures in temporary test directories instead of the source tree.

## 0.11.0-dev15 - 2026-07-27

- Promoted database security readiness from a static configuration check to a
  startup-time live TLS and grant proof.
- Classified MySQL errno 2026 as
  `DATABASE_TLS_NEGOTIATION_FAILED` instead of leaking a driver exception or
  collapsing it into a generic query failure.
- Kept the query tool unavailable when the server cannot negotiate mandatory
  TLS; contract and entity tools remain available for non-database planning.

## 0.11.0-dev14 - 2026-07-27

- Replaced global number-pool grounding with structured bindings across metric,
  entity, value role, unit and currency; added all Round 2 leak and false-positive
  cases as executable regression tests.
- Isolated evidence by session and turn so an interrupted retry cannot inherit
  stale query results.
- Added stable opaque metric references and business display labels to real
  timeout and failure payloads.
- Restricted database grants to explicitly configured schema/object scopes,
  rejected global SELECT and grant option, and split TLS/grant/write failures
  into auditable error codes.
- Serialized and validated vendored PyMySQL imports under concurrent startup.
- Added static startup health evidence and hid `datasage_query` when TLS,
  credentials, CA, grant scopes or the pinned dependency are not ready.
- Physically removed the unreachable dataset/detail execution and contract
  projection subsystems; the public release remains metric-only.
- Distinguished immutable source manifests from installer-canonicalized
  `distribution.yaml` and runtime-generated files.

## 0.11.0-dev13 - 2026-07-27

- Removed the keyword-driven WeCom pre-dispatch fast path and its private
  per-chat scope cache after prelaunch review found silent multi-entity,
  multi-intent, exclusion, and session-reset failures.
- Restored the contract-first planner path for every business question.
- Replaced the global deterministic answer renderer with a fail-closed evidence
  validator that leaves a conforming model answer intact.
- Added adversarial natural-language acceptance cases before changing behavior.
- Made verified TLS and server-issued read-only grant checks mandatory, added
  hard metric/detail time-span limits, and removed deprecated SSL arguments.
- Vendored the hash-pinned PyMySQL runtime with its license and reject any
  process that preloads an external copy.
- Removed the unresolved author-time source placeholder; official installation
  now owns canonical source and installed-at provenance.
- Pinned the candidate to the only verified Hermes runtime, `0.18.0`.
- Clarified that the current implementation performs no internal blind retry;
  timeout recovery requires a reviewed narrower request or a later user retry.
- Closed the unfinished model-facing dataset/column interface. The dev13
  release surface is metric-only; physical datasets remain plugin-private until
  a separately governed semantic detail contract exists.
- Explicitly disabled the inherited kanban toolset and separated source-only
  validation commands from installed-runtime checks.
- Profile and plugin share the same `0.11.0-dev13` release identifier. Historical
  `0.16.0`/`0.17.0` entries referred to intermediate plugin-only development
  lines that were superseded when the convergence Profile adopted unified
  versioning; they are not upgrade predecessors of this candidate.

## 0.11.0-dev12 - 2026-07-27

- Added a deterministic WeCom fast path for unambiguous department-month
  delivery, receipt, and target-progress questions.
- Reused the governed query tool and deterministic insight renderer while
  bypassing both model calls for these exact, low-ambiguity lookups.
- Preserved the normal model-led path for comparisons, diagnostics, causes,
  rankings, exploration, and ambiguous requests.

## 0.11.0-dev11 - 2026-07-27

- Moved verified fast-path mappings into the always-loaded Profile rules because
  the hardened WeCom tool surface intentionally excludes generic Skill tools.
- Explicitly prohibited redundant contract calls for those exact fast paths.

## 0.11.0-dev10 - 2026-07-27

- Added governed direct-query fast paths for common department delivery,
  receipt, and target-progress questions.
- Kept ambiguous, comparative, diagnostic, ranked, and exploratory questions
  on the full contract-first planning path.

## 0.11.0-dev9 - 2026-07-27

- Compressed multi-dimensional driver output into a decision-oriented summary:
  overall movement, two leading up/down drivers per dimension, a bounded
  judgment, and the next evidence check.
- Required "why did it change" plans to verify the overall movement before
  driver decomposition.

## 0.11.0-dev8 - 2026-07-27

- Replaced the audit-style evidence dump with a deterministic insight composer.
- Preserved the requested entity and period in the headline.
- Added business formatting for amounts, percentages, target/actual/gap, and
  concise receipt and delivery summaries.
- Added evidence-backed judgments for target progress and ranked data drivers
  while keeping real-world causes explicitly separate.

## 0.11.0-dev7 - 2026-07-27

- Added the governed business definition and available unit, currency, and
  answer policies to the deterministic evidence projection.
- Reworded the evidence header so safe model prose is not incorrectly described
  as having failed an audit when canary policy replaces all model prose.

## 0.11.0-dev6 - 2026-07-27

- Matched the Hermes `post_tool_call(tool_name, args, result, **kwargs)` hook
  contract and added a runtime-shaped regression test.
- Changed canary delivery to always use the deterministic evidence-only
  projection after a governed query instead of authorizing model prose through
  a global numeric whitelist.
- Tightened fallback rendering for missing values, result states, and entity
  display fields.

## 0.11.0-dev5 - 2026-07-27

- Added a session-bound final-output grounding gate using Hermes' official
  `pre_llm_call`, `post_tool_call`, and `transform_llm_output` hooks.
- Unsafe model-authored totals, shares, entity relationships, unsupported
  causes, and ungrounded numbers now fall back to direct query evidence only.
- Kept the query boundary read-only and the public tool surface at three tools.

## 0.11.0-dev4 - 2026-07-27

- Prohibited name-based entity grouping and model-authored cross-row subtotals,
  contribution shares, counts and residual attribution.
- Reduced two-sided driver evidence to five rows per direction by default.
- Removed the generic Skills toolset from CLI so local tests mirror the
  production WeCom tool surface and avoid an extra model/tool round trip.

## 0.11.0-dev3 - 2026-07-27

- Defined paired increase/decrease driver requests ordered by `delta_value`.
- Added final-answer checks for unit conversion, price claims, ratio interpretation,
  truncated subsets and unexplained residual attribution.
- Restricted `planner_and_fields` to explicit summary-plus-record-detail questions
  to reduce model context and latency.

## 0.11.0-dev2 - 2026-07-27

- Required dimensional period drivers to use one governed union comparison
  ordered by `delta_value`, rather than comparing two truncated rankings.
- Added fail-closed Top-N cutoff arithmetic and named-entity-set consistency
  rules after the first live open-question Canary review.

## 0.11.0-dev1 - 2026-07-27

- Established `datasage-mini-next` as the single lightweight Q&A development line.
- Retained six governed business domains, 121 registered metrics, deterministic
  entity resolution and bounded read-only evidence queries from DataSage Mini.
- Adopted the canary evidence-depth model for facts, comparisons, overviews,
  rankings, diagnostics and open exploration.
- Separated weekly slow-moving automation, cron, rendering and outbound delivery
  from the conversational Profile.
- Reduced the public plugin surface to three read-only tools and the Agent budget
  to 12 turns.
- Added fail-closed WeCom defaults, a 28-case behavioral acceptance set, static
  candidate validation, architecture ownership and release invariants.

## Unreleased - 2026-07-22

- Added one shared entity registry for verified aliases, canonical values, Unicode/whitespace/case normalization, and domain-specific filter roles.
- Added `datasage_entity_resolve` for one bounded, deterministic master-data candidate query only when a token remains unresolved; it makes no model or embedding call and never auto-binds prefix/contains candidates.
- Added query-time exact alias normalization and uniquely provable role repair, including customer-department versus warehouse-department behavior for receivable and inventory metrics.
- Preserved Hermes-owned planning and analysis: no router model, fixed analysis checklist, answer rewriter, or new metric formula was introduced.
- Added regression coverage for exact aliases, explicit department expansion, cross-type ambiguity, fuzzy clarification, parameter binding, physical-metadata hiding, role conflicts, and end-to-end query normalization.

## 0.10.0-rc2 - 2026-07-21

- Kept the lightweight `SOUL → datasage_contract → datasage_query → model answer` architecture with no router LLM, answer-rewriting hook, or model call inside the plugin.
- Added one deterministic, safety-first model projection for all domain planner contracts: non-overridable rules and removed capabilities precede defaults and canonical intents, while common routes and recipes precede wider metric and dimension catalogs.
- Separated delivery scope defaults from canonical overview bundles and adaptive investigation. Explicit metrics, trends, rankings, comparisons, grouping and diagnostics no longer risk being swallowed by the three-metric delivery overview.
- Aligned the delivery planner v5 recipe names and overview bundles with the plugin-only semantic contract, including the four-metric return overview without an unrequested return-rate metric.
- Reduced the model-visible query result to business evidence and one canonical `answer_scope_line`; private audit detail remains outside the answer context.
- Added evaluation-only synthetic evidence injection that exists only in isolated temporary Profiles. Five independent default-overview model runs and a three-case neighbor suite passed on the first query attempt without database access.

## 0.10.0-rc1 - 2026-07-21

- Replaced the WeCom Skill-loading chain with one read-only `datasage_contract` tool and one discoverable `datasage` Skill.
- Removed generic `skills` and slow-moving write operations from the WeCom tool surface; saved-result reads use `slow_moving_query`, while `slow_moving_maintenance` is CLI/no-agent only. Direct messages now fail closed behind an allowlist and groups are disabled.
- Removed deterministic final-answer rewriting so Hermes retains ownership of evidence-based analysis and wording.
- Kept aggregation, grouping, ranking, comparison and ratios behind governed metrics; dataset mode remains bounded detail-only until a semantic contract explicitly registers a safe analytical operation.
- Added unique request IDs, lossless Decimal serialization, MySQL 3024 timeout classification, complete fixed-filter evidence, generated evidence timestamps, and an explicit database-row untrusted-data marker.
- Added verified TLS as the default database transport requirement and temporarily removed customer phone/address fields pending user-level authorization.
- This is a workspace candidate only. Production database grants, TLS server readiness, real WeCom traces, performance evidence, and semantic-source consolidation remain release gates.

## 0.9.0 - 2026-07-21

- Added the `slow-moving-weekly-tracking` Skill and deterministic `slow_moving_tracker` tool to the existing `datasage-query` boundary.
- Confirmed and mapped the business slow tag, valid current inventory, valid delivery, and customer-return sources from production read-only schema evidence.
- Added idempotent weekly baselines, barcode traceability, local case continuity, delayed-data recomputation, unit separation, owner-candidate summaries, and zero-slow-week handling.
- Added explicit sales outflow, customer return, net sales reduction, all-product inventory net reduction, notified-barcode-pool reduction and remainder, return rate, unexplained-stock-change, and identity-match evidence.
- Added mutually exclusive result statuses for clearing, returns, growth, exit, improvement, no improvement, and anomaly reporting without treating every outflow as business completion.
- Added safe Hermes no-agent Monday reminder and Friday report entrypoints plus an idempotent installer that refuses WeCom delivery without an explicit target chat.
- Added offline scenario tests for returns, growth, exit, clearing, unit safety, owner precedence, lifecycle continuity, re-entry, idempotency, delayed recomputation, snapshot completeness, and governed SQL construction.

## 0.8.2 - 2026-07-21

- Added the official platform-scoped `WECOM_ALLOW_ALL_USERS=true` opt-in required when both WeCom direct-message and group policies are open.
- Kept ordinary direct-message users limited to `/new` while preserving `WECOM_OWNER_ID` as the direct-message administrator.
- Enabled an independent group slash gate with no real group administrator and an empty group command allowlist; `/new` and all operational or administrative commands are denied in groups.
- Documented the Hermes 0.18 invariant that authorized non-admin users always retain the read-only `/help` and `/whoami` floor; the Profile does not patch Hermes core to remove it.
- Left all six business-domain semantics, query construction, database behavior and metric contracts unchanged.

## 0.8.1 - 2026-07-21

- Rebuilt the inventory contract around explicit total, on-hand, available, allocated and in-transit scopes, with unit-safe quantities, original-currency protection, value-coverage evidence and governed current product/warehouse enrichment.
- Separated current warehouse age from month-end no-movement days, made the latter default to the latest non-null snapshot, preserved signed month-end roll counts, and distinguished warehouse organization from accounting organization.
- Restored the confirmed cost and DDP inventory-turnover-day definitions in one controlled analytical query using N+1 continuous month-end snapshots, trapezoid average inventory, natural days, net delivery and structured undefined states.
- Advanced the inventory planner to v2, inventory semantics to v5, shared datasets to v9, evaluation contracts to v8 and the query plugin to 0.16.0; 160 offline tests and seven production read-only inventory contract checks pass.
- Expanded target completion and allocated-performance queries to at most two business dimensions and governed monthly trends.
- Added explicit period, target and actual states so future periods, missing targets, true zero targets, incomplete target amounts and missing actuals cannot collapse into one answer.
- Made current-month completion compare the full monthly target with actuals accumulated to date, kept future periods target-only, and made completion rankings place undefined rates last.
- Made an unspecified target-completion overview return both delivery and receipt completion on their transaction-detail ledgers instead of guessing one target type.
- Defaulted target trends without a period to the latest six natural months including the current month, and required month-level output whenever one range crosses from occurred periods into future target-only months.
- Kept missing targets distinct from zero targets in narration and failed closed for unsupported salesperson-by-customer and product target-completion grains without inventing target values.
- Advanced the target planner to v3, target semantics to v8, evaluation contracts to v9 and the query plugin to 0.17.0; 164 offline tests and four production read-only target contract checks pass.
- Recorded exact department/organization alignment and the governed `biz_org` to `org_name` organization equivalence for delivery returns.
- Confirmed that ordinary receipt queries include all receipt usages unless the user explicitly filters a usage.
- Simplified the default receipt overview to receipt, refund and net receipt; actual receipt and actual refund now require explicit user intent.
- Retained owner approval for all WeCom users to explicitly query current customer phone and contact address.
- Made dataset-level fixed filters authoritative for standard governed metric construction as well as detail queries, with fail-closed detection when a metric declares a conflicting rule.
- Updated the receivable domain to exclude internal customers across occurrence, open-receivable, debt and aging evidence, disclose that scope, and use customer department plus business organization for occurrence attribution.
- Corrected the monthly debt snapshot grain and key to month plus customer from production read-only evidence, and fixed the excess-credit intent scope so the YAML contract parses exactly.
- Recorded the Profile owner's seven-point customer-risk reconfirmation without changing execution formulas: gross-delivery formal DSO, signed trapezoid debt averages, exclusion of completed-but-open bills, the confirmed excess-credit formula under its existing business name, asymmetric internal-customer scope for delivery-versus-receipt, evidence-lane attention lists without a composite ranking, and no model-invented peer-sample threshold.

## 0.8.0 - 2026-07-20

- Added compact model-facing planner contracts for receipt, receivable, customer risk, target and inventory; all six domains now keep physical semantics and source inventories out of normal business conversations.
- Removed free-text `purpose` keyword correction from the query tool. Gross-delivery intent and ready-goods filters now use explicit structured fields.
- Made metric and dataset request fields strictly mutually exclusive and corrected customer-risk recipes to use `metric_filters`.
- Replaced dataset-kind inference with explicit per-dataset time policies, registered legal time routes, and strict date/month parsing.
- Added internal matched-row evidence so empty, zero and undefined results remain distinct across base, composite, ratio, comparison and analytical queries.
- Unified analytical ordering, corrected settlement quality counts to the requested grouping grain, isolated unexpected failures per subquery, and added a total batch deadline.
- Added one bounded internal retry for a timed-out read-only SELECT and recorded recovery metadata without delegating retry selection to the model.
- Added a deterministic answer-time contract using Hermes 0.18's official `post_tool_call` and `transform_llm_output` hooks; only successful time-scope metadata is retained for the current turn.
- Kept evaluation source-only and excluded integration runtimes, state, logs and stage reports from the runtime distribution.
- Kept WeCom direct and group chat open to all users while separating slash-command administration through a portable owner identity.
- Allowed every WeCom user to run `/new` in direct messages and groups without granting other administrator commands.
- Made every successful tool result carry explicit public time/snapshot evidence, replaced model-authored scope lines with one canonical tool-derived line, and made delivered stdout authoritative in E2E evaluation.
- Preserved settlement-quality groups even when all their bills are excluded, recharged the call deadline after connection setup, and failed closed on previous-period date underflow.
- Normalized Markdown-formatted scope declarations before final delivery, made the release gate reject contradictory scope lines, covered analytical date underflow, and synchronized client socket timeouts with the remaining call budget.

## 0.7.1 - 2026-07-20

- Removed the original-outbound/source-warehouse attribution capability from the model planner, physical semantics, analytical query implementation, and regression catalog.
- Retained actual warehouse net flow only: gross delivery is grouped by actual outbound warehouse and returns are deducted at actual inbound warehouse.
- Added a fail-closed boundary for source-warehouse requests so the Agent neither queries nor silently substitutes actual-warehouse results.
- Replaced the two former source-warehouse E2E cases while keeping the 25-case release suite stable, and added structural absence assertions to prevent reintroduction.
- Advanced delivery planner to v3, physical semantics to v9, delivery Skill to 0.6.4, query plugin to 0.11.1, and evaluation contracts to v7/v3.

## 0.7.0 - 2026-07-20

- Added a compact model-facing delivery planner contract while retaining the complete v8 physical semantics for plugin execution; normal business Q&A no longer loads the 1,400-line physical contract.
- Made the shared query rules and answer boundary mandatory for every data request, and added stable confidentiality and evidence invariants without moving business formulas into `SOUL.md`.
- Unified original-currency planning: a bare original-currency request groups by currency, while an explicitly named currency becomes a filter; mixed currencies are never totaled.
- Made internal restrictions silent by default, required business-language explanations only for explicit follow-up questions, and prohibited physical identifiers or tool payloads in business answers.
- Added numeric-scale rules: preserve returned units and ratios, and divide yuan values by 10,000 before labeling them as 万元.
- Made metric mode mandatory for business summaries, rankings, ratios, comparisons, and net or target calculations; dataset mode is limited to controlled single-dataset detail retrieval and cannot bypass governed metrics.
- Kept confirmed detail scopes explicit: delivery details use `delivery_time` with `bill_status = 6`, order details use `sale_time` without status exclusion, and return details use `statement_time` with `bill_status = 4`, `complnt_type = 1`, and `channel_type = 1`.
- Unified original-currency requests as either filtering to one explicit currency or grouping by currency; unlike currencies are never combined.
- Recorded the Profile owner's decision not to enforce a fixed maximum query span while retaining resolved start and end dates, aggregation, row caps, timeouts, and database-side controls.
- Made `data_updated_at` optional, non-user-facing, and non-blocking; freshness metadata is not required to answer a successful business query.
- Clarified physical net delivery as positive barcode-level outbound facts minus negative piece-level return facts at the actual inbound warehouse, with current warehouse-master attributes used only as disclosed supplements. Batch, barcode, and defective-item warnings now describe the same boundary without changing formulas.
- Added 25 core delivery/order end-to-end strategies covering default and gross scopes, warehouse schemes, physical facts, original currency, governed dimensions, order-delivery alignment, dataset bypass attempts, and complex multi-question handling.
- Advanced the delivery semantic contract to v8, the delivery skill to 0.6.1, the guarded query plugin to 0.10.0, and the release evaluation contract to v6.

## 0.6.0 - 2026-07-20

- Added the true outbound barcode fact as the governed physical-delivery source while retaining sale detail as the ordinary business delivery/order source.
- Added physical warehouse gross flow and default warehouse net flow. Default warehouse net subtracts returns at their actual inbound warehouse and discloses that attribution.
- Added an explicit original-outbound-warehouse net-flow path that maps returns only when source bill, product, and SKU identify one outbound warehouse; ambiguous and unmatched mappings are not guessed.
- Added governed warehouse attributes, batch, barcode, source-barcode, cylinder, missing/defective/CCBS warehouse flags, and defective-item dimensions without exposing business remarks, receiver/collaboration text, or price details.
- Kept batch, barcode, and defective-item on the default period-flow net scope; source-barcode and cylinder are explicit gross-only dimensions, and unsupported net requests fail closed instead of silently changing scope.
- Added original-currency gross delivery, return, net delivery, and order metrics. Original values require currency filtering or grouping and are never aggregated across unlike currencies.
- Expanded offline contract coverage for dual fact grains, warehouse attribution, barcode one-to-many safety, original-currency boundaries, and delivery/inventory routing.

## 0.5.0 - 2026-07-17

- Moved split-table coverage, uniqueness, orphan, and amount-reconciliation guarantees to the ETL contract; the Profile no longer freezes historical quality observations into runtime availability gates.
- Added mandatory `attribution_mode` to target metric requests so transaction-detail and salesperson-allocation paths are selected explicitly, including overall and department/organization rollups.
- Added governed allocated net delivery and allocated net receipt metrics and removed public gross-actual component metrics that could be mistaken for target actuals.
- Made the target domain metric-only so dataset mode cannot bypass net-actual, target-completion, or ledger-selection rules.
- Expanded router precedence for collaboration/allocation, ready-goods delivery versus inventory, DSO versus inventory turnover, direct receivable metrics, and delivery-receipt comparisons.
- Scoped the one-person negative-fact exception to delivery returns; salesperson receipt refunds always use the refund split fact.
- Replaced quality-block regression expectations with positive SQL-source, attribution-mode, metric-only, and no-gross-component assertions.

## 0.4.0 - 2026-07-17

- Introduced an explicit two-ledger policy: transaction-detail facts serve ordinary queries, while salesperson-allocation facts are reserved for target, collaboration, allocation, and allocated-performance questions.
- Added `receive_return_bill_split_dwd` and corrected salesperson receipt-target actuals to use receipt split minus refund split rather than mixing split receipts with detail refunds.
- Added fail-closed release gates for split facts: complete base-detail coverage, unique allocation keys, no orphan rows, and per-detail amount reconciliation.
- Blocked salesperson receipt-target actuals after production evidence found missing split coverage and amount mismatches; marked salesperson delivery-target actuals unverified pending equivalent detail-level reconciliation.
- Added `employee_dwd` as a current-attribute supplement with fact salesperson mapping through `person_id`; blocked employee-master metric joins because `person_id` is not currently unique.
- Added structured `DATA_QUALITY_BLOCKED` and `DATA_QUALITY_UNVERIFIED` tool failures so invalid allocation paths stop before SQL execution.
- Applied the same allocation-quality gate to dataset mode so a model cannot bypass blocked metrics by querying split tables directly.
- Expanded offline regression coverage for ledger routing, split-data quality gates, employee join safety, and unavailable-path execution guards.

## 0.3.0 - 2026-07-16

- Added the governed delivery-return fact and rebuilt default sales/delivery as period-flow net delivery: gross delivery minus completed sales returns after independent aggregation.
- Added explicit gross delivery, return amount/quantity/rolls, return-document counts, and tool-computed amount/quantity/roll return rates with non-positive denominator guards and no 100% clamp.
- Added return fact `sale_bill_type` as the direct source for the governed `bill_type` dimension; production evidence showed complete coverage and 100% agreement on exactly matched source rows, so no source-delivery backfill is used.
- Kept order counts gross-only and removed order count from the default delivery overview; order-versus-delivery fulfillment evidence explicitly uses gross delivery.
- Changed receipt department attribution to `customer_dept` while retaining receipt/refund inclusion of internal customers.
- Changed delivery target actuals to net delivery and receipt target actuals to net receipt. Delivery target scope excludes internal customers; receipt target scope includes them.
- Changed customer-risk delivery/receipt comparison to net delivery versus net receipt, with the confirmed asymmetric internal-customer scope disclosed in every answer.
- Kept formal DSO on its confirmed gross-delivery denominator and kept inventory `pur_delivery_rmb` as separately named turnover-table net delivery.
- Added generic ratio metrics, governed component-level dimension mappings, and multi-fact analytical aggregation without raw fact joins.
- Expanded the governed dataset inventory to 24 and the offline regression suite to 76 tests before release-document synchronization.

## 0.2.0 - 2026-07-16

- Added named open-analysis recipes and source inventories for delivery/order, receipt, receivable, target, and inventory while preserving confirmed metric formulas.
- Added generic previous-period comparison for composite metrics so net receipt changes remain tool-computed.
- Replaced answer-layer target alignment with a governed target-completion query that selects split or non-split paths deterministically and returns target, actual, gap, and completion rate together.
- Added direct inventory quantity, roll, month-end cost, and longest-age evidence while explicitly leaving turnover formulas and slow-moving thresholds undefined.
- Fixed month-string time grouping centrally so `YYYY-MM` snapshot fields return one row per month instead of collapsing the range.
- Reconciled all 23 governed datasets against production `information_schema`; removed nonexistent `complaint_amount_rmb`, `inventory_amount_rmb`, and current-inventory `is_print` from allowlists.
- Expanded release evaluation to 71 passing offline tests and completed bounded, read-only live validation across all six business domains.

## 0.1.0 - 2026-07-15

- Created a minimal Hermes Profile distribution.
- Added a router, common data foundation, and six bounded domain skills.
- Added one official-layout `datasage_query` plugin without lifecycle hooks.
- Added offline evaluation cases and plugin unit tests.
- Rebuilt the delivery/order semantic contract around eight confirmed metrics, explicit default scopes, and separate order/delivery time and status rules.
- Added evidence-backed customer, product, SKU, supplier, ready-goods, and warehouse dataset metadata.
- Added tool-controlled many-to-one master enrichment for governed metric dimensions; raw joins remain unavailable to the model.
- Deprecated and blocked delivery-fact `is_self`, separated the two `is_customized` meanings, and prohibited warehouse-by-organization delivery joins.
- Expanded delivery regression coverage for metric fields, default intent mapping, join reuse, field collisions, and domain boundaries.
- Rebuilt the receipt semantic contract around gross receipt, actual receipt, bill count, actual refund, refund bill count, original-currency, and net-receipt metrics.
- Added the receipt-refund fact and confirmed default receipt scope: exclude only status A, include internal customers, sales receipts, and advance receipts.
- Added contract-defined composite metrics and sum-of-products aggregation so net receipt and RMB conversion are enforced inside the query tool.
- Added receipt-domain regression coverage and live read-only validation for gross receipt, actual receipt, bill count, refund, and net receipt by business department.
- Corrected the confirmed net-receipt formula to receipt RMB minus refund RMB while keeping actual receipt and actual refund as separate metrics.
- Added confirmed refund-original and net-receipt-original metrics with mandatory currency grouping or filtering; confirmed unqualified arrival as receipt RMB.
- Rebuilt the receivable domain from production schema evidence and confirmed business rules: occurrence, positive open receivables, signed debt snapshots, positive-debt lists, debt days, aging buckets, overdue, credit excess, and uncredited debt.
- Corrected receivable dataset contracts: `gmt_modified` freshness for occurrence facts, debt grain including currency, the ten physical aging bucket names, and the composite customer-organization-currency credit key.
- Added tool-controlled composite-key joins, comparison filters, latest-snapshot derived calculations, numeric-leading physical columns, multi-bucket RMB aggregation, debt-days, overdue-days, and clamped debt-minus-credit aggregation.
- Added receivable regression coverage for status/internal scope, positive open details, signed versus positive debt, latest snapshots, aging buckets, three-key credit joins, missing-credit handling, and original-currency guards.
- Added a hash-pinned PyMySQL runtime requirement after verifying that the active Hermes interpreter does not provide it by default; deployment now has an explicit dependency check instead of failing only at first query.
- Rebuilt customer risk as bounded customer-receivable-risk evidence analysis with seven named recipes for overview, attention scans, customer cards, trend change, peer comparison, settlement/open pressure, and decision handoff.
- Added document-grain historical settlement metrics, formal DSO with thirteen-snapshot completeness checks, and independently aggregated delivery-versus-gross-receipt comparison.
- Added tool-controlled day/month buckets, prior-period comparisons, and latest-snapshot month-offset comparisons for governed metrics.
- Fixed MySQL date-format escaping centrally in the query builders after production read-only execution exposed PyMySQL placeholder interpretation; the full live validation set now succeeds.
- Added production aggregate quality evidence and regression cases for customer-risk open questions, boundaries, comparison defaults, data-quality exclusions, and no-score/no-decision behavior.
