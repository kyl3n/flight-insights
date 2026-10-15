"""Descriptive comparisons of saved evidence, with no historical host inference."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import re

from .flight_environment import diff_snapshots
from .logs import check_cancel
from .performance_advice import explain_parameter_change

VERSION = "q22-insight-comparison/1"
BASE_METRICS = {
    "mean_power_w": "W", "energy_wh": "Wh", "peak_power_w": "W",
    "roll_tracking_rmse_deg": "deg", "minimum_pack_voltage_v": "V",
    "command_headroom_fraction": "fraction",
}
CONDITIONS = ("aircraft_mass_kg", "payload_mass_kg", "rotor_count", "propeller_diameter_m",
              "battery_cell_count", "battery_capacity_ah", "rotor_layout", "flight_phase")
PARAMETER_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
MAX_OBSERVATIONS = 128
MAX_METRICS = 96
MAX_EXPLANATIONS = 32


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e100 else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _duration(scope):
    start, end = _number(scope.get("interval_start_s")), _number(scope.get("interval_end_s"))
    return end - start if start is not None and end is not None and end > start else None


def _coverage(metric):
    return {key: _number(metric.get(key)) for key in (
        "sample_count", "covered_duration_s", "coverage_fraction", "valid_pair_count", "exclusion_count")}


def _metric_sources(report):
    """Only compare reviewed numeric fields, never arbitrary report objects."""
    result = {name: {**_mapping(_mapping(report.get("metrics")).get(name)), "expected_unit": unit}
              for name, unit in BASE_METRICS.items()}
    vibration = _mapping(report.get("vibration"))
    distributions = vibration.get("distributions", [])
    coverage_rows = vibration.get("coverage", [])
    coverage = {(str(row.get("instance_id")), row.get("axis")): row for row in (coverage_rows if isinstance(coverage_rows, list) else [])[:24]
                if isinstance(row, dict)}
    for row in distributions[:24] if isinstance(distributions, list) else []:
        if not isinstance(row, dict) or row.get("axis") not in {"x", "y", "z"}:
            continue
        instance = str(row.get("instance_id"))
        if not re.fullmatch(r"reported|[0-9]{1,2}", instance):
            continue
        key = (instance, row["axis"])
        for statistic in ("p95_mps2", "maximum_mps2"):
            name = f"vibration.{instance}.{row['axis']}.{statistic}"
            result[name] = {**_mapping(coverage.get(key)), "value": row.get(statistic), "unit": "m/s²", "expected_unit": "m/s²",
                            "distribution_approximate": row.get("distribution_approximate")}
    control = _mapping(report.get("control"))
    if control.get("status") == "observed" and control.get("axis") in {"roll", "pitch", "yaw"}:
        track = _mapping(control.get("tracking"))
        result[f"control.{control['axis']}.tracking_rmse_deg_s"] = {
            **track, "value": track.get("rmse_deg_s"), "unit": "deg/s", "expected_unit": "deg/s",
            "sample_count": control.get("sample_count")}
    spectrum = _mapping(report.get("spectrum"))
    if spectrum.get("status") == "conditional":
        bins = spectrum.get("bins")
        resolution = _number(spectrum.get("frequency_resolution_hz"))
        valid = (isinstance(bins, list) and 1 < len(bins) <= 1025 and resolution is not None and resolution > 0
                 and spectrum.get("unit") == "(rad/s)^2/Hz"
                 and all(isinstance(row, dict) and _number(row.get("frequency_hz")) is not None
                         and _number(row.get("psd")) is not None and row["psd"] >= 0 for row in bins))
        if valid:
            result["spectrum.retained_band_power"] = {
                "value": _number(sum(row["psd"] for row in bins) * resolution),
                "unit": "(rad/s)²", "expected_unit": "(rad/s)²", "sample_count": spectrum.get("sample_count"),
                "covered_duration_s": _duration({"interval_start_s": spectrum.get("measurement_interval_start_s"),
                                                 "interval_end_s": spectrum.get("measurement_interval_end_s")})}
    return result


def _spectrum_comparability(before, after):
    left, right = _mapping(before.get("spectrum")), _mapping(after.get("spectrum"))
    keys = ("method", "source", "filter_stage", "axis", "sensor_instance", "window_samples", "sample_rate_hz",
            "frequency_resolution_hz", "anti_alias_cutoff_hz", "measurement_clock_field")
    if any(left.get(key) in (None, "", "unknown") or left.get(key) != right.get(key) for key in keys):
        return False
    a, b = left.get("bins", []), right.get("bins", [])
    return (isinstance(a, list) and isinstance(b, list) and 1 < len(a) == len(b) <= 1025
            and all(isinstance(x, dict) and isinstance(y, dict) and x.get("frequency_hz") == y.get("frequency_hz")
                    for x, y in zip(a, b)))


def _observations(evidence):
    rows = evidence.get("observations", [])
    if not isinstance(rows, list):
        return {}, True
    result, invalid = {}, len(rows) > MAX_OBSERVATIONS
    for row in rows[:MAX_OBSERVATIONS]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not PARAMETER_NAME.fullmatch(row["name"]):
            invalid = True
            continue
        value = row.get("value")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool) or len(str(value)) > 128:
            invalid = True
            continue
        try:
            decimal = Decimal(str(value))
        except InvalidOperation:
            invalid = True
            continue
        if not decimal.is_finite() or not math.isfinite(float(decimal)) or row["name"] in result:
            invalid = True
            continue
        result[row["name"]] = {"value": str(value), "number": decimal,
                               "age_at_start_s": _number(row.get("age_at_start_s")),
                               "evidence": deepcopy(_mapping(row.get("evidence")))}
    return result, invalid


def _parameter_diff(before, after):
    left, right = _mapping(before.get("parameter_evidence")), _mapping(after.get("parameter_evidence"))
    old, old_invalid = _observations(left)
    new, new_invalid = _observations(right)
    eligible = all(item.get("state") == "observed_only" and item.get("observations_eligible_for_review") is True
                   and not item.get("observations_truncated") and not item.get("changes_truncated")
                   for item in (left, right)) and not (old_invalid or new_invalid) and bool(old) and bool(new)
    changed = []
    for name in sorted(old.keys() & new.keys()):
        if old[name]["number"] != new[name]["number"]:
            changed.append({"name": name, "before": old[name]["value"], "after": new[name]["value"],
                "before_age_at_start_s": old[name]["age_at_start_s"], "after_age_at_start_s": new[name]["age_at_start_s"],
                "before_evidence": old[name]["evidence"], "after_evidence": new[name]["evidence"]})
    before_only = [{"name": name, "value": old[name]["value"]} for name in sorted(old.keys() - new.keys())]
    after_only = [{"name": name, "value": new[name]["value"]} for name in sorted(new.keys() - old.keys())]
    return {"basis": "observed_at_interval_start", "before_state": left.get("state", "missing"),
            "after_state": right.get("state", "missing"), "observations_eligible_for_review": eligible,
            "complete_effective_configuration": False, "causal_attribution": False,
            "changed": changed, "before_only": before_only, "after_only": after_only,
            "counts": {"changed": len(changed), "before_only": len(before_only), "after_only": len(after_only),
                       "compared": len(old.keys() & new.keys())},
            "truncated": bool(left.get("observations_truncated") or right.get("observations_truncated") or old_invalid or new_invalid),
            "limitations": ["Differences describe numeric parameter observations, not confirmed changes to the complete effective configuration.",
                "A parameter observed in only one report is missing evidence in the other, not an added or removed aircraft setting.",
                "Loaded historical parameter files are not substituted for observed flight settings; activation/reboot requirements remain unverified."]}


def _flight_context(report):
    """Only a previously recorded verified flight linkage can bind an environment.

    The report-generation linkage helper must establish this exact contract. It
    prevents callers from accidentally using an analysis-host snapshot or
    a matching filename/system number as historical flight evidence.
    """
    context = _mapping(report.get("flight_context"))
    linkage = _mapping(context.get("linkage"))
    scope = _mapping(report.get("scope"))
    expected = {key: scope.get(key) for key in ("source_sha256", "source_key", "boot_epoch", "interval_start_s", "interval_end_s")}
    if (context.get("status") != "verified" or not context.get("flight_record_id")
            or not linkage.get("association_id") or linkage.get("scope") != expected
            or any(value is None for value in expected.values())):
        return {}
    return context


def _related_metric_names(parameter, metrics):
    if parameter.startswith(("INS_HNTCH_", "INS_HNTC2_", "SERVO_BLH_")) or parameter in {"INS_GYRO_FILTER", "SERVO_DSHOT_ESC"}:
        return [row["name"] for row in metrics if row["name"].startswith(("spectrum.", "vibration.", "control."))]
    rate = re.fullmatch(r"ATC_RAT_(RLL|PIT|YAW)_.+", parameter)
    if rate:
        axis = {"RLL": "roll", "PIT": "pitch", "YAW": "yaw"}[rate[1]]
        return [row["name"] for row in metrics if row["name"].startswith(f"control.{axis}.")
                or (axis == "roll" and row["name"] == "roll_tracking_rmse_deg") or row["name"] == "command_headroom_fraction"]
    if parameter.startswith(("MOT_", "BATT")):
        return [row["name"] for row in metrics if row["name"] in {"mean_power_w", "energy_wh", "peak_power_w", "minimum_pack_voltage_v", "command_headroom_fraction"}]
    if parameter.startswith(("ATC_ACC_", "ATC_ACCEL_", "ATC_RATE_")) or parameter == "ATC_INPUT_TC":
        return [row["name"] for row in metrics if row["name"].startswith("control.") or row["name"] == "roll_tracking_rmse_deg"]
    return []


def compare_reports(before, after, store=None, *, cancel_check=None, progress=None):
    """Return observations and caveats; no causal estimator or tuning writer."""
    check_cancel(cancel_check)
    if progress:
        progress("comparing_flight_reports", 0, 1)
    a, b = _mapping(before.get("scope")), _mapping(after.get("scope"))
    pa, pb = _mapping(before.get("operating_profile")), _mapping(after.get("operating_profile"))
    confounders = []
    def issue(code, detail):
        if not any(item["code"] == code for item in confounders):
            confounders.append({"code": code, "detail": detail})

    if before.get("id") == after.get("id"):
        issue("same_report", "The same saved report was selected twice; this is not a before/after experiment.")
    same_source = bool(a.get("source_sha256")) and a.get("source_sha256") == b.get("source_sha256")
    if same_source:
        issue("same_artifact", "Both reports use the same original artifact; independent flights are not established.")
        same_clock = all(a.get(key) == b.get(key) for key in ("source_key", "boot_epoch", "time_domain", "origin_time_us"))
        bounds = [_number(scope.get(key)) for scope in (a, b) for key in ("interval_start_s", "interval_end_s")]
        if same_clock and all(value is not None for value in bounds) and max(bounds[0], bounds[2]) < min(bounds[1], bounds[3]):
            issue("overlapping_interval", "The selected intervals overlap within the same artifact/source/clock, so measurements reuse evidence.")
    if type(a.get("synthetic")) is not bool or type(b.get("synthetic")) is not bool:
        issue("synthetic_status_unknown", "Synthetic versus real source status is missing for at least one report.")
    elif a["synthetic"] != b["synthetic"]:
        issue("synthetic_mismatch", "A synthetic report and a real report are being compared.")
    if before.get("algorithm_version") != after.get("algorithm_version") or not before.get("algorithm_version"):
        issue("algorithm_mismatch", "Metric algorithm versions differ or are unknown; deltas are withheld.")
    if a.get("source_key") != b.get("source_key"):
        issue("source_selection_changed", "Selected telemetry sources differ; sensor/source equivalence needs review.")
    if not a.get("source_sha256") or not b.get("source_sha256"):
        issue("source_provenance_unknown", "The archived original source digest is missing for at least one report.")
    if a.get("time_domain") != b.get("time_domain") or not a.get("time_domain"):
        issue("time_domain_mismatch", "Clock semantics differ or are unknown; deltas are withheld.")
    if a.get("time_domain") == "utc_receive_us" or b.get("time_domain") == "utc_receive_us":
        issue("receive_clock", "At least one report uses ground-receive timing, which can reflect telemetry and host latency.")
    if a.get("max_gap_s") != b.get("max_gap_s"):
        issue("gap_policy_changed", "Different maximum-gap policies change which samples contribute to the metrics.")
    if a.get("battery_id") != b.get("battery_id"):
        issue("battery_monitor_changed", "Different battery-monitor selections can change power and voltage measurement semantics.")
    if _duration(a) != _duration(b):
        issue("interval_duration_changed", "Selected durations differ; total energy is not directly an efficiency measure.")
    if any(_mapping(report.get("evidence_features")).get("barrier_count") for report in (before, after)):
        issue("evidence_barriers", "Corruption or clock barriers interrupt at least one selected evidence interval.")
    for key in CONDITIONS:
        av, bv = pa.get(key), pb.get(key)
        if av in (None, "", "unknown") or bv in (None, "", "unknown"):
            issue("condition_unknown_" + key, f"{key} is unknown in at least one report.")
        elif av != bv:
            issue("condition_changed_" + key, f"Declared {key} differs between the reports.")
    issue("unrecorded_conditions", "Wind, pilot demand, battery state/temperature, propeller condition and hardware changes are not fully controlled by these reports.")
    if not pa.get("declared_firmware") or not pb.get("declared_firmware"):
        issue("firmware_unknown", "The aircraft firmware version is unknown in at least one report.")
    elif pa["declared_firmware"] != pb["declared_firmware"]:
        issue("firmware_changed", "Declared aircraft firmware differs; parameter meaning and controller behavior may have changed.")
    else:
        issue("firmware_unverified", "Matching declared firmware labels do not verify an exact aircraft firmware build.")

    ca, cb = _flight_context(before), _flight_context(after)
    if not ca or not cb or not ca.get("hardware_uid") or not cb.get("hardware_uid"):
        issue("aircraft_identity_unresolved", "Reports do not establish the same verified aircraft identity; source/system numbers alone are insufficient.")
    elif ca["hardware_uid"] != cb["hardware_uid"]:
        issue("aircraft_changed", "The verified aircraft hardware identity differs.")
    elif ca.get("flight_record_id") == cb.get("flight_record_id"):
        issue("same_flight", "Both reports are linked to the same managed flight record.")
    environment = diff_snapshots(ca.get("environment_snapshot"), cb.get("environment_snapshot"))
    environment["linkage_basis"] = "previously_preserved_exact_verified_flight_context"
    if environment["status"] != "compared":
        issue("flight_environment_unknown", "Both reports need preserved, exactly linked flight environments; the current analysis computer is never substituted.")
    if environment["changes"]:
        issue("flight_environment_changed", "The preserved OS, software declarations, observed applications or host load differ; review acquisition effects separately from onboard behavior.")

    old, new = _metric_sources(before), _metric_sources(after)
    metric_rows = []
    semantic_block = any(item["code"] in {"algorithm_mismatch", "time_domain_mismatch"} for item in confounders)
    for name in sorted(old.keys() | new.keys())[:MAX_METRICS]:
        left, right = old.get(name, {}), new.get(name, {})
        av, bv = _number(left.get("value")), _number(right.get("value"))
        unit = left.get("expected_unit", right.get("expected_unit"))
        notes = []
        blocked = semantic_block
        if left.get("unit") != unit or right.get("unit") != unit:
            blocked = True
            notes.append("Matching validated units are unavailable; the numeric delta is withheld.")
        if name == "command_headroom_fraction" and any(pa.get(key) != pb.get(key) or pa.get(key) in (None, "unknown", [])
                for key in ("actuator_protocol", "motor_output_channels", "output_low_us", "output_high_us")):
            blocked = True
            notes.append("PWM channel mapping and endpoint calibration must match for command-headroom deltas.")
        if name in {"mean_power_w", "energy_wh", "peak_power_w", "minimum_pack_voltage_v"} and a.get("battery_id") != b.get("battery_id"):
            blocked = True
            notes.append("Battery-monitor selections differ; the delta is withheld.")
        cov_a, cov_b = _coverage(left), _coverage(right)
        for side, coverage in (("Before", cov_a), ("After", cov_b)):
            fraction = coverage.get("coverage_fraction")
            if fraction is not None and fraction < .8:
                notes.append(f"{side} covers less than 80% of the selected interval; this screening threshold is not a quality certification.")
                issue("limited_metric_coverage", "At least one metric covers less than 80% of its interval; inspect sample coverage and gaps.")
            if coverage.get("exclusion_count"):
                notes.append(f"{side} has excluded or interrupted sample pairs.")
                issue("metric_exclusions", "At least one metric excludes interrupted, invalid or missing sample pairs.")
        if av is not None and bv is not None and (cov_a.get("sample_count") is None or cov_b.get("sample_count") is None):
            notes.append("Sample counts are missing for at least one side.")
            issue("metric_coverage_unknown", "At least one numeric metric has unknown sample counts.")
        if left.get("distribution_approximate") or right.get("distribution_approximate"):
            notes.append("At least one vibration percentile uses a retained approximate sample distribution.")
        if name.startswith("vibration."):
            notes.append("Vibration summaries are not high-rate gyro spectra; this delta cannot validate a notch frequency or its causal effect.")
        if name == "spectrum.retained_band_power":
            if not _spectrum_comparability(before, after):
                blocked = True
                notes.append("Known matching acquisition stage, sensor, axis, clock, method, window and frequency grid are required; the spectral delta is withheld.")
                issue("spectrum_semantics_unmatched", "Spectral acquisition stage or analysis settings are unknown or differ; the spectral delta is withheld.")
            notes.append("Integral of retained PSD bins only; it includes motion within that band and does not isolate motor noise or establish a filter's causal effect.")
            notes.append("Anti-alias attenuation is operator-declared, and only the recorded continuous spectral windows contribute.")
        status = "unavailable" if av is None or bv is None else "not_comparable" if blocked else "descriptive"
        delta = _number(bv - av) if status == "descriptive" else None
        percent = _number(100 * delta / abs(av)) if delta is not None and av else None
        metric_rows.append({"name": name, "unit": unit,
                            "before_unit": left.get("unit") if isinstance(left.get("unit"), str) else None,
                            "after_unit": right.get("unit") if isinstance(right.get("unit"), str) else None,
                            "before": av, "after": bv, "delta": delta,
                            "percent_change": percent, "status": status, "before_coverage": cov_a,
                            "after_coverage": cov_b, "limitations": notes})
    observed_parameters = _parameter_diff(before, after)
    parameters = observed_parameters
    configuration = {"status": "unavailable", "eligible_for_parameter_review": False,
                     "reasons": ["No complete flight-bound readback comparison was requested."], "limitations": []}
    if store is not None and (before.get("configuration") or after.get("configuration")):
        from .flight_configuration import compare_flight_configurations
        configuration = compare_flight_configurations(store, before, after, cancel_check=cancel_check, progress=progress)
        if configuration.get("eligible_for_parameter_review") is True:
            parameters = {"basis": "complete_typed_preflight_readbacks", "before_state": "complete_preflight_readback",
                "after_state": "complete_preflight_readback", "observations_eligible_for_review": True,
                "complete_effective_configuration": False, "causal_attribution": False,
                **{key: deepcopy(configuration[key]) for key in ("changed", "before_only", "after_only", "counts", "truncated", "limitations")}}
        else:
            issue("complete_readback_comparison_unavailable", "Complete compatible preflight readbacks are unavailable; parameter differences use the separately preserved logged observations.")
    issue("incomplete_effective_configuration", "Observed parameter subsets do not establish complete effective configurations or activation/reboot state.")
    if not parameters["observations_eligible_for_review"]:
        issue("parameter_observations_uncertain", "At least one interval has missing, mixed, stale, invalid or truncated parameter observations; changed values remain contextual evidence only.")
    if len(parameters["changed"]) > 1:
        issue("multiple_parameter_changes", "Multiple observed parameters differ; the effect of an individual setting cannot be isolated.")
    if any(row["name"].startswith("BATT") for row in parameters["changed"]):
        issue("battery_configuration_changed", "A battery parameter differs; measurement calibration and compensation changes must be checked before interpreting power/energy deltas.")
    firmware = pa.get("declared_firmware") if pa.get("declared_firmware") == pb.get("declared_firmware") else None
    explanations = []
    for row in parameters["changed"][:MAX_EXPLANATIONS]:
        check_cancel(cancel_check)
        explanation = explain_parameter_change(row["name"], row["before"], row["after"], firmware)
        if "type_changed" in row:
            explanation.update(parameter_type_changed=row["type_changed"], before_type=row["before_type"], after_type=row["after_type"])
        relevant = set(_related_metric_names(row["name"], metric_rows))
        related = sorted([deepcopy(metric) for metric in metric_rows if metric["name"] in relevant],
                         key=lambda metric: (not metric["name"].startswith("spectrum."), metric["name"]))
        explanation.update(observations_eligible_for_review=parameters["observations_eligible_for_review"],
                           related_metrics=related[:12], related_metrics_truncated=len(related) > 12,
                           comparison_note="These are related measured observations, not a finding that this parameter caused their changes.")
        explanations.append(explanation)
    check_cancel(cancel_check)
    if progress:
        progress("comparing_flight_reports", 1, 1)
    return {"version": VERSION, "before_report_id": before.get("id"), "after_report_id": after.get("id"),
            "status": "confounded" if confounders else "descriptive_only", "causal_attribution": False,
            "independent_flight_count": None, "metrics": metric_rows, "parameter_diff": parameters,
            "observed_parameter_diff": observed_parameters, "configuration_comparison": configuration,
            "parameter_explanations": explanations, "parameter_explanations_truncated": len(parameters["changed"]) > MAX_EXPLANATIONS,
            "environment_diff": environment, "confounders": confounders,
            "limitations": ["Two selected intervals support descriptive association, not causal attribution, a validated optimum or an aircraft parameter write.",
                "Before and after follow the user's selection; report creation time is not proof of flight chronology.",
                "Matched maneuvers, phase, hardware, environment and repeat flights are needed to test a proposed mechanism.",
                "Expected effects come from the bundled reviewed guidance library; unsupported parameters remain explicitly unmapped."]}
