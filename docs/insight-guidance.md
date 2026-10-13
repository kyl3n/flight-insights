# ArduPilot guidance and optional AI design

The insights reference registry is an offline, reviewed snapshot. It explains
locally computed evidence and links to official upstream material. It does not
provide parameter commands, automatic tuning, a model API client, or a live
guidance updater. Its implementation is `engine/q22_engine/guidance.py`.

## Sources and currency

Registry version: **q22-ardupilot-guidance/2026-10-13.1**. Upstream files were
retrieved using GitHub's official contents API between **2026-10-13 04:40:15 UTC
and 04:40:55 UTC**; the registry records the retrieval batch completion time.
Each citation includes the upstream repository, exact commit, file path, line
range, whole-file SHA-256, and retrieval time. Hashes identify reviewed content;
they do not constitute a publisher signature. Summaries are Q22 paraphrases,
not an upstream endorsement of this app.

- ArduPilot wiki: commit
  [`21432301f2d504e29940a86473e74259b871d936`](https://github.com/ArduPilot/ardupilot_wiki/commit/21432301f2d504e29940a86473e74259b871d936),
  committed 2026-10-12 12:15:25 UTC.
- ArduPilot source: release
  [`Copter-4.7.1`](https://github.com/ArduPilot/ardupilot/releases/tag/Copter-4.7.1),
  published 2026-09-03 23:50:43 UTC, resolving to
  [`dbe792162d06cab66c3475fd5556bf7a120f119e`](https://github.com/ArduPilot/ardupilot/commit/dbe792162d06cab66c3475fd5556bf7a120f119e).
  This was the most recent non-prerelease Copter release observed in the official
  GitHub release listing at retrieval. It is not a claim about future releases
  or any particular aircraft's firmware.

The registry marks review due after 90 days (2027-01-11 04:40:55 UTC). This is a
Q22 editorial freshness policy, not an expiration declared by ArduPilot. It does
not verify remote updates. A clock earlier than retrieval is explicitly flagged.
Review-due citations remain available as historical references; they never
authorize configuration changes. Reports must retain the registry version and
source revisions used to produce them.

## Scope of the references

| Citation ID | Reviewed material | How it can inform an insight |
| --- | --- | --- |
| `vibration` | [Measuring vibration, lines 28–48](https://github.com/ArduPilot/ardupilot_wiki/blob/21432301f2d504e29940a86473e74259b871d936/common/source/docs/common-measuring-vibration.rst#L28-L48) | Interpret VIBE in m/s² with coverage, axis and IMU context. |
| `clipping` | [Firmware clipping implementation](https://github.com/ArduPilot/ardupilot/blob/dbe792162d06cab66c3475fd5556bf7a120f119e/libraries/AP_InertialSensor/AP_InertialSensor.cpp#L2216-L2246) | Count observed counter increases within a continuous boot/IMU segment. |
| `harmonic_notch` | [Notch setup and validation](https://github.com/ArduPilot/ardupilot_wiki/blob/21432301f2d504e29940a86473e74259b871d936/common/source/docs/common-imu-notch-filtering.rst#L38-L110) | Request measured spectra and reliable tracking evidence before discussing filter effectiveness. |
| `gyro_filter` | [Noise, aliasing and phase delay](https://github.com/ArduPilot/ardupilot_wiki/blob/21432301f2d504e29940a86473e74259b871d936/common/source/docs/common-imu-notch-filtering.rst#L20-L34) | Explain the filtering tradeoff without supplying a cutoff or gain. |
| `heavy_lift` | [Tuning sequence and advanced considerations](https://github.com/ArduPilot/ardupilot_wiki/blob/21432301f2d504e29940a86473e74259b871d936/copter/source/docs/tuning-process-instructions.rst#L7-L33) | Account for declared payload, flexible mounts, configuration, battery and actuator limitations. |
| `rate_limits` | [Rate/input-shaping metadata](https://github.com/ArduPilot/ardupilot/blob/dbe792162d06cab66c3475fd5556bf7a120f119e/libraries/AC_AttitudeControl/AC_AttitudeControl.cpp#L96-L134) and [acceleration metadata](https://github.com/ArduPilot/ardupilot/blob/dbe792162d06cab66c3475fd5556bf7a120f119e/libraries/AC_AttitudeControl/AC_AttitudeControl.cpp#L175-L203) | Distinguish imposed response constraints from tracking performance. |
| `tracking` | [RATE field definitions](https://github.com/ArduPilot/ardupilot/blob/dbe792162d06cab66c3475fd5556bf7a120f119e/libraries/AC_AttitudeControl/LogStructure.h#L122-L150) | Compare matching desired and achieved rates; do not confuse angle and rate error. |
| `actuator_limits` | [Multicopter mixer limit handling](https://github.com/ArduPilot/ardupilot/blob/dbe792162d06cab66c3475fd5556bf7a120f119e/libraries/AP_Motors/AP_MotorsMatrix.cpp#L359-L394) | Explain why normalized output or high PWM alone cannot prove saturation. |

The wiki calls VIBE below 30 m/s² normally acceptable, above 30 potentially
problematic, and above 60 usually associated with position/altitude problems.
These are source-attributed screening bands, not certified limits or a diagnosis.
They cannot be applied to raw acceleration magnitude or used to infer spectral
peaks. FFT/notch analysis needs validated high-rate raw IMU sampling, sensor
identity, timebase, missing-sample treatment, sample rate and anti-alias/filter
stage information. No numeric PID, notch or heavy-lift preset is bundled.

Clipping is cumulative. A nonzero first observed value does not mean those
events occurred during the selected flight. Counter resets/wraps, recording gaps
and IMU changes must break or qualify delta calculations. The wiki mentions 16G
in an example, but the reviewed firmware calls `get_clip_limit()` on each sensor
backend. That number must not become a universal clipping threshold.

## Firmware and historical applicability

The reviewed source metadata applies only as reference material for the pinned
Copter release. A declared `Copter-4.7.1` label can match the reviewed release,
but cannot establish an exact build, board, compiled features or custom patches.
Unknown, custom, prerelease, other-version and other-vehicle builds remain
unmatched. No registry response reports an exact build as validated or permits
parameter application. The registry does not turn parameter names in an export
into proof that those features are compiled in or active.

Two concrete reasons to retain this boundary:

- The pinned 4.7.1 source explicitly replaced `ATC_ACCEL_R_MAX`,
  `ATC_ACCEL_P_MAX` and `ATC_ACCEL_Y_MAX` (centidegrees/s²) with `ATC_ACC_*`
  (degrees/s²). Names and units must not be silently substituted across versions.
- The pinned [VIBE structure](https://github.com/ArduPilot/ardupilot/blob/dbe792162d06cab66c3475fd5556bf7a120f119e/libraries/AP_InertialSensor/LogStructure.h#L116-L140)
  uses an `IMU` instance and one `Clip` counter; older wiki examples use
  `Clip0`, `Clip1` and `Clip2`. Actual log schema and firmware context take
  precedence over example field layouts.

Flight reports must bind their historical claims to the original flight's
parameter evidence, timestamps and identity. A current readback or operator
profile can provide comparison context, but cannot fill missing historical
configuration. Observed performance changes alongside parameter changes remain
associations until flight regime, payload, battery, wind and hardware differences
are addressed. Calibration-specific parameters must not be copied between units.

## Optional model API boundary

AI is feasible as an explanation and retrieval layer above deterministic local
analysis. Keep decoding, units, timing, quality checks, interval selection,
statistics, spectrum prerequisites and parameter provenance local. A model
could receive an explicitly approved, minimized evidence report plus a selected
set of pinned citations, then produce a structured explanation with:

1. Claims linked to local evidence identifiers and accepted citation IDs.
2. Observations separated from hypotheses, missing evidence and suggested
   inspection steps.
3. Explicit uncertainty, firmware applicability and competing explanations.

Validate the response schema and reject unknown citations, fabricated evidence
IDs, unsupported numeric tuning instructions, parameter commands and tool calls.
Do not grant the model vehicle/network-control capabilities. Treat model output
and retrieved text as untrusted material, including instructions embedded in
logs, labels or documents. A response is advisory and never becomes a control
command. Preserve model/provider version, approved input hash, citation versions
and output for audit without persisting credentials.

Before implementing an external provider, the product needs a user-visible
provider choice, exact payload preview and consent, data minimization/redaction,
retention policy, cancellation, time/cost limits, and a local-only fallback.
Store any future key in an OS credential store, never in browser storage, source,
logs or evidence exports. Provider retention and regional handling need to be
checked against the chosen API contract. No API request, key collection or user
data upload was made during this work; no provider integration is implemented.

The local review packet uses an explicit typed export schema. It retains the
synthetic-data flag, time domain, coverage, quality warnings, declared PWM
mapping/endpoints, relative evidence references and safe tuning comparisons.
It omits source paths, absolute timestamps, hardware/device/serial identifiers,
location fields and arbitrary free-text notes (including anti-alias evidence).
Only a recognized canonical firmware release label is retained, still marked
as unverified for the exact build. Parameter exports use a narrow tuning-name
allowlist; omitted records are counted. Original local reports remain unchanged.
An unknown guidance-registry version is reported as unresolved, never silently
replaced with the current registry. Preview and explicit file export produce
the same JSON packet; neither submits it to a remote service.

## Signed guidance updates: feasible, not implemented

A future updater should consume a bounded, declarative data manifest rather than
Python, JavaScript or executable rules. Its trust contract must define an owned
publishing identity, embedded trusted root keys, offline root-key custody,
threshold signatures/key rotation/revocation, schema and engine compatibility,
firmware applicability, content hashes, monotonic versions and expiration.
TUF-style root, timestamp, snapshot and targets roles are a suitable design to
evaluate for rollback/freeze resistance. TLS and upstream commit hashes alone do
not establish the app publisher's authorization to activate guidance.

Download into a staging area, enforce byte/depth limits, verify signatures and
hashes before parsing/activation, reject rollback/incompatible bundles, then
atomically activate and retain the last verified snapshot. Handle unavailable
network or an unreliable clock explicitly. Pin the exact activated version into
each report so a later update never silently rewrites a historical result.
Source-content changes and firmware applicability need human editorial review
before signing; signed content is still not authority to apply aircraft settings.

There is no updater, signing key, trust root or automatic network fetch in this
implementation. Updates currently require a reviewed application change and
normal distribution process.
