# Development checkpoint — 15 October 2026

The restored checkout was clean at `c1f0ee6ac6393fbdfc5aadadfe6ba6c1a7b5bcb4`.
Reading the live remote and refreshing `origin/main` confirmed the same commit on
GitHub. The apparent 27-commit lead was a stale remote-tracking reference, not
27 unpublished commits. No uncommitted tracked source changes were found at
resumption. This verifies the recovered source checkpoint, not the cause of the
reported chat/cloud-publishing failure or recovery of the lost conversation.

The application is now the Flight Insights Preview 0.2.0 described in
[acceptance-status.md](acceptance-status.md). The Windows/Linux native build for
clean source `0ac9836` passed; downloads and exact results are in
[validation.md](validation.md). The October 13 build records remain historical.
Real Q22 data, GCS/radio/device qualification and release acceptance remain open.

## Recovered-session connection setup

- A four-step **Connection setup** screen and first-use overview entry.
- Guidance for a local GCS, another forwarding device, serial through the GCS,
  or saved files without live networking requirements.
- Actual receiver/sender/radio/camera address and port explanations, optional
  local-adapter discovery, input validation and explicit connection checks.
- Shared saved settings for existing Network checks and Prepare & watch, with
  unrelated sources retained and storage failures surfaced.
- Invalidation of stale previews and approvals when setup changes. A delayed
  observer start retains its reviewed capture while preserving newer saved
  settings for the next capture.
- Browser regression coverage and a [user walkthrough](connection-setup.md).

That connection-setup change did not alter the Python/Rust transport protocols or
evidence schemas. It added no direct serial transport.
Saving setup neither starts an observer nor enables acquisition/host repairs.

## Clarified product requirements

The user clarified the direction later on 15 October. See
[current-product-direction.md](current-product-direction.md) for the authoritative
current requirements and acceptance scenarios:

- Focus setup on field UDP, with serial handled by the existing field/GCS
  infrastructure. Dedicated COM setup and direct serial support are deferred.
- Use the current preflight aircraft settings as a new user's starting point.
  Capture before each flight where a reviewed disarmed bidirectional path is
  available, and explain missing evidence or one-way limitations clearly.
- Historical pre-FACt/FACt tunes are optional comparison artifacts. Preserve any
  supplied originals, aliases and hashes; their absence is not an onboarding or
  analysis blocker.
- Retain immutable per-flight environment/version context, including provenance
  for detected versus operator-declared facts. Do not stamp today's computer
  versions onto an old flight or infer exact GCS versions from heartbeats.
- Explain supported parameter-to-performance relationships using measurements,
  firmware-scoped sources and explicit confounders. Offer conditional improvement
  options with prerequisites, tradeoffs and validation steps, including notch
  tracking and bidirectional DShot as an RPM-telemetry option where supported.

This is a requirements correction, not evidence that every target is complete.
Consult the [acceptance status](acceptance-status.md) and [validation record](validation.md)
for implementation scope and executed checks. The historical handoff remains
unchanged; current requirements supersede its conflicting baseline assumptions.

## Version 0.2.0 source checkpoint

- Removed the dedicated COM workflow and inputs. Existing serial selections
  migrate to the GCS/UDP workflow; the field infrastructure retains serial setup.
- Added immutable environment observations at capture start and each observed
  armed interval. Available host OS/kernel/distribution/architecture and Flight
  Lab/engine versions are recorded automatically. Detection recognizes known
  running GCS/router/video apps and reads supported Windows file-resource or
  checksum-matched Linux package versions. Unknown metadata remains unavailable;
  lookup results still pending at capture time never rewrite that observation.
  File/package versions can differ from an application's About display and do not
  authenticate the software or prove control-path ownership. Operator declarations
  remain separate. Optional inspection and declarations live
  in **Prepare & watch → Flight computer & software**. Old flights are not filled
  using the current host.
- Improved current pre/postflight readback guidance, decoded/expected counts,
  staleness and recovery. Arm cycles revoke earlier readback eligibility,
  including readbacks that cross a cycle. Rich readbacks preserve all typed values
  in the parameter catalog while retaining and reverifying original acquisition
  hashes. Exact supported managed-flight linkage supplies pre/post references to
  reports. Partial, conflicting or ambiguous sets cannot replace missing
  preflight evidence. Count-complete readbacks remain non-atomic observations,
  not confirmed effective flight-time settings.
- Added saved-insight comparisons of supported metrics and observed parameter
  changes, with mechanisms, evidence requirements and confounders. The existing
  scoped-session comparison also includes parameter explanations. Recorded
  environment is available only through verified matching managed-flight
  source/clock/interval linkage. Full typed readback comparisons use all captured
  values rather than the report's bounded display preview; raw/type distinctions
  remain visible.
- Added conditional performance advice backed by bundled, revision-pinned
  ArduPilot sources, with prerequisites, alternatives, tradeoffs and validation
  steps. Six selected research topics provide offline passages and explicit
  updates from fixed official GitHub source paths. Updates retain commit/hash
  provenance and cached fallback reasons; they send no flight data or search
  terms. Retrieved passages remain unreviewed and do not replace reviewed advice.
  Numeric aircraft tuning and automatic parameter writes are not implemented.
- Added immutable repeated-flight plans, revisions and saved evaluations with
  groups/pairs, typed parameter hypotheses, phase/coverage requirements, goals,
  regression limits and retained counterexamples. Distinct verified flight
  records count toward repetition; an observed criterion improvement is neither
  a causal result nor proof of statistical independence or an optimum.
- Added bounded, schema-qualified DataFlash health observations for GNSS, EKF3,
  ESC, scheduler and board rails. Physical metrics need explicit units/scaling;
  enums/bitmasks retain codes without a misleading arithmetic mean. ESC spreads
  need exact shared log timestamps and all observed instances; telemetry freshness,
  motor mapping and unseen instances remain unknown. No universal pass/fail or
  guessed aircraft-specific limit is supplied.
- Added a reviewable source-delivery helper with per-file hashes, executable
  modes, dirty-tree/base-commit identity and an external manifest/checksum.
  Coordinate a stable source checkpoint before archiving; the helper's checks
  do not lock writers or create an atomic filesystem snapshot.

Analysis, readback promotion, report comparisons and experiment evaluation use
cancellable background jobs. The shared worker can queue an acquisition behind
another job; the UI keeps queued/failed preflight evidence explicit. There is no
guarantee of an immediate completed readback before arming.

These source changes are included in the Windows/Linux 0.2.0 CI preview artifacts
from clean source `0ac9836cff73ebc0d61b74aaeee51e555c502f37`. Both native pipelines
passed, including 39 browser and 17 desktop Rust tests per platform and three
fresh production WebView launches per packaged entrypoint. Windows passed NSIS
install/uninstall. The earlier Debian 13 package also passed its separate Docker
GUI recheck with system-installed WebKitGTK; the host sysroot retains its missing
child-process limitation. Downloads, checksums and exact-source recovery archive
are recorded in [validation.md](validation.md). These basic GUI checks do not
establish full installed-desktop or device qualification.

## Where to continue

Use [validation.md](validation.md) for the current Windows/Linux installers and
exact executed checks; the October 13 downloads do not contain these changes.
Continue installed-desktop qualification and bench-test the flow using the actual
Windows/Linux host, Mission Planner/QGroundControl
version, digiQ sender/management addresses, dedicated observer destination, and
camera URL. A separate bidirectional endpoint and controller UID are required
to qualify parameter/log acquisition.

The next field check should begin with a fresh data directory and no historical
tunes, establish current settings before arming, record one flight, then repeat
after a deliberate operator-controlled configuration change. Retain the actual
software versions and a matched parameter/BIN/TLOG bundle. Compare compatible
phases and review conditions before attributing a metric change to a parameter.
Also verify the one-way-feed case: passive recording works while unavailable
readback remains clearly explained.

Remaining engineering work includes unsupported software/version sources,
complete effective parameter alignment and reboot/activation proof, broader
research/health coverage and validated aircraft-specific numeric optimization.
The repeated-flight experiment workflow is implemented; controlled real-flight
qualification remains outstanding. Mechanical vibration, clipping and capability
checks remain prerequisites to any aircraft-specific filter advice.

The attached handoff ZIP contained specifications only, and available flight
fixtures are synthetic. No matched real Q22 flight bundle has been supplied.
The current native Windows build and basic GUI checks passed; minimum desktop OS,
upgrades, permissions and hardware qualification remain open. Three Windows
dependency notice texts, application distribution terms and publisher signing
also remain release gates. The release metadata audit found no
`release-signing` environment or active protected release-tag ruleset; runner and
variable readiness could not be established. See [release qualification](release-qualification.md)
for implementable delivery steps and external evidence gates. Record additional
qualification in [validation.md](validation.md) only after actual completion.

Keep original handoff files and their hashes unchanged. Update current docs and
validation records separately. Source changes, GitHub publication, a published
cloud workspace, and packaged installers are distinct checkpoints.
