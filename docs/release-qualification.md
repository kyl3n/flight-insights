# Release qualification and bench procedure

This is an executable test plan, not a record that these checks passed. A pilot
build means a labelled evaluation build with an explicit capability list. It does
not qualify Q22 flight operation or complete the supplied handoff. Record actual
results in [validation.md](validation.md) and attach the evidence described below.

The [current product direction](current-product-direction.md) supersedes older
baseline and COM-setup assumptions. New users start with current preflight
settings; historical pre-FACt/FACt files are optional evidence. Existing field
equipment/GCS retains serial setup and Flight Lab receives UDP. Direct serial
transport and macOS remain deferred.

## Result and artifact identity

Every test record must include the source commit, dirty-tree status, application
and engine versions, installer SHA-256, CPU architecture, exact OS build, machine
model, test date, operator, input hashes and parser/algorithm versions. For CI,
retain the actual job URL, logs and `native-verification-<target>.json`. A workflow
file or a passing result for an earlier executable is not current qualification.

Use **PASS**, **FAIL**, **NOT RUN**, or **UNSUPPORTED** with a reason and recovery
instruction. Unsupported functionality cannot count as a passed required test.
Keep synthetic fixtures distinct from captured device evidence. Record errors and
unexpected outputs; do not replace expected values to make a failing case pass.

| Recorded evidence | What it establishes |
| --- | --- |
| Linux engine, UI and native supervisor test results in `validation.md` | Shared calculations, mocked OS adapters, loopback protocols and supervised bundled IPC for the recorded source revision |
| Extracted Debian package and native CLI verification in the cloud | Payload identity and native supervisor operation from the packaged layout; no graphical installation claim |
| Native CI job results linked in `validation.md` | Executed target builds and packaged-runtime checks at the recorded commits. A recorded `--require-gui` result adds basic production WebView rendering and native IPC evidence; it does not establish full desktop or hardware qualification |
| Synthetic parameter, metric and protocol fixtures | Parser/arithmetic regressions; no real Q22, radio, camera or firmware validation |

New decoder, repair or release code needs its own recorded results. Never carry a
previous milestone's passing count forward as evidence for a later build.

## Delivery gates and available evidence

Keep the following outcomes separate. Completing a local build does not complete
the installed-platform or hardware gates.

| Gate | Work that can be completed in the development workspace | Additional evidence needed |
| --- | --- | --- |
| Reviewable source | Finish the declared features, pass their targeted/full regression checks, preserve handoff hashes, and create a source archive with per-file hashes | Review of any remaining unsupported capability before calling the full specification complete |
| Linux evaluation artifact | Freeze source, build the matching frozen engine and Linux native shell, package with the correct distribution configuration, and verify the actual bundle | Clean installed tests on each advertised Linux distribution, offline use, upgrades, accessibility and hardware behavior |
| Windows evaluation artifact | Build the frozen source on Windows, run shared/native tests, verify actual NSIS install/uninstall and packaged WebView, and retain checksums/reports | Clean installed tests on each advertised desktop Windows version, exact WebView2 version, offline use, upgrades, accessibility and hardware behavior |
| Publisher-signed release | Review protected release configuration and prepare exact artifacts/checksums without exposing signing material | Authorized publisher keys, protected signing runners/environment/tag rules and final signature verification on the signed artifacts |
| Qualified flight insights | Exercise supported algorithms, provenance and failure cases with clearly synthetic fixtures | Matched real pre/post parameters, BIN/TLOG, exact firmware/hardware/software context and controlled repeated flights where a benefit is claimed |
| Qualified field operation | Implement bounded UDP/readback/recovery workflows and deterministic failure tests | Actual GCS/digiQ/serial-to-UDP topology, camera/radio capabilities, disconnect/reconnect and host sleep/permission evidence |

At the 15 October development audit, the attached handoff ZIP contained
specification documents only. The available flight/parameter fixtures were
synthetic; no matched real-flight bundle was supplied. The later
[0.2.0 native build](https://github.com/kyl3n/flight-insights/actions/runs/38732778585)
passed on Windows/Linux at clean source `0ac9836`, including actual package and
basic production WebView checks. The separate Debian 13 container GUI recheck
also passed. Exact artifacts and limits are in [validation.md](validation.md).
These results do not complete the installed-desktop or field gates. The Windows
dependency inventory retains three unresolved notice texts; resolve them and the
application's distribution terms before release. Recheck recorded observations
before release instead of treating them as permanent capability claims.

The read-only `scripts/release-preflight.py` inspects GitHub protection, variable
names and runner metadata without reading secret values, dispatching workflows
or publishing. The 15 October inspection found no `release-signing` environment
or active release-tag ruleset. Variable and runner metadata were unavailable;
their readiness remains unknown. Private-key readiness requires the separate
protected-runner preflight. An unsigned local evaluation build can proceed
without claiming publisher signing.

## Freeze source and preserve delivery identity

Coordinate source edits before packaging so the tested code, frozen engine and
frontend belong to the same checkpoint. Do not build against files being edited
by another worker. Build the frozen engine on the corresponding OS/architecture;
Linux cannot produce the Windows PyInstaller runtime by changing a target flag.

Use the existing package and verification tools for installers. The managed
Debian 13 build must use `src-tauri/tauri.debian13.conf.json`; it does not qualify
the Ubuntu 22.04 CI baseline. Preserve the bundle verification report even when
a native GUI check is unavailable or fails. Never replace a failed GUI result
with a browser-test pass.

After the source checkpoint is stable, create a separate source delivery:

```bash
python3 scripts/package-source.py --output /workspace/artifacts/Q22-Flight-Lab-0.2.0-source.zip
```

Choose a new output name for each delivery. The helper includes tracked and
untracked source files, records the base Git commit, index identity and dirty-tree
state, and preserves each file's bytes/hash/executable mode in
`SOURCE-MANIFEST.json`. Tracked executable modes come from the Git index when
`core.filemode=false`. Its source-tree digest covers the ordered file manifest;
a sidecar receipt hashes the ZIP. Ignored dependencies, generated runtime/build
outputs and local evidence are excluded. Private/unfamiliar paths, symbolic links,
unmerged index entries and existing output files cause refusal.

Pause all source writers before capture and keep them paused until delivery
finishes. The helper checks file contents and identities, Git state and source
paths again before publishing, and rejects changes it observes. These checks do
not lock the checkout or create an atomic filesystem snapshot; an uncoordinated
writer can still race the final check. A successful archive is not evidence that
concurrent editing was safe.

Retain the source ZIP, manifest and checksum alongside installer checksums and
verification reports. A source archive alone does not prove an installer was
built from it. If documentation changes after the executable build, record that
distinction and show that executable source is unchanged; never label a dirty
working tree as the base commit alone.

## Native platform qualification

Run on Windows x64 and Linux x64 independently. macOS Apple Silicon and Intel cases below are deferred reference procedures, not current release gates.
Use a disposable VM for installation tests and a physical machine for USB/LAN and
sleep/resume tests. Build each frozen Python runtime on its matching OS and CPU.

| Platform | Required evidence |
| --- | --- |
| Windows x64 | Clean installed desktop Windows version(s) declared supported, plus exact WebView2 version. The Windows Server 2022 CI runner alone does not qualify a desktop Windows release. Establish the supported minimum before advertising it. |
| Linux x64 | Ubuntu 22.04 CI baseline and the intended desktop distribution with its declared WebKit/GTK/glibc packages. A local Debian 13 package uses the Debian 13 override and does not establish Ubuntu compatibility. Verify `.deb` and AppImage separately. |
| macOS arm64 and x64 (deferred) | Native architecture, declared macOS 13.0 floor and a current supported OS version. Verify embedded Mach-O minimums and actual installed execution; a macOS 15 build does not prove macOS 13 compatibility. |

1. Start with no Q22 installation or prior application data and no separately
   installed Python, Node or uv. Record the VM snapshot and prerequisite state.
   For Windows automated NSIS verification, use only a disposable clean runner:
   installation also affects uninstall registration, shortcuts and possibly the
   shared WebView2 runtime. A temporary destination is not complete isolation.
2. Verify the artifact hash. Run `scripts/verify-native-bundle.py` with
   `--require-gui --gui-repetitions 3` on the matching build host and retain its report. This checks
   the real packaged native supervisor plus basic production WebView rendering
   and native IPC, restart and shutdown. Complete the graphical, permission,
   accessibility and physical-device steps below separately.
3. Install through the intended package flow. Launch from the normal desktop
   shortcut/Finder/application menu, with no developer shell environment. Check
   the native file picker, fonts, 200% scaling, keyboard focus and error dialogs.
4. Disconnect Internet access. Import a labelled synthetic parameter file and a
   supported synthetic log through paths containing spaces and non-ASCII names.
   Check original byte hashes, source identities, persisted scope and comparison
   output. Close and reopen the app; repeat with a relocated install where the
   platform supports it. No external runtime or account should be needed.
5. Repeat imports using duplicate bytes under a second filename, a changed file
   with the same filename, a read-only input and a file that changes during
   import. Equal bytes may deduplicate raw storage; observations keep their own
   identity. A changing input must not produce a completed trusted manifest.
6. Exercise an unwritable data directory, insufficient disk space, an engine
   crash during import and restart after interruption. Verify that completed
   originals remain intact, incomplete work is explicit, no failed mutation is
   replayed automatically, and no orphan analysis process remains. Keep a copied
   test catalog for recovery; do not perform this against sole original evidence.
7. Upgrade a populated test installation and reopen all sources, snapshots and
   sessions. Verify hashes and earlier results. Reject a catalog newer than the
   running engine. Test uninstall/reinstall and document the actual data-retention
   behavior before offering any data-removal control.
8. Check every native permission failure: non-administrator repair, occupied UDP
   port, denied macOS Local Network access and missing OS route tools. The UI must
   show unavailable/partial evidence and a specific recovery action. It must not
   silently elevate, disable security controls or label missing data healthy.

Use the full shared engine suite on every target. A mocked Windows or macOS command
runner on Linux checks construction and interpretation, not native execution.

## Log and parameter evidence qualification

For each real format, retain a redistributable reference sample or a locally held
sample with provenance, SHA-256, exact firmware/dialect and a reproducible expected
result from a pinned trusted reader. Record permission to distribute samples;
flight logs can contain location, operator and device-identifying information.

Begin Q22 qualification with one matched controller BIN/LOG, GCS TLOG and actual
parameter readback, plus the exact board/build, flight interval, battery monitor
mapping/calibration and recorded flight conditions. Import logs through
**Evidence → Flight log** and a plain `.param`/`.params` export through
**Parameter snapshot**. App-acquired rich readback JSON is retained as an original
acquisition artifact. Promotion into the catalog as a typed parameter snapshot
and association with an exact verified managed flight are implemented. Verify
original hashes, typed values, full index/count coverage, firmware/component
identity and the acquisition's capture evidence before treating a snapshot as
usable for full comparison. Partial or conflicted captures must retain their
limitations. A bounded parameter preview is not the stored full parameter set.

Choose one source and clock segment before creating each scoped session. Attach
pre/postflight readbacks only through freshly verified aircraft identity,
alignment evidence, temporal membership and the exact managed-flight record
span. A standalone import does not establish that association. Even a complete,
flight-associated readback is non-atomic and does not prove that reboot-dependent
values were active throughout flight; effective configuration remains unknown.
Keep the association and capture evidence with the sample bundle.

| Scenario | Required observable result |
| --- | --- |
| BIN/textual DataFlash schemas change | FMT/FMTU/UNIT/MULT applicability retained; fixed-point scaling applied once; unknown fields and units remain unresolved |
| Multiple battery/IMU/ESC instances | Distinct instance keys and explicit selection; no merged pack energy or unverified motor mapping |
| Two MAVLink systems/components or capture links in one TLOG | Distinct source tracks and parameter histories; GCS-sent traffic is not silently assigned to the vehicle. Preserve the raw timestamp and state the exporter-specific basis of any capture-link interpretation. |
| Boot timer reset, wrap, reordered packets or repeated timestamps | Recorded per-source clock-segment/ordering evidence; no integration across an unresolved discontinuity. A conservative segment split is not proof of a reboot. |
| TLOG receive time vs onboard boot time | Both domains retained when available; radio/GCS latency is not represented as an exact clock alignment |
| Unknown/sentinel/NaN values, invalid CRC or truncated frames | Explicit exclusions/corruption counts, bounded recovery or a rejected import; no zero-filled healthy samples |
| Parameter changed during the selected interval | Before/after timeline and applicable interval, or abstention. Final/current settings cannot be assigned to the entire historical flight. |
| Incomplete parameter enumeration or missing firmware | An incomplete/unverified snapshot, with scope and unavailable compatibility; no fabricated baseline or full readback claim |
| Missing calibration, phase, sample coverage or matched conditions | Measurements retain their exclusions and interpretation limits; no causal efficiency or tuning conclusion |

Compare decoded values with the pinned reference reader before checking derived
metrics. Check the first/last raw record, byte offset or record identifier, source
ID, boot epoch and units for each selected signal. Preserve original bytes and
parser versions even when an analysis is rerun. Generated fixtures can exercise
all branches, but real Q22 logs remain necessary to validate the target hardware.

If supplied, preserve “Q22 original - pre-FACt” and “Q22 with FACt tune”
as separate immutable sources, retaining original bytes and legacy Brian aliases.
They are optional and must not block a fresh installation or current-flight
comparison. Do not reconstruct a missing original. Validate
Cube Orange+ and DEF H7 independently, including firmware build, sensor IDs,
motor/output/ESC mapping and battery calibration. One modified flight does not
establish causality; matched repeated flights and flight-level uncertainty are
required before claiming a tuning benefit.

## Descriptive flight-health qualification

The implemented health report summarizes bounded, schema-qualified DataFlash
observations in five domains. Qualify each domain against the pinned upstream
definitions and an independent expected result, then repeat with real target
logs. Record firmware, source/clock scope, units and multipliers, raw record
references, parser/algorithm versions, sample exclusions and source revision/hash.
Matching field names do not validate the exact installed firmware build.

| Domain | Required observable result |
| --- | --- |
| GNSS: GPS/GPA | Receiver instances stay separate; fix codes, satellite counts, dilution and reported accuracy retain their documented meanings. Binary/text fixed-point values agree after one scaling application. Receiver fix alone does not establish navigation use or position integrity. |
| Estimator: XKF3/XKF4 | EKF3 cores stay separate; signed innovations retain their units. Logged consistency indicators and fault/timeout masks remain descriptive. Crossing 1 does not establish a configured failsafe, raw covariance or a unique failure cause. |
| ESC | Motor speed, current, voltage and temperature require qualified units. Compare only complete observed instance sets at exact shared boot timestamps within the retained frame bound. Duplicate, missing or truncated instance evidence must not create an apparently complete spread. |
| Scheduler: PM | Long-loop fractions use each recorded window's valid counts; load uses its recorded percent scaling. Missing/unknown loop-time units remain unavailable. A long loop does not by itself prove a lost control update. |
| Board power/temperature: POWR/MCU | Retain qualified rail/temperature samples and raw power flags. Board topology and acceptable limits remain aircraft-specific; sampled minima and log endings do not establish a brownout. |

Exercise missing fields, unknown units, contradictory scaling, sentinels,
nonfinite/oversized values, duplicate/reordered times, clock boundaries and
corruption. Preserve explicit exclusions and gaps without inventing healthy
zeros. Enumerations and bitmasks expose code counts with no arithmetic mean;
their numeric ordering is not a health score. Verify memory bounds and visible
channel/finding/frame truncation on long or adversarial inputs.

ESC telemetry support, per-field freshness, expected instance count and physical
motor/output mapping remain unknown without separate evidence. Withhold ambiguous
zero-valued optional fields. Shared log timestamps do not establish simultaneous
fresh sensor measurements, and an observed spread does not identify a motor
fault. In both reports and the UI, unavailable data must remain unknown rather
than green/passed. An observed domain is not an airworthiness verdict.

## Current-settings and software-context qualification

Start with a fresh data directory and no historical tunes. Exercise a one-way
forwarded feed and a separately configured bidirectional path as distinct cases.
Record the actual GCS version and its forwarding controls; examples in the UI
must never become automatically detected addresses or capabilities.

1. Establish current parameters while disarmed through the reviewed path. Verify
   identity, times, decoded/expected counts, conflicts and freshness. One-way
   forwarding must explain why a full readback cannot be requested while still
   allowing passive recording.
2. Change a parameter or invalidate freshness after readback. Exercise repeated
   arm/disarm intervals and a readback crossing an arm cycle using a simulator or
   fixture. Earlier capture eligibility must not silently carry into the next
   flight. Reboot-required values remain uncertain until supported evidence
   establishes activation.
3. Inspect and record environment context, then change setup declarations or
   software between captures. Each earlier flight must retain its original
   detected and operator-declared evidence. A recognized process name is not an
   exact version or proof of control-path ownership.
4. Import a historical log without recorded host context. Its environment must
   remain unknown; current analysis-host data cannot fill that gap. Link recorded
   context only through verified source, clock and matching managed-flight scope.
5. Compare two compatible scoped reports. Verify parameter changes, metric units,
   sample coverage, other context changes and source/version-scoped explanations.
   Mixed configuration, changed payload/calibration and insufficient coverage
   must remain visible in interpretation.
6. Review an improvement option against pinned sources and the exact controller/
   ESC firmware. Require explicit prerequisites, alternatives, tradeoffs and a
   measurement plan. For notch/RPM/bidirectional-DShot advice, inspect mechanical
   faults/clipping, actual telemetry and supported board/output capabilities.
   Missing inputs must not yield an unsupported numeric tune or a causal claim.

The minimum field evidence is one correctly matched flight bundle. Establishing
a measured improvement requires comparable repeated flights, not just successful
software tests or one before/after pair.

## Research and repeated-flight experiment qualification

Explicit official-source updates, an offline research cache and immutable
repeated-flight experiment plans/evaluations are implemented. Their presence
does not establish complete solution coverage or a validated aircraft tune.

1. Open and search research while offline. Bundled reviewed excerpts and cached
   source notes must remain available with revision, retrieval time and content
   hashes. Trigger an update explicitly and verify that only the supported
   official documentation paths are requested; flight data, parameter values,
   identifiers and local search terms must remain local.
2. Exercise timeout, cancellation, rate limiting, oversized/invalid responses,
   partial updates and damaged cache entries. Preserve completed immutable notes
   and show the update outcome. Newly fetched passages remain unreviewed for the
   app and unvalidated for the aircraft; an update must not replace pinned
   reviewed advice, change applicability or apply parameter values. Confirm that
   supported topics are presented as bounded coverage rather than all possible
   performance solutions.
3. Save an experiment with explicit reference/candidate report IDs, flight phase,
   parameter hypothesis, metric units, desired changes, regression limits,
   coverage threshold and minimum repeated-flight count. A revised plan must
   create a new linked revision; evaluation must preserve earlier plans and
   results. All selected observations, failed criteria and counterexamples remain
   available for review.
4. Evaluate comparable repeated flights with verified aircraft/flight identities
   and the planned minimum number of distinct flights per group, never fewer
   than two. Multiple
   intervals from one flight cannot count as independent repeats; a flight cannot
   appear in both groups. Require consistent units, acquisition/calibration and
   algorithm semantics, matched phase and sufficient known sample coverage.
   Missing identity, inadequate coverage or mixed synthetic/real evidence must
   block an improvement result rather than silently dropping observations.
5. Check full typed preflight parameter hypotheses against their preserved,
   flight-associated readbacks; separately supported logged observations retain
   their narrower scope. Unverified or mismatched planned settings must remain
   explicit. Neither path establishes activation of reboot-dependent settings.
   Explicit matched pairs must cover every selected report exactly once; group
   medians or paired changes must preserve adverse observations and confounders.
6. Independently calculate goal/regression outcomes, including a zero-change
   goal, a failed regression limit, a counterexample and insufficient evidence.
   An observed improvement requires a positive change in each requested goal
   direction and all declared decision criteria. It remains descriptive evidence;
   it does not prove causation, statistical independence or an optimal tune.

These procedures and synthetic results do not supply missing matched real-flight
data or verified publisher signatures. The separate current-source Windows build
and basic GUI checks are recorded in [validation.md](validation.md); they do not
replace installed-desktop, device or flight qualification. Retain those remaining
gates until their actual evidence is recorded.

## Dual-network hardware bench

Use a stationary test rig with flight actuation disabled and one controlling GCS.
No arming, mode, mission, parameter-write or log-delete operation is needed. Keep
the Internet and device networks separate; use administrator-approved addresses.

| Component | Example configuration; replace with the actual recorded values |
| --- | --- |
| Internet adapter | `192.168.0.20/24`, gateway `192.168.0.1` |
| Dedicated device adapter | Verified unused `10.0.0.10/24`; no default gateway |
| Ground digiQ / SkyPulse | `10.0.0.2`, actual firmware recorded |
| Air radio / camera | Their actual configured addresses, firmware and RTSP path; do not invent defaults |
| App UDP observer | Dedicated forwarded destination `10.0.0.10:14551`, with the real sender IP configured |

Before each test, save the OS interface, address, route and relevant firewall
state, the GCS forwarding configuration, cable/topology diagram and device return
destination. Snapshot the disposable host where possible. Record Internet access
with one approved endpoint and observe the default route before and after every
repair; do not use a subnet sweep to discover addresses.

1. Start the controlling GCS. Inspect the host in Q22 without running probes.
   Confirm that no device traffic is generated by inspection, the Internet route
   still uses `192.168.0.1`, and route/source selection for `10.0.0.2` uses the
   dedicated adapter. A host without the device subnet must report the limitation.
2. Enable probes for explicitly configured sources. Retain HTTP endpoint/status
   evidence, RTSP negotiation and received UDP counts. JSON access is not RF
   health; SDP is not decoded video; a heartbeat is not complete authenticated
   telemetry. Use **digiQ / SkyPulse** only when the device supports the documented
   SkyPulse endpoints; other modules need their actual UDP/RTSP source type.
   For forwarded UDP, configure the GCS/router's actual sending IP, which may
   differ from the radio's IP. RTSP `401`/`403` means authentication is required;
   this probe does not negotiate credentials. Verify authenticated playback in
   the supported player/GCS and retain it as separate evidence.
3. Occupy the intended UDP port using a test receiver/GCS forwarding socket.
   Q22 must report the binding conflict and leave that receiver working. Move Q22
   to an unused dedicated forwarding port, then verify expected-sender packets
   arrive and other senders do not become the configured source's evidence.
4. Disconnect the device cable, stop forwarding, deny local-network access, or
   use a wrong test destination one fault at a time. Record the observed failure
   and recovery. An empty receive window cannot uniquely diagnose a firewall or
   RF problem. Restore the baseline between cases.
5. On a disposable host, preview each supported repair. Check exact destination,
   interface, address, persistence, commands and rollback before confirming it.
   A host route is `/32`; an alias must not replace an existing address, DHCP
   lease or Internet gateway; a firewall exception must stay within its stated
   source/destination/port/interface scope. Unsupported OS operations must stop
   explicitly. Do not change firewall state globally or enable routing/NAT.
6. Change the test adapter/route between plan and apply. The stale plan must fail
   without mutation. Also test lack of privilege, a pre-existing identical rule,
   command timeout and successful-command/failed-observation paths. Unknown
   outcomes retain recovery information and must not be reported as success.
7. After applying an addition, verify its exact owned state, retry the configured
   stream probe, then roll back. Compare host state with the saved baseline.
   Edit the test addition externally and confirm rollback refuses to remove a
   different route/address/rule. Preserve unrelated routes and firewall entries.
8. Test the durable repair journal by interrupting execution before dispatch,
   after dispatch and before verification; restart the app. It must reconstruct
   typed intentions safely, re-observe host state and require fresh confirmation.
   It must never execute command text loaded from an editable journal, reapply an
   old plan automatically, or infer ownership from a saved success flag alone.
   Test a tampered, truncated, wrong-host and outdated journal. A local receipt
   is an unauthenticated claim of origin, not proof that this app created matching
   state. Where ownership cannot be established, preserve the manual instructions
   and report the limitation; do not mark automatic rollback passed.
9. Close/reopen the app, sleep/wake the host and reconnect adapters. Check GCS
   continuity, permission behavior and stale observation labels. After the final
   rollback verify Internet access and the original routes/firewall state again.

Run this matrix on all supported operating systems with the actual OS adapter.
Record any unsupported repair as an outstanding requirement, not a successful
macOS or Windows hardware test. Actual stream decoding and glass-to-glass latency
need their own camera/player measurements; none is established by these probes.

## Signing, notices and pilot handoff

The project owner must supply signing identities through protected CI/OS key
storage. Never put certificates, private keys, passwords or token values in source
or release notes. An unsigned evaluation artifact must be labelled accordingly.

The manual signing workflow needs administrator-configured protected release-tag
rules and a protected `release-signing` environment before it can be used. A tag
name beginning with `v` and an environment name alone provide no protection. Use
dedicated disposable signing runners, reviewed source/dependency revisions and
the configured OS certificate store, keychain or GPG agent. Retain the exact
selected commit and final signature/hash verification. The workflow preserves
candidates as CI artifacts; it does not publish a GitHub release or upload to an
application store. Creating a workflow file does not provision signing identities
or establish that signed builds have run.

| Target | Before general distribution |
| --- | --- |
| Windows | Authorized Authenticode identity and timestamp service; verify publisher/signature on installer and shipped executables, then test the final signed installer on clean Windows |
| macOS (deferred) | Authorized Developer ID identity, required signing configuration and notarization credentials; sign nested code and app, notarize/staple as applicable, then verify Gatekeeper behavior on the final downloaded bundle |
| Linux | Published SHA-256 manifest and the selected package/repository signing policy; verify dependencies and final artifacts on each declared distribution |

Existing ad hoc Mach-O signatures establish neither Developer ID identity nor
notarization. Do not remove quarantine, disable Gatekeeper/WebKit sandboxing or
relax signature checks to turn a failed acceptance case into a pass. Re-run payload
and launch verification after signing: signatures can change artifact hashes.

Attach exact installation/recovery instructions, the capability/limitation matrix,
test records, fixture provenance, dependency notices and the review described in
[dependency-review.md](dependency-review.md). State which requested functions are
implemented, tested only with fixtures, unsupported, or awaiting hardware. A pilot
release cannot claim completion of the handoff's managed collection,
DSP/filter/PID, fault-rule, migration or performance acceptance families merely
because passive recording, explicit readback or exploratory local insights pass synthetic tests.
