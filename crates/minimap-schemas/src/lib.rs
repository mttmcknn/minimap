use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;

pub const CONFIG_SCHEMA_VERSION: &str = "minimap.config.v2";
pub const PLACE_SCHEMA_VERSION: &str = "minimap.place.v1";
pub const EDGE_SCHEMA_VERSION: &str = "minimap.edge.v2";
pub const RESULT_SCHEMA_VERSION: &str = "minimap.result.v1";

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub struct Viewport {
    pub width: i64,
    pub height: i64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub struct Point {
    pub x: i64,
    pub y: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AppProfile {
    #[serde(default)]
    pub android_package: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MinimapConfig {
    pub schema_version: String,
    pub active_app_profile: String,
    pub app_profiles: BTreeMap<String, AppProfile>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(deny_unknown_fields)]
pub struct Selector {
    pub kind: String,
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct StaticText {
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Fingerprint {
    #[serde(default)]
    pub selectors: Vec<Selector>,
    #[serde(default)]
    pub static_text: Vec<StaticText>,
    #[serde(default)]
    pub roles: BTreeMap<String, usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PlaceBaseline {
    pub identity_hash: String,
    pub fingerprint: Fingerprint,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Place {
    pub schema_version: String,
    pub id: String,
    pub slug: String,
    pub label: String,
    pub baseline: PlaceBaseline,
    #[serde(default)]
    pub variants: Vec<PlaceBaseline>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EdgeEndpoint {
    pub id: String,
    pub slug: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ActionStep {
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selector: Option<Selector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<Point>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub viewport: Option<Viewport>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub direction: Option<String>,
}

impl ActionStep {
    pub fn is_geometry(&self) -> bool {
        self.point.is_some()
    }

    pub fn validate(&self) -> Result<()> {
        match self.kind.as_str() {
            "tap" => {
                anyhow::ensure!(self.direction.is_none(), "tap cannot have a direction");
                match (&self.selector, self.point, self.viewport) {
                    (Some(selector), None, None) => selector.validate()?,
                    (None, Some(point), Some(viewport)) => {
                        anyhow::ensure!(
                            viewport.width > 0
                                && viewport.height > 0
                                && point.x >= 0
                                && point.y >= 0
                                && point.x < viewport.width
                                && point.y < viewport.height,
                            "point must be inside a positive viewport"
                        );
                    }
                    _ => bail!("tap needs exactly one selector or a point with its viewport"),
                }
            }
            "scroll" => anyhow::ensure!(
                self.selector.is_none()
                    && self.point.is_none()
                    && self.viewport.is_none()
                    && matches!(
                        self.direction.as_deref(),
                        Some("up" | "down" | "left" | "right")
                    ),
                "scroll needs one supported direction and no other payload"
            ),
            "press_back" => anyhow::ensure!(
                self.selector.is_none()
                    && self.point.is_none()
                    && self.viewport.is_none()
                    && self.direction.is_none(),
                "Back cannot have an action payload"
            ),
            _ => bail!("unsupported action kind"),
        }
        Ok(())
    }
}

impl Selector {
    pub fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            matches!(
                self.kind.as_str(),
                "test_tag"
                    | "testTag"
                    | "test-tag"
                    | "resource_id"
                    | "id"
                    | "resourceId"
                    | "resource-id"
                    | "content_desc"
                    | "content_description"
                    | "desc"
                    | "contentDesc"
                    | "contentDescription"
                    | "content-desc"
                    | "text"
            ),
            "unsupported selector kind"
        );
        anyhow::ensure!(
            !self.value.trim().is_empty()
                && self.value.len() <= 512
                && !self.value.chars().any(char::is_control),
            "selector value must be nonempty bounded text without control characters"
        );
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Edge {
    pub schema_version: String,
    pub id: String,
    pub from: EdgeEndpoint,
    pub to: EdgeEndpoint,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub intent: Option<String>,
    pub recipe: Vec<ActionStep>,
    /// Preferred replacement route; this edge remains a verified fallback for
    /// teammates whose app context still needs it.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub superseded_by: Vec<String>,
}

impl Edge {
    pub fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            !self.id.is_empty() && !self.from.id.is_empty() && !self.to.id.is_empty(),
            "edge and endpoint IDs must be nonempty"
        );
        anyhow::ensure!(
            !self.recipe.is_empty() && self.recipe.len() <= 32,
            "recipe needs between 1 and 32 actions"
        );
        for (index, step) in self.recipe.iter().enumerate() {
            step.validate()
                .map_err(|error| anyhow::anyhow!("action {index}: {error}"))?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MinimapResult {
    pub schema_version: String,
    pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(default)]
    pub data: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recommended_action: Option<String>,
}

impl MinimapResult {
    pub fn new(status: impl Into<String>, summary: impl Into<String>, data: Value) -> Self {
        Self {
            schema_version: RESULT_SCHEMA_VERSION.to_string(),
            status: status.into(),
            summary: Some(summary.into()),
            data,
            recommended_action: None,
        }
    }

    pub fn with_recommendation(mut self, recommendation: impl Into<String>) -> Self {
        self.recommended_action = Some(recommendation.into());
        self
    }
}

pub fn require_schema(value: &Value, expected: &str) -> Result<()> {
    let actual = value.get("schema_version").and_then(Value::as_str);
    if actual == Some(expected) {
        Ok(())
    } else {
        bail!("unsupported schema_version: expected {expected}, got {actual:?}")
    }
}

pub fn canonical_json(value: &Value) -> String {
    let sorted = sort_json(value);
    let mut output = serde_json::to_string_pretty(&sorted).expect("canonical JSON serialization");
    output.push('\n');
    output
}

pub fn sort_json(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut sorted = Map::new();
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            for key in keys {
                sorted.insert(key.clone(), sort_json(&map[key]));
            }
            Value::Object(sorted)
        }
        Value::Array(values) => Value::Array(values.iter().map(sort_json).collect()),
        _ => value.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_json_sorts_keys() {
        let value = serde_json::json!({"z": 1, "a": {"b": 2, "a": 1}});
        assert_eq!(
            canonical_json(&value),
            "{\n  \"a\": {\n    \"a\": 1,\n    \"b\": 2\n  },\n  \"z\": 1\n}\n"
        );
    }
}
