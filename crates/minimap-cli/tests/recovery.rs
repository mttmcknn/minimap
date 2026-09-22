#![cfg(unix)]

use assert_cmd::Command;
use serde_json::{json, Value};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use tempfile::TempDir;

struct App {
    temp: TempDir,
    bin: PathBuf,
}

impl App {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let bin = temp.path().join("bin");
        for name in ["bin", "layouts", "routes", "runtime"] {
            fs::create_dir(temp.path().join(name)).unwrap();
        }
        // A deadline can expire before the first subprocess starts; an empty
        // trace still proves that no input was sent in that case.
        fs::write(temp.path().join("calls"), "").unwrap();
        executable(
            &bin.join("android"),
            r#"#!/bin/sh
printf 'android %s\n' "$*" >> "$SIM_ROOT/calls"
if [ "$1" = layout ]; then cat "$SIM_ROOT/current.json"; else printf 'test-android\n'; fi
"#,
        );
        executable(
            &bin.join("adb"),
            r#"#!/bin/sh
printf 'adb %s\n' "$*" >> "$SIM_ROOT/calls"
if [ "$1" = -s ]; then shift 2; fi
if [ -n "$SIM_ADB_DELAY" ] && [ "$2" = dumpsys ]; then sleep "$SIM_ADB_DELAY"; fi
case "$1:$2:$3" in
  get-serialno:*) printf 'test-device\n';;
  get-state:*) printf 'device\n';;
  devices:*) printf 'List of devices attached\ntest-device\tdevice\n';;
  shell:wm:size) if [ -f "$SIM_ROOT/no-viewport" ]; then exit 1; fi; printf 'Physical size: 1080x2400\n';;
  shell:pidof:*) if [ -f "$SIM_ROOT/pid" ]; then cat "$SIM_ROOT/pid"; else printf '4242\n'; fi;;
  push:*) test -f "$SIM_ROOT/fast.json";;
  shell:CLASSPATH=*:app_process) cat "$SIM_ROOT/fast.json";;
  shell:dumpsys:*) if [ -f "$SIM_ROOT/focus" ]; then cat "$SIM_ROOT/focus"; else printf 'mCurrentFocus=Window{1 u0 com.example.app/.MainActivity}\nversionCode=1\nlastUpdateTime=fixture\n'; fi;;
  shell:input:*)
    source=$(cat "$SIM_ROOT/current-name")
    route="$SIM_ROOT/routes/${source}__${3}_${4}_${5}"
    if [ -f "$route" ]; then
      destination=$(cat "$route")
      if [ "$destination" = fail ]; then exit 1; fi
      cp "$SIM_ROOT/layouts/$destination.json" "$SIM_ROOT/current.json"
      printf '%s' "$destination" > "$SIM_ROOT/current-name"
    fi
    ;;
  *) exit 2;;
esac
"#,
        );
        let app = Self { temp, bin };
        app.command(&["init", "--no-skills", "--package", "com.example.app"]);
        app
    }

    fn cmd(&self, args: &[&str]) -> Command {
        let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("minimap"));
        let path = format!("{}:{}", self.bin.display(), std::env::var("PATH").unwrap());
        cmd.current_dir(self.temp.path())
            .env("PATH", path)
            .env("SIM_ROOT", self.temp.path())
            .env("HOME", self.temp.path())
            .env("TMPDIR", self.temp.path().join("runtime"))
            .env("MINIMAP_ACTION_SETTLE_MS", "0")
            .args(args);
        cmd
    }

    fn command(&self, args: &[&str]) -> Value {
        let output = self
            .cmd(args)
            .assert()
            .success()
            .get_output()
            .stdout
            .clone();
        serde_json::from_slice(&output).unwrap()
    }

    fn layout(&self, name: &str, value: Value) {
        fs::write(
            self.temp.path().join(format!("layouts/{name}.json")),
            value.to_string(),
        )
        .unwrap();
    }

    fn at(&self, name: &str) {
        fs::copy(
            self.temp.path().join(format!("layouts/{name}.json")),
            self.temp.path().join("current.json"),
        )
        .unwrap();
        fs::write(self.temp.path().join("current-name"), name).unwrap();
    }

    fn route(&self, source: &str, action: &str, destination: &str) {
        fs::write(
            self.temp.path().join(format!("routes/{source}__{action}")),
            destination,
        )
        .unwrap();
    }

    fn current(&self) -> String {
        fs::read_to_string(self.temp.path().join("current-name")).unwrap()
    }
}

fn executable(path: &Path, body: &str) {
    fs::write(path, body).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

fn screen(name: &str, controls: &[(&str, i64)]) -> Value {
    let mut nodes: Vec<Value> = (0..8)
        .map(|i| json!({"testTag": format!("{name}-{i}")}))
        .collect();
    nodes.extend(
        controls
            .iter()
            .map(|(text, x)| json!({"text": text, "center": format!("[{x},20]")})),
    );
    json!(nodes)
}

#[test]
fn goal_anchors_are_checked_even_when_already_at_the_known_screen() {
    let app = App::new();
    app.layout("profile", screen("profile", &[("Account Alice", 10)]));
    app.at("profile");
    app.command(&["whereami", "--label", "profile"]);
    let failed = app
        .cmd(&["go", "profile", "--expect", "text=Account Bob"])
        .assert()
        .code(2)
        .get_output()
        .stdout
        .clone();
    let failed: Value = serde_json::from_slice(&failed).unwrap();
    assert_eq!(failed["status"], "goal_mismatch");
    assert_eq!(failed["data"]["recovery"]["outcome"], "needs_agent");
    let token = failed["data"]["recovery"]["token"].as_str().unwrap();
    let retry = app
        .cmd(&[
            "go",
            "profile",
            "--expect",
            "text=Account Alice",
            "--recovery",
            token,
        ])
        .assert()
        .code(2)
        .get_output()
        .stdout
        .clone();
    assert_eq!(
        serde_json::from_slice::<Value>(&retry).unwrap()["status"],
        "goal_mismatch"
    );
    let passed = app.command(&["go", "profile", "--expect", "text=Account Alice"]);
    assert_eq!(passed["data"]["verification"]["passed"], true);
    assert!(!passed.to_string().contains("Account Alice"));
}

#[test]
fn renamed_destinations_keep_current_exit_labels_and_wait_for_requested_content() {
    let app = App::new();
    app.layout("home", screen("home", &[("Open", 10)]));
    app.layout(
        "profile",
        screen("profile", &[("Account", 20), ("Options", 30)]),
    );
    app.layout(
        "ready",
        screen(
            "profile",
            &[("Account", 20), ("Options", 30), ("Ready", 40)],
        ),
    );
    app.route("home", "tap_10_20", "profile");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    let learned = app.command(&["tap", "--selector", "text=Open", "--label", "profile"]);
    app.command(&["whereami", "--label", "account"]);
    app.at("home");
    let orientation = app.command(&["whereami", "--fresh"]);
    // The first destination frame has the right screen identity, but the
    // requested content arrives on the next observation.
    executable(
        &app.bin.join("android"),
        r#"#!/bin/sh
cat "$SIM_ROOT/current.json"
if [ "$(cat "$SIM_ROOT/current-name")" = profile ]; then
  cp "$SIM_ROOT/layouts/ready.json" "$SIM_ROOT/current.json"
fi
"#,
    );
    let result = app.command(&["go", "account", "--expect", "text=Ready"]);
    assert_eq!(result["data"]["verification"]["passed"], true);
    assert_eq!(orientation["known_exits"][0]["to"], "account");
    app.at("home");
    let repeated = app.command(&["tap", "--selector", "text=Open", "--label", "account"]);
    assert_eq!(repeated["data"]["edge"], learned["data"]["edge"]);
    assert_eq!(
        minimap_repo::load_graph(app.temp.path())
            .unwrap()
            .edges
            .len(),
        1
    );
}

#[test]
fn recovery_budget_is_debited_before_input_and_cannot_be_reset_by_retrying_go() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.layout("next", screen("next", &[]));
    app.layout("wrong", screen("wrong", &[]));
    app.route("home", "tap_10_20", "next");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Next", "--label", "next"]);
    app.route("home", "tap_10_20", "wrong");
    app.at("home");
    let result = app
        .cmd(&["go", "next", "--max-actions", "1"])
        .assert()
        .code(5)
        .get_output()
        .stdout
        .clone();
    let result: Value = serde_json::from_slice(&result).unwrap();
    assert_eq!(result["data"]["recovery"]["remaining_actions"], 0);
    let token = result["data"]["recovery"]["token"].as_str().unwrap();
    app.at("home");
    fs::write(app.temp.path().join("calls"), "").unwrap();
    let result = app
        .cmd(&["tap", "--selector", "text=Next", "--recovery", token])
        .assert()
        .code(2)
        .get_output()
        .stdout
        .clone();
    assert_eq!(
        serde_json::from_slice::<Value>(&result).unwrap()["status"],
        "recovery_exhausted"
    );
    app.cmd(&["go", "next", "--max-actions", "256", "--recovery", token])
        .assert()
        .failure();
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
}

#[test]
fn recovery_deadline_includes_time_spent_between_agent_commands() {
    let app = App::new();
    app.layout("home", screen("home", &[]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    let result = app
        .cmd(&["go", "missing", "--recovery-seconds", "1"])
        .assert()
        .code(5)
        .get_output()
        .stdout
        .clone();
    let result: Value = serde_json::from_slice(&result).unwrap();
    let token = result["data"]["recovery"]["token"].as_str().unwrap();
    std::thread::sleep(std::time::Duration::from_millis(1100));
    let result = app
        .cmd(&["layout", "--recovery", token])
        .assert()
        .code(2)
        .get_output()
        .stdout
        .clone();
    assert_eq!(
        serde_json::from_slice::<Value>(&result).unwrap()["status"],
        "recovery_exhausted"
    );
}

#[test]
fn malformed_later_actions_are_rejected_before_the_first_input() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.layout("next", screen("next", &[]));
    app.route("home", "tap_10_20", "next");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Next", "--label", "next"]);
    let graph = minimap_repo::load_graph(app.temp.path()).unwrap();
    let edge = graph.edges.values().next().unwrap();
    let path = minimap_repo::edge_path(app.temp.path(), &edge.id);
    let mut value = serde_json::to_value(edge).unwrap();
    value["recipe"]
        .as_array_mut()
        .unwrap()
        .push(json!({"kind":"scroll", "direction":"diagonal"}));
    fs::write(path, value.to_string()).unwrap();
    app.at("home");
    fs::write(app.temp.path().join("calls"), "").unwrap();
    app.cmd(&["doctor", "--repo-only"]).assert().code(7);
    app.cmd(&["go", "next"]).assert().code(7);
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
}

#[test]
fn private_labels_and_action_values_are_rejected_before_input() {
    let app = App::new();
    app.layout("home", screen("home", &[("private@example.com", 10)]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    fs::write(app.temp.path().join("calls"), "").unwrap();
    app.cmd(&["whereami", "--label", "private@example.com"])
        .assert()
        .code(7);
    app.cmd(&["tap", "--selector", "text=private@example.com"])
        .assert()
        .code(7);
    app.cmd(&["tap", "--point", "10,20", "--reason", "bearer veryprivate"])
        .assert()
        .code(7);
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
}

#[test]
fn scroll_refuses_to_guess_coordinates_when_the_viewport_is_unavailable() {
    let app = App::new();
    app.layout("home", screen("home", &[]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    fs::write(app.temp.path().join("no-viewport"), "").unwrap();
    fs::write(app.temp.path().join("calls"), "").unwrap();
    app.cmd(&["scroll"]).assert().code(6);
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
    assert!(minimap_repo::load_graph(app.temp.path())
        .unwrap()
        .edges
        .is_empty());
}

#[test]
fn navigation_deadline_also_bounds_device_preflight() {
    let app = App::new();
    app.layout("home", screen("home", &[]));
    app.at("home");
    let start = std::time::Instant::now();
    app.cmd(&["go", "home", "--recovery-seconds", "1"])
        .env("SIM_ADB_DELAY", "2")
        .assert()
        .code(6);
    assert!(start.elapsed() < std::time::Duration::from_secs(3));
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
}

#[test]
fn concurrent_agents_cannot_interleave_device_actions() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.layout("next", screen("next", &[]));
    app.route("home", "tap_10_20", "next");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    fs::write(app.temp.path().join("calls"), "").unwrap();
    let root = app.temp.path().to_path_buf();
    let bin = app.bin.clone();
    let first = std::thread::spawn(move || {
        std::process::Command::new(assert_cmd::cargo::cargo_bin!("minimap"))
            .current_dir(&root)
            .env("HOME", &root)
            .env("TMPDIR", root.join("runtime"))
            .env("SIM_ROOT", &root)
            .env("SIM_ADB_DELAY", "1")
            .env_remove("ANDROID_SERIAL")
            .env(
                "PATH",
                format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
            )
            .args(["whereami", "--fresh"])
            .output()
            .unwrap()
    });
    let start = std::time::Instant::now();
    while !fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("dumpsys")
    {
        assert!(start.elapsed() < std::time::Duration::from_secs(3));
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let second = app
        .cmd(&["tap", "--selector", "text=Next", "--label", "next"])
        .assert()
        .code(6)
        .get_output()
        .stdout
        .clone();
    assert!(String::from_utf8(second).unwrap().contains("busy"));
    assert!(first.join().unwrap().status.success());
    assert_eq!(app.current(), "home");
    app.command(&["tap", "--selector", "text=Next", "--label", "next"]);
    assert_eq!(app.current(), "next");
}

#[test]
fn agent_confirmation_repairs_changed_appearance_but_rejects_another_known_place() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.layout("next", screen("next", &[]));
    app.layout("redesigned", screen("redesigned", &[]));
    app.route("home", "tap_10_20", "next");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Next", "--label", "next"]);
    let original = minimap_repo::load_graph(app.temp.path()).unwrap().places["place_next"]
        .baseline
        .clone();
    app.at("home");
    app.cmd(&["whereami", "--confirm-place", "place_next"])
        .assert()
        .code(7);
    app.route("home", "tap_10_20", "redesigned");
    app.cmd(&["tap", "--selector", "text=Next"])
        .assert()
        .code(5);
    app.command(&["whereami", "--confirm-place", "place_next"]);
    let graph = minimap_repo::load_graph(app.temp.path()).unwrap();
    assert_eq!(graph.places["place_next"].baseline, original);
    assert_eq!(graph.places["place_next"].variants.len(), 1);
    app.at("home");
    app.command(&["go", "next"]);
    assert_eq!(app.current(), "redesigned");
}

#[test]
fn invalid_layout_and_corrupt_runtime_cache_cannot_be_learned_as_places() {
    let app = App::new();
    app.layout("home", screen("home", &[]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    fn corrupt(dir: &Path) {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                corrupt(&path);
            } else if path.extension().is_some_and(|ext| ext == "json") {
                fs::write(path, "{partial").unwrap();
            }
        }
    }
    corrupt(&app.temp.path().join("runtime"));
    app.command(&["whereami"]);
    fs::write(
        app.temp.path().join("current.json"),
        "launcher log, not a layout",
    )
    .unwrap();
    app.cmd(&["whereami", "--label", "bad"]).assert().code(6);
    assert_eq!(
        minimap_repo::load_graph(app.temp.path())
            .unwrap()
            .places
            .len(),
        1
    );
    app.cmd(&["doctor", "--repo-only"])
        .env("PATH", "")
        .assert()
        .success();
}

#[test]
fn wrong_foreground_app_cannot_receive_input_or_change_the_graph() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    fs::write(app.temp.path().join("calls"), "").unwrap();
    fs::write(
        app.temp.path().join("focus"),
        "mCurrentFocus=Window{1 u0 com.other.app/.MainActivity}\n",
    )
    .unwrap();
    app.cmd(&["tap", "--selector", "text=Next"])
        .assert()
        .code(6);
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
    assert_eq!(
        fs::read_dir(app.temp.path().join(".minimap/graph/edges"))
            .unwrap()
            .count(),
        0
    );
}

#[test]
fn app_restart_invalidates_cached_observation() {
    let app = App::new();
    app.layout("home", screen("home", &[]));
    app.layout("search", screen("search", &[]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.at("search");
    fs::write(app.temp.path().join("pid"), "9001").unwrap();
    let result = app
        .cmd(&["whereami"])
        .assert()
        .code(5)
        .get_output()
        .stdout
        .clone();
    let result: Value = serde_json::from_slice(&result).unwrap();
    assert_eq!(result["status"], "unknown");
}

#[test]
fn module_invocation_finds_graph_and_reused_old_label_keeps_both_places() {
    let app = App::new();
    app.layout("home", screen("home", &[]));
    app.layout("other", screen("other", &[]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["whereami", "--label", "renamed"]);
    app.at("other");
    app.command(&["whereami", "--label", "home"]);
    let graph = minimap_repo::load_graph(app.temp.path()).unwrap();
    assert_eq!(graph.places.len(), 2);
    let module = app.temp.path().join("app/src");
    fs::create_dir_all(&module).unwrap();
    let result = app
        .cmd(&["whereami", "--fresh"])
        .current_dir(module)
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let result: Value = serde_json::from_slice(&result).unwrap();
    assert_eq!(result["place"]["slug"], "home");
}

#[test]
fn duplicate_place_evidence_does_not_act_or_learn() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    let graph = minimap_repo::load_graph(app.temp.path()).unwrap();
    let mut duplicate = graph.places.values().next().unwrap().clone();
    duplicate.id = "duplicate".into();
    duplicate.slug = "duplicate".into();
    minimap_repo::commit_place(app.temp.path(), &duplicate).unwrap();
    fs::write(app.temp.path().join("calls"), "").unwrap();
    app.cmd(&["whereami", "--label", "new", "--fresh"])
        .assert()
        .code(2);
    assert_eq!(
        minimap_repo::load_graph(app.temp.path())
            .unwrap()
            .places
            .len(),
        2
    );
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
}

#[test]
fn fast_layout_drift_is_confirmed_before_adopting_new_screen_evidence() {
    let app = App::new();
    app.layout("home", screen("home", &[("Open", 10)]));
    app.layout("target", screen("target", &[("Done", 30)]));
    app.route("home", "tap_10_20", "target");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Open", "--label", "target"]);
    let before = minimap_repo::load_graph(app.temp.path()).unwrap();
    // Similar enough to the saved Home screen, but the new observation path
    // presents extra evidence and a different tap point. Confirm with Android
    // CLI before using it or persisting an alternate fingerprint.
    fs::write(
        app.temp.path().join("fast.json"),
        screen("home", &[("Open", 90), ("New header", 60)]).to_string(),
    )
    .unwrap();
    app.at("home");
    fs::write(app.temp.path().join("calls"), "").unwrap();
    let result = app.command(&["go", "target", "--expect", "text=Done"]);
    assert_eq!(result["data"]["changed_graph"], false);
    assert_eq!(app.current(), "target");
    let after = minimap_repo::load_graph(app.temp.path()).unwrap();
    for (id, place) in before.places {
        assert_eq!(
            serde_json::to_value(place).unwrap(),
            serde_json::to_value(&after.places[&id]).unwrap()
        );
    }
    let calls = fs::read_to_string(app.temp.path().join("calls")).unwrap();
    assert_eq!(calls.matches("dev.minimap.MinimapLayout").count(), 1);
    assert!(!calls.contains("input tap 90"));
}

#[test]
fn selector_recipe_waits_for_ready_controls_without_requiring_a_viewport() {
    let app = App::new();
    app.layout("home", screen("home", &[("Open", 10)]));
    app.layout("target", screen("target", &[("Done", 30)]));
    app.route("home", "tap_10_20", "target");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Open", "--label", "target"]);

    let graph = minimap_repo::load_graph(app.temp.path()).unwrap();
    let mut edge = graph.edges.values().next().unwrap().clone();
    edge.recipe.push(
        serde_json::from_value(json!({
            "kind":"tap", "selector":{"kind":"text", "value":"Continue"}
        }))
        .unwrap(),
    );
    minimap_repo::commit_edge(app.temp.path(), &edge).unwrap();
    app.layout("loading", screen("menu", &[("Loading", 20)]));
    app.layout("moving", screen("menu", &[("Continue", 20)]));
    app.layout("menu", screen("menu", &[("Continue", 30)]));
    app.route("home", "tap_10_20", "loading");
    app.route("menu", "tap_30_20", "target");
    executable(
        &app.bin.join("android"),
        r#"#!/bin/sh
printf 'android %s\n' "$*" >> "$SIM_ROOT/calls"
cat "$SIM_ROOT/current.json"
case "$(cat "$SIM_ROOT/current-name")" in
  loading) next=moving;;
  moving) next=menu;;
  *) exit 0;;
esac
cp "$SIM_ROOT/layouts/$next.json" "$SIM_ROOT/current.json"
printf '%s' "$next" > "$SIM_ROOT/current-name"
"#,
    );
    app.at("home");
    fs::write(app.temp.path().join("no-viewport"), "").unwrap();
    fs::write(app.temp.path().join("calls"), "").unwrap();
    let result = app.command(&["go", "target", "--expect", "text=Done"]);
    assert_eq!(result["data"]["verification"]["passed"], true);
    assert_eq!(app.current(), "target");
    let calls = fs::read_to_string(app.temp.path().join("calls")).unwrap();
    assert_eq!(calls.matches("shell input tap").count(), 2);
    assert_eq!(calls.matches("android layout").count(), 7);
    assert!(!calls.contains("input tap 20 20"));
    assert!(!calls.contains("wm size"));
}

#[test]
fn action_budget_prevents_partial_execution_of_a_long_recipe() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.layout("next", screen("next", &[]));
    app.route("home", "tap_10_20", "next");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Next", "--label", "next"]);
    let graph = minimap_repo::load_graph(app.temp.path()).unwrap();
    let mut edge = graph.edges.values().next().unwrap().clone();
    edge.recipe.push(edge.recipe[0].clone());
    minimap_repo::commit_edge(app.temp.path(), &edge).unwrap();
    app.at("home");
    fs::write(app.temp.path().join("calls"), "").unwrap();
    app.cmd(&["go", "next", "--max-actions", "1"])
        .assert()
        .code(2);
    assert_eq!(app.current(), "home");
    assert!(!fs::read_to_string(app.temp.path().join("calls"))
        .unwrap()
        .contains("shell input"));
}

#[test]
fn external_navigation_reorients_instead_of_claiming_zero_edge_success() {
    let app = App::new();
    app.layout("home", screen("home", &[("Search", 10)]));
    app.layout("search", screen("search", &[("Home", 20)]));
    app.route("home", "tap_10_20", "search");
    app.route("search", "tap_20_20", "home");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Search", "--label", "search"]);
    app.command(&["tap", "--selector", "text=Home", "--label", "home"]);
    app.at("search");
    let result = app.command(&["go", "home"]);
    assert_eq!(app.current(), "home");
    assert_eq!(result["data"]["start_source"], "layout");
    assert_eq!(
        result["data"]["executed_steps"].as_array().unwrap().len(),
        1
    );
}

#[test]
fn broken_selector_recovers_through_another_verified_edge() {
    let app = App::new();
    app.layout("home", screen("home", &[("A old", 10), ("B new", 20)]));
    app.layout("search", screen("search", &[]));
    app.route("home", "tap_10_20", "search");
    app.route("home", "tap_20_20", "search");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=A old", "--label", "search"]);
    app.at("home");
    app.command(&["tap", "--selector", "text=B new", "--label", "search"]);
    app.layout("home", screen("home", &[("B new", 20)]));
    app.at("home");
    let result = app.command(&["go", "search"]);
    assert_eq!(app.current(), "search");
    assert_eq!(result["data"]["recovery"]["outcome"], "recovered");
    assert_eq!(
        result["data"]["recovery"]["failures"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn unexpected_known_destination_replans_without_learning_the_wrong_edge() {
    let app = App::new();
    app.layout("home", screen("home", &[("Search", 10)]));
    app.layout("profile", screen("profile", &[("Search", 20)]));
    app.layout("search", screen("search", &[]));
    app.route("home", "tap_10_20", "search");
    app.route("profile", "tap_20_20", "search");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["tap", "--selector", "text=Search", "--label", "search"]);
    app.at("profile");
    app.command(&["whereami", "--label", "profile"]);
    app.command(&["tap", "--selector", "text=Search", "--label", "search"]);
    app.route("home", "tap_10_20", "profile");
    app.at("home");
    let result = app.command(&["go", "search"]);
    assert_eq!(app.current(), "search");
    assert_eq!(result["data"]["recovery"]["outcome"], "recovered");
    let edges = fs::read_dir(app.temp.path().join(".minimap/graph/edges"))
        .unwrap()
        .count();
    assert_eq!(
        edges, 2,
        "a misnavigation must not become a new proven edge"
    );
}

#[test]
fn unknown_start_returns_an_agent_recovery_handoff() {
    let app = App::new();
    app.layout("unknown", screen("unknown", &[("Account", 10)]));
    app.at("unknown");
    let output = app
        .cmd(&["go", "settings"])
        .assert()
        .code(5)
        .get_output()
        .stdout
        .clone();
    let result: Value = serde_json::from_slice(&output).unwrap();
    assert_eq!(result["data"]["recovery"]["outcome"], "needs_agent");
    assert_eq!(result["data"]["recovery"]["target"], "settings");
    assert_eq!(result["data"]["changed_graph"], false);
}

#[test]
fn learning_preserves_repeated_scrolls_and_same_place_taps() {
    let app = App::new();
    app.layout("home", screen("home", &[("Expand", 30), ("Search", 10)]));
    app.layout("search", screen("search", &[]));
    app.route("home", "tap_10_20", "search");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    app.command(&["scroll", "--direction", "down"]);
    app.command(&["scroll", "--direction", "down"]);
    app.command(&["tap", "--selector", "text=Expand"]);
    app.command(&["tap", "--selector", "text=Search", "--label", "search"]);
    let edge_path = fs::read_dir(app.temp.path().join(".minimap/graph/edges"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let edge: Value = serde_json::from_slice(&fs::read(edge_path).unwrap()).unwrap();
    assert_eq!(edge["recipe"].as_array().unwrap().len(), 4);
    assert_eq!(edge["recipe"][0]["kind"], "scroll");
    assert_eq!(edge["recipe"][1]["kind"], "scroll");
    assert_eq!(edge["recipe"][2]["selector"]["value"], "Expand");
}

#[test]
fn different_recipes_with_the_same_first_action_remain_distinct() {
    let app = App::new();
    app.layout("home", screen("home", &[("Search", 10)]));
    app.layout("search", screen("search", &[]));
    app.route("home", "tap_10_20", "search");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    for scrolls in [1, 2] {
        app.at("home");
        for _ in 0..scrolls {
            app.command(&["scroll"]);
        }
        app.command(&["tap", "--selector", "text=Search", "--label", "search"]);
    }
    let edges = fs::read_dir(app.temp.path().join(".minimap/graph/edges"))
        .unwrap()
        .count();
    assert_eq!(edges, 2);
}

#[test]
fn superseding_requires_replay_from_the_original_source_to_the_same_goal() {
    let app = App::new();
    app.layout("home", screen("home", &[("A old", 10), ("B new", 20)]));
    app.layout("search", screen("search", &[]));
    app.route("home", "tap_10_20", "search");
    app.route("home", "tap_20_20", "search");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    let old = app.command(&["tap", "--selector", "text=A old", "--label", "search"]);
    let old_id = old["data"]["edge"].as_str().unwrap();
    app.at("home");
    app.command(&["tap", "--selector", "text=B new", "--label", "search"]);
    app.cmd(&["go", "search", "--supersede", old_id])
        .assert()
        .code(7);
    assert!(app
        .temp
        .path()
        .join(format!(".minimap/graph/edges/{old_id}.json"))
        .exists());
    app.at("home");
    let rejected = app
        .cmd(&[
            "go",
            "search",
            "--supersede",
            old_id,
            "--expect",
            "text=Missing requested state",
        ])
        .assert()
        .code(2)
        .get_output()
        .stdout
        .clone();
    assert_eq!(
        serde_json::from_slice::<Value>(&rejected).unwrap()["status"],
        "goal_mismatch"
    );
    assert!(
        minimap_repo::load_graph(app.temp.path()).unwrap().edges[old_id]
            .superseded_by
            .is_empty()
    );
    app.at("home");
    let result = app.command(&["go", "search", "--supersede", old_id]);
    assert_eq!(app.current(), "search");
    assert_eq!(result["data"]["changed_graph"], true);
    assert!(app
        .temp
        .path()
        .join(format!(".minimap/graph/edges/{old_id}.json"))
        .exists());
    let graph = minimap_repo::load_graph(app.temp.path()).unwrap();
    assert_eq!(graph.edges[old_id].superseded_by.len(), 1);
    app.layout("home", screen("home", &[("A old", 10)]));
    app.at("home");
    let result = app.command(&["go", "search"]);
    assert_eq!(app.current(), "search");
    assert_eq!(result["data"]["planned_path"][0], old_id);
    assert_eq!(result["data"]["recovery"]["outcome"], "recovered");
}

#[test]
fn v1_edges_are_read_without_rewriting_the_shared_checkout() {
    let app = App::new();
    app.layout("home", screen("home", &[("Next", 10)]));
    app.layout("next", screen("next", &[]));
    app.route("home", "tap_10_20", "next");
    app.at("home");
    app.command(&["whereami", "--label", "home"]);
    let learned = app.command(&["tap", "--selector", "text=Next", "--label", "next"]);
    let path = minimap_repo::edge_path(app.temp.path(), learned["data"]["edge"].as_str().unwrap());
    let mut value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    value["schema_version"] = json!("minimap.edge.v1");
    let original = value.to_string();
    fs::write(&path, &original).unwrap();
    app.at("home");
    app.command(&["go", "next"]);
    assert_eq!(fs::read_to_string(path).unwrap(), original);
}
