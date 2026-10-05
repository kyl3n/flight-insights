# Native builds in the managed cloud machine

The cloud machine is Debian 13 on x86-64, with no administrator access. The local
helper installs Rust and Linux desktop development dependencies under
`/workspace/shared`; it does not change system packages, repositories, firewall
rules, or network routes.

From the existing checkout:

```bash
cd /workspace/flight-insights
python3 scripts/cloud-native-setup.py
source scripts/cloud-native-env.sh
rustc --version
cargo --version
pkg-config --modversion webkit2gtk-4.1 gtk+-3.0 libsoup-3.0
```

Run installation when native builds are idle. Source the environment helper in
each new shell before Cargo, Tauri, or a built Linux executable. The helper sets
the local executable, package metadata and shared-library search paths, a writable
Cargo cache, and four Cargo build workers. It does not bake cloud-specific runtime
paths into distributed executables. Keep the existing checkout: each cloud task
already has an isolated environment and does not need a Git worktree.

The installer reads the exact version in `src-tauri/rust-toolchain.toml` (currently
Rust 1.99.0). It downloads rustc, cargo, the standard library, rustfmt and Clippy
from `static.rust-lang.org`, checks the manifest SHA-256 and every component hash,
then uses the official component installers with a local prefix. TLS verification
remains enabled.

Linux prerequisites are WebKitGTK 4.1, GTK 3, Ayatana AppIndicator, librsvg,
OpenSSL, patchelf, Xvfb and xauth. The installer creates isolated APT configuration, lists and
cache directories, copies the host's package-status metadata, and resolves only
the missing development packages and necessary updates. Debian's supplied archive
keyring verifies Release signatures; APT verifies package hashes. Packages are
downloaded over HTTPS and extracted with `dpkg-deb`; no package maintainer scripts
run. Development-library stubs reuse runtime libraries already on this Debian
machine where appropriate. This local sysroot therefore belongs to this machine
image, not to an arbitrary Linux host.

Downloaded artifacts and verification receipts are retained in
`/workspace/shared/q22-toolchain`; extracted Linux files are in
`/workspace/shared/q22-sysroot`. Repeat runs reuse verified artifacts. Debian
security packages may advance when the installer refreshes signed indexes.
Required setup destinations are `static.rust-lang.org`, `deb.debian.org`, and
`security.debian.org`; Cargo also uses `index.crates.io` and `static.crates.io`.
Existing proxy and CA configuration is preserved. No credentials are required.

Validated in this machine:

- Two complete installer runs succeeded, including reuse of cached artifacts.
- Rust/Cargo 1.99.0, rustfmt and Clippy execute successfully.
- Package discovery resolves WebKitGTK 2.54.0, GTK 3.24.49 and Soup 3.6.5.
- A C program including WebKitGTK headers compiled, linked, and executed, reporting
  WebKitGTK 2.54.0. Its runtime dependencies resolved with the helper activated.
- `xvfb-run -a xdpyinfo` started a virtual display and verified X.Org 21.1.16.

These checks establish compiler/header/library readiness. Application Cargo checks,
frontend tests and desktop builds are separate checks.

A further WebView render smoke test reached GTK startup but could not launch
`/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1/WebKitNetworkProcess`. Debian's WebKit
release hardcodes that system subprocess path, while this setup extracts packages
under `/workspace`. A read-only namespace mapping was also unavailable because
the managed sandbox disallows nested user-namespace UID mapping. No browser
sandbox was disabled and no package binary was patched. Actual native WebView
startup therefore remains unverified here; use a Linux image with system-installed
WebKitGTK, or the intended Windows/macOS development machine, for that check.
The helper supplies writable XDG cache/config/data locations under the workspace.

On a normal Debian/Ubuntu development machine with administrator access, install
the supported system prerequisites instead of using this cloud sysroot:

```bash
sudo apt-get update
sudo apt-get install build-essential pkg-config curl file libwebkit2gtk-4.1-dev \
  libayatana-appindicator3-dev librsvg2-dev libssl-dev patchelf
```

Use the repository's pinned Rust toolchain and its normal desktop scripts on that
host; do not source the cloud helper there. See the upstream
[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for platform-specific
Windows and macOS setup.

Real UDP, camera, digiQ, firewall and flight-hardware validation requires the
corresponding operating system and connected equipment.
