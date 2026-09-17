use super::{
    clear_pending, clear_session_place, endpoint_for_place, execute_recipe, fingerprint_summary,
    load_session_place, observe_layout, orient_layout, remember_place_observation,
    result_with_data, save_session_place,
};
use anyhow::{Context, Result};
use minimap_android::{Adb, AndroidCli, CommandRunner};
use minimap_core::{detect_overlay, fingerprint_layout, match_place, normalize_label};
use minimap_graph::resolve_path_excluding;
use minimap_repo::{commit_edge, commit_place, load_graph, Graph};
use minimap_schemas::Place;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub(super) struct Options<'a> {
    pub expectations: &'a [String],
    pub supersede: Option<&'a str>,
    pub max_actions: u32,
    pub timeout: Duration,
    pub excluded: BTreeSet<String>,
}

struct Recovery<'a> {
    target: &'a str,
    options: Options<'a>,
    start: Instant,
    actions: u32,
    excluded: BTreeSet<String>,
    failures: Vec<Value>,
    executed: Vec<Value>,
    changed: Vec<PathBuf>,
    reoriented: bool,
}

impl Recovery<'_> {
    fn remaining_seconds(&self) -> u64 {
        self.options
            .timeout
            .saturating_sub(self.start.elapsed())
            .as_secs()
    }

    fn result(&self, status: &str, summary: &str, layout: &Value, place: Option<&Place>) -> Value {
        let success = status == "ok";
        let outcome = if !success {
            "needs_agent"
        } else if self.reoriented || !self.failures.is_empty() {
            "recovered"
        } else {
            "unchanged"
        };
        let mut recovery = json!({
            "outcome": outcome,
            "target": normalize_label(self.target),
            "failures": self.failures,
            "excluded_edges": self.excluded,
            "remaining_actions": self.options.max_actions.saturating_sub(self.actions),
            "remaining_seconds": self.remaining_seconds(),
        });
        if !success {
            recovery["observation"] = fingerprint_summary(&fingerprint_layout(layout));
            recovery["next_action"] = json!("Inspect fresh UI and relevant source; use Minimap actions to verify a route to the original target, then replay it before superseding an obsolete edge. Routine recovery does not need user intervention.");
        }
        result_with_data(
            status,
            summary,
            json!({
                "target": normalize_label(self.target),
                "current": place.map(|p| &p.slug),
                "planned_path": self.executed.iter().filter_map(|step| step.get("edge")).collect::<Vec<_>>(),
                "executed_steps": self.executed,
                "start_source": "layout",
                "changed_graph": !self.changed.is_empty(),
                "changed_files": super::changed_files_json(&self.changed),
                "recovery": recovery,
                "metrics": {"elapsed_ms": self.start.elapsed().as_millis(), "action_budget_used": self.actions},
                "verification": {"fresh": true, "expectations": self.options.expectations.len(), "passed": success},
            }),
        )
    }
}

pub(super) fn go_result<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    target: &str,
    options: Options<'_>,
) -> Result<Value> {
    let result = navigate(root, android, adb, target, options);
    if !result.as_ref().is_ok_and(|result| result["status"] == "ok") {
        // No later command may trust the starting point of a failed traversal.
        let _ = clear_session_place(root, adb);
        let _ = clear_pending(root, adb);
    }
    result
}

fn navigate<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    target: &str,
    options: Options<'_>,
) -> Result<Value> {
    let deadline = Instant::now() + options.timeout;
    android.set_deadline(deadline);
    adb.set_deadline(deadline);
    let cached = load_session_place(root, adb)?;
    clear_pending(root, adb)?;
    clear_session_place(root, adb)?;
    let mut recovery = Recovery {
        target,
        excluded: options.excluded.clone(),
        options,
        start: Instant::now(),
        actions: 0,
        failures: Vec::new(),
        executed: Vec::new(),
        changed: Vec::new(),
        reoriented: false,
    };
    // Always observe, including a zero-edge plan; TTL cannot prove ownership.
    let mut layout = observe_layout(android, false)?;
    adb.ensure_foreground()?;
    let orientation = orient_layout(root, &layout, None, false, false, adb)?;
    let mut current = orientation.matched_place;
    let mut graph = load_graph(root)?;
    recovery.reoriented = cached.is_some_and(|cached| {
        current
            .as_ref()
            .is_none_or(|place| place.id != cached.place.id)
    });
    if let Some(id) = recovery.options.supersede {
        let edge = graph
            .edges
            .get(id)
            .context("superseded edge does not exist")?;
        anyhow::ensure!(
            current
                .as_ref()
                .is_some_and(|place| place.id == edge.from.id),
            "replacement verification must start at the superseded edge's source"
        );
        let destination = graph
            .places
            .get(&edge.to.id)
            .context("superseded destination is missing")?;
        anyhow::ensure!(
            destination.slug == normalize_label(target),
            "replacement must preserve the superseded edge's destination"
        );
        recovery.excluded.insert(id.to_string());
    }
    let viewport = adb.display_size().ok();
    loop {
        if let Some(reason) = detect_overlay(&layout) {
            return Ok(recovery.result("blocked_by_overlay", &reason, &layout, None));
        }
        let Some(place) = current.as_ref() else {
            return Ok(recovery.result(
                "unknown",
                "fresh observation needs agent orientation",
                &layout,
                None,
            ));
        };
        if place.slug == normalize_label(target) {
            if minimap_android::verify_expectations(&layout, recovery.options.expectations).is_err()
            {
                return Ok(recovery.result(
                    "goal_mismatch",
                    "target screen is present but requested instance/state was not verified",
                    &layout,
                    Some(place),
                ));
            }
            if let Some(id) = recovery.options.supersede {
                anyhow::ensure!(
                    !recovery.executed.is_empty(),
                    "replacement must replay a verified route"
                );
                for step in &recovery.executed {
                    let edge_id = step["edge"].as_str().unwrap();
                    let mut replacement = graph.edges[edge_id].clone();
                    if !replacement.superseded_by.is_empty() {
                        replacement.superseded_by.clear();
                        recovery.changed.push(commit_edge(root, &replacement)?);
                    }
                }
                let mut old = graph
                    .edges
                    .get(id)
                    .context("superseded edge disappeared")?
                    .clone();
                old.superseded_by = recovery
                    .executed
                    .iter()
                    .filter_map(|step| step["edge"].as_str().map(str::to_string))
                    .collect();
                recovery.changed.push(commit_edge(root, &old)?);
            }
            save_session_place(
                root,
                adb,
                &endpoint_for_place(place),
                &fingerprint_layout(&layout),
                &layout,
            )?;
            return Ok(recovery.result("ok", "navigation completed", &layout, Some(place)));
        }
        if recovery.start.elapsed() >= recovery.options.timeout || recovery.failures.len() >= 3 {
            return Ok(recovery.result(
                "action_failed",
                "bounded recovery needs agent diagnosis",
                &layout,
                Some(place),
            ));
        }
        let plan = resolve_path_excluding(&graph, target, &place.id, viewport, &recovery.excluded);
        if plan.status != "ok" {
            let status = if recovery.failures.is_empty() {
                plan.status.as_str()
            } else {
                "action_failed"
            };
            return Ok(recovery.result(
                status,
                "no remaining compatible verified route",
                &layout,
                Some(place),
            ));
        }
        let edge = plan
            .edges
            .first()
            .context("planner returned an empty nonterminal route")?;
        let needed = u32::try_from(edge.recipe.len()).unwrap_or(u32::MAX);
        if needed
            > recovery
                .options
                .max_actions
                .saturating_sub(recovery.actions)
        {
            return Ok(recovery.result(
                "action_failed",
                "navigation action budget exhausted",
                &layout,
                Some(place),
            ));
        }
        recovery.actions += needed;
        let execution = execute_recipe(android, adb, &edge.recipe, Some(&layout), deadline);
        layout = match super::observation::observe_destination(
            android,
            |layout| {
                observed_place(&graph, layout).is_some_and(|place| place.id == edge.to.id)
                    && (edge.to.slug != normalize_label(target)
                        || minimap_android::verify_expectations(
                            layout,
                            recovery.options.expectations,
                        )
                        .is_ok())
            },
            false,
        ) {
            Ok(layout) => layout,
            Err(error) => {
                return Ok(recovery.result("environment_error", &error.to_string(), &layout, None))
            }
        };
        if let Err(error) = adb.ensure_foreground() {
            return Ok(recovery.result("environment_error", &error.to_string(), &layout, None));
        }
        current = observed_place(&graph, &layout);
        if execution.is_ok() && current.as_ref().is_some_and(|place| place.id == edge.to.id) {
            let place = current.as_mut().unwrap();
            if (place.slug != normalize_label(target)
                || minimap_android::verify_expectations(&layout, recovery.options.expectations)
                    .is_ok())
                && remember_place_observation(place, &fingerprint_layout(&layout))
            {
                recovery.changed.push(commit_place(root, place)?);
                graph.places.insert(place.id.clone(), place.clone());
            }
            recovery
                .executed
                .push(json!({"edge": edge.id, "to": place.slug, "status": "ok"}));
            save_session_place(
                root,
                adb,
                &endpoint_for_place(place),
                &fingerprint_layout(&layout),
                &layout,
            )?;
        } else {
            recovery.excluded.insert(edge.id.clone());
            recovery.failures.push(json!({
                "edge": edge.id,
                "expected": edge.to.slug,
                "observed": current.as_ref().map(|place| &place.slug),
                "reason": execution.err().map(|error| error.to_string()).unwrap_or_else(|| "unexpected_destination".to_string()),
            }));
        }
    }
}

fn observed_place(graph: &Graph, layout: &Value) -> Option<Place> {
    if detect_overlay(layout).is_some() {
        return None;
    }
    let found = match_place(&fingerprint_layout(layout), graph.places.values().cloned());
    if found.status == "unknown" || found.status == "ambiguous" {
        return None;
    }
    found.place_id.and_then(|id| graph.places.get(&id).cloned())
}
