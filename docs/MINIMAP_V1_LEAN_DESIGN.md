# Minimap Lean V1 Design

This document describes the lean command surface and persisted model. Its evolution follows the
[self-healing contract](MINIMAP_HARDENING_PLAN.md#self-healing-contract): routine
navigation recovery and verified map updates happen within the agent workflow,
with source diagnosis performed by the host agent. The
[follow-up report](../evals/results/2026-09-16-ethos.md) distinguishes implemented
behavior from outstanding validation and release gates.

Minimap v1 is a narrow Android navigation-memory tool for AI agents. It helps an
agent remember proven ways through a running app so later agents can navigate and
verify faster without rediscovering the same Android layout state every time.

The persisted product is the graph. Minimap should not become a crawler, test
assertion framework, source-code analyzer, telemetry system, or graph admin UI.

## Product Boundary

Persist only verified navigation memory:

- Semantic places in the app.
- Verified transitions between places.
- Ordered action recipes that produced those transitions.
- Compact matching fingerprints for places.
- Variants only for simultaneously valid states.

Do not persist:

- Raw Android layout JSON.
- Unexplored exits or crawler inventories.
- Business assertions.
- Reliability counters, timestamps, or usage telemetry.
- Proposals, accept flows, always-on journals, or undo state.
- Source-code analysis output.

Git diff and PR review are the review surface for graph changes.

## Command Surface

V1 exposes only these top-level commands:

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

`init` creates `.minimap/` and installs agent skills by default. It supports
`--agents all`, `--refresh-skills`, and `--no-skills`. Skill text installed by
`init` and the Claude plugin must come from the same source/template.

`doctor` is read-only. It checks repo health and device readiness separately:
config exists and parses, graph JSON is valid, labels are unique, edges do not
dangle, `android` is available, `adb` is available, and a device is reachable.

`whereami` calls `android layout` once, matches the current layout to the graph,
and returns compact orientation only. When Minimap has a very fresh verified
session and no `--label` was provided, it may return that session place without
another layout call. When a known place is observed with a new high-confidence
fingerprint, Minimap preserves the original baseline and records the new
fingerprint as a variant. It returns fingerprint summaries only for non-plain
known states such as unknown, changed, or label mismatch. It does not return the
full layout.

`whereami --label <label>` attaches or changes semantic identity:

- If the current place is unknown and the label is unused, create the place.
- If the current place is known and the label is unused, change its label while
  preserving its immutable ID and edge references.
- If the label already belongs to a different known place, return
  `label_mismatch`.
- Trust the explicit agent label unless there is a mechanical conflict.

`go <target>` always starts from a fresh layout, including a zero-edge plan.
It resolves the target by exact normalized label, uses deterministic weighted
search (action count, verification cost, geometry penalty), and verifies each
semantic transition. Failures exclude the affected edge and replan from the
actual observed place. Unknown/ambiguous observations hand off to the agent.
Defaults are 32 actions, 60 seconds, and three failed edges per replay command;
subprocesses also have a 30-second ceiling. A private recovery token carries the
original goal, checks, failed edges, deadline, and remaining inputs across host
handoffs. Every command for that unresolved goal must pass `--recovery <token>`;
larger retry limits cannot enlarge its budget. Only the host agent explores
unfamiliar actions. Repeatable `--expect <selector>` anchors verify visible
instance/state without persisting those assertions in the shared graph.

The host may confirm a source-verified appearance change with
`whereami --confirm-place <id>`; this preserves the baseline and adds a bounded
variant. `go <target> --supersede <edge-id>` excludes the obsolete edge, requires
its original source and destination, and prefers the replacement only after
successful replay and goal checks. The original edge remains a fallback for
other builds. New edges use `minimap.edge.v2` with optional `superseded_by` route
IDs; older v1 edges load without automatic file rewrites. Older clients reject
v2 explicitly. Fallback paths have higher planning cost than ordinary paths.

`tap` observes before the action and requires consecutive identical usable
post-action frames. The global `--stable-frames N` option accepts `1` through
`5`, defaults to `2`, and uses `1` as the explicit no-wait mode. The capture is
bounded to `N + 3` observations and does not accept repeated pre-action frames
early, so a delayed transition is not mistaken for a stable destination.
Results record foreground package/activity, pre/post identity hashes, the
post-action hash sequence, and the observed stability count. Empty/null-root
full captures have bounded retries; partial diffs cannot prove a destination.
It supports:

- `--selector <kind=value>`
- `--point <x,y>`
- `--screenshot-label <n> --screenshot <path>`

Destination identity is `--label <place>`. `--reason` records action intent.
`tap --label` names the post-tap destination. A new destination place is written
only when `--label` is present. Without a label, unknown destinations do not
enter the committed graph; Minimap may keep short-lived temp state outside the
repo so a follow-up `whereami --label` can complete the transition.

`scroll` calls layout before and after. Same-place scrolls are not graph edges.
Scroll actions can accumulate in temp state as part of the next transition
recipe.

`back` calls layout before and after. If Back moves from one known place to a
different known place, record a `press_back` edge. Back never creates a new
place. The planner currently excludes Back recipes because it cannot prove a
portable back-stack precondition; agents use explicit UI return routes.

`layout` wraps `android layout` and returns redacted Android layout plus
read-only Minimap orientation metadata. When it follows a fresh verified
observation such as `go`, `whereami`, `tap`, or `back`, it may return the
cached redacted session layout instead of calling `android layout` again. It is
the raw escape hatch for agents. `--fresh` bypasses cached observations on both
`layout` and `whereami`; use it for current product assertions.

## Repository State

`.minimap` contains only committed config and graph files:

```text
.minimap/
  config.json
  graph/
    places/
    edges/
```

No `.minimap/proposals`, `.minimap/journal.jsonl`, `.minimap/runs`,
`.minimap/state`, or `.minimap/checks`.

Session/temp state lives outside the repo, scoped by canonical repo, resolved
ADB serial, package, process ID, installed package metadata, and short TTLs.
Caches use atomic writes; corrupt caches are discarded. A cached observation
can serve an immediate diagnostic read, but never authorizes a `go` start.

Pending transitions append scrolls and same-place taps (up to 32 actions).
Failure, Back, restart/build change, stale evidence, or expiration abandons the
pending route. Locks serialize the whole command for each device and repo;
OS locks release on exit, including a killed process, and use a stable host temp
location across per-task TMPDIR values. Graph files use unique atomic staging.

## Config

`config.json` is required and intentionally small:

```json
{
  "schema_version": "minimap.config.v2",
  "active_app_profile": "default",
  "app_profiles": {
    "default": {
      "android_package": "com.example.myapp"
    }
  }
}
```

V1 uses one active app profile. The profile fields reserve a mechanical migration
path for multiple app maps later without adding complexity now.

## Graph Schema

Places use product language instead of screen language.

```json
{
  "schema_version": "minimap.place.v1",
  "id": "place_settings",
  "slug": "settings",
  "label": "Settings",
  "baseline": {
    "identity_hash": "sha256:...",
    "fingerprint": {
      "selectors": [
        {"kind": "test_tag", "value": "settings_title"},
        {"kind": "resource_id", "value": "com.example:id/settings_list"}
      ],
      "static_text": [
        {"value": "Settings"}
      ],
      "roles": {"Button": 5, "Text": 12}
    }
  },
  "variants": []
}
```

Labels normalize to globally unique lowercase kebab-case slugs. There are no
aliases in v1. Safe static UI copy may be stored when useful. Dynamic text,
emails, tokens, long user content, numeric sensitive values, and input values are
excluded or redacted before hashing or persistence.

Initial readable place IDs are slug-derived, for example `place_settings`,
with a fingerprint suffix when that ID was previously used. IDs are immutable;
relabeling changes one place file, and the graph loader resolves endpoint labels
from their IDs. Edge IDs include a full hash of the complete ordered recipe and
endpoint IDs, so distinct recipes cannot overwrite each other.

Edges are verified semantic transitions with ordered recipes:

```json
{
  "schema_version": "minimap.edge.v2",
  "id": "edge_home__settings__tap_test_tag_settings_button",
  "from": {"id": "place_home", "slug": "home"},
  "to": {"id": "place_settings", "slug": "settings"},
  "intent": "open settings",
  "recipe": [
    {
      "kind": "tap",
      "selector": {"kind": "test_tag", "value": "settings_button"}
    }
  ]
}
```

Coordinate and screenshot-label actions are geometry actions. They require exact
viewport guards:

```json
{
  "kind": "tap",
  "point": {"x": 540, "y": 1200},
  "viewport": {"width": 1080, "height": 2400}
}
```

Do not store ratios. If the viewport differs, the edge is incompatible.

Edge IDs are deterministic and agent-readable where practical: source slug,
destination slug, and primary action fingerprint, with a hash fallback for long
or colliding IDs. Equivalent edges are idempotent. Different recipes between the
same places are separate edges. Same-place taps do not create navigation edges.

## Matching And Learning Rules

The graph records only actions Minimap executed and verified.

- Action lands on known destination: write or dedupe the edge.
- Action lands on unknown destination with `--label`: create labelled place and
  write the edge.
- Action lands on unknown destination without `--label`: no graph write; return
  `needs_label`.
- Action lands on a known place different from the requested label: no graph
  write; return `label_mismatch`.
- Geometry action without viewport capture: action may execute, but no edge is
  committed.

Normal UI evolution adds a place variant when the new fingerprint still matches
the same semantic place. The original baseline is not overwritten during normal
navigation, which keeps diffs reviewable and avoids erasing the first proven
identity. Git history handles old app versions.

`whereami` may self-heal a known place baseline on high-confidence match. The
threshold is not configurable in v1.

Place baseline updates do not rewrite edge recipes. A stale edge is repaired
by learning and replaying a replacement to the same destination before
supersession. Automatic variants are capped at 16 and must still match the
original baseline, preventing unbounded growth and chains of fuzzy drift.

One app per graph is currently enforced, with foreground checks before inputs
and verification. Screen-type recognition does not prove item-specific state;
the host must verify that separately. Multi-profile graph namespaces and
parameterized destination contracts remain in the hardening roadmap.

Blocking overlays are reported, not managed. If an overlay prevents destination
verification, return `blocked_by_overlay` and do not record the edge. Agents
decide how to handle permissions, sign-in prompts, dialogs, keyboard state, and
other transient UI.

## Result Contract

All command output is JSON by default and includes `schema_version`.

Use a small shared status vocabulary:

```text
ok
known
known_changed
unknown
needs_label
label_mismatch
blocked_by_overlay
no_known_path
no_compatible_path
action_failed
environment_error
config_error
```

Graph changes are successful command execution with `changed_graph: true` and
`changed_files`. Nonzero exit codes are reserved for failures and blockers such
as `needs_label`, `blocked_by_overlay`, `label_mismatch`, incompatible paths,
environment errors, and config errors.

## Validation Strategy

Validation has three layers.

1. Rust unit tests for redaction, fingerprinting, label normalization, graph
   loading, graph consistency, matching thresholds, recipe IDs, viewport guards,
   and path ranking.
2. CLI contract tests with fake `android` and `adb` executables. These cover the
   stable JSON contract and file writes without requiring a device.
3. Live Android smoke tests against real Compose sample apps in
   `/Users/mmckenna/Dev/compose-samples`.

Live smoke is intentionally outside CI by default. It verifies that Minimap
works against real Compose semantics and Android CLI output while still assuming
the agent owns build/install/launch.

Recommended sample targets:

- `Jetsnack` (`com.example.jetsnack`): bottom navigation and detail navigation.
  Existing sample tests cover `HOME`, `SEARCH`, `MY CART`, `PROFILE`, and the
  `Chips` detail page. Use this to validate `whereami --label`, selector/text
  taps, known path replay, and edge dedupe.
- `JetNews` (`com.example.jetnews`): drawer navigation and scroll-to-post.
  Existing sample tests open the navigation drawer, go to `Interests`, and
  scroll/click a post. Use this to validate multi-action recipes
  (`scroll` + `tap`) and Back/up behavior.
- `Jetchat` (`com.example.compose.jetchat`): drawer profile navigation and Back.
  Existing sample tests open the drawer, navigate to a profile, and press Back.
  Use this to validate content-description selectors and `back` edge recording.

Manual smoke outline for a sample:

```bash
cd /Users/mmckenna/Dev/compose-samples/Jetsnack
./gradlew :app:installDebug
adb shell monkey -p com.example.jetsnack 1

minimap init --force --agents codex
minimap doctor
minimap whereami --label home
minimap tap --selector "content_desc=SEARCH" --label search --reason "open search"
minimap go search
```

Expected smoke behavior:

- `.minimap/graph/places` and `.minimap/graph/edges` contain only compact graph
  JSON.
- No raw layout, journal, proposal, run, or state files are created under
  `.minimap`.
- Re-running the same learned navigation is idempotent.
- `go` uses `whereami`, selects known UI paths, verifies each transition, and
  reports `changed_graph` only when graph files changed.
- Unknown destinations without `--label` return `needs_label` and do not write
  graph files.

## Implementation Direction

This is a breaking pre-1.0 refactor. Plain `init` should refuse incompatible old
`.minimap` layouts with a clear message. `init --force` should replace old
Minimap state with the minimal v1 layout.

Remove or hide old command concepts from the CLI and skills:

- `accept`
- `proposals`
- `journal`
- `undo`
- `route`
- `screen`
- `observe`
- `learn`
- `map`
- `repair`
- heavyweight `validate`

Rewrite README, skills, changelog, and tests around the lean command surface.
