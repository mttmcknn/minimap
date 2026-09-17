# Minimap evaluations

The [September 16 hardening report](results/2026-09-16-self-healing.md) records
the completed Compose trials, failures that led to fixes, and current limits.
The [follow-up report](results/2026-09-16-ethos.md) adds persistent recovery
budgets, goal checks, preserved fallback routes, and another emulator version.
The [controlled suite report](results/2026-09-16-controlled.md) records the
90-trial raw/first-use/reuse comparison, separate recovery and Git controls,
retained setup failures, and the unmeasured agent-token layer.
The [benchmark graphs](results/2026-09-17-benchmarks/README.md) show every trial,
exact saved-tool-text token counts under two reference encodings, paired timing
effects, and learning-cost projections with explicit skill-loading overhead.
They include PNG/SVG exports and portable CSV/JSON data; they do not claim
measured model usage or billing savings.

## Controlled baseline suite

[SUITE_V1.md](SUITE_V1.md) defines the hypotheses, control groups, matched trial
order, correctness gates, and reporting rules before execution. The device
layer compares raw Android navigation, first-use learning, and a copied graph
without runtime state across Jetsnack, JetNews, and Jetchat. Each device runs
45 trials. Use two separate emulators, one worker per device:

```sh
python3 evals/paired_navigation.py \
  --binary /absolute/path/to/frozen/minimap \
  --apks /absolute/path/to/fixtures/apks \
  --serial emulator-5556 --repetitions 5 --order-offset 0 \
  --output /absolute/path/to/new-api36-run
python3 evals/paired_navigation.py \
  --binary /absolute/path/to/frozen/minimap \
  --apks /absolute/path/to/fixtures/apks \
  --serial emulator-5554 --repetitions 5 --order-offset 1 \
  --output /absolute/path/to/new-api37-run
python3 evals/analyze_suite.py \
  /absolute/path/to/new-api36-run/results.json \
  /absolute/path/to/new-api37-run/results.json \
  --output /absolute/path/to/new-summary
python3 evals/audit_trial_graphs.py \
  /absolute/path/to/new-api36-run/results.json \
  /absolute/path/to/new-api37-run/results.json \
  --binary /absolute/path/to/frozen/minimap \
  --output /absolute/path/to/new-graph-audit.json
```

Freeze the binary, APKs, runner, and protocol hashes before launching workers.
Version changes to the harness and preserve each prior run separately. The
current v1.2 runner allows 30 seconds for Home to become observable before
measuring a trial; all readiness captures count as setup cost. It never retries
a measured navigation or failed final oracle. Seed failures produce failed
assignments rather than silently shrinking the sample. The analyzer reports
unstarted and missing trials, matched cost differences, descriptive bootstrap
intervals, and break-even only where all arms succeeded. It rejects mixed
binaries, protocols, APKs, and duplicate assignments. The graph audit runs
without a device and checks that reused graphs and `doctor` remain read-only.

The scripted raw control already knows the route, so this layer measures tool
overhead and response size rather than an agent's discovery or reasoning.
First-use measurements include `init`, labeling, and route recording. Minimap
replay is one top-level call; nested device inputs are not independently
instrumented in this layer. Setup, the separate destination oracle, and seed
preparation remain visible in the raw results. Never convert response bytes to
claimed model-token savings.

`agent_trial.py --prepare` writes a reviewable isolated trial without starting
an agent or touching a device. `--run` launches an independent Codex session
and must follow the invoking session's delegation authorization. The planned
24-trial agent layer uses real JSONL usage, a 180-second deadline, a 32-input
bridge cap, matching app source, and a separate grader. It is a prepared
experiment until its actual runs and usage are recorded; missing usage stays
`null`. Its evaluator regressions run in CI:

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
```

After a paired worker has finished, run its recovery and negative controls on
the same emulator. This tests stale graph replay against a renamed tab, a
relocated route, and a wrong callback; then wrong-person rejection, external
navigation, and rename/relearn stability. It uses a bounded readiness wait
before fixture setup and records independent failed cases without replacing
them with retries:

```sh
python3 evals/controlled_contracts.py \
  --binary /absolute/path/to/frozen/minimap \
  --apks /absolute/path/to/fixtures/apks \
  --paired-run /absolute/path/to/new-api36-run \
  --serial emulator-5556 --output /absolute/path/to/new-contract-run
```

The source-informed repair steps are scripted, so passing these controls
establishes the recovery contract, not independent agent diagnosis. Use the
resulting `recovery/seed`, `recovery/renamed_control`, and
`recovery/relocated_route` with `team_graph.py` for the separate Git controls.

`benchmark_charts.py` audits the frozen controlled report and saved stdout/stderr,
or re-renders the checked-in benchmark ledger without raw logs or devices. Install
the optional dependencies in an isolated environment; the product and evaluator
tests do not need plotting or tokenization libraries. Output directories must be
new so historical results are preserved:

```sh
python3 -m venv /tmp/minimap-chart-env
/tmp/minimap-chart-env/bin/pip install -r evals/requirements-benchmarks.txt
/tmp/minimap-chart-env/bin/python evals/benchmark_charts.py \
  --data evals/results/2026-09-17-benchmarks/benchmark.json \
  --output /tmp/minimap-charts-rebuilt
```

To re-tokenize original strings, use `--report` with the controlled report and
`--raw-root` with the preserved raw artifact directory instead of `--data`.
Each stream is counted independently and once per phase; source report hashes,
byte counts, trial assignments, and timing totals must reconcile. The report
explains which costs are measured, projected, and still unknown.

## Earlier smoke runner

`jetsnack_smoke.py` is a small live baseline and regression probe, not the full
release benchmark. It records Home/Search navigation, measures raw Android,
cold Minimap, and warm Minimap navigation with product-verification reads, and
checks whether external navigation can make `go home` falsely report success.

Build the current binary, attach an emulator, and provide a Jetsnack APK built
from the Android Compose samples:

```sh
cargo build --release --locked -p minimap-cli
python3 evals/jetsnack_smoke.py \
  --binary target/release/minimap \
  --apk /absolute/path/to/Jetsnack/app/build/outputs/apk/debug/app-debug.apk \
  --serial emulator-5554 \
  --output /absolute/path/to/a/new/eval-directory \
  --repetitions 5
```

The output directory must not exist. The runner deploys the APK with Android
CLI, navigates the selected emulator, and creates isolated graphs and runtime
caches under that directory; it does not modify the sample checkout or its
existing Minimap graph. Device locks and recovery-token state remain in the
private host runtime directory so operations from different roots serialize.
Install `android`, `adb`, and Python 3 on PATH.
The tested Android CLI returns a flat JSON layout array with `content-desc`
and string `center` fields. An unsupported shape fails explicitly.

The raw arm uses Android CLI layout observations and adb input; the Minimap
arms invoke the real binary. An additional fresh Android CLI observation
checks the actual destination after every trial. Warm-up, reset, deployment,
and independent-oracle calls are recorded but excluded from the primary trial
timing; compare their costs separately when interpreting a complete task.
All calls use a 60-second subprocess timeout.

`results.json` includes environment hashes, per-trial metrics, learning cost,
the external-navigation result, and a command trace with timings and byte
counts. Individual stdout/stderr files stay beside it. Timing measures
top-level subprocesses; it does not instrument nested calls. These scripted
trials do not measure an agent's reasoning, token usage, or billed cost.
Use `--goal-checks` to test `go --expect` with the Search anchors and omit the
separate Minimap layout call. The raw arm and independent destination oracle
stay the same. Report this mode explicitly when comparing timing and bytes.

Exit status is nonzero if a trial fails or the external-navigation check finds
a false success. Preserve that failure in the baseline; do not weaken the
oracle to make a run pass. A harness exception also exits nonzero and saves
partial results where possible.

The [hardening plan](../docs/MINIMAP_HARDENING_PLAN.md) defines the larger
Compose-sample, fault, portability, concurrency, and agent-cost evaluations.

## Reproducible source changes and repairs

`build_compose_fixtures.py` clones a local Compose samples repository into a new
directory and checks out commit `d3ff757b289f7036815978a8f7b16706ee3423b0`.
It builds Jetsnack, JetNews, and Jetchat plus three Jetsnack variants: renamed
Search control, Search reached through Profile, and a Search callback that
incorrectly opens Cart. It restores source files after building and writes a
manifest of source changes and APK hashes. Set `JAVA_HOME` to JDK 21 and
`ANDROID_HOME` to the Android SDK; Gradle may download dependencies.

```sh
python3 evals/build_compose_fixtures.py \
  --source /absolute/path/to/compose-samples \
  --output /absolute/path/to/new-fixtures
python3 evals/compose_recovery.py \
  --binary target/release/minimap \
  --apks /absolute/path/to/new-fixtures/apks \
  --serial emulator-5556 \
  --output /absolute/path/to/new-recovery-run
```

The controlled host workflow learns and replays repairs, prefers replacement
edges only after success, and checks a fresh teammate graph copy. It also runs
the repaired graph against the older APK to verify fallback compatibility. The broken
callback must reproduce twice without changing the shared graph. Android CLI
provides the independent destination oracle. These are scripted contract tests;
they do not establish that an arbitrary agent diagnoses an unseen change or
that any number of billed tokens was saved. Preserve partial failed runs too.
The host fixture shares a 32-action, 180-second budget across each controlled
scenario; `--host-seconds` changes the latter. Within each unresolved goal, it
also propagates the CLI recovery token through discovery and replay commands.
The engine defaults to 60 seconds per goal, including device preflight,
subprocesses, and time spent between commands.

`compose_journeys.py` validates two-action drawer recipes and repeated round
trips using a fresh graph copy for each trial:

```sh
python3 evals/compose_journeys.py \
  --sample jetnews \
  --binary target/release/minimap \
  --apk /absolute/path/to/new-fixtures/apks/jetnews-baseline.apk \
  --serial emulator-5556 \
  --output /absolute/path/to/new-jetnews-run --repetitions 3
```

Use `--sample jetchat` with `jetchat-baseline.apk` for the chat/profile journey.
Its oracle verifies Ali Conors and the Edit Profile control independently of
Minimap's generic profile identity. Replay uses `--expect` checks and a negative
wrong-person check must return `goal_mismatch` without changing the graph.
It does not send messages or edit profiles.

`compose_scrolls.py` records two scrolls followed by a Search-tab tap, checks
that all three actions survive in the recipe, and replays from a fresh graph
copy. Search remains visible, so this tests recipe preservation rather than
finding a particular off-screen item. It restarts the fixture app to restore
the unscrolled Home state; it does not clear app data.

```sh
python3 evals/compose_scrolls.py \
  --binary target/release/minimap \
  --apk /absolute/path/to/new-fixtures/apks/jetsnack-baseline.apk \
  --serial emulator-5556 \
  --output /absolute/path/to/new-scroll-run
```

Run deterministic tests and a device-free repository check with:

```sh
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
minimap doctor --repo-only
```

`team_graph.py` uses the learned files from a completed recovery run to exercise
real Git merges in a disposable checkout. It verifies that independent added
routes survive the merge, and that contradictory edits to one place produce an
explicit conflict that `doctor` rejects without rewriting the graph:

```sh
python3 evals/team_graph.py \
  --binary target/release/minimap \
  --seed /absolute/path/to/recovery-run/seed \
  --additions /absolute/path/to/recovery-run/renamed_control \
              /absolute/path/to/recovery-run/relocated_route \
  --output /absolute/path/to/new-team-run
```

This validates merge integrity for disjoint additions, not automatic resolution
of competing source changes or replay of every merged context.

`compose_relabel.py` starts with a learned Jetsnack seed, renames Search to
Catalog, relearns the same transition, and checks that no duplicate edge file
appears. It also verifies current exit labels and replay from a fresh graph
copy after the rename:

```sh
python3 evals/compose_relabel.py \
  --binary target/release/minimap \
  --apk /absolute/path/to/new-fixtures/apks/jetsnack-baseline.apk \
  --seed /absolute/path/to/recovery-run/seed \
  --serial emulator-5556 \
  --output /absolute/path/to/new-relabel-run
```
