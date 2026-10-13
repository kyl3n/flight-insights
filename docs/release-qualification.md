# Release qualification and bench procedure

This is an executable test plan, not a record that these checks passed. A pilot
build means a labelled evaluation build with an explicit capability list. It does
not qualify Q22 flight operation or complete the supplied handoff. Record actual
results in [validation.md](validation.md) and attach the evidence described below.

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
| Native CI job results linked in `validation.md` | Executed target builds and packaged-runtime checks at the recorded commits; no installed-GUI or hardware qualification |
| Synthetic parameter, metric and protocol fixtures | Parser/arithmetic regressions; no real Q22, radio, camera or firmware validation |

New decoder, repair or release code needs its own recorded results. Never carry a
previous milestone's passing count forward as evidence for a later build.

## Native platform qualification

Run on Windows x64, Linux x64, macOS Apple Silicon and macOS Intel independently.
Use a disposable VM for installation tests and a physical machine for USB/LAN and
sleep/resume tests. Build each frozen Python runtime on its matching OS and CPU.

| Platform | Required evidence |
| --- | --- |
| Windows x64 | Clean installed desktop Windows version(s) declared supported, plus exact WebView2 version. The Windows Server 2022 CI runner alone does not qualify a desktop Windows release. Establish the supported minimum before advertising it. |
| Linux x64 | Ubuntu 22.04 CI baseline and the intended desktop distribution with its declared WebKit/GTK/glibc packages. A local Debian 13 package uses the Debian 13 override and does not establish Ubuntu compatibility. Verify `.deb` and AppImage separately. |
| macOS arm64 and x64 | Native architecture, declared macOS 13.0 floor and a current supported OS version. Verify embedded Mach-O minimums and actual installed execution; a macOS 15 build does not prove macOS 13 compatibility. |

1. Start with no Q22 installation or prior application data and no separately
   installed Python, Node or uv. Record the VM snapshot and prerequisite state.
   For Windows automated NSIS verification, use only a disposable clean runner:
   installation also affects uninstall registration, shortcuts and possibly the
   shared WebView2 runtime. A temporary destination is not complete isolation.
2. Verify the artifact hash. Run `scripts/verify-native-bundle.py` on the matching
   build host and retain its report. It checks the real packaged native supervisor
   without a webview; complete the graphical steps separately.
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
**Evidence → Flight log** and the readback through **Parameter snapshot**. Choose
one source and clock segment before creating each scoped session. A separate
readback import does not automatically verify the log's parameter history; keep
that association evidence with the sample bundle.

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

Preserve accepted “Q22 original - before Brian settings” and “Q22 with Brian tune”
as separate immutable sources. Do not reconstruct a missing original. Validate
Cube Orange+ and DEF H7 independently, including firmware build, sensor IDs,
motor/output/ESC mapping and battery calibration. One modified flight does not
establish causality; matched repeated flights and flight-level uncertainty are
required before claiming a tuning benefit.

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
| macOS | Authorized Developer ID identity, required signing configuration and notarization credentials; sign nested code and app, notarize/staple as applicable, then verify Gatekeeper behavior on the final downloaded bundle |
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
