"""Bounded reconstruction of the pinned ArduPilot ISBH/ISBD wire format.

Independent implementation from published field/sequence semantics. No upstream
WebTools or firmware implementation is incorporated. All raw records survive.
"""
from __future__ import annotations

import math

FIRMWARE_REVISION = "dbe792162d06cab66c3475fd5556bf7a120f119e"
WEBTOOLS_REVISION = "5b7fe705f6516c0d60830449d14630135cd23ed2"
MAX_BATCH_SAMPLES = 8192
CHUNK_SAMPLES = 128
REFERENCES = [
    f"https://github.com/ArduPilot/ardupilot/blob/{FIRMWARE_REVISION}/libraries/AP_InertialSensor/BatchSampler.cpp",
    f"https://github.com/ArduPilot/ardupilot/blob/{FIRMWARE_REVISION}/libraries/AP_InertialSensor/AP_InertialSensor_Logging.cpp",
    f"https://github.com/ArduPilot/WebTools/blob/{WEBTOOLS_REVISION}/FilterReview/FilterReview.js",
]


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _schema(event, required):
    fields = event.get("field_schema", {})
    return all(fields.get(name, {}).get("format_code") == code for name, code in required.items())


class BatchAssembler:
    """One active batch, matching the firmware's serial batch logger.

    Emit nothing numerical until every block passes sequence/count checks.
    Discontinuities, duplicate headers/blocks and corruption discard the active
    batch explicitly. Boot and logging clocks never use reconstructed samples.
    """
    def __init__(self):
        self.active = None
        self.last_batch = None
        self.options = None
        self.options_time = None
        self.epoch = None

    def issue(self, event, reason):
        return {**event, "kind": "diagnostic", "name": "imu_batch_issue", "reason": reason,
                "quality_flags": [reason], "metric_barrier": True,
                "batch_decode": {"status": "rejected", "reason": reason}}

    def finish(self):
        if self.active is None:
            return []
        event = self.active["header"]
        self.active = None
        return [self.issue(event, "incomplete_imu_batch_at_end")]

    def accept(self, event):
        output = []
        epoch = event.get("boot_epoch")
        if self.epoch is not None and epoch != self.epoch:
            if self.active:
                output.append(self.issue(event, "imu_batch_clock_boundary"))
            self.active = self.last_batch = self.options = self.options_time = None
        self.epoch = epoch
        if event.get("kind") == "diagnostic":
            if self.active:
                output.append(self.issue(event, "imu_batch_corruption_boundary"))
                self.active = None
            return output
        if event.get("kind") == "parameter" and event.get("name") == "INS_LOG_BAT_OPT":
            value = event.get("value")
            self.options = int(value) if isinstance(value, (int, float)) and math.isfinite(value) and value == int(value) and 0 <= value <= 7 else None
            self.options_time = event.get("time_us")
            if self.active and self.options != self.active["options"]:
                self.active["options"] = None
        message, raw = event.get("message_type"), event.get("raw_fields", {})
        if message == "ISBH":
            if self.active:
                output.append(self.issue(event, "incomplete_imu_batch_before_next_header"))
            self.active = None
            required = dict(zip(("TimeUS", "N", "type", "instance", "mul", "smp_cnt", "SampleUS", "smp_rate"), "QHBBHHQf"))
            valid = (_schema(event, required) and _integer(raw.get("N"), 0, 65535)
                     and _integer(raw.get("type"), 0, 1) and _integer(raw.get("instance"), 0, 31)
                     and _integer(raw.get("mul"), 1, 65535)
                     and _integer(raw.get("smp_cnt"), 32, MAX_BATCH_SAMPLES) and raw["smp_cnt"] % 32 == 0
                     and _integer(raw.get("SampleUS"), 0, 2**53 - 1)
                     and isinstance(raw.get("smp_rate"), (int, float)) and not isinstance(raw.get("smp_rate"), bool)
                     and math.isfinite(raw["smp_rate"]) and 10 <= raw["smp_rate"] <= 64000
                     and isinstance(event.get("time_us"), int) and raw["SampleUS"] <= event["time_us"]
                     and not event.get("quality_flags"))
            if not valid:
                output.append(self.issue(event, "invalid_or_unsupported_imu_batch_header"))
                return output
            sequence = raw["N"]
            if self.last_batch is not None:
                if sequence == self.last_batch:
                    output.append(self.issue(event, "duplicate_imu_batch_header"))
                    return output
                if sequence != (self.last_batch + 1) % 65536:
                    output.append(self.issue(event, "missing_imu_batch_sequence"))
            self.last_batch = sequence
            eligible_options = self.options if isinstance(self.options_time, int) and self.options_time <= raw["SampleUS"] else None
            self.active = {"header": event, "raw": raw, "options": eligible_options,
                           "x": [], "y": [], "z": [], "references": []}
        elif message == "ISBD":
            required = dict(zip(("TimeUS", "N", "seqno", "x", "y", "z"), "QHHaaa"))
            current = self.active
            if current is None:
                output.append(self.issue(event, "imu_batch_data_without_header"))
                return output
            good = (_schema(event, required) and raw.get("N") == current["raw"]["N"]
                    and type(raw.get("seqno")) is int and raw["seqno"] == len(current["x"]) // 32
                    and all(isinstance(raw.get(axis), list) and len(raw[axis]) == 32
                            and all(_integer(value, -32768, 32767) for value in raw[axis]) for axis in "xyz")
                    and not event.get("quality_flags"))
            if not good:
                self.active = None
                output.append(self.issue(event, "missing_duplicate_or_invalid_imu_batch_block"))
                return output
            for axis in "xyz":
                current[axis].extend(raw[axis])
            current["references"].append({"record_index": event.get("record_index"), "byte_offset": event.get("byte_offset")})
            if len(current["x"]) == current["raw"]["smp_cnt"]:
                self.active = None
                header = current["raw"]
                final_sample = header["SampleUS"] + (header["smp_cnt"] - 1) * 1e6 / header["smp_rate"]
                if event.get("time_us") is None or final_sample > event["time_us"] + 1:
                    output.append(self.issue(event, "imu_batch_measurement_clock_after_log_clock"))
                    return output
                options = current["options"]
                # Offset instances encode pre/post using the *actual sensor
                # count*, which cannot safely be guessed from a partial log.
                stage = ("unknown" if options is None or options & 4 or options & 3 == 3
                         else "post" if options & 2 else "pre")
                for offset in range(0, header["smp_cnt"], CHUNK_SAMPLES):
                    count = min(CHUNK_SAMPLES, header["smp_cnt"] - offset)
                    blocks = current["references"][offset // 32:(offset + count) // 32]
                    output.append({**event, "kind": "imu_batch", "name": "validated_imu_batch_chunk",
                        "samples": {axis: [value / header["mul"] for value in current[axis][offset:offset + count]] for axis in "xyz"},
                        "sample_start_us": header["SampleUS"], "sample_offset": offset, "sample_count": count,
                        "batch_sample_count": header["smp_cnt"], "batch_id": header["N"],
                        "sensor_type": "gyro" if header["type"] == 1 else "accel",
                        "sensor_instance": str(header["instance"]), "sensor_instance_basis": "logged batch instance; physical sensor mapping unresolved when pre/post offset is used",
                        "sample_rate_hz": float(header["smp_rate"]), "sample_unit": "rad/s" if header["type"] == 1 else "m/s²",
                        "filter_stage": stage, "batch_options_observed": options,
                        "filter_stage_basis": "partial INS_LOG_BAT_OPT observation under pinned wire semantics; not full effective configuration proof",
                        "batch_header_reference": {"record_index": current["header"].get("record_index"), "byte_offset": current["header"].get("byte_offset")},
                        "sample_block_references": blocks,
                        "encoding_endpoint_count": sum(abs(value) >= 32767 for axis in "xyz" for value in current[axis][offset:offset + count]),
                        "batch_decode": {"status": "complete", "scale": "signed int16 divided by ISBH.mul", "references": REFERENCES,
                                         "firmware_build_verified": False}, "raw_fields": {}, "field_schema": {}})
        return output
