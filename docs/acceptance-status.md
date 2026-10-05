# Acceptance status

This is a foundation preview with two requested feature slices. Update this document with executed checks; a test fixture or CI configuration is not a hardware-validation result.

Windows x64, Linux x64, macOS Apple Silicon and macOS Intel are all required release targets. Core workflow qualification must pass on each; the original handoff's Windows-first sequencing does not override this user requirement. See the [platform support matrix](platform-support.md).

| Handoff stage | State | Remaining exit work |
| --- | --- | --- |
| 0 — Foundation | In progress | Target-OS clean install, desktop/sidecar qualification, migration recovery and release license inventory |
| 1 — Collection/review | Partial slice | Scoped BIN/LOG/TLOG decoding, boot/source separation and parameter observations implemented; live RF recorder, broader field normalization, real Q22 fixtures and health rules remain |
| 2 — Evidence workspace | Partial comparison slice | Cross-source alignment, segmentation, comprehensive charts, issue lifecycle |
| 3 — Control/filter/performance | Not implemented | DSP/PID parity against pinned trusted tools, scoped engineering validation |
| 4 — Native/live health | Partial host-network slice | Durable reviewed repairs and selected macOS routes/aliases implemented; hardware bench validation, USB/MAVFTP, recording isolation and live alerts remain |
| 5 — Release qualification | Not passed | Full mandatory acceptance plan, signed installers, accessibility and large-log benchmarks |

## Evidence still needed

- One matched Q22 BIN, TLOG and both-radio raw-response bundle, with source identities and exact firmware.
- Accepted original and Brian parameter files; keep these separate and never infer a missing baseline.
- Verified Cube/H7 profile and ESC mappings, battery calibration and flight conditions.
- Ground station operating system/version, actual stream destinations/ports, camera URL/protocol and physical dual-NIC topology for device bench tests.

Independent development can continue using explicitly synthetic fixtures. Unsupported evidence must remain unavailable rather than green/healthy.
