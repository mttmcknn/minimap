use minimap_core::{fingerprint_layout, redact_layout};
use serde_json::json;

#[test]
fn editable_and_password_content_never_enters_observations_or_fingerprints() {
    let layout = json!([
        {"class":"android.widget.EditText", "resourceId":"name_field", "text":"Private Person", "children":[{"text":"Private Street"}]},
        {"interactions":["password"], "text":"unmarkedPassphrase", "content-desc":"unmarkedPassphrase"},
        {"editable":true, "value":"Private Message", "text":"Private Message"},
        {"text":"Settings", "testTag":"settings"}
    ]);
    for serialized in [
        redact_layout(&layout).to_string(),
        serde_json::to_string(&fingerprint_layout(&layout)).unwrap(),
    ] {
        for private in [
            "Private Person",
            "Private Street",
            "unmarkedPassphrase",
            "Private Message",
        ] {
            assert!(!serialized.contains(private));
        }
        assert!(serialized.contains("Settings"));
        assert!(serialized.contains("name_field"));
    }
}
