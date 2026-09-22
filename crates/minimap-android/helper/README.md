# Replay observations

`minimap go` tries this small, bundled helper for fresh accessibility captures.
It opens a new UiAutomation connection for each observation, waits for a short
quiet period, emits the current semantic nodes, disconnects, and exits. It does
not tap, change the application, retain a UI snapshot, or start a daemon.

The host uploads the content-addressed JAR once per `go` command using the
explicitly selected device. Upload time is included in navigation benchmarks.
Only helper code remains in `/data/local/tmp/minimap-layout-<sha256>.jar`;
no application data is written there. Ordinary Cargo builds include the JAR
and require no Android SDK or Java installation.

The connection wrapper comes from the device's stock `uiautomator.jar` and is
not a supported SDK API. If the wrapper or capture is unavailable, malformed,
or empty, Minimap uses a fresh Android CLI observation for the rest of the
command. Unrecognized starting screens and unresolved destination/selector
checks also get an Android CLI confirmation before recovery. A saved screen
must match exactly on the fast path; changed screen evidence is confirmed with
Android CLI before it can enter the graph. All subprocesses
retain the navigation deadline, output limit, and selected-device scope.
`go --android-cli-layout` selects Android CLI throughout for compatibility.

Serialization preserves Android CLI's text, descriptions, semantic-node
selection, and shortened resource IDs so graphs recorded with Android CLI
remain usable. The helper also marks disabled and private input controls.
Graph matching, foreground checks, input budgets, and destination assertions
still run in the existing Rust code. Learning and `layout --diff` continue to
use Android CLI.

Rebuild with JDK 21, Android platform 36, and build-tools 36.0.0:

```sh
python3 crates/minimap-android/helper/build.py --sdk "$ANDROID_HOME" --jdk "$JAVA_HOME"
python3 crates/minimap-android/helper/build.py --sdk "$ANDROID_HOME" --jdk "$JAVA_HOME" --check
```

The archive has a fixed timestamp and contains only the compiled Minimap class.
`--check` compares bytes with the checked-in artifact. The implementation uses
[fresh root observations and bounded idle waits](https://developer.android.com/reference/android/app/UiAutomation),
with the shell connection used by Android's
[stock dump command](https://android.googlesource.com/platform/prebuilts/fullsdk/sources/android-31/+/refs/heads/androidx-savedstate-release/com/android/commands/uiautomator/DumpCommand.java).
