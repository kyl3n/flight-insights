# Approved collection policies

Collection can save a policy for parameter readbacks and selected onboard-log retrieval. Saving a policy or reopening the app sends nothing. Each **Prepare & watch** must explicitly approve the saved policy, its experimental direct UDP adapter, and any legacy log-transfer side effects. Passive recording remains a separate process and continues when acquisition is withheld or cancelled.

The request endpoint must be bidirectional and use a different local port from the observer. Both profiles must name the same aircraft, system/component and nonzero expected UID. A Mission Planner/QGC forwarded stream alone does not establish a request channel. The adapter is tested with synthetic peers; no aircraft/GCS hardware adapter is represented as qualified. Unknown aircraft identity still permits ordinary passive recording without a policy.

The observer must receive a fresh selected disarmed heartbeat and UID evidence without source ambiguity. The acquisition then independently requires fresh matching UID and boot-clock messages on its own endpoint. Missing evidence leaves the policy waiting; it never generates substitute heartbeats. If the forwarded stream does not include identity evidence, provide that stream or use the separately reviewed manual readback workflow. Recording does not establish acquisition readiness.

A policy can request one preflight enumeration when the watch first qualifies, and separate postflight/preflight enumerations after each latest observed disarm. A request is never retrospectively labelled postflight for an older interval after another arm. Unknown-end intervals remain missing evidence. Arming, stale/unknown state or identity, firmware, clock-segment or coverage discontinuities revoke active parameter requests. Failed/interrupted requests are retained and are not blindly retried.

Preflight freshness has an explicit operator-reviewed budget of 1–300 seconds (default 60). Observed parameter traffic after completion, identity/firmware/clock changes, gaps, unknown state or expiry invalidate that readback even within the budget. This conservative rule can invalidate an unchanged parameter retransmission too. An invalidated readback never silently becomes eligible again. Complete enumeration remains **non-atomic**, and a freshness label permits provisional review only; historical flight applicability remains unknown until separately supported association establishes more.

If selected-log retrieval is enabled, every observed closed interval gets a persistent retrieval intent tied to its flight record. No listing is sent until the operator explicitly stops the observer. The policy then lists actual remote log IDs. The operator selects one returned ID and declares that it is closed; download rechecks its metadata and validates received bytes using the [read-only acquisition workflow](read-only-acquisition.md). The declaration is not proof of logger closure, aircraft/flight matching, or resumed logging. Returned originals and hashes remain provisional evidence. Choosing a log never makes the flight manifest complete.

`LOG_REQUEST_*` can suspend onboard logging. Both the saved policy and this Prepare approval must acknowledge that risk. Verify logging has resumed before another flight even if `LOG_REQUEST_END` was transmitted. A new active observer blocks legacy log transfer through the normal acquisition safety gate.

## API and persistence

- `orchestration.preview {policy}` returns normalized policy, `policy_fingerprint`, adapter capability and limitations; it opens no socket.
- `orchestration.policy.save {policy,operation_id}` persists an immutable policy. `orchestration.policy.list {limit,offset}` pages saved policies.
- Prepare adds `acquisition_policy: {policy_id,policy_fingerprint,confirm_automatic_readback:true,confirm_experimental_adapter:true,confirm_log_transfer_side_effects:true}`; the last acknowledgment is required only when log retrieval is enabled. The prepare operation ID binds the entire reviewed profile and approval.
- `orchestration.status {capture_session_id?,limit,offset}` exposes runs and requests independently of ordinary flight-manifest completeness. Requests include their flight ID where known, role, job/acquisition IDs, retained source references, freshness and provisional applicability. Results include counts and pagination.
- `orchestration.retrieval.select {request_id,listing_id,log_id,confirm_selected_log_closed:true,operation_id}` starts the explicitly selected download once. Repeating the same operation returns the original request; changing its payload is rejected.

Policy fields are `aircraft_id`, direct acquisition `connection`, `synthetic`, `preflight_parameters`, `postflight_parameters`, `log_retrieval` (`disabled` or `selected_after_stop`), `confirm_log_transfer_side_effects`, `preflight_freshness_s`, `max_requests_per_capture` (1–256, default 64), and optional acquisition `limits`. The observer and acquisition provenance flags must agree. See the [acquisition contract](read-only-acquisition.md) for connection fields and bounded transport limits.

The policy monitor runs independently of UI polling. It keeps versioned run/request transitions in its own schema-versioned tables and retains original acquisition files through Store. Request/job failure does not stop the recorder. Process exit suspends authorization; startup only marks demonstrably dead-owner work interrupted and never sends or resumes jobs. Matching live owners are left untouched. A retry of an old Prepare UUID cannot reactivate its policy; a new explicit Prepare is required.

Synthetic verification:

```sh
uv run --frozen python -m pytest engine/tests/test_orchestration.py engine/tests/test_acquisition.py -q
```
