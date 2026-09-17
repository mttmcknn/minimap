# Minimap hardening and evaluation plan

Prepared 2026-09-16 from source revision
`e838065b7b9f837f11233e452afc47e8bbdeefb0` and a fresh release build.

The goal is dependable, self-healing navigation memory: an agent with a fresh
checkout should reach the intended place, recover from unexpected navigation,
and maintain the shared map as the app evolves. Routine mismatches, retries,
and verified graph repairs should stay inside the agent's workflow. The user
should experience their development task progressing, without being asked to
label screens, diagnose selectors, or approve ordinary map maintenance.

Recovery must preserve the requested destination and report the actual outcome
to the agent. A critical product issue should reach the user when the agent
has reproduced it and corroborated it in relevant code. Token savings must
include recovery and maintenance costs. Measure these outcomes explicitly;
neither indefinite retries nor silently claiming success count as recovery.

This is the implementation roadmap and its original findings. The September 16
implementation adds fresh navigation observations, bounded recovery and agent
handoffs, explicit appearance confirmation and verified supersession, complete
recipes, stable IDs, atomic writes and operation locks, app/process/build guards,
safer matching and selectors, and reproducible Compose evaluations. See the
[implementation results](../evals/results/2026-09-16-self-healing.md) for measured
outcomes and remaining release gates; the backlog below is not a claim that
every phase has shipped.

The [follow-up implementation](../evals/results/2026-09-16-ethos.md) adds a
private recovery token spanning host commands, fresh goal anchors, strict
recipe validation, bounded settling and transport retries, private-input
redaction, and retained fallback routes after verified supersession. Fallback
preference preserves older-build routes without claiming full feature-state
modeling. Read both reports before treating any roadmap item as still broken
or as already validated.

The [controlled evaluation](../evals/results/2026-09-16-controlled.md) compares
raw navigation, first-use learning, and graph reuse on three Compose apps and
two API levels with a frozen binary. Its readiness-adjusted run passed 90/90
trials and the defined recovery/team controls, with the original setup failures
retained separately. Reuse reduced response bytes by 81–87% but did not improve
navigation time. Independent agent tokens, diagnosis, and alert quality remain
unmeasured; the prepared agent layer requires separate delegation authorization.

## Self-healing contract

Self-healing is the default agent workflow, not a separate user-operated repair
mode. Keep Minimap deterministic and agent-independent: the binary observes,
plans, executes, verifies, and persists proven navigation; the host agent
diagnoses unfamiliar situations, inspects source, and explores the minimum
necessary route. The binary does not need an embedded model or source analyzer.

### Recovery loop

1. Observe the actual app/device state and compare it with the expected
   transition, preserving the original goal and last verified place.
2. Handle a transient observation with bounded settling; if the app is at a
   different known place, replan from that place. External navigation changes
   orientation but does not prove a new edge that Minimap never executed.
3. When deterministic recovery cannot proceed, return a compact handoff to
   the agent: target, attempted recipe, expected/observed identity, relevant
   controls or a fresh-layout handle, failure category, excluded candidates,
   and remaining recovery budget. A tool-level blocker is not automatically
   a user notification.
4. The agent inspects fresh UI evidence and, when needed, the relevant route,
   callback, resource, feature condition, test, or recent source change. Verify
   that the installed build corresponds to the code before using it to
   explain the behavior. A code diff alone does not establish intended behavior.
5. The agent uses Minimap actions to try a supported route to the same goal.
   Record a candidate repair only after the expected destination is verified;
   validate its replay from an appropriate fresh starting state before
   promoting it as the preferred replacement. Unverified pending recipes stay
   outside the committed graph. Promotion writes are atomic per file under the
   repo lock; retained fallback routes remain available to other app builds.
6. Continue the original development task and retain only the compact,
   verified navigation change in the repo. Graph diffs remain reviewable
   through normal project workflows without a per-repair confirmation step.

### Classify the evidence before changing memory

| Situation | Recovery | Graph effect | User experience |
| --- | --- | --- | --- |
| External navigation or a stale session | Re-observe and plan from the actual place | Invalidate runtime state; do not invent an edge for the external action | Continue silently |
| Loading, animation, missed input, temporary overlay, or transient tool failure | Bounded wait/retry or agent-managed recovery within the task's existing permissions | No durable topology change based on the failure | Continue silently if recovered |
| Legitimate product evolution, such as renamed controls or relocated Settings | Corroborate the new path in current UI and relevant source/task intent; execute and verify it | Add the proven path; supersede an obsolete recipe only in the matching app/build/feature context | Continue silently |
| Another supported state, such as a locale, feature flag, or arrival history | Establish the state/preconditions and verify a compatible path | Keep contextual variants or alternatives; do not retire paths still valid elsewhere | Continue silently |
| Reproducible product defect | Check the expected behavior against source and a focused reproduction; use a valid alternate route if it preserves the task | Do not turn the defective destination into the intended one or erase contrary evidence | Alert only for a corroborated critical issue |
| Unresolved or ambiguous evidence | Keep the map unchanged, try remaining justified alternatives, and return an honest agent-level outcome | No speculative repair | No navigation chatter; explain once if the task ultimately cannot be completed |

For example, if Settings moved into an account menu, the agent should discover
and verify that route and update the map. If the Settings callback now invokes
Checkout by mistake, relabeling Checkout as Settings would corrupt the map;
the agent should preserve the intended identity and investigate the callback.
Distinguish an obsolete route from a destination intentionally removed from the
product: reaching a different screen does not satisfy the original request.

The recovery budget spans all engine retries and agent handoffs for one goal
when the host carries `data.recovery.token` on every command with `--recovery`;
each handoff must not reset it. The token enforces elapsed time and input count
across processes, retains original goal anchors, and records failed edges.
Bound elapsed time, additional actions, and
repeated state/action pairs in Minimap, and track token/tool cost in the agent
loop. Calibrate limits with the change evals. Exhaustion means a truthful
unresolved result to the agent, followed by task-level fallback or explanation,
not an infinite loop, an automatic product-bug alert, or false success.

### User communication and agent integration

Routine recovery should produce compact machine-readable outcomes such as
unchanged, recovered, graph-updated, or unresolved, alongside the actual
navigation status and changed files. Keep retry details in opt-in diagnostics.
Do not print a user-facing warning for every mismatch or make the user approve
labels and verified repairs. The canonical agent skill must teach the whole
observe/recover/verify/continue loop; installed skills and the Claude plugin
must continue to share that one source.

A critical-issue alert should include the affected user task, a reproducible
observed failure, the relevant file/line or focused test corroborating the
cause, and its practical impact. Critical means a material problem such as a
reproducible crash, data loss, incorrect account access, or a regression that
prevents the requested task. Do not infer severity or cause from a selector
miss, a timeout, or an unexpected layout alone. Preserve the distinction
between a verified product defect and an unresolved navigation problem.

If credentials, a human decision, or an unavailable environment ultimately
prevents completion, the agent must say so accurately in one concise task-level
message; it must not fabricate a code diagnosis or hide unfinished work. This
exception does not make routine navigation recovery a user interaction.

## Evidence and starting point

- Existing checks pass: 107 tests, `cargo fmt --check`, Clippy with warnings
  denied, and an optimized release build.
- The graph already stores places, variants, and action recipes, and the
  planner composes known transitions. Keep that narrow product boundary.
- Compose samples are available locally at `../compose-samples`, revision
  `d3ff757b289f7036815978a8f7b16706ee3423b0`; there is an existing Jetsnack APK.
- A live emulator is available as `emulator-5554`; Android CLI version is
  `1.0.15498356`. The initial smoke uses the existing APK, whose SHA-256 is
  `78dfd1f74d9d56f84f4701afdea179360ae675aac6ed0112f590978821860f0b`.
- An APK already on disk is not proof that it matches the current source:
  record its hash for this smoke, then build from a pinned clean checkout for
  the release evaluation matrix.
- See [the reproducible smoke](../evals/README.md) and
  [the live baseline report](../evals/results/2026-09-16-jetsnack.md) for
  measured results. Existing [benchmark notes](MINIMAP_BENCHMARK_NOTES.md)
  mostly measure output bytes, tool calls, and warm-session timing.
- The live baseline reached Search in all 15 ordinary trials but confirmed
  a false-success bug after external navigation; the adversarial smoke exits
  nonzero. Median raw/cold/warm times were 5.669/5.750/2.942 seconds, with
  warm source-observation cost excluded from the last number.

### Original implementation risks

| Priority | Evidence before this hardening pass | Required behavior |
| --- | --- | --- |
| P0 | `go_result` trusts a session for up to 600 seconds; a zero-edge plan performs no fresh destination observation | External navigation, restart, and wrong-app cases cannot produce false success |
| P0 | `android_package` scopes the cache but does not guard actuation | Verify target device and foreground app before executing a recipe |
| P0 | `resolve_selector_point` selects the first match | Reject ambiguous, hidden, disabled, or unusable targets with a precise reason |
| P0 | `match_place` returns the first exact match or the best score above one threshold | Detect ambiguity and distinguish screen identity from route-specific state |
| P0 | `scroll_result` replaces the pending recipe; `tap_result` drops same-place taps | Preserve every required step until a transition is verified |
| P0 | Short edge IDs derive only from the first action and sanitized labels | Different complete recipes and geometry contexts cannot overwrite one another |
| P0 | `write_atomic` uses one `.tmp` filename per object, with no repository lock | Concurrent writers cannot lose updates or race over staging files |
| P0 | Replay stops at the first failing edge; repaired routes can leave the old edge selectable | Recover from observed state, hand unfamiliar cases to the agent, and promote verified replacements without user interruption |
| P1 | Baselines accumulate variants without lifecycle or ambiguity checks | New app versions do not progressively merge distinct places or grow without limit |
| P1 | Subprocesses have no deadline; layout parse failure falls back to arbitrary text | Bounded commands and typed errors, with no learning from malformed observations |
| P1 | BFS scans all edges for each visited place and optimizes hop count | Efficient, deterministic planning against execution cost |
| P1 | The CLI uses `.` as the repository root | Invocation from an app module resolves the intended project consistently |

## Delivery order

Each phase should be a small series of reviewable changes. Introduce a failing
behavioral regression before its fix, then rerun the relevant live case.
Do not combine a schema migration, matcher rewrite, and performance rewrite in
one change.

### 1. Make observation and execution trustworthy

1. Normalize Android CLI output at the adapter boundary into typed observations,
   elements, device identity, foreground app, and viewport. Support only tested
   output shapes and aliases; reject malformed or unsupported observations.
2. Resolve project root and app profile explicitly. Require an unambiguous
   serial when several devices are connected and validate the foreground app
   before any input. Do not silently target another app or device.
3. Re-observe at the start of every independent `go`, including an already-at-
   target request. A TTL does not establish that Minimap was the last actor.
   Reuse observations within a verified execution; expose an explicit fresh
   read for agents checking externally changed state. Treat cross-command
   cache reuse as an optimization with a provable validity contract.
4. Invalidate session and pending state after a failed action, unexpected
   destination, device change, app restart/build change, or expired context.
   Save the last verified intermediate location so a partial replay cannot
   leave the old starting location in the cache.
5. Resolve selectors uniquely against current actionable UI. Where labels are
   children of clickable containers, retain enough structure to resolve their
   intended container; do not simply tap the first duplicate text match.
6. Use bounded settle polling that requires usable stable evidence or the
   expected destination, rather than one retry after a fixed sleep. Apply it
   between recipe actions as needed. Add subprocess deadlines, bounded output,
   cancellation, and useful environment/action/config error distinctions.
7. Check blocking overlays before recording a destination, including when the
   caller supplies a label; a label must not override failed observation.
8. Add deterministic reorientation and known-alternative replanning inside
   the shared recovery budget. Return an evidence-backed agent handoff when
   the remaining recovery requires source inspection or new exploration.
   Keep the actual failure status distinct from notification policy.

Exit gate: deterministic tests cover external navigation, zero-edge false
success, wrong app/serial, duplicate selectors, hidden targets, intermediate
loading layouts, timeout, overlay-with-label, and partial replay failures;
recoverable cases must reach the independently verified destination without
user interruption, and unresolved cases must return a truthful agent handoff.

### 2. Record complete routes and model the necessary context

1. Give a pending transition one explicit lifecycle: source observation,
   ordered actions, most recent observation, and completion/abandonment.
   Append repeated scrolls and same-place navigation actions instead of
   overwriting or discarding them. Never attach stale pending actions to a
   newly observed source.
2. Represent route preconditions separately from place identity: required
   controls, scroll/expansion state where necessary, compatible geometry, and
   history requirements. A recorded Back action is valid only with a verified
   compatible predecessor; otherwise use another known route or report why it
   cannot be replayed.
3. Define semantic destinations before changing the matcher: a generic snack
   detail screen and the detail for a specific snack are different requests.
   Model instance parameters or explicit distinguishing anchors rather than
   silently treating `chips-detail` and another item's detail as equivalent.
4. Replace stringly typed action/status internals with enums and validated
   constructors. Preserve the public JSON contract where practical; add a
   versioned migration when the graph representation must change.
5. Initially keep the supported action vocabulary small. Test tap, repeated
   scroll, Back, menus, and expansion thoroughly. Add parameterized text input
   and other gestures only with reproducible routes that require them and a
   clear handling of dynamic values.

Exit gate: record and replay a route requiring two scrolls and a same-screen
expansion, distinguish sibling details, and traverse Back from two different
arrival paths without claiming the wrong destination.

### 3. Make the graph safe to share and maintain

1. Use immutable place IDs, with labels as editable metadata. Edge identity
   must include a digest of the complete canonical recipe, endpoints, app
   scope, and relevant preconditions; readable prefixes are only decoration.
2. Use a repository lock for read/modify/write operations and a separate
   device execution lock keyed by the physical device, shared across repo
   checkouts. Bound lock waits and report contention rather than interleaving
   inputs from two agents.
3. Create unique staging files exclusively, reread the latest object under
   the lock, merge set-valued observations deterministically, and atomically
   replace files. Keep lock/cache files out of the committed graph.
4. Keep every persisted intermediate graph valid: write a new place before
   an edge that references it; remove referencing edges before deleting a
   place. Immutable IDs let relabeling update one object. Make interrupted
   operations idempotent and report every file actually changed, even when
   the overall command fails.
5. Apply the self-healing classification before durable changes. Exclude a
   failed edge for the current attempt, re-observe, and try bounded alternatives.
   Validate an agent-discovered replacement before promotion and retirement
   of the old recipe; source/build/context evidence must establish that the
   old recipe is obsolete, rather than temporarily blocked or valid elsewhere.
   Recovery must not require user approval for routine verified graph updates.
6. Give variants a deliberate lifecycle: do not automatically absorb an
   ambiguous match; distinguish simultaneous valid states from obsolete app
   versions. Detect duplicate identity claims and give the agent sufficient
   evidence to resolve them without silently merging distinct destinations.
7. Add repo-only validation suitable for CI without a device, including graph
   schema, IDs, action payloads, references, app scopes, and conflict markers.
   Handle normal Git merges with deterministic serialization and clear
   conflicts, not timestamps and counters in every graph file.
8. Tighten persistence rules for shared repos: prefer stable selectors;
   exclude editable/password content and dynamic user data; validate labels,
   reasons, and action selectors as well as fingerprints. Maintain synthetic
   regression cases for names, addresses, messages, credentials, and tokens.

Exit gate: fresh clones and separate worktrees reuse only committed memory;
parallel writers preserve both updates; concurrent device control is refused
or serialized; interruption/collision/merge tests leave a valid graph; seeded
private values do not enter persisted graph files; repaired routes work in a
fresh teammate checkout and stop causing the same rediscovery or user alerts.

### 4. Improve performance after the correctness gates pass

1. Build graph adjacency lists and an exact-fingerprint index once per loaded
   graph. Avoid repeated directory scans, whole-graph clones, display-size
   probes, and selector parsing within a command.
2. Measure lookup/planning at 100, 1,000, and 10,000 places with 10 times as many
   edges. Profile matching, JSON parsing, I/O, and device calls separately.
   Use an inverted candidate index only if matching is a measured bottleneck.
3. Replace hop-only selection with deterministic weighted shortest paths.
   Start with explicit nonnegative costs for layout captures, actions, and
   geometry dependence. Fit costs from evals without adding usage telemetry
   to the committed graph; avoid pretending to predict LLM tokens precisely.
4. Preserve compact orientation and result output. Keep diagnostic detail
   opt-in, and measure `go` both alone and followed by the verification layout
   that a real development task needs.
5. Keep implementation boundaries simple: `minimap-android` owns device I/O,
   `minimap-core` normalization/matching, `minimap-graph` planning,
   `minimap-repo` persistence, and the CLI argument/result wiring. Extract the
   execution/session state machine from the large CLI file into focused
   modules; do not introduce a service, plugin framework, or crawler.

Exit gate: correctness remains unchanged; measured local CPU/I/O overhead
stays a small fraction of device time; graph lookup scales with relevant
nodes/edges; report cold and warm p50/p95 separately with raw measurements.

## Evaluation design

### Three layers

| Layer | Purpose | Run frequency |
| --- | --- | --- |
| Rust unit/property tests and subprocess fault fixtures | Matching ambiguity, graph invariants, complete recipes, concurrency, malformed output, timeouts, invalidation | Every PR on Linux/macOS; add Windows parity for advertised binaries |
| Real Android CLI and Minimap against pinned Compose APKs | Actual layout formats, timing, actionability, animation, package/device isolation, graph portability | Focused cases per affected PR; full pinned matrix before release |
| Complete agent tasks with usage traces | Navigation reasoning, source-backed diagnosis, quiet recovery, product verification, alert quality, billed token/cost savings | Milestones and before making savings claims |

Use Android CLI to manage emulators, install/launch samples, capture layouts,
and capture screenshots when needed. Use its documented adb input primitives
for raw-baseline actions because the installed CLI has no input command.
Use Minimap itself for learning/replay arms. An independent fresh Android CLI
observation checks outcomes; a cached Minimap result is not its own oracle.
Serialize all live trials that share a device.

### App and environment matrix

| App | Core routes and adverse cases |
| --- | --- |
| Jetsnack | Tabs; detail and return; sibling details; repeated scroll plus tap; filters/expansion; external navigation; removed/renamed controls |
| JetNews | Drawer to Interests; article selection; article opened from different origins; scrolled content and changed labels |
| Jetchat | Conversation to drawer/profile and back; keyboard visibility; repeated names/avatars; parameterized navigation only when supported |
| Reply, follow-on | Adaptive layouts and list/detail navigation on a foldable or tablet |

Build clean pinned source in an isolated checkout and record commit, applied
patch hash, APK hash, JDK/Gradle/Android CLI/ADB versions, OS, emulator image,
API, resolution/density, font scale, locale, animations, Minimap commit and
binary hash. Leave the user's existing sample graphs and local edits intact.
Start with the current phone emulator, then add a second API level and a
different screen size. Test a second locale/font scale and a foldable before
claiming that a graph is broadly portable across a team.

Keep a held-out app/device slice for matcher evaluation. Do not tune the
similarity threshold on every case used to advertise accuracy. Label
same-place/different-place pairs and report false acceptance, false rejection,
ambiguity, and confusion by app and state, not just an aggregate score.

### Trial arms and controls

1. Raw Android navigation with layout-driven decisions and final verification.
2. Initial learning, including graph initialization and labeling overhead.
3. Cold reuse from a fresh checkout containing only committed graph/config.
4. Warm reuse from a genuinely verified starting observation, with warmup cost
   recorded separately and also included in a full-task view.
5. Navigation plus product verification, measured for both raw and Minimap.
6. Reuse after a controlled source change: destination grows, new place,
   selector renamed, option removed, viewport changes, and stale app session.
7. Team handoff: person/agent A learns; B receives a fresh checkout on another
   device; separate worktrees extend the map and their changes are merged.
8. Paired self-healing cases: an intentional route relocation and a broken
   callback that initially produce similar navigation failures; require a
   verified repair without notification for the first and a correctly
   corroborated diagnosis for the second.
9. Persistence of healing: after a repair, repeat the task in a fresh checkout
   and on a compatible second device; the repaired route must be used without
   repeated exploration, repeated repair writes, or user intervention.

For source-change cases, label the intended change/defect and expected graph
effect before the trial, build/install that exact variant, and withhold the
answer from the navigating agent. Include unchanged controls: transient
failure, external navigation, overlapping screen identities, feature-flag
differences, and a source/APK mismatch that must remain unclassified.
Test both a recoverable noncritical issue and a critical defect; the oracle
must evaluate diagnosis and notification as well as arrival at a screen.

Use identical APKs, start states, goals, verification expectations, and reset
procedures. Rotate/randomize paired arms and record ordering/seeds. Pin model,
prompt, tool permissions, and context for agent trials. Repeated scripted
coordinates are a mechanical baseline, not evidence of less agent reasoning.
Include failures, timeouts, recovery work, and graph maintenance in the totals.

Begin with five repeats per arm to debug the harness, then at least 30 paired
trials per representative route for timing distributions. Increase repetitions
when confidence intervals are too wide to make the decision. Run at least 300
adversarial navigation trials before a release reliability claim; zero false
successes in 300 observations still only supports an approximately 1% upper
95% failure bound under the independence assumptions, not a guarantee.

### Metrics and decision rules

- Correctness: independently reached destination, false success, wrong input,
  ambiguous match, blocked status, recovery success, and graph validity.
- Self-healing: classification accuracy, silent-recovery rate, recovery
  latency/cost, correct repair and retirement, recurrence after repair,
  incorrect durable repairs, and preservation of still-valid alternatives.
- User experience: interruptions per completed task, unnecessary alerts,
  missed critical issues, code-corroboration accuracy, and repeated alerts for
  the same unresolved cause; success requires quiet recovery and honest task
  completion, not merely fewer messages.
- Usage: input/output/reasoning/cached tokens where the provider exposes them,
  actual model/pricing version, billed cost, tool invocations, and bytes.
  Missing usage is `null`, never an inferred token count from characters.
- Timing: full task wall time, tool wall time, layout captures, device actions,
  graph load/match/plan/write time, p50/p95, paired deltas, and confidence
  intervals. Count nested Android/ADB calls separately from agent-visible
  commands without double-counting their wall time.
- Maintenance: additional learning cost, repair effort, variants/edges added
  or retired, merge conflicts, bytes changed, graph growth and replay quality
  after successive app changes.
- Break-even: let `L` be learning cost above the equivalent first raw task,
  `M` maintenance cost, and `R`/`G` raw/reuse costs; savings require
  `N * (R - G) > L + M`. Report no break-even when `R <= G`, and use full
  verification/recovery costs in both arms.

Release gates are zero false success/wrong-app actions in the defined critical
suite; all persistence/concurrency/migration invariants pass; at least 99%
success on supported unchanged routes in the full live matrix; all defined
recoverable-change cases complete without user interruption; zero incorrect
durable repairs or unnecessary critical alerts in the labeled critical suite;
all seeded critical product defects are surfaced with code corroboration;
and a measured positive amortized benefit on representative complete agent
tasks, including recovery and source-inspection overhead. Fail a
change that buys speed through weaker validation. Establish route-specific
timing budgets from the pinned baseline; investigate a repeatable p95 regression
above 10% rather than hiding it inside an aggregate.

Keep redacted, licensed fixture data and machine-readable metric summaries in
the repo. Keep raw device dumps, temporary graphs, credentials, large traces,
and runtime caches outside it. Publish reports with environment manifests,
commands, successful and failed trial counts, and explicit limitations.

## First implementation slice

Start with fresh-source verification, failure invalidation, and bounded
reorientation/replanning, proving that external navigation is recovered without
a user-facing error. Follow with complete pending recipes, full-recipe IDs,
and repository/device locking so repairs can be persisted safely. Then deliver
the source-aware host-agent recovery loop and matching canonical skill update:
the agent must heal a renamed/relocated route, reuse the repaired graph from a
fresh checkout, and distinguish it from a seeded critical callback defect.

These are small separate changes, but the first milestone is the complete
self-healing workflow, including verification and notification behavior.
Run focused real-device and fault evals after each. Broader matching/lifecycle
work, performance tuning, and the full multi-app/team benchmark follow.

## GitHub rename work in this pass

The main repo's Cargo repository metadata, README commands, Claude plugin
metadata, Homebrew template, and release instructions now use `mttmcknn`.
Both the main checkout's origin and the installed Homebrew tap's origin have
been updated. All seven Cargo packages inherit the corrected repository URL.

The separate published Homebrew formula is covered by
[PR #1](https://github.com/mttmcknn/homebrew-minimap/pull/1).
Its new v0.1.3 source URL was downloaded and its existing SHA-256 verified.
The PR merged on 2026-09-16 as `5dceab25e9d68a82c06652c4f69cae56b42ab820`,
updating the published tap after all three platform checks passed. Older
historical commits and the installed tap directory's local alias are not rewritten.
