# Replay performance and AI cost experiment

Registered September 22, 2026, before performance changes or new measured trials.
Baseline source: `5c9298e` (v0.2.0 code plus README edits). Preserve the original
September 16–17 reports. Freeze release binaries, sample APKs, the protocol, and
the runner before each measured cohort; save their hashes in the report.

## Goals

- Make saved-route navigation faster than both the current Minimap release and
  the known-route Android script, without removing destination or app checks.
- Measure total agent input, cached input, output, reasoning, and elapsed time,
  including instructions and all tool interactions.
- Report measured token savings separately from modeled dollar savings; never
  call subscription usage an invoice or substitute text size for model usage.

## Device experiment

Use the same pinned Compose samples, goals, and independent final screen checks
as `SUITE_V1.md`, on APIs 36 and 37. Recover the installed baseline APKs only if
their SHA-256 values match the earlier report; otherwise rebuild pinned sources.
No personal apps or physical devices are involved.

Compare three arms in rotating matched blocks:

1. `raw`: the existing known-route script, with fresh layouts and a final check.
2. `baseline`: the frozen unmodified Minimap binary, using a copied verified graph.
3. `candidate`: the optimized frozen binary, using the same copied graph.

Each replay starts in a fresh directory without runtime cache. Learn each seed
once using the baseline, outside measured navigation. Reset the app and verify
Home before each trial. A separate Android layout grades the destination after
each measured attempt. Preserve setup, grader, learning, failed attempts, and
graph hashes separately; do not retry a failed measurement into a success.

Run diagnostic profiles first, followed by small development pilots. A profile
wraps `android` and `adb` to record subprocess durations; those wrappers add
overhead, so profiled runs cannot establish a speed claim. Final confirmation
uses no wrappers, 10 matched blocks per app/API, and 180 assignments total.
Run one device worker at a time on this shared host. Report per-app/API medians,
all paired differences, observed ranges, and a separate accounting of failures.
Do not stop the registered confirmation run when a favorable result appears.

Success requires zero false successes, all destination checks passing, no
unexpected graph changes, and lower candidate paired median navigation time
than both controls in every app/API case. Small or inconsistent differences must
be described as inconclusive; a faster aggregate cannot hide a slower case.
Retain wrong-person, external-navigation, changed-route, bounded-recovery,
viewport, and delayed-UI regression coverage before publishing a speed claim.

## Agent usage and cost experiment

Prepare 18 sequential, isolated Codex trials on API 36: three apps × raw/reuse ×
three matched blocks, alternating order. Use the configured model and reasoning
settings consistently, record their exact values, and give both arms identical
destinations and final checks. Each trial has a 180-second deadline and a
32-input cap. This is an initial measured comparison, not a reliability SLA.

The raw agent chooses actions from fresh layouts; the reuse agent receives the
normal navigation instructions and a copied verified graph. Include instruction
loading in measured usage. Do not feed grader observations to the agent. No
agent may delegate further, access personal apps, use external services, or
write outside its fixture. Launch these additional agents only after the user
explicitly authorizes the batch, as required by this session's delegation rule.
Device profiling and optimization proceed independently while that is pending.

Capture `codex exec --json` usage, completion status, tool calls, wall time,
input count, graph changes, and the independent destination result. Missing
fields stay null, including usage lost to a timeout. Cached input is part of
input, and reasoning is part of output: do not count either twice. Require
complete usage evidence before asserting a token-saving result. Preserve
failed trials in the denominator and report success alongside cost.

Any dollar figure uses a dated, model- and service-tier-specific price record
from [official pricing](https://developers.openai.com/api/docs/pricing).
Distinguish cache reads, cache writes, and ordinary input. If CLI events omit
cache-write or per-request context-tier details, report the resulting uncertainty
or cost bounds rather than inventing those fields. API-equivalent estimates
are not the user's ChatGPT subscription charges. See the official
[usage events](https://learn.chatgpt.com/docs/non-interactive-mode) and
[cache accounting](https://developers.openai.com/api/docs/guides/prompt-caching).

The initial agent target is lower median total tokens, modeled cost, and
end-to-end time at equal correctness. Report every app separately. Learning
and repeated-use payback need their own measured cohorts; do not infer them
from one replay or from tool-text-only projections.
