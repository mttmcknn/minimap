# Self-healing implementation and evaluation — 2026-09-16

The first self-healing milestone is implemented in the working tree. Minimap
observes the actual starting screen, verifies replay destinations, replans around
failed edges, and returns bounded recovery evidence to the host agent. The agent
can confirm a changed appearance and retire an obsolete edge after replaying its
replacement. Real Compose trials caught two false-success risks and drove fixes.

This is a development validation pass, not evidence that every hardening-plan
release gate has passed. The measurements and run-specific binary hashes are in
[the JSON report](2026-09-16-self-healing.json); runnable commands are in the
[evaluation guide](../README.md).

## What changed

- Fresh observations replace cached starting-point assumptions for every `go`, including zero-edge plans, while bounded replanning excludes failed edges without learning unexpected destinations.
- Device/package guards, process/build-scoped caches, strict layout parsing, unique actionable selectors, subprocess deadlines, and viewport checks prevent several classes of unsafe input.
- Complete pending recipes preserve repeated scrolls and drawer-opening taps, stable place IDs survive renames, and full-recipe edge hashes avoid overwrites.
- Atomic file replacement and OS locks serialize Minimap operations on a repository/device; module-directory invocations find the project graph, and `doctor --repo-only` works in CI.
- Matching rejects ambiguous identities and conflicting page content, automatic variants remain close to the original baseline, and a sixteen-variant limit bounds growth.
- Weighted deterministic planning considers action/verification cost; compact JSON is the default and `--pretty` preserves readable output.
- The canonical agent skill explains quiet diagnosis, fresh UI/source corroboration, shared recovery budgets, explicit appearance confirmation, verified supersession, and when a critical product defect warrants an alert.

## Live outcomes

Android CLI installed and launched the APKs and supplied independent fresh
destination checks. Minimap performed learning and replay; raw fixture setup
used adb input. Temporary checkouts, graphs, caches, APKs, and detailed traces
live outside the source repository.

| Case | Observed result |
| --- | --- |
| Jetsnack external navigation | `go home` reoriented from the actual Search screen and reached Home; it did not repeat the baseline's cached zero-edge false success. |
| Search renamed to Discover | The old selector failed without graph mutation; the controlled host confirmed the same Search content, learned and replayed a replacement, retired the old edge, and replayed from a fresh teammate graph. |
| Search moved behind Profile | The controlled host learned Home → Profile → Search, verified supersession, and replayed from a fresh teammate graph. |
| Search callback incorrectly opens Cart | Both reproductions returned `unknown`; an independent Subtotal/content check and the seeded callback change corroborated the defect, and the shared graph stayed unchanged. |
| JetNews drawer → Interests → Home | All three round trips passed; each direction retained its two-action recipe and used a fresh graph copy. |
| Jetchat drawer → Ali Conors profile → conversation | All three round trips passed; an independent oracle checked the profile name and Edit Profile control, then the conversation title. |
| Jetsnack scroll → scroll → Search | The final binary preserved all three actions and replayed them successfully from a fresh graph copy, with an independent Search oracle. |

The two repair scenarios each used seven Minimap commands and zero interactive
decisions in the scripted host workflow. Full host time, including intervening
fixture/oracle work, was 62.87 seconds for the renamed control and 68.92 seconds
for relocation, consuming five and eight actions respectively. Both stayed
inside their shared 180-second/32-action budgets. Minimap subprocess time alone
was 47.39 and 56.13 seconds; it is not the complete repair cost.

The host is deliberately given the fixture changes. These tests establish the
CLI/host repair contract and graph reuse, not an arbitrary model's ability to
diagnose unseen changes or its real-world notification accuracy.

## Usage and timing

The final compact-output comparison ran three rotated trials per arm on API 36.
Every trial passed an independent destination oracle. Primary timing excludes
deployment, reset, warm-up, and the extra independent oracle. The raw arm uses
layout → tap → layout; the Minimap arms use `go` → `layout`.

| Arm | Success | Median seconds | Top-level commands | Response bytes |
| --- | ---: | ---: | ---: | ---: |
| Raw Android | 3/3 | 6.654 | 3 | 6,017 |
| Cold Minimap | 3/3 | 7.744 | 2 | 3,667 |
| Warm Minimap | 3/3 | 7.764 | 2 | 3,667 |

Minimap returned 39.1% fewer bytes and required one fewer top-level command in
this comparison, but took about 1.1 seconds longer. A preceding five-trial run
with indented output also passed all fifteen trials; raw/cold/warm medians were
6.743/7.681/7.678 seconds. Compact output reduced Minimap's 5,676 response bytes
to 3,667 without changing the parsed JSON contract. Warm reuse still performs
a fresh source observation, so it no longer obtains speed by trusting stale UI.

Learning the two-direction Jetsnack tab graph cost four Minimap commands and
20.06 seconds in the compact-output run. Initial JetNews/Jetchat drawer learning
cost six commands and 30.94/30.65 seconds. Their three-trial round-trip medians
were 22.36/22.81 seconds, each comprising two `go` commands with four actions
total; there was no paired raw drawer benchmark in this pass.
The final repeated-scroll recipe took 14.18 seconds to replay; its learning
cost was six commands and 35.42 seconds. Search was visible throughout, so this
does not establish off-screen item discovery.

Actual model tokens, reasoning cost, and billed cost are unmeasured (`null` in
the reports). Byte counts are not token estimates. These one-hop timings show
no wall-time break-even; complete agent tasks may behave differently and must
be measured before making savings claims. Small samples do not establish p95
latency, a reliability SLA, or broad device portability.

## Validation loops and failures

The original baseline had a confirmed external-navigation false success. The
fresh-start implementation fixed it and passed focused emulator reruns.

On API 37, a narrow window dump omitted focus information, so the adapter now
reads the full window dump. Renaming Search exposed the need for an explicit,
guarded appearance-confirmation path. Later API 37 runs repeatedly failed with
`null root node returned by UiTestAutomationBridge` after APK installation even
though the app was visibly present. Those partial failures were retained; the
complete change matrix ran on a cold-started API 36 emulator. API 37 accessibility
compatibility remains unresolved rather than counted as a passing run.

The first API 36 relocation run exposed Profile being classified as Search at
confidence 0.8039 because they shared navigation controls. A small captured
fixture now reproduces that mistake, and the matcher requires independent page
content evidence. The complete repair matrix passed after this fix. Review also
found direct scrolling guessing 1080×2400 when the viewport query failed; a
failing subprocess regression reproduced the unwanted input, and the command
now fails before scrolling.

The first scroll trial reached Search correctly but failed during cleanup:
switching tabs preserved scroll position, while the cleanup oracle expected
unscrolled Home content. The fixture now restarts the app without clearing data
to restore that specific state; the complete rerun passed. This partial run is
also retained in the JSON report.

The final Rust suite passes **135 tests**, including alternate-route recovery,
wrong destinations, ambiguous matching, repeated actions, supersession,
concurrent device contention, corrupted caches, restart invalidation, malformed
layouts, unavailable viewports, and deadlines including device preflight.
Formatting, Clippy with warnings denied, the release build, and the skill
validator pass. A release-mode planner probe traversed a 10,000-place cycle in
9.54 ms; that is one local planner measurement, excluding graph I/O and matching.

## Provenance and remaining work

The implementation starts from Minimap commit
`e838065b7b9f837f11233e452afc47e8bbdeefb0`; changes are uncommitted. Samples were
built in isolated clones of `android/compose-samples` at
`d3ff757b289f7036815978a8f7b16706ee3423b0`. The reproducible builder generated
Jetsnack, JetNews, Jetchat, and all three changed Jetsnack APKs; its source
changes and APK hashes are in the JSON manifest. The original sample checkout
and its existing graph/agent files were preserved.

The host is macOS Darwin 25.6.0 arm64 with Rust 1.98.1, JDK 21, Gradle 9.4.1,
compile SDK 36, Android CLI 1.0.15498356, and adb 37.0.1. Full runs used API 36
at 1080×2400; earlier partial runs used API 37 at 1080×2424. Run-specific hashes
distinguish recovery, drawer, timing, and compact-output builds; they are not
presented as one frozen binary or one statistical sample.
The final binary adds the unavailable-viewport scroll guard after the timing
run and is the binary used by the successful repeated-scroll trial. API 36
display settings captured afterward were density 420, font scale 1.0, and
default locale en-US; raw animation settings are preserved in the manifest.

Remaining release work includes blinded agent tasks with actual usage traces,
more adversarial trials and held-out matching data, locale/font/foldable coverage,
cross-device and Git-merge workflows, contextual edge lifecycles, and stronger
privacy fixtures. Identity currently represents a screen type; callers must
independently verify item/account/form state. Each graph supports one app, and
the planner excludes Back recipes whose arrival history it cannot establish.

GitHub repository metadata, commands, plugin metadata, release documentation,
and the Homebrew template now use `mttmcknn`, and the local origins were updated.
The separate published formula fix merged in
[Homebrew PR #1](https://github.com/mttmcknn/homebrew-minimap/pull/1), with all three
platform checks passing. Its merge commit is
`5dceab25e9d68a82c06652c4f69cae56b42ab820`; the installed tap's legacy local alias
and the installed Minimap binary are unchanged.
