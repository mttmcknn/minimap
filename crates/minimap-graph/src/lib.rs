use minimap_core::normalize_label;
use minimap_repo::Graph;
use minimap_schemas::{Edge, Viewport};
use serde_json::{json, Value};
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, VecDeque};

#[derive(Debug, Clone)]
pub struct PathPlan {
    pub status: String,
    pub target_slug: String,
    pub current_slug: String,
    pub edges: Vec<Edge>,
    pub skipped_edges: Vec<Value>,
}

impl PathPlan {
    pub fn to_json(&self) -> Value {
        json!({
            "status": self.status,
            "target": self.target_slug,
            "current": self.current_slug,
            "edge_ids": self.edges.iter().map(|edge| edge.id.clone()).collect::<Vec<_>>(),
            "skipped_edges": self.skipped_edges
        })
    }
}

pub fn resolve_path(
    graph: &Graph,
    target: &str,
    current_place_id: &str,
    viewport: Option<Viewport>,
) -> PathPlan {
    resolve_path_excluding(graph, target, current_place_id, viewport, &BTreeSet::new())
}

pub fn resolve_path_excluding(
    graph: &Graph,
    target: &str,
    current_place_id: &str,
    viewport: Option<Viewport>,
    excluded: &BTreeSet<String>,
) -> PathPlan {
    let target_slug = normalize_label(target);
    let current_slug = graph
        .places
        .get(current_place_id)
        .map(|place| place.slug.clone())
        .unwrap_or_else(|| current_place_id.to_string());

    let Some(target_place) = graph
        .places
        .values()
        .find(|place| place.slug == target_slug)
    else {
        return PathPlan {
            status: "unknown".to_string(),
            target_slug,
            current_slug,
            edges: Vec::new(),
            skipped_edges: Vec::new(),
        };
    };

    if target_place.id == current_place_id {
        return PathPlan {
            status: "ok".to_string(),
            target_slug,
            current_slug,
            edges: Vec::new(),
            skipped_edges: Vec::new(),
        };
    }

    let mut skipped_edges = Vec::new();
    let all_edges = graph
        .edges
        .values()
        .filter(|edge| !excluded.contains(&edge.id))
        .collect::<Vec<_>>();
    let path = shortest_compatible_path(
        &all_edges,
        current_place_id,
        &target_place.id,
        viewport,
        &mut skipped_edges,
    );

    match path {
        Some(edges) => PathPlan {
            status: "ok".to_string(),
            target_slug,
            current_slug,
            edges,
            skipped_edges,
        },
        // The target is reachable when viewport compatibility is ignored, so a
        // path exists but cannot be replayed on this device.
        None if target_reachable_ignoring_viewport(
            &all_edges,
            current_place_id,
            &target_place.id,
        ) =>
        {
            PathPlan {
                status: "no_compatible_path".to_string(),
                target_slug,
                current_slug,
                edges: Vec::new(),
                skipped_edges,
            }
        }
        // No path exists at all, regardless of viewport.
        None => PathPlan {
            status: "no_known_path".to_string(),
            target_slug,
            current_slug,
            edges: Vec::new(),
            skipped_edges,
        },
    }
}

/// Adjacency is built once; neither search scans the entire edge set per node.
fn adjacency<'a>(edges: &[&'a Edge]) -> BTreeMap<&'a str, Vec<&'a Edge>> {
    let mut result: BTreeMap<&str, Vec<&Edge>> = BTreeMap::new();
    for edge in edges {
        result.entry(&edge.from.id).or_default().push(edge);
    }
    for outgoing in result.values_mut() {
        outgoing.sort_by_key(|edge| (edge_cost(edge), &edge.id));
    }
    result
}

fn target_reachable_ignoring_viewport(edges: &[&Edge], start: &str, target: &str) -> bool {
    let adjacent = adjacency(edges);
    let mut queue = VecDeque::from([start]);
    let mut visited = BTreeSet::from([start]);
    while let Some(place) = queue.pop_front() {
        if place == target {
            return true;
        }
        for edge in adjacent.get(place).into_iter().flatten() {
            if visited.insert(&edge.to.id) {
                queue.push_back(&edge.to.id);
            }
        }
    }
    false
}

/// Deterministic Dijkstra over explicit execution units: one verification per
/// edge, one per action, and extra cost for coordinate-dependent recipes.
/// These are planning units, not a prediction of model tokens or milliseconds.
fn shortest_compatible_path(
    edges: &[&Edge],
    start: &str,
    target: &str,
    viewport: Option<Viewport>,
    skipped_edges: &mut Vec<Value>,
) -> Option<Vec<Edge>> {
    let adjacent = adjacency(edges);
    // More than the total action cost of any simple route: prefer replacement
    // paths, but keep an old verified path available in a different app context.
    let fallback_penalty = edges
        .iter()
        .map(|edge| edge_cost(edge))
        .sum::<u64>()
        .saturating_add(1);
    let mut queue = BinaryHeap::from([Reverse((0u64, start))]);
    let mut distances = BTreeMap::from([(start, 0u64)]);
    let mut previous: BTreeMap<&str, &Edge> = BTreeMap::new();
    while let Some(Reverse((cost, place))) = queue.pop() {
        if distances.get(place) != Some(&cost) {
            continue;
        }
        if place == target {
            let mut path = Vec::new();
            let mut cursor = target;
            while cursor != start {
                let edge = *previous.get(cursor)?;
                path.push(edge.clone());
                cursor = &edge.from.id;
            }
            path.reverse();
            return Some(path);
        }
        for edge in adjacent.get(place).into_iter().flatten() {
            if edge.recipe.iter().any(|step| step.kind == "press_back") {
                skipped_edges
                    .push(json!({"edge": edge.id, "reason": "back_requires_verified_history"}));
                continue;
            }
            if !edge_compatible(edge, viewport) {
                skipped_edges.push(json!({"edge": edge.id, "reason": "incompatible_viewport"}));
                continue;
            }
            let next_cost = cost.saturating_add(edge_cost(edge)).saturating_add(
                if edge.superseded_by.is_empty() {
                    0
                } else {
                    fallback_penalty
                },
            );
            if distances
                .get(edge.to.id.as_str())
                .is_none_or(|old| next_cost < *old)
            {
                distances.insert(&edge.to.id, next_cost);
                previous.insert(&edge.to.id, edge);
                queue.push(Reverse((next_cost, &edge.to.id)));
            }
        }
    }
    None
}

fn edge_cost(edge: &Edge) -> u64 {
    1 + edge
        .recipe
        .iter()
        .map(|step| if step.is_geometry() { 3 } else { 1 })
        .sum::<u64>()
}

pub fn edge_compatible(edge: &Edge, viewport: Option<Viewport>) -> bool {
    edge.recipe.iter().all(|step| {
        if step.point.is_some() {
            step.viewport.is_some() && step.viewport == viewport
        } else {
            true
        }
    })
}

pub fn exit_code_for_status(status: &str) -> i32 {
    match status {
        "ok" | "known" | "known_changed" => 0,
        "needs_label" => 5,
        "unknown" | "no_known_path" | "no_compatible_path" => 5,
        "blocked_by_overlay" | "label_mismatch" | "action_failed" => 2,
        "environment_error" => 6,
        "config_error" => 7,
        _ => 2,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use minimap_schemas::{ActionStep, EdgeEndpoint, Fingerprint, Place, PlaceBaseline, Selector};
    use std::collections::BTreeMap;

    fn place(slug: &str) -> Place {
        Place {
            schema_version: minimap_schemas::PLACE_SCHEMA_VERSION.to_string(),
            id: format!("place_{slug}"),
            slug: slug.to_string(),
            label: slug.to_string(),
            baseline: PlaceBaseline {
                identity_hash: format!("sha256:{slug}"),
                fingerprint: Fingerprint {
                    selectors: Vec::new(),
                    static_text: Vec::new(),
                    roles: BTreeMap::new(),
                },
            },
            variants: Vec::new(),
        }
    }

    fn edge(id: &str, from: &str, to: &str, geometry: bool) -> Edge {
        Edge {
            superseded_by: Vec::new(),
            schema_version: minimap_schemas::EDGE_SCHEMA_VERSION.to_string(),
            id: id.to_string(),
            from: EdgeEndpoint {
                id: format!("place_{from}"),
                slug: from.to_string(),
            },
            to: EdgeEndpoint {
                id: format!("place_{to}"),
                slug: to.to_string(),
            },
            intent: None,
            recipe: vec![if geometry {
                ActionStep {
                    kind: "tap".to_string(),
                    selector: None,
                    point: Some(minimap_schemas::Point { x: 1, y: 2 }),
                    viewport: Some(Viewport {
                        width: 10,
                        height: 20,
                    }),
                    direction: None,
                }
            } else {
                ActionStep {
                    kind: "tap".to_string(),
                    selector: Some(Selector {
                        kind: "test_tag".to_string(),
                        value: "next".to_string(),
                    }),
                    point: None,
                    viewport: None,
                    direction: None,
                }
            }],
        }
    }

    #[test]
    fn weighted_search_prefers_less_work_and_respects_exclusions() {
        let mut expensive = edge("expensive", "home", "target", false);
        expensive.recipe = vec![expensive.recipe[0].clone(); 12];
        let mut graph = Graph {
            places: [place("home"), place("via"), place("target")]
                .into_iter()
                .map(|p| (p.id.clone(), p))
                .collect(),
            edges: [
                expensive,
                edge("first", "home", "via", false),
                edge("second", "via", "target", false),
            ]
            .into_iter()
            .map(|e| (e.id.clone(), e))
            .collect(),
        };
        assert_eq!(
            resolve_path(&graph, "target", "place_home", None)
                .edges
                .len(),
            2
        );
        let excluded = BTreeSet::from(["first".into()]);
        assert_eq!(
            resolve_path_excluding(&graph, "target", "place_home", None, &excluded).edges[0].id,
            "expensive"
        );
        graph.edges.get_mut("expensive").unwrap().recipe[0].kind = "press_back".into();
        assert_eq!(
            resolve_path_excluding(&graph, "target", "place_home", None, &excluded).status,
            "no_compatible_path"
        );
    }

    #[test]
    fn sparse_graph_with_cycles_and_ten_thousand_places_is_reachable() {
        let mut graph = Graph {
            places: BTreeMap::new(),
            edges: BTreeMap::new(),
        };
        for n in 0..10_000 {
            let p = place(&n.to_string());
            graph.places.insert(p.id.clone(), p);
            let e = edge(
                &format!("forward-{n}"),
                &n.to_string(),
                &((n + 1) % 10_000).to_string(),
                false,
            );
            graph.edges.insert(e.id.clone(), e);
        }
        let start = std::time::Instant::now();
        let plan = resolve_path(&graph, "9999", "place_0", None);
        assert_eq!(plan.edges.len(), 9999);
        eprintln!("10k-place planner: {:?}", start.elapsed());
    }

    #[test]
    fn resolves_selector_path_before_geometry() {
        let graph = Graph {
            places: BTreeMap::from([
                ("place_home".to_string(), place("home")),
                ("place_settings".to_string(), place("settings")),
            ]),
            edges: BTreeMap::from([
                (
                    "edge_home__settings__geo".to_string(),
                    edge("edge_home__settings__geo", "home", "settings", true),
                ),
                (
                    "edge_home__settings__selector".to_string(),
                    edge("edge_home__settings__selector", "home", "settings", false),
                ),
            ]),
        };
        let plan = resolve_path(&graph, "settings", "place_home", None);
        assert_eq!(plan.status, "ok");
        assert_eq!(plan.edges[0].id, "edge_home__settings__selector");
    }

    #[test]
    fn unreachable_target_reports_no_known_path_despite_unrelated_skipped_edge() {
        // `inbox` has no incoming edges, so it is structurally unreachable.
        // An unrelated viewport-incompatible geometry edge exists elsewhere
        // (home -> settings) and gets skipped during traversal. The status
        // must still be "no_known_path" because no path to the target exists
        // even when viewport compatibility is ignored.
        let graph = Graph {
            places: BTreeMap::from([
                ("place_home".to_string(), place("home")),
                ("place_settings".to_string(), place("settings")),
                ("place_inbox".to_string(), place("inbox")),
            ]),
            edges: BTreeMap::from([(
                "edge_home__settings__geo".to_string(),
                edge("edge_home__settings__geo", "home", "settings", true),
            )]),
        };
        let plan = resolve_path(&graph, "inbox", "place_home", None);
        assert_eq!(plan.status, "no_known_path");
    }

    #[test]
    fn target_reachable_only_via_incompatible_geometry_reports_no_compatible_path() {
        // The only path home -> settings is a geometry edge whose viewport does
        // not match the requested (None) viewport, so it is skipped. A path
        // exists when viewport is ignored, so the status is "no_compatible_path".
        let graph = Graph {
            places: BTreeMap::from([
                ("place_home".to_string(), place("home")),
                ("place_settings".to_string(), place("settings")),
            ]),
            edges: BTreeMap::from([(
                "edge_home__settings__geo".to_string(),
                edge("edge_home__settings__geo", "home", "settings", true),
            )]),
        };
        let plan = resolve_path(&graph, "settings", "place_home", None);
        assert_eq!(plan.status, "no_compatible_path");
        assert!(!plan.skipped_edges.is_empty());
    }
}
