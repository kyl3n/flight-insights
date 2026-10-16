# Current product direction — updated 16 October 2026

Flight Lab should explain how changes to an aircraft's configuration relate to
changes in its measured flight performance, and help the operator choose the next
well-supported improvement to investigate. A new user starts from the aircraft's
current settings. No historical tune, named baseline, or prior flight is required.

This document records the user's updated requirements and takes precedence over
conflicting product assumptions in the original Revision 2 handoff. Original
handoff files, hashes, uploaded evidence and legacy aliases remain preserved.
The requirements below are acceptance targets; they are not a declaration that
every target has been implemented or qualified. See [acceptance status](acceptance-status.md)
and the executed checks in [validation.md](validation.md).

## Automatic aircraft identification in the 0.3.1 source

Manual Controller UID entry is no longer required for normal shared collection.
Flight Lab discovers and pins the reported UID for each new collection; the same
saved connection can be reused after stopping collection and changing aircraft.
An optional advanced UID lock restricts a connection to one controller. Existing
locks are preserved until cleared. Missing UID leaves recording and log analysis
available while automatic parameter capture remains unavailable. A different UID
during collection stops requests; saved flights are never reassigned silently.
System/component selection and the return-capable UDP path still apply. See
[validation](validation.md) for exact build results; 0.3.0 packages predate this change.

## Flight-centered workspace in the 0.3.0 source

The current redesign puts **Flights** and **Setup** in the primary navigation.
Collection controls and log import sit with chronological flight history. Opening
a flight brings metrics, problems, configuration evidence, available preceding-
flight comparisons, recent history and conditional suggestions into that record.
Advanced manual analysis and evidence tools remain accessible without separate
primary navigation tabs. See the [workspace guide](flight-workspace.md).

Supported logs are indexed and analyzed automatically in bounded local jobs.
The app separates aircraft sources, clock segments and observed armed intervals;
unknown dates or incomplete boundaries remain review items. Aircraft grouping
requires observed UID evidence or an explicit operator declaration, and compatible
ordering/interval evidence is required before choosing a previous flight.

After capture stops, eligible independent logs can be matched automatically using
the existing UID/firmware, supported-clock, exact-interval and original-integrity
rules. Provisional and ambiguous evidence is not upgraded. The app’s own capture
provenance is labelled separately from this independent alignment.

Guided setup now supports one bidirectional app endpoint for both recording and
parameter requests, independent of the selected GCS. The configured radio/router
must supply the actual return path. The default enabled policy refreshes stale
completed preflight captures while disarmed within a cooldown and request budget.
The app uses its own configurable MAVLink request identity and stops readback on
an observed identity collision. Old receive-only profiles stay receive-only. No parameter writes or automatic
onboard log transfer are added. Library analysis pauses during collection.

The clean 0.3.0 source `77e39a0` passed local engine, real-engine browser and native
desktop Rust checks. Its Windows/Linux native builds and packaged production GUI
checks passed on 15 October; delivery documentation was finalized on 16 October.
Exact results and installers are in [validation.md](validation.md). Earlier 0.2.0
results remain historical and do not qualify the redesigned UI or shared
transport. Physical aircraft/GCS routing and flight optimization acceptance
remain open.

## Implemented in the 0.2.0 source

The 0.2.0 source included the following slices, retained by the redesign. Windows/Linux 0.2.0 CI preview
installers passed their native builds and basic production WebView checks at clean
source `0ac9836`; downloads and executed validation are recorded separately in
[validation.md](validation.md). Desktop, hardware and flight acceptance remain open.

| Area | Implemented behavior | Still to qualify or extend |
| --- | --- | --- |
| Field setup | UDP and saved-file workflows; the COM wizard is removed and legacy serial selections migrate to the GCS/UDP workflow | Actual digiQ/GCS/serial-to-UDP field validation and qualified GCS adapters |
| Flight environment | Immutable observations at capture start and each observed armed interval include available OS/kernel/distribution/architecture and Flight Lab/engine versions. Recognized running GCS/router/video apps can receive Windows file-resource or checksum-matched Linux package versions; operator declarations remain separate | Portable/modified apps and unsupported metadata stay unknown; unfinished lookups stay pending in their original snapshot. Installed GCS/field qualification and broader runtime/device inventory remain |
| Current parameters | Pre/postflight policy guidance, decoded/expected counts, freshness and missing-evidence recovery are surfaced. Arm cycles revoke earlier eligibility. Rich readbacks preserve full typed values in the parameter library; verified exact managed-flight linkage supplies pre/post references to reports, comparisons and advice | Complete effective-state alignment and reboot/activation proof. Complete enumeration is non-atomic and does not exclude unbroadcast changes |
| Flight comparison | Saved insight reports compare supported metrics, observed parameter deltas, parameter mechanisms/evidence requirements and confounders. The existing scoped-session comparison also includes parameter explanations | Controlled repeated-flight validation, richer phase matching and causal inference. Recorded environment is included only with a verified matching managed-flight interval |
| Improvement advice and research | Reports include conditional advice from bundled, revision-pinned ArduPilot sources. Six research topics provide offline passages and explicit updates from fixed official GitHub source paths; fetched text is cached with commit/hash provenance | Updated passages are unreviewed, not automatically promoted into guidance. Coverage is selected, not exhaustive. Aircraft/firmware qualification and validated numeric optimization remain; no aircraft writes |
| Repeated-flight experiments | Immutable plans/revisions and saved evaluations support explicit groups/pairs, typed parameter hypotheses, phase/coverage requirements, metric goals, regression limits and retained counterexamples. Only distinct verified flight records count toward repeated-flight requirements | Controlled real-flight validation, statistical independence and causal inference. An observed improvement is an operator-criterion result, not proof of an optimum or airworthiness |
| Flight health observations | Schema-qualified DataFlash GNSS, EKF3, ESC, scheduler and board-rail summaries retain units, instances, bounded evidence and source citations; conditional findings identify observations to investigate | Other message layouts, per-field ESC freshness, physical motor mapping, aircraft-specific limits and native flight validation. Missing channels stay unavailable; there is no universal health pass |

Inspecting the computer from setup is optional; collection takes its own
environment observations automatically. Running-process names do not establish
versions or control-path ownership. Supported executable metadata supplies an
attributed file/package version, which can differ from the application's About
display and does not authenticate the software. Historical records never receive
current-host backfill. Complete typed readbacks are reverified against archived
acquisition artifacts; display previews do not limit comparisons to those rows.
Partial, ambiguous or mismatched evidence cannot replace missing preflight data.

Analysis, readback promotion and report comparison use cancellable background
jobs. The shared worker can queue an acquisition behind other work; queued or
failed preflight collection stays explicit. A saved policy does not guarantee a
completed readback immediately before an operator arms the aircraft.

## Field workflow and first use

The primary workflow uses the existing field serial/radio/GCS infrastructure and
a MAVLink UDP endpoint for Flight Lab alongside the GCS. A return-capable endpoint can share recording and readback within the app. Dedicated COM-port setup, serial-port
enumeration and direct serial transport in Flight Lab are deferred. The user
keeps serial setup in the working field equipment or ground station. Live setup
must explain the actual receiver address, sender address, UDP destination and
optional radio/camera management addresses without assuming example IPs.

A fresh installation should guide the user through:

1. Identify the aircraft and how telemetry reaches this computer; explain what
   has actually been detected and what the user has declared.
2. Record the software and equipment context, retaining unknown fields explicitly.
3. Before arming, establish the aircraft's current parameters through a configured
   and explicitly reviewed bidirectional path while fresh disarmed identity and
   clock evidence permit readback. A saved readback policy can automate later
   requests within its approved capture and request budget.
4. Show parameter-readback progress, missing values, conflicting observations,
   observation time and freshness. If the feed is one-way, explain that passive
   telemetry cannot request a full readback. Guide the user to a
   bidirectional radio/router route or a contemporaneous parameter export from the GCS.
5. Record the flight and retain the configuration and environment evidence linked
   to it. Collect postflight settings and onboard logs when available; preserve
   partial collection and clear recovery instructions when evidence is absent.
6. Review the first flight against its own measured metrics and supported guidance.
   Offer comparisons as later flights and setting changes accumulate.

Missing preflight evidence must remain visible without preventing passive
recording. Flight Lab does not decide aircraft arming or readiness and does not
write aircraft parameters. An imported export is a declared observation at its
recorded time, not automatic proof of the configuration effective at arming.

The historical **Q22 original - pre-FACt** and **Q22 with FACt tune** files are
optional comparison evidence. If supplied, retain their bytes, filenames and
legacy Brian aliases. Their absence must never block onboarding, collection,
analysis or a comparison between two newer flights.

## Parameter evidence over a flight's lifetime

Each flight needs a durable reference to its own parameter evidence: aircraft
identity, firmware/build evidence, source, acquisition interval, decoded and
expected counts, missing/conflicting values, and the original archived artifact.
Retain full readback artifacts as well as individual observed changes. A
count-complete enumeration is non-atomic: values arrive over an interval and may
change during capture.

Distinguish the last observed value, a stored value, and a value known to be
effective. Some changes need a reboot or other firmware-specific activation.
Record evidence of those transitions; an uncorroborated clock reset or a later
readback cannot establish them. Parameter traffic, identity/firmware changes,
restarts, gaps and freshness expiry must invalidate assumptions where appropriate.
Preserve uncertainty when no evidence can establish an effective value.

Before and after each flight, compare actual snapshots rather than requiring a
permanent reference tune. A preflight readback can serve as that flight's observed
reference and later comparisons can choose any compatible prior flight. Do not
promote a postflight/current snapshot into a missing historical preflight state.
Split analysis at supported configuration-change boundaries or withhold metrics
that would mix configurations. Repeated arm/disarm intervals need distinct
records; a single capture may contain more than one flight candidate.

## Flight environment and version provenance

Every new flight should retain an immutable environment snapshot for the capture
that observed it, including where available:

- Host OS name, version/build and architecture.
- Flight Lab version/build, analysis engine and relevant runtime versions.
- Ground-station application name and exact version; additional relevant field
  applications and versions, such as forwarding, camera or logging software.
- Controller firmware/build, hardware identity and relevant radio/ESC firmware
  when reported or explicitly supplied, with their source retained.
- The connection configuration and operator declarations needed to interpret the
  evidence, without inventing detected settings.

Every value needs its observation/declaration source and capture time. Label
detected, operator-declared, imported and unknown values distinctly. A MAVLink GCS
heartbeat does not disclose an exact Mission Planner/QGroundControl version.
Browser platform information also does not establish an exact OS release. Ask
for missing context in the normal setup workflow; do not substitute guesses.

Keep the environment used for analysis separate from the environment used during
flight. Importing an old log on a new machine must not stamp that machine's OS or
installed apps onto the historical flight. Old records without environment
evidence remain unknown. New declarations or corrections are retained as
attributed additions rather than silently overwriting archived observations.
Changing a setup preference affects later captures, not prior flights.

Comparisons should surface environment changes alongside parameter changes.
Software context may affect telemetry quality, logging, latency and operations;
its presence alone does not prove that host software changed the aircraft's
onboard control performance. Link an environment change to a performance claim
only when the evidence supports that mechanism.

## Explanations, research and improvement recommendations

The app should connect a parameter change to the relevant physical/control
mechanism, explain which metrics are expected to change, and compare that
expectation with the actual observations. Preserve units, phase, source, interval,
sample coverage and sensor limitations. Report association and uncertainty
honestly; two flights with different settings do not establish causation.

Research should cover the plausible solutions for a supported problem, including
mechanical, instrumentation, operating-condition and configuration causes. Start
from version-appropriate upstream documentation and firmware source. Preserve a
versioned, reviewable guidance library and its citations for offline use; retain
the library/source revision with each saved report. An evidence packet or research
request must be explicit about any external data transfer. Coverage grows through
reviewed additions; the product must not claim to have researched every possible
solution or to provide live online research when it has not done so.

Each actionable recommendation should contain:

- The observed finding and supporting evidence, including missing evidence that
  limits the conclusion.
- A causal mechanism or hypothesis, clearly distinguished from a demonstrated
  cause, and the parameter families or hardware checks involved.
- Applicable firmware/board/ESC capabilities and unresolved prerequisites.
- Sources with version scope and review date; distinguish documentation-backed
  guidance from aircraft-specific measured evidence.
- Alternatives, tradeoffs, contraindications and expected measurable outcomes.
- A practical validation experiment: what to capture, what to hold comparable,
  what improvement or regression would look like, and how to restore the prior
  configuration through the operator's existing tooling.

Exact parameter values require sufficient aircraft, firmware and measurement
evidence. When that evidence is absent, recommend the next measurement or
conditional option and explain why. Recommendations remain advisory; Flight Lab
does not apply aircraft settings.

For vibration, investigate propeller/motor balance, mounting, resonance, sensor
clipping and spectral coverage before treating filtering as a solution. Review
static, throttle-tracked, RPM/ESC-telemetry-tracked and FFT-driven notch options
where the actual firmware supports them. Bidirectional DShot can provide ESC RPM
telemetry on compatible hardware/firmware; it is a possible input to a dynamic
notch, not a filter by itself. Verify board/output support, ESC capability, motor
mapping and telemetry validity before recommending a transition from
throttle-based tracking. Explain how notch center, bandwidth, attenuation,
harmonics and update behavior affect residual noise, delay and processor budget.
Filtering cannot undo sensor clipping or repair a mechanical fault.

## Completion criteria

| Scenario | Required result |
| --- | --- |
| Fresh install with no historical files | User can configure a field UDP feed and establish a current-settings starting point without any pre-FACt/FACt prerequisite |
| One-way forwarding only | Recording works; full readback is marked unavailable and the guide explains an appropriate alternative |
| Disarmed bidirectional readback | Actual source/identity/time/completeness are preserved and surfaced; no aircraft settings are written |
| Parameters change after readback or need reboot | Staleness/effective-state uncertainty is shown; affected flight intervals are not silently treated as one confirmed configuration |
| GCS, OS or app version changes | Each capture retains its original environment evidence; comparisons show the change and its provenance |
| Historical log without environment data | Flight environment stays unknown; current analysis-host details remain separate |
| New flight after a parameter change | Comparison links compatible configuration and metric evidence, highlights other differences, and explains uncertainty |
| Vibration or another supported poor metric | Report offers evidence-linked, sourced options with capability prerequisites, tradeoffs and a validation plan |
| Missing firmware, spectra, RPM or configuration evidence | Report explains the gap and recommends the next useful capture instead of presenting unsupported numeric tuning |
| Installed field workflow | Windows/Linux bench validation covers actual GCS/digiQ routing, disconnects, repeated flights and a matched evidence bundle |

Version 0.3.1 adds automatic aircraft identification to the evaluation build with flight-centered analysis, shared
collection, evidence, comparison, research and experiment workflows. Complete software inventory, effective
configuration reconstruction, firmware-exact dynamic filter simulation and
aircraft-specific numeric tuning remain separate work. Building an installer and
passing synthetic tests do not complete installed-platform or field qualification.
