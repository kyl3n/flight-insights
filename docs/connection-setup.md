# Setup

The main workspace has two destinations: **Flights** and **Setup**. Configure a
connection once in Setup, then start collection and review flight logs from
Flights. Saved-log analysis needs no live connection or historical baseline.

## Choose how data reaches Flight Lab

- **Live aircraft connection:** use a return-capable UDP endpoint from your
  radio or MAVLink router, alongside Mission Planner, QGroundControl or another
  ground station. One Flight Lab port handles recording and supported parameter
  requests.
- **Local telemetry feed:** receive the stream supplied by a GCS or router on
  this computer. Loopback addresses are normally `127.0.0.1`. A one-way feed
  supports recording; parameter requests require a working return path.
- **Saved flight logs:** skip connection settings and import original BIN, LOG
  or TLOG files from Flights. Existing connection profiles are retained.

Serial connections remain in the existing field equipment or GCS. Flight Lab
does not open COM ports. Previous serial setup preferences migrate to the local
telemetry workflow without adding another serial connection.

The app needs telemetry delivered to its own endpoint. It cannot reliably share
the UDP socket already owned by a GCS. A radio/router can supply both applications;
local GCS forwarding is another option when it provides the required direction.
This does not require another aircraft telemetry radio link. QGroundControl's
one-way forwarding cannot return parameter requests. Check Mission Planner or
another router against its actual version and configuration; a received heartbeat
does not prove a working return path.

## Enter the connection

Set an aircraft profile name, this computer's receiving IPv4, the actual
telemetry sender's IPv4 and the app's UDP port. **Find computer addresses** reads
local adapter information; it does not discover or configure the radio. A radio's
management address can differ from its telemetry sender address. Remote devices
cannot use `127.0.0.1` to reach this computer.

The suggested app port is `14551`. Use an unused port from 1024–65535 and configure
the sender to deliver telemetry to the displayed receiving address and port.
Keep the GCS's existing endpoint available to the GCS. The suggested system and
component IDs are `1`; these are defaults, not detected identities. Change them
under **Aircraft IDs and advanced settings** when needed.

With **Capture parameters automatically before and after flights** enabled,
also enter the return-capable **Aircraft link UDP port**. No manual Controller
UID is required: the app requests `AUTOPILOT_VERSION` and discovers the connected
controller's nonzero UID for each new collection. The same app socket receives
telemetry and sends bounded read-only requests to that peer. Parameter capture
waits for consistent identity, boot-clock evidence and fresh disarmed telemetry.
If the aircraft does not report a supported UID, recording and log analysis
remain available, while automatic parameter capture reports the missing identity.
Disable automatic capture for a one-way connection; full exports can be imported.

Connect one aircraft at a time and stop collection before switching aircraft.
The next collection discovers its identity again without changing the saved
connection. A different UID during collection stops readback; it never silently
switches the current flight to another aircraft. **Aircraft IDs and advanced
settings → Lock to controller UID (optional)** restricts the connection to one
controller. Existing UID locks remain in effect until explicitly cleared.
Reported UIDs are identity evidence, not authentication or proof of uniqueness.

Under **Aircraft IDs and advanced settings**, **App MAVLink system ID** and
**App MAVLink component ID** identify Flight Lab's outgoing requests, separately
from the aircraft. Defaults are system `254` and component `191`. Leave these
unchanged unless another client uses that identity. The app's system ID must
differ from the aircraft's. If incoming traffic uses the app identity, readback
stops with an explanation; resolve the duplicate identity or router echo before
starting a new collection. The app does not take over a GCS identity.

Old receive-only setups remain receive-only when upgraded. Saving or reopening
setup never starts traffic. **Start collecting** is the action that starts the
reviewed connection and enables its configured automatic parameter policy.

Radio and camera checks are under **Optional device checks**. Supply only the
devices you use and their real management/RTSP addresses. SkyPulse HTTP checks
require supported endpoints; RTSP checks do not decode video or authenticate to
the camera. Do not put credentials into the RTSP path.

## Review and save

**Check connections** briefly listens on the selected app port and checks enabled
devices. It sends no MAVLink request. Probing is unavailable during collection so
it cannot compete for the running recorder's socket.

| Result | Next step |
| --- | --- |
| No packets | Check sender destination, actual sender address, adapter and inbound UDP firewall rule |
| Port unavailable | Choose an unused app port or inspect the running collection; keep the GCS connection intact |
| Packets without valid telemetry | Confirm this output is MAVLink telemetry rather than video or another protocol |
| MAVLink heartbeat received | Check aircraft IDs and, for automatic readback, the return-capable path and UID |
| Device unavailable or inspection partial | Follow the specific recovery detail; no successful result is inferred |

**Save setup** validates the profile and stores it on this computer. A successful
probe is optional so equipment can be configured before connection. A saved badge
does not claim connectivity. Edits clear stale probe results; saving during an
active collection affects the next collection only.

Choose **Open flights**, then **Start collecting** before arming. Collection
shows parameter counts, freshness and missing evidence. With the automatic policy
enabled, stale completed preflight captures are refreshed while disarmed within
a bounded request budget. Readback is non-atomic and does not prove every stored
setting was effective throughout a flight. Missing or failed evidence remains
visible; the app does not control aircraft arming or write aircraft settings.
See [automatic parameter capture](automatic-acquisition.md).

Available host OS and software context is recorded automatically at collection
start and each observed arm transition. Optional version declarations and detailed
collection tools are under **Flights → Collection details**. Old logs never
inherit the current computer's environment. See the [flight workspace](flight-workspace.md)
for analysis and comparison behavior.

## Storage and qualification

Setup uses the existing `q22.collection-profile.v1`, `q22.network-sources.v1` and
`q22.connection-setup.v1` settings. Unrelated sources and archived evidence are
preserved. Storage failures are displayed instead of a saved confirmation.
Settings are local to the desktop profile, not cloud synchronization.

Open **Setup → Connection troubleshooting** for network inspection and scoped
repair guidance. Setup does not configure radio routing, change host routes or
install firewall rules automatically. Synthetic tests do not qualify an actual
digiQ/GCS/aircraft route; physical and installed-platform qualification remains
separate from the implemented workflow.
