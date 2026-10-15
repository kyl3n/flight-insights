"""Bounded descriptive health evidence from schema-qualified DataFlash records.

No network, vehicle commands, guessed units, universal airworthiness thresholds,
or replacement of missing channels with zero. Callers select one source/clock
interval. Raw flags and documented sample ratios are observations, not a fault
cause or a complete firmware failsafe evaluation.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math

VERSION = "q22-flight-health/1"
REVISION = "dbe792162d06cab66c3475fd5556bf7a120f119e"
MAX_CHANNELS = 64
MAX_ESC_FRAMES = 256
MAX_FINDINGS = 32
DOMAINS = {"gnss": "GNSS observations", "estimator": "Estimator observations",
           "esc": "ESC observations", "scheduler": "Scheduler observations",
           "power_rails": "Board power and temperature"}
SOURCE_FILES = {
    "gps": ("libraries/AP_GPS/LogStructure.h", "c9dab1c5ada8f9492158168f9636063798f7e86ddd2c2b7ed00afe8570ba1f6c"),
    "gps_status": ("libraries/AP_GPS/AP_GPS.h", "636ca87c14ba80871e50b99601a1007cf2aecb0fbe4a7a027f227b6b27117761"),
    "ekf": ("libraries/AP_NavEKF3/LogStructure.h", "3dae18f4d47072c63731ecc752fc75047b9c7caf167f1de30ece143849163674"),
    "ekf_writer": ("libraries/AP_NavEKF3/AP_NavEKF3_Logging.cpp", "be0f332667fe52c063b96480a7d0692170bef2d31b8912a3273a112ed5c09dd9"),
    "esc": ("libraries/AP_ESC_Telem/LogStructure.h", "3e2ab40412c890abcc887d76bd8833d6eec1807f9974a30574ebb1007d13cc64"),
    "system": ("libraries/AP_Logger/LogStructure.h", "73cb573d9dfebb8f0ed6c4a1edcddd7aa31caf60661207f1f4ee408d5c50aabc"),
    "scheduler": ("libraries/AP_Scheduler/AP_Scheduler.cpp", "333110f89d6f5d22849303282a943b4ae71b4659928273f6a01746509bac314f"),
}
DOMAIN_LIMITS = {
    "gnss": ["Fix type, satellite count and dilution are receiver observations, not position integrity or an interference diagnosis.",
             "Receiver selection and estimator use are separate evidence; sample counts are not time-in-state durations."],
    "estimator": ["XKF3/XKF4 observations remain separated by EKF3 core. Other EKF layouts are not silently interpreted as this schema.",
                  "Logged innovation indicators are not raw covariance. Crossing 1 is not a complete EKF failsafe evaluation; configuration, filtering and timer logic remain unverified."],
    "esc": ["ESC instance numbers are not a verified physical motor/output map. Only observed instances are known; absent channels remain unknown.",
            "A logged ESC record does not establish per-field measurement freshness. Zero values are withheld because unsupported telemetry can look like zero; this also withholds a genuine stopped/zero-current observation without capability evidence.",
            "Spreads use exact shared boot timestamps and all observed instances within a bounded retained window. They describe logged values, not simultaneous fresh sensor samples or a proven motor fault."],
    "scheduler": ["Long-loop counts describe each recorded PM measurement window, not a lifetime counter or a proven lost control update.",
                  "Load requires recorded percent units and scaling. No universal acceptable load or loop-duration limit is assumed."],
    "power_rails": ["Rail readings require recorded voltage units; board topology and acceptable operating limits remain aircraft-specific.",
                    "A sampled voltage minimum or an abrupt log ending does not prove a brownout; unlogged transients remain unknown."],
}


def _number(value):
    if type(value) not in (int, float):
        return None
    # Text decoders can preserve arbitrarily large integers; bound them before
    # conversion inside isfinite would overflow.
    return float(value) if abs(value) <= 1e12 and math.isfinite(value) else None


def _descriptor(event, field):
    descriptor = event.get("field_schema", {}).get(field, {})
    return descriptor if descriptor.get("source_field") == f"{event.get('message_type')}.{field}" else {}


def _integer(event, field, code, maximum):
    value = event.get("raw_fields", {}).get(field)
    descriptor = _descriptor(event, field)
    if type(value) is not int or not 0 <= value <= maximum or descriptor.get("format_code") != code:
        return None
    if descriptor.get("unit") not in (None, "", "-", "1", "instance", "satellites"):
        return None
    # Enumerations/counts use the documented integer wire field, not a guessed
    # physical unit. A contradictory explicit multiplier must not be ignored.
    if descriptor.get("applied_multiplier") not in (None, 1, 1.0):
        return None
    return value


def _quantity(event, field, units, minimum=None, maximum=None):
    descriptor = _descriptor(event, field)
    value = _number(event.get("raw_fields", {}).get(field))
    multiplier = _number(descriptor.get("applied_multiplier"))
    if (value is None or multiplier is None or multiplier <= 0
            or descriptor.get("unit") not in units):
        return None
    value = _number(value * multiplier)
    if value is None or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return None
    return value


def _dimensionless_centi(event, field, code="c"):
    descriptor = _descriptor(event, field)
    expected = .01 if event.get("source_format") == "bin" else 1.0
    if (descriptor.get("format_code") != code or descriptor.get("unit_id") != "-"
            or descriptor.get("unit") not in (None, "", "1", "-")
            or descriptor.get("applied_multiplier") != expected):
        return None
    value = _number(event.get("raw_fields", {}).get(field))
    return value * expected if value is not None and value >= 0 else None


def _reference(event, time_s):
    return {"time_s": time_s, "record_index": event.get("record_index"),
            "byte_offset": event.get("byte_offset"), "message_type": event.get("message_type"),
            "source_format": event.get("source_format"), "boot_epoch": event.get("boot_epoch"),
            "time_domain": event.get("time_domain")}


class _Summary:
    def __init__(self, identifier, label, unit, gap):
        self.identifier, self.label, self.unit, self.gap = identifier, label, unit, gap
        self.count = self.excluded = self.gaps = 0
        self.minimum = self.maximum = self.mean = None
        self.first = self.last = self.minimum_ref = self.maximum_ref = None
        self.epoch = None
        self.codes = Counter()
        self.unlisted_codes = 0

    def add(self, value, time_s, reference, epoch):
        if value is None or (self.last is not None and time_s <= self.last):
            self.excluded += 1
            return False
        if self.last is not None and (time_s - self.last > self.gap or epoch != self.epoch):
            self.gaps += 1
        self.count += 1
        self.mean = value if self.mean is None else self.mean + (value - self.mean) / self.count
        if self.unit in {"enum", "bitmask"}:
            if str(value) in self.codes or len(self.codes) < 32:
                self.codes[str(value)] += 1
            else:
                self.unlisted_codes += 1
        if self.minimum is None or value < self.minimum:
            self.minimum, self.minimum_ref = value, reference
        if self.maximum is None or value > self.maximum:
            self.maximum, self.maximum_ref = value, reference
        if self.first is None:
            self.first = time_s
        self.last, self.epoch = time_s, epoch
        return True

    def finish(self):
        return {"id": self.identifier, "label": self.label, "unit": self.unit,
                "status": "observed" if self.count else "unavailable", "sample_count": self.count,
                "minimum": self.minimum, "maximum": self.maximum,
                "mean": None if self.unit in {"enum", "bitmask"} else self.mean,
                "first_time_s": self.first, "last_time_s": self.last, "gap_count": self.gaps,
                "excluded_count": self.excluded, "minimum_evidence": self.minimum_ref,
                "maximum_evidence": self.maximum_ref,
                "observed_codes": dict(self.codes), "unlisted_code_samples": self.unlisted_codes,
                "statistics_basis": "Raw recorded codes; numeric ordering and arithmetic average have no health interpretation." if self.unit in {"enum", "bitmask"}
                    else "Unweighted valid recorded samples; not duration-weighted or a flight-level confidence interval."}


class FlightHealthAnalyzer:
    def __init__(self, start, end, max_gap_s=1.0, firmware=None):
        if any(_number(value) is None for value in (start, end, max_gap_s)) or not 0 <= start < end or max_gap_s <= 0:
            raise ValueError("Health analysis requires a finite increasing interval and positive maximum gap.")
        self.start, self.end, self.gap, self.firmware = start, end, max_gap_s, firmware
        self.channels, self.findings = {}, {}
        self.exclusions = Counter()
        self.barriers = self.epoch = 0
        self.source = None
        self.esc_instances, self.esc_frames = set(), []
        self.esc_frame = None
        self.esc_frames_seen = 0

    def barrier(self, reason="corruption_or_clock_boundary"):
        self.epoch += 1
        self.barriers += 1
        self._flush_esc()

    def _channel(self, domain, message, instance):
        key = (domain, message, str(instance))
        if key not in self.channels:
            if len(self.channels) >= MAX_CHANNELS:
                self.exclusions["channel_limit"] += 1
                return None
            self.channels[key] = {"metrics": {}, "observations": Counter()}
        return self.channels[key]

    def _metric(self, channel, identifier, label, unit, value, time_s, ref):
        if identifier not in channel["metrics"]:
            channel["metrics"][identifier] = _Summary(identifier, label, unit, self.gap)
        return channel["metrics"][identifier].add(value, time_s, ref, self.epoch)

    def _finding(self, identifier, domain, title, explanation, ref, checks, families, refs, severity="review"):
        if identifier in self.findings:
            self.findings[identifier]["observation_count"] += 1
            return
        if len(self.findings) >= MAX_FINDINGS:
            self.exclusions["finding_limit"] += 1
            return
        self.findings[identifier] = {"id": identifier, "domain": domain, "severity": severity,
            "title": title, "explanation": explanation, "observation_count": 1,
            "evidence": [ref], "recommended_checks": checks, "parameter_families": families,
            "reference_ids": refs, "numeric_tuning_proposed": False}

    def _flush_esc(self):
        if self.esc_frame is not None:
            self.esc_frames_seen += 1
            if len(self.esc_frames) < MAX_ESC_FRAMES:
                self.esc_frames.append(self.esc_frame)
            self.esc_frame = None

    def observe(self, event, time_s):
        if event.get("kind") == "diagnostic":
            if event.get("metric_barrier"):
                self.barrier()
            return
        message = event.get("message_type")
        if message not in {"GPS", "GPA", "XKF3", "XKF4", "ESC", "PM", "POWR", "MCU"}:
            return
        if _number(time_s) is None or not self.start <= time_s <= self.end:
            return
        if event.get("source_format") not in {"bin", "log"} or event.get("time_domain") != "flight_controller_boot":
            self.exclusions["unsupported_schema_or_time_domain"] += 1
            return
        source = (event.get("source_format"), event.get("system_id"), event.get("component_id"),
                  event.get("capture_link_id"), event.get("boot_epoch"))
        if self.source is None:
            self.source = source
        if source != self.source:
            self.exclusions["different_source_or_clock_segment"] += 1
            return
        if any(flag in {"boot_time_discontinuity_unclassified", "receive_clock_regression"} for flag in event.get("quality_flags", [])):
            self.barrier()
        ref = _reference(event, time_s)
        if message in {"GPS", "GPA"}:
            instance = _integer(event, "I", "B", 255)
            domain = "gnss"
        elif message in {"XKF3", "XKF4"}:
            instance = _integer(event, "C", "B", 255)
            domain = "estimator"
        elif message == "ESC":
            instance = _integer(event, "Instance", "B", 255)
            domain = "esc"
        else:
            instance, domain = "board", "scheduler" if message == "PM" else "power_rails"
        if instance is None:
            self.exclusions["missing_instance_or_schema"] += 1
            return
        if message == "ESC":
            # Keep the complete observed identity set even when metric-channel
            # storage is full, so a partial subset cannot become a full spread.
            # The validated byte-valued identity bounds this set to 256 entries.
            self.esc_instances.add(str(instance))
        channel = self._channel(domain, message, instance)
        if channel is None:
            return
        metric = lambda identifier, label, unit, value: self._metric(channel, identifier, label, unit, value, time_s, ref)
        if message == "GPS":
            fix = _integer(event, "Status", "B", 8)
            if metric("fix_type", "Reported fix type", "enum", fix):
                channel["observations"][f"fix_type_{fix}"] += 1
                if fix < 3:
                    self._finding(f"gnss-fix-{instance}", domain, f"GNSS {instance}: observations without a 3D fix",
                        "The recorded receiver fix code was below 3. This does not identify the receiver used for navigation or prove a position-control fault.", ref,
                        ["Check receiver selection, antenna installation, sky view and the matching estimator interval.",
                         "Review satellite count, dilution and reported accuracy without replacing unknown data with zero."],
                        ["GPS_", "EK3_SRC"], ["gps", "gps_status"])
            metric("satellites", "Visible satellites", "satellites", _integer(event, "NSats", "B", 255))
            hdop = _dimensionless_centi(event, "HDop")
            metric("hdop", "Horizontal dilution of precision", "dimensionless", hdop if hdop and hdop > 0 else None)
        elif message == "GPA":
            for field, identifier, label, units, unit in (
                    ("HAcc", "horizontal_accuracy_m", "Reported horizontal accuracy", {"m"}, "m"),
                    ("VAcc", "vertical_accuracy_m", "Reported vertical accuracy", {"m"}, "m"),
                    ("SAcc", "speed_accuracy_m_s", "Reported speed accuracy", {"m/s"}, "m/s")):
                value = _quantity(event, field, units, 0)
                metric(identifier, label, unit, value if value is not None and value > 0 else None)
        elif message == "XKF3":
            for field in ("IVN", "IVE", "IVD", "IPN", "IPE", "IPD"):
                unit = "m/s" if field.startswith("IV") else "m"
                metric(field.lower(), f"Recorded {field} innovation", unit, _quantity(event, field, {unit}))
        elif message == "XKF4":
            for field in ("SV", "SP", "SH", "SM", "SVT"):
                value = _dimensionless_centi(event, field)
                if metric(field.lower(), f"Logged {field} consistency indicator", "dimensionless", value) and value > 1:
                    self._finding(f"ekf-indicator-{instance}-{field}", domain, f"EKF3 core {instance}: {field} exceeded 1",
                        "A schema-qualified logged innovation consistency indicator exceeded the pinned source's unit boundary. It is not raw covariance or proof that the configured EKF failsafe activated.", ref,
                        ["Inspect the matching innovations, aiding source, vibration, sensor changes and reset evidence.",
                         "Verify exact firmware and EKF gate/failsafe parameters before interpreting activation."],
                        ["EK3_", "FS_EKF_THRESH"], ["ekf", "ekf_writer"])
            for field, code, maximum in (("FS", "H", 65535), ("TS", "B", 255), ("SS", "I", 4294967295), ("GPS", "H", 65535)):
                value = _integer(event, field, code, maximum)
                if metric(field.lower()+"_flags", f"Recorded {field} bitmask", "bitmask", value) and value and field in {"FS", "TS"}:
                    self._finding(f"ekf-flags-{instance}-{field}", domain, f"EKF3 core {instance}: nonzero {field} flags",
                        "A recorded filter fault/timeout bitmask was nonzero. The report retains the raw mask; it does not infer a unique sensor failure or decode another firmware's bits.", ref,
                        ["Inspect the exact mask against the controller firmware and nearby innovations/aiding transitions."],
                        ["EK3_"], ["ekf"])
        elif message == "ESC":
            accepted = {}
            for field, identifier, label, units, unit, minimum in (
                    ("RPM", "rpm", "Reported motor speed", {"rpm"}, "rpm", 0),
                    ("Volt", "voltage_v", "ESC input voltage", {"V", "v", "volt", "volts"}, "V", 0),
                    ("Curr", "current_a", "ESC current", {"A", "amp", "amps"}, "A", None),
                    ("Temp", "temperature_c", "ESC temperature", {"degC", "C", "°C"}, "degC", -273.15),
                    ("MotTemp", "motor_temperature_c", "Motor temperature", {"degC", "C", "°C"}, "degC", -273.15)):
                value = _quantity(event, field, units, minimum)
                if value == 0:
                    channel["observations"][f"ambiguous_zero_{field}"] += 1
                    value = None
                if metric(identifier, label, unit, value):
                    accepted[identifier] = value
            stamp = event.get("boot_time_us")
            if type(stamp) is int and stamp >= 0:
                key = (self.epoch, stamp)
                if self.esc_frame is not None and self.esc_frame["key"] != key:
                    self._flush_esc()
                if self.esc_frame is None:
                    self.esc_frame = {"key": key, "time_s": time_s, "instances": {}, "invalid": False, "evidence": ref}
                if str(instance) in self.esc_frame["instances"] or time_s != self.esc_frame["time_s"]:
                    self.esc_frame["invalid"] = True
                self.esc_frame["instances"][str(instance)] = accepted
        elif message == "PM":
            long = _integer(event, "NLon", "H", 65535)
            loops = _integer(event, "NL", "H", 65535)
            valid = long is not None and loops is not None and 0 <= long <= loops and loops > 0
            if metric("long_loop_fraction", "Long loops / loops in recorded window", "fraction", long / loops if valid else None) and long:
                self._finding("scheduler-long-loops", domain, "Recorded scheduler windows contain long loops",
                    "PM reports long-running loops in its measurement window. The ratio describes that logged window; it does not by itself prove a control deadline failure or identify CPU load's cause.", ref,
                    ["Compare loop rate, logging/filter workload, firmware and the same flight phase.",
                     "Confirm sensor/control logging requirements before changing rates or filters."],
                    ["SCHED_", "LOG_", "INS_HNTCH_", "INS_HNTC2_", "FFT_"], ["system", "scheduler"])
            metric("load_percent", "Reported processor load", "%", _quantity(event, "Load", {"%"}, 0, 100))
            metric("loop_rate_hz", "Reported loop rate", "Hz", _quantity(event, "LR", {"Hz"}, 0))
            metric("maximum_loop_us", "Maximum loop time", "us", _quantity(event, "MaxT", {"us", "µs"}, 0))
            metric("free_memory_bytes", "Reported free memory", "B", _quantity(event, "Mem", {"B", "bytes"}, 0))
        else:
            for field, label in (("Vcc", "Board rail voltage"), ("VServo", "Servo rail voltage")) if message == "POWR" else (
                    ("MVolt", "MCU voltage"), ("MVmin", "MCU sampled minimum voltage"), ("MVmax", "MCU sampled maximum voltage")):
                metric(field.lower()+"_v", label, "V", _quantity(event, field, {"V", "v", "volt", "volts"}, 0))
            if message == "MCU":
                metric("temperature_c", "MCU temperature", "degC", _quantity(event, "MTemp", {"degC", "C", "°C"}, -273.15))
            else:
                for field in ("Flags", "AccFlags"):
                    metric(field.lower(), f"Recorded {field} power bitmask", "bitmask", _integer(event, field, "H", 65535))

    def finish(self):
        # Work on the buffered final frame without changing state: repeated
        # finish() calls must produce the same report.
        frames = self.esc_frames + ([self.esc_frame] if self.esc_frame is not None and len(self.esc_frames) < MAX_ESC_FRAMES else [])
        spreads = []
        if len(self.esc_instances) > 1:
            for identifier, unit in (("rpm", "rpm"), ("current_a", "A"), ("temperature_c", "degC")):
                summary = _Summary(identifier+"_spread", f"Observed ESC {identifier} max − min", unit, self.gap)
                for frame in frames:
                    rows = frame["instances"]
                    if frame["invalid"] or set(rows) != self.esc_instances or any(identifier not in row for row in rows.values()):
                        continue
                    values = [row[identifier] for row in rows.values()]
                    summary.add(max(values) - min(values), frame["time_s"], frame["evidence"], frame["key"][0])
                spreads.append(summary.finish())
        domains = []
        for domain, title in DOMAINS.items():
            channels = [{"instance_id": instance, "message_type": message,
                         "metrics": [summary.finish() for summary in value["metrics"].values()],
                         "observations": dict(value["observations"])}
                        for (key, message, instance), value in sorted(self.channels.items()) if key == domain]
            observed = any(metric["sample_count"] for channel in channels for metric in channel["metrics"])
            result = {"id": domain, "title": title, "status": "observed" if observed else "unavailable",
                      "channels": channels, "limitations": DOMAIN_LIMITS[domain]}
            if domain == "esc":
                result.update(observed_instances=sorted(self.esc_instances), expected_instances="unknown",
                              matched_frame_spreads=spreads, retained_frame_limit=MAX_ESC_FRAMES,
                              frames_observed=self.esc_frames_seen + int(self.esc_frame is not None),
                              spreads_truncated=self.esc_frames_seen + int(self.esc_frame is not None) > MAX_ESC_FRAMES)
            domains.append(result)
        return deepcopy({"algorithm_version": VERSION,
            "status": "observed" if any(item["status"] == "observed" for item in domains) else "unavailable",
            "scope": {"start_s": self.start, "end_s": self.end, "maximum_gap_s": self.gap},
            "domains": domains, "findings": list(self.findings.values()),
            "excluded_records": dict(self.exclusions), "barrier_count": self.barriers,
            "references": [{"id": key, "repository": "ArduPilot/ardupilot", "revision": REVISION,
                "path": path, "sha256": digest, "retrieved_at": "2026-10-15", "url": f"https://github.com/ArduPilot/ardupilot/blob/{REVISION}/{path}"}
                for key, (path, digest) in SOURCE_FILES.items()],
            "firmware_scope": {"reviewed_release": "Copter-4.7.1", "declared_firmware": self.firmware,
                               "exact_build_validated": False},
            "limitations": ["Descriptive schema-qualified DataFlash observations only; unsupported telemetry layouts and missing channels remain unavailable.",
                "No healthy/airworthy verdict, numerical tuning, unique failure cause or parameter write follows from these summaries.",
                "Instances and clocks remain separate. Statistics describe valid samples, not complete flight-time coverage or causal improvement.",
                "Source metadata is pinned to Copter-4.7.1; matching fields or an operator firmware label do not validate the exact installed build."]})
