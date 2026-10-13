# Acceptance status

This is a managed-analysis preview against the supplied Revision 2 handoff. Update this document with executed checks; a test fixture or CI configuration is not a hardware-validation result. Revision 2 Stage 1 is not complete.

Windows x64 and Linux x64 are the current required release targets. macOS is deferred by the user's latest instruction; optional Mac build configurations remain available. See the [platform support matrix](platform-support.md).

| Handoff stage | State | Remaining exit work |
| --- | --- | --- |
| 0 — Foundation | In progress | Transactional migration with a verified SQLite backup and explicit original-hash verification implemented. Clean-machine upgrade qualification and distribution review of per-target dependency inventories remain; automated checks are recorded in [validation.md](validation.md) |
| 1 — Collection/review | Partial slice | Passive recorder, immutable manifests, explicitly approved pre/postflight parameter orchestration, selected log transfer and bounded content alignment available. Qualified GCS adapters, multi-chunk automatic attachment, complete effective parameter history and real Q22 qualification remain |
| 2 — Evidence workspace | Partial comparison slice | Identity/clock/interval alignment and reviewed verified attachments implemented for supported evidence. Broader segmentation/charts and issue lifecycle remain |
| 3 — Control/filter/performance | Partial local insight slice | Vibration distributions/exposure, power/PWM context, RATE/PID tracking and qualified step response, complete batch-IMU spectra and validated static filter equations available. Dynamic firmware/filter parity, aircraft-specific numerical tuning and real-data engineering review remain |
| 4 — Native/live health | Partial host-network slice | Versioned Q22 reference, cross-platform diagnosis, exact repair previews and durable recovery available. Separate administrator-enrolled Windows/Linux helpers support explicitly approved experimental bench repairs; default installations remain diagnosis-only. Hardware qualification, USB/MAVFTP and video decoding remain |
| 5 — Release qualification | Not passed | Full mandatory acceptance plan, publisher-signed installers, accessibility and reference-laptop benchmarks. The cloud synthetic 2 GiB import result and its event-count limitation are recorded in [validation.md](validation.md) |

## Evidence still needed

- One matched Q22 BIN, TLOG and both-radio raw-response bundle, with source identities and exact firmware.
- Accepted **Q22 original - pre-FACt** and **Q22 with FACt tune** parameter files; retain legacy Brian aliases and original bytes, and never infer a missing baseline.
- Verified Cube/H7 profile and ESC mappings, battery calibration and flight conditions.
- Exact Windows/Linux and Mission Planner/QGroundControl versions, actual destinations/ports, A8 Mini URL/protocol and physical dual-NIC topology for bench tests. User-declared devices are DigiQ SkyPulse and SIYI A8 Mini; exact flight-controller firmware remains unknown.

Independent development can continue using explicitly synthetic fixtures. Unsupported evidence must remain unavailable rather than green/healthy.
