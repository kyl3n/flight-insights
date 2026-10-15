# Guided connection setup

Open **Connection setup** from the sidebar or **Set up connections** on the overview.
The guide saves settings on this computer and shares them with **Prepare & watch**
and **Network checks**. It does not start recording, configure the radio, change
the computer's network, or request aircraft parameters.

The primary field workflow uses UDP from the existing serial/radio/GCS
infrastructure. Dedicated COM-port setup and direct serial transport in Flight
Lab are deferred. A fresh install requires no pre-FACt/FACt baseline or previous
flight; the next step is to establish the aircraft's current settings before its
first recorded flight. See [current product direction](current-product-direction.md).

## Choose your workflow

- **Ground station on this computer:** connect Mission Planner or QGroundControl
  to the aircraft first, keep its native logging enabled, and configure a separate
  MAVLink UDP forwarding output. For local loopback forwarding, Flight Lab's
  receiving and sender addresses are both `127.0.0.1`.
- **Forwarded feed from another device:** use this computer's actual receiving
  adapter IPv4 and the actual packet sender's IPv4. The sender may be a radio,
  router or another computer. The radio's management address is not necessarily
  the telemetry sender address. A remote device cannot use `127.0.0.1` to reach
  this computer.
- **Analyze saved files only:** skip all connection settings and open Evidence to
  import BIN, LOG, TLOG or parameter files. Existing connection profiles are kept.

If the aircraft or radio connects by serial, retain that connection in the
existing field equipment or GCS and use the appropriate UDP workflow above.
Flight Lab does not enumerate or open serial ports. Legacy saved COM/baud notes
are not applied transport settings. The separate COM workflow and inputs have
been removed; legacy serial selections migrate to the GCS/UDP workflow without
asking the user to configure another serial connection.

Ground-station forwarding controls vary by version. The guide does not claim a
qualified automatic Mission Planner/QGC adapter. QGroundControl forwarding is
one-way and is suitable for the passive observer, not parameter/log requests.

## Enter only the devices you use

Radio and camera checks are optional. Enter the actual digiQ/SkyPulse management
IPv4 and HTTP port (usually 80). For an RTSP camera, enter its actual IPv4, port
(usually 554) and path. Do not enter credentials in the path. SkyPulse probes
require the supported management endpoints; RTSP checks do not decode video or
authenticate to the camera.

The historical Q22 reference uses a radio at `10.0.0.2` and drone adapter at
`10.0.0.5/24`. These remain examples, not detected values. The guide never applies
them automatically. Keep the existing Internet gateway and DNS. Use **Network
checks** for adapter, subnet, route and scoped firewall guidance.

## Prepare the telemetry destination

**Find computer addresses** reads local adapter information without scanning or
contacting devices. Choose an address on the adapter connected to your sender.
An adapter being up does not establish device reachability.

Set an aircraft profile name, receiving IPv4, actual sender IPv4, dedicated
observer UDP port, and the aircraft's MAVLink system/component IDs. Check the IDs
in the ground station; the suggested `1` values are not detected identities. A
controller UID can remain unknown for passive recording.

The guide suggests port `14551`, which must match the forwarding destination.
Keep the controlling GCS's port (normally `14550`) and video port for their
existing applications. A different free port from 1024–65535 is supported.
The review screen shows the exact `receiving IPv4:UDP port` to enter in the
forwarding application.

## Check, save, then start explicitly

**Check connections** requires confirmation that the port is reserved for the
separate feed. It briefly listens for arriving telemetry and contacts only the
configured radio/camera. It does not send MAVLink traffic. While an app observer
is already running, use its collection status instead; this guide's probes are
disabled so they do not compete for its receive socket.

| Result | What to do next |
| --- | --- |
| Connection not tested | Save for later, or configure the forwarding destination and run the check |
| No packets | Check the sender's destination IPv4/port, actual sender address, receiving adapter and inbound UDP firewall rule |
| Port unavailable or recorder owns the socket | Use the running observer's status or choose a separate unused port; do not stop the controlling GCS to take its socket |
| Packets received without valid telemetry | Confirm the output is MAVLink telemetry, not video or another protocol |
| MAVLink heartbeat received | Confirm the selected aircraft source IDs in Prepare & watch; a heartbeat does not authenticate aircraft identity or establish readiness to fly |
| Device response unavailable | Check the management/RTSP address, port and device capability; inspect the specific recovery action |
| Computer inspection unavailable or partial | Check the adapter and OS diagnostic availability; no healthy status is implied |

**Save setup** validates the observer profile using the existing engine and saves
the settings locally. Saving is allowed without a successful device check so
equipment can be configured before it is connected. A saved badge is never a
connectivity claim. Edits clear previous probe results.

Then open **Prepare & watch**, review the saved profile and explicitly approve
and start the observer using its existing controls. Saving never starts or stops
an observer, changes an active capture, or enables acquisition. If the guide
changes a pending setup, old observer previews and approvals are cleared.
Unrelated configured sources and archived evidence are retained.

Parameter readbacks and onboard-log acquisition remain a separate reviewed
bidirectional UDP workflow with actual remote address/port, controller UID and
fresh disarmed identity/clock gates. See [read-only acquisition](read-only-acquisition.md).
Video diagnostics remain in [Network checks](network-diagnostics.md).

## Establish current settings before flight

The first useful configuration reference is the aircraft's current preflight
settings. Do this before each flight so later comparisons can relate parameter
changes to performance changes without relying on an old named tune.

In **Prepare & watch**, use the separate parameter-readback workflow or save and
approve a bounded [automatic acquisition policy](automatic-acquisition.md) for
that capture. It requires the actual bidirectional endpoint and fresh disarmed
aircraft identity. The passive observer's address and port do not automatically
provide a return path. One-way GCS forwarding supports recording only; use a
separate bidirectional route or import a current GCS parameter export when that
is the available path.

Review readback completeness, conflicts, timestamps and freshness. Parameter
changes, restarts, gaps or expiry can invalidate a previously captured reference.
The collection view shows decoded/expected parameter counts and the current
readback state. An arm cycle revokes earlier readback eligibility, including a
readback that crosses that cycle; it is not reused as a fresh reference for the
next flight.
A full enumeration is non-atomic and does not prove every setting was effective
throughout the flight. Missing evidence remains visible and recording can still
continue. Preserve its absence rather than filling it with a later readback.
Flight Lab reads aircraft settings; it does not change them.

## Record the flight computer and software

In **Prepare & watch → Flight computer & software**, use the optional **Inspect
flight computer** check to review available host OS/kernel/distribution,
architecture, Flight Lab/engine versions and recognized running GCS/router/video
applications. The check describes this computer now. Starting collection and
observing each armed interval automatically retain their own immutable
environment observations; inspection is not required to record them.

Enter the ground-station name/version and other relevant applications when known.
These entries are optional and explicitly operator-declared. Running-process
names and MAVLink heartbeats do not establish exact GCS versions or prove which
application controlled the aircraft. Leave unknown versions blank.

Saved flight bundles display the recorded context and its provenance. Later
setup edits or software updates do not replace it, and old flights without
environment evidence remain unknown. Saved insight reports use that context only
when linked to a verified matching managed-flight interval. See
[current product direction](current-product-direction.md) for the broader
environment and comparison requirements.

## Local settings and validation scope

The guide reuses `q22.collection-profile.v1` and `q22.network-sources.v1`, plus
`q22.connection-setup.v1` for setup preferences and optional ground-station notes.
Writes preserve unrelated sources and attempt to restore prior values if storage
fails. A storage failure is shown instead of a saved confirmation. These settings
are per desktop WebView profile, not cloud synchronization. Captured environment
observations are separately preserved with flight evidence; setup preferences
alone are not historical evidence.

Browser integration tests exercise the real engine profile boundary and use
explicit test responses for selected OS/device outcomes. Physical radio, camera,
field/GCS coexistence, and installed Windows/Linux behavior need bench validation;
the guide does not expand the existing release qualification claims.
