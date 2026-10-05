use anyhow::Result;
use minimap_android::{AndroidCli, CommandRunner, DriverError};
use minimap_core::{detect_overlay, fingerprint_layout, fingerprint_usable};
use minimap_schemas::PlaceBaseline;
use serde_json::Value;
use std::time::Duration;

pub(super) fn observe_layout<R: CommandRunner>(
    android: &mut AndroidCli<R>,
    diff: bool,
) -> Result<Value> {
    for attempt in 0..3 {
        let result = android.layout(diff);
        let transient = match &result {
            Ok(output) => {
                output.stdout.trim().is_empty()
                    || output
                        .stdout
                        .contains("null root node returned by UiTestAutomationBridge")
                    || output
                        .stderr
                        .contains("null root node returned by UiTestAutomationBridge")
            }
            Err(error) => error
                .to_string()
                .contains("null root node returned by UiTestAutomationBridge"),
        };
        if !diff && transient && attempt < 2 {
            android.pause(Duration::from_millis(150 * (attempt + 1)))?;
            continue;
        }
        return minimap_android::parse_layout(&result?.stdout);
    }
    unreachable!()
}

/// Learning needs stable evidence; a replay can finish as soon as the expected
/// destination is verified. Neither path turns an unsettled frame into a place.
pub(super) fn observe_after_action<R: CommandRunner>(
    android: &mut AndroidCli<R>,
    _previous: Option<&PlaceBaseline>,
) -> Result<Value> {
    observe_destination(android, |_| false, true)
}

/// Visibility alone does not make an animating control safe to tap. Require
/// the same unique, enabled tap point in two consecutive fresh observations.
pub(super) fn observe_selector<R: CommandRunner>(
    android: &mut AndroidCli<R>,
    selector: &str,
    mut initial_layout: Option<Value>,
) -> Result<Value> {
    let mut previous = None;
    let mut last_error = None;
    for attempt in 0..5 {
        if attempt == 3 && android.uses_fast_layout() {
            android.use_android_cli_layout();
            previous = None;
        }
        let layout = match initial_layout.take() {
            Some(layout) => layout,
            None => observe_layout(android, false)?,
        };
        if let Some(reason) = detect_overlay(&layout) {
            anyhow::bail!("input blocked by overlay: {reason}");
        }
        match minimap_android::resolve_selector_point(&layout, selector) {
            Ok(point) => {
                if previous == Some(point) {
                    return Ok(layout);
                }
                previous = Some(point);
                last_error = None;
            }
            Err(error) => {
                previous = None;
                last_error = Some(error);
            }
        }
        if attempt < 4 {
            android.pause(Duration::from_millis(super::action_settle_ms().min(250)))?;
        }
    }
    Err(last_error.unwrap_or_else(|| {
        DriverError("Selector did not settle into a stable tap location".into()).into()
    }))
}

pub(super) fn observe_destination<R: CommandRunner>(
    android: &mut AndroidCli<R>,
    expected: impl Fn(&Value) -> bool,
    require_stable: bool,
) -> Result<Value> {
    let mut previous = None;
    let mut last = None;
    for attempt in 0..3 {
        let layout = observe_layout(android, false)?;
        let baseline = fingerprint_layout(&layout);
        if detect_overlay(&layout).is_some() || expected(&layout) {
            return Ok(layout);
        }
        if require_stable
            && fingerprint_usable(&baseline)
            && previous.as_ref() == Some(&baseline.identity_hash)
        {
            return Ok(layout);
        }
        previous = Some(baseline.identity_hash);
        last = Some(layout);
        if attempt < 2 {
            android.pause(Duration::from_millis(super::action_settle_ms().min(250)))?;
        }
    }
    if require_stable {
        Err(DriverError(
            "UI did not settle into a usable observation; no destination was learned".into(),
        )
        .into())
    } else {
        // A faster capture can see a transition earlier than the stock dumper.
        // Confirm unresolved UI with that original fresh path before treating
        // it as a changed route, failed selector, or wrong destination.
        if android.uses_fast_layout() {
            android.use_android_cli_layout();
            observe_layout(android, false)
        } else {
            Ok(last.unwrap())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use minimap_android::CommandResult;
    use serde_json::json;
    use std::collections::VecDeque;

    struct Frames(VecDeque<String>);
    impl CommandRunner for Frames {
        fn run(&mut self, args: &[String], _: &[(String, String)]) -> Result<CommandResult> {
            Ok(CommandResult {
                args: args.to_vec(),
                status: 0,
                stdout: self.0.pop_front().expect("unexpected extra observation"),
                stderr: String::new(),
            })
        }
    }
    fn android(frames: Vec<String>) -> AndroidCli<Frames> {
        AndroidCli::new(Frames(frames.into()), Some("fixture".into()))
    }

    #[test]
    fn null_root_and_empty_transport_responses_are_retried_before_parsing() {
        let target = json!([{"text":"Ready"}]);
        let mut android = android(vec![
            "null root node returned by UiTestAutomationBridge".into(),
            "".into(),
            target.to_string(),
        ]);
        assert_eq!(observe_layout(&mut android, false).unwrap(), target);
        assert_eq!(android.layout_calls(), 3);
    }

    #[test]
    fn replay_waits_through_intermediate_frames_until_the_expected_destination() {
        let target = json!([{"text":"Search results"}]);
        let mut android = android(vec![
            "[]".into(),
            json!([{"text":"Loading"}]).to_string(),
            target.to_string(),
        ]);
        assert_eq!(
            observe_destination(&mut android, |layout| layout == &target, false).unwrap(),
            target
        );
        assert_eq!(android.layout_calls(), 3);
    }

    #[test]
    fn next_selector_waits_for_a_unique_enabled_control() {
        let ready = json!([{"text":"Next", "center":"[10,20]"}]);
        let mut android = android(vec![
            json!([{"text":"Loading"}]).to_string(),
            json!([{"text":"Next", "center":"[10,20]", "enabled":false}]).to_string(),
            ready.to_string(),
            ready.to_string(),
        ]);
        assert_eq!(
            observe_selector(&mut android, "text=Next", None).unwrap(),
            ready
        );
        assert_eq!(android.layout_calls(), 4);
    }

    #[test]
    fn initial_selector_observation_is_reused_but_its_position_is_confirmed() {
        let ready = json!([{"text":"Next", "center":"[10,20]"}]);
        let mut android = android(vec![ready.to_string()]);
        assert_eq!(
            observe_selector(&mut android, "text=Next", Some(ready.clone())).unwrap(),
            ready
        );
        assert_eq!(android.layout_calls(), 1);
    }

    #[test]
    fn a_visible_control_must_stop_moving_before_a_tap_is_allowed() {
        let ready = json!([{"text":"Next", "center":"[30,20]"}]);
        let mut android = android(vec![
            json!([{"text":"Next", "center":"[10,20]"}]).to_string(),
            ready.to_string(),
            ready.to_string(),
        ]);
        assert_eq!(
            observe_selector(&mut android, "text=Next", None).unwrap(),
            ready
        );
        assert_eq!(android.layout_calls(), 3);
    }

    #[test]
    fn a_control_that_keeps_moving_is_not_tapped_after_the_observation_bound() {
        let frames = (0..5)
            .map(|x| json!([{"text":"Next", "center":format!("[{x},20]")}]).to_string())
            .collect();
        let mut android = android(frames);
        assert!(observe_selector(&mut android, "text=Next", None)
            .unwrap_err()
            .to_string()
            .contains("stable tap location"));
        assert_eq!(android.layout_calls(), 5);
    }

    #[test]
    fn unresolved_fast_frames_are_confirmed_by_a_fresh_android_cli_observation() {
        let ready = json!([{"text":"Next", "center":"[10,20]"}]);
        let loading = json!([{"text":"Loading"}]).to_string();
        let mut android = android(vec![
            "".into(), // helper upload
            loading.clone(),
            loading.clone(),
            loading,
            ready.to_string(),
            ready.to_string(),
        ]);
        android.prefer_fast_layout();
        assert_eq!(
            observe_selector(&mut android, "text=Next", None).unwrap(),
            ready
        );
        assert_eq!(android.layout_calls(), 5);
        assert!(!android.uses_fast_layout());
    }

    #[test]
    fn ambiguous_selector_stays_an_error_after_bounded_observation() {
        let ambiguous = json!([
            {"text":"Next", "center":"[10,20]"},
            {"text":"Next", "center":"[30,20]"}
        ]);
        let mut android = android(vec![ambiguous.to_string(); 5]);
        assert!(observe_selector(&mut android, "text=Next", None)
            .unwrap_err()
            .to_string()
            .contains("Ambiguous selector"));
        assert_eq!(android.layout_calls(), 5);
    }

    #[test]
    fn overlay_prevents_replaying_an_otherwise_visible_selector() {
        let mut android = android(vec![json!([
            {"resource-id":"com.android.permissioncontroller:id/permission_allow_button"},
            {"text":"Next", "center":"[10,20]"}
        ])
        .to_string()]);
        assert!(observe_selector(&mut android, "text=Next", None)
            .unwrap_err()
            .to_string()
            .contains("blocked by overlay"));
        assert_eq!(android.layout_calls(), 1);
    }

    #[test]
    fn learning_rejects_an_observation_that_keeps_changing() {
        let mut android = android(vec![
            json!([{"text":"Loading one"}]).to_string(),
            json!([{"text":"Loading two"}]).to_string(),
            json!([{"text":"Loading three"}]).to_string(),
        ]);
        assert!(observe_after_action(&mut android, None).is_err());
    }
}
