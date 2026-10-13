# Validation record

## Managed analysis and private bench qualification — 13 October 2026

The [native run for `41e1974`](https://github.com/kyl3n/flight-insights/actions/runs/38372096954)
passed on both required targets, Windows x64 and Linux x64. Its exact source is
`41e1974f1d721c05275852a599c32ec77981a208`. macOS remains a deferred future target;
the older four-target results below describe their own earlier commits.

| Target | Executed package checks | Preview download |
| --- | --- | --- |
| Windows x64, Windows Server 2022 runner | NSIS install/uninstall, packaged supervisor, three fresh production WebView launches with IPC/restart/shutdown | [Windows artifact](https://github.com/kyl3n/flight-insights/actions/runs/38372096954/artifacts/11799349643) |
| Linux x64, Ubuntu 22.04 runner | Debian and AppImage payload/supervisor; three fresh GUI launches each for Debian, extracted AppRun and actual AppImage launcher | [Linux artifacts](https://github.com/kyl3n/flight-insights/actions/runs/38372096954/artifacts/11799443597) |

Both pipelines also passed the complete engine suite, expanded frozen smoke,
native Rust checks, all 19 browser integration tests, dependency inventories and
notice checks. Every required GUI launch must pass; failures are not retried.
Artifacts include native verification reports and expire on 27 October 2026.
They are unsigned private evaluation builds, not a general-release qualification.
The delivered source archive adds documentation only after this CI commit.

The latest managed Debian 13 full engine run passed **642 tests and 29 subtests**
in 34.81 seconds, with **15 skips**: nine optional SciPy signal-reference checks
because SciPy was unavailable, three actual Windows ACL/link cases, two Windows recorder
read-handle cases, and one Windows offline-bench wrapper case. These are explicit
gaps in this local run, not passing checks. The transcript is retained as
`q22-managed-analysis-qualified-final-pytest.log` in the delivery workspace.
All **19 browser integration tests** and the TypeScript/Vite production build
also passed. These browser checks use the real engine through the test bridge;
they are separate from the production WebView checks above. The native-helper suite
passed **46 Rust tests**, rustfmt and Clippy with warnings denied. An independent
review found no remaining concrete blocker in the implemented Linux write and
rollback path; no privileged host mutation or hardware validation was performed.

Coverage now includes bounded managed acquisition, evidence alignment, catalog
migration/integrity, validated IMU batch decoding, conditional control/filter
analysis and the private helper's injected-state/protocol boundaries. Tests remain
synthetic unless stated otherwise. Implemented numerical analyses are conditional
on their recorded schema, units, clock and sampling gates; this does not establish
complete Filter Review/PID parity or tuning changes for a real aircraft. See
[acceptance-status.md](acceptance-status.md) for current scope.

The native qualification work adds a production-WebView rendering/IPC check with
isolated engine/browser data and a nonce-bound, natively owned report. Seventeen Rust
lifecycle/GUI guard tests passed locally, including retained shutdown failure and
cancelled-waiter outcomes, followed by Clippy with warnings denied. The final
frozen sidecar passed expanded smoke checks from a relocated Unicode path,
including migration, policy restart gates, real loopback acquisition, positive
alignment, batch/control/filter outputs, recorder/archive recovery and shutdown.
Both Windows/Linux workflows passed Actionlint
1.7.7. The Linux installer input tests cover read-only review, approval binding,
exclusive writes, protected/visible image paths, executable access, and FIFO
rejection. No helper installer or elevated service was run in this environment.

The workflow passed actual installed/extracted WebView verification on normal
Windows/Linux runners. The managed cloud's extracted WebKit subprocess paths
still prevent credible local GUI qualification; no sandbox or OS security was
bypassed. Native file-picker/accessibility behavior, upgrades, desktop Windows
minimums and other supported-OS boundaries still need their full matrix.
Physical SkyPulse/SIYI streams,
Mission Planner/QGroundControl coexistence, real network-manager repair/rollback,
and publisher signing remain separate checks. Read-only signing preflight found
no repository environments/rulesets; credential-name and runner metadata were
unavailable with HTTP 403, not proven absent. The user reported no Windows signing
identity. Private unsigned testing does not require one.

The separate Debian 13 preview passed payload, dependency inventory, frozen engine
and native-supervisor verification from clean source
`1d79ff4705098e71e77e4ccaddc2b989a9bc20d0`. Its file is
`Q22-Flight-Lab-0.1.0-managed-analysis-1d79ff4-linux-amd64.deb`, 49,077,834 bytes,
SHA-256 `423177c153db11d3cf0ecddd1b75213d6980dfa2a535f394e5a910658dd7ca98`.
All 320 application dependency graph entries have preserved notice texts; this
inventory is not legal sign-off. The Debian 13 runtime requirements differ from
the Ubuntu CI baseline, and its local check does not establish GUI qualification.

The initial native attempt exposed a Windows system-file hardlink test assumption,
an excessively fast synthetic telemetry fixture, and an AppImage GUI launch that
omitted its normal AppRun environment. Follow-up tests exercise Windows ACLs and
hardlink rejection separately, pace the integration peer at 5 Hz using unchanged
production freshness gates, and verify both extracted AppRun and actual AppImage
launchers. No production safety gate or WebKit sandbox was weakened.

Subsequent native diagnostics exposed two product issues: Windows performed
repeated per-rule firewall queries and exceeded its 15-second snapshot budget; a
cold-start burst could fill the desktop's eight-slot request queue. Windows now
batches filter reads while independently preserving foreign filter evidence and
directly verifying helper-owned associations. The desktop admits at most 32
pending requests with bounded, cancellation-safe queue waiting; it never replays
a dispatched operation. A 24-call frozen-engine startup regression passes.
Native CI requires three fresh successful GUI launches per packaged entrypoint;
the first failed attempt stops verification and preserves its evidence.

Loaded-runner integration tests also exposed an inappropriate 150 ms synthetic
heartbeat budget during durable catalog work. These fixtures now use unchanged
production freshness defaults. A deliberate 250 ms transcript-write pause
reproduces rejection under the short test budget and succeeds under production
timing; subsequent stale/gap/identity/boot invalidation still passes. Production
gates are unchanged. Actual Windows read-only inspection now verifies the
documented native CIM classes, rather than deriving class names from cmdlets.

Two earlier Windows packaged-verification exits did not retain actionable
diagnostics, so their specific cause remains unconfirmed. The final run uses
explicit UTF-8 for the verifier and its transcript wrapper, with bounded
exception/traceback annotations and retained failure evidence. It passed all
three Windows GUI launches and cleanup; this result does not retroactively
explain those earlier exits.

### Measured synthetic import boundaries

The full wide-record benchmark used **2,147,483,775 bytes** of valid DataFlash BIN:
9,460,275 BAT records of 24 bytes alternating with 9,460,275 PAD records of
203 bytes, plus 20 schema/identity/parameter records. PAD is a complete unknown
message whose payload is normalized and persisted, not skipped file filler.
Source-byte SHA-256 matched the immutable archive. The measured outputs were:

| Measurement | Observed result |
| --- | --- |
| Complete decode and normalized persistence | 18,920,570 events; exactly 18,920,570 Parquet rows |
| Persisted shards | 145, totaling 555,499,751 bytes |
| Full import elapsed time | 768.622 seconds |
| Peak engine RSS | 103,124,992 bytes, approximately 98.3 MiB |
| Initial import RPC | 91.0 ms |
| Maximum concurrent status RPC | 39.1 ms |
| Separate 2,147,483,743-byte source cancellation | Cancelled during archive; 0.743 ms acknowledgment, 11.576 ms to terminal state |

The cancellation case does not establish a completed decode of its own source.
The completed wide-record case establishes this synthetic mix on this managed
machine, not arbitrary 2 GiB logs, reference-laptop performance, raw-IMU/DSP
throughput, or real-flight qualification. Byte size and event density are separate
limits. The existing **20 million normalized-event guard was not raised**.

An earlier dense BAT-only attempt requested 2048 MiB, representing approximately
89 million records. It correctly failed at that event guard after **1,043.155
seconds**, with the last progress update at **19,998,720 events** and the error
“Log exceeds the supported 20 million normalized events.” It did not complete.
Its original harness removed temporary inputs before retaining their hashes, so
exact fixture/source fingerprints are unavailable; the observed terminal job,
timestamps and failure-log hash were preserved without inventing them.

The revised benchmark writes bounded structured progress/failure evidence and
source/fixture hashes before cleanup. The completed wide-run report has SHA-256
`71ccad9474829acf517bf5d171d70f7b14156b35819e715952a727c664ce7143`.
Its start/finish hashes show concurrent edits to `__main__.py`,
`collection_controller.py`, `network.py`, `network_helper.py` and
`network_helper_windows.py`. The measured importer modules `logs.py`,
`log_decoder.py`, `imu_batches.py`, `jobs.py`, `store.py` and `catalog.py` did not
change. This is evidence scoped to the launched workload and preserved hashes,
not a claim that the whole working tree stayed unchanged.

Retained workspace evidence is under `/workspace/artifacts/`:
`Q22-managed-analysis-2gib-wide-benchmark.json`, its `-qualification.json` scope
annotation, and `Q22-managed-analysis-2gib-dense-failure.json` / `.log`.
Reproduce a new measurement, using a new report path, with:

```sh
uv run --frozen python scripts/benchmark-log-import.py --size-mib 2048 --cancel-size-mib 2048 --record-mix bat-pad --report /absolute/new-benchmark.json
```

## Evidence insights and explicit acquisition — 13 October 2026

This slice adds persistent local insight reports, reviewed ArduPilot guidance, a minimized local AI review-packet preview/export, explicit read-only MAVLink acquisition, content-based folder/acquisition reconciliation, RTP/H.265 framing diagnostics and bounded raw SkyPulse history. No AI service receives evidence, no aircraft parameters are written, and production host repairs remain diagnosis-only.

The managed Debian 13 engine suite passed **446 tests and 29 subtests**; two Windows-only recorder read-handle cases were skipped locally and are included in native Windows CI. All **15 browser integration tests**, the React/TypeScript production build, **5 Rust transport tests**, rustfmt and Clippy with warnings denied passed. Desktop and compact insight views were inspected. The final browser regression confirms that a delayed review-packet reply cannot relabel or export a different report. These browser tests use the real engine through a test-only bridge; they do not establish native WebView or hardware qualification.

The expanded frozen-runtime smoke passed from a relocated Unicode path. It performs real loopback readback/retry/idempotency, actual-ID log listing/download/EOF, original-byte hashes, downloaded BIN decoding, vibration/power reports, exact preview/export bytes, and persistence after restart, alongside the existing passive recorder/archive/recovery checks. The inventory requires every new module and retains the prior dependency exclusions.

The separate Debian 13 installer passed payload, frozen-runtime and packaged native-supervisor verification from clean commit `97262bf15ae66ad50a154b275a11024aecbf6cb8`. `Q22-Flight-Lab-0.1.0-insights-linux-amd64.deb` is 48,432,426 bytes, SHA-256 `fbdb8eb081fe9fc47420bfc1a2baa163f5f56a9f51c42893c96eaa801cb30a86`. This artifact requires its declared Debian 13 dependencies; it is distinct from the Ubuntu CI baseline and does not establish installed GUI or hardware qualification.

Cross-platform follow-up corrected Windows folder scanning to read fresh non-following path metadata rather than incomplete `DirEntry.stat()` identity fields. Regression coverage retains descriptor identity, filesystem-boundary, symlink, stability and same-size/time replacement checks. Native test harnesses handle macOS `/var`–`/private/var` aliases and Windows UDP startup resets without weakening production checks. CI failure tails are split into bounded annotations so the final cause remains visible.

The [final native run for `97262bf`](https://github.com/kyl3n/flight-insights/actions/runs/38352931495) passed on all four targets. Each target ran engine tests, the expanded frozen smoke, Rust tests/lints, the frontend build, all **15 browser integration tests**, installer builds and verification through the packaged native supervisor. Windows also executed both real read-handle tests. The two longest browser workflows have explicit 90-second total budgets and independently bounded 10-second reloads; all assertions remain enforced, with no automatic retries.

| Target | Automated pipeline | Verified preview artifacts |
| --- | --- | --- |
| Windows x64 (Windows Server 2022 runner) | Passed | [NSIS `.exe`](https://github.com/kyl3n/flight-insights/actions/runs/38352931495/artifacts/11791644193) |
| Ubuntu 22.04 x64 | Passed | [`.deb`, AppImage](https://github.com/kyl3n/flight-insights/actions/runs/38352931495/artifacts/11791264241) |
| macOS 15 Apple Silicon | Passed | [`.app`, `.dmg`](https://github.com/kyl3n/flight-insights/actions/runs/38352931495/artifacts/11791393673) |
| macOS 15 Intel | Passed | [`.app`, `.dmg`](https://github.com/kyl3n/flight-insights/actions/runs/38352931495/artifacts/11791553509) |

Every artifact includes source identity, dependency inventory and a native verification report. Actions downloads expire after 14 days. Windows previews have no publisher signature; Mac previews have an ad hoc integrity seal without Developer ID signing or notarization. Mac app bundles are archived as `.app.tar.gz` to preserve executable modes and symlinks. The final source archive adds documentation only after the validated runtime commit; artifact hashes and this distinction are recorded in the delivery manifest.

New tests use explicitly synthetic logs and real loopback peers. They cover VIBE schema/units, clipping resets/gaps, time regression, selected measurement-clock bounds, power/tracking arithmetic, declared mass/geometry/PWM limits, and conditional Welch spectra against small DFT/Parseval/sine oracles. Independent review caught and fixed repeated-time integration, out-of-interval gyro samples and misleading parameter representation comparisons. Insight export tests exercise the actual imported-log report and exclude private free text, identifiers and device/calibration parameters while preserving synthetic and evidence-quality labels.

Acquisition tests verify the narrow outbound allowlist, expected UID, fresh disarmed heartbeat and boot-clock gates, revocation on arming/staleness, missing-index retries, contradictory parameter counts, exact byte coverage, explicit EOF, conflicting late retransmissions through finalization, durable partial evidence and no network replay after restart. A separate real-engine job test performs readback, downloads a synthetic real-format BIN, indexes the archived bytes and reopens the catalog. Its candidates remain provisional. Reconciliation never treats filenames, dates or system IDs as proof, and synthetic log associations to declared-live flight records are rejected.

Radio and RTP tests distinguish transport/framing evidence from RF health or video decode. Raw capture covers partial/malformed/deep JSON, slow headers, refused connections, redirect refusal, cancellation, byte limits, digest mismatch, orphan recovery and bounded Windows manifest replacement. No tests connect to Q22 hardware. Full Filter Review/PID parity, numerical tuning, verified automatic flight matching, qualified GCS adapters, native repair helpers, publisher signing and installed-app/device qualification remain open.

## Revision 2 passive collection — 12 October 2026

The new slice adds a real receive-only UDP worker, managed capture/armed-interval records, source-span associations, passive parameter evidence, archive recovery, and Q22 reference/capability reporting. Automatic host repair is now diagnosis-only in production. The complete Revision 2 acceptance plan remains open.

Local validation includes real loopback capture through the production coordinator and UI, operation replay/conflict handling, occupied UDP ports, no outbound packets, CRC/source filtering, actual receive timestamps, raw/TLOG/journal hashes, chunk spans, unknown end after shutdown, explicit archive recovery, queued cancellation, disk/fsync failures, source ambiguity, boot boundaries, unsupported parameter observations, and preservation of old manifests. Synthetic data stays in isolated catalogs and is not Q22 hardware evidence.

The frozen-runtime smoke also launches the actual separate worker from the relocated packaged executable, requires journal delivery without callback errors, validates exact archived artifact digests/roles/lengths, reimports its TLOG, verifies socket/worker cleanup, and checks restart plus explicit recovery. The native transport allowlist includes only the new typed session/association/profile operations; event injection and aircraft controls are not renderer methods.

Windows follow-up validation covers bounded checkpoint replacement when another process briefly holds a read handle, preservation of the prior checkpoint when that handle remains locked, and rejection of browser-test RPC calls during engine teardown. Generated CI transcripts are excluded from source tracking so they do not incorrectly mark packaged source inventories as modified.

| Check | Result |
| --- | --- |
| Python engine | 281 tests and 29 subtests passed locally; 2 Windows-only read-handle tests passed in native CI |
| UI integration | All 10 Playwright tests passed, including real isolated loopback collection, retained bundles and closed-transport polling regression |
| React/TypeScript | Production build passed |
| Frozen engine | Expanded worker, archive, restart and EOF recovery smoke passed |
| Native Rust | 5 tests passed; rustfmt and Clippy with warnings denied passed against the final packaged engine |
| Handoff provenance | All 11 canonical/archived source-file hashes matched; both revisions' document bytes preserved |

The [final native run for `2887703`](https://github.com/kyl3n/flight-insights/actions/runs/38318550751) passed on all four targets. Every target ran engine tests, frozen recorder/archive/restart checks, Rust tests and lints, frontend build, all ten browser integration tests, installer builds and verification through the packaged native supervisor. Windows also executed both real read-handle tests. The application and test code is identical to `19fa5a6`; the later commit only excludes generated CI logs from source tracking and updates this validation record.

| Target | Automated pipeline | Verified preview artifacts |
| --- | --- | --- |
| Windows x64 (Windows Server 2022 runner) | Passed | [NSIS `.exe`](https://github.com/kyl3n/flight-insights/actions/runs/38318550751/artifacts/11778301477) |
| Ubuntu 22.04 x64 | Passed | [`.deb`, AppImage](https://github.com/kyl3n/flight-insights/actions/runs/38318550751/artifacts/11778180312) |
| macOS 15 Apple Silicon | Passed | [`.app`, `.dmg`](https://github.com/kyl3n/flight-insights/actions/runs/38318550751/artifacts/11777818854) |
| macOS 15 Intel | Passed | [`.app`, `.dmg`](https://github.com/kyl3n/flight-insights/actions/runs/38318550751/artifacts/11778515196) |

Each artifact includes its source identity, dependency inventory and `native-verification-<target>.json`. Actions downloads have a 14-day retention period. Windows previews have no publisher signature; Mac previews have an ad hoc integrity seal, without Developer ID signing or notarization. Mac app bundles are archived as `.app.tar.gz` to preserve executable modes and symlinks.

Installed-GUI close confirmation, real GCS coexistence/failsafes, actual dual-NIC hardware, complete readbacks, automatic matched BIN retrieval, radio/video adapters and qualified repair helpers remain unpassed. Earlier run links below belong to earlier source commits and do not validate this slice.

The separately built Debian 13 preview passed packaged-runtime verification at clean source commit `19fa5a615908a06e6b5814234016b2484d48b667`. Its file is `Q22-Flight-Lab-0.1.0-passive-collection-linux-amd64.deb`, 48,282,184 bytes, SHA-256 `e3da02595915b3d0c473bdf2d857d86bf7a07cbff9faa8b4f748b5a84c95e68a`. It is distinct from the Ubuntu CI artifact and requires the declared Debian 13 runtime dependencies. The bundled engine's recorder, archival, restart and EOF checks passed again after the Windows checkpoint fix, followed by all five Rust tests and verification of the installer payload.

## Earlier flight-log preview — 5 October 2026

Local checks ran in the managed Debian 13 x86-64 environment on 5 October 2026. Native GitHub Actions results are recorded separately below. These checks establish this milestone's automated development and packaging capabilities; they do not qualify the complete handoff or real aircraft hardware.

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

At this earlier milestone, Windows/Linux/macOS were required; the [platform support matrix](platform-support.md) records the current Windows/Linux focus. The follow-up fixed deterministic SQLite connection closure, Windows binary-file handling, localized PowerShell JSON and adapter names, sparse desktop executable paths, and macOS route/source-address selection. The local engine suite, all eight UI tests and the frontend build passed after these changes. UI recovery used a deterministic host fixture; the separate real-host inspection test remained. Source and simulated-platform checks are distinct from the native CI results below.

That milestone required shared engine/UI checks, native tests, and verification of the packaged runtime on Windows x64, Linux x64, Apple Silicon and Intel Macs. It removed the original Linux-only UI test restriction. Its native bundle verification exercised the packaged shell's engine supervisor independently of graphical startup; those results are not passed GUI or hardware tests.

## Log ingestion and recovery follow-up

The new slice decodes DataFlash BIN/LOG and timestamp-prefixed MAVLink TLOG into bounded Parquet batches. Tests cover separate vehicles/clock segments, unknown units, battery sentinels, corruption boundaries, parameter changes and interval-start snapshots. Incomplete parameter observations remain explicitly incomplete; metrics spanning parameter observation changes are withheld until the user chooses separate intervals.

An independent one-off comparison against the installed pinned pymavlink 2.4.49 `DFReader_binary` and `DFReader_text` passed 248 scalar checks over the tiny synthetic fixtures. Normalized BAT and ATT values match; binary floating-point parameter representations remain distinct from text decimal spellings. DFReader is a test oracle here and is excluded from the shipped runtime. This establishes fixture parity, not arbitrary firmware or hardware qualification.

Managed-cloud benchmarks caught growing Parquet footer memory and prompted bounded output shards. With that fix, a 21,038,317-byte synthetic BIN completed in 44.13 seconds at 134,344,704 bytes peak engine RSS; a 104,891,629-byte BIN completed in 202.57 seconds at 135,077,888 bytes peak RSS. Maximum concurrent status-request latency was 93.6 ms and 33.5 ms respectively. This demonstrates stable memory over those measured sizes; it does not establish every larger-file limit. Cancellation during archival of a 2,147,734,854-byte source reached a cancelled state in 12.7 ms. That is a cancellation check, not a completed 2 GiB decode. Reproduce current-machine measurements using `scripts/benchmark-log-import.py`; results depend on hardware, source layout, caches and concurrent work.

Background job results survive restart; unfinished jobs become interrupted without replay. Durable network receipts are validated as local claims of origin and rechecked against current OS state before a separately confirmed rollback. No real host repairs were performed during this work.

## Native CI and artifact provenance

The [native run for `ebe0352`](https://github.com/kyl3n/flight-insights/actions/runs/37315922353) runs engine tests, frozen imports, Rust tests/lints, frontend build, browser integration and packaged-runtime verification on each target. The verification uses the bundled native supervisor, including restart/shutdown, from paths containing spaces and Unicode. It checks runtime payloads and notices. These are automated runtime checks; they do not establish native WebView rendering or hardware qualification.

| Target | Automated pipeline | Artifact formats |
| --- | --- | --- |
| Windows x64 | Passed on `ebe0352` | [NSIS `.exe`](https://github.com/kyl3n/flight-insights/actions/runs/37315922353/artifacts/11348971081) |
| Ubuntu 22.04 x64 | Passed on `ebe0352` | [`.deb`, AppImage](https://github.com/kyl3n/flight-insights/actions/runs/37315922353/artifacts/11347564605) |
| macOS 15 Apple Silicon | Passed on `ebe0352` | [`.app`, `.dmg`](https://github.com/kyl3n/flight-insights/actions/runs/37315922353/artifacts/11347084679) |
| macOS 15 Intel | Passed on `ebe0352` | [`.app`, `.dmg`](https://github.com/kyl3n/flight-insights/actions/runs/37315922353/artifacts/11348870261) |

macOS previews carry an ad hoc integrity seal, not a Developer ID signature or notarization. Windows previews have no publisher signature. CI uploads artifacts only after the target's verification succeeds, with a 14-day retention period. Each artifact includes its own `native-verification-<target>.json`, dependency inventory and source identity. Extract the downloaded Actions archive to find the installer and report. Mac `.app` bundles are inside `.app.tar.gz` archives to preserve executable modes and symlinks. Keep each app bundle and its bundled engine intact.

The separately built Debian 13 preview also passed packaged-runtime verification at clean source commit `fa89915e75e1d6b04ac208673c6b8074ba691595`. Its file is `Q22-Flight-Lab-0.1.0-log-preview-linux-amd64.deb`, 48,174,900 bytes, SHA-256 `91d85c6fabf257bae90699d3ea280f13d01685902b7853eb10fcff219ef8bf94`. This is distinct from the Ubuntu CI artifact and retains the Debian 13 runtime dependency requirements.

## Earlier flight-log milestone limitations

The following limits were recorded for the earlier flight-log preview. The current
managed-analysis checks and outstanding native CI are recorded at the top of this
document; current acceptance scope is in [acceptance-status.md](acceptance-status.md).

- Full native WebView rendering is blocked in this cloud: the workspace-extracted Debian WebKit library expects its renderer subprocess in the system `/usr/lib` location. No package binary or browser sandbox was weakened to bypass this. See [cloud prerequisites](cloud-native-prerequisites.md).
- The `.deb` was built and inspected, not installed and launched on a clean desktop. This local artifact requires the declared Debian/glibc runtime dependencies. Native CI execution is recorded below; publisher signing, notarization and clean-machine GUI qualification remain open.
- No real Q22 BIN/TLOG/RF bundle, accepted original/Brian parameter baselines, device stream, matched flight experiment or physical ESC mapping was supplied. No hardware-readiness, video-decoding, parameter-tuning or causal-performance claim is made.
- Full managed collection beyond the passive recorder, complete verified parameter-state reconstruction, spectrum/filter/PID parity, full migrations, completed 2 GiB decoding on a reference laptop and the rest of the handoff's acceptance families remain pending.

Source and artifact provenance are recorded separately from environment publication. A saved cloud configuration, published filesystem snapshot, Git commit and native build are distinct actions; none establishes the others.
