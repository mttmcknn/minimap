# Minimap Implementation Plan

The active implementation target is the lean v1 design in
[MINIMAP_V1_LEAN_DESIGN.md](MINIMAP_V1_LEAN_DESIGN.md).

The next development roadmap, ordered fixes, and evaluation gates are in
[MINIMAP_HARDENING_PLAN.md](MINIMAP_HARDENING_PLAN.md), grounded in the
[2026-09-16 live baseline](../evals/results/2026-09-16-jetsnack.md).

The implemented recovery loop supports quiet self-healing: Minimap recovers known navigation
failures, the host agent diagnoses unfamiliar changes using UI and source
evidence, and verified repairs improve the shared graph. Routine recovery
does not interrupt the user; corroborated critical product issues do. See
the roadmap's [self-healing contract](MINIMAP_HARDENING_PLAN.md#self-healing-contract)
for the classification, persistence, notification, and evaluation rules.
Measured results and remaining gates are recorded in the
[self-healing evaluation](../evals/results/2026-09-16-self-healing.md).

Minimap is now implemented as a breaking pre-1.0 Rust refactor around one narrow
goal: Android navigation memory for agents. The committed graph stores semantic
places and verified transition recipes only.

## Active Command Surface

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

Removed top-level commands and heavy workflows remain out of scope; this does
not prohibit automatic recovery inside navigation or the host agent's loop:

- observe/learn/map
- route/screen admin commands
- proposals/accept
- repair
- undo
- heavyweight validate
- always-on journal

## Implementation Priorities

1. Keep `.minimap/` minimal: config plus graph.
2. Keep JSON output stable and agent-first.
3. Persist only verified navigation facts.
4. Reject old schemas/layouts instead of carrying compatibility code.
5. Validate with Rust tests, fake Android/ADB CLI contract tests, and manual
   live smoke against `/Users/mmckenna/Dev/compose-samples`.
