//! A goal's budget survives CLI processes and agent handoffs. Files are private
//! runtime state, never graph data; the repo/device operation locks protect RMW.
use anyhow::{Context, Result};
use minimap_android::{CommandResult, CommandRunner, SubprocessRunner};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::cell::RefCell;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub type Handle = Rc<RefCell<Recovery>>;

#[derive(Serialize, Deserialize)]
struct State {
    repo: String,
    serial: String,
    target: String,
    expectations: Vec<String>,
    expires_ms: u64,
    remaining_actions: u32,
    #[serde(default)]
    excluded_edges: BTreeSet<String>,
    complete: bool,
}

pub struct Recovery {
    path: PathBuf,
    state: State,
}

#[derive(Debug)]
pub struct Exhausted;
impl std::fmt::Display for Exhausted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("shared recovery budget exhausted; preserve the unresolved goal")
    }
}
impl std::error::Error for Exhausted {}

#[derive(Debug)]
pub struct Failure {
    pub cause: anyhow::Error,
    pub recovery: Value,
}
impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.cause.fmt(f)
    }
}
impl std::error::Error for Failure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.cause.as_ref())
    }
}

fn repo_identity(root: &Path) -> Result<String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(root.canonicalize()?.to_string_lossy().as_bytes())
    ))
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

impl Recovery {
    pub fn create(
        root: &Path,
        serial: &str,
        target: &str,
        expectations: &[String],
        actions: u32,
        deadline: Instant,
    ) -> Result<Handle> {
        let directory = minimap_repo::OperationLock::runtime_directory()?;
        // Bounded opportunistic cleanup; no active five-minute goal can be a
        // day old. Retain failures briefly for local diagnosis, never in git.
        for entry in std::fs::read_dir(&directory)?
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().starts_with("recovery-"))
            .take(64)
        {
            if entry
                .metadata()
                .and_then(|m| m.modified())
                .ok()
                .and_then(|modified| SystemTime::now().duration_since(modified).ok())
                .is_some_and(|age| age > Duration::from_secs(86_400))
            {
                let _ = std::fs::remove_file(entry.path());
            }
        }
        let file = tempfile::Builder::new()
            .prefix("recovery-")
            .tempfile_in(directory)?;
        let (_, path) = file.keep()?;
        let recovery = Self {
            path,
            state: State {
                repo: repo_identity(root)?,
                serial: serial.into(),
                target: minimap_core::normalize_label(target),
                expectations: expectations.to_vec(),
                expires_ms: now_ms().saturating_add(
                    deadline
                        .saturating_duration_since(Instant::now())
                        .as_millis() as u64,
                ),
                remaining_actions: actions,
                excluded_edges: BTreeSet::new(),
                complete: false,
            },
        };
        recovery.save()?;
        Ok(Rc::new(RefCell::new(recovery)))
    }

    pub fn load(root: &Path, serial: &str, token: &str) -> Result<Handle> {
        anyhow::ensure!(
            token
                .strip_prefix("recovery-")
                .is_some_and(|id| !id.is_empty()
                    && id.len() <= 64
                    && id.chars().all(|c| c.is_ascii_alphanumeric())),
            "invalid recovery token"
        );
        let path = minimap_repo::OperationLock::runtime_directory()?.join(token);
        let state: State = serde_json::from_slice(
            &std::fs::read(&path).context("recovery token is unavailable")?,
        )?;
        anyhow::ensure!(
            state.repo == repo_identity(root)? && state.serial == serial,
            "recovery token belongs to another repository or device"
        );
        Ok(Rc::new(RefCell::new(Self { path, state })))
    }

    fn save(&self) -> Result<()> {
        minimap_repo::write_json(&self.path, &serde_json::to_value(&self.state)?)
    }

    pub fn deadline(&self) -> Instant {
        Instant::now() + Duration::from_millis(self.state.expires_ms.saturating_sub(now_ms()))
    }

    pub fn check_time(&self) -> Result<()> {
        if self.state.complete || now_ms() >= self.state.expires_ms {
            return Err(Exhausted.into());
        }
        Ok(())
    }

    pub fn remaining_actions(&self) -> u32 {
        self.state.remaining_actions
    }
    pub fn excluded_edges(&self) -> BTreeSet<String> {
        self.state.excluded_edges.clone()
    }

    pub fn expectations_for(&self, target: &str) -> Vec<String> {
        if self.state.target == minimap_core::normalize_label(target) {
            self.state.expectations.clone()
        } else {
            Vec::new()
        }
    }

    pub fn reserve_action(&mut self) -> Result<()> {
        self.check_time()?;
        if self.state.remaining_actions == 0 {
            return Err(Exhausted.into());
        }
        // Persist before sending input. A killed or failed command cannot get
        // this action back by reconnecting to the same recovery token.
        self.state.remaining_actions -= 1;
        self.save()
    }

    pub fn record_go(&mut self, target: &str, result: &Value) -> Result<()> {
        if let Some(edges) = result["data"]["recovery"]["excluded_edges"].as_array() {
            self.state
                .excluded_edges
                .extend(edges.iter().filter_map(Value::as_str).map(str::to_string));
        }
        if result["status"] == "ok" && minimap_core::normalize_label(target) == self.state.target {
            self.state.complete = true;
            self.state.expectations.clear();
        }
        self.save()
    }

    pub fn context(&self) -> Value {
        json!({"token": self.path.file_name().unwrap().to_string_lossy(),
            "original_target": self.state.target, "remaining_actions": self.state.remaining_actions,
            "remaining_seconds": self.state.expires_ms.saturating_sub(now_ms()) / 1000,
            "complete": self.state.complete})
    }
}

pub fn decorate(mut result: Value, handle: Option<&Handle>) -> Value {
    if let Some(handle) = handle {
        if !result["data"].is_object() {
            result["data"] = json!({});
        }
        if !result["data"]["recovery"].is_object() {
            result["data"]["recovery"] = json!({});
        }
        for (key, value) in handle.borrow().context().as_object().unwrap() {
            result["data"]["recovery"][key] = value.clone();
        }
    }
    result
}

pub struct Runner {
    inner: SubprocessRunner,
    budget: Option<Handle>,
}
impl Runner {
    pub fn new(deadline: Option<Instant>, budget: Option<Handle>) -> Self {
        let mut inner = SubprocessRunner::default();
        if let Some(deadline) = deadline {
            inner.set_deadline(deadline);
        }
        Self { inner, budget }
    }
}
impl CommandRunner for Runner {
    fn deadline(&self) -> Option<Instant> {
        self.inner.deadline()
    }
    fn set_deadline(&mut self, deadline: Instant) {
        let deadline = self
            .budget
            .as_ref()
            .map_or(deadline, |budget| deadline.min(budget.borrow().deadline()));
        self.inner.set_deadline(deadline);
    }
    fn run(&mut self, args: &[String], env: &[(String, String)]) -> Result<CommandResult> {
        if let Some(budget) = &self.budget {
            budget.borrow().check_time()?;
            if args
                .windows(2)
                .any(|args| args[0] == "shell" && args[1] == "input")
            {
                budget.borrow_mut().reserve_action()?;
            }
        }
        self.inner.run(args, env)
    }
}
