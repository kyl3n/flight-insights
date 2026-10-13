"""Bounded, source-scoped offline diagnostics; never aircraft control or tuning writes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import random

from . import guidance
from .control_analysis import ControlAnalyzer
from .control_analysis import quantile
from .filter_models import filter_model
from .logs import LogService, check_cancel
from .parameters import MAX_PARAMETERS, MAX_TIME_SECONDS, NAME, finite, text_field

VERSION = "q22-offline-insights/2"
MAX_FINDINGS = 100
MAX_SERIES = 256
PARAMETER_PREFIXES = ("INS_", "ATC_", "PSC_", "MOT_", "BATT", "SERVO", "RC")
LIMITATIONS = [
    "Findings describe this selected source, boot and interval; they do not establish aircraft airworthiness or causation.",
    "Observed parameters are incomplete evidence, not proof of a complete unchanged configuration or validated firmware build.",
    "VIBE contains vibration summaries, not a raw waveform; no frequency or notch setting is inferred from VIBE.",
    "Declared mass, payload, geometry, battery and actuator calibration are operator claims, not independently measured values.",
    "Recommendations are diagnostic actions and parameter families for review. No parameter values or control writes are generated.",
]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and abs(value) <= 1e100 else None


def quantity(event, field, units):
    """Apply only the decoder's declared scale and independently recorded unit."""
    schema = event.get("field_schema", {}).get(field, {})
    value = number(event.get("raw_fields", {}).get(field))
    if value is None or schema.get("unit") not in units:
        return None
    if event.get("source_format") == "tlog":
        scale = schema.get("applied_multiplier", 1)
    else:
        scale = schema.get("applied_multiplier")
    scale = number(scale)
    return number(value * scale) if scale is not None else None


def operating_profile(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("operating_profile must be an object with explicit-unit fields.")
    bounds = {"aircraft_mass_kg": (.001, 100000), "payload_mass_kg": (0, 100000),
              "rotor_count": (1, 64), "propeller_diameter_m": (.01, 10),
              "battery_cell_count": (1, 128), "battery_capacity_ah": (.001, 100000),
              "output_low_us": (100, 4000), "output_high_us": (100, 4000)}
    allowed = set(bounds) | {"declared_firmware", "rotor_layout", "motor_output_channels", "actuator_protocol", "flight_phase"}
    if set(value) - allowed:
        raise ValueError("Unknown operating_profile fields: " + ", ".join(sorted(set(value) - allowed)))
    result = {"basis": "operator_declared", "mass_basis": "all-up aircraft mass INCLUDING payload"}
    for key, (low, high) in bounds.items():
        datum = value.get(key)
        if datum is None or datum == "":
            result[key] = None
            continue
        datum = finite(datum, key)
        if not low <= datum <= high:
            raise ValueError(f"{key} must be in {low}..{high}, using the unit in its name.")
        if key in {"rotor_count", "battery_cell_count"} and datum != int(datum):
            raise ValueError(f"{key} must be an integer.")
        result[key] = int(datum) if key in {"rotor_count", "battery_cell_count"} else datum
    if result["aircraft_mass_kg"] is not None and result["payload_mass_kg"] is not None:
        if result["payload_mass_kg"] > result["aircraft_mass_kg"]:
            raise ValueError("Payload is a subset of declared all-up aircraft mass, not an additional mass.")
    result["declared_firmware"] = text_field(value, "declared_firmware", 160) if value.get("declared_firmware") else None
    result["flight_phase"] = text_field(value, "flight_phase", 160) if value.get("flight_phase") else "unknown"
    result["flight_phase_basis"] = "operator-declared; never inferred from threshold excursions"
    layout = value.get("rotor_layout", "unknown")
    if layout not in {"unknown", "non_overlapping", "coaxial"}:
        raise ValueError("rotor_layout must be unknown, non_overlapping or coaxial.")
    result["rotor_layout"] = layout
    channels = value.get("motor_output_channels", [])
    if not isinstance(channels, list) or len(channels) > 16 or any(type(item) is not int or not 1 <= item <= 16 for item in channels):
        raise ValueError("motor_output_channels must contain up to 16 distinct channel numbers in 1..16.")
    if len(channels) != len(set(channels)):
        raise ValueError("motor_output_channels must not contain duplicates.")
    result["motor_output_channels"] = channels
    protocol = value.get("actuator_protocol", "unknown")
    if protocol not in {"unknown", "pwm"}:
        raise ValueError("Only explicitly calibrated PWM output headroom is supported; other protocols remain unavailable.")
    result["actuator_protocol"] = protocol
    low, high = result["output_low_us"], result["output_high_us"]
    if low is not None and high is not None and low >= high:
        raise ValueError("output_high_us must exceed output_low_us.")
    return result


def reference(event, time_s):
    return {"time_s": time_s, "time_us": str(event["time_us"]) if event.get("time_us") is not None else None,
            "record_index": event.get("record_index"), "byte_offset": event.get("byte_offset"),
            "message_type": event.get("message_type")}


class Trace:
    """Integrate a linear signal and its square only across valid, short pairs."""
    def __init__(self, start, end, gap):
        self.start, self.end, self.gap = start, end, gap
        self.previous = None
        # A data/clock barrier must not make previously covered time eligible
        # again. Receive-clock regressions can remain in the same log stream.
        self.high_water = None
        self.integral = self.square = self.covered = 0.0
        self.count = self.pairs = self.exclusion_count = 0
        self.low = self.high = self.high_ref = self.low_ref = None
        self.exclusions = []

    def barrier(self, reason="corruption_or_clock_boundary"):
        self.previous = None
        self.exclusion_count += 1
        if len(self.exclusions) < 30:
            self.exclusions.append({"reason": reason})

    def add(self, stamp, value, ref):
        if self.high_water is not None and stamp <= self.high_water:
            self.barrier("duplicate_or_nonmonotonic_time")
            return
        self.high_water = stamp
        value = number(value)
        if value is not None and self.start <= stamp <= self.end:
            self.count += 1
            if self.low is None or value < self.low:
                self.low, self.low_ref = value, ref
            if self.high is None or value > self.high:
                self.high, self.high_ref = value, ref
        if self.previous is not None:
            old_t, old_value = self.previous
            if stamp <= old_t:
                self.barrier("duplicate_or_nonmonotonic_time")
                return
            left, right = max(old_t, self.start), min(stamp, self.end)
            if right > left:
                if stamp - old_t > self.gap or old_value is None or value is None:
                    self.exclusion_count += 1
                    if len(self.exclusions) < 30:
                        self.exclusions.append({"start_s": left, "end_s": right,
                                                "reason": "gap_or_missing_units_or_value"})
                else:
                    a = old_value + (value - old_value) * ((left - old_t) / (stamp - old_t))
                    b = old_value + (value - old_value) * ((right - old_t) / (stamp - old_t))
                    self.integral += (a + b) / 2 * (right - left)
                    self.square += (a*a + a*b + b*b) / 3 * (right - left)
                    self.covered += right - left
                    self.pairs += 1
        self.previous = stamp, value

    def metric(self, value, unit, interpretation):
        return {"value": value, "unit": unit, "sample_count": self.count,
                "covered_duration_s": self.covered, "coverage_fraction": self.covered / (self.end - self.start),
                "valid_pair_count": self.pairs, "exclusions": self.exclusions,
                "exclusion_count": self.exclusion_count, "interpretation": interpretation,
                "unavailable_reason": "No qualifying observations/pairs with validated units in the selected interval." if value is None else None}


class VibrationDistribution:
    """Time-weighted threshold exposure plus a bounded uniform sample reservoir."""
    def __init__(self, start, end, gap, warning, critical):
        self.trace = Trace(start, end, gap)
        self.thresholds = {"warning": warning, "critical": critical}
        self.duration = {key: 0.0 for key in self.thresholds}
        self.values = []
        self.random = random.Random(2201)

    def add(self, t, value, ref):
        old = self.trace.previous
        accepted = self.trace.high_water is None or t > self.trace.high_water
        self.trace.add(t, value, ref)
        if not accepted or value is None:
            return
        if len(self.values) < 4096:
            self.values.append(value)
        else:
            slot = self.random.randrange(self.trace.count)
            if slot < len(self.values):
                self.values[slot] = value
        if old is None or old[1] is None or not 0 < t - old[0] <= self.trace.gap:
            return
        a, b = old[1], value
        for key, threshold in self.thresholds.items():
            if min(a, b) > threshold:
                fraction = 1
            elif max(a, b) <= threshold:
                fraction = 0
            else:
                fraction = (max(a, b) - threshold) / abs(b - a)
            self.duration[key] += (t - old[0]) * fraction

    def summary(self):
        return {"sample_count": self.trace.count, "retained_distribution_samples": len(self.values),
                "distribution_approximate": self.trace.count > len(self.values),
                "distribution_method": "Exact sample quantiles up to4096 observations; thereafter deterministic uniform reservoir sample, not time-weighted quantiles.",
                **{f"p{int(q*100)}_mps2": quantile(self.values, q) if self.values else None for q in (.5,.95,.99)},
                "maximum_mps2": self.trace.high, "time_above_warning_s": self.duration["warning"],
                "time_above_critical_s": self.duration["critical"], "covered_duration_s": self.trace.covered,
                "exposure_basis": "Piecewise-linear threshold crossing within valid short pairs only; gaps and unobserved interval tails excluded.",
                "exposure_fraction_of_covered_warning": self.duration["warning"] / self.trace.covered if self.trace.covered else None,
                "exposure_fraction_of_covered_critical": self.duration["critical"] / self.trace.covered if self.trace.covered else None}


def fft(values):
    """Radix-2 forward transform used only on a bounded qualified raw window."""
    n = len(values)
    if n < 2 or n & (n - 1):
        raise ValueError("FFT requires a power-of-two sample count.")
    result = [complex(value) for value in values]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            result[i], result[j] = result[j], result[i]
    size = 2
    while size <= n:
        angle = -2 * math.pi / size
        step = complex(math.cos(angle), math.sin(angle))
        for offset in range(0, n, size):
            weight = 1 + 0j
            for k in range(size // 2):
                a = result[offset + k]
                b = weight * result[offset + k + size // 2]
                result[offset + k], result[offset + k + size // 2] = a + b, a - b
                weight *= step
        size *= 2
    return result


def welch(samples, rate, window):
    weights = [.5 - .5 * math.cos(2 * math.pi * i / (window - 1)) for i in range(window)]
    scale = rate * sum(value * value for value in weights)
    density, segments = [0.0] * (window // 2 + 1), 0
    for offset in range(0, len(samples) - window + 1, window // 2):
        values = samples[offset:offset + window]
        mean = sum(values) / window
        transformed = fft([(value - mean) * weight for value, weight in zip(values, weights)])
        for k in range(len(density)):
            density[k] += abs(transformed[k]) ** 2 / scale * (1 if k in (0, window // 2) else 2)
        segments += 1
    if not segments:
        raise ValueError("Insufficient samples for one complete spectral window.")
    return [value / segments for value in density], segments


class Spectrum:
    def __init__(self, config, *, origin_s=0, start=0, end=MAX_TIME_SECONDS, gap=1):
        self.config = config
        self.origin_s, self.start, self.end, self.gap = origin_s, start, end, gap
        self.samples, self.best = [], []
        self.boundaries = self.observed = 0
        self.clock_exclusions = 0
        self.sample_high_water = None
        self.batch_key = None
        self.last_sample_time = None
        self.capped_samples = 0
        self.reason = "No qualified raw high-rate GYR sample-clock waveform and anti-alias declaration was selected. VIBE summaries cannot supply frequency."
        if config is None:
            return
        if not isinstance(config, dict):
            raise ValueError("spectrum must be an object.")
        self.rate = finite(config.get("sample_rate_hz"), "spectrum.sample_rate_hz")
        self.cutoff = finite(config.get("anti_alias_cutoff_hz"), "spectrum.anti_alias_cutoff_hz")
        if not 100 <= self.rate <= 32000 or not 0 < self.cutoff < self.rate * .45:
            raise ValueError("Spectrum requires rate 100..32000 Hz and declared anti-alias cutoff below 45% of that rate.")
        self.evidence = text_field(config, "anti_alias_evidence", 1000)
        self.instance = text_field(config, "sensor_instance", 32)
        self.source = config.get("source", "raw_gyr")
        self.filter_stage = config.get("filter_stage", "any")
        if self.source not in {"raw_gyr", "batch_imu"} or self.filter_stage not in {"any", "unknown", "pre", "post"}:
            raise ValueError("Spectrum source must be raw_gyr or batch_imu; filter_stage must be any/unknown/pre/post.")
        self.axis = config.get("axis", "x")
        self.window = config.get("window_samples", 1024)
        if self.axis not in ("x", "y", "z") or type(self.window) is not int or self.window not in (256, 512, 1024, 2048):
            raise ValueError("Spectrum axis must be x/y/z and window_samples one of 256,512,1024,2048.")

    def barrier(self, count=True):
        if len(self.samples) > len(self.best):
            self.best = self.samples
        self.samples = []
        self.last_sample_time = None
        if count:
            self.boundaries += 1

    def add(self, event, t):
        if self.config is None or self.source != "raw_gyr" or event.get("message_type") != "GYR" or event.get("source_format") not in ("bin", "log"):
            return
        if self.filter_stage not in {"any", "unknown"}:
            return  # No verified raw logging-stage mapping is available here.
        raw = event.get("raw_fields", {})
        if str(raw.get("I", raw.get("IMU", "unknown"))) != self.instance:
            return
        self.observed += 1
        sample = quantity(event, "SampleUS", {"s"})
        if sample is None:
            micros = quantity(event, "SampleUS", {"us", "µs"})
            sample = micros / 1e6 if micros is not None else None
        value = quantity(event, "Gyr" + self.axis.upper(), {"rad/s", "rad/sec"})
        if sample is None or value is None or event.get("quality_flags"):
            self.barrier()
            return
        relative_sample = sample - self.origin_s
        # DataFlash SampleUS and TimeUS are boot clocks. The measurement, not
        # merely its later log record, must belong to this selected interval.
        # Negative or excessive apparent logging delay is incoherent evidence.
        if not self.start <= relative_sample <= self.end or not -1e-6 <= t - relative_sample <= self.gap:
            self.clock_exclusions += 1
            self.barrier()
            return
        if self.sample_high_water is not None and sample <= self.sample_high_water:
            self.clock_exclusions += 1
            self.barrier()
            return
        self.sample_high_water = sample
        if self.last_sample_time is not None:
            dt = sample - self.last_sample_time
            if dt <= 0 or abs(dt * self.rate - 1) > .01:
                self.barrier()
        if len(self.samples) < 4096:
            self.samples.append((sample, value, reference(event, t)))
        else:
            self.capped_samples += 1
        self.last_sample_time = sample

    def add_batch(self, event):
        if (self.config is None or self.source != "batch_imu" or event.get("sensor_type") != "gyro"
                or event.get("sensor_instance") != self.instance or event.get("sample_unit") != "rad/s"
                or event.get("batch_decode", {}).get("status") != "complete"):
            return
        if self.filter_stage != "any" and event.get("filter_stage") != self.filter_stage:
            return
        rate = event.get("sample_rate_hz")
        key = event.get("batch_id"), event.get("sample_start_us")
        if key != self.batch_key:
            self.barrier()
            self.batch_key = key
        if not isinstance(rate, (int, float)) or abs(rate / self.rate - 1) > .01 or event.get("encoding_endpoint_count", 0):
            self.barrier()
            self.clock_exclusions += 1
            return
        values = event.get("samples", {}).get(self.axis, [])
        for index, value in enumerate(values):
            self.observed += 1
            sample = event["sample_start_us"] / 1e6 + (event["sample_offset"] + index) / rate
            relative = sample - self.origin_s
            if not self.start <= relative <= self.end:
                self.barrier()
                continue
            if self.sample_high_water is not None and sample <= self.sample_high_water:
                self.clock_exclusions += 1
                self.barrier()
                continue
            self.sample_high_water = sample
            if self.last_sample_time is not None and abs((sample - self.last_sample_time) * self.rate - 1) > .01:
                self.barrier()
            block = event["sample_block_references"][index // 32]
            ref = {**reference(event, relative), **block, "measurement_time_s": relative,
                   "batch_id": event["batch_id"], "batch_sample_index": event["sample_offset"] + index,
                   "filter_stage": event.get("filter_stage", "unknown")}
            if len(self.samples) < 4096:
                self.samples.append((sample, value, ref))
            else:
                self.capped_samples += 1
            self.last_sample_time = sample

    def finish(self):
        if self.config is None:
            return {"status": "unavailable", "reason": self.reason, "bins": [], "peaks": [], "citation_ids": ["harmonic_notch"]}
        self.barrier(count=False)
        if len(self.best) < self.window:
            return {"status": "unavailable", "reason": "Insufficient continuous schema-validated raw GYR samples within 1% of the declared sample period.",
                    "observed_records": self.observed, "qualifying_samples": len(self.best), "bins": [], "peaks": [],
                    "clock_exclusions": self.clock_exclusions,
                    "continuity_boundaries": self.boundaries, "citation_ids": ["harmonic_notch"]}
        selected = self.best
        rate = (len(selected) - 1) / (selected[-1][0] - selected[0][0])
        density, segments = welch([row[1] for row in selected], rate, self.window)
        used_samples = self.window + (segments - 1) * (self.window // 2)
        bins = [{"frequency_hz": k * rate / self.window, "psd": power}
                for k, power in enumerate(density) if k * rate / self.window <= self.cutoff]
        peaks = [item for k, item in enumerate(bins) if 0 < k < len(bins) - 1
                 and item["psd"] > bins[k-1]["psd"] and item["psd"] >= bins[k+1]["psd"]]
        offsets = list(range(0, used_samples - self.window + 1, self.window // 2))
        chosen = sorted({round(i * (len(offsets) - 1) / max(1, min(16, len(offsets)) - 1)) for i in range(min(16, len(offsets)))})
        spectrogram = []
        rms = []
        for position in chosen:
            offset = offsets[position]
            waveform = [row[1] for row in selected[offset:offset + self.window]]
            power, _ = welch(waveform, rate, self.window)
            mean = sum(waveform) / len(waveform)
            rms.append(math.sqrt(sum((value - mean)**2 for value in waveform) / len(waveform)))
            spectrogram.append({"center_time_s": (selected[offset][0] + selected[offset + self.window - 1][0]) / 2 - self.origin_s,
                                "psd": power[:len(bins)]})
        mean_rms = sum(rms) / len(rms)
        rms_cv = math.sqrt(sum((value - mean_rms)**2 for value in rms) / len(rms)) / mean_rms if mean_rms else None
        return {"status": "conditional", "method": "Welch, symmetric Hann, per-window mean removed, 50% overlap, one-sided density",
                "source": self.source, "filter_stage": selected[0][2].get("filter_stage", "unknown"),
                "unit": "(rad/s)^2/Hz", "sample_rate_hz": rate, "declared_sample_rate_hz": self.rate,
                "frequency_resolution_hz": rate / self.window, "nyquist_hz": rate / 2,
                "anti_alias_cutoff_hz": self.cutoff, "anti_alias_basis": "operator declaration; attenuation and acquisition chain not independently verified",
                "anti_alias_evidence": self.evidence, "axis": self.axis, "sensor_instance": self.instance,
                "sample_count": used_samples, "continuous_sequence_samples": len(selected), "window_samples": self.window, "averaged_windows": segments,
                "sample_cap": 4096, "scope_start": selected[0][2], "scope_end": selected[used_samples - 1][2],
                "sample_cap_reached": self.capped_samples > 0,
                "sample_clock_start_s": selected[0][0], "sample_clock_end_s": selected[used_samples - 1][0],
                "measurement_clock_field": "ISBH.SampleUS + sample_index / ISBH.smp_rate" if self.source == "batch_imu" else "GYR.SampleUS with schema-validated units",
                "clock_exclusions": self.clock_exclusions,
                "measurement_interval_start_s": selected[0][0] - self.origin_s,
                "measurement_interval_end_s": selected[used_samples - 1][0] - self.origin_s,
                "maximum_sample_period_error_fraction": max(abs((selected[k][0] - selected[k-1][0]) * self.rate - 1) for k in range(1, used_samples)),
                "bins": bins, "peaks": sorted(peaks, key=lambda item: item["psd"], reverse=True)[:5],
                "spectrogram": {"frequencies_hz": [item["frequency_hz"] for item in bins], "windows": spectrogram,
                                "window_limit": 16, "total_windows": len(offsets), "selection": "evenly spaced complete windows; no interpolation"},
                "window_rms_coefficient_of_variation": rms_cv,
                "continuity_boundaries": self.boundaries, "citation_ids": ["harmonic_notch", "gyro_filter"],
                "limitations": ["Only the reported continuous window is analyzed; other periods may have different spectra.",
                                "A declared cutoff does not prove sufficient Nyquist attenuation. Peaks can still contain aliasing or non-motor motion.",
                                "Frequency peaks are diagnostic evidence, not proposed notch parameter values."]}


class InsightService:
    def __init__(self, store):
        self.store, self.logs = store, LogService(store)

    def dispatch(self, method, params, cancel_check=None, progress=None):
        if method != "insights.report":
            raise ValueError("Unsupported insights method.")
        return self.report(params, cancel_check, progress)

    def report(self, params, cancel_check=None, progress=None):
        if not isinstance(params, dict):
            raise ValueError("Insights params must be an object.")
        log = self.store.get_document("log", text_field(params, "log_id"))
        selected = self.logs.select_stream(log, params)
        start, end = finite(params.get("interval_start_s"), "interval_start_s"), finite(params.get("interval_end_s"), "interval_end_s")
        gap = finite(params.get("max_gap_s", 1), "max_gap_s")
        freshness = finite(params.get("parameter_max_age_s", 300), "parameter_max_age_s")
        if not 0 <= start < end <= MAX_TIME_SECONDS or not 0 < gap <= 3600 or not 0 < freshness <= 86400:
            raise ValueError("Require a positive bounded interval, max_gap_s in (0,3600], and parameter_max_age_s in (0,86400].")
        profile = operating_profile(params.get("operating_profile"))
        thresholds = params.get("thresholds", {})
        if not isinstance(thresholds, dict):
            raise ValueError("thresholds must be an object.")
        warning = finite(thresholds.get("vibration_warning_mps2", 30), "vibration_warning_mps2")
        critical = finite(thresholds.get("vibration_critical_mps2", 60), "vibration_critical_mps2")
        roll_threshold = finite(thresholds["roll_error_deg"], "roll_error_deg") if thresholds.get("roll_error_deg") is not None else None
        if not 0 < warning < critical <= 10000 or (roll_threshold is not None and not 0 < roll_threshold <= 180):
            raise ValueError("Require 0 < vibration warning < critical <=10000, and any roll_error_deg in (0,180].")
        batteries, battery = selected.get("battery_ids", []), params.get("battery_id")
        if battery is None and len(batteries) == 1:
            battery = batteries[0]
        if batteries and battery not in batteries:
            raise ValueError("Select one explicit recorded battery monitor; monitors are never aggregated.")
        if not batteries and battery is not None:
            raise ValueError("This stream has no recorded battery monitor.")
        self.logs.verify_artifact(log, cancel_check)
        origin = int(selected["origin_time_us"])
        power, tracking, voltage = (Trace(start, end, gap) for _ in range(3))
        spectrum = Spectrum(params.get("spectrum"), origin_s=origin / 1e6, start=start, end=end, gap=gap)
        control = ControlAnalyzer(params.get("control"), start, end, gap, profile["declared_firmware"], profile["flight_phase"])
        model = filter_model(params["filter_model"]) if params.get("filter_model") is not None else {
            "status": "unavailable", "reason": "No explicit static filter model was selected; aircraft settings are never inferred from a spectrum.", "response": []}
        findings, warnings, changes, current, at_start = [], [], [], {}, {}
        parameter_times = {}
        finding_count = change_count = untimed_parameters = invalid_parameters = 0
        observed_kinds = set()
        barriers = excluded_vibe = observed_vibe = 0
        vibe_buckets, excursions, clip_previous, clip_deltas, clip_resets = {}, {}, {}, {}, []
        vibe_traces, vibe_distributions, clip_high_water, clip_coverage, clip_gaps = {}, {}, {}, {}, []
        vibe_instances = set()
        clip_gap_count = clip_reset_count = 0
        last_power = None
        sag = None
        actuator_min, actuator_peak_ref, actuator_samples = None, None, 0
        actuator_ready = (profile["actuator_protocol"] == "pwm" and profile["motor_output_channels"]
                          and profile["output_low_us"] is not None and profile["output_high_us"] is not None)

        def finding(kind, severity, title, ref, details, citations):
            nonlocal finding_count
            finding_count += 1
            observed_kinds.add(kind)
            if len(findings) < MAX_FINDINGS:
                findings.append({"id": f"finding-{finding_count}", "kind": kind, "severity": severity,
                                 "title": title, "evidence": ref, "details": details, "citation_ids": citations})

        def finish_excursions():
            for key in list(excursions):
                item = excursions.pop(key)
                finding("vibration_excursion", item["severity"], f"Vibration {item['axis']} exceeded the screening threshold",
                        item["peak_evidence"], item, ["vibration"])

        def clipping_gap(counter, reason, ref=None, old_ref=None):
            nonlocal clip_gap_count
            clip_gap_count += 1
            if len(clip_gaps) < 50:
                clip_gaps.append({"counter": counter, "reason": reason, "start": old_ref, "end": ref})

        for index, event in enumerate(self.logs.events(log, selected, cancel_check=cancel_check), 1):
            check_cancel(cancel_check)
            if progress and index % 1024 == 0:
                progress("analyzing_scoped_evidence", index, None)
            stamp = event.get("time_us")
            t = (int(stamp) - origin) / 1e6 if stamp is not None else None
            kind, message = event.get("kind"), event.get("message_type")
            if kind == "diagnostic":
                if event.get("metric_barrier"):
                    barriers += 1
                    for trace in (power, tracking, voltage):
                        trace.barrier()
                    for trace in vibe_traces.values():
                        trace.barrier()
                    spectrum.barrier()
                    control.barrier()
                    finish_excursions()
                    for counter, (_, _, old_ref) in clip_previous.items():
                        clipping_gap(counter, "corruption_or_clock_boundary", old_ref=old_ref)
                    clip_previous.clear()
                    last_power = None
                continue
            if kind == "parameter":
                name, value = str(event.get("name", "")), str(event.get("value", ""))
                try:
                    valid = NAME.fullmatch(name) and Decimal(value).is_finite() and math.isfinite(float(value))
                except (InvalidOperation, ValueError, OverflowError):
                    valid = False
                if not valid:
                    invalid_parameters += 1
                    continue
                if t is None:
                    untimed_parameters += 1
                    continue
                if name in parameter_times and t < parameter_times[name]:
                    invalid_parameters += 1
                    continue
                if name not in current and len(current) >= MAX_PARAMETERS:
                    raise ValueError("Parameter observation set exceeds the supported 50,000 names.")
                parameter_times[name] = t
                previous = current.get(name)
                if t <= start:
                    at_start[name] = {"name": name, "value": value, "age_at_start_s": start - t, "evidence": reference(event, t)}
                elif t < end and (previous is None or Decimal(previous) != Decimal(value)):
                    change_count += 1
                    # A response across mixed observed configuration cannot
                    # establish one isolated controller response window.
                    control.barrier()
                    if len(changes) < 64:
                        changes.append({"name": name, "before": previous, "after": value,
                                        "kind": "newly_observed" if previous is None else "changed", "evidence": reference(event, t)})
                current[name] = value
                continue
            if t is None:
                continue
            if kind == "imu_batch":
                spectrum.add_batch(event)
            control.add(event, t)
            ref = reference(event, t)
            inside = start <= t <= end
            raw = event.get("raw_fields", {})
            if kind == "power" and str(event.get("battery_id", "unknown")) == battery:
                accepted_power_time = power.high_water is None or t > power.high_water
                volts, amps = number(event.get("voltage_v")), number(event.get("current_a"))
                volts = volts if volts is not None and volts >= 0 else None
                power.add(t, volts * amps if volts is not None and amps is not None else None, ref)
                voltage.add(t, volts, ref)
                if inside and accepted_power_time and volts is not None and amps is not None:
                    if last_power is not None:
                        old_t, old_v, old_a, old_ref = last_power
                        if 0 < t - old_t <= gap and amps > old_a and volts < old_v:
                            candidate = {"voltage_drop_v": old_v - volts, "current_rise_a": amps - old_a,
                                         "before_voltage_v": old_v, "after_voltage_v": volts,
                                         "start": old_ref, "end": ref}
                            if sag is None or candidate["voltage_drop_v"] > sag["voltage_drop_v"]:
                                sag = candidate
                    last_power = t, volts, amps, ref
                else:
                    last_power = None
            if kind == "attitude":
                desired, actual = number(event.get("desired_roll_deg")), number(event.get("actual_roll_deg"))
                tracking.add(t, desired - actual if desired is not None and actual is not None else None, ref)
            if not inside:
                continue
            spectrum.add(event, t)
            if message in {"VIBE", "VIBRATION"}:
                observed_vibe += 1
                instance = str(raw.get("IMU", raw.get("I", "reported")))
                if len(vibe_instances) >= 4 and instance not in vibe_instances:
                    excluded_vibe += 1
                    continue
                vibe_instances.add(instance)
                for axis in "xyz":
                    field = "Vibe" + axis.upper() if message == "VIBE" else "vibration_" + axis
                    value = quantity(event, field, {"m/s/s", "m/s^2", "m/s²"})
                    axis_key = instance, axis
                    if axis_key not in vibe_distributions:
                        vibe_distributions[axis_key] = VibrationDistribution(start, end, gap, warning, critical)
                    distribution = vibe_distributions[axis_key]
                    axis_trace = distribution.trace
                    vibe_traces[axis_key] = axis_trace
                    rejected_time = axis_trace.high_water is not None and t <= axis_trace.high_water
                    distribution.add(t, value if value is not None and value >= 0 else None, ref)
                    if rejected_time:
                        value = None
                    if value is None or value < 0:
                        excluded_vibe += 1
                        key = (instance, axis)
                        if key in excursions:
                            item = excursions.pop(key)
                            finding("vibration_excursion", item["severity"], "Vibration excursion ended at a data boundary", item["peak_evidence"], item, ["vibration"])
                        continue
                    bucket = min(MAX_SERIES - 1, int((t - start) / (end - start) * MAX_SERIES))
                    key = instance, axis, bucket
                    point = {"instance_id": instance, "axis": axis, "time_s": t, "value_mps2": value, "record_index": event.get("record_index")}
                    if key not in vibe_buckets or value > vibe_buckets[key]["value_mps2"]:
                        vibe_buckets[key] = point
                    active_key = instance, axis
                    active = excursions.get(active_key)
                    if active and (t <= active["end_s"] or t - active["end_s"] > gap or value <= warning):
                        excursions.pop(active_key)
                        finding("vibration_excursion", active["severity"], f"Vibration {axis} exceeded screening threshold", active["peak_evidence"], active, ["vibration"])
                        active = None
                    if value > warning:
                        if active is None:
                            active = {"instance_id": instance, "axis": axis, "start_s": t, "end_s": t,
                                      "peak_mps2": value, "peak_evidence": ref, "sample_count": 0,
                                      "severity": "critical" if value > critical else "warning",
                                      "time_interpretation": "Observed above-threshold samples only; boundary crossings/intersample peaks unknown."}
                            excursions[active_key] = active
                        active["end_s"], active["sample_count"] = t, active["sample_count"] + 1
                        if value > active["peak_mps2"]:
                            active["peak_mps2"], active["peak_evidence"] = value, ref
                        if value > critical:
                            active["severity"] = "critical"
                clip_fields = [field for field in raw if field in {"Clip", "Clip0", "Clip1", "Clip2", "clipping_0", "clipping_1", "clipping_2"}]
                for counter in list(clip_previous):
                    if counter.startswith(instance + ":") and counter.split(":", 1)[1] not in clip_fields:
                        _, _, old_ref = clip_previous.pop(counter)
                        clipping_gap(counter, "gap_or_missing_units_or_value", ref, old_ref)
                for field in clip_fields:
                    clip_id = f"{instance}:{field}"
                    count = raw[field]
                    if (type(count) is not int or not 0 <= count <= 2 ** 32 - 1
                            or (event.get("source_format") != "tlog" and event.get("field_schema", {}).get(field, {}).get("format_code") != "I")):
                        old = clip_previous.pop(clip_id, None)
                        clipping_gap(clip_id, "gap_or_missing_units_or_value", ref, old[2] if old else None)
                        continue  # Do not relabel arbitrary numeric fields as uint32 counters.
                    coverage = clip_coverage.setdefault(clip_id, {"sample_count": 0, "valid_pair_count": 0, "covered_duration_s": 0.0})
                    coverage["sample_count"] += 1
                    previous = clip_previous.get(clip_id)
                    if clip_id in clip_high_water and t <= clip_high_water[clip_id]:
                        clip_previous.pop(clip_id, None)
                        clipping_gap(clip_id, "duplicate_or_nonmonotonic_time", ref)
                        continue
                    clip_high_water[clip_id] = t
                    if previous is not None:
                        old_t, old_count, old_ref = previous
                        if t <= old_t or t - old_t > gap:
                            clipping_gap(clip_id, "gap_or_missing_units_or_value", ref, old_ref)
                        elif count < old_count:
                            clip_reset_count += 1
                            reset = {"counter": clip_id, "before": old_count, "after": count, "evidence": ref}
                            if len(clip_resets) < 50:
                                clip_resets.append(reset)
                            finding("clipping_counter_reset", "warning", "Clipping counter decreased; reset/wrap is unclassified", ref, reset, ["clipping"])
                        else:
                            coverage["valid_pair_count"] += 1
                            coverage["covered_duration_s"] += t - old_t
                            if count > old_count:
                                delta = count - old_count
                                clip_deltas[clip_id] = clip_deltas.get(clip_id, 0) + delta
                                finding("accelerometer_clipping", "warning", "Accelerometer clipping counter increased", ref,
                                        {"counter": clip_id, "delta": delta, "start": old_ref, "end": ref,
                                         "interpretation": "Events occurred between these samples; exact event times are unavailable."}, ["clipping"])
                    clip_previous[clip_id] = t, count, ref
            if actuator_ready and message in {"RCOU", "SERVO_OUTPUT_RAW"}:
                if message == "SERVO_OUTPUT_RAW" and raw.get("port") != 0:
                    continue
                for channel in profile["motor_output_channels"]:
                    field = f"C{channel}" if message == "RCOU" else f"servo{channel}_raw"
                    value = quantity(event, field, {"us", "µs"})
                    if value is None:
                        continue
                    low, high = profile["output_low_us"], profile["output_high_us"]
                    if not low <= value <= high:
                        finding("actuator_calibration_mismatch", "warning", "PWM output lies outside declared calibration", ref,
                                {"channel": channel, "output_us": value, "declared_low_us": low, "declared_high_us": high}, ["actuator_limits"])
                        continue
                    actuator_samples += 1
                    margin = (high - value) / (high - low)
                    if actuator_min is None or margin < actuator_min:
                        actuator_min, actuator_peak_ref = margin, {**ref, "channel": channel, "output_us": value}
        finish_excursions()
        check_cancel(cancel_check)
        stale = sum(item["age_at_start_s"] > freshness for item in at_start.values())
        parameter_state = ("mixed" if change_count else "missing" if not at_start else "stale" if stale
                           else "uncertain" if barriers or untimed_parameters or invalid_parameters else "observed_only")
        relevant = [item for name, item in sorted(at_start.items()) if name.startswith(PARAMETER_PREFIXES)]
        parameter_evidence = {"state": parameter_state, "completeness": "observed_only", "attribution_allowed": False,
                              "observations_eligible_for_review": parameter_state == "observed_only",
                              "attribution_note": "Partial observations never establish the complete effective configuration or causal setting effects; eligible observations support descriptive review only.",
                              "observed_at_start_count": len(at_start), "stale_count": stale, "max_age_s": freshness,
                              "observations": relevant[:128], "observations_truncated": len(relevant) > 128,
                              "changes": changes, "change_count": change_count, "changes_truncated": change_count > len(changes),
                              "untimed_count": untimed_parameters, "invalid_count": invalid_parameters}
        snapshot_id = params.get("parameter_snapshot_id")
        if snapshot_id is not None:
            snapshot = self.store.get_document("snapshot", text_field(params, "parameter_snapshot_id"))
            loaded = snapshot.get("parameters", {})
            if not isinstance(loaded, dict) or len(loaded) > MAX_PARAMETERS:
                raise ValueError("Loaded parameter snapshot exceeds supported bounds.")
            differences, compared, different = [], 0, 0
            for name, observed in at_start.items():
                if name not in loaded:
                    continue
                value = loaded[name].get("value") if isinstance(loaded[name], dict) else loaded[name]
                try:
                    a, b = Decimal(str(value)), Decimal(observed["value"])
                except InvalidOperation:
                    continue
                if not a.is_finite() or not b.is_finite():
                    continue
                compared += 1
                if a != b:
                    different += 1
                    if len(differences) < 128:
                        differences.append({"name": name, "loaded_value": str(value), "observed_value": observed["value"], "evidence": observed["evidence"]})
            parameter_evidence["loaded_snapshot"] = {"id": snapshot["id"], "compared_count": compared,
                "different_count": different, "differences": differences, "differences_truncated": different > len(differences),
                "loaded_not_observed_count": len(set(loaded) - set(at_start)),
                "source_sha256": snapshot.get("source", {}).get("sha256"),
                "comparison_basis": "Numeric representation differences with unknown serialization precision; not proof that effective aircraft settings changed.",
                "applicability": "unverified: a loaded file is not proof of settings active during this flight interval"}
        metrics = {
            "mean_power_w": power.metric(power.integral / power.covered if power.covered else None, "W", "Mean over qualifying covered pairs only; sampled power is piecewise linear."),
            "energy_wh": power.metric(power.integral / 3600 if power.covered else None, "Wh", "Signed energy over qualifying pairs only; gaps are not filled."),
            "peak_power_w": power.metric(power.high, "W", "Largest observed sample; intersample peaks are unknown."),
            "roll_tracking_rmse_deg": tracking.metric(math.sqrt(tracking.square / tracking.covered) if tracking.covered else None, "deg", "Exact integral of piecewise-linear roll error squared; not a validated PID step-response metric."),
            "minimum_pack_voltage_v": voltage.metric(voltage.low, "V", "Lowest validated observed pack voltage, not the weakest cell voltage."),
            "command_headroom_fraction": {"value": actuator_min, "unit": "fraction", "sample_count": actuator_samples,
                                          "evidence": actuator_peak_ref, "basis": "operator-declared PWM mapping and endpoints",
                                          "unavailable_reason": None if actuator_min is not None else "Requires declared PWM protocol, motor channels/endpoints, and schema-validated PWM samples.",
                                          "interpretation": "Margin to declared command upper endpoint; not thrust, torque, power or safety reserve."},
        }
        if sag:
            finding("battery_load_step_drop", "review", "Pack voltage fell while observed load current rose", sag["end"], sag, ["heavy_lift"])
        peak_error = max(abs(tracking.low or 0), abs(tracking.high or 0)) if tracking.count else None
        if roll_threshold is not None and peak_error is not None and peak_error > roll_threshold:
            ref = tracking.low_ref if abs(tracking.low) > abs(tracking.high) else tracking.high_ref
            finding("roll_tracking_error", "review", "Roll tracking error exceeded the declared review threshold", ref,
                    {"observed_peak_absolute_error_deg": peak_error, "operator_threshold_deg": roll_threshold}, ["tracking", "rate_limits"])
        if actuator_min is not None and actuator_min <= .05:
            finding("actuator_command_near_upper_endpoint", "review", "Observed PWM command was within 5% of its declared upper endpoint", actuator_peak_ref,
                    {"headroom_fraction": actuator_min, "interpretation": "Descriptive screening only; motor thrust saturation is not established."}, ["actuator_limits"])
        mass, payload = profile["aircraft_mass_kg"], profile["payload_mass_kg"]
        count, diameter = profile["rotor_count"], profile["propeller_diameter_m"]
        area = count * math.pi * (diameter / 2) ** 2 if count and diameter else None
        profile["derived_context"] = {
            "payload_mass_fraction": payload / mass if payload is not None and mass else None,
            "summed_rotor_disk_area_m2": area,
            "disk_loading_n_m2": mass * 9.80665 / area if mass and area and profile["rotor_layout"] == "non_overlapping" else None,
            "disk_loading_note": "Requires declared non-overlapping rotor disks; coaxial/unknown geometry is not assigned an effective disk area.",
            "mean_electrical_power_w_per_kg": metrics["mean_power_w"]["value"] / mass if mass and metrics["mean_power_w"]["value"] is not None else None,
            "minimum_mean_cell_voltage_v": voltage.low / profile["battery_cell_count"] if voltage.low is not None and profile["battery_cell_count"] else None,
            "cell_voltage_note": "Pack voltage divided by declared series count, not individual cell telemetry or balance evidence.",
            "endurance_estimate_minutes": None,
            "endurance_unavailable_reason": "Declared Ah alone does not establish usable energy, flight phase, future load or reserve."}
        registry = guidance.registry(profile["declared_firmware"])
        recommendations = []
        kinds = observed_kinds
        action_map = [
            (bool(kinds & {"vibration_excursion", "accelerometer_clipping"}), "inspect_vibration", "Inspect the timestamped vibration/clipping intervals and mechanical installation before tuning filters.", ["vibration", "clipping", "harmonic_notch"], ["INS_HNTCH_", "INS_GYRO_FILTER"]),
            ("roll_tracking_error" in kinds, "review_tracking", "Compare desired/actual motion with output limits, flight phase and applicable rate limits before reviewing gains.", ["tracking", "rate_limits"], ["ATC_RAT_", "ATC_ACC_", "ATC_ACCEL_"]),
            (bool(kinds & {"actuator_command_near_upper_endpoint", "battery_load_step_drop"}) or payload is not None, "review_load", "Review declared all-up mass, propulsion capability, battery condition and logged output/current demand together; payload does not determine PID values.", ["heavy_lift", "actuator_limits"], ["MOT_", "BATT_", "ATC_"]),
            (parameter_state != "observed_only", "restore_parameter_evidence", "Choose an interval with timely parameter observations and split at observed changes before attributing performance to settings.", ["tracking"], []),
        ]
        for enabled, identifier, action, citations, families in action_map:
            if enabled:
                recommendations.append({"id": identifier, "action": action, "citation_ids": citations,
                                        "parameter_families": families, "proposed_values": None,
                                        "parameter_attribution_state": parameter_state,
                                        "evidence_ids": [item["id"] for item in findings][:20],
                                        "requires_operator_review": True,
                                        "firmware_caveat": "Parameter names/units vary by firmware; declared label is not exact-build verification."})
        if barriers:
            warnings.append("Corruption/clock boundaries interrupt integration, clipping deltas and spectral windows.")
        if excluded_vibe:
            warnings.append("Some vibration fields lacked validated units/values or exceeded the instance limit; they were excluded.")
        if selected.get("time_domain") == "utc_receive_us":
            warnings.append("Times describe ground reception, not synchronized sensor measurement time.")
        if profile["flight_phase"] == "unknown":
            warnings.append("Selected interval, flight status unknown: excursions are not classified as airborne, ground operation or landing impacts.")
        scope = {"log_id": log["id"], "source_sha256": log["source"]["sha256"], "synthetic": log["synthetic"],
                 "source_key": selected["source_key"], "boot_epoch": selected["boot_epoch"],
                 "time_domain": selected["time_domain"], "origin_time_us": selected["origin_time_us"],
                 "interval_start_s": start, "interval_end_s": end, "max_gap_s": gap, "battery_id": battery}
        scope["flight_phase"] = profile["flight_phase"]
        spectrum_result = spectrum.finish()
        modeled_spectrum = {"status": "unavailable", "reason": "Requires a qualified spectrum and explicit static model at the same sample rate."}
        if (spectrum_result.get("status") == "conditional" and model.get("status") == "modeled_static"
                and abs(spectrum_result["sample_rate_hz"] / model["sample_rate_hz"] - 1) <= 1e-6):
            bins = spectrum_result["bins"]
            response = filter_model({**params["filter_model"], "frequencies_hz": [row["frequency_hz"] for row in bins]})["response"]
            transformed = [{"frequency_hz": row["frequency_hz"], "observed_psd": row["psd"],
                            "modeled_psd": row["psd"] * gain["gain"]**2 if gain["gain"] is not None else None}
                           for row, gain in zip(bins, response)]
            before = sum(row["observed_psd"] for row in transformed) * spectrum_result["frequency_resolution_hz"]
            after = (sum(row["modeled_psd"] for row in transformed) * spectrum_result["frequency_resolution_hz"]
                     if all(row["modeled_psd"] is not None for row in transformed) else None)
            modeled_spectrum = {"status": "hypothetical_static_application", "bins": transformed,
                "observed_band_power": before, "modeled_band_power": after, "band_power_unit": "(rad/s)^2",
                "band_attenuation_db": 10*math.log10(before/after) if before > 0 and after is not None and after > 0 else None,
                "band_start_hz": bins[0]["frequency_hz"], "band_end_hz": bins[-1]["frequency_hz"],
                "interpretation": "Additional hypothetical static filtering of the logged waveform, PSD multiplied by |H|². Not a measured pre/post comparison or prediction of the aircraft's effective pipeline."}
        report = {"algorithm_version": VERSION, "scope": scope, "operating_profile": profile, "metrics": metrics,
                  "control": control.finish(bool(observed_kinds & {"accelerometer_clipping", "vibration_excursion", "clipping_counter_reset"})),
                  "filter_model": model,
                  "modeled_spectrum": modeled_spectrum,
                  "vibration": {"status": "available" if vibe_buckets else "unavailable", "series": sorted(vibe_buckets.values(), key=lambda item: (item["time_s"], item["instance_id"], item["axis"])),
                                "series_reduction": "Maximum per axis/instance in each of 256 interval buckets; no interpolation or FFT.",
                                "warning_threshold_mps2": warning, "critical_threshold_mps2": critical,
                                "threshold_basis": "ArduPilot guidance defaults" if warning == 30 and critical == 60 else "operator_declared",
                                "excursions": [item for item in findings if item["kind"] == "vibration_excursion"],
                                "coverage": [{"instance_id": key[0], "axis": key[1], "sample_count": trace.count,
                                              "covered_duration_s": trace.covered, "coverage_fraction": trace.covered / (end - start),
                                              "valid_pair_count": trace.pairs, "exclusions": trace.exclusions,
                                              "exclusion_count": trace.exclusion_count} for key, trace in sorted(vibe_traces.items())],
                                "distributions": [{"instance_id": key[0], "axis": key[1], **value.summary()} for key, value in sorted(vibe_distributions.items())],
                                "clipping": {"observed_deltas": clip_deltas, "resets": clip_resets, "gap_count": clip_gap_count,
                                             "reset_count": clip_reset_count, "resets_truncated": clip_reset_count > len(clip_resets),
                                             "gaps": clip_gaps, "gaps_truncated": clip_gap_count > len(clip_gaps),
                                             "coverage": [{"counter": key, **value, "coverage_fraction": value["covered_duration_s"] / (end - start),
                                                           "observed_delta": clip_deltas.get(key, 0) if value["valid_pair_count"] else None,
                                                           "observed_rate_per_s": clip_deltas.get(key, 0) / value["covered_duration_s"] if value["covered_duration_s"] else None}
                                                          for key, value in sorted(clip_coverage.items())],
                                             "interpretation": "Only monotonic counter deltas across short, uninterrupted sampled pairs are counted; initial absolute counts are not this interval's event count."},
                                "observed_records": observed_vibe, "excluded_fields": excluded_vibe},
                  "parameter_evidence": parameter_evidence, "findings": findings, "finding_count": finding_count,
                  "findings_truncated": finding_count > len(findings), "recommendations": recommendations,
                  "spectrum": spectrum_result, "guidance": registry, "warnings": warnings, "limitations": LIMITATIONS,
                  "evidence_features": {"source_sha256": log["source"]["sha256"], "sample_counts": {key: item.get("sample_count", 0) for key, item in metrics.items()},
                                        "barrier_count": barriers, "parameter_state": parameter_state,
                                        "vibration_records": observed_vibe, "clipping_observed_deltas": clip_deltas,
                                        "observed_peak_absolute_roll_error_deg": peak_error, "load_step_voltage_drop": sag}}
        return {"report": report}
