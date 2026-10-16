# Q22 Flight Lab

A local desktop app for understanding UAV flight performance and how it changes
with configuration, parameters and software. Start with the aircraft's current
settings; historical tunes and pre-FACt/FACt baselines are optional evidence.

**Flight Insights Preview 0.3.1 source:** the main workspace is now **Flights**
and **Setup**. Original logs, automatic analysis, previous-flight comparisons,
configuration evidence and recommendations are brought together in each flight.
See the [flight workspace guide](docs/flight-workspace.md) and
[current product direction](docs/current-product-direction.md).

Shared collection now discovers the connected controller UID automatically.
Manual UID entry is optional under advanced settings; existing UID locks remain
until cleared. Stop collection before switching aircraft. Missing reported UID
keeps recording available, with automatic parameter capture unavailable.

The 0.3.1 installer checks are pending. **Previous 0.3.0 downloads** (manual UID
required for automatic parameter capture):
[Windows x64 installer](https://github.com/kyl3n/flight-insights/actions/runs/38748020018/artifacts/11981671377)
· [Linux Debian package and AppImage](https://github.com/kyl3n/flight-insights/actions/runs/38748020018/artifacts/11980595928).
Extract the downloaded Actions archive to find the installer. These unsigned
evaluation archives expire on 29 October 2026. The
[native run](https://github.com/kyl3n/flight-insights/actions/runs/38748020018)
passed on both platforms at clean source `77e39a0`, including all 43 browser
cases and three fresh production WebView launches per packaged entrypoint.
Windows NSIS install/uninstall also passed. Exact results and installer checksums
are in [validation](docs/validation.md). Publisher signing, Windows dependency
notice completion, full desktop/upgrade qualification and physical field
validation remain open. Windows x64 and Linux x64 are the release targets;
macOS is deferred.

## The normal workflow

1. **Setup:** choose saved logs or a live UDP connection through your existing
   radio/router/GCS infrastructure. Serial setup stays in the field equipment or
   GCS. A return-capable app endpoint can handle both recording and automatic
   read-only parameter capture through one port.
2. **Flights:** start collecting before arming, or import original BIN, LOG or
   TLOG files. Supported flight intervals are indexed and analyzed automatically.
   Known flight dates sort chronologically; unknown dates stay clearly separated.
3. **Open a flight:** review its metrics, findings, configuration evidence and
   recommendations. Where identity and interval evidence support it, the app
   selects a compatible preceding flight and displays recent history.

[Guided setup](docs/connection-setup.md) explains addresses, ports and the return
path. Any GCS can coexist through a suitable radio/router endpoint; one-way
forwarding supports recording only. Setup and application startup send nothing.
**Start collecting** starts the saved connection and its configured readbacks.
Old receive-only profiles are not silently changed to enable aircraft requests.

Collection records available OS, Flight Lab and recognized application versions
at capture and observed arm transitions. Current software is never copied onto
old flights. Eligible independent logs can be aligned automatically after collection stops.
Complete typed readbacks and recorded environment context contribute
to a report only through supported exact flight linkage. Partial parameter logs,
unknown dates, missing identity and incomplete boundaries remain explicit.

Recommendations explain supported mechanisms and prerequisites using bundled,
revision-pinned ArduPilot references. They cover issues such as vibration,
clipping, notch tracking, control response and power observations without claiming
causation or prescribing unsupported numerical tunes. Flight Lab does not write
aircraft settings. See [performance insights](docs/performance-insights.md),
[control and filter analysis](docs/control-and-filter-analysis.md), and
[automatic parameter capture](docs/automatic-acquisition.md).

## Advanced work

**Flights → Advanced tools** retains explicit parameter/CSV imports, selected
interval and sensor analysis, saved-report comparisons, repeated-flight
experiments, offline research and review-packet export. Research updates fetch
selected official sources only when requested; no flight data is uploaded.
Collection details retain manifests, original integrity, alignment, missing
evidence and optional post-stop log acquisition. Onboard `LOG_REQUEST_*` can
suspend logging and remains a separate reviewed action.

**Setup → Connection troubleshooting** contains adapter, route, firewall,
digiQ/SkyPulse and RTSP checks. Host repairs are diagnosis-only by default;
experimental enrolled helpers retain their exact-plan approval and recovery
requirements. See [network diagnostics](docs/network-diagnostics.md),
[managed collection](docs/managed-collection.md), and
[read-only acquisition](docs/read-only-acquisition.md).

No aircraft data is fabricated. Fixtures are explicitly synthetic. The original
[design handoff](docs/handoff/PROVENANCE.md) and its hashes remain preserved.
Automatic grouping and descriptive comparisons do not establish independent
flights, a causal effect, optimal settings or a flight-readiness verdict. The
[acceptance plan](docs/acceptance-status.md) retains the remaining qualification
and product coverage work.

## Development

Use the existing checkout; this cloud task is already isolated and does not need another Git worktree. Required developer tools: Node 24, Python 3.12, uv, the pinned Rust toolchain, and the native libraries for your operating system. Installed users receive a bundled Python engine and do not need Node or Python.

```bash
npm ci
uv sync --frozen
uv run --frozen pytest
npm run build
uv run --frozen python scripts/package-engine.py
npm run desktop:dev
```

The [native build guide](docs/native-build.md) covers normal-host prerequisites, installer commands, platform CI and signing limits. `package-lock.json`, `uv.lock`, and `src-tauri/Cargo.lock` retain tested dependency resolutions. `npm run desktop:build` builds the engine before packaging the app.

### Managed cloud workspace

```bash
cd /workspace/flight-insights
python3 scripts/cloud-native-setup.py
source scripts/cloud-native-env.sh
UV_CACHE_DIR=/workspace/shared/q22-uv-cache uv sync --frozen
npm ci --cache /workspace/shared/q22-npm-cache
uv run --frozen pytest
npm run build
uv run --frozen python scripts/package-engine.py
cargo test --manifest-path src-tauri/Cargo.toml --locked
npm test
```

The setup helper downloads verified native dependencies into the workspace, requiring no system writes. Native compilation, packaging and engine tests work here. The extracted host WebKit libraries cannot launch their hardcoded system subprocesses; a separate Debian 13 container with system-installed WebKitGTK passed the earlier 0.2.0 package's GUI check without weakening security. Current 0.3.0 native GUI evidence comes from the Windows/Linux CI builds. See [cloud prerequisites](docs/cloud-native-prerequisites.md). Full installed-desktop and upgrade qualification remain separate.

UI integration tests use Chromium and a test-only transport to the real Python engine. Production exposes no browser HTTP bridge. `Q22_CHROMIUM` can select an installed Chromium executable for tests. `npm run dev` starts a loopback-only UI development server; the browser alone cannot inspect the host or import evidence.

### Headless engine

```bash
uv run --frozen python -m q22_engine --data-dir .local/development-evidence
```

Send one JSON-RPC request per line:

```json
{"jsonrpc":"2.0","id":1,"method":"handshake","params":{}}
{"jsonrpc":"2.0","id":2,"method":"network.inspect","params":{"sources":[]}}
```

Requests are limited to 1 MiB and responses to 4 MiB. Diagnostics go to stderr; originals and SQLite metadata live under the specified directory. The desktop uses its OS-specific application data directory automatically. Never use a synthetic test data directory for real aircraft baselines.

## First use

Open **Setup** if collecting live telemetry; choose **Saved flight logs** if
working from existing files. Save the settings and open **Flights**. Use **Start
collecting** before arming, or **Import flight logs** to add original files.
Select an entry to inspect its automatic insights and available comparisons.

If an entry needs aircraft grouping, or a battery-monitor choice, supply that information in its **Flight context**.
Use manual analysis for additional operating context. Unknown dates and
incomplete arm/disarm evidence are never filled from a filename or the current
computer. Use **Advanced tools** only when a custom interval, parameter export,
manual comparison or additional evidence review is needed.

Read the [workspace guide](docs/flight-workspace.md), [setup walkthrough](docs/connection-setup.md),
[validation results](docs/validation.md), and [release qualification procedure](docs/release-qualification.md)
for the implemented workflow and its tested scope.
