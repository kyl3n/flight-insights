# Q22 Flight Lab

An offline desktop engineering workspace for Q22 flight evidence, host-network checks, and ArduPilot configuration comparisons. Built from the supplied [CODEX_START_HERE](docs/handoff/CODEX_START_HERE.txt) design handoff, with the user's updated requirements recorded in [current product direction](docs/current-product-direction.md).

**Flight Insights Preview 0.2.0:** local analysis, collection, research and repeated-flight experiment workflows are available, with explicit evidence limits. The full [acceptance plan](docs/acceptance-status.md) remains open.

The current goal is to establish each flight's current parameters and software context, then explain how configuration changes relate to measured performance across flights. A new user needs no pre-FACt/FACt files or other historical baseline. Field setup centers on UDP from the existing serial/radio/GCS infrastructure; dedicated COM-port setup and direct serial support in Flight Lab are deferred.

[Earlier Windows and Linux preview — 13 October, before the 0.2.0 changes](https://github.com/kyl3n/flight-insights/actions/runs/38372096954). Both native pipelines passed for `41e1974`, including repeated production WebView launches. These unsigned evaluation artifacts expire on 27 October 2026. See the [validation record](docs/validation.md) for installer links, exact source identity and remaining desktop/device qualification.

The 0.2.0 source checkpoint passed 941 engine tests, 39 browser tests and 17 native tests. A local Debian 13 evaluation installer passed packaged-engine verification; its cloud GUI check was blocked by missing system WebKit child processes. A recovery source archive and artifact receipts accompany this checkpoint. Updated installers have not been published, and the current source still needs a Windows build and physical field qualification.

**Windows x64 and Linux x64 are the current release targets.** macOS is deferred; existing Mac build configurations remain optional for future work. The common UI, evidence store and analysis semantics stay shared. See the [platform support and validation matrix](docs/platform-support.md).

## What you can do

- Follow **Connection setup** for a local GCS, remote UDP feed, or saved-file workflow. Serial connections remain in the existing field/GCS infrastructure. The guide explains actual radio/receiver/sender addresses and ports, offers explicit checks, and saves settings into the existing collection and network tools. See the [guided setup walkthrough](docs/connection-setup.md).
- Inspect the computer's adapters and routes to configured digiQ/SkyPulse, camera and UDP sources. Probe the documented SkyPulse HTTP endpoints, RTSP negotiation, and actual UDP receipt. A checked telemetry heartbeat includes CRC validation; generic packets do not establish playable video.
- Preview scoped host-route, subnet-address or inbound UDP firewall changes where supported. Default operation is diagnosis only. An explicitly administrator-enrolled experimental Windows/Linux service can execute supported changes after per-session opt-in and exact-plan approval. Linux experimental execution currently supports host routes only with NetworkManager 1.42 or newer; aliases and firewalld changes remain manual. The helper attempts conditional rollback, which can remain blocked by changed ownership or unknown/unsafe state. Hardware qualification remains open. See the [OS support and recovery guide](docs/network-diagnostics.md).
- Prepare a separate receive-only MAVLink observer and retain raw UDP, TLOG and recorder-journal evidence. Each observed armed interval receives a durable flight record; an armed interval alone does not prove flight. Source identity, gaps, missing BIN files and incomplete parameter evidence remain explicit. See [managed collection](docs/managed-collection.md).
- Automatically preserve available host OS/kernel/distribution/architecture and Flight Lab/engine versions at capture start and each observed armed interval. Recognized running GCS/router/video apps can include Windows file-resource or checksum-matched Linux package versions, with operator declarations kept separate. Unsupported metadata stays unknown and unfinished lookups stay pending in that immutable observation; old flights are never filled from the current host. Review **Flight computer & software** in Prepare & watch and saved flight bundles.
- Import actual Mission Planner or QGroundControl parameter exports. Originals and snapshots remain immutable. Compare changed, added and removed settings without writing to the aircraft.
- Associate an explicit-unit performance CSV and a selected flight interval with its actual parameter snapshot. Compare power, energy and roll tracking error alongside parameter changes and flight-condition confounders. A current readback is never treated as proof of historical settings. See the [CSV format and interpretation limits](docs/parameter-comparison.md).
- Import DataFlash BIN/LOG and timestamped MAVLink TLOG files in the background. Inspect separate sources and clock segments, review timestamped parameter observations, and create scoped performance sessions from supported fields. A segment boundary does not confirm a reboot. Cancellation and interrupted-job recovery are visible. See [log ingestion and its evidence limits](docs/log-import.md).
- Build saved reports with vibration distributions, clipping and power findings, declared payload/PWM context, RATE/PID tracking and qualified step responses. Inspect raw or complete batch-IMU spectra and hypothetical static filter attenuation, phase and delay. Preview and export a minimized local AI review packet; no AI service receives data automatically. See [performance insights](docs/performance-insights.md) and [control/filter limits](docs/control-and-filter-analysis.md).
- Compare saved insight reports using supported metrics, observed parameter changes, mechanisms, evidence requirements and confounders. Complete typed preflight readbacks contribute all preserved values when their originals and exact flight linkage revalidate; partial logged subsets remain separately labelled. Recorded software/environment context requires a verified matching managed-flight interval. Conditional advice uses bundled, revision-pinned ArduPilot sources, including mechanical checks and supported notch-tracking approaches; it does not prescribe numeric aircraft tuning or establish causation.
- Review schema-qualified DataFlash GNSS, EKF3, ESC, scheduler and board-power observations in **Flight insights**. Per-instance metrics, source evidence, raw categorical codes and conditional investigation steps retain their limits. Unknown units, unavailable telemetry, unsupported message layouts and missing motor mappings do not become a green health result.
- Plan and evaluate **Repeated-flight experiments** with immutable revisions, explicit groups or pairs, parameter hypotheses, phase/coverage requirements, metric goals and regression limits. Distinct verified flight records count toward repeated observations; counterexamples and failed criteria stay visible. A measured improvement against a selected criterion does not establish causation or an optimum.
- Open **Research** for six selected ArduPilot topics, including vibration, sampling, notch tracking, bidirectional DShot, control response and propulsion/power. Search offline passages or explicitly update fixed official GitHub sources into a versioned local cache. Updates send no flight data or search terms; fetched passages remain unreviewed source material and do not replace the bundled reviewed advice.
- Request a parameter readback or a selected onboard log through an explicitly reviewed bidirectional MAVLink endpoint. Fresh disarmed identity and clock gates, bounded transfers and preserved partial evidence constrain these experimental operations. Legacy log transfer can suspend onboard logging; review the [acquisition workflow and recovery limits](docs/read-only-acquisition.md).
- Preserve acquired rich readbacks directly in the parameter library without export/reimport. Full typed values and acquisition originals remain linked; saved reports show qualified preflight/postflight references. A complete non-atomic enumeration does not prove reboot-dependent settings were active or exclude unbroadcast changes.
- Inspect selected SD/export folders or acquired logs for content-based flight candidates, then review provisional associations. Capture bounded raw SkyPulse response history and inspect RTP/H.265 packet framing without claiming video decode or RF-loss diagnosis.
- Save an explicit collection policy for preflight/postflight parameter readback and post-stop log listing. Current-settings guidance shows readback counts, freshness and missing evidence; an arm cycle revokes earlier readback eligibility. A selected closed log still requires confirmation before transfer. Timer-driven gates operate independently of the visible page; restarting never resumes requests. See [automatic acquisition](docs/automatic-acquisition.md).
- Match eligible logs using reported identity, clock anchors and armed intervals; ambiguous or incomplete matches remain provisional. Review a saved alignment before attaching verified spans to a flight record. File names alone never establish a match. See [evidence alignment](docs/evidence-alignment.md).
- Review catalog migration history and explicitly verify archived originals. Version 1 catalog migration preserves a verified SQLite backup before schema changes; this is not a backup of all flight files. See [catalog integrity](docs/catalog-integrity.md).

No Q22 baselines or aircraft data are fabricated. Fixtures under `fixtures/` are explicitly synthetic. Historical tunes are optional evidence; a current preflight snapshot establishes a new user's observed starting point. Passive observations and non-atomic current readbacks do not by themselves establish complete effective flight-time settings. Analysis and acquisition share a background worker; a queued preflight request is not a completed readback or a guarantee of capture before arming. Qualified GCS/field adapters, complete software-version discovery, full dynamic firmware filter parity, aircraft-specific numerical tuning and hardware qualification remain open.

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

Start with **Connection setup** (or **Set up connections** on the overview). Follow the [guided setup walkthrough](docs/connection-setup.md) to choose how data reaches the app, enter the actual addresses and ports, and save the settings. Keep existing serial setup in the GCS/field equipment and forward a separate UDP feed to Flight Lab. Saved-file analysis needs no live connection settings or historical tune.

For a new collection, continue to **Prepare & watch** and follow the [passive observer setup](docs/managed-collection.md). In **Flight computer & software**, optionally inspect the current computer and enter known ground-station/other-app versions; collection preserves its own environment observations automatically. Review and explicitly start the separate observer, keep native GCS logging enabled, and review the missing-evidence indicators. Establish current parameters before arming through the separately reviewed [bidirectional readback workflow](docs/read-only-acquisition.md), or use a contemporaneous GCS parameter export with its provenance recorded. One-way forwarding cannot request parameters. Saving setup alone never starts collection or confirms connectivity. The remaining steps cover existing files and host diagnostics.

1. Open **Evidence → Flight log**, choose the original BIN, LOG or TLOG and select **Import flight log**. When import finishes, select its recorded source and clock segment. Review battery monitors, decoder warnings and parameter observations before using **Create scoped session**. Select an actual flight/phase interval; the complete recorded range may include bench time and parameter changes.
2. Use **Evidence → Parameter snapshot** for actual current parameter exports, recording aircraft, board, exact firmware, provenance and observation time. Acquired rich readbacks can be preserved directly in the library; verified flight-linked reports use the full typed values without export/reimport. No named baseline is required. If historical **Q22 original - pre-FACt** or **Q22 with FACt tune** files are supplied, preserve them independently with their original filenames and legacy Brian aliases. **Flight metrics CSV** imports explicit-unit measurements associated with a selected snapshot. A separately imported export does not automatically replace or verify a log session's observed parameter history.
3. Open **Compare → Flight performance** for scoped sessions, or **Parameter snapshots** for export differences. Use **Flight insights** for time-localized findings and payload context. If a log interval crosses parameter observations, inspect its reported split boundaries and create separate intervals; mixed-configuration metrics remain unavailable. Guidance is advisory and firmware-specific; no settings are written to the aircraft.
4. Open **Network checks → Inspect computer**, then add the actual sources. For a radio at `10.0.0.2` alongside internet on `192.168.0.x`, verify the OS route uses the physically connected device adapter and a suitable local address. The **digiQ / SkyPulse** source type probes SkyPulse's documented HTTP endpoints; use it only if the actual device supports them. For forwarded UDP, **Device IPv4** means the actual packet sender—the GCS/router address may differ from the radio address. Configure that sender to target the computer's receiving address and a dedicated port.
5. Select **Check configured streams**. Camera checks perform unauthenticated RTSP negotiation, not playback; authentication-required results need verification in a credentials-capable camera player/GCS. Preview a relevant repair after checking the physical path and evidence. Default operation provides diagnosis and manual instructions. On an explicitly administrator-enrolled experimental Windows/Linux bench, **Enable experimental bench repairs** requires session consent; **Preview exact changes** and **Apply approved repair** require a separate exact-plan review. Linux experimental execution is limited to host routes with NetworkManager 1.42 or newer; aliases and firewalld changes remain manual. A rollback deadline is an attempt, not a guarantee: changed ownership or unknown/unsafe state can leave recovery unresolved. Hardware qualification remains open. Host routing cannot configure the device's return path. Preserve receipts and inspect **Applied repairs & recovery** before retrying.

Read [architecture decisions](docs/architecture-decisions.md), [dependency review](docs/dependency-review.md), [validation results](docs/validation.md), and the [release qualification procedure](docs/release-qualification.md) before distributing a milestone build. Native CI targets Windows and Linux; manual dispatch can include Mac builds. Consult the validation record for executed outcomes. Real-device and supported-OS qualification remain open.
