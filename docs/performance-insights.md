# Evidence-based performance insights

Open **Insights**, choose an imported log, its recorded source and clock segment, and an explicit interval. Add the actual operating conditions. The resulting report is saved locally with the original source hash, interval, algorithm version, missing-evidence reasons and pinned guidance. Synthetic evidence remains marked synthetic.

## Measurements and interpretation

- VIBE analysis reports each supported IMU's axes, observed above-threshold samples, timestamped excursions and clipping-counter increments. It distinguishes legacy and current schemas, missing units, gaps and counter resets. The 30/60 m/s² screening levels are guidance, not aircraft-specific airworthiness limits. Clipping, mounting, propeller balance and structural resonance need investigation before changing filters.
- Supported battery and attitude fields provide scoped electrical power, energy and roll-tracking error. Load-step voltage drops are observations; they do not identify an unhealthy cell or estimate battery internal resistance. A pack-average cell voltage is not an individual-cell measurement.
- Motor command margin is available only for explicitly mapped PWM channels with declared endpoints. PWM command is not measured thrust, and the calculation is unavailable for an unknown actuator protocol or calibration.
- A bounded exploratory Welch spectrum uses actual supported GYR samples, separate from VIBE. It requires sample-rate and anti-aliasing evidence and checks observed timing. It reports conditional peaks, not a prescribed notch frequency. It does not implement ArduPilot Filter Review, batch-IMU decoding or full PID/filter simulation parity.

Timestamp regressions, corruption boundaries, missing fields and partial parameter history constrain results. A metric may be unavailable even when a log imports successfully. Logged parameter observations can be compared with a selected export for review; an export does not establish settings active during a historical flight. No result establishes that a parameter change caused a performance change.

## Heavy payloads

`aircraft_mass_kg` is the **all-up takeoff mass including payload**. `payload_mass_kg` is a subset, not a second mass added to it. Record rotor count, propeller diameter, rotor layout, battery configuration, flight phase and exact firmware. Disk loading is calculated only for declared non-overlapping rotor geometry; coaxial rotors need a different model. These inputs are operator declarations.

Mass and propeller size support disk loading, payload fraction and electrical power per kilogram. They cannot determine optimal gains, thrust margin, safe gross mass, endurance or motor temperature. Those require validated propulsion curves, actual actuator/ESC mappings, calibrated sensors, aircraft limits and representative flight evidence. Unknown context stays unknown. The app does not assign a generic heavy-lift tune.

For useful tuning recommendations, first capture matched parameter snapshots and flight logs; record payload, battery, weather and flight phase; investigate mechanical vibration and clipping; then perform controlled, repeatable comparisons within the aircraft's operating limits. Review any proposed setting against the exact firmware and trusted ArduPilot tools. This preview never writes aircraft parameters.

## Current guidance and optional AI

Numerical analysis stays local and deterministic. Recommendations use a bundled, versioned registry of reviewed ArduPilot sources with exact source commits, hashes, line references and a review date. Firmware compatibility and source age are separate: the newest documentation can describe a parameter that an older aircraft firmware does not have. See [guidance provenance](insight-guidance.md).

An AI API can explain measurements, organize possible causes and suggest further evidence to collect. It cannot make a weak measurement reliable or guarantee current advice. A future integration should receive a minimized evidence packet and firmware-matched retrieved references, return structured findings with citations and uncertainty, and pass deterministic compatibility checks. Numerical tuning or automatic application needs separate engineering validation. No AI provider or automatic guidance updater is configured in this preview.

**Preview AI review packet** shows the exact local packet before export. **Export review packet** writes it to a chosen folder; it does not send it anywhere. The projection retains synthetic/quality labels, relative evidence times and selected technical context while excluding original logs, locations, local paths, device identifiers and arbitrary operator text. Exported measurements and hashes may still be sensitive; inspect the preview before choosing to share it. Export never modifies the saved report or original evidence.

## Validation boundary

Synthetic tests exercise numerical integration, schema gates, clipping resets, gaps, explicit PWM calibration, spectrum parity against small DFT/Parseval/sine oracles, persistence and export projection. They do not qualify Q22 vibration diagnosis or flight tuning. Matched real Q22 logs, verified hardware mappings, exact firmware and installed-app/device checks remain required. See [acceptance status](acceptance-status.md) and [validation](validation.md).
