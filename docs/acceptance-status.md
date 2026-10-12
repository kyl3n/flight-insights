# Acceptance status

This is a passive-collection preview against the supplied Revision 2 handoff. Update this document with executed checks; a test fixture or CI configuration is not a hardware-validation result. Revision 2 Stage 1 is not complete.

Windows x64, Linux x64, macOS Apple Silicon and macOS Intel are all required release targets. Core workflow qualification must pass on each; the original handoff's Windows-first sequencing does not override this user requirement. See the [platform support matrix](platform-support.md).

| Handoff stage | State | Remaining exit work |
| --- | --- | --- |
| 0 — Foundation | In progress | Clean installed-GUI qualification, migration recovery and review of per-target dependency inventories; automated native builds/runtime checks are recorded in [validation.md](validation.md) |
| 1 — Collection/review | Partial slice | Separate passive UDP recorder, managed capture/armed-interval records, immutable manifest revisions and provisional associations added. Full pre/post parameter readback, qualified bidirectional GCS adapter, automatic matched BIN retrieval, rolling radio history and real Q22 qualification remain |
| 2 — Evidence workspace | Partial comparison slice | Cross-source alignment, segmentation, comprehensive charts, issue lifecycle |
| 3 — Control/filter/performance | Not implemented | DSP/PID parity against pinned trusted tools, scoped engineering validation |
| 4 — Native/live health | Partial host-network slice | Versioned Q22 reference, cross-platform diagnosis, exact repair previews and durable recovery available; production execution disabled pending qualified native helper. Hardware bench validation, USB/MAVFTP and video decoding remain |
| 5 — Release qualification | Not passed | Full mandatory acceptance plan, publisher-signed installers, accessibility and reference-laptop 2 GiB benchmarks |

## Evidence still needed

- One matched Q22 BIN, TLOG and both-radio raw-response bundle, with source identities and exact firmware.
- Accepted **Q22 original - pre-FACt** and **Q22 with FACt tune** parameter files; retain legacy Brian aliases and original bytes, and never infer a missing baseline.
- Verified Cube/H7 profile and ESC mappings, battery calibration and flight conditions.
- Ground station operating system/version, actual stream destinations/ports, camera URL/protocol and physical dual-NIC topology for device bench tests.

Independent development can continue using explicitly synthetic fixtures. Unsupported evidence must remain unavailable rather than green/healthy.
