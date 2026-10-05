These are **synthetic fixtures in real file formats**, not Q22 flight evidence.

`generate_synthetic.py` deterministically creates DataFlash BIN and text LOG
records with in-file FMT/FMTU/UNIT/MULT definitions, timed parameter changes,
one battery monitor, paired ATT demand/actual fields, a boot-time reset, and
battery sentinel values. The BIN is independently assembled from wire structs;
the text LOG applies intrinsic field scaling as DataFlash text exports do.

The timestamp-prefixed TLOG contains CRC-valid MAVLink 1 and 2 packets, two
system IDs, parameter observations, BATTERY_STATUS and ATTITUDE, a controller
boot-time reset, and unknown battery sentinels. Packets are generated with
the pinned pymavlink 2.4.49 ArduPilotMega v20 dialect. The TLOG does not contain
the entire parameter list; it must never become a claimed complete snapshot.

Regenerate from the repository root with:
` .venv/bin/python fixtures/logs/generate_synthetic.py `

These fixtures validate wire decoding and arithmetic only. They do not validate
an aircraft firmware build, calibration, hardware performance or causal tuning
effects. CRC is checked, while MAVLink cryptographic signatures are not verified.
