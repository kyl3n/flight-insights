# Network diagnostics milestone

The Network screen inspects this computer and explicitly configured IPv4 sources. It does not scan the LAN, send MAVLink commands, change aircraft settings, or start a second video player. Inspection works through `psutil` plus the OS routing tools: `ip` on Linux, PowerShell NetTCPIP on Windows, and `netstat`/`route` on macOS. Known system-tool locations are used when the desktop launcher's PATH is sparse. Missing tools produce a partial result and block repair planning.

Windows PowerShell output is explicitly UTF-8, with BOM-safe JSON parsing so localized interface aliases survive intact. Firewall commands encode the alias as UTF-8 data inside a fixed decoding expression, avoiding PowerShell interpretation of ASCII or smart quotes in names. Supplemental interface-query failure preserves the addresses observed through `psutil` for explicit probes, while the partial result blocks repair planning. On macOS, abbreviated route prefixes and changing `netstat` column layouts are normalized. Since `route get` omits the chosen source address, an ephemeral UDP socket uses `connect` and `getsockname` for local kernel source selection; it never sends a datagram or binds to a GCS port. Failure is reported as an unknown source address rather than guessing among aliases. The kernel-selected address is checked against the reported interface to detect inconsistent observations.

For an Internet adapter at `192.168.0.20/24` with gateway `192.168.0.1`, and a dedicated digiQ adapter at `10.0.0.10/24`, configure the digiQ host as `10.0.0.2` and select that dedicated adapter. The app queries the OS route to that exact host and reports the selected interface and source address. A valid route alone does not prove that the radio sends packets back to this computer. Configure the sender's destination as the computer's device-side address and a dedicated receive port.

If the dedicated adapter has no address in the radio subnet, a host route by itself is insufficient. A reviewed Linux address alias can add an operator-specified, verified unused address. It uses `noprefixroute`, so adding a host route is a separate reviewed step. The app never chooses an unverified static IP automatically. A successful host route only selects a path for this computer; it does not enable forwarding/NAT between networks or configure the radio's return route.

## What the checks establish

| Check | Evidence | Still unverified |
| --- | --- | --- |
| Host inspection | Adapter state, IPv4 address/subnet, default routes, OS route to each configured target | Device power, physical path, sender configuration, effective firewall policy |
| SkyPulse | Read-only GET of `/scatter_data.json` and `/events.json`; status, byte count, valid complete JSON | Vendor field schema, units, sample times, RF link health; this is a probe, not the rolling-history recorder |
| RTSP | DESCRIBE on the exact configured URL; basic SDP structure and codec names actually advertised | Media reception/playback, decoder availability, resolution, dropped frames, glass-to-glass latency |
| UDP | Exclusive bind on one existing local IPv4 address and configured port; packets from the exact configured sender | Playback or full telemetry continuity; other senders are counted separately |
| MAVLink UDP | A common HEARTBEAT packet with valid MAVLink v1/v2 framing and CRC extra 50 | Signing authentication, other message types, stream completeness, physical RF loss |

No incoming UDP during the timeout means **no packets observed**. It does not prove a firewall block. JSON reachability does not establish radio health; an SDP reply does not establish playable video. Effective firewall policy remains `unknown`, even after creating a scoped exception, because other rules and security software can override it. A native video receive/decode adapter and full MAVLink parser integration are later milestones.

Use a dedicated GCS/router forwarding destination such as port `14551`; do not take over the GCS's active telemetry port. The UDP socket never enables address/port reuse and uses exclusive binding on Windows. An occupied port returns `bind_failed` with instructions to configure forwarding while leaving the GCS running.

## Reviewed repairs and ownership

Generating a plan only displays exact additions and rollback commands. Applying it requires the server-held plan ID and a separate explicit confirmation. Client-supplied commands are rejected. Plans expire after ten minutes before application; interfaces/routes and firewall scope are rechecked immediately before mutation. The native app must already have administrator rights for the apply step; otherwise it returns `permission_required` without running any command. No silent elevation or password prompt is implemented.

| Repair | Linux | Windows | macOS |
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
