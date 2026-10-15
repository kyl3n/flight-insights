"""Evidence-scoped performance hypotheses, with reviewed offline source pins.

This module reads a diagnostic report only. It does not fetch advice, control an
aircraft, choose numerical tuning values, or turn association into causation.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from . import guidance


VERSION = "q22-performance-advice/2026-10-15.2"
RETRIEVED_AT = "2026-10-15"
MAX_RECOMMENDATIONS = 8
MAX_OBSERVATIONS = 24
MAX_PARAMETER_VALUE_CHARS = 2048  # Includes exact canonical REAL64 subnormals.
FIRMWARE_SCOPE = (
    "General mechanisms; parameter metadata reviewed against Copter-4.7.1 only. "
    "The installed build, board, ESC firmware and enabled features remain unverified."
)

# Retrieved and SHA-256 checked from the same immutable wiki revision as the
# existing guidance registry. The date records our review, not a live update.
_SOURCE_DATA = {
    "esc_telemetry": ("common/source/docs/common-esc-telemetry.rst",
        "4a1f39c3eff2eaa8d36571aedfe0fca164594425bebcbf6aeb49707dd26e33d2"),
    "esc_notch": ("common/source/docs/common-esc-telem-based-notch.rst",
        "c1d5d5a0c0c4576712990e8100dbe7702e8ca161ec2438ab8a6263c2655fc1f8"),
    "throttle_notch": ("common/source/docs/common-throttle-based-notch.rst",
        "b6d03b846376b6f7bef720245a5c7d3bd77c960d8bf874a08a01fc33cab473ae"),
    "sampling": ("common/source/docs/common-imu-batchsampling.rst",
        "708f6aa2f005101297c0a26af2125259a8ef6b3bd98ebad67e0010ce084d1789"),
    "fft_tracking": ("common/source/docs/common-imu-fft.rst",
        "0f609fcc68e524511d0ac5eda1b0adda2b1d36ec3e551d82e74582bc6a23fea1"),
    "thrust": ("copter/source/docs/motor-thrust-scaling.rst",
        "5d264c96121f9867701fb07320688ff756fafc96078f74b2733bb0513b0e960b"),
    "power": ("copter/source/docs/current-limiting-and-voltage-scaling.rst",
        "e7cb91e46b94935b19d39e3d7f40873b5c837cf35fda4ed46eb37753c6e3dd26"),
    "mechanical": ("common/source/docs/common-vibration-damping.rst",
        "abb20121449872b4a43e6da45d9f55850fb5e59c0c4f9f821543d9dc7e122d47"),
}
_FIRMWARE_SOURCES = {
    "pid_source": ("libraries/AC_PID/AC_PID.cpp",
        "d7e0fb25e50b634a89ddef7eed803471509ff1aa9d5bf5067164d37499d07776"),
    "esc_source": ("libraries/AP_BLHeli/AP_BLHeli.cpp",
        "8d92834b6fae5dfd658c00fbba3965d3e303a6585a1192f66f9903d8e757c32b"),
}


def _references(*identifiers: str) -> list[dict]:
    references = []
    for identifier in identifiers:
        if identifier in _SOURCE_DATA or identifier in _FIRMWARE_SOURCES:
            firmware = identifier in _FIRMWARE_SOURCES
            path, digest = (_FIRMWARE_SOURCES if firmware else _SOURCE_DATA)[identifier]
            repo = "ardupilot" if firmware else "ardupilot_wiki"
            revision = guidance.FIRMWARE_REVISION if firmware else guidance.WIKI_REVISION
            references.append({"source_id": identifier, "repository": f"ArduPilot/{repo}",
                "revision": revision, "path": path, "sha256": digest,
                "url": f"https://github.com/ArduPilot/{repo}/blob/{revision}/{path}",
                "retrieved_at": RETRIEVED_AT})
        else:
            references.extend(guidance.citation(identifier)["references"])
    # Some topics share a source; return independent copies without duplication.
    return deepcopy(list({item["url"]: item for item in references}.values()))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _scalar(value):
    """Preserve normal report scalars without copying unbounded nested input."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, (int, float)) and _number(value) is not None:
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)[:256]
    return None


def _parameter_value(value):
    # Never shorten a numeric string: that changes its meaning, especially for
    # exact decimal representations of IEEE subnormals. Original values stay in
    # the archived readback even when an unsupported representation is omitted.
    if isinstance(value, (str, Decimal)):
        text = str(value)
        return text if len(text) <= MAX_PARAMETER_VALUE_CHARS else None
    return _scalar(value)


def _observations(report):
    values = report.get("parameter_evidence", {}).get("observations", [])
    result = {}
    for item in values[:128]:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            name = item["name"][:64]
            result[name] = {"name": name, "value": _scalar(item.get("value")),
                            "age_at_start_s": _scalar(item.get("age_at_start_s")),
                            "basis": "logged_at_interval_start"}
    return result


def _parameter_context(report, store, cancel_check):
    """Select reverified full preflight evidence without merging log values."""
    logged = _observations(report)
    evidence = report.get("parameter_evidence", {})
    context = {"basis": "logged_at_interval_start", "complete_preflight_readback": False,
        "parameter_count": len(logged), "readback_reference": None,
        "logged_evidence": {"state": evidence.get("state", "missing"), "retained_observation_count": len(logged),
            "observations_truncated": evidence.get("observations_truncated", False),
            "change_count": evidence.get("change_count", 0)},
        "numeric_disagreements": [], "numeric_disagreement_count": 0, "numeric_disagreements_truncated": False,
        "logged_only_names": [], "logged_only_count": 0,
        "effective_configuration": "unknown", "causal_attribution": False, "warnings": [],
        "interpretation": "Parameter presence, readback completeness and effective in-flight activation are different evidence."}
    configuration = report.get("configuration", {})
    if not configuration or configuration.get("status") != "flight_bound_readbacks":
        return logged, logged, context
    if store is None:
        context["warnings"].append("The full preflight readback could not be reverified without its local catalog; advice uses only logged observations, never the display preview.")
        return logged, logged, context
    try:
        from .flight_configuration import configuration_parameters
        values, reference = configuration_parameters(store, report, cancel_check=cancel_check)
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as error:
        context["warnings"].append("The complete preflight readback could not be reverified: " + str(error))
        return logged, logged, context
    params = {}
    for index, (name, row) in enumerate(values.items()):
        if index % 256 == 0 and cancel_check and cancel_check():
            raise CancelledError("Performance advice cancelled while reviewing preflight evidence.")
        params[name] = {"name": name, "value": _parameter_value(row["value"]),
            "mav_param_type": row["mav_param_type"], "decoded_type": row["decoded_type"],
            "canonical_token": row["canonical_token"], "snapshot_id": reference["snapshot_id"],
            "basis": "complete_typed_preflight_readback", "observed_at": deepcopy(row.get("observed_at")),
            "effective_time_bounds": {"earliest": None, "latest": None, "basis": "unknown"},
            "reboot_requirement": "unknown"}
    context.update(basis="complete_typed_preflight_readback", complete_preflight_readback=True, parameter_count=len(params),
        readback_reference={key: deepcopy(reference[key]) for key in (
            "snapshot_id", "acquisition_id", "parameter_count", "typed_hash", "source", "firmware_identity",
            "completeness", "consistency", "observation_range_utc_us")})
    disagreements = [{"name": name, "preflight_value": params[name]["value"], "logged_value": logged[name]["value"]}
        for name in sorted(params.keys() & logged.keys())
        if _number(params[name]["value"]) is not None and _number(logged[name]["value"]) is not None
        and _number(params[name]["value"]) != _number(logged[name]["value"])]
    only_logged = sorted(logged.keys() - params.keys())
    context.update(numeric_disagreements=disagreements[:MAX_OBSERVATIONS], numeric_disagreement_count=len(disagreements),
                   numeric_disagreements_truncated=len(disagreements) > MAX_OBSERVATIONS,
                   logged_only_names=only_logged[:MAX_OBSERVATIONS], logged_only_count=len(only_logged))
    if disagreements:
        context["warnings"].append("Some logged values differ numerically from the preflight readback. Timing, representation precision or a setting change may explain this; the app has not established effective-state changes.")
    if only_logged:
        context["warnings"].append("Some logged names are absent from this component's complete preflight enumeration. They remain separate observations, not additions to the verified readback.")
    return params, logged, context


def _firmware(report, parameter_context):
    declared = report.get("operating_profile", {}).get("declared_firmware")
    reference = parameter_context.get("readback_reference") or {}
    return {"declared_firmware": _scalar(declared), "reviewed_release": guidance.FIRMWARE_RELEASE,
            "reported_readback_firmware_identity": deepcopy(reference.get("firmware_identity")),
            "exact_build_validated": False, "scope": FIRMWARE_SCOPE}


def build_performance_advice(report: dict, store=None, *, cancel_check=None) -> dict:
    """Build bounded, reproducible hypotheses using existing report evidence.

    Conditional spectra are deliberately not promoted to proven motor noise;
    neither parameter presence nor a release label proves an active feature.
    """
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    if cancel_check and cancel_check():
        raise CancelledError("Performance advice cancelled.")
    params, logged_params, parameter_context = _parameter_context(report, store, cancel_check)
    findings = report.get("findings", [])
    kinds = {item.get("kind") for item in findings if isinstance(item, dict)}
    evidence_ids = [item["id"] for item in findings[:20] if isinstance(item, dict) and "id" in item]
    evidence = report.get("parameter_evidence", {})
    spectrum = report.get("spectrum", {})
    control = report.get("control", {})
    metrics = report.get("metrics", {})
    recommendations = []
    firmware = _firmware(report, parameter_context)

    def add(identifier, title, priority, status, symptom, mechanism, families,
            action, alternatives, requirements, tradeoffs, experiment, references):
        observed = [item for name, item in params.items() if name.startswith(tuple(families))]
        logged = [item for name, item in logged_params.items() if name.startswith(tuple(families))]
        recommendations.append({
            "id": identifier, "title": title, "priority": priority, "status": status,
            "symptom": symptom, "mechanism": mechanism, "parameter_families": families,
            "observed_parameters": observed[:MAX_OBSERVATIONS],
            "observed_parameters_truncated": len(observed) > MAX_OBSERVATIONS,
            "observed_parameter_count": len(observed), "parameter_context_basis": parameter_context["basis"],
            "logged_parameters": logged[:MAX_OBSERVATIONS], "logged_parameters_truncated": len(logged) > MAX_OBSERVATIONS,
            "candidate_action": action, "alternative_hypotheses": alternatives,
            "evidence_requirements": requirements, "tradeoffs": tradeoffs,
            "verification_experiment": experiment, "references": _references(*references),
            "firmware_scope": deepcopy(firmware), "evidence_ids": evidence_ids,
            "parameter_evidence_state": evidence.get("state", "missing"),
            "proposed_values": None, "causal_attribution": False,
            "requires_operator_review": True,
        })

    if parameter_context["complete_preflight_readback"]:
        add("review_preflight_readback", "Review the preflight readback with in-flight observations", 1, "conditional_review",
            f"A complete typed preflight enumeration containing {parameter_context['parameter_count']} parameters is linked to this flight. Logged interval evidence remains {evidence.get('state', 'missing')}.",
            "The requested readback establishes observed stored values for one component before arming. It is not atomic and does not establish when a change became active or whether a reboot was required.",
            ["INS_", "ATC_", "MOT_", "BATT", "SERVO_"],
            "Use this preserved preflight readback as the configuration comparison reference. Inspect separately retained logged observations, postflight readback and change timing before linking a parameter difference to performance.",
            ["A parameter was written or activated after the readback", "A log value differs because of serialization precision or observation time", "Unobserved edits or reboot-dependent activation"],
            ["Exact aircraft/component and flight linkage with intact originals", "Logged change timeline and postflight readback", "Firmware-specific activation and reboot requirements"],
            ["Completeness covers one reported enumeration; it does not prove complete effective in-flight configuration.", "A differing logged observation is retained separately and never silently replaces the preflight value."],
            ["Compare matched flight phases using each flight's own complete preflight readback.", "Resolve readback/log discrepancies and repeated-trial confounders before claiming a setting caused a metric change."],
            ["tracking", "heavy_lift"])
    elif evidence.get("state") != "observed_only" or parameter_context["warnings"]:
        add("establish_flight_configuration", "Establish the settings used by this flight", 1, "evidence_needed",
            f"Parameter evidence is {evidence.get('state', 'missing')}; historical configuration is not established.",
            "A current readback can differ from the settings flown. Changes within a flight or gaps can mix multiple configurations.",
            ["INS_", "ATC_", "MOT_", "BATT", "SERVO_"],
            "Capture the current complete parameter list before arming, bind it to this vehicle and flight, then retain observed changes and the postflight readback.",
            ["A performance difference may follow a firmware, hardware, payload or flight-regime change."],
            ["Vehicle identity and boot identity", "Snapshot capture time, completeness and armed state", "Change timeline and data continuity"],
            ["A complete readback is configuration evidence; it does not prove a change caused a performance difference."],
            ["Use the earliest usable flight as a comparison starting point; no pre-FACt or FACt file is required.",
             "Compare matched flight phases and retain all changed settings; split analysis at in-flight changes."],
            ["tracking", "heavy_lift"])

    vibration_problem = bool(kinds & {"vibration_excursion", "accelerometer_clipping"})
    if vibration_problem:
        add("mechanical_vibration", "Investigate vibration and clipping at the source", 1, "inspect_first",
            "The selected interval contains a vibration screening excursion or a positive accelerometer clipping delta.",
            "Propeller imbalance, bearings, loose structure, isolation and cable coupling can transmit vibration into the IMU. Clipped acceleration loses information that a software notch cannot recover.",
            ["INS_"],
            "Inspect propellers, motors, frame joints, flight-controller mounting and cable strain relief before considering a filter change.",
            ["Landing or ground contact rather than sustained airborne vibration", "Airframe/payload resonance", "Different IMU, logging gaps or changed flight regime"],
            ["Same IMU and continuous clipping counters", "Armed/airborne phase and timestamped vibration evidence", "Mechanical inspection and high-rate gyro evidence for filter assessment"],
            ["Softer mounting is not automatically better; mounting dynamics and cable coupling matter.", "A gyro notch does not fix mechanical defects or reconstruct clipped accelerometer samples."],
            ["Record the mechanical change separately from parameters.", "Repeat a comparable interval and compare VIBE distributions and clipping deltas with coverage shown."],
            ["mechanical", "vibration", "clipping"])

    notch_interest = vibration_problem or spectrum.get("status") == "conditional" or any(
        name.startswith(("INS_HNTCH_", "INS_HNTC2_", "INS_GYRO_FILTER", "SERVO_BLH_", "FFT_")) for name in params)
    if notch_interest:
        qualified = spectrum.get("status") == "conditional"
        add("qualify_noise_measurement", "Measure noise before choosing filter settings", 2,
            "conditional_review" if qualified else "evidence_needed",
            "A conditional high-rate spectrum is available." if qualified else "No qualified high-rate gyro spectrum is available; VIBE alone cannot locate a notch.",
            "The sampling rate, anti-alias filtering and recorded filter stage determine which frequencies can be interpreted. A spectral peak can be motion, resonance or aliased noise.",
            ["INS_LOG_", "LOG_", "INS_GYRO_RATE", "INS_RAW_"],
            "Collect firmware-supported high-rate pre/post-filter gyro data and inspect continuity, actual sample rate and anti-alias context before selecting frequency, bandwidth or harmonics.",
            ["Aliasing from noise above the acquisition bandwidth", "Non-motor airframe resonance", "Different filtering stages or sample rates between flights"],
            ["Known IMU, units, measurement clock and filtering stage", "Continuous waveform and measured sample rate", "Anti-alias chain evidence and aligned motor RPM/throttle"],
            ["Higher logging and filter rates consume storage and processor time.", "A hypothetical static filter model is not measured attenuation of the aircraft's dynamic filter chain."],
            ["Compare pre/post spectra from the same operating condition and sensor; do not join clock resets or large gaps.", "Check residual peaks and control response across hover and changing thrust, not just one peak."],
            ["sampling", "gyro_filter"])
        add("notch_tracking_source", "Compare RPM, throttle and FFT notch tracking", 3, "evidence_needed",
            "Motor-noise filtering merits review; observed parameters do not establish tracking accuracy or motor-noise origin.",
            "A dynamic notch follows a noise source. Throttle estimates motor speed and depends on thrust calibration and payload; measured ESC RPM can track changing motor speeds directly.",
            ["INS_HNTCH_", "INS_HNTC2_", "SERVO_BLH_", "SERVO_DSHOT_", "MOT_THST_EXPO", "MOT_SPIN_MIN", "FFT_"],
            "If reliable compatible ESC RPM telemetry is available, evaluate an RPM-tracked notch against the existing throttle-tracked setup. Bidirectional DShot is one way to obtain RPM, not a filter itself. Keep throttle tracking or assess supported FFT/RPM-sensor alternatives when telemetry is unsuitable.",
            ["Throttle tracking with correctly measured hover frequency/reference and thrust response", "An independent RPM sensor", "In-flight FFT where compiled support, peak tracking and processor capacity are demonstrated", "Static or second-notch review for independently verified residual noise"],
            ["Exact flight-controller board/build, output mapping and DMA/output support", "Compatible ESC model and installed ESC firmware", "Correct motor pole count and electrical-to-mechanical RPM conversion", "Per-motor RPM availability, update rate, delay, dropouts and agreement with noise peaks", "Serial ESC telemetry or supported CAN telemetry may provide RPM without bidirectional DShot"],
            ["More per-motor/harmonic notches add processor cost and phase delay; wider bandwidth can worsen response.", "Bad pole count or unreliable RPM can position a notch incorrectly; unsupported bidirectional DShot can cause unpredictable motor operation.", "FFT adds computation/tracking delay; throttle tracking may need review after payload changes."],
            ["Confirm hardware/firmware compatibility and telemetry first, then compare one reviewed configuration change at a time.", "Check pre/post gyro noise, rate tracking, RPM dropouts and processor/logging health under matched conditions.", "Retain the previous settings and record adverse response as well as improvements."],
            ["esc_telemetry", "esc_source", "esc_notch", "throttle_notch", "fft_tracking", "harmonic_notch"])

    if control.get("status") == "observed" or any(name.startswith("ATC_") for name in params):
        add("control_response", "Separate response limits, noise and actuator authority", 3, "conditional_review",
            "Rate-response evidence or control parameters are present; a universal good/bad tracking threshold is not assumed.",
            "Gains, input shaping, filtering delay, disturbances and actuator limits can all change desired-versus-achieved response. D-term activity alone does not diagnose a gain problem.",
            ["ATC_RAT_", "ATC_ACC_", "ATC_ACCEL_", "ATC_RATE_", "ATC_INPUT_TC", "INS_GYRO_FILTER"],
            "Review aligned desired/actual rates, PID terms, input limits, noise and verified output limiting before choosing a gain or filter change. Use the exact firmware's tuning sequence once the aircraft and noise evidence are suitable.",
            ["Demand shaping or acceleration/rate limits", "Mechanical vibration or filter phase delay", "Battery/actuator authority, payload, wind or aggressive manoeuvres"],
            ["Validated RATE units and aligned times", "Comparable excited flight interval and sufficient coverage", "Noise spectrum and actual limit flags with exact firmware interpretation"],
            ["More aggressive gains can improve response while increasing oscillation and noise sensitivity.", "Lower filter cutoff may reduce noise while increasing delay; raising it may pass motor noise.", "ATC_ACCEL_* and ATC_ACC_* names can have different units across releases."],
            ["Change a single reviewed control/filter factor with the rest recorded.", "Compare tracking RMSE, bias, lag, overshoot and noise on comparable manoeuvres; preserve missing/ambiguous metrics.", "Check output limiting and battery condition before attributing an improvement to gains."],
            ["tracking", "rate_limits", "gyro_filter", "pid_source", "actuator_limits"])

    power_interest = bool(kinds & {"battery_load_step_drop", "actuator_command_near_upper_endpoint"}) or any(
        metrics.get(key, {}).get("value") is not None for key in ("mean_power_w", "command_headroom_fraction")) or any(
        name.startswith(("MOT_", "BATT")) for name in params)
    if power_interest:
        add("propulsion_and_power", "Check thrust response, power and measurement calibration", 2, "conditional_review",
            "Power/output evidence or propulsion parameters are present; high commanded output alone is not proof of thrust saturation.",
            "Thrust nonlinearity, voltage compensation, current limiting and payload can change response without a PID change. Voltage/current calibration can also change apparent efficiency.",
            ["MOT_THST_", "MOT_SPIN_", "MOT_BAT_", "BATT", "SERVO_"],
            "Check battery monitor calibration and propulsion mapping; inspect limit flags and voltage/current under load. Review thrust-curve parameters only against measured motor/ESC/propeller thrust data, and voltage/current limits against the actual pack and hardware.",
            ["Different pack charge, temperature, age or connector resistance", "Mass, payload, propeller or wind changes", "Current limiting or incorrect measurement scaling"],
            ["Battery identity, cell count and calibrated voltage/current", "Motor/output mapping, protocol and actual limit flags", "Matched payload and flight phase", "Measured thrust curve before adjusting MOT_THST_EXPO"],
            ["Changing a current limit changes protection and available control authority; removing protection is not an optimization shortcut.", "Command headroom is not measured thrust reserve or remaining endurance."],
            ["Compare equal-duration, comparable phases with battery condition and payload recorded.", "Compare calibrated power/energy and tracking together; inspect voltage sag and limiting at the same timestamps."],
            ["thrust", "power", "actuator_limits", "heavy_lift"])

    add("comparison_context", "Compare flights with their software and hardware context", 4, "evidence_needed",
        "OS, ground-station and companion software changes can coincide with configuration and performance changes.",
        "The flight controller closes its control loops onboard. Ground software or UDP changes may alter commands, missions or recorded timing/packet coverage; they do not by themselves establish a change in physical flight dynamics.",
        [], "Link OS/build, Flight Lab version, GCS versions, relevant companion apps, flight-controller/ESC firmware and hardware to each flight, distinguishing detected values from operator declarations.",
        ["A recording/telemetry artifact rather than changed aircraft behavior", "Simultaneous firmware, calibration, hardware, payload, weather or flight-regime changes"],
        ["Flight-bound context snapshot and parameter change timeline", "Onboard log to distinguish aircraft behavior from ground reception", "Matched vehicle, phase, manoeuvre, payload and battery context"],
        ["Version correlation is a confounder to investigate, not proof that the OS or GCS caused a flight-performance change."],
        ["Use the first usable current-configuration flight as the reference and compare later matched flights.", "Repeat promising changes and report counterexamples; describe association until controlled evidence supports a stronger explanation."],
        ["tracking", "heavy_lift"])

    recommendations.sort(key=lambda item: item["priority"])
    return {"version": VERSION, "mode": "bundled_offline", "retrieved_at": RETRIEVED_AT,
            "parameter_context": parameter_context,
            "coverage_topics": ["flight_configuration", "mechanical_vibration_and_clipping", "sampling_and_aliasing",
                "notch_tracking_and_esc_telemetry", "filter_delay_and_control_response", "propulsion_power_and_calibration",
                "software_hardware_and_flight_comparability"],
            "recommendations": recommendations[:MAX_RECOMMENDATIONS],
            "recommendations_truncated": len(recommendations) > MAX_RECOMMENDATIONS,
            "firmware_scope": firmware,
            "limitations": ["Reviewed hypotheses cover the listed topics, not every possible cause or solution.",
                "No runtime internet research or automatic aircraft parameter writes occur.",
                "No numerical notch, filter or PID values are generated from VIBE, an unknown build, or these diagnostic summaries.",
                "Observed associations and conditional expected effects are separate from causal conclusions.",
                "The pinned documentation is a historical reviewed snapshot, not a claim of latest guidance."]}


def explain_parameter_change(name: str, before, after, firmware_scope=None) -> dict:
    """Explain a setting difference without assigning it a measured effect.

    The direction describes the recorded value only. Expected effects remain
    conditional on feature use, firmware, other settings and operating context.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
        raise ValueError("parameter name must contain 1-64 letters, digits or underscores")
    name = name.upper()
    a, b = _number(before), _number(after)
    direction = ("unchanged" if a == b else "increased" if b > a else "decreased") if a is not None and b is not None else (
        "unchanged" if before == after else "changed")
    mechanism = "No reviewed mechanism is mapped to this exact parameter or supported family."
    expected = "Consult exact firmware/board parameter metadata before interpreting this change. No benefit or detriment is inferred."
    requirements = ["Flight-bound before/after parameter snapshots and exact firmware metadata", "Matched flight phases, hardware, payload, battery and environment"]
    metrics = ["Relevant validated flight metric, selected after the parameter meaning is verified"]
    sources = ["tracking"]
    matched = True
    if name.startswith(("INS_HNTCH_", "INS_HNTC2_")):
        suffix = name.split("_", 2)[2]
        mechanisms = {
            "ENABLE": "Enables or disables this harmonic-notch group; presence does not prove it ran on the aircraft.",
            "MODE": "Selects the source tracking the notch center frequency. Throttle estimates speed; ESC/RPM measures it; FFT estimates spectral peaks.",
            "FREQ": "Sets a reference or minimum notch frequency whose role depends on tracking mode; it is not always the in-flight center frequency.",
            "BW": "Changes the notch bandwidth and therefore the noise band attenuated and the phase-delay tradeoff.",
            "ATT": "Changes notch attenuation at its target frequencies; this alone does not establish total noise reduction or better control.",
            "HMNCS": "Selects a harmonic bitmask, not a harmonic count; its numeric direction does not describe filter count or quality.",
            "OPTS": "Changes an option bitmask; per-motor/multiple notches and update behavior depend on exact firmware and mode.",
            "REF": "Changes mode-dependent reference/scaling; in throttle mode its meaning depends on the measured hover thrust/frequency relationship.",
            "FM_RAT": "Changes the allowed lower frequency ratio; lower-frequency filtering can add more control-band phase delay.",
        }
        if suffix in mechanisms:
            mechanism = mechanisms[suffix]
            expected = "If the active notch follows verified noise more accurately, residual gyro noise may fall; added bandwidth or notches can increase phase delay. Check measured pre/post noise and response before calling the change an improvement."
            requirements += ["High-rate known-stage gyro data with sample-rate/anti-alias context", "Actual notch tracking source, availability and active filter configuration"]
            metrics = ["Pre/post gyro spectrum and residual noise", "Rate tracking, lag and oscillation", "Tracking-source dropouts and processor health"]
            sources = ["harmonic_notch", "esc_notch", "throttle_notch"]
        else:
            matched = False
    elif name in {"SERVO_BLH_BDMASK", "SERVO_BLH_POLES", "SERVO_BLH_TRATE", "SERVO_DSHOT_ESC"}:
        mechanism = ("Motor pole count converts electrical RPM to shaft RPM; an incorrect count can misplace RPM-tracked filters." if name.endswith("POLES") else
            "ESC telemetry configuration controls a possible RPM source. Bidirectional DShot carries RPM over the motor signal wire; it is not itself a notch filter.")
        expected = "Compatible, correctly scaled, reliable RPM may improve dynamic notch tracking. Protocol/channel or firmware incompatibility can prevent telemetry or disrupt motor operation; a larger bitmap/value is not inherently better."
        requirements += ["Board/DMA/output and ESC firmware compatibility", "Motor pole count, per-motor RPM, update rate and dropout measurements"]
        metrics = ["RPM consistency with measured motor-noise peaks", "Telemetry coverage/delay", "Residual gyro noise and rate response"]
        sources = ["esc_telemetry", "esc_source", "esc_notch"]
    elif name == "INS_GYRO_FILTER" or re.fullmatch(r"ATC_RAT_(RLL|PIT|YAW)_FLT[EDT]", name):
        mechanism = "Low-pass cutoff trades suppression of high-frequency content against phase delay; the signal being filtered depends on the exact parameter."
        expected = "If this is an active conventional low-pass cutoff, a higher cutoff generally passes more high-frequency content with less filtering delay; a lower cutoff does the converse. Neither direction guarantees better tracking."
        requirements += ["Exact filter stage and units", "High-rate spectrum plus aligned desired/actual response"]
        metrics = ["Residual noise spectrum", "Tracking error and lag", "PID activity and oscillation"]
        sources = ["gyro_filter", "pid_source", "tracking"]
    elif re.fullmatch(r"ATC_RAT_(RLL|PIT|YAW)_(P|I|D|FF|D_FF)", name):
        term = "D_FF" if name.endswith("_D_FF") else name.rsplit("_", 1)[1]
        mechanism = {"P": "Proportional gain weights current rate error.", "I": "Integral gain weights accumulated rate error and can reject sustained bias subject to integrator limits.",
            "D": "Derivative action responds to changing error and is sensitive to high-frequency noise.",
            "FF": "Feedforward acts from the desired command rather than waiting for tracking error.",
            "D_FF": "Derivative feedforward responds to how quickly the desired command changes."}[term]
        expected = "A gain increase may improve one aspect of response while increasing oscillation, noise or limiting; a decrease may reduce those effects while worsening tracking. Closed-loop error or term RMS alone does not select a gain direction."
        requirements += ["Exact controller equation, gain units and active limits", "Matched excited RATE/PID data, noise and actuator-limit evidence"]
        metrics = ["Tracking RMSE and bias", "Rise time/overshoot/settling where identifiable", "Noise and limit flags"]
        sources = ["pid_source", "tracking", "gyro_filter", "actuator_limits"]
    elif name == "ATC_INPUT_TC" or name.startswith(("ATC_ACC_", "ATC_ACCEL_", "ATC_RATE_")):
        mechanism = "Input shaping and rate/acceleration limits change the requested response, which can alter apparent tracking even without a different controller tune."
        expected = "A longer input time constant generally requests a gentler response; larger enabled rate/acceleration limits can permit more aggressive demand. Zero/disabled semantics and units require exact release metadata."
        requirements += ["Exact parameter units; ATC_ACCEL_* and ATC_ACC_* are not interchangeable", "Aligned pilot input, desired rates, actual rates and limits"]
        metrics = ["Demand amplitude and slew", "Actual tracking, lag and output limiting"]
        sources = ["rate_limits", "tracking"]
    elif name.startswith(("MOT_THST_", "MOT_SPIN_")):
        mechanism = "Thrust mapping and spin endpoints affect how a requested thrust becomes a motor command; they also affect throttle-based notch tracking."
        expected = "A mapping matching the measured propulsion system may improve consistency. A numerical increase does not imply more efficiency or better response; inappropriate changes can degrade control."
        requirements += ["Measured motor/ESC/propeller thrust curve and correct output mapping", "Battery voltage and payload context"]
        metrics = ["Tracking versus thrust demand", "Output/limit behavior", "Calibrated power and residual motor noise"]
        sources = ["thrust", "throttle_notch"]
    elif name.startswith("MOT_BAT_"):
        mechanism = "Battery voltage compensation and current limiting alter available output/control response as load and voltage change."
        expected = "Correct battery settings may improve consistency; changing protection limits can change available authority and battery stress. Do not interpret a larger limit as an improvement."
        requirements += ["Actual battery capability and calibrated voltage/current", "Exact compensation and limit semantics"]
        metrics = ["Voltage sag/current and limit flags", "Tracking at comparable voltage/load"]
        sources = ["power", "actuator_limits"]
    elif re.fullmatch(r"BATT\d*_(VOLT_MULT|AMP_PERVLT|AMP_OFFSET|VLT_OFFSET|MONITOR)", name):
        mechanism = "Battery monitor type and scaling alter reported voltage/current, and may change compensation or protection decisions that use them."
        expected = "A changed power/energy number may reflect measurement calibration rather than changed physical efficiency. Verify with independent measurements before comparing flights."
        requirements += ["Independent voltage/current calibration and battery monitor identity"]
        metrics = ["Calibrated voltage/current", "Recomputed comparable energy and power", "Compensation/limit behavior"]
        sources = ["power"]
    elif name.startswith(("INS_LOG_", "LOG_", "INS_RAW_")) or name == "INS_GYRO_RATE":
        mechanism = "Logging/sampling settings change the evidence acquired; gyro backend-rate changes may also change filter execution and processor load."
        expected = "More samples can improve diagnosis only with correct timing and anti-alias context. Logging changes can also create dropped data or load; a changed spectrum may reflect acquisition changes."
        requirements += ["Actual rate, filtering stage, units and acquisition chain", "Logging drops and CPU timing"]
        metrics = ["Sample continuity/rate and spectral comparability", "Processor and logging health"]
        sources = ["sampling"]
    else:
        matched = False
    return {"parameter": name, "before": _parameter_value(before), "after": _parameter_value(after), "direction": direction,
            "status": "conditional_explanation" if matched else "unmapped_parameter", "mechanism": mechanism,
            "expected_effect": expected, "evidence_requirements": requirements, "verification_metrics": metrics,
            "references": _references(*sources), "firmware_scope": {"declared": _scalar(firmware_scope), "scope": FIRMWARE_SCOPE},
            "causal_attribution": False, "proposed_values": None,
            "interpretation": "Direction describes the recorded parameter value, not the direction of performance change. Expected effects are conditional hypotheses, not a causal finding."}
