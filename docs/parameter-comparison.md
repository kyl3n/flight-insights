# Parameter-linked performance: foundation preview

This milestone imports actual Mission Planner or QGroundControl parameter export
formats and an explicitly defined performance CSV. [Flight-log import](log-import.md)
also creates scoped sessions from DataFlash BIN/LOG and MAVLink TLOG. It archives original bytes,
freezes each flight interval's associated snapshot, shows changed settings beside
measured performance differences, and gates interpretation on evidence and
conditions. It does not infer historical settings from a
currently connected aircraft, produce causal conclusions, or recommend/write
parameter values.

No Q22 baselines are supplied or invented. Import the actual accepted files before
using **Q22 original - pre-FACt** and **Q22 with FACt tune** as display aliases.
Preserve original bytes, filenames and legacy Brian labels; an alias does not
rename or replace evidence. All snapshots are immutable;
aircraft and exact board/firmware identity are entered explicitly. Their identity
is marked **operator declared**, not independently verified. Cube Orange+ and DEF
H7 snapshots remain separate because sessions must match their snapshot identity
and comparisons flag identity differences.

## Local storage and provenance

The engine's `--data-dir` contains `catalog.sqlite3` and `raw/sha256/<hash>`.
Original files are copied in 1 MiB chunks to a temporary file, hashed, flushed,
and published without overwriting an existing blob. Every import gets a separate
source manifest even when identical bytes share a blob. A changed original
filename with different content creates a new hash. Existing blobs are verified
before deduplication; corruption causes refusal rather than replacement.

The catalog records original name/path, SHA-256, size, acquisition method, creation
time, snapshot observation time, source kind, parser/algorithm versions, metadata,
and immutable snapshot/session IDs. SQL triggers reject updates/deletions of
source and document rows. The application never edits the original file or writes
aircraft settings. Explicit [read-only acquisition](read-only-acquisition.md) has a
narrow request allowlist. Read-only file permissions and catalog triggers protect against
accidental application mutation; they are not a tamper-proof filesystem security
boundary. Import errors can leave a safely archived original with no parsed
snapshot/session, which is preferable to losing evidence. Original files must be
completed regular files; directories, devices, pipes and changing recordings are
rejected.

Archive behavior, corruption handling and persistence run in the native target
suites; exact executed revisions are listed in [validation](validation.md). Windows
uses a no-replacement rename before setting the read-only attribute, avoiding a
read-only hard-link cleanup failure. Installed-app and real-flight qualification remain open.

The catalog stores parameter maps, metric aggregates and source metadata.
The numeric CSV is streamed and retained unchanged; no full-series Python
dictionary is created. Log imports now create typed Parquet event tables.
Advanced signal-query/DuckDB integration remains future work.

## Supported parameter exports

Mission Planner `.param` / `.parm` records are `NAME,value` or `NAME value`.
QGroundControl uses five whitespace/tab-separated fields:

```text
system_id component_id NAME value MAV_PARAM_TYPE
```

UTF-8/BOM files, blank lines, and full-line `#` or `;` comments are accepted.
Original numeric spelling and QGC system/component/type metadata are retained.
Numeric equality compares decimal values, so `30` and `30.0` are equivalent.
Duplicate names, multiple MAVLink sources within one snapshot, mixed formats,
malformed names/numbers and nonfinite values are rejected. A parameter count above
50,000 or a parameter export above 20 MiB is rejected explicitly.

Functional groups are name-prefix labels. Units, bitmask labels, allowed ranges,
reboot requirements and calibration applicability require matching validated
firmware metadata; the engine returns unavailable values rather than invented
interpretations. Calibration/device-related prefixes are marked for review, not
as an exhaustive firmware-specific classification.

## Performance CSV contract: `q22.performance.v1`

Required case-sensitive columns:

| Column | Meaning and unit |
| --- | --- |
| `time_s` | Nonnegative, strictly increasing relative seconds in one boot epoch |
| `voltage_v` | Simultaneous calibrated primary pack voltage, volts |
| `current_a` | Simultaneous primary pack current, amperes; negative measurements retained |

Optional paired columns `desired_roll_deg` and `actual_roll_deg` use degrees.
Both must be present to calculate roll tracking. Empty measurement cells mean
unavailable and exclude adjacent integration intervals. Other columns are ignored;
the importer does not infer units from aliases, mix battery monitor instances,
combine duplicate sources, or claim support for an arbitrary vendor CSV.

The user selects a flight/phase interval and a positive `max_gap_s`, based on the
source's actual cadence. Times must fit the microsecond-resolution relative clock
range. Canonical interval microseconds are decimal strings over JSON; relative
seconds are also supplied for display and arithmetic. Original CSV timestamps
remain intact in the archived bytes. Imported phase, calibration, selected flight
interval and monitor identity are declarations, not automatically detected facts.

Only continuous valid pairs whose spacing is at most `max_gap_s` contribute:

* Sampled power is `voltage_v × current_a`. Energy is the trapezoidal integral of
  signed sampled power divided by 3600. Mean power divides the same integral by
  **observed valid duration**, not by the complete selected interval.
* A window boundary within a valid pair uses linear interpolation of sampled
  power. Voltage and current are not independently interpolated before multiplying.
* Peak power is the maximum of the sampled/interpolated endpoints that contribute
  to valid intervals. Unsampled peaks are unknown.
* Tracking RMSE integrates the square of linearly interpolated roll error. For an
  interval with endpoint errors `a,b`, mean squared error is `(a² + a×b + b²)/3`.
  Missing tracking pairs and long gaps have separate exclusions/coverage. This is
  an angle-tracking measure, not a validated PID response analysis.

Each metric carries selected/covered duration, sample/pair counts, coverage,
algorithm version, source fields and excluded intervals. No long gap is bridged,
no missing cell is zero-filled, and zero coverage yields unavailable rather than
zero energy. Energy applies only to observed portions. Measurement uncertainty is
explicitly unquantified. A selected interval may have zero sample timestamps but
valid bracketing pairs; its pair count and coverage explain that result.

CSV input is bounded to 8 GiB, 10 million rows, 64 columns and 1 MiB per physical
line. Standard CSV field-size limits also apply. These are parser limits, not
claimed performance benchmarks. Cancellation and the handoff's 2 GiB responsiveness
acceptance target have not yet been validated.

## Association and interpretation gates

Each session records a snapshot ID, its source SHA-256 and observation timestamp.
Importing a newer/current snapshot never rewrites a historical session. Snapshot
source kinds are `flight_readback`, `current_readback`, `file_export`, and
`logged_startup`. Source-kind declarations are retained as provenance, not treated
as automatic proof of aircraft state.

A descriptive comparison requires an operator-attested **flight readback** with
an explanatory note and `parameter_changes=none_reported`. A currently loaded
snapshot or startup export alone does not prove settings throughout an earlier
flight. Unknown or present parameter changes flag an unsupported timeline:
import separately scoped intervals with their applicable snapshots. This milestone
reports observed split boundaries for logs and withholds mixed-configuration metrics.
Complete parameter timeline verification remains unavailable. Numerical differences
remain descriptive measurements of the input intervals; parameter attribution is
withheld.

Comparison also flags mismatched aircraft/board/exact firmware, phase, duration,
synthetic status, duplicate source bytes, missing power/tracking coverage, negative
current interpretation, and missing/different conditions. Recorded conditions are
`payload_kg`, `mass_kg`, `battery_id`, `battery_soc_pct`,
`battery_temperature_c`, `wind_mps`, `hardware_revision` and `props`. Zero values
are legitimate and remain distinct from unknown. No arbitrary tolerance or
unsupported normalization removes a difference.

Results give absolute and relative metric changes (`change / abs(before) × 100`;
zero/unavailable baselines produce unavailable percentages), parameter
changed/added/removed counts and entries, and all confounders. `descriptive_only`
means no configured gate was triggered, not that causality, calibration or
independent firmware identity was established. `confounded` means interpretation
is restricted. A pair of intervals never supplies an independent-flight count or
statistical confidence interval. Repeated matched trials and additional stability,
vibration, thermal and authority measurements remain necessary.

## RPC methods

All methods use the engine's JSON-RPC 2.0 transport. Paths are chosen through the
desktop's native file dialog; no imported contents execute as commands.

* `parameters.import`: `path`, `label`, `aircraft_id`, `board`, `firmware`,
  `source_kind`, timezone-qualified ISO8601 `observed_at`, and optional boolean
  `synthetic`; returns `{snapshot}` without the potentially large parameter map.
* `parameters.list`: optional `limit` (1–100) and `offset`; returns
  `{snapshots,next_offset}`. Full maps stay in the local catalog.
* `parameters.diff`: `before_id`, `after_id`; returns identity compatibility,
  changed/added/removed arrays, counts and limitations. Arrays are capped to 1,000
  entries each with an explicit `truncated` indicator.
* `sessions.import`: `path`, `label`, `snapshot_id`, matching `aircraft_id`,
  `board`, `firmware`, `phase`, `interval_start_s`, `interval_end_s`, `max_gap_s`,
  `parameter_evidence` (`unverified` or `operator_attested`),
  `parameter_evidence_note`, `parameter_changes` (`unknown`, `present` or
  `none_reported`), `conditions` and boolean `synthetic`; returns `{session}`.
  Synthetic status must match the snapshot.
* `sessions.list`: optional `limit` and `offset`; returns `{sessions,next_offset}`
  with persisted metrics/scope/exclusions. Lists are additionally capped to 2 MiB;
  `next_offset` allows subsequent pages without oversized RPC responses.
* `comparison.run`: `before_session_id`, `after_session_id`; returns `status`,
  `parameter_diff`, metric differences, confounders and limitations.

Imports are immutable observations. Reimporting is permitted and creates a new
manifest/snapshot/session; equal bytes deduplicate storage, not flight identity.
The small explicitly named **synthetic** fixtures under `fixtures/` validate
formats/arithmetic and must be imported with `synthetic=true`. They are never
auto-loaded and are not Q22 hardware validation.

Run the focused checks from the checkout:

```sh
PYTHONPATH=engine .venv/bin/python -m unittest engine.tests.test_store engine.tests.test_parameters -v
```

Tests cover immutable/deduplicated archiving, corruption refusal, persistent
catalog reopening, pipe rejection, actual export formats, ambiguity/NaN rejection,
firmware mismatches, constant/ramp/partial-window arithmetic, negative current,
missing measurements, long gaps, timestamp resets, historical association,
confounders and synthetic evidence separation.
