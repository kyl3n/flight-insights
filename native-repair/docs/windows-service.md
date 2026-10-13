# Windows private bench service

The app defaults to diagnosis only. The experimental Windows helper supports a
single IPv4 host route or an exact inbound UDP rule scoped to interface, local
address, remote address and port. Windows address aliases are withheld because
adding an address can change DHCP behavior. Domain/MDM ownership, unknown policy,
disabled firewall profiles, or enabled blocking rules withhold writes.

This implementation is an administrator-enrolled private bench adapter. Image
hash enrollment is not public code signing, hardware qualification, or proof that
unsigned MAVLink cannot be replayed. Do not describe it as production qualified.

## Explicit installation

1. Build the native helper on Windows: `cargo build --locked --release
   --manifest-path native-repair/Cargo.toml` (run this as one line).
2. Install the **whole desktop app and frozen engine directory**, including its
   libraries, under an administrator-controlled `Program Files` directory.
   A per-user AppData install is unsuitable for this privilege boundary. Find the
   actual `q22-engine.exe` used by the desktop's sidecar resolver and enroll that
   path; copying an isolated EXE will not install its runtime or change the app's
   resolver. Every containing directory and the image must be protected against
   ordinary-user replacement. If the current desktop installer cannot select an
   all-users location, make an explicit administrator-managed whole-app install
   before enrollment; do not weaken ACL checks.
3. In the **ordinary account that will run Q22**, obtain its SID with
   `[Security.Principal.WindowsIdentity]::GetCurrent().User.Value`. Pass that exact
   value as `-ClientSid` when running `scripts/install-windows.ps1` in an elevated
   Windows PowerShell. The installer never infers the user from the elevated
   administrator account. Its comment contains a complete argument example.
4. Declare the adapter alias, exact device/probe addresses and TCP verification
   ports. Configure a dedicated forwarded UDP endpoint for the native observer,
   separate from the recorder/GCS/video sockets. Forward `HEARTBEAT`,
   `AUTOPILOT_VERSION` and `SYSTEM_TIME`; missing UID/clock/heartbeat evidence
   keeps repair blocked. Optionally enroll a signing key and require signatures.
   Never put key material into app RPC, logs or a command argument.
5. Review the installer's confirmation. It creates the LocalSystem service,
   fixed protected enrollment/journal and exact helper/client image hashes.
   It changes no routes or firewall rules. Existing destination directories or
   enrollment are rejected to preserve evidence and exclude hostile links.
6. Run Q22 normally, explicitly opt into the available experimental helper for
   this engine session, then inspect and approve each generated repair plan.
   Opt-in does not enroll the service or provide its safety state.

The fixed endpoint is `\\.\pipe\Q22NetworkRepair-v1`. Native IPC verifies the
client SID, process image/path/hash and protected enrollment. The engine verifies
the pipe server against the running LocalSystem SCM service and protected helper
image/hash. RPC cannot select a command, executable, journal, pipe or safety state.
Lease renewal uses independent connections while native operations are serialized.

## Changes and recovery

The service reads Windows adapter/address/route/DNS/firewall/policy state through
fixed System32 PowerShell cmdlets. Firewall filters are read in seven bounded bulk
queries. Independent filter records remain in the comparison context, including
filters for disabled or unselected rules. Runtime provider probes diagnose an
unsupported identity mapping; they do not prove every foreign association.
Every helper-prefixed rule requires actual native association and scope checks
before its filters can be treated as part of the helper-owned addition. Missing,
duplicate, changed, or contradictory records reject the snapshot. More than 16
helper-prefixed rules withhold repair rather than return partial evidence.

The service checks canonical state immediately before a
compiled narrow write, independently rechecks fresh disarmed/recording-inactive
state, and records durable intent first. A successful command plus the exact
expected native object is required for ownership. This is guarded compare/check,
not an atomic Windows transaction against every external administrator. Concurrent
or changed state causes refusal/uncertain recovery rather than broad restoration.

Only the matching helper-owned tuple/rule can be rolled back. Other changes,
including changes to a similarly named rule, prevent automatic deletion. TCP
verification checks the enrolled device service and Internet probe; it does not
prove UDP reception, camera decoding, telemetry quality or restored onboard logging.

The service supervises its journal independently of the UI. Rollback remains
conditional on fresh native disarmed evidence, recorder inactivity and exact
ownership. Lost/expired engine leases or an orphan recorder can leave an unresolved
receipt that needs a fresh safe engine session. Engine death alone is **not** proof
that recording stopped. Do not promise unconditional undo after app loss.

For removal, run `scripts/install-windows.ps1 -Uninstall` as administrator. It
stops request admission, waits for in-flight service operations, then rereads the
journal. Unresolved/unknown receipts restart the service and block removal. Review
recovery in the enrolled app first. Successful uninstall retains binary, enrollment
and journal; explicitly archive/migrate them before reenrollment or upgrades.

## Validation boundary

Cross-compilation and synthetic snapshot tests cover canonical ordering, policy
abstention, exact scope/ownership, opaque foreign-filter swaps and missing or
contradictory associations. The snapshot retains its 15-second execution and
2 MiB output bounds; native mutation timeouts have not increased. Windows CI must execute
native ACL/pipe/cmdlet checks. Real dual-network repair, interruption, privilege
policy, DHCP continuity, video/telemetry verification and power-loss recovery still
require the planned Windows bench; no cloud host network settings are changed by
these tests.
