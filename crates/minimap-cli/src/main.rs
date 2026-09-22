mod budget;
mod navigation;
mod observation;
use observation::{observe_after_action, observe_layout};

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use minimap_android::{
    parse_input_tap, resolve_selector_point, Adb, AndroidCli, CommandRunner, SubprocessRunner,
    TapPoint,
};
use minimap_core::{
    detect_overlay, fingerprint_layout, fingerprint_usable, match_place, normalize_label,
    place_id_for_slug, redact_layout,
};
use minimap_graph::exit_code_for_status;
use minimap_repo::{
    commit_edge, commit_place, load_config, load_graph, run_init, validate_graph, Graph,
    InitOptions,
};
use minimap_schemas::{
    canonical_json, ActionStep, Edge, EdgeEndpoint, MinimapResult, Place, PlaceBaseline, Point,
    Selector, Viewport, EDGE_SCHEMA_VERSION, PLACE_SCHEMA_VERSION, RESULT_SCHEMA_VERSION,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, SystemTime};

const DEFAULT_ACTION_SETTLE_MS: u64 = 1_000;
const SESSION_TTL_SECS: u64 = 600;
const PENDING_TTL_SECS: u64 = 600;
const LAYOUT_CACHE_TTL_SECS: u64 = 30;

#[derive(Debug, Parser)]
#[command(name = "minimap")]
#[command(version)]
#[command(about = "Android navigation memory for AI agents.")]
struct Cli {
    /// Indent JSON for human inspection; compact JSON is the agent default.
    #[arg(long, global = true)]
    pretty: bool,
    #[arg(long)]
    json: bool,
    #[arg(long)]
    quiet: bool,
    #[arg(long = "no-color")]
    no_color: bool,
    /// Android device serial to target when more than one device is attached.
    #[arg(long, global = true, env = "ANDROID_SERIAL")]
    serial: Option<String>,
    /// Continue a goal using the token returned in data.recovery.
    #[arg(long, global = true)]
    recovery: Option<String>,
    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Initialize lean Minimap state and install agent skills.
    Init {
        #[arg(long)]
        dry_run: bool,
        #[arg(long, default_value = "auto")]
        agents: String,
        #[arg(long)]
        force: bool,
        #[arg(long = "refresh-skills")]
        refresh_skills: bool,
        #[arg(long = "no-skills")]
        no_skills: bool,
        /// Bind this graph to one Android application package.
        #[arg(long)]
        package: Option<String>,
    },
    /// Check repo graph health and Android device readiness.
    Doctor {
        /// Validate the shared graph without Android tools or a device (for CI).
        #[arg(long)]
        repo_only: bool,
    },
    /// Identify the current semantic place from one Android layout observation.
    Whereami {
        /// Explicit host confirmation of a changed screen after UI/source verification.
        #[arg(long, conflicts_with = "label")]
        confirm_place: Option<String>,
        /// Bypass the recent observation cache.
        #[arg(long)]
        fresh: bool,
        #[arg(long)]
        label: Option<String>,
        /// If the label slug collides with a different known place, append the
        /// smallest free numeric suffix (e.g. `account-settings-2`) instead of
        /// returning label_mismatch.
        #[arg(long = "allow-duplicate-label")]
        allow_duplicate_label: bool,
    },
    /// Navigate to a known place through verified graph edges.
    Go {
        target: String,
        /// Verify instance/state anchors in the fresh destination; repeat for AND.
        #[arg(long = "expect")]
        expectations: Vec<String>,
        /// Prefer a verified replacement while retaining this edge as a fallback.
        #[arg(long)]
        supersede: Option<String>,
        #[arg(long, default_value_t = 32, value_parser = clap::value_parser!(u32).range(1..=256))]
        max_actions: u32,
        #[arg(long, default_value_t = 60, value_parser = clap::value_parser!(u64).range(1..=300))]
        recovery_seconds: u64,
        /// Use Android CLI observations throughout (compatibility/diagnostics).
        #[arg(long)]
        android_cli_layout: bool,
    },
    /// Tap by selector, coordinate, or screenshot label; --label names the destination.
    Tap {
        #[arg(long)]
        selector: Option<String>,
        #[arg(long)]
        point: Option<String>,
        #[arg(long = "screenshot-label")]
        screenshot_label: Option<i64>,
        #[arg(long)]
        screenshot: Option<String>,
        #[arg(long)]
        label: Option<String>,
        #[arg(long)]
        reason: Option<String>,
        /// If the destination label slug collides with a different known place,
        /// append the smallest free numeric suffix (e.g. `account-settings-2`)
        /// instead of returning label_mismatch.
        #[arg(long = "allow-duplicate-label")]
        allow_duplicate_label: bool,
    },
    /// Scroll and retain the action as part of a pending transition recipe.
    Scroll {
        #[arg(long, default_value = "down")]
        direction: String,
    },
    /// Press Android Back and record a verified known transition if one occurs.
    Back,
    /// Return redacted Android layout plus read-only Minimap orientation metadata.
    Layout {
        #[arg(long)]
        fresh: bool,
        #[arg(long)]
        diff: bool,
    },
}

#[derive(Debug, Clone)]
struct Orientation {
    status: String,
    baseline: PlaceBaseline,
    matched_place: Option<Place>,
    confidence: f64,
    hash_matched: bool,
    changed_files: Vec<PathBuf>,
}

#[derive(Debug, Clone, serde::Deserialize)]
struct PendingTransition {
    source: EdgeEndpoint,
    recipe: Vec<ActionStep>,
    destination: PlaceBaseline,
    intent: Option<String>,
}

#[derive(Debug, Clone, serde::Deserialize)]
struct SessionPlace {
    place: EdgeEndpoint,
    baseline: PlaceBaseline,
    layout: Value,
}

#[derive(Debug, Clone, Copy)]
struct TapRequest<'a> {
    selector: Option<&'a str>,
    point: Option<&'a str>,
    screenshot_label: Option<i64>,
    screenshot: Option<&'a str>,
    label: Option<&'a str>,
    reason: Option<&'a str>,
    allow_duplicate_label: bool,
}

fn main() {
    let cli = Cli::parse();
    let pretty = cli.pretty;
    let code = match run(cli) {
        Ok(code) => code,
        Err(error) => {
            let status = if error.chain().any(|e| e.is::<budget::Exhausted>()) {
                "recovery_exhausted"
            } else if error
                .chain()
                .any(|e| e.is::<minimap_android::DriverError>())
                || error
                    .downcast_ref::<std::io::Error>()
                    .is_some_and(|e| e.kind() == std::io::ErrorKind::WouldBlock)
            {
                "environment_error"
            } else {
                "config_error"
            };
            let mut data = json!({"error": {"message": error.to_string()}});
            if let Some(failure) = error.downcast_ref::<budget::Failure>() {
                data["recovery"] = failure.recovery.clone();
            }
            let result = MinimapResult::new(status, error.to_string(), data);
            print_json(&serde_json::to_value(result).expect("error json"), pretty);
            exit_code_for_status(status)
        }
    };
    std::process::exit(code);
}

fn run(mut cli: Cli) -> Result<i32> {
    if let Commands::Go { expectations, .. } = &mut cli.command {
        for expectation in expectations {
            let (kind, value) = parse_selector(expectation)?;
            *expectation = format!("{kind}={value}");
        }
    }
    let root = if matches!(cli.command, Commands::Init { .. }) {
        PathBuf::from(".")
    } else {
        minimap_repo::find_root(Path::new("."))?
    };
    let deadline = match &cli.command {
        Commands::Go {
            recovery_seconds, ..
        } => Some(std::time::Instant::now() + Duration::from_secs(*recovery_seconds)),
        _ => None,
    };
    let runner = || {
        let mut runner = SubprocessRunner::default();
        if let Some(deadline) = deadline {
            runner.set_deadline(deadline);
        }
        runner
    };
    let lock_timeout = || {
        deadline
            .map(|d| {
                d.saturating_duration_since(std::time::Instant::now())
                    .min(Duration::from_secs(2))
            })
            .unwrap_or(Duration::from_secs(2))
    };
    let serial = cli.serial;
    // Device first, repository second is the global lock order. OS locks are
    // released on error or process death and never enter the committed graph.
    let device_key = if matches!(cli.command, Commands::Init { .. } | Commands::Doctor { .. }) {
        None
    } else {
        Some(Adb::new(runner(), serial.clone()).serial()?)
    };
    let _device_lock = device_key
        .as_deref()
        .map(|key| minimap_repo::OperationLock::device(key, lock_timeout()))
        .transpose()?;
    let _repo_lock = minimap_repo::OperationLock::repository(&root, lock_timeout())?;
    let serial = serial.or(device_key);
    let budget = if let Some(token) = &cli.recovery {
        Some(budget::Recovery::load(
            &root,
            serial
                .as_deref()
                .context("recovery requires a device command")?,
            token,
        )?)
    } else if let Commands::Go {
        target,
        expectations,
        max_actions,
        ..
    } = &cli.command
    {
        Some(budget::Recovery::create(
            &root,
            serial.as_deref().unwrap(),
            target,
            expectations,
            *max_actions,
            deadline.unwrap(),
        )?)
    } else {
        None
    };
    let deadline = budget
        .as_ref()
        .map(|b| {
            let end = b.borrow().deadline();
            deadline.map_or(end, |d| d.min(end))
        })
        .or(deadline);
    let runner = || budget::Runner::new(deadline, budget.clone());
    let print = |result: &Value| {
        print_json(
            &budget::decorate(result.clone(), budget.as_ref()),
            cli.pretty,
        )
    };
    let result = (|| {
        if let Some(budget) = &budget {
            budget.borrow().check_time()?;
        }
        match cli.command {
            Commands::Init {
                dry_run,
                agents,
                force,
                refresh_skills,
                no_skills,
                package,
            } => {
                if let Some(package) = &package {
                    anyhow::ensure!(
                        package.contains('.')
                            && package
                                .chars()
                                .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_'),
                        "invalid Android package"
                    );
                    if root.join(".minimap/config.json").exists() && !force {
                        let config = load_config(&root)?;
                        let current = &config
                            .app_profiles
                            .get(&config.active_app_profile)
                            .context("missing active app profile")?
                            .android_package;
                        anyhow::ensure!(
                            current == package || load_graph(&root)?.places.is_empty(),
                            "cannot rebind a populated graph to another app"
                        );
                    }
                }
                let mut result = run_init(
                    &root,
                    InitOptions {
                        dry_run,
                        agents: &agents,
                        force,
                        refresh_skills,
                        no_skills,
                    },
                )?;
                if let Some(package) = package {
                    if !dry_run {
                        let mut config = load_config(&root)?;
                        config
                            .app_profiles
                            .get_mut(&config.active_app_profile)
                            .context("missing active app profile")?
                            .android_package = package;
                        minimap_repo::write_json(
                            &root.join(".minimap/config.json"),
                            &serde_json::to_value(config)?,
                        )?;
                    }
                    result.changes.push(minimap_repo::InitChange {
                        kind: "app_binding".into(),
                        path: ".minimap/config.json".into(),
                        status: if dry_run { "planned" } else { "bound" }.into(),
                    });
                }
                print(&serde_json::to_value(result)?);
                Ok(0)
            }
            Commands::Doctor { repo_only } => {
                let result = doctor(&root, serial.as_deref(), repo_only);
                let code = exit_code_for_status(result["status"].as_str().unwrap_or("ok"));
                print(&result);
                Ok(code)
            }
            Commands::Whereami {
                confirm_place,
                fresh,
                label,
                allow_duplicate_label,
            } => {
                let mut android = AndroidCli::new(runner(), serial.clone());
                let mut adb = configured_adb(&root, serial, deadline, budget.clone())?;
                if let Some(id) = confirm_place {
                    let result = confirm_place_result(&root, &mut android, &mut adb, &id)?;
                    let code =
                        exit_code_for_status(result["status"].as_str().unwrap_or("config_error"));
                    print(&result);
                    return Ok(code);
                }
                if fresh {
                    clear_session_place(&root, &mut adb)?;
                }
                let result = whereami_result(
                    &root,
                    &mut android,
                    &mut adb,
                    label.as_deref(),
                    allow_duplicate_label,
                    true,
                )?;
                let code = exit_code_for_status(result["status"].as_str().unwrap_or("ok"));
                print(&result);
                Ok(code)
            }
            Commands::Go {
                target,
                mut expectations,
                supersede,
                max_actions,
                recovery_seconds,
                android_cli_layout,
            } => {
                let mut android = AndroidCli::new(runner(), serial.clone());
                if !android_cli_layout {
                    android.prefer_fast_layout();
                }
                let mut adb = configured_adb(&root, serial, deadline, budget.clone())?;
                if let Some(budget) = &budget {
                    expectations.extend(budget.borrow().expectations_for(&target));
                    expectations.sort();
                    expectations.dedup();
                }
                let result = navigation::go_result(
                    &root,
                    &mut android,
                    &mut adb,
                    &target,
                    navigation::Options {
                        expectations: &expectations,
                        supersede: supersede.as_deref(),
                        max_actions: budget.as_ref().map_or(max_actions, |b| {
                            max_actions.min(b.borrow().remaining_actions())
                        }),
                        excluded: budget
                            .as_ref()
                            .map(|b| b.borrow().excluded_edges())
                            .unwrap_or_default(),
                        timeout: deadline
                            .map(|d| d.saturating_duration_since(std::time::Instant::now()))
                            .unwrap_or(Duration::from_secs(recovery_seconds)),
                    },
                )?;
                if let Some(budget) = &budget {
                    budget.borrow_mut().record_go(&target, &result)?;
                }
                let code = exit_code_for_status(result["status"].as_str().unwrap_or("ok"));
                print(&result);
                Ok(code)
            }
            Commands::Tap {
                selector,
                point,
                screenshot_label,
                screenshot,
                label,
                reason,
                allow_duplicate_label,
            } => {
                let mut android = AndroidCli::new(runner(), serial.clone());
                let mut adb = configured_adb(&root, serial, deadline, budget.clone())?;
                let result = tap_result(
                    &root,
                    &mut android,
                    &mut adb,
                    TapRequest {
                        selector: selector.as_deref(),
                        point: point.as_deref(),
                        screenshot_label,
                        screenshot: screenshot.as_deref(),
                        label: label.as_deref(),
                        reason: reason.as_deref(),
                        allow_duplicate_label,
                    },
                )?;
                let code = exit_code_for_status(result["status"].as_str().unwrap_or("ok"));
                print(&result);
                Ok(code)
            }
            Commands::Scroll { direction } => {
                let mut android = AndroidCli::new(runner(), serial.clone());
                let mut adb = configured_adb(&root, serial, deadline, budget.clone())?;
                let result = scroll_result(&root, &mut android, &mut adb, &direction)?;
                let code = exit_code_for_status(result["status"].as_str().unwrap_or("ok"));
                print(&result);
                Ok(code)
            }
            Commands::Back => {
                let mut android = AndroidCli::new(runner(), serial.clone());
                let mut adb = configured_adb(&root, serial, deadline, budget.clone())?;
                let result = back_result(&root, &mut android, &mut adb)?;
                let code = exit_code_for_status(result["status"].as_str().unwrap_or("ok"));
                print(&result);
                Ok(code)
            }
            Commands::Layout { diff, fresh } => {
                let mut android = AndroidCli::new(runner(), serial.clone());
                let mut adb = configured_adb(&root, serial, deadline, budget.clone())?;
                if fresh {
                    clear_session_place(&root, &mut adb)?;
                }
                let result = layout_result(&root, &mut android, &mut adb, diff)?;
                print(&result);
                Ok(0)
            }
        }
    })();
    result.map_err(|cause| match &budget {
        Some(budget) => budget::Failure {
            cause,
            recovery: budget.borrow().context(),
        }
        .into(),
        None => cause,
    })
}

fn configured_adb(
    root: &Path,
    serial: Option<String>,
    deadline: Option<std::time::Instant>,
    budget: Option<budget::Handle>,
) -> Result<Adb<budget::Runner>> {
    let config = load_config(root)?;
    anyhow::ensure!(
        config.app_profiles.len() == 1,
        "one app profile per graph is supported; use separate roots for different apps"
    );
    let package = &config
        .app_profiles
        .get(&config.active_app_profile)
        .context("missing active app profile")?
        .android_package;
    anyhow::ensure!(!package.is_empty(), "bind the app first: minimap init --package <applicationId> (for a legacy graph, set its existing app package in .minimap/config.json)");
    let runner = budget::Runner::new(deadline, budget);
    let mut adb = Adb::new(runner, serial);
    adb.bind_package(package.clone());
    adb.ensure_foreground()?;
    adb.capture_cache_context()?;
    Ok(adb)
}

fn action_settle_ms() -> u64 {
    std::env::var("MINIMAP_ACTION_SETTLE_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(DEFAULT_ACTION_SETTLE_MS)
        .min(2_000)
}

fn confirm_place_result<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    id: &str,
) -> Result<Value> {
    let layout = observe_layout(android, false)?;
    adb.ensure_foreground()?;
    let baseline = fingerprint_layout(&layout);
    let graph = load_graph(root)?;
    let matched = match_place(&baseline, graph.places.values().cloned());
    anyhow::ensure!(
        fingerprint_usable(&baseline) && detect_overlay(&layout).is_none(),
        "cannot confirm a blank screen or blocking overlay"
    );
    anyhow::ensure!(
        matched.status != "ambiguous"
            && (matched.status == "unknown" || matched.place_id.as_deref() == Some(id)),
        "confirmation conflicts with another known place"
    );
    let mut place = graph
        .places
        .get(id)
        .context("confirmed place ID does not exist")?
        .clone();
    let mut changed = Vec::new();
    if baseline.identity_hash != place.baseline.identity_hash
        && !place
            .variants
            .iter()
            .any(|v| v.identity_hash == baseline.identity_hash)
    {
        anyhow::ensure!(
            place.variants.len() < 16,
            "place variant limit reached; review existing variants before confirming more"
        );
        place.variants.push(baseline.clone());
        place
            .variants
            .sort_by(|a, b| a.identity_hash.cmp(&b.identity_hash));
        changed.push(commit_place(root, &place)?);
    }
    if let Some(path) = commit_pending_edge_for_place(root, adb, &graph, &place, &baseline)? {
        changed.push(path);
    }
    save_session_place(root, adb, &endpoint_for_place(&place), &baseline, &layout)?;
    Ok(result_with_data(
        "ok",
        "agent-confirmed observation attached to existing place",
        json!({"place": {"id": place.id, "slug": place.slug}, "changed_graph": !changed.is_empty(), "changed_files": changed_files_json(&changed), "confirmation": "host_agent"}),
    ))
}

fn whereami_result<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    label: Option<&str>,
    allow_duplicate_label: bool,
    allow_write: bool,
) -> Result<Value> {
    if let Some(label) = label {
        require_graph_text(label)?;
    }
    if label.is_none() {
        if let Some(session) =
            load_recent_session_place(root, adb, Duration::from_secs(LAYOUT_CACHE_TTL_SECS))?
        {
            let graph = load_graph(root).unwrap_or_else(|_| Graph {
                places: Default::default(),
                edges: Default::default(),
            });
            if let Some(place) = graph_place_for_session(&graph, &session) {
                return Ok(cached_whereami_json(root, &session.baseline, &place));
            }
            clear_session_place(root, adb)?;
        }
    }

    let layout = observe_layout(android, false)?;
    adb.ensure_foreground()?;
    let orientation = orient_layout(
        root,
        &layout,
        label,
        allow_duplicate_label,
        allow_write,
        adb,
    )?;
    remember_orientation_session(root, adb, &orientation, &layout)?;
    Ok(orientation_json(root, &orientation, true))
}

fn orient_layout<DR: CommandRunner>(
    root: &Path,
    layout: &Value,
    label: Option<&str>,
    allow_duplicate_label: bool,
    allow_write: bool,
    adb: &mut Adb<DR>,
) -> Result<Orientation> {
    let baseline = fingerprint_layout(layout);
    let graph = load_graph(root)?;
    let matched = match_place(&baseline, graph.places.values().cloned());
    let mut changed_files = Vec::new();
    let mut status = matched.status.clone();
    let mut matched_place = if matches!(matched.status.as_str(), "unknown" | "ambiguous") {
        None
    } else {
        matched
            .place_id
            .as_deref()
            .and_then(|id| graph.places.get(id))
            .cloned()
    };

    if matched.status == "ambiguous" || detect_overlay(layout).is_some() {
        return Ok(Orientation {
            status: if matched.status == "ambiguous" {
                "ambiguous"
            } else {
                "blocked_by_overlay"
            }
            .into(),
            baseline,
            matched_place: None,
            confidence: matched.confidence,
            hash_matched: false,
            changed_files,
        });
    }

    if let Some(label) = label {
        // normalize_label always yields a non-empty pure-ASCII slug (Tranche C),
        // so there is no empty-slug case to guard.
        let slug = normalize_label(label);
        let existing_label_place = graph
            .places
            .values()
            .find(|place| place.slug == slug)
            .cloned();
        match (matched_place.clone(), existing_label_place) {
            (Some(place), Some(existing)) if place.id != existing.id => {
                // The current screen matched a known place, but the requested
                // label slug is already owned by a DIFFERENT place. By default
                // this is a label_mismatch; under --allow-duplicate-label we
                // relabel the matched place with the smallest free numeric
                // suffix instead.
                if allow_write && allow_duplicate_label {
                    let unique = unique_label(&graph, label, &slug);
                    let (new_place, mut files) = relabel_place(root, &place, &unique, &baseline)?;
                    changed_files.append(&mut files);
                    matched_place = Some(new_place);
                    status = "ok".to_string();
                } else {
                    return Ok(Orientation {
                        status: "label_mismatch".to_string(),
                        baseline,
                        matched_place: Some(place),
                        confidence: matched.confidence,
                        hash_matched: matched.hash_matched,
                        changed_files,
                    });
                }
            }
            (Some(mut place), Some(_)) => {
                if allow_write && remember_place_observation(&mut place, &baseline) {
                    changed_files.push(commit_place(root, &place)?);
                    matched_place = Some(place);
                    status = "known_changed".to_string();
                } else {
                    status = "known".to_string();
                    matched_place = Some(place);
                }
            }
            (Some(place), None) => {
                if allow_write {
                    let (new_place, mut files) = relabel_place(root, &place, label, &baseline)?;
                    changed_files.append(&mut files);
                    matched_place = Some(new_place);
                    status = "ok".to_string();
                }
            }
            (None, Some(_existing)) => {
                // A NEW fingerprint whose slug collides with a different existing
                // place: label_mismatch (no write) by default, or a fresh
                // suffixed place under --allow-duplicate-label.
                if allow_write && allow_duplicate_label && fingerprint_usable(&baseline) {
                    let unique = unique_label(&graph, label, &slug);
                    let place = new_place(&graph, &unique, &baseline);
                    changed_files.push(commit_place(root, &place)?);
                    commit_pending_edge_for_place(root, adb, &graph, &place, &baseline)?
                        .into_iter()
                        .for_each(|file| changed_files.push(file));
                    matched_place = Some(place);
                    status = "ok".to_string();
                } else {
                    return Ok(Orientation {
                        status: "label_mismatch".to_string(),
                        baseline,
                        matched_place: None,
                        confidence: matched.confidence,
                        hash_matched: matched.hash_matched,
                        changed_files,
                    });
                }
            }
            (None, None) => {
                if allow_write && fingerprint_usable(&baseline) {
                    let place = new_place(&graph, label, &baseline);
                    changed_files.push(commit_place(root, &place)?);
                    commit_pending_edge_for_place(root, adb, &graph, &place, &baseline)?
                        .into_iter()
                        .for_each(|file| changed_files.push(file));
                    matched_place = Some(place);
                    status = "ok".to_string();
                }
            }
        }
    } else if allow_write && status == "known_changed" {
        if let Some(mut place) = matched_place.clone() {
            if remember_place_observation(&mut place, &baseline) {
                changed_files.push(commit_place(root, &place)?);
                matched_place = Some(place);
            }
        }
    }

    Ok(Orientation {
        status,
        baseline,
        matched_place,
        confidence: matched.confidence,
        hash_matched: matched.hash_matched,
        changed_files,
    })
}

fn orientation_json(root: &Path, orientation: &Orientation, include_exits: bool) -> Value {
    let changed_files = changed_files_json(&orientation.changed_files);
    let graph = load_graph(root).ok();
    let place_json = orientation.matched_place.as_ref().map(|place| {
        json!({
            "id": place.id,
            "slug": place.slug,
            "label": place.label
        })
    });
    let known_exits = if include_exits {
        graph
            .as_ref()
            .zip(orientation.matched_place.as_ref())
            .map(|(graph, place)| {
                graph
                    .edges
                    .values()
                    .filter(|edge| edge.from.id == place.id)
                    .map(|edge| {
                        json!({
                            "edge": edge.id,
                            "to": edge.to.slug,
                            "intent": edge.intent,
                            "recipe": edge.recipe
                        })
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let mut value = json!({
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": orientation.status,
        "summary": match orientation.status.as_str() {
            "known" => "current place is known",
            "known_changed" => "current place matched and baseline was updated",
            "label_mismatch" => "label belongs to a different known place",
            "ok" => "place label applied",
            _ => "current place is unknown"
        },
        "place": place_json,
        "match": {
            "confidence": orientation.confidence,
            "hash_matched": orientation.hash_matched,
            "identity_hash": orientation.baseline.identity_hash
        },
        "known_exits": known_exits,
        "changed_graph": !orientation.changed_files.is_empty(),
        "changed_files": changed_files
    });
    if orientation.status != "known" {
        value["fingerprint_summary"] = fingerprint_summary(&orientation.baseline);
    }
    value
}

fn cached_whereami_json(root: &Path, baseline: &PlaceBaseline, place: &Place) -> Value {
    let orientation = Orientation {
        status: "known".to_string(),
        baseline: baseline.clone(),
        matched_place: Some(place.clone()),
        confidence: 1.0,
        hash_matched: true,
        changed_files: Vec::new(),
    };
    let mut value = orientation_json(root, &orientation, true);
    value["cache"] = json!({
        "hit": true,
        "source": "session-place",
        "max_age_secs": LAYOUT_CACHE_TTL_SECS
    });
    value["metrics"] = json!({
        "layout_calls_total": 0,
        "layout_json_returned_to_agent": false
    });
    value
}

fn layout_minimap_json(
    graph: &Graph,
    baseline: &PlaceBaseline,
    place: Option<&Place>,
    status: &str,
    confidence: f64,
    hash_matched: bool,
) -> Value {
    let known_exits = place
        .map(|place| {
            graph
                .edges
                .values()
                .filter(|edge| edge.from.id == place.id)
                .map(|edge| json!({"edge": edge.id, "to": edge.to.slug, "intent": edge.intent}))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    json!({
        "status": status,
        "place": place.map(|place| {
            json!({"id": place.id, "slug": place.slug, "label": place.label})
        }),
        "match": {
            "confidence": confidence,
            "hash_matched": hash_matched,
            "identity_hash": baseline.identity_hash
        },
        "known_exits": known_exits
    })
}

fn layout_result<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    diff: bool,
) -> Result<Value> {
    if !diff {
        if let Some(session) =
            load_recent_session_place(root, adb, Duration::from_secs(LAYOUT_CACHE_TTL_SECS))?
        {
            let graph = load_graph(root).unwrap_or_else(|_| Graph {
                places: Default::default(),
                edges: Default::default(),
            });
            if let Some(place) = graph_place_for_session(&graph, &session) {
                return Ok(json!({
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "status": "ok",
                    "kind": "android_layout",
                    "layout": session.layout,
                    "minimap": layout_minimap_json(
                        &graph,
                        &session.baseline,
                        Some(&place),
                        "known",
                        1.0,
                        true
                    ),
                    "cache": {
                        "hit": true,
                        "source": "session-place",
                        "max_age_secs": LAYOUT_CACHE_TTL_SECS
                    },
                    "metrics": {
                        "layout_calls_total": 0,
                        "layout_json_returned_to_agent": true
                    },
                    "changed_graph": false,
                    "changed_files": []
                }));
            }
        }
    }

    let layout = observe_layout(android, diff)?;
    let (minimap, cache_hit) = if diff {
        (json!({"orientation": "unavailable_for_diff"}), false)
    } else {
        let orientation = orient_layout(root, &layout, None, false, false, adb)?;
        remember_orientation_session(root, adb, &orientation, &layout)?;
        let graph = load_graph(root).unwrap_or_else(|_| Graph {
            places: Default::default(),
            edges: Default::default(),
        });
        (
            layout_minimap_json(
                &graph,
                &orientation.baseline,
                orientation.matched_place.as_ref(),
                &orientation.status,
                orientation.confidence,
                orientation.hash_matched,
            ),
            false,
        )
    };
    Ok(json!({
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "ok",
        "kind": if diff { "android_layout_diff" } else { "android_layout" },
        "layout": redact_layout(&layout),
        "minimap": minimap,
        "cache": {"hit": cache_hit},
        "metrics": {
            "layout_calls_total": android.layout_calls(),
            "layout_json_returned_to_agent": true
        },
        "changed_graph": false,
        "changed_files": []
    }))
}

fn action_outcome<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
    result: Result<Value>,
) -> Result<Value> {
    let status = result
        .as_ref()
        .ok()
        .and_then(|value| value["status"].as_str());
    if !matches!(status, Some("ok" | "known" | "known_changed")) {
        let _ = clear_session_place(root, adb);
        if status != Some("needs_label") {
            let _ = clear_pending(root, adb);
        }
    }
    result
}

fn tap_result<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    request: TapRequest<'_>,
) -> Result<Value> {
    let result = record_tap(root, android, adb, request);
    action_outcome(root, adb, result)
}

fn record_tap<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    request: TapRequest<'_>,
) -> Result<Value> {
    for text in [request.label, request.reason].into_iter().flatten() {
        require_graph_text(text)?;
    }
    let action_count = request.selector.is_some() as u8
        + request.point.is_some() as u8
        + request.screenshot_label.is_some() as u8;
    if action_count != 1 {
        anyhow::bail!("tap requires exactly one of --selector, --point, or --screenshot-label");
    }
    if request.screenshot_label.is_some() && request.screenshot.is_none() {
        anyhow::bail!("--screenshot-label requires --screenshot");
    }
    // Validate the supplied value before observing/orienting the layout (which can
    // write to the graph). A malformed coordinate/selector must be a guaranteed
    // no-op, not a partial mutation that then errors out.
    if let Some(point) = request.point {
        parse_point(point)?;
    }
    if let Some(selector) = request.selector {
        let (_, value) = parse_selector(selector)?;
        require_graph_text(&value)?;
    }

    let pre_layout = observe_layout(android, false)?;
    let pre_orientation = orient_layout(root, &pre_layout, None, false, false, adb)?;
    let pre_pending = load_pending(root, adb)?;
    let source_place = match pre_orientation.matched_place.clone() {
        Some(place) => place,
        None => {
            let pending_source = pre_pending
                .as_ref()
                .filter(|pending| {
                    pending.destination.identity_hash == pre_orientation.baseline.identity_hash
                })
                .map(|pending| pending.source.id.clone());
            if let Some(source_id) = pending_source {
                load_graph(root)?
                    .places
                    .get(&source_id)
                    .cloned()
                    .context("pending transition source place missing")?
            } else {
                return Ok(result_with_data(
                    "needs_label",
                    "current source place is unknown; run whereami --label before recording a transition",
                    json!({
                        "orientation": orientation_json(root, &pre_orientation, true)
                    }),
                ));
            }
        }
    };
    let action = match build_and_execute_tap_action(
        android,
        adb,
        &pre_layout,
        request.selector,
        request.point,
        request.screenshot_label,
        request.screenshot,
    )? {
        TapActionOutcome::Recorded(action) => action,
        TapActionOutcome::SelectorNotFound(message) => {
            return Ok(result_with_data(
                "action_failed",
                &message,
                json!({"changed_graph": false, "changed_files": []}),
            ));
        }
        TapActionOutcome::ViewportUnavailable => {
            return Ok(result_with_data(
                "environment_error",
                "device viewport unavailable for geometry edge",
                json!({"changed_graph": false, "changed_files": []}),
            ));
        }
    };
    let pending = pre_pending.filter(|pending| {
        pending.source.id == source_place.id
            && pending.destination.identity_hash == pre_orientation.baseline.identity_hash
    });
    let mut recipe = pending
        .as_ref()
        .map(|pending| pending.recipe.clone())
        .unwrap_or_default();
    recipe.push(action.clone());
    let edge_source = pending
        .as_ref()
        .map(|pending| pending.source.clone())
        .unwrap_or_else(|| endpoint_for_place(&source_place));
    let edge_intent = request.reason.or_else(|| {
        pending
            .as_ref()
            .and_then(|pending| pending.intent.as_deref())
    });
    let post_layout = observe_after_action(android, Some(&pre_orientation.baseline))?;
    adb.ensure_foreground()?;
    let post_baseline = fingerprint_layout(&post_layout);
    if let Some(reason) = detect_overlay(&post_layout) {
        clear_pending(root, adb)?;
        clear_session_place(root, adb)?;
        return Ok(result_with_data(
            "blocked_by_overlay",
            &reason,
            json!({"reason": reason, "changed_graph": !pre_orientation.changed_files.is_empty(),
                   "changed_files": changed_files_json(&pre_orientation.changed_files)}),
        ));
    }
    let mut graph = load_graph(root)?;
    let post_match = match_place(&post_baseline, graph.places.values().cloned());
    let mut changed_files = Vec::new();
    if post_match.status == "ambiguous" {
        clear_pending(root, adb)?;
        clear_session_place(root, adb)?;
        return Ok(result_with_data(
            "ambiguous",
            "destination matches multiple places; inspect fresh UI before learning",
            json!({"changed_graph": !changed_files.is_empty(), "changed_files": changed_files_json(&changed_files)}),
        ));
    }
    let matched_post = if matches!(post_match.status.as_str(), "unknown" | "ambiguous") {
        None
    } else {
        post_match
            .place_id
            .as_deref()
            .and_then(|id| graph.places.get(id))
            .cloned()
    };

    if matched_post
        .as_ref()
        .map(|place| place.id == source_place.id)
        .unwrap_or(false)
    {
        save_pending(
            root,
            adb,
            &PendingTransition {
                source: edge_source.clone(),
                recipe: recipe.clone(),
                destination: post_baseline.clone(),
                intent: edge_intent.map(str::to_string),
            },
        )?;
        save_session_place(
            root,
            adb,
            &endpoint_for_place(&source_place),
            &post_baseline,
            &post_layout,
        )?;
        return Ok(result_with_data(
            "ok",
            "tap retained in the pending transition; destination unchanged",
            json!({
                "source": source_place.slug,
                "changed_graph": false,
                "changed_files": []
            }),
        ));
    }

    let destination = match request.label {
        Some(label) => {
            // normalize_label always yields a non-empty pure-ASCII slug.
            let slug = normalize_label(label);
            let label_place = graph
                .places
                .values()
                .find(|place| place.slug == slug)
                .cloned();
            match (label_place, matched_post) {
                (Some(target), Some(observed)) if target.id != observed.id => {
                    return Ok(result_with_data(
                        "label_mismatch",
                        "tap reached a different known place than the requested label",
                        json!({
                            "requested_label": slug,
                            "observed": observed.slug,
                            "changed_graph": false,
                            "changed_files": []
                        }),
                    ));
                }
                // The post-layout fingerprint matched the place that already owns
                // this label slug: a legitimate same-place observation, so fold it
                // in as a variant.
                (Some(mut target), Some(_observed)) => {
                    if target.baseline.identity_hash != post_baseline.identity_hash
                        && !fingerprint_usable(&post_baseline)
                    {
                        return Ok(result_with_data(
                            "unknown",
                            "destination layout has no usable fingerprint",
                            json!({"changed_graph": false, "changed_files": []}),
                        ));
                    }
                    if remember_place_observation(&mut target, &post_baseline) {
                        changed_files.push(commit_place(root, &target)?);
                    }
                    target
                }
                // A NEW fingerprint (no similarity match) whose label slug collides
                // with a DIFFERENT existing place. Previously this silently merged
                // the new screen into the slug owner; now it is a label_mismatch
                // (no write) by default, or a fresh suffixed place under
                // --allow-duplicate-label.
                (Some(target), None) => {
                    if !request.allow_duplicate_label {
                        return Ok(result_with_data(
                            "label_mismatch",
                            "tap reached a new place whose label collides with a different known place; pass --allow-duplicate-label to keep both",
                            json!({
                                "requested_label": slug,
                                "collides_with": target.slug,
                                "changed_graph": false,
                                "changed_files": []
                            }),
                        ));
                    }
                    if !fingerprint_usable(&post_baseline) {
                        clear_session_place(root, adb)?;
                        return Ok(result_with_data(
                            "unknown",
                            "destination layout has no usable fingerprint",
                            json!({"changed_graph": false, "changed_files": []}),
                        ));
                    }
                    let unique = unique_label(&graph, label, &slug);
                    let place = new_place(&graph, &unique, &post_baseline);
                    changed_files.push(commit_place(root, &place)?);
                    graph.places.insert(place.id.clone(), place.clone());
                    place
                }
                (None, Some(observed)) => {
                    return Ok(result_with_data(
                        "label_mismatch",
                        "tap reached a known place with a different label",
                        json!({
                            "requested_label": slug,
                            "observed": observed.slug,
                            "changed_graph": false,
                            "changed_files": []
                        }),
                    ));
                }
                (None, None) => {
                    if !fingerprint_usable(&post_baseline) {
                        clear_session_place(root, adb)?;
                        return Ok(result_with_data(
                            "unknown",
                            "destination layout has no usable fingerprint",
                            json!({"changed_graph": false, "changed_files": []}),
                        ));
                    }
                    let place = new_place(&graph, label, &post_baseline);
                    changed_files.push(commit_place(root, &place)?);
                    graph.places.insert(place.id.clone(), place.clone());
                    place
                }
            }
        }
        None => {
            if let Some(place) = matched_post {
                place
            } else {
                if let Some(reason) = detect_overlay(&post_layout) {
                    return Ok(result_with_data(
                        "blocked_by_overlay",
                        "a blocking overlay (e.g. a permission dialog) intercepted the transition; no edge recorded",
                        json!({
                            "reason": reason,
                            "changed_graph": false,
                            "changed_files": []
                        }),
                    ));
                }
                save_pending(
                    root,
                    adb,
                    &PendingTransition {
                        source: edge_source.clone(),
                        recipe: recipe.clone(),
                        destination: post_baseline,
                        intent: edge_intent.map(str::to_string),
                    },
                )?;
                clear_session_place(root, adb)?;
                return Ok(result_with_data(
                    "needs_label",
                    "tap reached an unknown destination; rerun whereami --label to commit it",
                    json!({
                        "source": source_place.slug,
                        "changed_graph": false,
                        "changed_files": []
                    }),
                ));
            }
        }
    };

    let edge = edge_from_parts(
        &edge_source,
        &endpoint_for_place(&destination),
        recipe,
        edge_intent,
    );
    changed_files.extend(persist_edge(root, &edge)?);
    clear_pending(root, adb)?;
    save_session_place(
        root,
        adb,
        &endpoint_for_place(&destination),
        &post_baseline,
        &post_layout,
    )?;

    Ok(result_with_data(
        "ok",
        "tap transition recorded",
        json!({
            "from": edge_source.slug,
            "to": destination.slug,
            "edge": edge.id,
            "changed_graph": !changed_files.is_empty(),
            "changed_files": changed_files_json(&changed_files)
        }),
    ))
}

/// Outcome of attempting a tap action. Runtime/device failures are surfaced as
/// structured variants so `tap_result` can map them to the right status
/// (`action_failed` / `environment_error`) instead of letting them bubble up to
/// the `main()` catch-all and collapse into `config_error`.
enum TapActionOutcome {
    Recorded(ActionStep),
    SelectorNotFound(String),
    ViewportUnavailable,
}

fn build_and_execute_tap_action<AR: CommandRunner, DR: CommandRunner>(
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    pre_layout: &Value,
    selector: Option<&str>,
    point: Option<&str>,
    screenshot_label: Option<i64>,
    screenshot: Option<&str>,
) -> Result<TapActionOutcome> {
    if let Some(selector) = selector {
        let tap_point = match resolve_selector_point(pre_layout, selector) {
            Ok(point) => point,
            Err(error) => return Ok(TapActionOutcome::SelectorNotFound(error.to_string())),
        };
        anyhow::ensure!(
            resolve_selector_point(&redact_layout(pre_layout), selector).ok() == Some(tap_point),
            "selector refers to private or editable content; use a stable control identifier"
        );
        adb.tap(tap_point)?;
        let (kind, value) = parse_selector(selector)?;
        return Ok(TapActionOutcome::Recorded(ActionStep {
            kind: "tap".to_string(),
            selector: Some(Selector { kind, value }),
            point: None,
            viewport: None,
            direction: None,
        }));
    }
    if let Some(point) = point {
        let (x, y) = parse_point(point)?;
        let Some(viewport) = adb.display_size().ok() else {
            return Ok(TapActionOutcome::ViewportUnavailable);
        };
        anyhow::ensure!(
            x >= 0 && y >= 0 && x < viewport.width && y < viewport.height,
            "tap point is outside the device viewport"
        );
        adb.tap(TapPoint { x, y })?;
        return Ok(TapActionOutcome::Recorded(ActionStep {
            kind: "tap".to_string(),
            selector: None,
            point: Some(Point { x, y }),
            viewport: Some(viewport),
            direction: None,
        }));
    }
    let label = screenshot_label.expect("checked action count");
    let screenshot = screenshot.expect("checked screenshot");
    android.screen_capture(screenshot, true)?;
    let resolved = android.screen_resolve(screenshot, &format!("input tap #{label}"))?;
    let tap_point = parse_input_tap(&resolved.stdout)?;
    let Some(viewport) = adb.display_size().ok() else {
        return Ok(TapActionOutcome::ViewportUnavailable);
    };
    anyhow::ensure!(
        tap_point.x >= 0
            && tap_point.y >= 0
            && tap_point.x < viewport.width
            && tap_point.y < viewport.height,
        "tap point is outside the device viewport"
    );
    adb.tap(tap_point)?;
    Ok(TapActionOutcome::Recorded(ActionStep {
        kind: "tap".to_string(),
        selector: None,
        point: Some(Point {
            x: tap_point.x,
            y: tap_point.y,
        }),
        viewport: Some(viewport),
        direction: None,
    }))
}

fn scroll_result<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    direction: &str,
) -> Result<Value> {
    anyhow::ensure!(
        ["up", "down", "left", "right"].contains(&direction),
        "unsupported scroll direction"
    );
    let result = record_scroll(root, android, adb, direction);
    action_outcome(root, adb, result)
}

fn record_scroll<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    direction: &str,
) -> Result<Value> {
    let pre_layout = observe_layout(android, false)?;
    let pre_orientation = orient_layout(root, &pre_layout, None, false, false, adb)?;
    let pending = load_pending(root, adb)?.filter(|pending| {
        pending.destination.identity_hash == pre_orientation.baseline.identity_hash
    });
    let graph = load_graph(root)?;
    let source = pending
        .as_ref()
        .and_then(|pending| graph.places.get(&pending.source.id).cloned())
        .or_else(|| pre_orientation.matched_place.clone());
    let viewport = adb.display_size()?;
    let (sx, sy, ex, ey) = swipe_for_direction(direction, viewport);
    adb.swipe(sx, sy, ex, ey, 350)?;
    let post_layout = observe_after_action(android, Some(&pre_orientation.baseline))?;
    adb.ensure_foreground()?;
    let post_orientation = orient_layout(root, &post_layout, None, false, true, adb)?;
    if matches!(
        post_orientation.status.as_str(),
        "ambiguous" | "blocked_by_overlay"
    ) {
        return Ok(result_with_data(
            &post_orientation.status,
            "fresh observation requires agent recovery",
            json!({"changed_graph": false, "changed_files": []}),
        ));
    }
    let step = ActionStep {
        kind: "scroll".to_string(),
        selector: None,
        point: None,
        viewport: None,
        direction: Some(direction.to_string()),
    };
    let mut recipe = pending.map(|pending| pending.recipe).unwrap_or_default();
    recipe.push(step);
    if let Some(source) = source.clone() {
        if let Some(dest) = post_orientation
            .matched_place
            .clone()
            .filter(|dest| source.id != dest.id)
        {
            let edge = edge_from_parts(
                &endpoint_for_place(&source),
                &endpoint_for_place(&dest),
                recipe.clone(),
                Some("scroll"),
            );
            let mut paths = post_orientation.changed_files.clone();
            paths.extend(persist_edge(root, &edge)?);
            clear_pending(root, adb)?;
            save_session_place(
                root,
                adb,
                &endpoint_for_place(&dest),
                &post_orientation.baseline,
                &post_layout,
            )?;
            return Ok(result_with_data(
                "ok",
                "scroll transition recorded",
                json!({
                    "from": source.slug,
                    "to": dest.slug,
                    "edge": edge.id,
                    "changed_graph": !paths.is_empty(),
                    "changed_files": changed_files_json(&paths)
                }),
            ));
        }
        save_pending(
            root,
            adb,
            &PendingTransition {
                source: endpoint_for_place(&source),
                recipe: recipe.clone(),
                destination: post_orientation.baseline.clone(),
                intent: Some("scroll".to_string()),
            },
        )?;
    }
    remember_orientation_session(root, adb, &post_orientation, &post_layout)?;
    Ok(result_with_data(
        "ok",
        "scroll executed",
        json!({
            "place": post_orientation.matched_place.map(|place| place.slug),
            "changed_graph": !post_orientation.changed_files.is_empty(),
            "changed_files": changed_files_json(&post_orientation.changed_files)
        }),
    ))
}

fn back_result<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
) -> Result<Value> {
    let result = record_back(root, android, adb);
    action_outcome(root, adb, result)
}

fn record_back<AR: CommandRunner, DR: CommandRunner>(
    root: &Path,
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
) -> Result<Value> {
    clear_pending(root, adb)?;
    let pre_layout = observe_layout(android, false)?;
    let pre_orientation = orient_layout(root, &pre_layout, None, false, false, adb)?;
    adb.back()?;
    let post_layout = observe_after_action(android, Some(&pre_orientation.baseline))?;
    adb.ensure_foreground()?;
    let post_orientation = orient_layout(root, &post_layout, None, false, true, adb)?;
    if matches!(
        post_orientation.status.as_str(),
        "ambiguous" | "blocked_by_overlay"
    ) {
        return Ok(result_with_data(
            &post_orientation.status,
            "fresh observation requires agent recovery",
            json!({"changed_graph": false, "changed_files": []}),
        ));
    }
    if let (Some(source), Some(dest)) = (
        pre_orientation.matched_place.clone(),
        post_orientation.matched_place.clone(),
    ) {
        if source.id != dest.id {
            let edge = edge_from_parts(
                &endpoint_for_place(&source),
                &endpoint_for_place(&dest),
                vec![ActionStep {
                    kind: "press_back".to_string(),
                    selector: None,
                    point: None,
                    viewport: None,
                    direction: None,
                }],
                Some("press back"),
            );
            let mut paths = post_orientation.changed_files.clone();
            paths.extend(persist_edge(root, &edge)?);
            save_session_place(
                root,
                adb,
                &endpoint_for_place(&dest),
                &post_orientation.baseline,
                &post_layout,
            )?;
            return Ok(result_with_data(
                "ok",
                "back transition recorded",
                json!({
                    "from": source.slug,
                    "to": dest.slug,
                    "edge": edge.id,
                    "changed_graph": !paths.is_empty(),
                    "changed_files": changed_files_json(&paths)
                }),
            ));
        }
    }
    remember_orientation_session(root, adb, &post_orientation, &post_layout)?;
    Ok(result_with_data(
        "ok",
        "back executed",
        json!({
            "place": post_orientation.matched_place.map(|place| place.slug),
            "changed_graph": !post_orientation.changed_files.is_empty(),
            "changed_files": changed_files_json(&post_orientation.changed_files)
        }),
    ))
}

fn execute_recipe<AR: CommandRunner, DR: CommandRunner>(
    android: &mut AndroidCli<AR>,
    adb: &mut Adb<DR>,
    recipe: &[ActionStep],
    initial_layout: Option<&Value>,
    deadline: std::time::Instant,
) -> Result<()> {
    let mut cached_layout = initial_layout.cloned();
    let current_display_size = if recipe.iter().any(ActionStep::is_geometry) {
        adb.display_size().ok()
    } else {
        None
    };
    for (index, step) in recipe.iter().enumerate() {
        anyhow::ensure!(
            std::time::Instant::now() < deadline,
            "navigation deadline exhausted"
        );
        // Selector steps wait for fresh, actionable UI below. Coordinate and
        // gesture steps have no such condition, so retain their settling delay.
        if index > 0 && step.selector.is_none() {
            thread::sleep(
                Duration::from_millis(action_settle_ms())
                    .min(deadline.saturating_duration_since(std::time::Instant::now())),
            );
        }
        match step.kind.as_str() {
            "tap" => {
                if let Some(selector) = &step.selector {
                    let selector = format!("{}={}", selector.kind, selector.value);
                    let layout =
                        observation::observe_selector(android, &selector, cached_layout.take())?;
                    let point = resolve_selector_point(&layout, &selector)?;
                    adb.tap(point)?;
                } else if let Some(point) = step.point {
                    if step.is_geometry()
                        && (step.viewport.is_none() || current_display_size != step.viewport)
                    {
                        anyhow::bail!(
                            "geometry edge requires a matching device viewport; refusing to tap raw pixels"
                        );
                    }
                    adb.tap(TapPoint {
                        x: point.x,
                        y: point.y,
                    })?;
                } else {
                    anyhow::bail!("tap step has neither selector nor point");
                }
                cached_layout = None;
            }
            "scroll" => {
                let viewport = adb.display_size()?;
                let direction = step.direction.as_deref().unwrap_or("down");
                let (sx, sy, ex, ey) = swipe_for_direction(direction, viewport);
                adb.swipe(sx, sy, ex, ey, 350)?;
                cached_layout = None;
            }
            "press_back" => {
                adb.back()?;
                cached_layout = None;
            }
            other => anyhow::bail!("unsupported recipe step: {other}"),
        }
    }
    Ok(())
}

fn doctor(root: &Path, serial: Option<&str>, repo_only: bool) -> Value {
    let mut repo_checks = validate_graph(root);
    if let Ok(graph) = load_graph(root) {
        let unsafe_metadata = graph
            .places
            .values()
            .any(|place| !minimap_core::safe_graph_text(&place.label))
            || graph.edges.values().any(|edge| {
                edge.intent
                    .as_deref()
                    .is_some_and(|text| !minimap_core::safe_graph_text(text))
                    || edge
                        .recipe
                        .iter()
                        .filter_map(|step| step.selector.as_ref())
                        .any(|selector| !minimap_core::safe_graph_text(&selector.value))
            });
        repo_checks.push(json!({"name":"graph_privacy", "status":if unsafe_metadata {"fail"} else {"pass"},
            "detail":if unsafe_metadata {Some("Review sensitive labels, intents, or action selectors before sharing the graph")} else {None}}));
    }
    let repo_ok = repo_checks.iter().all(|check| check["status"] == "pass");
    if repo_only {
        return json!({"schema_version": RESULT_SCHEMA_VERSION, "status": if repo_ok { "ok" } else { "config_error" }, "ok": repo_ok, "repo_ok": repo_ok, "checks": {"repo": repo_checks}});
    }
    let android_ok = command_on_path("android");
    let adb_ok = command_on_path("adb");
    // With no serial resolved, two or more attached devices make every bare
    // adb/android call ambiguous, so fail the device check with a fix instead
    // of surfacing adb's opaque "more than one device" error.
    let multi_device = serial.is_none() && adb_ok && adb_devices_in_device_state() > 1;
    let device_ok = adb_ok && !multi_device && adb_device_ready(serial);
    let mut device_check = json!({
        "name": "device",
        "status": if device_ok { "pass" } else { "fail" }
    });
    if multi_device {
        device_check["hint"] =
            json!("multiple devices attached; pass --serial or set ANDROID_SERIAL");
    }
    if let Some(serial) = serial {
        device_check["serial"] = json!(serial);
    }
    json!({
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": if repo_ok && android_ok && adb_ok && device_ok { "ok" } else { "config_error" },
        "ok": repo_ok && android_ok && adb_ok && device_ok,
        "repo_ok": repo_ok,
        "device_ok": device_ok,
        "checks": {
            "repo": repo_checks,
            "environment": [
                {"name": "android", "status": if android_ok { "pass" } else { "fail" }},
                {"name": "adb", "status": if adb_ok { "pass" } else { "fail" }},
                device_check
            ]
        }
    })
}

fn command_on_path(name: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|path| path.join(name).exists()))
        .unwrap_or(false)
}

fn adb_device_ready(serial: Option<&str>) -> bool {
    let mut args = vec!["adb".to_string()];
    if let Some(serial) = serial {
        args.extend(["-s".into(), serial.into()]);
    }
    args.push("get-state".into());
    SubprocessRunner::default()
        .run(&args, &[])
        .is_ok_and(|out| out.status == 0 && out.stdout.trim() == "device")
}

fn adb_devices_in_device_state() -> usize {
    let Ok(output) = SubprocessRunner::default().run(&["adb".into(), "devices".into()], &[]) else {
        return 0;
    };
    if output.status != 0 {
        return 0;
    }
    output
        .stdout
        .lines()
        .skip(1)
        .filter(|line| line.split_whitespace().nth(1) == Some("device"))
        .count()
}

fn new_place(graph: &Graph, label: &str, baseline: &PlaceBaseline) -> Place {
    let mut place = place_from_label(label, baseline);
    if graph.places.contains_key(&place.id) {
        place.id = format!(
            "{}_{}",
            place.id,
            baseline.identity_hash.trim_start_matches("sha256:")
        );
    }
    place
}

fn place_from_label(label: &str, baseline: &PlaceBaseline) -> Place {
    let slug = normalize_label(label);
    Place {
        schema_version: PLACE_SCHEMA_VERSION.to_string(),
        id: place_id_for_slug(&slug),
        slug,
        label: label.trim().to_string(),
        baseline: baseline.clone(),
        variants: Vec::new(),
    }
}

/// Derive a label whose slug does not collide with any existing place by
/// appending the smallest free numeric suffix (e.g. `Account Settings` ->
/// `Account Settings 2`, normalizing to `account-settings-2`). `slug` is the
/// already-normalized form of `label`; it is assumed to be taken.
fn unique_label(graph: &Graph, label: &str, slug: &str) -> String {
    let taken = |candidate: &str| graph.places.values().any(|place| place.slug == candidate);
    debug_assert!(taken(slug), "unique_label called for a free slug");
    let base = label.trim();
    let mut suffix = 2u32;
    loop {
        let candidate = format!("{base} {suffix}");
        if !taken(&normalize_label(&candidate)) {
            return candidate;
        }
        suffix += 1;
    }
}

/// If a pending transition lands on `place` (its destination hash matches and its
/// source is a known place), commit the edge and clear the pending state.
/// Returns the committed edge file path (if any) so the caller can record it.
fn persist_edge(root: &Path, edge: &Edge) -> Result<Vec<PathBuf>> {
    let path = minimap_repo::edge_path(root, &edge.id);
    let expected = canonical_json(&serde_json::to_value(edge)?);
    if fs::read_to_string(&path).ok().as_deref() == Some(expected.as_str()) {
        return Ok(Vec::new());
    }
    Ok(vec![commit_edge(root, edge)?])
}

fn commit_pending_edge_for_place<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
    graph: &Graph,
    place: &Place,
    baseline: &PlaceBaseline,
) -> Result<Option<PathBuf>> {
    if let Some(mut pending) = load_pending(root, adb)? {
        if pending.destination.identity_hash == baseline.identity_hash
            && graph.places.contains_key(&pending.source.id)
        {
            let edge = edge_from_parts(
                &pending.source,
                &endpoint_for_place(place),
                pending.recipe.split_off(0),
                pending.intent.as_deref(),
            );
            let files = persist_edge(root, &edge)?;
            clear_pending(root, adb)?;
            return Ok(files.into_iter().next());
        }
    }
    Ok(None)
}

fn remember_place_observation(place: &mut Place, baseline: &PlaceBaseline) -> bool {
    if place.baseline.identity_hash == baseline.identity_hash
        || place
            .variants
            .iter()
            .any(|variant| variant.identity_hash == baseline.identity_hash)
    {
        return false;
    }
    if !fingerprint_usable(baseline) {
        return false;
    }
    if !fingerprint_usable(&place.baseline) {
        place.baseline = baseline.clone();
        return true;
    }
    // Bound graph growth and prevent a chain of fuzzy variants drifting away
    // from the original semantic place.
    let mut original = place.clone();
    original.variants.clear();
    if place.variants.len() >= 16
        || match_place(baseline, std::iter::once(original)).status == "unknown"
    {
        return false;
    }
    place.variants.push(baseline.clone());
    place
        .variants
        .sort_by(|left, right| left.identity_hash.cmp(&right.identity_hash));
    true
}

fn endpoint_for_place(place: &Place) -> EdgeEndpoint {
    EdgeEndpoint {
        id: place.id.clone(),
        slug: place.slug.clone(),
    }
}

fn relabel_place(
    root: &Path,
    place: &Place,
    label: &str,
    baseline: &PlaceBaseline,
) -> Result<(Place, Vec<PathBuf>)> {
    let mut updated = place.clone();
    updated.slug = normalize_label(label);
    updated.label = label.trim().to_string();
    remember_place_observation(&mut updated, baseline);
    let path = commit_place(root, &updated)?;
    Ok((updated, vec![path]))
}

fn edge_from_parts(
    from: &EdgeEndpoint,
    to: &EdgeEndpoint,
    recipe: Vec<ActionStep>,
    intent: Option<&str>,
) -> Edge {
    Edge {
        superseded_by: Vec::new(),
        schema_version: EDGE_SCHEMA_VERSION.to_string(),
        id: edge_id(from, to, &recipe),
        from: from.clone(),
        to: to.clone(),
        intent: intent.map(str::to_string),
        recipe,
    }
}

fn edge_id(from: &EdgeEndpoint, to: &EdgeEndpoint, recipe: &[ActionStep]) -> String {
    let primary = recipe
        .first()
        .map(action_fingerprint)
        .unwrap_or_else(|| "action".to_string());
    // Readable prefixes must be stable too: a mutable label otherwise creates
    // a second filename for the exact same endpoint IDs and action recipe.
    let from_name = from.id.strip_prefix("place_").unwrap_or(&from.id);
    let to_name = to.id.strip_prefix("place_").unwrap_or(&to.id);
    let prefix = sanitize_id(&format!("edge_{from_name}__{to_name}__{primary}"));
    let digest = format!(
        "{:x}",
        Sha256::digest(
            canonical_json(&json!({
                "from": from.id, "to": to.id, "recipe": recipe
            }))
            .as_bytes()
        )
    );
    format!(
        "{}__{}",
        prefix.chars().take(64).collect::<String>(),
        digest
    )
}

fn action_fingerprint(step: &ActionStep) -> String {
    match step.kind.as_str() {
        "tap" => {
            if let Some(selector) = &step.selector {
                format!("tap_{}_{}", selector.kind, selector.value)
            } else if let Some(point) = step.point {
                format!("tap_point_{}_{}", point.x, point.y)
            } else {
                "tap".to_string()
            }
        }
        "scroll" => format!("scroll_{}", step.direction.as_deref().unwrap_or("down")),
        "press_back" => "press_back".to_string(),
        other => other.to_string(),
    }
}

fn sanitize_id(value: &str) -> String {
    let mut out = String::new();
    let mut last_underscore = false;
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || ch == '-' {
            out.push(ch);
            last_underscore = false;
        } else if !last_underscore {
            out.push('_');
            last_underscore = true;
        }
    }
    out.trim_matches('_').to_string()
}

fn parse_selector(selector: &str) -> Result<(String, String)> {
    let (kind, value) = selector
        .split_once('=')
        .ok_or_else(|| anyhow::anyhow!("selector must use kind=value syntax"))?;
    let kind = match kind.trim() {
        "id" | "resourceId" | "resource-id" => "resource_id",
        "testTag" | "test-tag" => "test_tag",
        "desc" | "content_description" | "contentDesc" | "contentDescription" | "content-desc" => {
            "content_desc"
        }
        other => other,
    }
    .to_string();
    let value = value.trim().to_string();
    Selector {
        kind: kind.clone(),
        value: value.clone(),
    }
    .validate()?;
    Ok((kind, value))
}

fn require_graph_text(text: &str) -> Result<()> {
    anyhow::ensure!(minimap_core::safe_graph_text(text),
        "value cannot be stored safely in the shared graph; use a stable non-sensitive label or selector");
    Ok(())
}

fn parse_point(point: &str) -> Result<(i64, i64)> {
    let (x, y) = point
        .split_once(',')
        .ok_or_else(|| anyhow::anyhow!("--point must be x,y"))?;
    Ok((x.trim().parse()?, y.trim().parse()?))
}

fn swipe_for_direction(direction: &str, viewport: Viewport) -> (i64, i64, i64, i64) {
    let x = viewport.width / 2;
    match direction {
        "up" => (x, viewport.height / 4, x, viewport.height * 5 / 6),
        "left" => (
            viewport.width * 2 / 3,
            viewport.height / 2,
            viewport.width / 3,
            viewport.height / 2,
        ),
        "right" => (
            viewport.width / 3,
            viewport.height / 2,
            viewport.width * 2 / 3,
            viewport.height / 2,
        ),
        _ => (x, viewport.height * 5 / 6, x, viewport.height / 4),
    }
}

fn fingerprint_summary(baseline: &PlaceBaseline) -> Value {
    json!({
        "identity_hash": baseline.identity_hash,
        "selectors": baseline.fingerprint.selectors.iter().take(12).collect::<Vec<_>>(),
        "static_text": baseline.fingerprint.static_text.iter().take(12).collect::<Vec<_>>(),
        "roles": baseline.fingerprint.roles
    })
}

fn result_with_data(status: &str, summary: &str, data: Value) -> Value {
    let mut result = serde_json::to_value(MinimapResult::new(status, summary, data)).unwrap();
    if status == "needs_label" {
        result["recommended_action"] = json!(
            "run whereami --label <place> if the current destination should be added to the graph"
        );
    }
    result
}

fn changed_files_json(paths: &[PathBuf]) -> Value {
    json!(paths
        .iter()
        .map(|path| path.display().to_string())
        .collect::<Vec<_>>())
}

/// Resolve the cache path for a given file name. Returns `Ok(None)` when the
/// device serial cannot be resolved: without a serial we cannot safely scope the
/// cache to one device, and a shared "unknown-device" bucket would let two
/// devices read each other's cached place, so we skip the cache entirely.
fn pending_path<DR: CommandRunner>(root: &Path, adb: &mut Adb<DR>) -> Result<Option<PathBuf>> {
    let repo = root
        .canonicalize()
        .unwrap_or_else(|_| root.to_path_buf())
        .display()
        .to_string();
    let repo_hash = format!("{:x}", Sha256::digest(repo.as_bytes()));
    let serial = match adb.serial() {
        Ok(serial) if !serial.trim().is_empty() => serial,
        _ => return Ok(None),
    };
    let package = load_config(root)
        .ok()
        .and_then(|config| {
            config
                .app_profiles
                .get(&config.active_app_profile)
                .map(|profile| profile.android_package.clone())
        })
        .filter(|package| !package.is_empty())
        .unwrap_or_else(|| "default-package".to_string());
    let context = adb.cache_context().unwrap_or("unbound");
    let context_hash = format!("{:x}", Sha256::digest(context.as_bytes()));
    Ok(Some(
        std::env::temp_dir()
            .join("minimap")
            .join(&repo_hash[..16])
            .join(sanitize_id(&serial))
            .join(sanitize_id(&package))
            .join(&context_hash[..16])
            .join("pending-transition.json"),
    ))
}

fn session_path<DR: CommandRunner>(root: &Path, adb: &mut Adb<DR>) -> Result<Option<PathBuf>> {
    let Some(mut path) = pending_path(root, adb)? else {
        return Ok(None);
    };
    path.set_file_name("session-place.json");
    Ok(Some(path))
}

fn remember_orientation_session<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
    orientation: &Orientation,
    layout: &Value,
) -> Result<()> {
    if let Some(place) = &orientation.matched_place {
        save_session_place(
            root,
            adb,
            &endpoint_for_place(place),
            &orientation.baseline,
            layout,
        )
    } else {
        clear_session_place(root, adb)
    }
}

fn save_session_place<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
    place: &EdgeEndpoint,
    baseline: &PlaceBaseline,
    layout: &Value,
) -> Result<()> {
    let Some(path) = session_path(root, adb)? else {
        return Ok(());
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
        restrict_cache_dir_permissions(parent);
    }
    let value = json!({
        "place": place,
        "baseline": baseline,
        "layout": redact_layout(layout)
    });
    minimap_repo::write_json(&path, &value)?;
    restrict_cache_file_permissions(&path);
    Ok(())
}

fn load_session_place<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
) -> Result<Option<SessionPlace>> {
    let Some(path) = session_path(root, adb)? else {
        return Ok(None);
    };
    if !path.exists() {
        return Ok(None);
    }
    if let Ok(metadata) = fs::metadata(&path) {
        if let Ok(modified) = metadata.modified() {
            let expired = SystemTime::now()
                .duration_since(modified)
                .map(|age| age > Duration::from_secs(SESSION_TTL_SECS))
                .unwrap_or(true);
            if expired {
                let _ = fs::remove_file(&path);
                return Ok(None);
            }
        }
    }
    Ok(read_runtime_cache(&path))
}

fn load_recent_session_place<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
    max_age: Duration,
) -> Result<Option<SessionPlace>> {
    let Some(path) = session_path(root, adb)? else {
        return Ok(None);
    };
    if !path.exists() {
        return Ok(None);
    }
    let fresh = fs::metadata(&path)
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| SystemTime::now().duration_since(modified).ok())
        .map(|age| age <= max_age)
        .unwrap_or(false);
    if !fresh {
        return Ok(None);
    }
    Ok(read_runtime_cache(&path))
}

fn graph_place_for_session(graph: &Graph, session: &SessionPlace) -> Option<Place> {
    graph
        .places
        .get(&session.place.id)
        .cloned()
        .filter(|place| {
            place.baseline.identity_hash == session.baseline.identity_hash
                || place
                    .variants
                    .iter()
                    .any(|variant| variant.identity_hash == session.baseline.identity_hash)
        })
}

fn clear_session_place<DR: CommandRunner>(root: &Path, adb: &mut Adb<DR>) -> Result<()> {
    let Some(path) = session_path(root, adb)? else {
        return Ok(());
    };
    if path.exists() {
        fs::remove_file(path)?;
    }
    Ok(())
}

fn save_pending<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
    pending: &PendingTransition,
) -> Result<()> {
    anyhow::ensure!(
        pending.recipe.len() <= 32,
        "pending transition exceeds 32 actions; orient and resume from a verified place"
    );
    let Some(path) = pending_path(root, adb)? else {
        return Ok(());
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
        restrict_cache_dir_permissions(parent);
    }
    let value = json!({
        "source": pending.source,
        "recipe": pending.recipe,
        "destination": pending.destination,
        "intent": pending.intent
    });
    minimap_repo::write_json(&path, &value)?;
    restrict_cache_file_permissions(&path);
    Ok(())
}

fn load_pending<DR: CommandRunner>(
    root: &Path,
    adb: &mut Adb<DR>,
) -> Result<Option<PendingTransition>> {
    let Some(path) = pending_path(root, adb)? else {
        return Ok(None);
    };
    if !path.exists() {
        return Ok(None);
    }
    if let Ok(metadata) = fs::metadata(&path) {
        if let Ok(modified) = metadata.modified() {
            let expired = SystemTime::now()
                .duration_since(modified)
                .map(|age| age > Duration::from_secs(PENDING_TTL_SECS))
                .unwrap_or(true);
            if expired {
                let _ = fs::remove_file(&path);
                return Ok(None);
            }
        }
    }
    Ok(read_runtime_cache(&path))
}

fn read_runtime_cache<T: serde::de::DeserializeOwned>(path: &Path) -> Option<T> {
    let value = fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok());
    if value.is_none() {
        let _ = fs::remove_file(path);
    }
    value
}

fn clear_pending<DR: CommandRunner>(root: &Path, adb: &mut Adb<DR>) -> Result<()> {
    let Some(path) = pending_path(root, adb)? else {
        return Ok(());
    };
    if path.exists() {
        fs::remove_file(path)?;
    }
    Ok(())
}

/// On Unix, restrict the minimap cache directory tree to the owner (0o700) so
/// cache files on a shared /tmp are not world-readable. Best-effort: failures to
/// adjust permissions are ignored. No-op on non-Unix platforms.
#[cfg(unix)]
fn restrict_cache_dir_permissions(dir: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let temp_root = std::env::temp_dir().join("minimap");
    let mut current = Some(dir);
    while let Some(path) = current {
        if !path.starts_with(&temp_root) {
            break;
        }
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o700));
        if path == temp_root.as_path() {
            break;
        }
        current = path.parent();
    }
}

#[cfg(not(unix))]
fn restrict_cache_dir_permissions(_dir: &Path) {}

/// On Unix, restrict a written cache file to the owner (0o600). Best-effort; a
/// no-op on non-Unix platforms.
#[cfg(unix)]
fn restrict_cache_file_permissions(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_cache_file_permissions(_path: &Path) {}

fn print_json(value: &Value, pretty: bool) {
    if pretty {
        print!("{}", canonical_json(value));
    } else {
        println!("{}", serde_json::to_string(value).expect("result JSON"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use minimap_android::CommandResult;
    use minimap_schemas::{Fingerprint, StaticText};
    use std::collections::BTreeMap;

    /// Minimal fake `CommandRunner`. When `serial` is `Some`, `get-serialno`
    /// succeeds with that value; when `None`, every command fails so `serial()`
    /// errors and the cache is skipped.
    struct FakeRunner {
        serial: Option<String>,
    }

    impl CommandRunner for FakeRunner {
        fn run(&mut self, args: &[String], _env: &[(String, String)]) -> Result<CommandResult> {
            if args.iter().any(|arg| arg == "get-serialno") {
                if let Some(serial) = &self.serial {
                    return Ok(CommandResult {
                        args: args.to_vec(),
                        status: 0,
                        stdout: format!("{serial}\n"),
                        stderr: String::new(),
                    });
                }
            }
            Ok(CommandResult {
                args: args.to_vec(),
                status: 1,
                stdout: String::new(),
                stderr: "no device".to_string(),
            })
        }
    }

    fn fake_adb(serial: Option<&str>) -> Adb<FakeRunner> {
        Adb::new(
            FakeRunner {
                serial: serial.map(str::to_string),
            },
            None,
        )
    }

    fn endpoint(slug: &str) -> EdgeEndpoint {
        EdgeEndpoint {
            id: format!("place_{slug}"),
            slug: slug.to_string(),
        }
    }

    fn baseline(hash: &str) -> PlaceBaseline {
        PlaceBaseline {
            identity_hash: format!("sha256:{hash}"),
            fingerprint: Fingerprint {
                selectors: Vec::new(),
                static_text: vec![StaticText {
                    value: hash.to_string(),
                }],
                roles: BTreeMap::new(),
            },
        }
    }

    fn tap_step(value: &str) -> ActionStep {
        ActionStep {
            kind: "tap".to_string(),
            selector: Some(Selector {
                kind: "text".to_string(),
                value: value.to_string(),
            }),
            point: None,
            viewport: None,
            direction: None,
        }
    }

    // FIX 1: edge_id must never panic regardless of selector/slug length and must
    // yield a valid sanitized id.
    #[test]
    fn edge_id_handles_long_selectors_without_panicking() {
        let long_value = "x".repeat(200);
        let from = endpoint("home");
        let to = endpoint("search");
        let recipe = vec![tap_step(&long_value)];
        let id = edge_id(&from, &to, &recipe);
        assert!(
            id.starts_with("edge_"),
            "id should keep readable prefix: {id}"
        );
        assert!(
            id.chars()
                .all(|ch| ch.is_ascii_alphanumeric() || ch == '-' || ch == '_'),
            "id must be a sanitized slug: {id}"
        );
        // Deterministic for the same recipe.
        assert_eq!(id, edge_id(&from, &to, &recipe));
    }

    #[test]
    fn edge_id_long_unicode_selector_does_not_panic() {
        // Multi-byte chars near the truncation boundary must not split mid-UTF8.
        let unicode_value = "é".repeat(120);
        let from = endpoint("éhome");
        let to = endpoint("search");
        let recipe = vec![tap_step(&unicode_value)];
        let id = edge_id(&from, &to, &recipe);
        assert!(!id.is_empty());
    }

    // FIX 4: with no resolvable serial, the cache path is None (no shared bucket),
    // so loads return None and saves are skipped (no file written).
    #[test]
    fn no_serial_skips_cache_entirely() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let mut adb = fake_adb(None);

        assert!(pending_path(root, &mut adb).unwrap().is_none());
        assert!(session_path(root, &mut adb).unwrap().is_none());

        let pending = PendingTransition {
            source: endpoint("home"),
            recipe: vec![tap_step("SEARCH")],
            destination: baseline("dest"),
            intent: None,
        };
        save_pending(root, &mut adb, &pending).unwrap();
        assert!(load_pending(root, &mut adb).unwrap().is_none());

        // Nothing should have been written to the shared minimap temp tree on
        // behalf of this serial-less invocation.
        let path = pending_path(root, &mut adb).unwrap();
        assert!(path.is_none());
    }

    // FIX 2: a pending file older than the TTL is ignored and removed.
    #[test]
    fn stale_pending_is_ignored_and_removed() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let mut adb = fake_adb(Some("fix2-serial"));

        let pending = PendingTransition {
            source: endpoint("home"),
            recipe: vec![tap_step("SEARCH")],
            destination: baseline("dest"),
            intent: None,
        };
        save_pending(root, &mut adb, &pending).unwrap();
        let path = pending_path(root, &mut adb).unwrap().unwrap();
        assert!(path.exists());

        // Backdate the file well beyond the TTL.
        let old = SystemTime::now() - Duration::from_secs(PENDING_TTL_SECS + 60);
        filetime_set(&path, old);

        assert!(load_pending(root, &mut adb).unwrap().is_none());
        assert!(!path.exists(), "stale pending should be removed");
    }

    // FIX 3: a pending whose source.id is absent from the graph must NOT be
    // committed as a dangling edge when orienting a new destination place.
    #[test]
    fn orient_does_not_commit_edge_from_missing_source() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        run_init(
            root,
            InitOptions {
                dry_run: false,
                agents: "codex",
                force: false,
                refresh_skills: false,
                no_skills: true,
            },
        )
        .unwrap();
        let mut adb = fake_adb(Some("fix3-serial"));

        // A pending transition whose source place is NOT in the graph.
        let dest_layout = json!({
            "class": "Column",
            "children": [
                {"class": "Text", "text": "Brand New Destination Screen Title"},
                {"class": "Text", "text": "A second distinctive line of body copy here"}
            ]
        });
        let dest_baseline = fingerprint_layout(&dest_layout);
        let pending = PendingTransition {
            source: endpoint("ghost-source"),
            recipe: vec![tap_step("SEARCH")],
            destination: dest_baseline.clone(),
            intent: Some("open ghost".to_string()),
        };
        save_pending(root, &mut adb, &pending).unwrap();

        // Orient on the destination layout with a label -> a new place is created,
        // but the edge must NOT be committed because the source is missing.
        let orientation =
            orient_layout(root, &dest_layout, Some("newdest"), false, true, &mut adb).unwrap();
        assert_eq!(orientation.status, "ok");

        let graph = load_graph(root).unwrap();
        assert!(
            graph.edges.is_empty(),
            "no edge should be committed from a missing source: {:?}",
            graph.edges
        );
    }

    // FIX 7 (Unix only): written cache files are mode 0o600.
    #[cfg(unix)]
    #[test]
    fn cache_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let mut adb = fake_adb(Some("fix7-serial"));

        let pending = PendingTransition {
            source: endpoint("home"),
            recipe: vec![tap_step("SEARCH")],
            destination: baseline("dest"),
            intent: None,
        };
        save_pending(root, &mut adb, &pending).unwrap();
        let path = pending_path(root, &mut adb).unwrap().unwrap();
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "cache file should be owner-only");
    }

    /// Set a file's mtime to `when` without an extra crate dependency.
    /// `set_accessed`/`set_modified` live on `FileTimes` itself (cross-platform).
    fn filetime_set(path: &Path, when: SystemTime) {
        let times = fs::FileTimes::new().set_accessed(when).set_modified(when);
        let file = fs::OpenOptions::new().write(true).open(path).unwrap();
        file.set_times(times).unwrap();
    }
}
