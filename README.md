# Minimap

Android navigation memory for AI agents.

Minimap helps an AI remember how to get around an Android app. Teach it a route
once, such as Home → Settings, then ask it to follow that route and check the
screen it reaches. Following a saved route does not need a new AI decision for
every tap.

The routes live in your project's `.minimap/` folder. Commit them to Git so your
teammates and their AI agents can use what has already been learned.

## Results

**Our latest test build reused saved routes in about 4–7 seconds, compared
with 7–11 seconds without Minimap.** It reached the right screen in **60/60
trips**. Across the six app/Android combinations, the middle time saving in
matched runs was **30–45%**.

These results are from the experimental build in [PR #12](https://github.com/mttmcknn/minimap/pull/12),
which has not been released. The full comparison was **179/180 verified**:
one released-version control's separate screen checker timed out. That means
our strict rule that every test must pass **has not been met**.

### Navigation time

We tested the same short routes in three Android sample apps on two emulators
running different Android versions (APIs 36 and 37). Each route took one or two
taps. Every method already knew the route; no AI chose the individual steps.

| Method | What it does |
| --- | --- |
| Without Minimap | Uses regular Android tools to follow the known route and check the screen. |
| Previous Minimap | Reuses the saved route with released Minimap v0.2.0. |
| New Minimap | Reuses the same route with faster screen reads and a check that buttons have stopped moving before a tap. |

![Typical navigation times: the new test build takes about 4–7 seconds, compared with 7–11 seconds without Minimap; one released-version checker timed out.](evals/results/2026-09-23-replay-performance/replay-summary.png)

Each bar is the **middle result (median)** from up to ten successful runs for
that app and Android version. Shorter bars are better. Startup, the one-time
work of recording the route, and the separate test checker are outside these
times. The new build was faster in **58 of 60 matched trips** against the
no-Minimap script; the two slower trips remain in the detailed graphs.

A saved route still runs through one `minimap go` call. Minimap reads the
screen, taps, and verifies the destination without asking an LLM to decide
each step. Much of this build's speed improvement comes from faster screen reads.

### Less text for the AI to read

A *token* is a small chunk of text an AI reads. In this run, the new build
returned **about 83–88% less navigation-tool text** than the tools used without
Minimap.

![Navigation-tool tokens: Jetsnack drops from 1,979 to 244, JetNews from 1,471 to 257, and Jetchat from 1,598 to 244.](evals/results/2026-09-23-replay-performance/replay-text.png)

These bars combine both Android versions and count each tool response once.
**We have not measured the full AI-token cost or money saved.** Instructions,
reasoning, previous conversation text, and caching also affect the total.
Loading the full Minimap instructions for every short task can erase the
saving shown here.

### What still needs testing?

- **The full AI task:** the prepared agent comparison must measure actual input, output, timing, and API-equivalent cost, including instructions and reasoning.
- **Recovery without help:** all 16 scripted change/error checks passed, but an AI discovering and repairing unexpected navigation on its own remains untested.
- **Broader use:** these were short routes on two emulators, so longer routes, physical devices, larger graphs, and more app states still need evaluation.

See the [complete results, every-run graphs, failures, and raw evidence](evals/results/2026-09-23-replay-performance/README.md),
including a graph showing the time saved or lost in each matched trip and
instructions for rebuilding all the graphs.
The [earlier failed test build](evals/results/2026-09-23-performance-progress/README.md)
and the [released-version benchmark](evals/results/2026-09-17-benchmarks/README.md)
remain available with their original results.

## Install

From a checkout:

```bash
cargo build -p minimap-cli --bin minimap
```

Building requires Rust 1.89 or newer.

From source after publication:

```bash
cargo install --git https://github.com/mttmcknn/minimap minimap-cli
```

## Basic Workflow

Initialize the repo and install agent skills:

```bash
minimap init --agents all --package com.example.myapp
minimap doctor
```

Label the current place:

```bash
minimap whereami --label home
```

Navigate and learn a verified transition:

```bash
minimap tap --selector "text=SEARCH" --label search --reason "open search"
```

Reuse the graph:

```bash
minimap go search --expect "text=Categories"
```

Saved-route replay uses fresh UI observations and waits for stable tap positions
before acting. If its faster observer cannot recognize a screen, it
automatically checks again using Android CLI. Use `go --android-cli-layout`
to select Android CLI throughout when diagnosing device compatibility.

Use raw layout only when the agent needs details Minimap does not model:

```bash
minimap layout
```

`layout` returns redacted Android layout plus Minimap orientation metadata.
Unlabeled `whereami` returns compact orientation. If either immediately follows a
fresh verified observation, Minimap can serve the cached session state instead
of paying for another Android layout capture.
Use `layout --fresh` or `whereami --fresh` after external changes and for current
product assertions. Every `go` observes the real starting screen, even when the
graph suggests that it is already at the destination.
Repeat `--expect` for every required visible anchor. Each must match exactly one
visible element, including for an already-at-target request. A missing anchor
returns `goal_mismatch`; reaching a generic screen does not prove the right item
or account. A successful check avoids a separate full-layout read when it
covers the task's verification needs.

## Self-healing navigation

Minimap replans from observed destinations, excludes failing edges, and tries
other verified routes within an action/time budget. Unfamiliar changes return
a compact recovery handoff to the host agent. The installed skill directs the
agent to inspect fresh UI and matching source, learn a replacement, verify it,
and resume the task quietly. A mismatch alone is not a product defect.

The first `go` returns `data.recovery.token`. Continue the same goal with
`--recovery <token>` on every discovery/replay command. The token retains the
original goal checks, failed edges, and budget across processes and time spent
in the agent. Defaults are 32 inputs and 60 seconds; set `--max-actions` and
`--recovery-seconds` on the initial request if needed. Retries cannot extend
that token's budget. The host must carry it forward instead of starting a new
goal for each retry.

After verifying a changed screen, the agent can explicitly attach its new
appearance with `whereami --confirm-place <existing-place-id>`. From an obsolete
edge's original source, `go <destination> --supersede <edge-id>` prefers a
replacement only after it reaches the same destination and passes the goal
checks. The old route remains a fallback for other builds or supported states.
Critical product bugs are escalated with reproduction and code evidence.

Device and repo locks serialize Minimap operations. Writes are atomic, place
IDs survive relabeling, and runtime caches are scoped to the repo, device,
package, process, and installed build. Commit `.minimap/` for teammates to reuse;
run `minimap doctor --repo-only` in CI without an emulator.
Recipes are validated before any input, and post-action learning requires stable UI.
Editable/password content is redacted before caching and fingerprinting;
obvious sensitive labels, intents, and selectors are rejected. Unmarked personal
text still needs agent judgment.

Current boundaries: one app per graph, screen-level identity, and explicit UI
return routes for reusable navigation. Generic detail screens do not prove a
particular item was reached, and the planner does not replay Back recipes
without history it can verify. New edges use `minimap.edge.v2` for fallback
preferences; existing lean v1 edge records load without rewriting files, and older clients reject
v2 rather than silently ignoring its meaning. Upgrade teammates together.
Total AI token savings still need measured agent trials; the results above
count only text returned by the tools.

## Commands

The v1 command surface is intentionally small:

```text
minimap init
minimap doctor
minimap whereami
minimap go
minimap tap
minimap scroll
minimap back
minimap layout
```

All commands return compact JSON by default; use `--pretty` for indented output.
Graph changes are reported with `changed_graph: true` and `changed_files`.

## Graph State

`.minimap/` contains only committed graph/config files:

```text
.minimap/
  config.json
  graph/
    places/
    edges/
```

There are no proposals, journals, run directories, or hidden repo-local runtime
state. Review graph changes through normal git diffs and PRs.

## Claude Code Plugin

Claude Code users can install the Minimap skill from this repo's plugin
marketplace.

From Claude Code, add the marketplace:

```text
/plugin marketplace add mttmcknn/minimap
```

Then install the plugin:

```text
/plugin install minimap@minimap
```

For local development from a checkout:

```text
/plugin marketplace add .
/plugin install minimap@minimap
```

The plugin ships `minimap-app-navigation`, the same skill `minimap init`
installs for everyday Minimap navigation and incremental graph growth.

## Live Device Smoke

With an Android app already built, installed, and launched plus `android` and
`adb` on `PATH`:

```bash
minimap doctor
minimap whereami --label home
minimap tap --selector "text=SEARCH" --label search --reason "open search"
minimap back
minimap go search
```

If more than one device or emulator is attached, pass `--serial <SERIAL>` on
any command (or set `ANDROID_SERIAL`) so every `adb` and `android` call targets
a single device; `minimap doctor` flags ambiguous multi-device setups.

For controlled raw navigation, first-use learning, graph reuse, and recovery
comparisons, see [Evaluations](evals/README.md) and the
[90-trial controlled results](evals/results/2026-09-16-controlled.md) and
[benchmark graphs](evals/results/2026-09-17-benchmarks/README.md).
The next development steps and release gates are in the
[hardening plan](docs/MINIMAP_HARDENING_PLAN.md).

For broader manual validation, clone the public
[Android Compose samples](https://github.com/android/compose-samples) and build
one of its apps (Jetsnack, JetNews, or Jetchat):

```bash
git clone https://github.com/android/compose-samples
```

Then build and launch a sample (for example Jetsnack) from the cloned
`compose-samples/` checkout and run the smoke commands above against it; see
[docs/MINIMAP_V1_LEAN_DESIGN.md](docs/MINIMAP_V1_LEAN_DESIGN.md).
