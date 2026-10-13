# Content alignment and parameter evidence

Alignment compares explicitly selected imported sources and clock segments. Its
result is an immutable report. **Verified** means the documented software rule
passed within the recorded candidate universe. It does not mean cryptographic
aircraft authentication, flight qualification, complete collection or known
effective settings. Unsigned replay remains possible.

The reference can be imported directly from one complete app recorder TLOG chunk.
Original hashes and capture-global record bounds must agree. A selected chunk
must contain the relevant observed arm and disarm boundaries; a flight spanning
several chunks currently requires provisional review. No file is trimmed or
renamed. One original can support separate selections for several armed intervals.

## Association rule

The rule requires all of the following:

- One matching nonzero reported hardware UID and one exact firmware/board
  identity per source. A downloaded onboard log may obtain identity provenance
  from the immutable acquisition record bound to those exact original bytes,
  with matching initial/final gates. A filename, system ID, VER text or matching
  parameter hash cannot substitute for this evidence. Unsupported SD BIN
  identity remains unavailable.
- At least three supported UTC/boot anchors spanning two seconds on each source.
  MAVLink SYSTEM_TIME or schema-checked DataFlash GPS week/milliseconds are
  supported. The GPS adapter requires a valid reported 3D-or-better fix and a
  pinned 18-second GPS/UTC offset valid only for 2017 through 2026. It abstains
  outside that reviewed leap-policy horizon.
- An affine clock model with at most 200 ppm drift and 50 ms residual, compatible
  absolute boot origins within 500 ms, and no extrapolation outside the observed
  anchor range. These are explicit engineering policy gates, not universal
  hardware accuracy guarantees. Reports retain up to 64 indexed anchor/residual
  samples, total counts and full-data extrema, plus a 100 ms
  allowance. TLOG observations without a boot clock additionally use a bounded
  receive-clock bracket, carrying its full width as uncertainty.
- Both observed armed boundary ranges overlap. Unknown starts/ends, ordering
  conflicts, corruption, unsupported clocks and excessive gaps withhold
  verification. Arming is not proof of flight or takeoff.
- No indistinguishable same-format candidate in the bounded imported catalog.
  All known imported source/clock alternatives are checked, including unselected
  candidates. Identical-byte imports are duplicates rather than independent
  corroboration. Truncated search leaves uniqueness unestablished. New imports
  may change the current result; earlier reports remain immutable.

The present bounds are 64 imported log documents, 256 source/clock segments,
four million decoded events, 500,000 retained relevant observations, 32,768 anchors
per stream, 2,048 compacted armed boundary observations per stream, and 20,000
parameter observations per stream. Every retained anchor participates in the
clock fit and continuity checks; presentation sampling does not change those
checks. Constant armed-state runs retain their first and last observations, with
every incoming heartbeat checked for clock regression and continuity gaps before
compaction. Counts distinguish all observed heartbeats from retained boundaries.
Hitting an internal bound prevents verification. Long or fragmented recordings
can therefore remain provisional; the app must not silently call them complete.

Before attachment the engine re-reads originals and parser artifacts, re-evaluates
the current catalog and verifies exact local-to-capture record membership in the
chosen flight. A changed profile or conflicting target fails. Renderer-supplied
confidence, a saved report alone, and operator acceptance cannot manufacture
verified evidence. Attachment appends association, timeline and manifest
revisions; earlier manifests are retained. This does not automatically resolve
all retrieval requests or make the overall flight bundle complete.

## Parameters

The report preserves source-hashed parameter observations and unknown-change
windows across supported aligned logs. `observed_at` is separate from
`effective_from`; activation remains unknown without firmware-specific evidence.
An observed value change does not prove immediate controller activation. Numeric
representation differences are not necessarily different effective values.

Existing readback acquisitions are examined using actual initial/final UID,
firmware, boot and monotonic/UTC receipt evidence. A complete consistent readback
may receive an identity/time-supported preflight or postflight role when its
entire uncertainty range fits outside the armed interval within the 60-second
policy. The declared role is not used as proof. Partial, conflicting, unbracketed
or different-boot acquisitions remain provisional or rejected. These links and
their original rich snapshot hashes appear in the manifest's parameter evidence;
they do not imply an atomic or continuously known configuration.

Imported `.param` references receive descriptive value comparisons and retain
their original and canonical hashes. Their identity, capture time and flight
applicability remain unknown. Matching values never silently promote a reference
file to the historical flight configuration. Parameter attribution remains
blocked where effect times or completeness are unknown.

## API and validation

`alignment.reference` imports/reuses a completed managed chunk selected by
`flight_record_id` and optional `artifact_source_id`, returning `log` and
`reference_choices`. Select a chunk explicitly when several are available.

`alignment.analyze` takes `reference` and 1–16 `candidates`, each containing
`log_id`, `source_key` and `boot_epoch`, plus an optional `flight_record_id`.
It returns a saved `report`. `alignment.get {report_id}` and bounded
`alignment.list {limit,offset}` retrieve reports. `alignment.attach` requires
`report_id`, `flight_record_id`, a new `operation_id`, the current
`target_profile_version` and `applicability_fingerprint`. Reference import,
analysis and attachment run through cancellable jobs.

Synthetic real-wire TLOG and clock oracles exercise identity/boot conflicts,
hidden competitors, duplicate bytes, multiple intervals, prior manifest history,
readback boundaries and incomplete evidence. They do not establish behavior on
the user's actual aircraft firmware, radio, Mission Planner or QGroundControl.
