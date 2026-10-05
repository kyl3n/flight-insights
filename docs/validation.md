# Validation record — foundation preview

Executed in the managed Debian 13 x86-64 environment on 5 October 2026. These checks establish the first milestone's development capabilities; they do not qualify the complete handoff or real aircraft hardware.

| Check | Observed result |
| --- | --- |
| Python engine suite | 135 tests passed after log ingestion and durable recovery, with 14 additional passing unittest subtests |
| React/TypeScript production build | Passed |
| UI integration/recovery | 8 Playwright tests passed |
| Native Rust transport/lifecycle | 5 tests passed, including real packaged-engine handshake/restart and descendant cleanup after launcher crash |
| Rust lint/format | Clippy with warnings denied and rustfmt checks passed |
| Frozen Python runtime | Packaged executable passed handshake, engine status, parameter storage, BIN/LOG/TLOG background imports, scoped Parquet metrics, Unicode paths and clean EOF exit; inventory excludes unused GPL/LGPL helpers and optional fastcrc |
| Installer payload | Linux Debian package built; extracted payload and SHA-256 verified in a path containing spaces and Unicode; the packaged native supervisor passed handshake, status, restart and shutdown |
| Reusable cloud setup | `bash scripts/cloud-install.sh` completed successfully using frozen npm/uv/Cargo lockfiles and verified package/toolchain downloads |
| Actual host observation | Read-only Linux inspection enumerated interfaces, subnet routes and the default route; no host repairs were performed |

## What the tests exercise

The Python suite covers immutable archive publication, duplicate-byte identity and corruption, rejected special files, catalog reopening, real-format synthetic MP/QGC exports, parameter differences, incompatible identity, missing evidence, interval interpolation, long gaps, calibration/sign warnings and condition-based confounders. Network tests cover the dual-NIC example, wrong route selection, incomplete visibility, confirmation and stale plans, exact rollback ownership and uncertain command outcomes.

Four transport tests use actual loopback TCP/UDP sockets for SkyPulse-shaped JSON responses, RTSP DESCRIBE, generic UDP receipt and protection of an existing UDP listener. Payloads are synthetic. They validate transport behavior, not any particular radio or camera.

UI workflows exercise browser capability denial, parameter import and persistent diff, snapshot-linked performance comparison, actual host inspection, and original BIN/TLOG import with scoped session construction through a test-only native transport bridge to the real engine. The persistence test also checks 360px/640px layouts and ensures reload does not automatically probe devices. Recovery UI testing uses explicitly synthetic native/network boundary replies, so confirmation and uncertain-outcome behavior can be exercised without changing host settings.

Privileged OS mutations, including the new macOS scoped route/address plans and durable recovery, are tested with injected command runners. Their execution on real Windows/Linux/macOS hosts and multi-NIC hardware behavior have not been qualified. Effective firewall policy remains unknown where it cannot be established.

## Cross-platform audit follow-up

The user's Windows/Linux/macOS requirement is recorded in the [platform support matrix](platform-support.md). The follow-up fixes deterministic SQLite connection closure, Windows binary-file handling, localized PowerShell JSON and adapter names, sparse desktop executable paths, and macOS route/source-address selection. Linux engine tests passed after these changes; all six UI tests and the frontend build passed with portable per-run screenshot paths. These source and simulated-platform checks do not establish native Windows/macOS execution.

CI now requires shared engine/UI checks, native tests, and verification of the packaged runtime on Windows x64, Linux x64, Apple Silicon and Intel Macs. The original Linux-only UI test restriction has been removed. Native bundle verification exercises the packaged shell's engine supervisor independently of graphical startup; its result is not a passed GUI or hardware test.

## Log ingestion and recovery follow-up

The new slice decodes DataFlash BIN/LOG and timestamp-prefixed MAVLink TLOG into bounded Parquet batches. Tests cover separate vehicles/clock segments, unknown units, battery sentinels, corruption boundaries, parameter changes and interval-start snapshots. Incomplete parameter observations remain explicitly incomplete; metrics spanning parameter observation changes are withheld until the user chooses separate intervals.

An independent one-off comparison against the installed pinned pymavlink 2.4.49 `DFReader_binary` and `DFReader_text` passed 248 scalar checks over the tiny synthetic fixtures. Normalized BAT and ATT values match; binary floating-point parameter representations remain distinct from text decimal spellings. DFReader is a test oracle here and is excluded from the shipped runtime. This establishes fixture parity, not arbitrary firmware or hardware qualification.

Managed-cloud benchmarks caught growing Parquet footer memory and prompted bounded output shards. With that fix, a 21,038,317-byte synthetic BIN completed in 44.13 seconds at 134,344,704 bytes peak engine RSS; a 104,891,629-byte BIN completed in 202.57 seconds at 135,077,888 bytes peak RSS. Maximum concurrent status-request latency was 93.6 ms and 33.5 ms respectively. This demonstrates stable memory over those measured sizes; it does not establish every larger-file limit. Cancellation during archival of a 2,147,734,854-byte source reached a cancelled state in 12.7 ms. That is a cancellation check, not a completed 2 GiB decode. Reproduce current-machine measurements using `scripts/benchmark-log-import.py`; results depend on hardware, source layout, caches and concurrent work.

Background job results survive restart; unfinished jobs become interrupted without replay. Durable network receipts are validated as local claims of origin and rechecked against current OS state before a separately confirmed rollback. No real host repairs were performed during this work.

## Unpassed or unrun release checks

- Full native WebView rendering is blocked in this cloud: the workspace-extracted Debian WebKit library expects its renderer subprocess in the system `/usr/lib` location. No package binary or browser sandbox was weakened to bypass this. See [cloud prerequisites](cloud-native-prerequisites.md).
- The `.deb` was built and inspected, not installed and launched on a clean desktop. This local artifact requires the declared Debian/glibc runtime dependencies. Windows/macOS build workflows exist but were not executed here; signed installers, notarization and clean-machine qualification remain open.
- No real Q22 BIN/TLOG/RF bundle, accepted original/Brian parameter baselines, device stream, matched flight experiment or physical ESC mapping was supplied. No hardware-readiness, video-decoding, parameter-tuning or causal-performance claim is made.
- Live recording, complete verified parameter-state reconstruction, spectrum/filter/PID parity, full migrations, completed 2 GiB decoding on a reference laptop and the rest of the handoff's acceptance families remain pending.

Source and artifact provenance are recorded separately from environment publication. A saved cloud configuration, published filesystem snapshot, Git commit and native build are distinct actions; none establishes the others.
