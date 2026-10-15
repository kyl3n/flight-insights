# Live collection and flight evidence

This slice implements the receiving and evidence-management part of Revision 2. It is not the complete Stage 1 workflow. The app cannot guarantee recovery of a file that the aircraft or GCS never recorded, and it does not report a bundle complete merely because filenames, dates, system IDs or parameter hashes agree.

## Collect every flight consistently

Use the flight record and immutable manifest as the collection checklist. The app automatically gives each qualifying observed armed interval its own record and associates recorder evidence by source and recorded span. Keep native GCS and onboard logging enabled as independent sources. An armed interval is not automatically classified as flight.

Before a flight, declare the aircraft/profile and verify the actual hardware UID and firmware. With automatic capture enabled, Start collecting authorizes bounded preflight readbacks when the configured bidirectional endpoint meets the documented gates; retain and review their results. During collection, monitor gaps and recorder health. After disarm, retain a postflight readback, stop the app observer before legacy onboard-log transfer, retrieve a selected actual log ID or scan an explicitly chosen SD/export folder, and inspect content-based candidates against the intended flight record. Verify onboard logging has resumed before another flight.

The acquisition's preflight/postflight label is an operator declaration. The separate alignment workflow can infer a temporal role from matching UID/firmware, boot clocks and bounded receipt evidence; the label alone never establishes it. Readback snapshot JSON retains richer evidence than a plain parameter export and is not a legacy imported-snapshot ID. Even an identity/time-supported readback cannot prove the complete effective flight-time parameter state. Do not substitute the currently loaded settings for missing historical evidence.

After collection stops, the library can automatically run the existing independent-log alignment rule against eligible imported candidates. Original hashes, UID/firmware, supported clocks, exact armed spans and the bounded candidate universe must pass again before attachment. Ambiguity, missing evidence and prior rejected associations are not upgraded. This performs local evidence association only; it sends no aircraft traffic.

Missing or ambiguous artifacts stay visible as partial/provisional evidence. The app does not block aircraft arming, configure a GCS logger, claim a complete flight from filenames, or silently relabel an uncertain file. [Content alignment](evidence-alignment.md) can verify a source/interval association under a documented UID, firmware, reported-clock and armed-boundary rule with no competing catalog candidate. This is narrower than authenticated physical identity or guaranteed collection. Qualified GCS adapters, real Q22 evidence and device validation remain required.

## Start collection from Flights

1. In **Setup**, configure the app's UDP endpoint and select the actual aircraft.
   Your radio/router can supply both the app and any GCS. QGC one-way forwarding
   supports incoming recording; parameter requests need a return-capable route.
   Keep the existing GCS connection and native/onboard logging enabled.
2. If automatic parameter capture is enabled, enter the peer's return port and
   actual controller UID. Recording and readback then share one app socket.
   Existing receive-only profiles and the legacy dedicated acquisition mode
   remain available; missing identity does not prevent incoming recording.
3. Select **Flights → Start collecting**. This starts the saved connection and,
   if enabled, approves its bounded preflight/postflight policy for this capture.
   Saving setup or reopening the app does not start traffic. The recorder owns
   the UDP socket exclusively. Shared readback sends only the allowlisted
   requests after fresh disarmed/identity/clock gates; see [read-only acquisition](read-only-acquisition.md).
4. Keep the app open. Navigation does not stop collection. A qualifying arm
   transition creates an armed interval and durable flight record. Starting
   already armed leaves the actual start unknown; an armed bench test does not
   establish airborne flight. Current parameter evidence and gaps remain visible.
5. **Stop collecting** seals files and queues archival. It does not command
   disarm or certify a completed flight. Completed archived telemetry can enter
   the flight library automatically. Import onboard BIN/LOG evidence for richer
   analysis; use content alignment when exact managed-flight linkage is needed.

**Collection details** retains profile preview, manifests, version declarations,
original-file recovery, acquisition policies and evidence alignment. Closing the
app ends collection, with a native warning while it is running. Unexpected exits
retain interrupted evidence and unknown interval ends; recording never resumes
silently after restart.

No automatic host configuration change is part of starting a watch. Correct the physical path, receiver destination and GCS forwarding configuration first. The Q22 reference preserves the separate `10.0.0.5/24` drone interface and Internet DHCP interface without bridging, NAT, or an extra drone default gateway.

## Files, intervals and parameters

The worker retains raw UDP envelopes with receive times/endpoints, validated MAVLink TLOG chunks, and an append-only event journal. Chunk digests, byte counts and capture-global record ranges accompany completed files. Raw bytes from accepted sender addresses include malformed frames within the configured resource limits; excluded sender addresses are counted and discarded before archival, and those bytes cannot establish a qualifying aircraft heartbeat. TLOG records preserve received wire frames, not invented vehicle timestamps.

The Flights library can group an app-recorded TLOG interval with its own capture using the archived original hash and exact capture-global arm/disarm record span. Its saved environment is labelled capture provenance; this grouping does not verify an independent imported log or grant complete parameter applicability.

Each armed interval has a flight record and append-only manifest revisions. Original source bytes are archived under SHA-256. A TLOG chunk may overlap more than one interval, and one interval may span multiple chunks. Associations carry explicit source filters and record spans; unknown ranges from interrupted chunks remain capture-level evidence. Automatic recorder membership establishes capture provenance, while physical-aircraft identity remains provisional. Manual imported-log candidates are also provisional or rejected; a user selection cannot force verification over a contradiction.

Passive `PARAM_VALUE` observations retain source/component/boot scope, timing, count/index evidence and value-encoding limits. Completeness, consistency, freshness and flight-time applicability are reported separately. Receiving a full-looking enumeration does not prove an atomic readback or the settings effective throughout a flight. The app never substitutes a current parameter export for historical settings. Existing offline BIN/LOG/TLOG and explicit-unit CSV comparisons remain available with their existing applicability/confounder gates.

Synthetic/replay events do not create managed live flight records. Test fixtures can exercise the declared-live transport in isolated temporary catalogs; they are never shipped as actual aircraft evidence. CRC validation establishes framing, not authentication. Unsigned replay cannot be universally detected, and an observed UID is not cryptographic proof.

## Failure and scope boundaries

- Missing/stale heartbeat, discontinuous boot evidence, identity conflicts and recorder gaps leave state unknown or uncertain. Silence never implies disarm.
- The worker has exclusive UDP ownership, bounded source/frame/capture sizes, private per-user storage, disk reserve checks and durable checkpoints. Kernel packet loss is not fully observable; checkpoints and sequence evidence do not guarantee lossless reception.
- The recorder runs separately from analysis work. Automatic library analysis yields while collection is active to keep its shared background worker available for parameter capture. Both still share CPU and disk resources; real-hardware load, storage-failure and power-loss qualification remain open.
- Archive jobs are cancellable and expose durable outcomes. Cancellation or restart retains original capture files and offers explicit recovery. Archived files and older manifest revisions remain available.
- Explicit preflight/postflight readbacks and selected onboard-log requests are experimental; supported packets and disarmed identity gates are described in [read-only acquisition](read-only-acquisition.md). The operator must review legacy log-transfer side effects. No qualified GCS adapter or universal automatic retrieval is claimed.
- Selected SD/export folders and acquired log sources can be indexed for content-based candidates. Observed UID, firmware, clock anchors and armed spans support review; filenames and dates cannot verify a match. The separate alignment action evaluates all bounded catalog alternatives and attaches only freshly supported matches with exact membership in an archived managed recorder chunk. Unsupported SD BIN identity or clock evidence remains provisional. Synthetic imports cannot be linked to declared-live flight records.
- Bounded raw SkyPulse history preserves responses and receipt times without inventing firmware-specific RF metrics. Airborne segmentation, normalized radio metrics and video decode qualification remain open. Missing evidence stays visible; this preview never grants a complete-flight readiness claim.
- Production network repairs are diagnosis-only until the native privilege helper and rollback requirements are qualified. No aircraft controls or parameter writes are exposed.

## Boundary contracts

`session.preview` normalizes the declared profile and returns its applicability fingerprint. `session.prepare_and_watch`, `session.stop`, and association mutations require an `operation_id`, matching `target_profile_version`, and the previewed `applicability_fingerprint`. Reusing an operation ID with different content fails; repeating the same start never launches another worker. A persisted failed start is not silently retried.

`session.status` reports capture/recorder health, readiness and flight-record summaries. `session.detail` returns a chosen manifest revision, source associations and missing retrieval requests. `associations.evaluate` validates an explicit imported-log source/boot/span candidate, and `associations.review` can retain a provisional association or reject it. Renderer requests cannot inject recorder events, change an aircraft's observed armed state, or claim verified evidence.

Legacy analysis `session` documents remain distinct from managed CaptureSession, ArmedInterval and FlightRecord entities. Existing source/document IDs and schema version 1 remain intact; the new collection tables have their own version metadata. The supplied [Revision 2 examples](handoff/PROVENANCE.md) are design examples only, with preserved hashes and prior handoff bytes.
