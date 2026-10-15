# Approved collection policies

The normal **Setup → Flights** workflow can enable automatic parameter capture
through the same app UDP endpoint used for recording. Enable it in Setup, enter
the actual return-capable peer port and controller UID, then use **Start
collecting**. That action creates and approves a bounded policy for this capture.
Saving setup, saving a policy or reopening the app sends nothing. Existing
receive-only profiles remain receive-only after upgrade.

The standard policy requests current preflight and postflight parameters, uses a
60-second preflight freshness budget, enables stale-preflight refresh and permits
at most 64 requests per capture. It does not schedule onboard log requests. The
advanced collection controls retain explicit saved-policy selection and a legacy
dedicated UDP mode for existing configurations.

Shared mode uses one exclusive local UDP socket. The recorder retains incoming
bytes and passes the same observations to the readback adapter; the socket owner
independently validates every outgoing request. A radio/router must deliver the
selected aircraft's telemetry and provide a return path to the exact configured
peer. QGC one-way forwarding cannot supply that path. Different GCS software does
not change this app-owned protocol. Actual GCS/aircraft routing remains subject to
field qualification.

The app uses its own MAVLink source identity (default `254:191`), with optional
advanced client IDs. Incoming traffic using that same identity blocks readback;
recording continues. See [request identity and gates](read-only-acquisition.md).

A fresh selected disarmed heartbeat permits the narrow `AUTOPILOT_VERSION`
bootstrap request if the declared UID has not yet been observed. Full parameter
requests additionally require matching UID and fresh boot-clock observations.
Stale, ambiguous or armed state withholds requests while recording continues. The
app does not generate GCS heartbeats or infer disarm from silence.

A policy requests a preflight enumeration when the watch first qualifies, and
separate postflight/preflight enumerations after the latest observed disarm. An
older interval does not receive a retrospectively labelled postflight capture
after another arm. Unknown-end intervals remain missing evidence. Arming,
identity/firmware/clock changes or coverage discontinuities revoke active
parameter requests. Failed, partial or interrupted requests remain visible and
require a new explicit collection attempt after the problem is resolved.

Preflight freshness has a policy budget of 1–300 seconds, default 60. Observed
parameter traffic after completion, identity/firmware/clock changes, gaps, unknown
state or expiry invalidate the readback; an unchanged parameter retransmission
can also invalidate it. With `automatic_preflight_refresh` enabled, a completed
stale readback gets a new request after at least 30 seconds and only while live
gates allow it and the capture request budget remains. Each attempt retains its
own evidence. Old policies default this refresh option to false. No invalidated
readback is silently made eligible again.

A complete enumeration remains **non-atomic**. Freshness allows provisional
review, not proof of effective settings throughout a flight. Historical
applicability requires supported exact flight association. Library analysis
pauses during collection to avoid occupying the worker needed for readback;
a queued request is still not a completed capture before arming.

If selected-log retrieval is enabled, every observed closed interval gets a persistent retrieval intent tied to its flight record. No listing is sent until the operator explicitly stops the observer. The policy then lists actual remote log IDs. The operator selects one returned ID and declares that it is closed; download rechecks its metadata and validates received bytes using the [read-only acquisition workflow](read-only-acquisition.md). The declaration is not proof of logger closure, aircraft/flight matching, or resumed logging. Returned originals and hashes remain provisional evidence. Choosing a log never makes the flight manifest complete.

`LOG_REQUEST_*` can suspend onboard logging. Both the saved policy and this Prepare approval must acknowledge that risk. Verify logging has resumed before another flight even if `LOG_REQUEST_END` was transmitted. A new active observer blocks legacy log transfer through the normal acquisition safety gate.

## API and persistence

- `orchestration.preview {policy}` returns normalized policy, `policy_fingerprint`, adapter capability and limitations; it opens no socket.
- `orchestration.policy.save {policy,operation_id}` persists an immutable policy. `orchestration.policy.list {limit,offset}` pages saved policies.
- Prepare adds `acquisition_policy: {policy_id,policy_fingerprint,confirm_automatic_readback:true,confirm_experimental_adapter:true,confirm_log_transfer_side_effects:true}`; the last acknowledgment is required only when log retrieval is enabled. The prepare operation ID binds the entire reviewed profile and approval.
- `orchestration.status {capture_session_id?,limit,offset}` exposes runs and requests independently of ordinary flight-manifest completeness. Requests include their flight ID where known, role, job/acquisition IDs, retained source references, freshness and provisional applicability. Results include counts and pagination.
- `orchestration.retrieval.select {request_id,listing_id,log_id,confirm_selected_log_closed:true,operation_id}` starts the explicitly selected download once. Repeating the same operation returns the original request; changing its payload is rejected.

Policy fields are `aircraft_id`, direct acquisition `connection`, `synthetic`, `preflight_parameters`, `postflight_parameters`, `log_retrieval` (`disabled` or `selected_after_stop`), `confirm_log_transfer_side_effects`, `preflight_freshness_s`, `max_requests_per_capture` (1–256, default 64), `automatic_preflight_refresh` (default false for legacy policies), and optional acquisition `limits`. Shared policies use `connection.transport: "shared_recorder"` and the recorder’s exact local endpoint and selected remote peer. Legacy dedicated policies require a distinct local socket. The observer and acquisition provenance flags must agree. See the [acquisition contract](read-only-acquisition.md) for connection fields and bounded transport limits.

The policy monitor runs independently of UI polling. It keeps versioned run/request transitions in its own schema-versioned tables and retains original acquisition files through Store. Request/job failure does not stop the recorder. Process exit suspends authorization; startup only marks demonstrably dead-owner work interrupted and never sends or resumes jobs. Matching live owners are left untouched. A retry of an old Prepare UUID cannot reactivate its policy; a new explicit Prepare is required.

Synthetic verification:

```sh
uv run --frozen python -m pytest engine/tests/test_orchestration.py engine/tests/test_acquisition.py -q
```
