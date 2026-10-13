# Offline control and filter analysis

The desktop Insights report analyzes one explicitly selected source, conservative
boot segment and interval. It generates no aircraft commands, parameter writes,
gain prescriptions or proof of airworthiness. Unknown installed firmware remains
unknown. A declared release label does not verify an aircraft's exact build.
New reports identify `q22-offline-insights/2`; preserved version-1 reports keep
their original algorithm identifier and results.

## Raw and batch IMU evidence

Raw GYR analysis requires recorded units and a measurement clock. Batch analysis
supports the ISBH `QHBBHHQf` and ISBD `QHHaaa` formats documented at ArduPilot
commit `dbe792162d06cab66c3475fd5556bf7a120f119e` (Copter 4.7.1). The importer
preserves original records, validates complete sequences, divides signed int16
samples by ISBH.mul, and reconstructs sample time from SampleUS and sample rate.
No numerical samples are emitted from incomplete, duplicated, corrupt,
unsupported or implausibly timed batches. One pending batch is bounded to 8192
samples; complete batches are stored as chunks of at most 128 samples.

Gyro and accelerometer types, logged instance, sample scale, original header and
block offsets remain attached. Batch pre/post labels require an eligible observed
INS_LOG_BAT_OPT value. Modes whose instance offset requires the actual physical
sensor count retain an unknown stage; missing parameters are not guessed. These
observations are not proof of the full effective configuration. Original raw GYR
filter stage remains unknown in this implementation.

Parser version 2 adds this derived evidence. Previously imported version-1 logs
retain their immutable derived data; reimport the preserved original to obtain
batch evidence. No previous report is silently recomputed.

## Spectra and modeled filtering

The operator selects raw GYR or batch data, logged instance, axis, sample rate,
window size and acquisition anti-alias evidence. Measurement times must belong
to the selected interval. Discontinuities break the window; separate batches are
not stitched across gaps. Encoding endpoints in batch samples disqualify that
chunk rather than silently treating potential clipping as a clean waveform.

At most 4096 continuous samples enter Welch analysis. It removes each window's
mean, applies a symmetric Hann window, uses 50% overlap, and reports one-sided
power spectral density in `(rad/s)^2/Hz`. Up to 16 complete windows form the
time-frequency view. Resolution, actual used measurement bounds, continuity
exclusions, rate error, sample caps and window RMS variation remain visible.
VIBE is a summary signal and never supplies frequency or notch estimates.
Operator anti-alias declarations are not measured attenuation evidence, so
spectral qualification remains conditional.

The static model implements a second-order Butterworth low-pass and finite-depth
ArduPilot static notch, plus cascades of at most 16 stages. It reports digital
coefficients, stability, amplitude gain, attenuation, continuous phase and
analytic group delay. Zero-transmission phase/delay remain undefined. The
low-pass source's 0.4-sample-rate cutoff clamp is explicit. The notch uses the
pinned source's octave-derived bandwidth convention, not a generic bandwidth
substitution.

Static equations are numerically validated against original pinned C++
coefficient outputs and independent SciPy design/response/delay oracles. Runtime
code is independently written and uses only the standard library. This does not
validate firmware float32 transients, dynamic tracking, harmonic-bank options,
sensor decimation or a board's complete filtering pipeline. No numeric candidate
is selected automatically.

When a spectrum and explicit model use the same rate, the report can apply
`PSD * |H|²` and integrate the displayed frequency band. This is additional
hypothetical filtering of the logged waveform, not a measured pre/post comparison
or a prediction of the aircraft's effective pipeline. Phase and group delay
describe the model; they are not closed-loop stability margins.

## RATE and PID observations

RATE desired/achieved pairs require in-log units and scaling. Radians per second
convert once to degrees per second. At most 8192 chronological pairs are retained,
with actual bounds and truncation shown. Time-weighted error RMSE/bias use valid
short linear pairs; sample quantiles are labeled separately. Corrupt/replayed
times never acquire fresh coverage. The display series is independently bounded
to 512 points.

Lag uses mean-centered normalized cross-correlation on at most 2048 continuous
samples. Insufficient excitation, irregular samples, edge maxima, broad peaks
and periodic aliases withhold the lag estimate. Sample resolution is not a
statistical confidence interval or a plant-delay identification.

An explicit step window must fit the report scope, have steady pre/post command
plateaus and initial response, adequate excitation, uniform coverage and a
settled final response. Known PID flags must cover the window under declared
pinned firmware semantics; limiting, reset, unknown flags, clipping/vibration or
gaps withhold response classification. Rise time uses observed 10%/90% reference
crossings, settling uses the declared band, and overshoot is reference-normalized.
The phase must be declared. These remain observed closed-loop measurements;
unlogged disturbances and estimator behavior are not independently excluded.

P/I/D/FF/DFF/Dmod/SRate statistics retain recorded controller units. A large
D-term RMS does not itself establish sensor noise or imply a gain direction.
PWM/output percentages do not substitute for missing saturation evidence.

## Numerical evidence

- `test_imu_batches.py` covers real BIN and text arrays, complete/missing/duplicate
  sequences, scale, clock gates, encoding bounds, provenance and real import to
  PSD. A frozen numerical oracle was produced from FilterReview.js at WebTools
  commit `5b7fe705f6516c0d60830449d14630135cd23ed2`, SHA256
  `ef5a9708a173726b81668b9c42d29f5b8792983be6400992b1c2a3d82feaa439`.
- `test_control_analysis.py` checks analytic first-order rise/settling, exact
  integration, a known delayed nonperiodic signal, rejected periodic/weak
  excitation, limiting/reset/gaps, input bounds and the complete imported report.
- `test_filter_models.py` preserves numeric fixtures from original pinned C++
  routines compiled with g++14.2.0 and SciPy1.17.1. Frozen oracles run without
  SciPy; optional live independent checks skip explicitly if it is absent.
- `synthetic-control-batch.log` is a marked synthetic DataFlash fixture, not an
  aircraft log or hardware qualification result.

Only numerical outputs and source provenance are retained from upstream review
tools; their GPL implementation is not incorporated into the application.
Representative aircraft logs, verified RPM-order/pre/post comparisons, dynamic
firmware-pipeline parity and hardware confirmation remain distinct qualification
work. Their absence must not become a fabricated tuning recommendation.
