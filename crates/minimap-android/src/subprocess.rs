use super::{CommandResult, CommandRunner, DriverError};
use anyhow::{Context, Result};
use std::io::{Read, Seek, SeekFrom};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const OUTPUT_LIMIT: u64 = 8 * 1024 * 1024;
const COMMAND_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Default)]
pub struct SubprocessRunner {
    deadline: Option<Instant>,
}

struct RunningChild(Child);
impl Drop for RunningChild {
    fn drop(&mut self) {
        if self.0.try_wait().is_ok_and(|status| status.is_some()) {
            return;
        }
        #[cfg(unix)]
        // Each command has its own process group, so timeout also stops shell children.
        unsafe {
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

impl CommandRunner for SubprocessRunner {
    fn deadline(&self) -> Option<Instant> {
        self.deadline
    }
    fn set_deadline(&mut self, deadline: Instant) {
        self.deadline = Some(deadline);
    }

    fn run(&mut self, args: &[String], env: &[(String, String)]) -> Result<CommandResult> {
        let (program, rest) = args.split_first().context("empty command")?;
        let deadline = self
            .deadline
            .unwrap_or_else(|| Instant::now() + COMMAND_TIMEOUT)
            .min(Instant::now() + COMMAND_TIMEOUT);
        if Instant::now() >= deadline {
            return Err(DriverError("Navigation deadline exhausted".into()).into());
        }
        // Private temporary files avoid pipe deadlocks and unbounded in-memory output.
        let mut stdout = tempfile::tempfile()?;
        let mut stderr = tempfile::tempfile()?;
        let mut command = Command::new(program);
        command
            .args(rest)
            .envs(env.iter().cloned())
            .stdin(Stdio::null())
            .stdout(stdout.try_clone()?)
            .stderr(stderr.try_clone()?);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = RunningChild(
            command
                .spawn()
                .map_err(|error| DriverError(format!("Cannot execute {program}: {error}")))?,
        );
        let status = loop {
            if stdout.metadata()?.len() + stderr.metadata()?.len() > OUTPUT_LIMIT {
                return Err(DriverError(format!("{program} exceeded the output limit")).into());
            }
            if let Some(status) = child.0.try_wait()? {
                break status;
            }
            if Instant::now() >= deadline {
                return Err(DriverError(format!("{program} timed out")).into());
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        if stdout.metadata()?.len() + stderr.metadata()?.len() > OUTPUT_LIMIT {
            return Err(DriverError(format!("{program} exceeded the output limit")).into());
        }
        stdout.seek(SeekFrom::Start(0))?;
        stderr.seek(SeekFrom::Start(0))?;
        let mut out = String::new();
        let mut err = String::new();
        stdout.take(OUTPUT_LIMIT).read_to_string(&mut out)?;
        stderr.take(OUTPUT_LIMIT).read_to_string(&mut err)?;
        Ok(CommandResult {
            args: args.to_vec(),
            status: status.code().unwrap_or(-1),
            stdout: out,
            stderr: err,
        })
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn timeout_stops_a_child_before_it_can_act() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("acted");
        let mut runner = SubprocessRunner::default();
        runner.set_deadline(Instant::now() + Duration::from_millis(80));
        let result = runner.run(
            &[
                "sh".into(),
                "-c".into(),
                "sleep 0.3; touch \"$1\"".into(),
                "test".into(),
                marker.display().to_string(),
            ],
            &[],
        );
        assert!(result.unwrap_err().to_string().contains("timed out"));
        std::thread::sleep(Duration::from_millis(350));
        assert!(!marker.exists());
    }

    #[test]
    fn excessive_output_is_rejected() {
        let mut runner = SubprocessRunner::default();
        let error = runner
            .run(
                &[
                    "sh".into(),
                    "-c".into(),
                    "dd if=/dev/zero bs=1048576 count=12 2>/dev/null".into(),
                ],
                &[],
            )
            .unwrap_err();
        assert!(error.to_string().contains("output limit"));
    }
}
