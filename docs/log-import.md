# Real-format flight-log import

The desktop now imports DataFlash BIN, textual DataFlash LOG, and timestamped
MAVLink TLOG files. These are working decoders and evidence flows, tested with
explicitly synthetic format fixtures. They do not constitute validation against
a matched Q22 flight or every ArduPilot firmware revision.

Import a log, inspect its sources/clock segments and parameter observations, then
create a session for one source, segment, battery monitor and selected interval.
That session joins the existing parameter/performance comparison workspace.
Aircraft, board, exact firmware, phase and conditions remain operator declarations;
recorded identity messages are retained for review, not silently accepted as a
verified aircraft profile.

## Formats and units

Content sniffing works with the content-addressed original filenames.

* **BIN** reads FMT wire schemas and bounded records. FMTU, UNIT and MULT retain
  field units/scaling lineage. Intrinsic fixed-point scaling applies once.
* **LOG** reads bounded text records with FMT schemas. Already-decoded intrinsic
  fixed-point values are not scaled twice. Unknown records produce diagnostics.
* **TLOG** reads recording timestamps followed by MAVLink v1/v2 frames. The pinned
  pymavlink ArduPilot dialect checks CRCs; a CRC-bypass environment flag is refused.
  Packet signatures remain explicitly unauthenticated.

DataFlash power/tracking quantities require supported in-log units/scales. Unknown
units yield unavailable measurements. Battery identity follows the FMTU `#` marker
or an unambiguous instance field; missing identity does not silently become zero.
BAT.RemPct=255, BAT.SH=0 and ambiguous zero temperature remain explicit unknowns.
Raw cumulative consumption is retained without guessing its applicable units.

MAVLink millivolts/centiamps convert according to its pinned wire schema. Unknown
current `-1`, unknown voltage entries and temperature `32767` remain unavailable.
SYS_STATUS is a separate aggregate monitor choice and is never summed with
BATTERY_STATUS. Reported voltage arrays cannot establish whether entries represent
individual cells or split pack-voltage encoding. ATTITUDE radians convert to
degrees, but no desired angle is invented; ATTITUDE alone cannot provide tracking
RMSE.

PARM and validated PARAM_VALUE produce individual timed observations. Generic
integer PARAM_VALUE encoding is not guessed: REAL32 is supported directly;
ArduPilot C-cast integer interpretation requires a recorded ArduPilot heartbeat.
Indices/counts are preserved but do not prove unchanged settings for a flight.

## Clocks and corruption

DataFlash uses its explicit TimeUS/TimeMS boot-time convention. TLOG sources are
separated by MAVLink system/component IDs and assumed capture channels. A
boot-time regression starts a conservative clock segment: `boot_epoch` does not
mean a confirmed reboot. Wrap, delay and reboot require further classification.

TLOG windows use recorded receive time with a per-stream origin, not synchronized
aircraft measurement time. The pymavlink low-two-timestamp-bit capture-link
convention is retained with an **unverified exporter convention** label and the
original timestamp. This convention does not establish separate physical aircraft.
Absolute microseconds cross JSON as decimal strings; windows use relative seconds.
No cross-source synchronization is claimed.

Corrupt records carry diagnostics. If corruption loses source/time, every active
stream gets a metric barrier. The next affected integration pair is excluded.
Receive-clock regressions and discarded nonmonotonic samples also prevent bridging.
Long gaps/missing values retain explicit coverage/exclusions. DataFlash byte
resynchronization is limited to 1 MiB and always reported.

Unrecognized files are archived with an `unsupported` result. Decoder failure
after a usable prefix produces `partial` evidence. Hard resource/I/O failures may
fail a job while preserving its original archive. Partial evidence never appears
complete.

## Storage and bounds

Originals use the immutable SHA-256 store. Typed Parquet events retain source,
clock segment, integer time, monitor, normalized power/tracking, record index and
original fields/schema provenance. SQLite remains the metadata authority. Decoder
and writer batches contain 1,024 events. A writer closes after 131,072 events and
starts another shard, bounding Arrow's retained row-group footer metadata. Queries
scan one shard at a time in original record order; no full-log Python dictionary
or accumulating whole-dataset footer cache is used.

Derived shards have individual SHA-256 values and an aggregate manifest hash.
Timeline reads and session construction
verify it; repeated verification is cached only while size, modification time and
change time remain unchanged. A mismatch refuses the derived data and requires
reimport from the original. Parquet is reproducible derived evidence, not the
original record.

Limits: 8 GiB original, 20 million normalized events, 64 source/clock streams,
128 KiB per normalized event, 64 KiB per text record, and 50,000 distinct parameters
per snapshot. Lists/timelines are bounded and paginated. These limits are not a
claim that all large-file/reference-laptop acceptance gates have passed; see
recorded benchmark/release evidence for measured scope.

## Historical snapshots and sessions

A snapshot includes only observations **at or before the selected start**, never
the file's final parameter dictionary applied retrospectively. Untimed observations
remain visible but are omitted from the start snapshot. Snapshot source kind is
`logged_interval`, completeness is `observed_only`, and absent UTC observation
time remains null. SHA, source, clock segment and original observations persist.

Changed or newly observed parameters inside the selected interval produce
`parameter_change_observations` and `parameter_split_points_s`. Scalar comparison
metrics are withheld for that mixed/unknown configuration interval. Select
separate intervals around those boundaries. Observation time is not proof of the
controller's exact application time; unobserved changes remain possible.

Power uses one chosen, unit-validated monitor. Power and paired attitude retain
separate cadences. Streamed scratch data feeds the same tested energy/time-weighted
power/tracking core used by CSV imports. Missing channels remain unavailable.
Metrics retain source SHA/fields, decoder/algorithm versions, source/clock/monitor,
coverage and exclusions. Cancellation is checked during metric calculation too.

Comparisons gate on aircraft/firmware, conditions, phase, exposure, parameter
completeness and quality warnings. Associations never produce causal verdicts,
tuning numbers or aircraft commands. Two intervals in one original are not two
independent flights. DSP/filter/PID analysis, airborne detection, verified identity
and complete historical readback reconstruction remain outstanding.

## API and jobs

`LogService.dispatch()` is synchronous for Python reuse/tests. The RPC boundary
runs import/session operations through cancellable jobs.

| Method | Inputs | Completed result |
| --- | --- | --- |
| `logs.import` | Native-dialog `path`, `label`, boolean `synthetic` | `{log}` |
| `logs.list` | Optional `limit` (1–100), `offset` | `{logs,next_offset}` |
| `logs.detail` | `log_id`, optional `source_key`/numeric `boot_epoch`, `timeline_offset`, `timeline_limit` (1–200) | Log/stream/schema metadata, observations, `timeline_next_offset` |
| `logs.session` | Explicit log/source/segment, monitor, identity, interval and conditions | `{session}` |

Session fields: `log_id`, `source_key`, `boot_epoch`, `battery_id`, `aircraft_id`,
`board`, `firmware`, `label`, `phase`, `interval_start_s`, `interval_end_s`,
`max_gap_s`, and `conditions` matching the CSV contract. A missing monitor permits
null `battery_id` and unavailable power; a sole monitor can default automatically.
Multiple monitors require selection. Source keys are opaque identifiers returned
by the engine; clients should not construct them.

RPC imports/session calls return `{job}`. States are `queued`, `running`,
`succeeded`, `failed`, `cancelled`, or `interrupted`. Poll `jobs.get {job_id}` for
the result; `jobs.list` summarizes jobs and `jobs.cancel` requests cancellation.
The runner allows four pending/running jobs and one worker. Terminal results
persist; unfinished jobs become interrupted after restart. Archive, checksum,
decoder, Parquet scan and metric loops check cancellation. Completed original
archives may remain after cancellation to preserve evidence.

## Reproduce checks

```sh
uv sync --frozen --group dev
.venv/bin/python fixtures/logs/generate_synthetic.py
.venv/bin/python -m pytest engine/tests/test_log_decoder.py engine/tests/test_logs.py -q
```

The fixture generator constructs genuine BIN/LOG wire records and CRC-bearing
MAVLink packets. Synthetic tests cover source/clock separation, scaling once,
unknown units/sentinels, CRC/corruption, timeline scoping, cancellation, provenance,
persistence and derived-artifact tampering. These are not hardware tests.
