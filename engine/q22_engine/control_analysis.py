"""Bounded, descriptive RATE/PID evidence. No plant identification or gain tuning."""
from __future__ import annotations

import math
import statistics

from .parameters import finite

MAX_SAMPLES = 8192
AXES = {"roll": ("RDes", "R", "ROut", "PIDR"), "pitch": ("PDes", "P", "POut", "PIDP"), "yaw": ("YDes", "Y", "YOut", "PIDY")}
FIRMWARE_REVISION = "dbe792162d06cab66c3475fd5556bf7a120f119e"
REFERENCES = [f"https://github.com/ArduPilot/ardupilot/blob/{FIRMWARE_REVISION}/libraries/AP_Logger/LogFile.cpp",
              f"https://github.com/ArduPilot/ardupilot/blob/{FIRMWARE_REVISION}/libraries/AC_AttitudeControl/LogStructure.h"]


def quantile(values, probability):
    ordered = sorted(values)
    location = (len(ordered) - 1) * probability
    left = int(location)
    right = min(len(ordered) - 1, left + 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (location - left)


def _quantity(event, name):
    raw, schema = event.get("raw_fields", {}), event.get("field_schema", {})
    value, descriptor = raw.get(name), schema.get(name, {})
    multiplier = descriptor.get("applied_multiplier")
    if (type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e6
            or type(multiplier) not in (int, float) or not math.isfinite(multiplier)
            or descriptor.get("unit") not in {"deg/s", "rad/s"}):
        return None
    value *= multiplier
    value = value * 180 / math.pi if descriptor["unit"] == "rad/s" else value
    return value if math.isfinite(value) and abs(value) <= 1e6 else None


def _uniform(rows):
    if len(rows) < 16 or len({row[4] for row in rows}) != 1:
        return None
    differences = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    interval = statistics.median(differences)
    return interval if interval > 0 and max(abs(value / interval - 1) for value in differences) <= .01 else None


def correlation_lag(rows):
    """Positive lag means achieved rate follows demand; never a stability margin."""
    rows = rows[:2048]
    dt = _uniform(rows)
    unavailable = {"status": "unavailable", "reason": "Requires at least 16 continuous uniformly sampled RATE pairs within 1% jitter.", "seconds": None}
    if dt is None:
        return unavailable
    desired, actual = [row[1] for row in rows], [row[2] for row in rows]
    if statistics.pstdev(desired) < .5 or statistics.pstdev(actual) < .5:
        return {**unavailable, "reason": "Insufficient command or response excitation (standard deviation below 0.5 deg/s)."}
    maximum = min(128, len(rows) // 4, max(1, int(.5 / dt)))
    values = []
    for lag in range(-maximum, maximum + 1):
        a = desired[:len(rows) - lag] if lag >= 0 else desired[-lag:]
        b = actual[lag:] if lag >= 0 else actual[:len(rows) + lag]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        covariance = sum((x-ma) * (y-mb) for x, y in zip(a, b))
        scale = math.sqrt(sum((x-ma)**2 for x in a) * sum((y-mb)**2 for y in b))
        values.append((covariance / scale if scale else -1, lag))
    peak, lag = max(values)
    # Broad peaks and periodic aliases must not produce a falsely precise delay.
    alternatives = [score for score, offset in values if abs(offset - lag) > 2]
    ambiguous = (peak < .7 or abs(lag) == maximum or (alternatives and peak - max(alternatives) < .02))
    return {"status": "ambiguous" if ambiguous else "available", "seconds": None if ambiguous else lag * dt,
            "best_observed_lag_s": lag * dt, "correlation": peak, "resolution_s": dt,
            "sample_count": len(rows), "search_half_width_s": maximum * dt,
            "reason": "Weak, broad, edge or multiply-peaked correlation; delay withheld." if ambiguous else None,
            "method": "Mean-centered normalized cross-correlation of a bounded continuous RATE window; positive means achieved follows desired.",
            "uncertainty": "One sample resolution is not a statistical confidence interval. Closed-loop correlation does not identify plant delay or stability margins."}


class ControlAnalyzer:
    def __init__(self, config, start, end, gap, firmware, phase="unknown"):
        config = {} if config is None else config
        if not isinstance(config, dict) or set(config) - {"axis", "step"}:
            raise ValueError("control accepts axis and optional explicit step only.")
        self.axis = config.get("axis", "roll")
        if self.axis not in AXES:
            raise ValueError("control.axis must be roll, pitch or yaw.")
        self.start, self.end, self.gap = start, end, gap
        self.rows, self.pid = [], []
        self.observed = self.excluded = self.segment = 0
        self.high_water = None
        self.pid_high_water = None
        self.firmware_matched = firmware in {"Copter-4.7.1", "ArduCopter 4.7.1", "ArduCopter V4.7.1"}
        self.phase = phase
        self.step_config = config.get("step")
        if self.step_config is not None:
            step = self.step_config
            if not isinstance(step, dict) or set(step) - {"time_s", "pre_s", "post_s", "min_amplitude_deg_s", "settling_band_fraction"}:
                raise ValueError("control.step requires an explicit command time and bounded response window.")
            self.step_config = {key: finite(step.get(key, default), key) for key, default in
                                (("time_s", None), ("pre_s", .5), ("post_s", 2), ("min_amplitude_deg_s", 5), ("settling_band_fraction", .05))}
            step = self.step_config
            if (not start < step["time_s"] < end or not .1 <= step["pre_s"] <= 10 or not .2 <= step["post_s"] <= 30
                    or step["time_s"] - step["pre_s"] < start or step["time_s"] + step["post_s"] > end
                    or not .5 <= step["min_amplitude_deg_s"] <= 1000 or not .01 <= step["settling_band_fraction"] <= .2):
                raise ValueError("Step must fit selected interval; pre0.1..10s, post0.2..30s, amplitude0.5..1000deg/s, band1..20%.")

    def barrier(self):
        self.segment += 1

    def add(self, event, t):
        if not self.start <= t <= self.end:
            return
        message = event.get("message_type")
        desired_name, actual_name, output_name, pid_name = AXES[self.axis]
        if message == "RATE":
            self.observed += 1
            desired, actual = _quantity(event, desired_name), _quantity(event, actual_name)
            if desired is None or actual is None or (self.high_water is not None and t <= self.high_water):
                self.excluded += 1
                self.barrier()
                return
            if self.high_water is not None and t - self.high_water > self.gap:
                self.barrier()
            self.high_water = t
            if len(self.rows) < MAX_SAMPLES:
                ref = {"time_s": t, "record_index": event.get("record_index"), "byte_offset": event.get("byte_offset"), "message_type": message}
                self.rows.append((t, desired, actual, ref, self.segment))
        elif message == pid_name and len(self.pid) < MAX_SAMPLES:
            if self.pid_high_water is not None and t <= self.pid_high_water:
                self.barrier()
                return
            self.pid_high_water = t
            raw, schema = event.get("raw_fields", {}), event.get("field_schema", {})
            # Dimensionless controller terms are explicitly labeled recorded
            # units; angle/rate units are never inferred from PID field names.
            terms = {key: raw[key] for key in ("P", "I", "D", "FF", "DFF", "Dmod", "SRate")
                     if type(raw.get(key)) in (int, float) and math.isfinite(raw[key]) and abs(raw[key]) <= 1e6
                     and schema.get(key, {}).get("format_code") == "f"}
            flag = raw.get("Flags") if schema.get("Flags", {}).get("format_code") == "B" else None
            self.pid.append({"time_s": t, "terms": terms, "flags": flag if type(flag) is int and 0 <= flag <= 255 else None,
                             "record_index": event.get("record_index")})

    def step(self, clipping):
        rejected = lambda reason: {"status": "unavailable", "reason": reason}
        if self.step_config is None:
            return rejected("Select an explicit isolated command step with before/after duration; no automatic tuning maneuver is inferred.")
        if self.phase == "unknown":
            return rejected("Declare the observed flight phase before interpreting a response window; phase is never inferred from a command change.")
        config = self.step_config
        when, pre, post = config["time_s"], config["pre_s"], config["post_s"]
        rows = [row for row in self.rows if when - pre <= row[0] <= when + post]
        dt = _uniform(rows)
        if dt is None or rows[0][0] > when - pre + dt * 1.01 or rows[-1][0] < when + post - dt * 1.01:
            return rejected("Step window has insufficient coverage, irregular sampling or a data/clock barrier.")
        if clipping:
            return rejected("Observed clipping/vibration or reset evidence in the selected interval withholds response classification.")
        pid = [row for row in self.pid if when - pre <= row["time_s"] <= when + post]
        if not self.firmware_matched or len(pid) < 16 or any(row["flags"] is None for row in pid):
            return rejected("Requires matching pinned firmware semantics and recorded PID flags covering the step; output percentage alone cannot establish unsaturated response.")
        if (pid[0]["time_s"] > when - pre + dt * 2 or pid[-1]["time_s"] < when + post - dt * 2
                or any(b["time_s"] - a["time_s"] > max(dt * 2.01, .05) for a, b in zip(pid, pid[1:]))):
            return rejected("PID flag coverage has gaps across the step window.")
        if any(row["flags"] != 0 for row in pid):
            return rejected("Recorded PID limiting, reset, integral reset or unknown flag is present; isolated unsaturated step metrics withheld.")
        before, after = [row for row in rows if row[0] < when], [row for row in rows if row[0] >= when]
        if len(before) < 5 or len(after) < 10:
            return rejected("Insufficient pre/post samples.")
        initial = statistics.median(row[1] for row in before)
        target = statistics.median(row[1] for row in after)
        amplitude = target - initial
        if abs(amplitude) < config["min_amplitude_deg_s"]:
            return rejected("Command step lacks declared minimum excitation.")
        tolerance = abs(amplitude) * .05
        if (max(abs(row[1] - initial) for row in before) > tolerance
                or max(abs(row[1] - target) for row in after) > tolerance):
            return rejected("Command is not an isolated plateau-to-plateau step; overlapping/shaped demand requires different analysis.")
        if max(abs(row[2] - initial) for row in before) > tolerance:
            return rejected("Initial achieved rate is not steady at the starting reference.")
        normalized = [(row[0], (row[2] - initial) / amplitude) for row in after]
        tail = normalized[-max(5, len(normalized) // 5):]
        band = config["settling_band_fraction"]
        if any(abs(value - 1) > band for _, value in tail):
            return rejected("Final achieved rate has not remained within the declared settling band; extend capture or investigate undertracking.")
        crossings = []
        for threshold in (.1, .9):
            found = next((stamp for stamp, value in normalized if value >= threshold), None)
            crossings.append(found)
        if None in crossings:
            return rejected("Missing 10% or 90% response crossing.")
        last_outside = max((index for index, (_, value) in enumerate(normalized) if abs(value - 1) > band), default=-1)
        settling = normalized[last_outside + 1][0] - when
        return {"status": "observed_response", "command_time_s": when, "amplitude_deg_s": amplitude,
                "rise_time_s": crossings[1] - crossings[0], "settling_time_s": settling,
                "overshoot_fraction": max(0, max(value for _, value in normalized) - 1), "settling_band_fraction": band,
                "crossing_resolution_s": dt, "sample_count": len(rows), "reason": None,
                "interpretation": "Observed closed-loop reference-normalized response; no plant model, stability margin, gain prescription or proof of spare thrust."}

    def finish(self, clipping=False):
        rows = self.rows
        if not rows:
            return {"status": "unavailable", "axis": self.axis, "reason": "No schema-validated desired/actual RATE pairs in the selected interval.", "series": [], "sample_count": 0}
        integral = square = covered = 0.0
        for a, b in zip(rows, rows[1:]):
            dt = b[0] - a[0]
            if a[4] != b[4] or not 0 < dt <= self.gap:
                continue
            x, y = a[1] - a[2], b[1] - b[2]
            integral += (x+y) / 2 * dt
            square += (x*x+x*y+y*y) / 3 * dt
            covered += dt
        errors = [abs(row[1] - row[2]) for row in rows]
        terms = {}
        for name in ("P", "I", "D", "FF", "DFF", "Dmod", "SRate"):
            values = [row["terms"][name] for row in self.pid if name in row["terms"]]
            if values:
                terms[name] = {"sample_count": len(values), "mean": sum(values) / len(values),
                               "rms": math.sqrt(sum(value*value for value in values) / len(values)),
                               "minimum": min(values), "maximum": max(values), "unit": "recorded controller units; not inferred angle/rate units"}
        stride = max(1, math.ceil(len(rows) / 512))
        return {"status": "observed", "axis": self.axis, "unit": "deg/s", "sample_count": len(rows),
                "sample_limit": MAX_SAMPLES, "observed_records": self.observed, "excluded_records": self.excluded,
                "truncated": self.observed - self.excluded > len(rows),
                "scope": {"start_s": rows[0][0], "end_s": rows[-1][0]},
                "tracking": {"rmse_deg_s": math.sqrt(square / covered) if covered else None,
                    "bias_deg_s": integral / covered if covered else None, "covered_duration_s": covered,
                    "coverage_fraction": covered / (self.end-self.start),
                    **{f"absolute_error_p{int(q*100)}_deg_s": quantile(errors, q) for q in (.5, .95, .99)},
                    "quantile_basis": "Sample quantiles of bounded retained RATE window; RMSE/bias time-weight linear pairs only."},
                "lag": correlation_lag(rows), "step": self.step(clipping),
                "pid_terms": {"status": "observed" if terms else "unavailable", "statistics": terms,
                    "flags": {"sample_count": len(self.pid), "nonzero_count": sum(row["flags"] not in (None, 0) for row in self.pid),
                              "unknown_count": sum(row["flags"] is None for row in self.pid), "pinned_firmware_label_matched": self.firmware_matched},
                    "interpretation": "P/D/I activity can reflect motion, noise or limiting. Flags require firmware context; no gain direction follows from term RMS."},
                "series": [{"time_s": row[0], "desired_deg_s": row[1], "actual_deg_s": row[2], "record_index": row[3]["record_index"]} for row in rows[::stride]],
                "references": REFERENCES, "limitations": ["No gain/phase margins or numerical gain candidates are inferred from closed-loop RATE correlation.",
                    "PID terms retain recorded units. D activity alone does not establish sensor noise or a required gain change.",
                    "Matching a declared firmware label is not independent verification of the installed build."]}
