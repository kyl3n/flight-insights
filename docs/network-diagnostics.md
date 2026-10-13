# Network diagnostics milestone

Revision 2 makes production host repairs **diagnosis-only on Windows, Linux and macOS**. The app has no qualified signed privilege helper, manager-aware writer or supervised timed rollback. Administrator privileges do not enable the old Python mutation path. The command previews, ownership checks and receipt format below remain available for review and manual recovery; automatic execution tests use an explicitly injected fake backend. They do not qualify a production repair helper.

`network.profile` returns the supplied Q22 reference: drone NIC `10.0.0.5/24`, SkyPulse ground `10.0.0.2`, receive destination `10.0.0.5`, Internet Wi-Fi on DHCP `192.168.0.x` with gateway `192.168.0.1`, H.265 UDP `5600`, and GCS MAVLink UDP `14550`. These are historical expectations, not detected settings or an enrolled aircraft. The historical Windows firewall profile uses wired interfaces, remote `10.0.0.2`, all profiles and no local-address pin; it is preserved separately from narrower proposed previews. The app does not silently replace it.

Mission Planner's documented system-wide MinGW GStreamer/H.265 path is separate from QGC's runtime. QGC forwarding is one-way and cannot establish a parameter or log request path. SkyPulse may learn a destination from incoming MAVLink: diagnostic traffic must not teach it a new receiver. The passive observer therefore sends no packets and uses an explicitly configured separate forwarding port.

The Network screen inspects this computer and explicitly configured IPv4 sources. It does not scan the LAN, send MAVLink commands, change aircraft settings, or start a second video player. Inspection works through `psutil` plus the OS routing tools: `ip` on Linux, PowerShell NetTCPIP on Windows, and `netstat`/`route` on macOS. Known system-tool locations are used when the desktop launcher's PATH is sparse. Missing tools produce a partial result and block repair planning.

Windows PowerShell output is explicitly UTF-8, with BOM-safe JSON parsing so localized interface aliases survive intact. Firewall commands encode the alias as UTF-8 data inside a fixed decoding expression, avoiding PowerShell interpretation of ASCII or smart quotes in names. Supplemental interface-query failure preserves the addresses observed through `psutil` for explicit probes, while the partial result blocks repair planning. On macOS, abbreviated route prefixes and changing `netstat` column layouts are normalized. Since `route get` omits the chosen source address, an ephemeral UDP socket uses `connect` and `getsockname` for local kernel source selection; it never sends a datagram or binds to a GCS port. Failure is reported as an unknown source address rather than guessing among aliases. The kernel-selected address is checked against the reported interface to detect inconsistent observations.

For an Internet adapter at `192.168.0.20/24` with gateway `192.168.0.1`, and a dedicated digiQ adapter at `10.0.0.10/24`, configure the digiQ host as `10.0.0.2` and verify that the reported OS route uses that dedicated adapter. Choose an adapter explicitly when previewing a repair. The app reports the route's selected interface and source address; a valid route alone does not prove that the radio sends packets back to this computer. Configure the sender's destination as the computer's device-side address and a dedicated receive port.

The UI's **digiQ / SkyPulse** source type specifically probes `/scatter_data.json` and `/events.json`. Select it only for a device/firmware that documents those endpoints; their absence on another digiQ module is not evidence of a routing failure. Add separate UDP or RTSP sources for the actual stream protocols.

If the dedicated adapter has no address in the radio subnet, a host route by itself is insufficient. A reviewed Linux or macOS address alias can add an operator-specified, verified unused address. Linux uses `noprefixroute`; macOS adds a `/32` alias. Adding the device host route is a separate reviewed step. The app never chooses an unverified static IP automatically. A successful host route only selects a path for this computer; it does not enable forwarding/NAT between networks or configure the radio's return route.

## What the checks establish

| Check | Evidence | Still unverified |
| --- | --- | --- |
| Host inspection | Adapter state, IPv4 address/subnet, default routes, OS route to each configured target | Device power, physical path, sender configuration, effective firewall policy |
| SkyPulse probe | Read-only GET of `/scatter_data.json` and `/events.json`; status, byte count, valid complete JSON | Vendor field schema, units, sample times, RF link health; a single probe does not record rolling history |
| Explicit SkyPulse history | Bounded repeated GETs, original entity bytes/hash, UTC and monotonic request/receipt times, API/parse failures and scheduling gaps | Vendor schema/units, source sample timestamps, deduplicated RF history and collection from an unconfigured second radio |
| RTSP | DESCRIBE on the exact configured URL; basic SDP structure and codec names actually advertised | Media reception/playback, decoder availability, resolution, dropped frames, glass-to-glass latency |
| UDP | Exclusive bind on one existing local IPv4 address and configured port; packets from the exact configured sender | Playback or full telemetry continuity; other senders are counted separately |
| RTP UDP | RTP header/CSRC/extension/padding structure, bounded per-sender-port/SSRC sequence observations and local arrival cadence; optionally explicitly configured H.265 single-NAL/AP/FU framing | Codec identification, FU reassembly, fresh decoded frames, exact RF loss, sender authentication, receiver playback |
| MAVLink UDP | A common HEARTBEAT packet with valid MAVLink v1/v2 framing and CRC extra 50 | Signing authentication, other message types, stream completeness, physical RF loss |

No incoming UDP during the timeout means **no packets observed**. It does not prove a firewall block. JSON reachability does not establish radio health; an SDP reply does not establish playable video. Effective firewall policy remains `unknown` because other rules and security software can override a scoped exception. An RTP framing observation is not a native video decode adapter; actual decode and consuming-player inspection remain later milestones. Separate passive MAVLink capture and recorded TLOG parsing are available; see [managed collection](managed-collection.md).

RTSP probes use unauthenticated `DESCRIBE` over TCP to a numeric IPv4 address, port and path. Credential negotiation and encrypted RTSP are not implemented. A `401`/`403` response is reported as `authentication_required`, not a route/firewall failure; verify the URL and credentials using the camera's supported player or GCS.

Use a dedicated GCS/router forwarding destination such as port `14551`; do not take over the GCS's active telemetry port. The UDP socket never enables address/port reuse and uses exclusive binding on Windows. An occupied port returns `bind_failed` with instructions to configure forwarding while leaving the GCS running.

For UDP, the UI's **Device IPv4** is the packet sender to accept, not necessarily the aircraft or radio address. If a GCS/router forwards telemetry, enter its actual sending address (including loopback when applicable), and set **Local adapter IPv4** to the address receiving that forwarding destination. Keep radio HTTP and forwarded UDP as separate sources when their sender addresses differ. Packets from other addresses are counted separately and do not satisfy the configured source check.



## Explicit raw SkyPulse history

**Capture radio history** starts only after selecting a configured SkyPulse source and requesting a duration. It polls the documented read-only `/scatter_data.json` and `/events.json` paths, preserving each body independently. Overlapping response windows are deliberately retained; identical responses still have distinct request/receipt records even when their content-addressed original bytes deduplicate. Five seconds is the proposed default interval, not a vendor guarantee. The current collector captures one explicitly selected radio; a successful ground-unit capture does not imply an air-unit capture.

`radio.capture` is an asynchronous job. It accepts a source with only `id`, `kind: skypulse`, numeric IPv4 `host`, optional port and interface, plus integer `duration_s` (1–300, default 30), `interval_ms` (1000–30000, default 5000), and `timeout_ms` (100–5000, default 1500). It does not accept credentials, custom paths, request headers, radio configuration endpoints or redirects. No request sends MAVLink or writes device settings. It starts no collector on app launch, list/detail inspection or engine restart.

```json
{
  "source": {"id": "ground-radio", "kind": "skypulse", "host": "10.0.0.2", "port": 80},
  "duration_s": 30,
  "interval_ms": 5000,
  "timeout_ms": 1500
}
```

The job result contains `{capture: summary}`. `radio.list` accepts optional `limit` (1–100) and `offset` and returns `captures`, `next_offset`, per-record read errors and a declared scan bound. `radio.detail` accepts `{capture_id}` and returns the full capture with `polls` and scheduling `gaps`. Capture states include `running`, `complete`, `partial`, `cancelled`, `failed`, `budget_exhausted`, and recovered `interrupted`/`unknown`. Here **complete** means the requested polling run finished without observed HTTP/parse/scheduling failures; it does not mean complete radio history or a healthy RF link.

Each poll includes the fixed endpoint, HTTP status when observed, exact entity byte count, SHA-256 and archived Store source, UTC/monotonic request and receipt times, framing completeness and JSON syntax status. Malformed JSON, exceeded JSON parser depth, unexpected HTTP status, transport timeout/refusal, unsupported content encoding and missed schedule slots remain different evidence. A deeply nested reply is retained as `json_unvalidated` rather than failing the collection job. Response cookies, authorization headers and raw HTTP headers are not archived. HTTP entity bytes are preserved without JSON reserialization; unknown transfer framing may be retained as explicitly labelled partial transfer-body bytes. The collector does not invent units, deduplicate history points, infer sample timestamps from receipt time or claim an RF outage from an HTTP failure.

Replies are bounded to 1 MiB of body and 64 KiB of headers under an overall per-request deadline, including slow-drip headers. One capture retains at most 32 MiB of response bodies; its local original files and content-addressed archive copies can together use roughly twice that amount. Requested duration limits polling/network waits; bounded body archival and OS filesystem flushes may finish afterward. Polls do not overlap or accumulate an unbounded request queue. If one cycle overruns the next scheduled slot, skipped slots within the requested capture window are recorded explicitly.

Cancellation uses the existing jobs control. It stops further requests while preserving prior bodies and any partial body returned by the interrupted request. Request intent is written before contacting the device; completed poll metadata and originals are flushed afterward. Atomic manifest replacement preserves the preceding checkpoint on a write failure. Temporary files close before cleanup, and Windows sharing violations use the recorder’s bounded checkpoint-replacement retry; actual Windows power-loss qualification remains pending. A process crash may leave the current request outcome unknown, but previous replies and completed local body files remain available. Detail lists up to 32 unindexed original files left between a body write and manifest publication; those files are preserved without manufacturing a completed response or flight association.

Restart recovery reads the capture owner's process identity and saved state, reports an interrupted or uncertain owner when applicable, and never resumes polling. `radio-history/<capture_id>/` retains local checkpoints and body files; originals archived through Store carry acquisition method `app_owned_readonly_radio_capture`. UTC/monotonic receipt times do not establish the clocks inside the radio's approximately 50-second rolling windows. Actual firmware responses, schema, sample identity/time units and air/ground topology are still required before RF normalization or deduplication can be qualified.

## Passive RTP and configured H.265 framing

Select **RTP** only for a dedicated forwarded/mirrored UDP destination. The existing GCS receiver remains the intended consumer; the app does not duplicate its unicast socket or start playback. The probe classifies bounded RTP headers and RTCP-shaped block headers from the configured sender. It groups RTP sequences by observed sender port and SSRC, keeping changing payload types inside that sequence scope. Source UDP ports need not equal the destination port.

Sequence gaps, duplicates, reordering and large discontinuities are reported separately. A late packet can fill a missing slot within the 4096-sequence reconciliation window. Jumps larger than 3000 sequence numbers begin a new observation segment; their cause remains unknown. Gaps at the beginning/end of a probe are unknowable, and old slots outside the window may remain unreconciled. These counters must not be presented as RF loss percentages. Arrival gaps use the computer's local monotonic receipt times, not camera frame rate or glass-to-glass latency. RTP marker bits are counted without treating them as decoded frames.

H.265 framing is optional and requires the actual sender/session's configured payload type and explicit knowledge that DONL fields are absent (`sprop-max-don-diff=0`). The port number and historic Q22 codec expectation do not establish that mapping. Sources with unknown negotiation still receive header/sequence diagnostics. DONL-present packetization and PACI are not implemented; fragment packets are inspected individually without reassembling or decoding video. Structurally plausible payload bytes do not identify a codec.

```json
{
  "sources": [{
    "id": "forwarded-video",
    "kind": "udp",
    "host": "127.0.0.1",
    "local_address": "127.0.0.1",
    "port": 5601,
    "expected_format": "rtp",
    "rtp_codec": "h265",
    "rtp_payload_type": 96,
    "h265_donl": false
  }],
  "timeout_ms": 1500
}
```

This is an example of a separately configured forwarding endpoint, not a default camera address or negotiated payload mapping. `rtp_codec`, `rtp_payload_type` and `h265_donl` must be omitted when their configuration is unknown. Explicit H.265 payload types must be integers 0–127 excluding 64–95, whose marker-bearing octets overlap RTCP multiplexing. RTCP classification checks block header lengths only, not report contents or session validity.

`network.probe` keeps its existing packet-level state and adds `checks[0].rtp`: `rtp_packets`, `rtcp_packets`, `rtcp_header_only`, `invalid_packets`, error categories, `streams_truncated`, `budget_exhausted`, and up to 16 stream summaries. Each stream records `ssrc_hex`, observed `sender_port`, payload-type counts, byte/marker counters, `sequence`, `cadence`, and optional `h265` framing counters/errors. `video_decoded` and `codec_identified` remain false. The existing 4096-datagram/time limits, source filter, exclusive bind and recorder-ownership interlock remain in force. Header parsing follows [RFC 3550](https://www.rfc-editor.org/rfc/rfc3550); the explicitly configured HEVC framing subset follows [RFC 7798](https://www.rfc-editor.org/rfc/rfc7798).

## Native repair qualification still required

The Python backend interface is an injection boundary for test fixtures and future native integration; setting its capability booleans is not a qualification procedure. Production constructs no backend. Administrator/root privileges alone do not change this, and neither the renderer nor mutable receipt JSON can provide a backend, command array or qualified status.

| Native adapter | Required implementation before enabling it | Required installed-machine evidence |
| --- | --- | --- |
| Windows | Signed narrowly scoped helper with authenticated local IPC, stable adapter identity, typed ActiveStore route and exact Windows Firewall filters, durable owned-object receipt and independent supervisor | Declined UAC, profile/interface changes, policy-managed firewall, competing route edits, helper/UI crashes and repair/rollback on supported Windows versions |
| Linux | Identify the active network manager; use its address/route transaction and checkpoint model with a narrowly authorized helper. Integrate the actual firewall manager without raw-command fallback that bypasses ownership | NetworkManager/networkd or explicitly unmanaged adapter qualification, polkit denial, manager reapply/reconnect, shared zones, concurrent edits and process/power interruption |
| macOS | Signed helper and approved network-service configuration through SystemConfiguration, stable service/adapter identity, local privacy handling, independent supervisor and durable exact receipts | Signing/notarization, Local Network privacy denial, service order/DHCP changes, Wi-Fi/Ethernet transitions and host-route/alias recovery on supported macOS releases |

Every implementation must consume a server-held typed descriptor and reviewed baseline, revalidate interface/topology/object ownership immediately before a write, preserve Internet default route and DNS, and verify both the precise addition and configured target/return path afterward. Current disarmed identity evidence must come from the engine, never a renderer boolean. A missing/armed/stale state or active recorder blocks disruptive apply and rollback. The observer must remain responsive while a helper works; holding its lock across native operations is not an interlock.

Reachability-changing operations additionally need a native supervised rollback deadline that survives UI/engine exit, with a tested policy for aircraft-state changes or lost evidence during recovery. A timer must not blindly mutate an armed or unknown-state link. Crash/timeout outcomes retain the original intent and exact observed state; no restart auto-replays a repair. Changed or ambiguously recreated external objects require manual review. Recovery receipts remain untrusted origin claims, and a privileged process must recompile allowed typed operations rather than execute persisted argv.

These are pending implementation and qualification requirements, not completed acceptance results. Mocked OS tests, source review and this Linux cloud environment cannot supply Windows/macOS signing, actual manager policy, multi-NIC continuity or radio/GCS hardware evidence. Raw SkyPulse responses can be recorded from documented read-only endpoints without schema claims; deduplicated RF histories still require actual firmware-specific sample/time/units evidence.

## Reviewed repairs and ownership

Generating a plan displays exact additions and rollback commands. Production apply and rollback return `diagnosis_only`; client-supplied commands are rejected. The retained backend contract requires a server-held plan ID, explicit confirmation, current host-state checks and fresh disarmed/identity/recorder safety evidence. Plans expire after ten minutes. Armed, stale, unknown, unverified or active-recording states block disruptive changes. No elevation helper is implemented.

| Preview/ownership model (automatic execution disabled) | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Interface-scoped `/32` host route | Supported with static protocol, unique metric, exact destination/interface/source/gateway/table/type ownership | Supported with interface index, unique metric, exact destination and next hop in ActiveStore | Reviewed static `/32` route through the selected interface, `PROTO2` marker and exact observed receipt; native qualification pending |
| Additional IPv4 address | Dedicated adapter only, no default-route adapter, no subnet overlap, unique ownership label, no implicit subnet route | Unsupported: adding an address can disable DHCP | Reviewed `/32` alias on a dedicated adapter, with unchanged original address/route state required for rollback; native qualification pending |
| Inbound UDP exception | Active firewalld only, existing zone with exactly the selected interface and no source memberships | Named Windows Firewall rule, exact interface/local address/sender/port | Manual OS-specific review required |

Linux routes, aliases and firewalld rules, and macOS routes/aliases, are runtime changes. Windows host routes use ActiveStore; Windows Firewall rules use PersistentStore and remain until rolled back or removed. Exact commands and this persistence difference appear in the plan. The app does not disable firewalls, replace existing routes, alter the default gateway, enable forwarding/NAT, change zone membership, or modify aircraft settings. Firewall rules created through other Linux backends are outside this milestone. macOS firewall modifications remain unsupported.

Rollback requires the exact native addition to match the plan and its observed receipt. Linux address ownership includes a plan-specific address label; route ownership includes its unique metric and relevant identity. Windows firewall matching includes address, interface and port filters. A changed or missing addition returns `ownership_lost`. macOS receipts must contain verified post-change state; a crash between mutation and observation requires manual review. Its alias rollback also requires the original adapter and routes to remain unchanged except for the new local address. Remove any subsequently added device host route before removing its alias. Identically recreated external changes cannot be distinguished perfectly; avoid concurrent editing of an applied repair.

**Durable recovery requires the app's data directory.** `NetworkService(data_dir=...)` stores per-plan typed receipts in `network-repairs/`. The original descriptor and baseline remain immutable while lifecycle events preserve intent, observed state, uncertain outcomes and rollback. Intent is flushed before mutation; a failed pre-write blocks the host change. Updates use a private temporary file, file flush/fsync, atomic replacement and POSIX directory fsync. An OS writer lock prevents simultaneous receipt updates and releases on process exit. Windows uses file fsync and atomic replacement; Windows filesystem/power-loss qualification remains pending. Corrupt receipts are preserved and reported rather than silently overwritten. At most sixteen unresolved repairs may be applied; recovery has a twenty-second inspection budget and marks unchecked entries unknown.

Recovery never replays an apply command. The engine validates typed descriptors and rebuilds fixed command templates; journal files cannot supply executable command arrays, paths, shell code or arbitrary destinations. Unknown schemas and injected command fields are rejected. POSIX receipts must be private, ordinary files owned by the current OS user, so switching from a user account to root can require manual recovery. A local receipt is an **untrusted claim of origin**, not cryptographic proof that the app created an OS object. Recovery requires an exact current-state match, a fresh preview and explicit human re-authorization. Another same-user process can edit the journal; no claim of authenticated provenance is made. The native privilege helper remains unimplemented. A session constructed without `data_dir` retains the explicit session-only recovery warning.

If a command times out or post-change verification fails, the result has `may_have_applied: true` and the receipt remains available. A successful OS command is only reported as applied after the exact addition is observed; rerun the source probe to assess connectivity. macOS adds a `/32` address alias to avoid creating a broad connected-subnet route. The separately reviewed route to the device requires the explicitly verified device prefix, such as `/24`, when the local address itself is `/32`. Command forms were checked against Apple's official [route manual](https://github.com/apple-oss-distributions/network_cmds/blob/main/route.tproj/route.8), [route implementation](https://github.com/apple-oss-distributions/network_cmds/blob/main/route.tproj/route.c), and [ifconfig manual](https://github.com/apple-oss-distributions/network_cmds/blob/main/ifconfig.tproj/ifconfig.8); this source review does not replace native execution tests.

## RPC contract

`NetworkService.dispatch(method, params)` returns an object; invalid inputs raise `ValueError` for the engine's JSON-RPC error handler. Hostnames, subnets, broadcast/multicast destinations, command fragments and noninteger ports are rejected. Up to eight individually configured sources are accepted; no auto-discovery occurs.

`network.profile` accepts `{ "profile_id": "q22" }` and returns a versioned handoff reference. `network.capabilities` accepts `{}` and reports each platform's inspection/probe/repair capability and current engine-owned safety reasons. A renderer cannot declare itself disarmed or provide a repair backend. A UDP probe defers when ownership is unknown or the passive recorder already owns the requested socket.

```json
{
  "sources": [{
    "id": "ground-radio",
    "kind": "skypulse",
    "host": "10.0.0.2",
    "port": 80,
    "interface": "eth0"
  }],
  "timeout_ms": 1500
}
```

`network.inspect` accepts `sources` and returns `status`, `platform`, `interfaces`, `routes`, `default_routes`, `targets`, `firewall` and `limitations`. It sends no packets to configured devices. Each target has `source_id`, `host`, `state`, `selected_interface`, `local_address`, `local_address_state` (`os_selected`, `unknown` or `inconsistent`), and `issues`. `status: partial` means host visibility is incomplete, not healthy. An unknown source address requires an explicitly selected, existing local address before a UDP probe.

`network.probe` uses the same structure and adds `probes`. Each contains `source_id`, `state`, `checks`, `details`, and `recovery_action`. `kind` is `skypulse`, `rtsp` or `udp`. RTSP supports `path`, default `/`; UDP supports `local_address` and `expected_format: mavlink|rtp|unknown`. Defaults are port 80, 554 and 14551 respectively. The timeout is 100–5000 ms per endpoint, with a 20-second total remote-probe budget after host inspection. Remaining sources return `budget_exhausted`. Read-only Linux/macOS OS commands have two-second timeouts; Windows PowerShell queries allow six seconds for process and module startup; mutations allow eight seconds. HTTP replies are bounded to roughly one MiB and RTSP replies to 64 KiB under an overall response deadline, including headers. Redirects are not followed.

```json
{
  "kind": "host_route",
  "source": {"id": "ground", "kind": "skypulse", "host": "10.0.0.2"},
  "interface": "eth0",
  "local_address": "10.0.0.10"
}
```

`network.plan` accepts the example above. `kind` may also be `address_alias` (requires new `local_address`, optional integer verified device `prefix`, default 24) or `firewall_udp` (UDP source and existing `local_address` required). A macOS `host_route` through a `/32` alias additionally requires the verified device `prefix` from 1–30. It returns `state: ready|blocked|unsupported|not_needed`, `title`, `commands`, `rollback_commands`, `requires_elevation`, and `warnings`, with `plan_id` only required for a ready plan. A blocked partial host inspection may not allocate a plan ID.

`network.apply` and `network.rollback` accept **only** `{"plan_id":"…","confirm":true}`. Results contain `plan_id`, `state`, `message`, and optionally `may_have_applied`. Recovered receipts cannot be applied again. Failed validation, missing permissions, stale state, an expired plan, changed ownership and uncertain execution all have separate states; they are not success.

`network.recovery` accepts `{}` and performs read-only receipt/current-state checks. It returns `{status: "ready"|"unavailable", repairs: [...], warnings: [...]}`. Each repair contains the ordinary preview fields plus `recovered: true`, `ownership: "owned"|"changed"|"absent"|"unknown"`, `state`, `may_have_applied`, and `message`. Here `owned` means exact native-state match to a typed receipt, not independently authenticated app provenance. Rolled-back history remains on disk and is omitted from active results. Absent additions set `may_have_applied: false`; changed or uninspectable additions disable automatic rollback. The UI must present the recovered warnings and exact removal before asking for confirmation.

## Validation and remaining qualification

Run `PYTHONPATH=engine python -m unittest discover -s engine/tests -p 'test_network*.py'`. Tests mock host mutations and remote devices: dual interfaces, wrong routes, unknown firewall, incomplete inspection, exclusive bind conflicts, UDP timeout, CRC validation, HTTP endpoints, SDP-only capability evidence, injection/confirmation rejection, stale/expired plans, exact rollback ownership, uncertain outcomes, and OS command construction. Journal tests cover restart recovery, changed state, interrupted mutation, pre-write failure, atomic replacement failure, preserved history, injected descriptors/argv and symlink rejection. Platform fixtures cover macOS route/alias plans and rollback gates, route columns/abbreviated prefixes and multiple local aliases, Windows Unicode/BOM JSON and separate address/route objects, failed supplemental inspection, localized PowerShell alias encoding and sparse launcher PATHs.

Linux host inspection can be run read-only in the development environment. No privileged repair, real SkyPulse/SIYI stream, or Windows/macOS machine has been hardware-tested here. Native multi-NIC and firewall acceptance tests, actual device return paths, full stream decoding and native crash/power-loss recovery qualification remain release requirements.
