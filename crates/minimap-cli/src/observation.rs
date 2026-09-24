use anyhow::Result;
use minimap_android::{AndroidCli, CommandRunner, DriverError};
use minimap_core::{detect_overlay, fingerprint_layout, fingerprint_usable};
use minimap_schemas::PlaceBaseline;
use serde_json::Value;
use std::time::Duration;

#[derive(Debug, Clone)]
pub(super) struct StableObservation {
    pub layout: Value,
    pub identity_hashes: Vec<String>,
    pub stable_frames_observed: usize,
}

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
    previous: Option<&PlaceBaseline>,
    stable_frames_required: usize,
) -> Result<StableObservation> {
    let stable_frames_required = stable_frames_required.clamp(1, 5);
    let max_attempts = stable_frames_required + 3;
    let previous_hash = previous.map(|baseline| baseline.identity_hash.as_str());
    let mut identity_hashes = Vec::with_capacity(max_attempts);
    let mut last_hash: Option<String> = None;
    let mut stable_frames_observed = 0;

    for attempt in 0..max_attempts {
        let layout = observe_layout(android, false)?;
        let baseline = fingerprint_layout(&layout);
        if last_hash.as_deref() == Some(&baseline.identity_hash) {
            stable_frames_observed += 1;
        } else {
            stable_frames_observed = 1;
        }
        last_hash = Some(baseline.identity_hash.clone());
        identity_hashes.push(baseline.identity_hash.clone());

        if detect_overlay(&layout).is_some() {
            return Ok(StableObservation {
                layout,
                identity_hashes,
                stable_frames_observed,
            });
        }

        let still_pre_action = previous_hash == Some(baseline.identity_hash.as_str());
        let stable =
            fingerprint_usable(&baseline) && stable_frames_observed >= stable_frames_required;
        // A delayed transition can yield the old screen for several consecutive
        // frames. Do not call that stable until a different identity appears or
        // the bounded capture window is exhausted. `--stable-frames 1` is the
        // explicit opt-out and accepts the first usable post-action frame.
        if stable
            && (stable_frames_required == 1 || !still_pre_action || attempt + 1 == max_attempts)
        {
            return Ok(StableObservation {
                layout,
                identity_hashes,
                stable_frames_observed,
            });
        }

        if attempt + 1 < max_attempts {
            android.pause(Duration::from_millis(super::action_settle_ms().min(250)))?;
        }
    }

    Err(DriverError(format!(
        "UI did not produce {stable_frames_required} identical usable post-action frames; no destination was learned"
    ))
    .into())
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
        Ok(last.unwrap())
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
    fn learning_rejects_an_observation_that_keeps_changing() {
        let mut android = android(vec![
            json!([{"text":"Loading one"}]).to_string(),
            json!([{"text":"Loading two"}]).to_string(),
            json!([{"text":"Loading three"}]).to_string(),
            json!([{"text":"Loading four"}]).to_string(),
            json!([{"text":"Loading five"}]).to_string(),
        ]);
        assert!(observe_after_action(&mut android, None, 2).is_err());
    }

    #[test]
    fn learning_waits_past_repeated_pre_action_frames_for_the_destination() {
        let home = json!([{"text":"Home"}]);
        let detail = json!([{"text":"Detail"}]);
        let previous = fingerprint_layout(&home);
        let mut android = android(vec![
            home.to_string(),
            home.to_string(),
            detail.to_string(),
            detail.to_string(),
        ]);

        let observed = observe_after_action(&mut android, Some(&previous), 2).unwrap();

        assert_eq!(observed.layout, detail);
        assert_eq!(observed.identity_hashes.len(), 4);
        assert_eq!(observed.stable_frames_observed, 2);
        assert_ne!(observed.identity_hashes[1], observed.identity_hashes[2]);
    }

    #[test]
    fn learning_can_require_three_identical_frames() {
        let target = json!([{"text":"Ready"}]);
        let mut android = android(vec![
            target.to_string(),
            target.to_string(),
            target.to_string(),
        ]);

        let observed = observe_after_action(&mut android, None, 3).unwrap();

        assert_eq!(observed.layout, target);
        assert_eq!(observed.identity_hashes.len(), 3);
        assert_eq!(observed.stable_frames_observed, 3);
    }
}
