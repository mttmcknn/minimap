# Controlled evaluation suite v1

Registered before execution on 2026-09-16. Freeze the tested binary and APKs;
record their SHA-256 values in each run. Product fixes discovered during this
suite belong in a subsequent version, with the original results preserved.

## Questions and controls

1. Does committed navigation memory improve repeated navigation over raw Android tools?
2. What does initial graph learning cost, and how many later uses amortize it?
3. Does recovery repair legitimate route changes without learning product bugs?
4. Can an independent agent realize the benefit in measured model tokens?

The primary device experiment has three arms:

| Arm | Starting memory | Measured work |
| --- | --- | --- |
| `raw` control | No graph | Fresh Android layouts, selector-based adb inputs, final visible-goal verification |
| `new_graph` learning control | Empty isolated repo | Initialize Minimap, label Home, learn every action, verify the requested destination with `go --expect` |
| `reused_graph` treatment | Only a copied verified graph; no runtime cache | One `go --expect` from a fresh observed source |

All three arms get the same goal, starting state, APK, and visible checks.
The scripted control knows the route and is an optimistic lower bound for raw
navigation; it does not measure model discovery. A separate independent
Android layout grades the final screen in every arm. No oracle data is passed
back into the measured task. Setup, reset, oracle, seed-learning, and primary
navigation costs are all recorded separately.

## Cases and sample size

- Jetsnack: Home → Search; verify Categories, Lifestyles, and SEARCH.
- JetNews: Home → drawer → Interests; verify Topics, People, and Publications.
- Jetchat: conversation → drawer → Ali Conors profile; verify Ali Conors and Edit Profile.

Five matched blocks per app on both API 36 and 37 yield **90 device trials**:
3 apps × 3 arms × 5 repetitions × 2 emulator versions. Rotate arm order within
each block and offset by device. Restart the app and independently verify Home
before every trial. Never run two workers on one emulator. Shared host load can
affect both devices; report per-device results rather than treating devices as
independent hosts. No app data is cleared and no personal device is used.

The recovery/negative suite uses the pinned renamed-control, relocated-route,
and wrong-callback APKs. A frozen stale graph is the control: replay it once,
record the failure and unchanged digest, then enable the source-informed host
repair workflow. Grade replacement replay, teammate reuse, and older-build
fallback. Repeat the matrix on both APIs. Also run wrong-person, external
navigation, rename/relearn, and real Git merge/conflict checks.
These paired before/after recovery cases are contract tests with known changes,
not blinded diagnosis trials or a statistical sample of product changes.

## Independent agent experiment

The agent layer uses the configured Codex model and reasoning setting unchanged
across arms, fresh conversations without this task's history, isolated repos,
the same goals, and an independent final oracle. Record input, cached input,
output, and reasoning tokens from `codex exec --json` usage events; missing
usage is `null`, never an estimate derived from bytes. Do not infer invoiced
dollars from subscription usage. See the official
[non-interactive usage documentation](https://learn.chatgpt.com/docs/non-interactive-mode).

Initial plan: **18 navigation trials**, 3 apps × 3 arms × 2 blocks, plus **6
diagnosis trials**, renamed/relocated/broken × raw/reused graph. Each trial has
a 180-second deadline, 32-input cap, and no graph/source writes outside its
isolated fixture. All agents see matching sample source but no fixture change
manifest or expected diagnosis. The agent must discover route changes from
observations and source. Preserve first attempts and their failures.
For every Jetsnack agent arm, the destination oracle uses Categories and
Lifestyles, which survive the legitimate Search → Discover label change.
The baseline-only device benchmark also checks its unchanged SEARCH label.

Device operations pass through a bounded local command bridge so the controller
can count inputs and grade outcomes. Start only after explicit authorization
for additional agents, as required by this session's delegation rule. The
device-only suite can proceed independently.

## Metrics and predeclared gates

- Correctness: independently reached goal, false success, unresolved status, graph validity, unexpected graph mutation, and whether a repaired graph is reusable.
- Cost: wall time, measured subprocess time, top-level calls, response bytes, input actions, graph bytes/files changed, and real model usage where available.
- Require zero false successes and zero negative-case graph corruption; require at least 95% destination success in each primary arm, with reused-graph success no lower than raw.
- Report response-size and latency changes as hypotheses, not automatic release passes; the target is at least 50% fewer response bytes on reuse and non-inferior correctness.
- For agent trials, target at least 20% lower median total tokens on reuse at equal correctness; include all failures and initial learning when calculating amortized benefit.
- Compute break-even separately for time, bytes, and measured tokens; if per-use savings are zero or negative, report no demonstrated break-even.

Report every planned trial, with setup failures and watchdog timeouts retained.
Use paired differences by app/device/repetition, median and range; optional
bootstrap intervals are descriptive at this sample size. Do not claim p95,
an SLA, or universal portability. The statistical unit is the matched block,
not each nested shell command.

## Execution and artifacts

`paired_navigation.py` runs one device worker; `analyze_suite.py` consolidates
workers and their paired effects. Existing recovery and negative scripts are
run with the same frozen binary. Keep raw UI and agent traces outside the repo;
commit the protocol, runners, compact JSON summaries, and a readable report.
Every report includes which layers actually ran, exact binary/APK provenance,
all failures, and which gates passed, failed, or remain unmeasured.

Preflight amendment: the installed Android CLI returned success without launching
an app for an activity-only `android run` invocation. Both initial workers failed
the Home oracle before assigning any measured trial. Preserve those artifacts
and use the pinned APK with `android run --apks` for every reset; the comparisons,
sample size, and gates are unchanged.

Follow-up amendment v1.2: the first APK-reset run also exposed empty startup
layouts and null-root responses; one worker stopped on its between-app reset.
Preserve that run as v1.1, including unassigned trials, and run a separate full
90-trial replication. In v1.2, allow up to 30 seconds of fresh layout captures
to establish Home before a trial; all attempts count as setup cost. Do not
retry navigation outcomes or final oracles. Remove the unnecessary reset after
each sample, and record seed failures against every affected assignment instead
of aborting the remaining samples. The product binary, APKs, measured work,
arm order, sample size, and acceptance gates remain unchanged.
