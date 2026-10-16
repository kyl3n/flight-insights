# Development checkpoint — 16 October 2026

Current development source is **0.3.1**, with automatic controller UID discovery
and an optional advanced UID lock, at clean build commit
`0c3f6b3edd3472d80255dd3ce0ca8f0b8affd189` on
`codex/flight-insights-auto-aircraft`. The
[replacement Windows/Linux native run](https://github.com/kyl3n/flight-insights/actions/runs/38894511694)
is in progress. The initial Windows run passed all three GUI launches but failed
during temporary WebView cache cleanup; the corrected verifier waits up to 15
seconds for transient Windows locks without retrying any GUI check. See
[validation.md](validation.md) for the preserved failure and current delivery status.

## Current product and workflow

The main navigation now has **Flights** and **Setup**. The two-step setup flow
configures live UDP or saved-log use. Serial setup stays in the existing field
radio/GCS equipment; there is no COM-port wizard. One app UDP endpoint can record
telemetry and perform bounded read-only parameter capture when the radio/router
provides a return path and a supported nonzero controller UID is reported.
Manual UID entry is optional in 0.3.1; existing UID locks remain until cleared. An existing GCS
socket is not shared or taken over. A one-way feed still supports recording.
Saving setup and starting the app send nothing; **Start collecting** activates
the saved connection and its configured readback policy.

Flights combines collection controls, log import and chronological flight
history. Original BIN/LOG/TLOG files are preserved and supported source, clock
and armed intervals are indexed and analyzed automatically. Each flight opens
its metrics, findings, configuration/software context, conditional suggestions,
compatible previous-flight comparison and recent history. Unknown dates,
incomplete boundaries and unsupported identity stay explicit. Duplicate bytes
do not add an independent flight. Advanced manual analysis, comparisons,
experiments and research remain available under **Advanced tools**. See the
[workspace guide](flight-workspace.md) and [setup walkthrough](connection-setup.md).

A new user starts from the aircraft's current settings. Historical pre-FACt/FACt
tunes remain optional. Collection preserves available OS, Flight Lab and
recognized application versions at capture and observed arm transitions, with
separate operator declarations; old flights never receive today's host versions.
The shared connection can refresh stale completed preflight readbacks while
fresh disarmed evidence permits it, within a cooldown and request budget. Arming,
identity collisions and evidence gaps stop requests while recording can continue.
Flight Lab does not write aircraft parameters or guarantee a complete snapshot
before the operator arms.

Full typed readbacks remain distinct from logged parameter observations and
confirmed effective settings. Own-capture UUID/hash/record-span provenance can
show recorded environment, but cannot alone prove the independent alignment
required for complete flight-linked configuration. After collection stops,
eligible independent imported logs can be aligned automatically under the
existing identity, clock, exact-interval and original-integrity checks. Ambiguous
or incomplete evidence remains for review. Library analysis pauses during live
collection; cached reports remain available.

The 0.2.0 analysis features remain: sourced parameter explanations, conditional
improvement options, six selected offline research topics with explicit official
source updates, repeated-flight experiments, vibration/control/filter analysis,
and schema-qualified GNSS/EKF3/ESC/scheduler/board-rail observations. Comparisons
are descriptive. Neither synthetic tests nor an observed metric change establish
causation, an optimal tune or an aircraft readiness verdict.

## Previous verified delivery — 0.3.0

Local final-source checks passed 983 engine tests and 29 subtests (15 documented
platform/dependency skips), all 43 distinct browser cases across full/focused
runs, 17 desktop Rust tests, production frontend, formatting/lint and frozen-engine
smoke. Both native CI pipelines additionally passed their full browser suite and
packaged production GUI checks requiring exactly **Flights** and **Setup**.
Windows verified NSIS install/uninstall; Linux verified Debian and AppImage
entrypoints. These are unsigned evaluation artifacts. Exact platform results and
remaining notice/signing limits are recorded in [validation.md](validation.md).

The clean executable-source archive is
`/workspace/artifacts/Q22-Flight-Lab-0.3.0-source-77e39a0.zip`, SHA-256
`4aeeedb5d5abbf777ace46aac7bf6587a06e47c7dbb7c04c655baac5ce005aa6`.
It precedes this documentation-only update. Native evidence is retained under
`/workspace/artifacts/native-ci-38748020018/`.

The earlier 0.2.0 Windows/Linux artifacts and Debian 13 Docker GUI recheck are
historical. They do not qualify 0.3.0. The managed cloud host's extracted WebKit
sysroot still lacks its system child-process paths; passing current native GUI
receipts come from the Windows/Linux CI hosts with their installed runtimes.

## Where to continue

After the 0.3.1 native build passes, run installed-desktop and field qualification
against that exact installer.
Record the actual Windows/Linux host, Mission Planner or QGroundControl version,
digiQ sender/management addresses, app UDP endpoint and return path, automatically
observed controller UID and camera URL. Use [release qualification](release-qualification.md) and the
[offline bench scripts](native-release-checks.md) to retain concrete receipts.
Minimum desktop OS, native file selection, permissions, accessibility, upgrades,
actual GCS coexistence and physical device behavior remain unqualified.

Begin the field check with a fresh data directory and no historical tunes.
Establish current settings while disarmed, record one flight, then repeat after
a deliberate operator-controlled configuration change. Retain actual software
versions and a matched parameter/BIN/TLOG bundle. Import the independent log,
check its exact linkage, and inspect automatic comparisons in flight details.
Compare compatible phases and conditions before attributing a metric change to
a parameter. Also test one-way telemetry: recording should work while unavailable
readback remains clearly explained.

Remaining engineering scope includes unsupported software/version sources,
complete effective parameter history and reboot/activation proof, multi-chunk
automatic attachment, broader health/research coverage, dynamic firmware/filter
parity and validated aircraft-specific numeric optimization. Mechanical vibration,
clipping and capability checks remain prerequisites to filter advice. See
[acceptance status](acceptance-status.md) for the full scope and
[current product direction](current-product-direction.md) for the requirements.

The supplied handoff ZIP contained specifications only; available flight fixtures
are synthetic. No matched real Q22 flight bundle has been supplied. Publisher
signing, application distribution terms and the recorded dependency-notice gaps
remain release gates. The earlier release-metadata audit found no protected
release-signing environment or active release-tag ruleset; runner/variable
readiness was unavailable and remains unknown. Recheck before signed delivery.

## Recovered-session provenance

The restored checkout and refreshed `origin/main` both identified
`c1f0ee6ac6393fbdfc5aadadfe6ba6c1a7b5bcb4`; the apparent 27-commit lead came from a
stale remote-tracking reference. No uncommitted tracked source changes were
found at that resumption. This establishes the recovered source checkpoint,
not the cause of the reported chat/cloud-publishing failure or recovery of the
lost conversation. The initial connection wizard and 0.2.0 follow-up are retained
in Git history and the historical sections of [validation.md](validation.md).

Keep original handoff files and hashes unchanged. Update current docs and
validation separately. Source pushed to GitHub, a published cloud workspace,
packaged installers and a signed release are distinct checkpoints.
