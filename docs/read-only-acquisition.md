# Explicit MAVLink readback

The acquisition adapter requests parameter enumeration and selected onboard log
inventory/data through an explicitly configured **bidirectional UDP endpoint**.
The normal Setup workflow shares the recorder's one app socket for recording and
parameter readback. Advanced dedicated-socket acquisition remains available for
existing configurations. Neither mode supplies a GCS heartbeat or creates a
return path through one-way forwarding. The adapter uses synthetic protocol
validation; actual aircraft/GCS routing remains unqualified.

In **Setup**, supply the computer's receiving IPv4/app port, selected peer IPv4
and return port, and select the system/component IDs. In shared mode the
controller UID is discovered during collection unless an optional UID lock is
configured. Legacy dedicated-socket acquisition still requires an explicit UID. The
peer must already deliver autopilot heartbeats and a message containing
`time_boot_ms`, such as `SYSTEM_TIME`. A received heartbeat does not establish
bidirectional reachability. In shared mode the recorder keeps every accepted raw
UDP datagram and acquisition reads those same receipts; it does not bind a second
socket or compete with the recorder for replies.

The service first waits for a fresh **disarmed** heartbeat from the selected autopilot. It can then send one narrowly defined version request to obtain `AUTOPILOT_VERSION`. A reported nonzero UID is pinned for the acquisition and the shared recorder's lifetime; a configured UID lock must also match. Parameter/log requests additionally require fresh boot-clock observations. Missing prerequisites produce a partial acquisition with an explanation. An aircraft without a supported reported UID can still be recorded and its logs analyzed, but cannot complete automatic readback. The service never substitutes a heartbeat or assumes disarmed because packets stopped.

Flight Lab defaults to MAVLink source system `254` and component `191` for its
requests. Optional `client_system_id` and `client_component_id` override those
values; each acquisition records its effective client identity. The app system
ID must differ from the selected vehicle system ID. Incoming traffic with the
same app system/component pair, including a routed echo, interrupts readback.
The shared socket owner also validates the outgoing frame's configured source
identity. Choose unused client IDs in Setup's advanced settings when needed;
ordinary GCS traffic using a different identity is preserved alongside aircraft
telemetry. A radio, router or controller that filters request sources must allow
the configured app identity through its established route; Flight Lab does not
change those aircraft or router settings automatically.

An explicit preflight/postflight label records operator intent. It does not establish that an acquisition occurred before/after a particular flight, or that the resulting settings were effective in flight. Captures separate enumeration completeness, observed consistency, freshness and historical applicability. They are never atomic.

## Onboard logging side effects

`LOG_REQUEST_LIST` and `LOG_REQUEST_DATA` can suspend onboard logging on some firmware. Listing and download therefore require a separate `confirm_log_transfer_side_effects: true` acknowledgment. Verify that onboard logging has resumed before the next flight. Do not run concurrent GCS log transfers against the same vehicle.

The service attempts `LOG_REQUEST_END` cleanup only while its disarmed, source, UID and clock gates remain valid. If the aircraft arms, identity becomes ambiguous, cancellation occurs or the live gate expires, sending stops; cleanup may be withheld. A persistent recovery warning states that remote transfer/logging state is unknown. Even a transmitted cleanup request is **not verified logging resumption**.

## Allowed messages and live gates

Outgoing traffic across the supported modes is limited to:

- `PARAM_REQUEST_LIST` (21) and `PARAM_REQUEST_READ` (20).
- `LOG_REQUEST_LIST` (117), `LOG_REQUEST_DATA` (119), and `LOG_REQUEST_END` (122).
- `COMMAND_LONG` (76) only for hardcoded `MAV_CMD_REQUEST_MESSAGE` (512), requesting `AUTOPILOT_VERSION` (148), with all other parameters zero and the exact selected target.

The shared recording socket permits only the two parameter request types and the version bootstrap. Onboard log requests require collection to be stopped and retain the separate transfer acknowledgment. No renderer-supplied command number is accepted. The service never sends `PARAM_SET`, `LOG_ERASE`, flight commands, modes or heartbeats. The version request is the only request permitted before UID evidence arrives, and it still requires the fresh selected disarmed heartbeat.

Each outgoing operation rechecks the live gate and drains queued inbound evidence first. External application flight/recorder checks run before binding, during waits and before sends. Stale/missing heartbeats or boot clocks, arming, UID/firmware/capability changes, conflicting endpoints, corrupt selected-source framing, signed/unsigned changes or boot-clock regressions interrupt acquisition. Clock regression includes unclassified reboot, wrap or reordering; it is not silently joined into one generation.

CRC and a matching reported UID do not authenticate UDP or universally prevent replay. MAVLink signature presence is recorded, but signatures are not cryptographically verified. The profile is an operator declaration, not physical enrollment.

## RPC contract

`acquisition.preview` is read-only and opens no socket. Supply `connection` and optional `limits`; it returns normalized `connection`, `limits`, `applicability_fingerprint`, `prerequisites`, `outgoing_allowlist` and `limitations`.

```json
{
  "connection": {
    "local_address": "192.0.2.10",
    "local_port": 14551,
    "remote_address": "192.0.2.20",
    "remote_port": 14550,
    "system_id": 1,
    "component_id": 1,
    "expected_uid": "123",
    "feed_origin": "direct_bidirectional",
    "transport": "shared_recorder"
  }
}
```

The collection profile enables the shared socket with `connection.readback: {"remote_port": 14550}` (plus optional `client_system_id` / `client_component_id`) and the same local address, app port, selected peer, aircraft IDs and optional UID lock. Omit `expected_uid` from both profiles for automatic identification during collection; the observed UID is retained in acquisition evidence, never written into the saved policy. Omit `transport` or use `dedicated_udp` for the legacy distinct-socket mode. After collection stops, shared-mode acquisition can use that endpoint exclusively only with an explicit UID lock; automatic discovery requires the active recorder. An active observer blocks onboard log transfer.

These documentation addresses and UID are examples, not Q22 configuration or a hardware baseline. Both ports must be explicit integers from 1 through 65535. The expected UID is a nonzero uint64 decimal string.

Long operations run through the existing job service and return a job for polling/cancellation. All three methods require `connection`, `aircraft_id`, `synthetic`, a new `operation_id`, and the preview's `applicability_fingerprint`:

| Method | Additional fields |
| --- | --- |
| `acquisition.parameters.capture` | `role`: `preflight`, `postflight` or `unknown` |
| `acquisition.logs.list` | `confirm_log_transfer_side_effects: true` |
| `acquisition.logs.download` | `confirm_log_transfer_side_effects: true`, completed `listing_id`, and an actually listed numeric `log_id` |

The job's successful handler result contains `acquisition` and `operation_replayed`. Inspect the acquisition's own `state`: a finished job can contain `partial` or `interrupted` evidence, not a complete transfer. Exact operation retries return the previously saved result without sending again. Reusing an operation ID with different arguments is rejected. An unfinished operation is not automatically resent after restart; use a new explicit operation after reviewing retained evidence.

`acquisition.list {limit,offset}` returns bounded summaries, `total`, `next_offset`, and any recovery warnings; maximum page size is 100. `acquisition.detail {acquisition_id}` returns the saved acquisition, source references and retained-file status. Cancellation records the acquisition before the job becomes cancelled; refresh the acquisition list to find it.

## Parameter evidence

Enumeration collects indexed `PARAM_VALUE` replies and retries missing indices within request/time bounds. Signed 16-bit `PARAM_REQUEST_READ.param_index` limits indexed retries to 32767; unavailable higher indices remain incomplete rather than wrapping into a different request. The accumulator supports at most 50,000 reported parameters. Changing counts, index/name conflicts, conflicting values, invalid types and incomplete coverage prevent a complete claim.

Integer interpretation follows the observed capability bits. Bytewise integers use the original four payload bytes; C-cast values must be finite, integral and within a range whose original integer is not lost to binary32 precision. Unsupported 64-bit legacy parameter representations, ambiguous encoding capabilities and nonfinite values stay ineligible. Saved evidence retains exact typed representations and original wire references; a UI-rounded value is not used as the canonical hash.

The archived `parameter_readback_snapshot` is rich JSON, not a Mission Planner/QGC `.param` export. Preserve its completeness/consistency and observed clock bounds. Do not pass it through the text parameter importer or treat its `snapshot_id` as a legacy imported-snapshot catalog ID. Historical flight applicability remains unknown until separate supported association/corroboration establishes it.

## Log listing and download evidence

A listing retains actual `LOG_ENTRY` IDs, reported sizes and timestamps. `time_utc=0` remains unknown; no SD filename is guessed. IDs need not be contiguous. Listing completeness means receipt of the reported count with consistent metadata, not independently verified logger closure.

Before and after a download, the service re-observes the selected entry and checks size/time stability. It receives bounded windows, checks offsets/counts, retries missing bytes, deduplicates identical retransmissions and rejects conflicting overlap. All expected bytes plus an explicit matching zero-count EOF are required for `transfer_complete`. Silence or a short packet is insufficient. A changing/approximate size or missing EOF leaves a partial candidate.

SHA-256 identifies the bytes received locally. MAVLink log transfer supplies no authenticated remote content checksum. Stable size plus EOF does not prove that the logger closed the file, nor that it belongs to the selected flight. `logger_closed` and historical applicability remain unknown; association stays provisional until content analysis supplies stronger evidence.

Complete windows form a contiguous prefix. Interrupted windows can contain gaps; `download.coverage.json` records actual received byte ranges. Never interpret unreceived sparse-file bytes as measured data. The `onboard_log_candidate` source role can be sent to content-based log reconciliation; transcript and parameter-snapshot roles cannot be treated as BIN/TLOG files.

## Bounds, cancellation and recovery

Defaults are a 120-second operation budget, 10-second bootstrap budget, three bounded retry rounds, 128 MiB maximum reported log size and 256 MiB transcript budget. Reviewed `limits` can increase the operation budget up to one hour, log size to 512 MiB and transcript budget to 2 GiB. Heartbeat/boot/identity freshness thresholds are transport policies, not vehicle safety or tuning baselines.

The custom binary `exchange.mavudp` original preserves received and sent datagrams with endpoint and monotonic/UTC clocks. Shared-mode inbound clocks are the recorder’s original receipt clocks; outbound transcript clocks describe request submission, not independently observed wire delivery. The worker separately records whether it accepted a send. Dedicated mode records socket receipt/submission clocks. It is not a TLOG. Each record follows the `Q22ACQ1\0` header using `>BQQ4sHI` metadata: direction (0 receive, 1 send), monotonic nanoseconds, UTC microseconds, IPv4 address, port, byte length, followed by the datagram bytes.

Completed originals are archived through Store with `acquisition_method: app_owned_readonly_mavlink_acquisition`. Source references include SHA-256 and a role. Cancellation stops transport promptly and preserves all locally retained partial files; it does not wait to copy a large partial download. `sealed`, `durability` and `archived` report which guarantees were achieved.

Startup recovery only inspects owned attempt directories and checks prior process identity before declaring interruption. It does not replay requests or disturb a matching live owner. Dead-owner files become visible as interrupted acquisitions with `crash_recovered_unverified` durability; their presence is not a claim of a complete transfer. Recovery scanning and metadata reads are bounded, and any withheld recovery is reported. Retain original evidence and explicitly retry the needed operation after resolving the live-gate or transport issue.

Synthetic protocol tests are in [test_acquisition.py](../engine/tests/test_acquisition.py):

```sh
uv run --frozen python -m pytest engine/tests/test_acquisition.py engine/tests/test_shared_transport.py -q
```
