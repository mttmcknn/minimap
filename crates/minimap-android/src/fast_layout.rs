//! Fresh, short-lived UiAutomation captures for saved-route replay.
//! The helper never injects input or persists UI data. Unsupported devices use
//! the existing Android CLI adapter instead; deadline failures still propagate.
use super::{parse_layout, run_checked, CommandResult, CommandRunner};
use anyhow::Result;
use sha2::{Digest, Sha256};
use std::io::Write;

const HELPER: &[u8] = include_bytes!("../helper/minimap-layout.jar");

#[derive(Default)]
pub(super) struct FastLayout {
    remote: Option<String>,
}

impl FastLayout {
    pub fn capture<R: CommandRunner>(
        &mut self,
        runner: &mut R,
        serial: &str,
    ) -> Result<CommandResult> {
        let prefix = || vec!["adb".into(), "-s".into(), serial.into()];
        if self.remote.is_none() {
            let remote = format!(
                "/data/local/tmp/minimap-layout-{:x}.jar",
                Sha256::digest(HELPER)
            );
            let mut file = tempfile::NamedTempFile::new()?;
            file.write_all(HELPER)?;
            let mut args = prefix();
            args.extend([
                "push".into(),
                file.path().to_string_lossy().into_owned(),
                remote.clone(),
            ]);
            run_checked(runner, args, &[])?;
            self.remote = Some(remote);
        }
        let mut args = prefix();
        args.extend([
            "shell".into(),
            format!(
                "CLASSPATH={}:/system/framework/uiautomator.jar",
                self.remote.as_ref().unwrap()
            ),
            "app_process".into(),
            "/system/bin".into(),
            "dev.minimap.MinimapLayout".into(),
        ]);
        let output = run_checked(runner, args, &[])?;
        let layout = parse_layout(&output.stdout)?;
        anyhow::ensure!(
            layout.as_array().is_some_and(|nodes| !nodes.is_empty()),
            "Empty fast observation"
        );
        Ok(output)
    }
}
