# Acceptance status

Version 0.3.1 source adds automatic controller UID discovery to the flight-centered Flight Insights Preview. Manual UID entry is an optional advanced lock; current native build status is in [validation](validation.md). The [current product direction](current-product-direction.md) records the user's 15 October 2026 requirements and supersedes conflicting assumptions in the supplied Revision 2 handoff; preserve the original handoff files unchanged. Update this document with executed checks; a test fixture or CI configuration is not a hardware-validation result. Stage 1 is not complete.

Windows x64 and Linux x64 are the current required release targets. macOS is deferred by the user's latest instruction; optional Mac build configurations remain available. See the [platform support matrix](platform-support.md).

The main workspace is **Flights** and **Setup**. A chronological library brings
collection, log import, automatic analysis, compatible preceding-flight
comparisons, recent history and conditional recommendations into each flight.
Unknown dates and incomplete intervals remain explicit; identical originals are
not counted as independent flights. Advanced analysis and evidence tools remain
available without additional primary navigation tabs. See the
[flight workspace guide](flight-workspace.md).

The [guided connection setup](connection-setup.md) explains a shared app UDP
endpoint, one-way feeds, optional radio/camera checks and saved-file use. The
shared endpoint can record telemetry and request parameters when the actual
radio/router supplies a return path. Standard collection refreshes stale completed
preflight captures while disarmed within its cooldown and request budget. Saving
setup sends nothing; **Start collecting** activates the configured policy.
Old receive-only profiles remain receive-only. Existing serial
connections remain in the field/GCS infrastructure; dedicated COM setup and
direct serial transport in Flight Lab are deferred.
It saves shared settings without starting capture or changing host settings.
The COM wizard has been removed; legacy serial selections migrate to the GCS/UDP
workflow. Qualified GCS adapters and physical-device validation remain open;
these changes do not complete Stage 1 or establish physical compatibility.

A new user needs no pre-FACt/FACt files or prior flight. The intended starting
point is a current parameter capture before each flight, linked to immutable
software/environment context and later performance comparisons. One-way feeds
cannot request parameters; unavailable preflight evidence must remain explicit.
Version discovery, effective parameter history and evidence-backed improvement
advice are qualified separately rather than inferred from a successful recording.

The current source automatically retains immutable environment observations at
capture start and each observed armed interval: available host OS/kernel,
distribution, architecture and Flight Lab/engine versions, recognized running
flight-app names, and separate operator declarations. Supported Windows file
resources and checksum-matched Linux package metadata add attributed executable
versions. Unavailable or pending metadata remains explicit, and earlier flights
receive no current-host backfill. Current-settings guidance exposes readback
counts and freshness, including revocation across arm cycles. Complete typed
readbacks are preserved in the parameter library and used by reports, comparisons
and advice only with reverified originals and exact supported flight linkage.

Saved insight comparisons and scoped-session comparisons explain supported
parameter changes; reports add conditional advice from bundled, revision-pinned
ArduPilot sources. Research provides six selected topics, offline passages and
explicit cached official-source updates. Repeated-flight experiments preserve
plans, revisions, goals, regressions, verified flight counts and counterexamples.
Schema-qualified health observations cover GNSS, EKF3, ESC, scheduler and board
rails without assigning a universal pass. Recorded environment context requires
verified matching managed-flight linkage. The app's own archived telemetry can
show its recorded environment through exact capture provenance, labelled
separately from independently verified flight association. Eligible independent
logs can be aligned automatically after collection stops; ambiguous or incomplete
matches remain for review. Complete readback association retains the independent
content, identity, clock and original-integrity requirements.

The 0.3.1 identification change passed 989 local engine tests, 29 subtests,
44 browser cases and 17 desktop Rust tests. Four additional cleanup regression
cases passed within the 15-case local qualification-script suite. Current native
installer qualification is tracked in [validation.md](validation.md), including
the initial Windows temporary-cache cleanup failure after three passing GUI launches.

The previous 0.3.0 clean source `77e39a0` passed 983 engine tests, 29 subtests, 43 distinct
real-engine browser cases and 17 native desktop Rust tests locally. The 15 engine
skips retain their platform/dependency reasons. Its Windows/Linux native run
passed on 15 October, including both full browser suites and packaged production
GUI checks requiring exactly Flights and Setup. Delivery documentation was
finalized on 16 October. The 0.2.0 installers remain historical.
Use [validation.md](validation.md) for exact checks and remaining notice, signing,
desktop and hardware gates; this feature description is not a qualification
result.

| Handoff stage | State | Remaining exit work |
| --- | --- | --- |
| 0 — Foundation | In progress | Transactional migration with a verified SQLite backup and explicit original-hash verification implemented. Clean-machine upgrade qualification and distribution review of per-target dependency inventories remain; automated checks are recorded in [validation.md](validation.md) |
| 1 — Collection/review | Partial slice | Shared UDP recording/readback, immutable environment observations, supported executable-version adapters, bounded pre/postflight orchestration and refresh, full typed readback preservation, selected log transfer and automatic eligible independent-log alignment available. Field/native adapter qualification, unsupported software inventories, multi-chunk automatic attachment and complete effective parameter history/reboot proof remain |
| 2 — Evidence workspace | Partial comparison slice | Chronological flight library, automatic bounded analysis, compatible previous-flight comparisons, recent history, identity/clock/interval alignment, verified attachments and immutable repeated-flight experiment evaluation implemented. Broader segmentation/charts, issue lifecycle and controlled real-flight experimental validation remain |
| 3 — Control/filter/performance | Partial local insight slice | Vibration distributions/exposure, power/PWM context, RATE/PID tracking and qualified step response, complete batch-IMU spectra, static filter equations, sourced advice, cached research and schema-qualified GNSS/EKF3/ESC/scheduler/rail observations available. Dynamic firmware/filter parity, broader health/research coverage, validated numeric optimization and real-data engineering review remain |
| 4 — Native/live health | Partial host-network slice | Versioned Q22 reference, cross-platform diagnosis, exact repair previews and durable recovery available. Separate administrator-enrolled Windows/Linux helpers support explicitly approved experimental bench repairs; default installations remain diagnosis-only. Hardware qualification, MAVFTP and video decoding remain; direct USB/serial transport is deferred |
| 5 — Release qualification | Not passed | Full mandatory acceptance plan, publisher-signed installers, accessibility and reference-laptop benchmarks. The cloud synthetic 2 GiB import result and its event-count limitation are recorded in [validation.md](validation.md) |

## Current acceptance priorities

- A fresh install can record a first flight and establish current settings without
  historical named baselines. Before-flight readback uses an explicitly reviewed
  bidirectional path while disarmed; one-way or incomplete evidence produces
  usable guidance instead of a false complete status.
- Setup and Flights provide the normal workflow. Original logs produce bounded
  automatic analysis and supported comparisons in flight details; missing
  identity, dates or boundaries must not manufacture a chronological flight pair.
- Each newly collected flight retains its capture's OS, app/runtime and declared
  GCS/other-app context with provenance. Unknown historical versions stay unknown,
  and editing settings for a later capture does not rewrite earlier evidence.
- A complete current readback retains all typed values and original hashes.
  Promotion and report comparison run as cancellable jobs. Library analysis
  pauses during collection; other submitted work can still queue a readback.
  Queued/failed requests cannot be
  represented as complete or as a guarantee of flight-time configuration.
- Supported parameter changes have evidence-linked performance comparisons with
  mechanisms, source/version scope, confounders and uncertainty visible.
- Improvement advice offers conditional alternatives, prerequisites, tradeoffs
  and validation steps. Vibration guidance includes mechanical/clipping review
  and supported notch tracking options, with hardware/firmware/RPM evidence
  required before recommending bidirectional DShot as a telemetry source.
- Windows/Linux field qualification covers the actual serial-to-UDP chain,
  disconnects, repeated flight intervals and configuration changes. See the
  detailed scenarios in [current product direction](current-product-direction.md).

Research updates are explicit network actions and do not send flight data or
search terms. Retrieved passages remain unreviewed source material; they cannot
silently replace the bundled reviewed advice. Experiment results retain failed
criteria and cannot establish causation or statistical independence. Health
summaries require known schema/units and retain categorical codes without an
arithmetic mean; missing data is unavailable, never healthy by default.

## Evidence needed for field qualification

- One matched Q22 BIN, TLOG and both-radio raw-response bundle, with source identities and exact firmware.
- Current preflight and postflight parameter evidence matched to the flight, with
  acquisition time, identity, completeness and consistency recorded. Historical
  **Q22 original - pre-FACt** and **Q22 with FACt tune** files are optional; preserve
  them and their legacy Brian aliases if supplied, but never require or invent them.
- Verified Cube/H7 profile and ESC mappings, battery calibration and flight conditions.
- Exact Windows/Linux and Mission Planner/QGroundControl versions, actual destinations/ports, A8 Mini URL/protocol and physical dual-NIC topology for bench tests. User-declared devices are DigiQ SkyPulse and SIYI A8 Mini; exact flight-controller firmware remains unknown.

Independent development can continue using explicitly synthetic fixtures. Unsupported evidence must remain unavailable rather than green/healthy.
