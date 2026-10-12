# Prepare & watch — passive collection preview

This slice implements the receiving and evidence-management part of Revision 2. It is not the complete Stage 1 workflow. The app cannot guarantee recovery of a file that the aircraft or GCS never recorded, and it does not report a bundle complete merely because filenames, dates, system IDs or parameter hashes agree.

## Use the observer

1. Configure a separate live MAVLink forwarding output in the GCS or router. Keep its normal aircraft connection and native logging enabled. QGC forwarding is one-way; it is suitable for this passive receiver but cannot support parameter or onboard-log requests. Mission Planner behavior must be checked against the actual version.
2. Open **Prepare & watch** and declare the aircraft/profile version, local receiving IPv4 address, separate UDP port, expected sender, autopilot system/component IDs and optionally the expected hardware UID. These are declarations, not proof of physical identity. The Q22 handoff's `14550` GCS port and `5600` video port are not observer defaults.
3. Preview the setup and explicitly confirm that it is a live forwarded feed. **Prepare & watch** persists a capture UUID before starting an exclusive receive-only worker. An occupied or invalid local endpoint fails visibly. The app sends no MAVLink packet, substitute heartbeat, parameter write or flight command.
4. Keep the app open. Navigation does not stop collection. Observe recorder health, gaps, identity limitations and missing evidence. Starting already armed records an unknown start. A fresh qualifying armed transition creates one armed-interval UUID and one flight record with an aircraft-scoped display number. An armed bench test remains unclassified; it is not automatically a flight.
5. **Stop app observer** seals recorder files and queues content-addressed archival. It sends no aircraft command and does not imply disarming. A subsequent watch can start while earlier files are being archived. Closing the app ends its engine and recorder; a native close warning is shown while collection is active. An unexpected exit retains an interrupted capture and an unknown interval end. Recovery requires explicit archival of retained evidence; the app never silently restarts recording.

No automatic host configuration change is part of starting a watch. Correct the physical path, receiver destination and GCS forwarding configuration first. The Q22 reference preserves the separate `10.0.0.5/24` drone interface and Internet DHCP interface without bridging, NAT, or an extra drone default gateway.

## Files, intervals and parameters

The worker retains raw UDP envelopes with receive times/endpoints, validated MAVLink TLOG chunks, and an append-only event journal. Chunk digests, byte counts and capture-global record ranges accompany completed files. Raw bytes include rejected senders and malformed data within the configured resource limits; those bytes cannot establish a qualifying aircraft heartbeat. TLOG records preserve received wire frames, not invented vehicle timestamps.

Each armed interval has a flight record and append-only manifest revisions. Original source bytes are archived under SHA-256. A TLOG chunk may overlap more than one interval, and one interval may span multiple chunks. Associations carry explicit source filters and record spans; unknown ranges from interrupted chunks remain capture-level evidence. Automatic recorder membership establishes capture provenance, while physical-aircraft identity remains provisional. Manual imported-log candidates are also provisional or rejected; a user selection cannot force verification over a contradiction.

Passive `PARAM_VALUE` observations retain source/component/boot scope, timing, count/index evidence and value-encoding limits. Completeness, consistency, freshness and flight-time applicability are reported separately. Receiving a full-looking enumeration does not prove an atomic readback or the settings effective throughout a flight. The app never substitutes a current parameter export for historical settings. Existing offline BIN/LOG/TLOG and explicit-unit CSV comparisons remain available with their existing applicability/confounder gates.

Synthetic/replay events do not create managed live flight records. Test fixtures can exercise the declared-live transport in isolated temporary catalogs; they are never shipped as actual aircraft evidence. CRC validation establishes framing, not authentication. Unsigned replay cannot be universally detected, and an observed UID is not cryptographic proof.

## Failure and scope boundaries

- Missing/stale heartbeat, discontinuous boot evidence, identity conflicts and recorder gaps leave state unknown or uncertain. Silence never implies disarm.
- The worker has exclusive UDP ownership, bounded source/frame/capture sizes, private per-user storage, disk reserve checks and durable checkpoints. Kernel packet loss is not fully observable; checkpoints and sequence evidence do not guarantee lossless reception.
- The recorder runs separately from analysis work. Both still share CPU and disk resources; real-hardware load, storage-failure and power-loss qualification remain open.
- Archive jobs are cancellable and expose durable outcomes. Cancellation or restart retains original capture files and offers explicit recovery. Archived files and older manifest revisions remain available.
- Full preflight/postflight parameter requests, tested bidirectional adapters, automatic BIN download/content matching, rolling SkyPulse radio history, airborne segmentation and video decode qualification remain unimplemented. Missing evidence stays visible; this preview never grants a complete-flight readiness claim.
- Production network repairs are diagnosis-only until the native privilege helper and rollback requirements are qualified. No aircraft controls or parameter writes are exposed.

## Boundary contracts

`session.preview` normalizes the declared profile and returns its applicability fingerprint. `session.prepare_and_watch`, `session.stop`, and association mutations require an `operation_id`, matching `target_profile_version`, and the previewed `applicability_fingerprint`. Reusing an operation ID with different content fails; repeating the same start never launches another worker. A persisted failed start is not silently retried.

`session.status` reports capture/recorder health, readiness and flight-record summaries. `session.detail` returns a chosen manifest revision, source associations and missing retrieval requests. `associations.evaluate` validates an explicit imported-log source/boot/span candidate, and `associations.review` can retain a provisional association or reject it. Renderer requests cannot inject recorder events, change an aircraft's observed armed state, or claim verified evidence.

Legacy analysis `session` documents remain distinct from managed CaptureSession, ArmedInterval and FlightRecord entities. Existing source/document IDs and schema version 1 remain intact; the new collection tables have their own version metadata. The supplied [Revision 2 examples](handoff/PROVENANCE.md) are design examples only, with preserved hashes and prior handoff bytes.
