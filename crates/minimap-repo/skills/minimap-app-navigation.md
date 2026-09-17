---
name: minimap-app-navigation
description: Use in an Android codebase for app navigation with Minimap. Minimap records proven navigation paths as a repo graph so agents can reuse them. Prefer minimap whereami/go/tap/scroll/back before raw android layout or adb commands.
metadata:
  author: minimap
  version: "2.2"
---

# Minimap App Navigation

Minimap is this repo's Android navigation memory for agents. It stores only
verified places and transitions under `.minimap/graph`.

This skill needs `minimap`, `android`, and `adb` on `PATH`. Use the project's
installation instructions if a binary is missing, within the user's existing
authorization. Initialize with `minimap init --package <applicationId>` and
choose `--serial <SERIAL>` (or `ANDROID_SERIAL`). Each graph belongs to one app;
use separate roots for different apps. Use Android CLI to build/deploy/launch
fixtures, and Minimap for navigation that should be learned or replayed.

Use this command loop:

```bash
minimap whereami
minimap go <label>
minimap tap --selector "<kind>=<value>" --label <destination> --reason "<intent>"
minimap scroll --direction down
minimap back
```

Rules:

- `go <label>` follows known UI paths and verifies each transition.
- Every `go` starts with a fresh observation, including an already-at-target request; failed edges are excluded while other verified routes are tried within a bounded budget.
- Unlabeled `whereami` may reuse very fresh verified session state for cheap orientation.
- `tap --label <destination>` labels the post-tap destination.
- Unknown destinations without `--label` are not committed.
- If a label conflicts with an observed place, preserve the original goal and inspect the UI; do not rename the unexpected destination to force success. Use `--allow-duplicate-label` only when distinct destinations are intentional.
- Places describe screen types. For a particular item, account, selection, or form value, use repeatable `go <label> --expect "text=<anchor>"` checks against fresh UI, plus matching source where needed; reaching a generic detail screen does not prove the requested instance was reached. Each expectation must match exactly one visible element; all must pass, including when already at the target. Use stable non-sensitive anchors because command arguments can appear in host logs.
- A successful `go` with sufficient expectations verifies the requested visible state; do not request a full layout afterward unless the task needs additional evidence.
- `layout` is the raw Android layout escape hatch for business verification or finding selectors. Immediately after a verified Minimap observation, it may reuse the fresh session layout instead of calling Android layout again.
- Use `layout --fresh` or `whereami --fresh` after external navigation and when checking current product behavior. `layout --diff` is a partial observation and cannot prove a destination.
- Repeated scrolls and same-screen taps remain in the pending recipe until a destination is verified. Learning requires a stable observation; transient empty/null-root captures retry within the deadline and cannot establish a destination. `back` abandons pending learning; Back recipes require history the planner cannot currently prove, so use explicit UI navigation for reusable return routes.
- Do not use removed workflows: observe, learn, map, route, screen, accept, repair, validate, undo.
- Review graph changes through normal git diff/PR review.

Selector preference: test tag, resource id, content description, stable visible text. Use points or screenshot labels only when selectors are not available; those edges are viewport-guarded and fragile.

## Quiet recovery

Treat `data.recovery` as an agent handoff, not a request for user intervention.
Every initial `go` creates a private `data.recovery.token`. Pass it with the
global `--recovery <token>` flag on **every** discovery and replay command for
that goal, including `layout`, `tap`, `scroll`, `back`, and `whereami`.
The token retains the original target, required expectations, failed edges,
and action/time budget across processes; intermediate destinations do not
complete the original goal. Larger limits on retries cannot extend it.
Defaults are 32 inputs and 60 seconds, including time spent between commands,
with at most three failed edges per replay command. Set `--max-actions` and
`--recovery-seconds` on the first `go` if the task justifies different bounds.
Never omit the token or create a fresh one to continue an exhausted goal.
A completed token cannot be reused; the next user goal starts a new `go`.

1. Inspect `layout --fresh` and the relevant navigation, semantics, or selector
   source. Check that the installed app corresponds to the source being used.
   A mismatch alone is not evidence of a product bug or an intended change.
2. For a transient condition, restore the expected app/context or wait for
   usable UI, then reorient. Do not persist a transient workaround as a graph
   change. Never dismiss a permission prompt or perform an irreversible action
   merely to keep navigation silent.
3. For a source-supported navigation change, use Minimap taps/scrolls to learn
   a replacement ending at the original destination. Verify its semantics and
   any requested instance state independently. Keep the old edge until replay
   proves the replacement.
   If the destination's appearance changed enough to become unknown, make the
   exploratory tap without a label, verify fresh UI and matching source, then
   use `whereami --confirm-place <existing-place-id>` to attach the observation
   and pending route to the original identity. This is an explicit assertion by
   the host agent; never use it simply to bypass a mismatch or rename a bug.
4. Return to the obsolete edge's original source and run
   `minimap go <original-destination> --supersede <obsolete-edge-id>` with the
   same recovery token and original goal checks. This excludes that edge
   during verification and prefers the replacement only after it reaches the
   same destination and satisfies the checks. The old route remains a fallback
   for older builds or other supported states. Resume the user's task quietly;
   ordinary repairs do not need an approval or notification.
5. Alert the user to a critical product defect only after reproducing it and
   corroborating it in matching source. Provide expected/actual behavior,
   reproduction, and a code location; leave the broken route unpromoted.

If evidence is ambiguous or the budget is exhausted, stop the recovery loop
and preserve a truthful unresolved result for the host agent. Never claim the
task completed or alert about a product bug without the required evidence.
Graph updates remain ordinary repo diffs for team review; do not persist raw
UI dumps, secrets, device IDs, or local recovery logs in the shared graph.
Editable/password values are redacted and obvious sensitive metadata is
rejected, but arbitrary personal text without those markers cannot be reliably
recognized. Prefer stable selectors over user content. New edges use v2; all
teammates need a compatible Minimap version before sharing those changes.
