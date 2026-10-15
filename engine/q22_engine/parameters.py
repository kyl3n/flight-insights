"""Versioned parameter snapshots and explicitly scoped descriptive comparisons.

This module does not decode DataFlash, infer units, verify hardware identity,
send vehicle commands, or establish a causal effect from two flights.
"""
from __future__ import annotations

import csv
from concurrent.futures import CancelledError
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re

from .store import Store


ALGORITHM = "q22-explicit-csv-metrics/1"
PARAMETER_PARSER = "mission-planner-qgc-parameters/1"
MAX_PARAMETERS = 50000
MAX_DIFF = 1000
MAX_PARAMETER_EXPLANATIONS = 128
MAX_TIME_SECONDS = (2 ** 53 - 1) / 1_000_000
NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
SOURCE_KINDS = {"flight_readback", "current_readback", "file_export", "logged_startup"}
CONDITIONS = ("payload_kg", "mass_kg", "battery_id", "battery_soc_pct",
              "battery_temperature_c", "wind_mps", "hardware_revision", "props")


def text_field(data: dict, key: str, maximum: int = 512) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} must be a nonempty string, at most {maximum} characters.")
    return value.strip()


def finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric data.")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite numeric data.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite numeric data.")
    return result


def identity(data: dict) -> dict:
    # These values are supplied explicitly, never guessed from a filename.
    return {key: text_field(data, key) for key in ("aircraft_id", "board", "firmware")}


def parse_parameters(path: Path) -> tuple[dict, dict]:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Parameter export exceeds the 20 MiB parser limit.")
    parameters = {}
    format_name = None
    source_ids = None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, line in enumerate(handle, 1):
            if len(line) > 4096:
                raise ValueError(f"Parameter line {line_number} exceeds 4096 characters.")
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            parts = [piece.strip() for piece in line.split(",")] if "," in line else line.split()
            if len(parts) == 2:
                current_format = "mission_planner"
                name, value = parts
                metadata = {}
            elif len(parts) == 5 and "," not in line:
                current_format = "qgroundcontrol"
                system, component, name, value, mav_type = parts
                if not all(re.fullmatch(r"\d{1,3}", item) for item in (system, component, mav_type)):
                    raise ValueError(f"Invalid QGC source/type fields at line {line_number}.")
                ids = (int(system), int(component))
                if not 1 <= ids[0] <= 255 or not 1 <= ids[1] <= 255 or not 1 <= int(mav_type) <= 10:
                    raise ValueError(f"Invalid QGC source/type range at line {line_number}.")
                if source_ids is not None and source_ids != ids:
                    raise ValueError("A snapshot cannot combine multiple MAVLink systems/components.")
                source_ids = ids
                metadata = {"system_id": ids[0], "component_id": ids[1], "mav_param_type": int(mav_type)}
            else:
                raise ValueError(f"Unrecognized parameter format at line {line_number}.")
            if format_name is not None and current_format != format_name:
                raise ValueError("Mixed parameter formats are ambiguous; import separate exports.")
            format_name = current_format
            if not NAME.fullmatch(name):
                raise ValueError(f"Invalid parameter name at line {line_number}.")
            if not NUMBER.fullmatch(value) or len(value) > 128:
                raise ValueError(f"Invalid parameter number at line {line_number}.")
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"Invalid parameter number at line {line_number}.") from exc
            if not number.is_finite() or not math.isfinite(float(number)):
                raise ValueError(f"Nonfinite/out-of-range parameter at line {line_number}.")
            if name in parameters:
                raise ValueError(f"Duplicate parameter {name}; export a single unambiguous snapshot.")
            parameters[name] = {"value": value, **metadata}
            if len(parameters) > MAX_PARAMETERS:
                raise ValueError("Parameter count exceeds the supported 50,000 limit.")
    if not parameters:
        raise ValueError("No parameters found in export.")
    return parameters, {"format": format_name, "parser_version": PARAMETER_PARSER,
                        "mavlink_source": list(source_ids) if source_ids else None}


def group_for(name: str) -> str:
    prefix = name.split("_", 1)[0]
    return {"ATC": "Attitude / rate control", "INS": "Inertial / filtering",
            "BATT": "Battery / calibration", "BATT2": "Battery / calibration",
            "MOT": "Motors", "GPS": "Navigation", "GPS2": "Navigation",
            "EK3": "Estimator", "COMPASS": "Compass / calibration",
            "PSC": "Position control", "LOG": "Logging"}.get(prefix, prefix)


def parameter_annotation(name: str) -> dict:
    return {"group": group_for(name), "units": None, "bitmask_labels": None,
            "calibration_device_specific": name.startswith(("COMPASS_", "BATT", "INS_ACC", "INS_GYR", "SERVO", "RC"))}


def snapshot_summary(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key != "parameters"}


def bounded_csv_lines(handle):
    while line := handle.readline(1024 * 1024 + 1):
        if len(line) > 1024 * 1024:
            raise ValueError("CSV physical line exceeds the 1 MiB decoder limit.")
        yield line


def bounded_page(key: str, documents: list[dict], offset: int, limit: int) -> dict:
    retained, size = [], 0
    for document in documents:
        size += len(json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        if size > 2 * 1024 * 1024:
            break
        retained.append(document)
    more = len(retained) < len(documents) or len(documents) == limit
    return {key: retained, "next_offset": offset + len(retained) if more else None}


def parameter_diff(before: dict, after: dict) -> dict:
    issues = [f"{key} differs" for key in ("aircraft_id", "board", "firmware") if before[key] != after[key]]
    preserved_readbacks = any(snapshot.get("acquisition_id") for snapshot in (before, after))
    if preserved_readbacks:
        for label, snapshot in (("before", before), ("after", after)):
            if snapshot.get("usable_for_full_comparison") is not True:
                issues.append(f"{label} does not establish a complete, consistent requested readback")
        if before.get("hardware_uid") != after.get("hardware_uid"):
            issues.append("reported hardware UID differs or is unavailable")
    changed, added, removed = [], [], []
    old, new = before["parameters"], after["parameters"]
    for name in sorted(old.keys() | new.keys()):
        annotation = {"name": name, **parameter_annotation(name)}
        if name not in old:
            added.append({**annotation, "value": new[name]["value"]})
        elif name not in new:
            removed.append({**annotation, "value": old[name]["value"]})
        else:
            typed = all(row.get("canonical_token") is not None and row.get("mav_param_type") is not None for row in (old[name], new[name]))
            differs = ((old[name]["mav_param_type"], old[name]["canonical_token"]) != (new[name]["mav_param_type"], new[name]["canonical_token"])) if typed else Decimal(old[name]["value"]) != Decimal(new[name]["value"])
            if differs:
                changed.append({**annotation, "before": old[name]["value"], "after": new[name]["value"],
                    **({"before_type": old[name]["mav_param_type"], "after_type": new[name]["mav_param_type"],
                        "before_token": old[name]["canonical_token"], "after_token": new[name]["canonical_token"]} if typed else {})})
    counts = {"changed": len(changed), "added": len(added), "removed": len(removed)}
    retained, remaining = [], 1024 * 1024
    for rows in (changed, added, removed):
        output = []
        for row in rows[:MAX_DIFF]:
            size = len(json.dumps(row, separators=(",", ":")).encode())
            if size > remaining:
                break
            output.append(row)
            remaining -= size
        retained.append(output)
    return {"before_id": before["id"], "after_id": after["id"],
            "compatibility": {"compatible": not issues, "issues": issues,
                              "identity_basis": "reported UID and firmware; not authenticated" if preserved_readbacks else "operator-declared exact board/firmware identity"},
            "changed": retained[0], "added": retained[1], "removed": retained[2],
            "counts": counts, "truncated": any(len(rows) != len(output) for rows, output in zip((changed, added, removed), retained)),
            "limitations": ["Units, bitmask labels, ranges and applicability remain unavailable until matching firmware metadata is validated.",
                            "A snapshot diff never authorizes copying calibration or applying values to an aircraft."] +
                           (["Missing values in incomplete readbacks mean unobserved evidence, not removed or added aircraft settings.",
                             "Readback values do not establish in-flight activation times or reboot applicability."] if preserved_readbacks else [])}


def performance_metrics(path: Path, start: float, end: float, max_gap: float, *, break_before_column=None, cancel_check=None) -> dict:
    """Streaming trapezoidal power and exact integral of linear tracking error²."""
    duration = end - start
    energy_ws = covered = squared_error_seconds = tracking_covered = 0.0
    peak = None
    rows = selected_rows = negative_count = valid_pairs = 0
    first_time = last_time = None
    previous = None
    exclusions = []
    exclusion_count = 0
    tracking_exclusions = []
    tracking_exclusion_count = 0
    tracking_valid_pairs = 0
    tracking_enabled = False

    def exclude(left, right, reason):
        nonlocal exclusion_count
        if right > left:
            exclusion_count += 1
            if len(exclusions) < 100:
                exclusions.append({"start_s": left, "end_s": right, "reason": reason})

    def exclude_tracking(left, right, reason):
        nonlocal tracking_exclusion_count
        if right > left:
            tracking_exclusion_count += 1
            if len(tracking_exclusions) < 100:
                tracking_exclusions.append({"start_s": left, "end_s": right, "reason": reason})

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(bounded_csv_lines(handle))
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError("CSV has duplicate column names.")
        if not {"time_s", "voltage_v", "current_a"}.issubset(fields):
            raise ValueError("CSV requires explicit-unit columns time_s,voltage_v,current_a.")
        if len(fields) > 64:
            raise ValueError("CSV exceeds 64 columns.")
        optional = {"desired_roll_deg", "actual_roll_deg"}
        if len(optional & set(fields)) == 1:
            raise ValueError("Tracking requires both desired_roll_deg and actual_roll_deg.")
        tracking_enabled = optional.issubset(fields)
        for row in reader:
            if cancel_check and cancel_check():
                raise CancelledError("Metric calculation cancelled.")
            rows += 1
            if rows > 10_000_000:
                raise ValueError("CSV exceeds the supported 10 million rows.")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV row {rows + 1}.")
            time = finite(row["time_s"], f"row {rows + 1} time_s")
            if time < 0 or time > MAX_TIME_SECONDS or (last_time is not None and time <= last_time):
                raise ValueError("CSV timestamps must be nonnegative and strictly increasing in a single boot epoch.")
            if first_time is None:
                first_time = time
            last_time = time

            def optional_number(name):
                return None if row[name].strip() == "" else finite(row[name], f"row {rows + 1} {name}")

            voltage, current = optional_number("voltage_v"), optional_number("current_a")
            power = voltage * current if voltage is not None and current is not None else None
            if voltage is not None and voltage < 0:
                raise ValueError("Negative pack voltage is invalid for this explicit-unit adapter.")
            if power is not None and not math.isfinite(power):
                raise ValueError("Power overflow; check imported units.")
            error = None
            if tracking_enabled:
                desired, actual = optional_number("desired_roll_deg"), optional_number("actual_roll_deg")
                if desired is not None and actual is not None:
                    error = desired - actual
                    if not math.isfinite(error) or abs(error) > 1e100:
                        raise ValueError("Tracking range overflow; check units.")
            if start <= time <= end:
                selected_rows += 1
                negative_count += int(current is not None and current < 0)
            if previous is not None:
                old_time, old_power, old_error, old_current = previous
                left, right = max(old_time, start), min(time, end)
                if right > left:
                    dt = time - old_time
                    barrier = break_before_column and row.get(break_before_column) == "1"
                    if dt > max_gap or barrier:
                        reason = "corruption_or_clock_boundary" if barrier else "gap_exceeds_max_gap_s"
                        exclude(left, right, reason)
                        exclude_tracking(left, right, reason)
                    else:
                        def interpolate(a, b, at):
                            return a + (b - a) * ((at - old_time) / dt)

                        if old_power is None or power is None:
                            exclude(left, right, "missing_voltage_or_current")
                        else:
                            p_left = interpolate(old_power, power, left)
                            p_right = interpolate(old_power, power, right)
                            energy_ws += (p_left + p_right) * 0.5 * (right - left)
                            covered += right - left
                            peak = max(p_left, p_right) if peak is None else max(peak, p_left, p_right)
                            valid_pairs += 1
                            # Include interpolation endpoints outside the selected window.
                            if old_time < start and old_current is not None and old_current < 0:
                                negative_count += 1
                            if time > end and current is not None and current < 0:
                                negative_count += 1
                        if error is not None and old_error is not None:
                            e_left = interpolate(old_error, error, left)
                            e_right = interpolate(old_error, error, right)
                            squared_error_seconds += (e_left * e_left + e_left * e_right + e_right * e_right) / 3 * (right - left)
                            tracking_covered += right - left
                            tracking_valid_pairs += 1
                        else:
                            exclude_tracking(left, right, "missing_desired_or_actual_roll")
            previous = (time, power, error, current)
    if rows < 2:
        raise ValueError("Performance CSV requires at least two time samples.")
    exclude(start, min(end, first_time), "before_first_sample")
    exclude(max(start, last_time), end, "after_last_sample")
    exclude_tracking(start, min(end, first_time), "before_first_sample")
    exclude_tracking(max(start, last_time), end, "after_last_sample")
    if not math.isfinite(energy_ws) or not math.isfinite(squared_error_seconds):
        raise ValueError("Metric overflow; check units and selected interval.")
    scope = {"algorithm_version": ALGORITHM, "interval_start_s": start, "interval_end_s": end,
             "interval_start_us": str(round(start * 1_000_000)), "interval_end_us": str(round(end * 1_000_000)),
             "selected_duration_s": duration, "sample_count": selected_rows,
             "source_row_count": rows, "source_fields": ["time_s", "voltage_v", "current_a"],
             "valid_pair_count": valid_pairs, "covered_duration_s": covered,
             "coverage_fraction": covered / duration, "exclusions": exclusions,
             "exclusion_count": exclusion_count, "exclusions_truncated": exclusion_count > len(exclusions),
             "uncertainty": "Measurement calibration and sampling uncertainty are not quantified."}

    def metric(value, unit, **extra):
        return {**scope, "value": value, "unit": unit, **extra}

    metrics = {
        "energy_wh": metric(energy_ws / 3600 if covered else None, "Wh", interpretation="Signed energy in observed valid intervals only; excluded intervals are not filled."),
        "mean_power_w": metric(energy_ws / covered if covered else None, "W"),
        "peak_power_w": metric(peak, "W", interpretation="Peak of the piecewise linear sampled power; intersample peaks are unknown."),
        "roll_tracking_rmse_deg": metric(math.sqrt(squared_error_seconds / tracking_covered) if tracking_covered else None,
                                          "deg", covered_duration_s=tracking_covered,
                                          coverage_fraction=tracking_covered / duration,
                                          source_fields=["time_s", "desired_roll_deg", "actual_roll_deg"],
                                          valid_pair_count=tracking_valid_pairs,
                                          exclusions=tracking_exclusions, exclusion_count=tracking_exclusion_count,
                                          exclusions_truncated=tracking_exclusion_count > len(tracking_exclusions),
                                          interpretation="Time-weighted roll angle tracking error, assuming linear error between valid samples. Missing pairs and long gaps are excluded. This is not a validated PID response metric."),
    }
    return {"metrics": metrics, "negative_current_sample_count": negative_count,
            "source_time_start_s": first_time, "source_time_end_s": last_time,
            "warnings": (["Negative current was retained as measured; inspect calibration, sign, and returned-energy interpretation."] if negative_count else [])}


class ParameterService:
    def __init__(self, store: Store):
        self.store = store

    def dispatch(self, method: str, params: dict, cancel_check=None, progress=None) -> dict:
        methods = {"parameters.import": self.import_parameters, "parameters.list": self.list_parameters,
                   "parameters.promote_readback": self.promote_readback,
                   "parameters.diff": self.diff, "sessions.import": self.import_session,
                   "sessions.list": self.list_sessions, "comparison.run": self.compare}
        if method not in methods:
            raise ValueError(f"Unsupported parameter/session method: {method}")
        if not isinstance(params, dict):
            raise ValueError("Method params must be an object.")
        if method == "parameters.promote_readback":
            return self.promote_readback(params, cancel_check=cancel_check, progress=progress)
        return methods[method](params)

    def promote_readback(self, params, *, cancel_check=None, progress=None):
        from .flight_configuration import FlightConfigurationService
        return FlightConfigurationService(self.store).promote(params, cancel_check=cancel_check, progress=progress)

    def import_parameters(self, params):
        metadata = identity(params)
        label = text_field(params, "label")
        source_kind = text_field(params, "source_kind")
        if source_kind not in SOURCE_KINDS:
            raise ValueError("Unsupported parameter source_kind.")
        observed_at = text_field(params, "observed_at")
        try:
            stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError as exc:
            raise ValueError("observed_at requires an ISO8601 timestamp with timezone.") from exc
        if not isinstance(params.get("synthetic", False), bool):
            raise ValueError("synthetic must be boolean.")
        source = self.store.archive_file(text_field(params, "path", 4096))
        parameters, parsed_metadata = parse_parameters(self.store.raw_path(source["sha256"]))
        snapshot = self.store.add_document("snapshot", {
            **metadata, **parsed_metadata, "label": label, "source_kind": source_kind,
            "observed_at": observed_at, "identity_verification": "operator_declared",
            "parameters": parameters, "parameter_count": len(parameters), "source": source,
            "synthetic": params.get("synthetic", False), "immutable": True,
        })
        return {"snapshot": snapshot_summary(snapshot)}

    def list_parameters(self, params):
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        documents = self.store.list_documents("snapshot", limit, offset)
        return bounded_page("snapshots", [snapshot_summary(item) for item in documents], offset, limit)

    def diff(self, params):
        return parameter_diff(self.store.get_document("snapshot", text_field(params, "before_id")),
                              self.store.get_document("snapshot", text_field(params, "after_id")))

    def import_session(self, params):
        metadata = identity(params)
        label, phase = text_field(params, "label"), text_field(params, "phase")
        snapshot = self.store.get_document("snapshot", text_field(params, "snapshot_id"))
        if any(metadata[key] != snapshot[key] for key in metadata):
            raise ValueError("Session identity must match its immutable parameter snapshot.")
        start = finite(params.get("interval_start_s"), "interval_start_s")
        end = finite(params.get("interval_end_s"), "interval_end_s")
        gap = finite(params.get("max_gap_s"), "max_gap_s")
        if start < 0 or end <= start or end > MAX_TIME_SECONDS or gap <= 0:
            raise ValueError("Require 0 <= interval_start_s < interval_end_s and max_gap_s > 0.")
        evidence = params.get("parameter_evidence", "unverified")
        changes = params.get("parameter_changes", "unknown")
        if evidence not in ("unverified", "operator_attested"):
            raise ValueError("parameter_evidence must be unverified or operator_attested.")
        if changes not in ("none_reported", "unknown", "present"):
            raise ValueError("parameter_changes must be none_reported, unknown, or present.")
        note = params.get("parameter_evidence_note", "")
        if not isinstance(note, str) or len(note) > 2000 or (evidence == "operator_attested" and not note.strip()):
            raise ValueError("An operator attestation requires a nonempty parameter_evidence_note (max 2000 characters).")
        supplied = params.get("conditions", {})
        if not isinstance(supplied, dict):
            raise ValueError("conditions must be an object.")
        conditions = {}
        for key in CONDITIONS:
            value = supplied.get(key)
            if value is None or value == "":
                conditions[key] = None
            elif key in ("battery_id", "hardware_revision", "props"):
                conditions[key] = text_field(supplied, key)
            else:
                value = finite(value, key)
                if key in ("payload_kg", "mass_kg", "wind_mps") and value < 0:
                    raise ValueError(f"{key} cannot be negative.")
                if key == "battery_soc_pct" and not 0 <= value <= 100:
                    raise ValueError("battery_soc_pct must be 0..100.")
                conditions[key] = value
        synthetic = params.get("synthetic", False)
        if not isinstance(synthetic, bool):
            raise ValueError("synthetic must be boolean.")
        if synthetic != snapshot["synthetic"]:
            raise ValueError("Synthetic and real session/snapshot evidence cannot be mixed.")
        source = self.store.archive_file(text_field(params, "path", 4096))
        measured = performance_metrics(self.store.raw_path(source["sha256"]), start, end, gap)
        session = self.store.add_document("session", {
            **metadata, **measured, "label": label, "phase": phase,
            "snapshot_id": snapshot["id"], "snapshot_source_sha256": snapshot["source"]["sha256"],
            "snapshot_observed_at": snapshot["observed_at"], "source": source,
            "interval_start_s": start, "interval_end_s": end, "max_gap_s": gap,
            "interval_start_us": str(round(start * 1_000_000)), "interval_end_us": str(round(end * 1_000_000)),
            "parameter_evidence": evidence, "parameter_evidence_note": note,
            "parameter_changes": changes, "conditions": conditions, "synthetic": synthetic,
            "time_domain": "single_boot_relative_seconds", "interval_basis": "operator_selected",
            "primary_battery_monitor": "one explicitly exported voltage/current pair",
            "identity_verification": "operator_declared", "immutable": True,
        })
        return {"session": session}

    def list_sessions(self, params):
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        sessions = self.store.list_documents("session", limit, offset)
        return bounded_page("sessions", sessions, offset, limit)

    def compare(self, params):
        # Keep the shared explanation layer out of parameter-parser imports.
        # These are hypotheses about meaning, never an attribution of measured
        # changes to the setting or a parameter command.
        from .performance_advice import explain_parameter_change

        before = self.store.get_document("session", text_field(params, "before_session_id"))
        after = self.store.get_document("session", text_field(params, "after_session_id"))
        before_snapshot = self.store.get_document("snapshot", before["snapshot_id"])
        after_snapshot = self.store.get_document("snapshot", after["snapshot_id"])
        confounders = []

        def flag(field, reason, a=None, b=None):
            confounders.append({"field": field, "reason": reason, "before": a, "after": b})

        if before["id"] == after["id"]:
            flag("session_id", "The same flight interval is not an independent comparison.")
        elif before["source"]["sha256"] == after["source"]["sha256"]:
            flag("source_sha256", "The same original file is not evidence of two independent flights.")
        for key in ("aircraft_id", "board", "firmware", "phase", "synthetic"):
            if before[key] != after[key]:
                flag(key, "Comparison context differs.", before[key], after[key])
        for key in CONDITIONS:
            a, b = before["conditions"][key], after["conditions"][key]
            if a is None or b is None:
                flag(key, "Condition was not recorded for both intervals.", a, b)
            elif a != b:
                flag(key, "Condition differs; no unsupported normalization is applied.", a, b)
        for label, session, snapshot in (("before", before, before_snapshot), ("after", after, after_snapshot)):
            if snapshot["source_kind"] != "flight_readback" or session["parameter_evidence"] != "operator_attested":
                reason = ("Logged values are observations at or before the selected start; completeness and unobserved changes are not verified."
                          if snapshot["source_kind"] == "logged_interval" else
                          "A current/exported snapshot alone does not establish settings in this flight interval.")
                flag(f"{label}.parameter_evidence", reason)
            if session["parameter_changes"] != "none_reported":
                flag(f"{label}.parameter_changes", "Parameter timeline is incomplete or changed. Inspect the logged observation boundaries and select separately scoped intervals; unobserved changes remain unknown.")
            if session.get("warnings") and session.get("log_id"):
                flag(f"{label}.log_quality", "Decoded log carries timing, schema or coverage warnings. Review its source evidence before interpretation.")
            if session["metrics"]["energy_wh"]["coverage_fraction"] < 1 - 1e-9:
                flag(f"{label}.power_coverage", "Selected interval has missing power coverage; totals describe observed portions only.")
            if session["negative_current_sample_count"]:
                flag(f"{label}.negative_current", "Confirm sensor calibration/sign and returned-energy interpretation.")
            tracking = session["metrics"]["roll_tracking_rmse_deg"]
            if tracking["coverage_fraction"] < 1 - 1e-9:
                flag(f"{label}.tracking_coverage", "Tracking is unavailable or covers only part of the selected interval; a lower power result cannot establish a control-performance improvement.")
        old_duration = before["interval_end_s"] - before["interval_start_s"]
        new_duration = after["interval_end_s"] - after["interval_start_s"]
        if old_duration != new_duration:
            flag("selected_duration_s", "Different exposure durations confound total-energy comparison.", old_duration, new_duration)
        metrics = []
        for name, old in before["metrics"].items():
            new = after["metrics"][name]
            a, b = old["value"], new["value"]
            delta = b - a if a is not None and b is not None else None
            if delta is not None and not math.isfinite(delta):
                delta = None
            percent = delta / abs(a) * 100 if delta is not None and a != 0 else None
            if percent is not None and not math.isfinite(percent):
                percent = None
            metrics.append({"name": name, "unit": old["unit"], "before": a, "after": b,
                            "absolute_change": delta, "percent_change": percent,
                            "before_coverage_fraction": old["coverage_fraction"], "after_coverage_fraction": new["coverage_fraction"]})
        diff = parameter_diff(before_snapshot, after_snapshot)
        firmware = before_snapshot["firmware"] if before_snapshot["firmware"] == after_snapshot["firmware"] else None
        explanations = [explain_parameter_change(item["name"], item["before"], item["after"], firmware)
                        for item in diff["changed"][:MAX_PARAMETER_EXPLANATIONS]]
        return {"before_session_id": before["id"], "after_session_id": after["id"],
                "status": "confounded" if confounders else "descriptive_only", "synthetic": before["synthetic"] or after["synthetic"],
                "parameter_diff": diff, "metrics": metrics,
                "parameter_explanations": explanations,
                "parameter_explanations_truncated": diff["counts"]["changed"] > len(explanations),
                "parameter_explanations_limit": MAX_PARAMETER_EXPLANATIONS,
                "confounders": confounders, "independent_flight_count": None,
                "limitations": ["Observed association is not causation. No tuning values or aircraft writes are generated.",
                                "One selected interval per input is not a repeated-trial experiment; no statistical confidence interval is claimed.",
                                "Firmware, conditions, phase, and parameter applicability are operator-declared, not independently verified.",
                                "A flight readback attestation records the operator's evidence; decoded parameter timelines remain incomplete observations.",
                                "Temperature, vibration, stability and control authority are not validated by these measured power/tracking comparisons."]}
