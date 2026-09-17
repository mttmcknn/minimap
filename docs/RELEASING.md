# Releasing Minimap

Minimap uses Apache-2.0 and ships from tagged GitHub releases. The GitHub
release is the source of truth for binary assets. crates.io and Homebrew should
follow the same version tag.

## Release Channels

- GitHub Releases: macOS, Linux, and Windows archives built by `.github/workflows/release.yml`.
- crates.io: Rust users install with `cargo install minimap-cli`.
- Homebrew tap: macOS and Linux users install with `brew install mttmcknn/minimap/minimap`.

Scoop, winget, Nix, AUR, Debian, and RPM packages can be added later once there
is demand.

## Prerequisites

- A clean `main` branch.
- A semver version in `Cargo.toml` and all internal crate dependency versions.
- For crates.io publication, a token saved as the repository secret `CARGO_REGISTRY_TOKEN`.
- For Homebrew updates, a tap repository named `mttmcknn/homebrew-minimap`.

## Local Verification

For the 0.2.0 hardening release, read the
[controlled evaluation](../evals/results/2026-09-16-controlled.md) and
[benchmark graphs](../evals/results/2026-09-17-benchmarks/README.md), including
their remaining measurement gaps. Historical reports preserve the publication
status and uncommitted source state at the time of each experiment; they are
not a live release-status feed.
New edge-v2 files require a compatible CLI across the team. Refresh installed
agent skills so recovery tokens and goal checks are carried through the host
workflow; upgrading the binary alone does not update copied skills.

The plugin's `SKILL.md` is canonical. After changing it, update the byte-identical
crate copy at `crates/minimap-repo/skills/minimap-app-navigation.md`; the workspace
test rejects drift. The bundled copy lets crate archives compile independently
of the repository's plugin directory.

Run these checks before tagging:

```bash
cargo fmt --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
python3 -m unittest discover -s evals -p 'test_*.py'
cargo build --release --locked -p minimap-cli --bin minimap
cargo package --workspace
```

For a smoke test from the built binary:

```bash
target/release/minimap --version
target/release/minimap --help
target/release/minimap init --dry-run --agents all
```

## crates.io

Publishing to crates.io is manual because published versions are permanent and
cannot be overwritten.

The `Publish crates` GitHub Actions workflow publishes packages in dependency
order. Run it with `dry_run=true` first, then rerun with `dry_run=false`.

Manual equivalent:

```bash
cargo publish -p minimap-schemas --dry-run
cargo publish -p minimap-core --dry-run
cargo publish -p minimap-android --dry-run
cargo publish -p minimap-repo --dry-run
cargo publish -p minimap-graph --dry-run
cargo publish -p minimap-cli --dry-run
```

Then publish for real in the same order, waiting for each crate to appear in the
registry index before publishing dependents:

```bash
cargo publish -p minimap-schemas
cargo publish -p minimap-core
cargo publish -p minimap-android
cargo publish -p minimap-repo
cargo publish -p minimap-graph
cargo publish -p minimap-cli
```

## GitHub Release

Commit the release date and notes, merge the tested source into `main`, and
create an annotated tag from that exact commit. GitHub binary releases do not
require a crates.io publication; publish to crates.io separately when that
channel is requested, using the same release source and version.

```bash
git tag -a v0.2.0 -m "Minimap v0.2.0"
git push origin v0.2.0
```

The release workflow creates archives and `.sha256` files for each supported
target. Wait for all five target builds, confirm all archives and checksums are
attached, and smoke-test the downloaded binary for the local platform before
reporting the release complete. Do not retarget an already-published version.

## Homebrew Tap

Create the tap once:

```bash
brew tap-new mttmcknn/homebrew-minimap
gh repo create mttmcknn/homebrew-minimap --public --source "$(brew --repository mttmcknn/homebrew-minimap)" --push
```

For each release:

1. Copy `packaging/homebrew/Formula/minimap.rb.template` to the tap as
   `Formula/minimap.rb`.
2. Replace `__VERSION__` with the release version without the leading `v`.
3. Replace `__SOURCE_SHA256__` with the SHA-256 of the GitHub source archive.

Get the source archive checksum:

```bash
curl -L https://github.com/mttmcknn/minimap/archive/refs/tags/v0.2.0.tar.gz | shasum -a 256
```

Test the formula locally from the tap:

```bash
brew install --build-from-source mttmcknn/minimap/minimap
brew test mttmcknn/minimap/minimap
brew audit --strict --online mttmcknn/minimap/minimap
```

Then commit and push the formula in `mttmcknn/homebrew-minimap`.

Users install with:

```bash
brew install mttmcknn/minimap/minimap
```

## Version Bump Checklist

For the next release after `0.2.0`:

1. Update `[workspace.package].version` in `Cargo.toml`.
2. Update internal dependency versions in each `crates/minimap-*/Cargo.toml`.
3. Update release notes in `CHANGELOG.md`.
4. Run local verification.
5. Merge the tested release source and push the release tag.
6. Verify all GitHub binary assets and checksums.
7. Publish crates and update the Homebrew tap when those channels are requested.
