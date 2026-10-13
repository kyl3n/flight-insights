# Q22 Flight Lab

An offline desktop engineering workspace for Q22 flight evidence, host-network checks, and ArduPilot configuration comparisons. Built from the supplied [CODEX_START_HERE](docs/handoff/CODEX_START_HERE.txt) design handoff.

**Evidence-insights preview:** local analysis and collection workflows are available, with explicit evidence limits. The full [acceptance plan](docs/acceptance-status.md) remains open.

**Windows, Linux, and macOS are all required release platforms.** The common UI, local evidence store, and analysis engine must work on each. Native installers target Windows x64, Linux x64, and both Apple Silicon and Intel Macs. See the [platform support and validation matrix](docs/platform-support.md) for build commands, OS-specific capabilities, and qualification still required.

## What you can do

- Inspect the computer's adapters and routes to configured digiQ/SkyPulse, camera and UDP sources. Probe the documented SkyPulse HTTP endpoints, RTSP negotiation, and actual UDP receipt. A checked telemetry heartbeat includes CRC validation; generic packets do not establish playable video.
- Preview scoped host-route, subnet-address or inbound UDP firewall changes where supported. Automatic execution is disabled until a qualified native repair helper provides narrow privileges and supervised rollback. Exact manual recovery instructions and earlier repair receipts remain available. See the [OS support and recovery guide](docs/network-diagnostics.md).
- Prepare a separate receive-only MAVLink observer and retain raw UDP, TLOG and recorder-journal evidence. Each observed armed interval receives a durable flight record; an armed interval alone does not prove flight. Source identity, gaps, missing BIN files and incomplete parameter evidence remain explicit. See [managed collection](docs/managed-collection.md).
- Import actual Mission Planner or QGroundControl parameter exports. Originals and snapshots remain immutable. Compare changed, added and removed settings without writing to the aircraft.
- Associate an explicit-unit performance CSV and a selected flight interval with its actual parameter snapshot. Compare power, energy and roll tracking error alongside parameter changes and flight-condition confounders. A current readback is never treated as proof of historical settings. See the [CSV format and interpretation limits](docs/parameter-comparison.md).
- Import DataFlash BIN/LOG and timestamped MAVLink TLOG files in the background. Inspect separate sources and clock segments, review timestamped parameter observations, and create scoped performance sessions from supported fields. A segment boundary does not confirm a reboot. Cancellation and interrupted-job recovery are visible. See [log ingestion and its evidence limits](docs/log-import.md).
- Build saved reports with timestamped vibration/clipping findings, scoped power/tracking metrics, declared payload and motor-output context, exploratory raw-gyro spectra and reviewed ArduPilot references. Preview and export a minimized local AI review packet; no AI service receives data automatically. See [performance insights](docs/performance-insights.md).
- Request a parameter readback or a selected onboard log through an explicitly reviewed bidirectional MAVLink endpoint. Fresh disarmed identity and clock gates, bounded transfers and preserved partial evidence constrain these experimental operations. Legacy log transfer can suspend onboard logging; review the [acquisition workflow and recovery limits](docs/read-only-acquisition.md).
- Inspect selected SD/export folders or acquired logs for content-based flight candidates, then review provisional associations. Capture bounded raw SkyPulse response history and inspect RTP/H.265 packet framing without claiming video decode or RF-loss diagnosis.

No Q22 baselines or aircraft data are fabricated. Fixtures under `fixtures/` are explicitly synthetic. Passive observations and current readbacks cannot establish complete flight-time settings. Independently verified automatic flight matching, qualified bidirectional GCS adapters, full DSP/PID analysis, aircraft-specific numerical tuning and hardware qualification remain open.

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

For a new collection, start with **Prepare & watch** and follow the [passive observer setup](docs/managed-collection.md). Configure a dedicated forwarding destination first, keep native GCS logging enabled, and review the missing-evidence indicators. The remaining steps cover existing files and host diagnostics.

1. Open **Evidence → Flight log**, choose the original BIN, LOG or TLOG and select **Import flight log**. When import finishes, select its recorded source and clock segment. Review battery monitors, decoder warnings and parameter observations before using **Create scoped session**. Select an actual flight/phase interval; the complete recorded range may include bench time and parameter changes.
2. Use **Evidence → Parameter snapshot** for actual parameter exports, recording aircraft, board, exact firmware, provenance and observation time. Preserve accepted **Q22 original - pre-FACt** and **Q22 with FACt tune** files independently, retaining original filenames and legacy Brian aliases. **Flight metrics CSV** imports explicit-unit measurements associated with a selected snapshot. A separately imported readback does not automatically replace or verify a log session's observed parameter history.
3. Open **Compare → Flight performance** for scoped sessions, or **Parameter snapshots** for export differences. Use **Insights** for time-localized findings and payload context. If a log interval crosses parameter observations, inspect its reported split boundaries and create separate intervals; mixed-configuration metrics remain unavailable. Guidance is advisory and firmware-specific; no settings are written to the aircraft.
4. Open **Network checks → Inspect computer**, then add the actual sources. For a radio at `10.0.0.2` alongside internet on `192.168.0.x`, verify the OS route uses the physically connected device adapter and a suitable local address. The **digiQ / SkyPulse** source type probes SkyPulse's documented HTTP endpoints; use it only if the actual device supports them. For forwarded UDP, **Device IPv4** means the actual packet sender—the GCS/router address may differ from the radio address. Configure that sender to target the computer's receiving address and a dedicated port.
5. Select **Check configured streams**. Camera checks perform unauthenticated RTSP negotiation, not playback; authentication-required results need verification in a credentials-capable camera player/GCS. Preview a relevant repair after checking the physical path and evidence. The current release provides diagnosis and manual instructions; it does not execute host repairs. Host routing cannot configure the device's return path. Preserve earlier receipts and their manual recovery instructions.

Read [architecture decisions](docs/architecture-decisions.md), [dependency review](docs/dependency-review.md), [validation results](docs/validation.md), and the [release qualification procedure](docs/release-qualification.md) before distributing a milestone build. Native CI runs on all four OS/CPU targets; consult the validation record for exact source commits and outcomes. Installed-GUI and real-device qualification remain open.
