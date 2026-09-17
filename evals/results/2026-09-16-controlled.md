# Controlled navigation evaluation — 2026-09-16

The readiness-adjusted suite passed **90/90 navigation trials** on Jetsnack, JetNews, and Jetchat across Android APIs 36 and 37. Reused graphs returned **81.2–86.6% fewer stdout bytes**, but their paired median navigation times were **0.58–1.81 seconds slower** than the scripted raw control. This demonstrates compact reusable navigation, not a measured model-token or speed saving.

The runtime binary and APKs stayed frozen. The earlier run and its failed setup attempts remain separate; no failed navigation was silently replaced with a retry.

## Baselines and treatment

[The registered protocol](../SUITE_V1.md) defines three arms: raw Android layouts and adb inputs; an empty graph including initialization and route learning; and a fresh checkout of a verified graph with no runtime cache. All drivers know the route. Each arm gets the same starting state and destination checks, plus an independent Android layout oracle after navigation. Five blocks per app/device rotate the arm order, yielding 30 trials per arm.

The tasks are Home → Search in Jetsnack, drawer → Interests in JetNews, and drawer → Ali Conors in Jetchat. These are short routes of one or two input actions; model discovery is not part of the scripted measurement.

## Timing and response size

Every arm passed 5/5 in each row below. Times are median **navigation subprocess seconds**, with setup and the independent oracle recorded separately. The paired delta is the median of the five within-block reuse-minus-raw differences, which can differ from subtracting the two arm medians.

| App | API | Raw | First use | Reused graph | Paired reuse delta | Fewer stdout bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| jetsnack | 36 | 7.652 | 20.057 | 8.648 | +0.851 | 86.6% |
| jetnews | 36 | 10.625 | 28.907 | 12.349 | +1.810 | 81.2% |
| jetchat | 36 | 10.496 | 28.608 | 12.012 | +1.618 | 83.2% |
| jetsnack | 37 | 8.274 | 18.177 | 8.581 | +0.577 | 86.6% |
| jetnews | 37 | 10.251 | 28.754 | 11.675 | +1.551 | 81.2% |
| jetchat | 37 | 10.533 | 27.487 | 11.981 | +1.645 | 83.2% |

Reuse emits 804 bytes for Jetsnack, 858 for JetNews, and 844 for Jetchat, versus raw medians of 6,017, 4,553, and 5,017–5,018 bytes. It uses one top-level command instead of three or five. The learned graphs contain three JSON files each, totaling 3,442–4,493 bytes.

Initial learning takes 18.18–28.91 seconds in these scripted tasks. There is **no demonstrated time break-even**, because reuse remains slower per navigation. The byte-only break-even is the first use in all six comparisons, since even scripted learning returns fewer bytes than raw layouts. That calculation excludes model prompts, reasoning, source exploration, and maintenance frequency, so it is not a token-cost payback claim.

Matched differences and descriptive bootstrap intervals are retained in the JSON report. Both Jetsnack intervals span zero. With five pairs per task/device, these are descriptive results, not a p95 estimate or a release reliability guarantee. Both emulators shared one host.

## Readiness failures retained

The first activity-only launch attempts failed the Home oracle before assigning any measured trials: the installed Android CLI returned success but did not launch the requested app. Switching resets to the pinned APK exposed intermittent empty/null-root startup captures. That original v1.1 run recorded **52 successes, 8 setup failures, and 30 missing assignments** out of 90 planned trials; its API 37 worker stopped between apps. All 52 trials with verified starts succeeded.

The separately registered v1.2 follow-up waits up to 30 seconds for Home during setup, records every readiness capture, removes the unnecessary between-app reset, and retains assignments if a seed cannot be prepared. It never retries measured navigation or a failed final oracle. It completed all 90 trials with no setup failures. Readiness took two to four captures, with per-device median waits of 8.23 and 8.42 seconds and a maximum of 15.64 seconds. Total reset/setup and independent-oracle costs remain available per trial.

This repairs the benchmark startup handling; it does not establish the underlying cause of the Android CLI null-root responses.

## Recovery, negative controls, and teams

| Check | API 36 | API 37 |
| --- | --- | --- |
| Renamed tab: stale rejection, repair, teammate reuse, older-build replay | pass | pass |
| Relocated route: stale rejection, repair, teammate reuse, older-build replay | pass | pass |
| Wrong Search → Cart callback: two rejections, unchanged graph | pass | pass |
| Wrong-person expectations: three rejections, unchanged graphs | pass | pass |
| External navigation: re-observe and return Home correctly | pass | pass |
| Rename/relearn: stable place and edge IDs, teammate replay | pass | pass |

The frozen stale graph is the recovery control: it must reject the changed path without modifying memory. The scripted source-informed workflow then learns and verifies the replacement. A teammate receives only the graph, and the older build must still work. The injected wrong callback must reproduce twice without learning Cart as Search. These are known-change contract tests; they do not establish independent agent diagnosis or alert quality.

| Source-informed repair | API | Measured tool seconds | Whole host scenario seconds | Host action debits |
| --- | ---: | ---: | ---: | ---: |
| renamed_control | 36 | 73.17 | 117.03 | 7/32 |
| relocated_route | 36 | 81.27 | 121.47 | 10/32 |
| renamed_control | 37 | 73.29 | 116.78 | 7/32 |
| relocated_route | 37 | 79.87 | 122.39 | 10/32 |

The whole host scenario includes repair, independent checks, teammate replay, and older-build deployment/validation; it is not the latency of one repair alone. Each scenario has a 180-second shared host budget.

All 60 primary learned/reused graphs pass `doctor --repo-only`; it changes none of them. All 30 reused graphs remain identical to their seed, and the 30 raw controls create no graph. In a separate real Git fixture, disjoint additions from the two successful repairs survive a merge. Conflicting edits to one place produce a Git conflict that `doctor` rejects without rewriting the graph.

## Gates and remaining measurements

- complete_assignment: pass
- zero_false_successes: pass
- at_least_95_percent_each_arm: pass
- reuse_correctness_not_lower_than_raw: pass
- at_least_50_percent_fewer_response_bytes_each_task: pass
- agent_token_reduction: unmeasured
- primary_graph_integrity: pass
- recovery_and_negative_controls_both_apis: pass
- disjoint_git_additions: pass
- explicit_git_conflicts: pass
- independent_agent_diagnosis: unmeasured

**Actual model-token usage remains unmeasured.** The 24 isolated agent fixtures are prepared: 18 navigation comparisons and six blinded change-diagnosis trials, each capped at 180 seconds and 32 inputs. No additional agent has been started because this task requires explicit authorization for additional agents; that approval remains pending. Usage fields are `null`, not estimates converted from response bytes.

Eight evaluator regression tests pass locally; CI now runs them on Linux and macOS. They cover visible/unique oracles, bounded startup readiness, valid goals after a tab rename, failure denominators, incompatible/duplicate trial rejection, break-even arithmetic, missing model usage, and the input cap. Python parsing and `git diff --check` also pass. No runtime code was changed during this experiment.

The remaining evaluation gaps are actual agent token/diagnosis/alert measurements, longer and unseen routes, graph growth and successive changes, additional locales/font scales/form factors, and broader reliability sampling. These results do not satisfy every release gate in the [hardening plan](../../docs/MINIMAP_HARDENING_PLAN.md).

## Reproduction and provenance

Use the [runner instructions](../README.md). The tested build is from the uncommitted development workspace based on `e838065b7b9f837f11233e452afc47e8bbdeefb0`, with binary SHA-256 `5875a78c9a691935f1546b6c5ee32ac3ec7a289af0f50578dd68347872682f03`. Compose samples are pinned at `d3ff757b289f7036815978a8f7b16706ee3423b0`. Nothing was published as part of this evaluation.

The [JSON report](2026-09-16-controlled.json) contains per-trial outcomes, paired differences, confidence intervals, setup costs, APK hashes, frozen manifests, graph checks, and retained failures. Raw layouts, command output, copied graphs, and prepared agent fixtures remain at `/private/tmp/minimap-controlled-20260916`; the pinned fixture APKs and source manifest remain at `/private/tmp/minimap-self-healing-20260916/reproducible-fixtures`.
