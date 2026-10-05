# Q22 Flight Lab

An offline desktop engineering workspace for Q22 flight evidence, host-network checks, and ArduPilot configuration comparisons. Built from the supplied [CODEX_START_HERE](docs/handoff/CODEX_START_HERE.txt) design handoff.

**Foundation preview:** this is a working first milestone, not the complete application specified in the handoff. The full [acceptance plan](docs/acceptance-status.md) remains open.

**Windows, Linux, and macOS are all required release platforms.** The common UI, local evidence store, and analysis engine must work on each. Native installers target Windows x64, Linux x64, and both Apple Silicon and Intel Macs. See the [platform support and validation matrix](docs/platform-support.md) for build commands, OS-specific capabilities, and qualification still required.

## What you can do

- Inspect the computer's adapters and routes to configured digiQ/SkyPulse, camera and UDP sources. Probe the documented SkyPulse HTTP endpoints, RTSP negotiation, and actual UDP receipt. A checked telemetry heartbeat includes CRC validation; generic packets do not establish playable video.
- Preview scoped host-route, subnet-address or inbound UDP firewall changes where supported. Review the exact commands and undo commands before applying. Administrator privileges are required for changes; existing internet default routes remain intact. Durable repair receipts survive engine restarts, and rollback checks the current settings against the recorded addition. See the [OS support and recovery guide](docs/network-diagnostics.md).
- Import actual Mission Planner or QGroundControl parameter exports. Originals and snapshots remain immutable. Compare changed, added and removed settings without writing to the aircraft.
- Associate an explicit-unit performance CSV and a selected flight interval with its actual parameter snapshot. Compare power, energy and roll tracking error alongside parameter changes and flight-condition confounders. A current readback is never treated as proof of historical settings. See the [CSV format and interpretation limits](docs/parameter-comparison.md).
- Import DataFlash BIN/LOG and timestamped MAVLink TLOG files in the background. Inspect separate sources and boot epochs, review timestamped parameter observations, and create scoped performance sessions from supported fields. Cancellation and interrupted-job recovery are visible. See [log ingestion and its evidence limits](docs/log-import.md).

No Q22 baselines or aircraft data are fabricated. Fixtures under `fixtures/` are explicitly synthetic. Logged parameter observations do not establish complete aircraft settings. Live recording, advanced DSP/PID analysis, and hardware-qualified adapters remain future stages.

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

The setup helper downloads verified native dependencies into the workspace, requiring no system writes. Native compilation, packaging and engine tests work here. Full native WebView rendering is blocked by Debian WebKit's hardcoded `/usr/lib` subprocess location in this restricted image; the extracted libraries live in the workspace. See [cloud prerequisites](docs/cloud-native-prerequisites.md). A built installer must be tested on a normal desktop with its runtime packages installed.

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

1. Open **Evidence** and import a BIN, LOG or TLOG from the flight. Review the source, boot epoch, available battery monitors, decoder warnings and parameter observations before creating an analysis interval.
2. Import actual parameter exports separately when available, recording aircraft, board, exact firmware identity, provenance and observation time. Preserve accepted original/Brian files independently. CSV imports remain available for explicitly specified measurements linked to the appropriate snapshot.
3. Open **Compare** to view parameter and measured-performance changes with evidence limits.
4. Open **Network checks**, inspect adapters, and add the actual radio/camera/telemetry addresses. For a radio at `10.0.0.2` alongside an internet connection on `192.168.0.x`, use the physically connected device adapter and an appropriate device-subnet address. The radio/GCS must send to the computer's receiving address and dedicated port. Host routing alone cannot configure the device's return path.
5. Run stream checks. Preview a relevant repair only after checking physical connections and the reported evidence. Apply a reviewed supported plan in an administrator session and recheck. Retained recovery receipts offer rollback only when the app can verify ownership of the current setting; keep the displayed manual instructions for uncertain cases.

Read [architecture decisions](docs/architecture-decisions.md), [dependency review](docs/dependency-review.md), [validation results](docs/validation.md), and the [release qualification procedure](docs/release-qualification.md) before distributing a milestone build. Windows/macOS workflows are provided, but their presence does not establish successful builds or hardware qualification.
