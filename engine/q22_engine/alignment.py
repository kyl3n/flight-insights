"""Content-scoped reported-clock alignment and conservative association rules.

Verified means this documented rule passed, not authenticated aircraft identity,
atomic parameter state, complete collection, or qualified hardware behavior.
"""
from __future__ import annotations

from bisect import bisect_left
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math

from .collection import CollectionService, _hash, _integer, _text
from .logs import LogService, check_cancel, hash_file, source_key
from .store import Store


RULE = "q22-uid-clock-armed-corroboration/1"
MAX_LOGS = 64
MAX_EVENTS = 4_000_000
MAX_ANCHORS = 32768
MAX_REPORT_ANCHORS = 64
MAX_PARAMETERS = 20_000
MAX_TRANSITIONS = 2048
MAX_STREAMS = 256
MAX_RETAINED = 500_000
MAX_GAP_US = 2_000_000
CLOCK_TOLERANCE_US = 500_000
LIMITATIONS = [
    "Verified means the documented content rule passed within the recorded catalog candidate universe, not cryptographic aircraft authentication.",
    "Reported UTC/GNSS clock consistency is not independent calibration of absolute time; uncertainty includes an engineering allowance.",
    "Armed intervals are observed activity, not airborne flight. Complete boundary evidence is required for automatic association.",
    "Parameter observation time does not establish activation time; non-atomic readbacks and unobserved changes remain possible.",
    "Synthetic tests establish software behavior only; Q22 hardware and GCS qualification remain open.",
]


def integer(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
        return result if str(result) == str(value) or isinstance(value, int) else None
    except (TypeError, ValueError, OverflowError):
        return None


def selector(value):
    if not isinstance(value, dict) or set(value) - {"log_id", "source_key", "boot_epoch"}:
        raise ValueError("Select exactly one log/source/clock segment.")
    return {"log_id": _text(value.get("log_id"), "log_id"),
            "source_key": _text(value.get("source_key"), "source_key"),
            "boot_epoch": _integer(value.get("boot_epoch"), "boot_epoch", 0, 2**31 - 1)}


def key(value):
    return value["log_id"], value["source_key"], value["boot_epoch"]


def ref(event):
    return {"record_index": event.get("record_index"), "byte_offset": event.get("byte_offset"),
            "message_type": event.get("message_type")}


def gps_utc(raw, schema):
    """Pinned modern GPS wire convention; no leap extrapolation past 2026."""
    expected = {"TimeUS": "Q", "GWk": "H", "GMS": "I", "Status": "B"}
    if any(schema.get(name, {}).get("format_code") != code for name, code in expected.items()):
        return None
    week, millis, status = (integer(raw.get(name)) for name in ("GWk", "GMS", "Status"))
    if week is None or millis is None or status is None or not 3 <= status <= 6 or not 0 <= millis < 604800000:
        return None
    # GPS epoch 1980-01-06; GPS-UTC=18 s from 2017-01-01. Bound the
    # reviewed leap policy rather than silently assuming future leap seconds.
    utc = 315964800000000 + week * 604800000000 + millis * 1000 - 18000000
    return utc if 1483228800000000 <= utc < 1798761600000000 else None


def clock_model(anchors):
    # Every retained observation participates in the checks and fit. Only the
    # diagnostic presentation is sampled; the endpoints remain auditable.
    indices = (list(range(len(anchors))) if len(anchors) <= MAX_REPORT_ANCHORS else
               [i * (len(anchors) - 1) // (MAX_REPORT_ANCHORS - 1) for i in range(MAX_REPORT_ANCHORS)])
    receipt_offsets = [a["receive_us"] - a["utc_us"] for a in anchors if a.get("receive_us") is not None]
    presentation = {"anchors": [anchors[i] for i in indices], "anchor_count": len(anchors),
                    "anchor_sample_count": len(indices), "anchor_sample_indices": indices,
                    "anchors_sampled": len(indices) != len(anchors),
                    "receive_clock_offset_range_us": [min(receipt_offsets), max(receipt_offsets)] if receipt_offsets else None}
    failure = {"state": "unavailable", **presentation}
    if len(anchors) < 3:
        return failure | {"reason": "At least three supported UTC/boot anchors are required."}
    x0, y0 = anchors[0]["boot_us"], anchors[0]["utc_us"]
    xs = [a["boot_us"] - x0 for a in anchors]
    ys = [a["utc_us"] - y0 for a in anchors]
    if any(b <= a for a, b in zip(xs, xs[1:])) or any(b <= a for a, b in zip(ys, ys[1:])) or xs[-1] < 2_000_000:
        return failure | {"reason": "Clock anchors repeat, regress, or span less than two seconds."}
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    variance = sum((x - mx)**2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / variance
    offset = my - slope * mx
    residuals = [y - (slope * x + offset) for x, y in zip(xs, ys)]
    worst = max(abs(v) for v in residuals)
    if abs(slope - 1) > .0002 or worst > 50000:
        return failure | {"reason": "Clock drift/residual exceeds the pinned 200 ppm / 50 ms rule.",
                          "slope": slope, "maximum_residual_us": worst}
    return {"state": "supported_reported_clock", "method": "affine_least_squares_no_extrapolation",
            "boot_origin_us": x0, "utc_origin_us": y0, "offset_us": offset, "slope": slope,
            "boot_range_us": [anchors[0]["boot_us"], anchors[-1]["boot_us"]],
            "boot_epoch_utc_us": y0 + offset - slope * x0,
            "residuals_us": [residuals[i] for i in indices], "residual_count": len(residuals),
            "maximum_residual_us": worst,
            "maximum_residual_anchor_index": max(range(len(residuals)), key=lambda i: abs(residuals[i])),
            "rms_residual_us": math.sqrt(sum(v*v for v in residuals) / len(residuals)),
            "uncertainty_us": worst + 100000, "uncertainty_basis": "residual envelope plus 100 ms engineering allowance; not a calibrated confidence interval",
            **presentation,
            "absolute_time_authenticated": False}


def mapped(model, boot):
    if model["state"] != "supported_reported_clock" or boot is None or not model["boot_range_us"][0] <= boot <= model["boot_range_us"][1]:
        return None
    return model["utc_origin_us"] + model["offset_us"] + model["slope"] * (boot - model["boot_origin_us"])


class Evidence:
    def __init__(self, log, stream):
        self.log, self.stream = log, stream
        self.selection = {"log_id": log["id"], "source_key": stream["source_key"], "boot_epoch": stream["boot_epoch"]}
        self.anchors, self.states, self.parameters = [], [], []
        self.uids, self.firmware, self.identity_provenance = set(), [], []
        self.issues, self.limits = [], []
        self.count = 0
        self.anchor_count, self.armed_count = 0, 0
        self._last_armed, self._receive_index = None, None

    def issue(self, reason):
        if reason not in self.issues and len(self.issues) < 32:
            self.issues.append(reason)

    def observe(self, event):
        self.count += 1
        if event.get("quality_flags") or event.get("kind") == "diagnostic":
            self.issue("Decoder quality/clock discontinuity in this source segment.")
        raw, name = event.get("raw_fields", {}), event.get("message_type")
        if name == "AUTOPILOT_VERSION":
            uid = integer(raw.get("uid"))
            if uid and uid > 0:
                if len(self.uids) < 4:
                    self.uids.add(str(uid))
                else:
                    self.issue("Hardware UID observation bound exceeded.")
            fw = {k: raw[k] for k in ("flight_sw_version", "board_version", "vendor_id", "product_id") if k in raw}
            if fw and fw not in self.firmware and len(self.firmware) < 4:
                self.firmware.append(fw)
            elif fw not in self.firmware:
                self.issue("Firmware identity observation bound exceeded.")
            if len(self.identity_provenance) < 32:
                self.identity_provenance.append(ref(event))
        utc, boot = None, integer(event.get("boot_time_us"))
        if name == "SYSTEM_TIME":
            value = integer(raw.get("time_unix_usec"))
            if value is not None and 946684800000000 <= value < 4102444800000000:
                utc = value
        elif name == "GPS" and raw.get("I", 0) == 0:
            utc = gps_utc(raw, event.get("field_schema", {}))
        if utc is not None and boot is not None:
            self.anchor_count += 1
            if len(self.anchors) >= MAX_ANCHORS:
                if "clock_anchor_limit" not in self.limits:
                    self.limits.append("clock_anchor_limit")
            else:
                self.anchors.append({"boot_us": boot, "utc_us": utc, "receive_us": integer(event.get("receive_time_us")),
                                     "basis": "MAVLink_SYSTEM_TIME" if name == "SYSTEM_TIME" else "DataFlash_GPS_week_ms_18s_leap_2017_2026",
                                     "evidence": ref(event)})
                self._receive_index = None
        armed = None
        if name == "HEARTBEAT" and raw.get("autopilot") not in (None, 8) and raw.get("type") != 6 and type(raw.get("base_mode")) is int:
            armed = bool(raw["base_mode"] & 128)
        elif name == "ARM" and type(raw.get("ArmState")) is int and raw["ArmState"] in (0, 1):
            armed = bool(raw["ArmState"])
        if armed is not None:
            self.armed_count += 1
            row = {"armed": armed, "boot_us": boot, "receive_us": integer(event.get("receive_time_us")), "evidence": ref(event)}
            previous = self._last_armed
            if previous:
                for clock in ("boot_us", "receive_us"):
                    if row[clock] is not None and previous[clock] is not None:
                        elapsed = row[clock] - previous[clock]
                        if elapsed < 0:
                            self.issue("Armed observation clock regressed before state compaction.")
                        elif (name == "HEARTBEAT" and previous["evidence"]["message_type"] == "HEARTBEAT"
                              and elapsed > MAX_GAP_US):
                            self.issue("Heartbeat gap prevents armed-state continuity verification.")
            self._last_armed = row
            # Retain first and last of each constant state run, including both
            # adjacent observations around every transition. Check all receipts
            # above so compaction cannot conceal a discontinuity.
            if len(self.states) >= 2 and self.states[-1]["armed"] == armed == self.states[-2]["armed"]:
                self.states[-1] = row
            elif len(self.states) < MAX_TRANSITIONS:
                self.states.append(row)
            elif "armed_observation_limit" not in self.limits:
                self.limits.append("armed_observation_limit")
        if event.get("kind") == "parameter":
            try:
                value = str(event["value"])
                if not Decimal(value).is_finite():
                    return
            except (KeyError, InvalidOperation):
                return
            if len(self.parameters) < MAX_PARAMETERS:
                self.parameters.append({"name": event["name"], "value": value, "reported_type": raw.get("param_type"),
                                        "boot_us": boot, "receive_us": integer(event.get("receive_time_us")), "evidence": ref(event)})
            elif "parameter_observation_limit" not in self.limits:
                self.limits.append("parameter_observation_limit")

    def time(self, observation, model):
        boot, uncertainty = observation["boot_us"], model.get("uncertainty_us", 0)
        if boot is None:
            receive = observation["receive_us"]
            if self._receive_index is None:
                anchors = [a for a in self.anchors if a["receive_us"] is not None]
                times = [a["receive_us"] for a in anchors]
                self._receive_index = anchors, times, len(times) >= 2 and all(b > a for a, b in zip(times, times[1:]))
            anchors, times, valid = self._receive_index
            if receive is None or not valid:
                return None
            index = bisect_left(times, receive)
            if index == 0 or index == len(times):
                return None
            a, b = anchors[index - 1], anchors[index]
            width = b["receive_us"] - a["receive_us"]
            if width > MAX_GAP_US:
                return None
            boot = a["boot_us"] + (receive - a["receive_us"]) / width * (b["boot_us"] - a["boot_us"])
            uncertainty += width  # Conservative ordering envelope, not exact link latency.
        utc = mapped(model, boot)
        return None if utc is None else {"utc_us": utc, "uncertainty_us": uncertainty, "boot_us": boot}

    def finish(self):
        model = clock_model(self.anchors)
        intervals, active, previous = [], None, None
        for row in self.states:
            timed = self.time(row, model)
            row = row | {"time": timed}
            if row["armed"] and (previous is None or not previous["armed"]):
                active = {"start": row, "prior_disarmed": previous, "last_armed": row, "end": None}
            elif row["armed"] and active:
                active["last_armed"] = row
            elif not row["armed"] and active:
                active["end"] = row
                intervals.append(active)
                active = None
            previous = row
        if active:
            intervals.append(active)
        for index, interval in enumerate(intervals):
            interval["index"] = index
            interval["complete_boundaries"] = all(interval.get(k) and interval[k]["time"] for k in ("start", "prior_disarmed", "last_armed", "end"))
            if interval["complete_boundaries"]:
                for label, first, last in (("arm_range_us", "prior_disarmed", "start"), ("disarm_range_us", "last_armed", "end")):
                    a, b = interval[first]["time"], interval[last]["time"]
                    interval[label] = [a["utc_us"] - a["uncertainty_us"], b["utc_us"] + b["uncertainty_us"]]
                    if b["utc_us"] < a["utc_us"] or b["utc_us"] - a["utc_us"] > MAX_GAP_US:
                        interval["complete_boundaries"] = False
            interval["record_span"] = {"kind": "records", "start": interval["start"]["evidence"]["record_index"],
                                       "end": interval["end"]["evidence"]["record_index"] if interval["end"] else None}
        return {"selection": self.selection, "artifact_sha256": self.log["source"]["sha256"], "format": self.log["format"],
                "synthetic": self.log["synthetic"], "uids": sorted(self.uids), "firmware": self.firmware,
                "identity_provenance": self.identity_provenance[:32], "clock_model": model,
                "armed_intervals": intervals, "quality_issues": self.issues, "limits_reached": self.limits,
                "observation_counts": {"clock_anchors": self.anchor_count, "retained_clock_anchors": len(self.anchors),
                                       "armed": self.armed_count, "retained_armed_boundaries": len(self.states),
                                       "armed_compaction": "first_and_last_per_constant_state_run_all_receipts_checked"},
                "record_count": self.count}


def overlap(a, b):
    return max(a[0], b[0]) <= min(a[1], b[1])


def match(reference, candidate):
    reasons, contradictions, matches = [], [], []
    if reference["synthetic"] != candidate["synthetic"]:
        reasons.append("Synthetic and declared-real source provenance cannot corroborate each other.")
    if len(reference["uids"]) != 1 or len(candidate["uids"]) != 1:
        reasons.append("Exactly one supported nonzero UID observation/provenance is required on each source.")
    if len(reference["uids"]) > 1 or len(candidate["uids"]) > 1:
        contradictions.append("Conflicting hardware UID evidence within a source segment.")
    elif reference["uids"] and candidate["uids"] and reference["uids"] != candidate["uids"]:
        contradictions.append("Nonzero hardware UID contradiction.")
    fw_a, fw_b = reference["firmware"], candidate["firmware"]
    if (len(fw_a) != 1 or len(fw_b) != 1
            or not all(isinstance(value, dict) and all(k in value for k in ("flight_sw_version", "board_version", "vendor_id", "product_id"))
                       for value in fw_a + fw_b)):
        reasons.append("One comparable observed firmware/board identity is required per source.")
    elif fw_a != fw_b:
        contradictions.append("Observed firmware/board identity differs.")
    a, b = reference["clock_model"], candidate["clock_model"]
    if a["state"] != "supported_reported_clock" or b["state"] != "supported_reported_clock":
        reasons.append("Independent supported UTC/boot clock models are unavailable.")
    elif abs(a["boot_epoch_utc_us"] - b["boot_epoch_utc_us"]) > CLOCK_TOLERANCE_US:
        contradictions.append("Supported absolute boot origins differ by more than 500 ms.")
    if reference["artifact_sha256"] == candidate["artifact_sha256"]:
        reasons.append("Identical original bytes are duplicate evidence, not independent corroboration.")
    if reference["quality_issues"] or candidate["quality_issues"] or reference["limits_reached"] or candidate["limits_reached"]:
        reasons.append("Quality discontinuity or bounded-evidence truncation prevents verification.")
    for left in reference["armed_intervals"]:
        if not left["complete_boundaries"]:
            continue
        alternatives = [right for right in candidate["armed_intervals"] if right["complete_boundaries"]
                        and overlap(left["arm_range_us"], right["arm_range_us"])
                        and overlap(left["disarm_range_us"], right["disarm_range_us"])]
        for right in alternatives:
            matches.append({"reference_interval_index": left["index"], "candidate_interval_index": right["index"],
                            "reference_record_span": left["record_span"], "candidate_record_span": right["record_span"],
                            "arm_overlap_us": [max(left["arm_range_us"][0], right["arm_range_us"][0]), min(left["arm_range_us"][1], right["arm_range_us"][1])],
                            "disarm_overlap_us": [max(left["disarm_range_us"][0], right["disarm_range_us"][0]), min(left["disarm_range_us"][1], right["disarm_range_us"][1])]})
    if not matches:
        reasons.append("No pair of complete observed armed boundaries has compatible uncertainty ranges.")
        if (not reasons[:-1] and reference["armed_intervals"] and candidate["armed_intervals"]):
            contradictions.append("Supported armed interval sequences do not overlap.")
    ambiguous = any(sum(m["reference_interval_index"] == item["reference_interval_index"] or m["candidate_interval_index"] == item["candidate_interval_index"] for m in matches) > 1 for item in matches)
    state = "rejected" if contradictions else "provisional" if reasons else "ambiguous" if ambiguous else "verified"
    return {"selection": candidate["selection"], "artifact_sha256": candidate["artifact_sha256"], "format": candidate["format"],
            "state": state, "matches": matches, "missing_evidence": reasons, "contradictions": contradictions,
            "competing_candidates": [], "rule_version": RULE, "evidence": candidate}


class AlignmentService:
    def __init__(self, store: Store, logs=None, collection=None):
        self.store = store
        self.logs = logs or LogService(store)
        self.collection = collection or CollectionService(store)

    def dispatch(self, method, params, *, cancel_check=None, progress=None):
        if method == "alignment.analyze":
            return {"report": self.analyze(params, cancel_check, progress)}
        if method == "alignment.get":
            return {"report": self.store.get_document("alignment_report", _text(params.get("report_id"), "report_id"))}
        if method == "alignment.attach":
            return self.attach(params, cancel_check, progress)
        if method == "alignment.reference":
            return self.reference(params, cancel_check, progress)
        if method == "alignment.list":
            limit = _integer(params.get("limit", 20), "limit", 1, 100)
            offset = _integer(params.get("offset", 0), "offset", 0, 1_000_000)
            reports = self.store.list_documents("alignment_report", limit, offset)
            return {"reports": [{"id": r["id"], "created_at": r["created_at"], "inputs": r["inputs"],
                                  "algorithm_version": r["algorithm_version"],
                                  "candidate_states": [c["state"] for c in r["candidates"]]} for r in reports],
                    "next_offset": offset + len(reports) if len(reports) == limit else None}
        raise ValueError("Unknown alignment method.")

    def reference(self, params, cancel_check=None, progress=None):
        with self.collection._lock, self.store._connect() as db:
            record = self.collection._get(db, "flight_record", params.get("flight_record_id"))
            capture = self.collection._get(db, "capture", record["capture_session_id"])
            sources = [a for a in capture["artifact_sources"] if a["role"] == "app_mavlink_capture"
                       and a.get("coverage", {}).get("complete_chunk") is True and a.get("coverage", {}).get("record_scope") == "capture_global"
                       and (params.get("artifact_source_id") is None or a["source"]["id"] == params["artifact_source_id"])]
        if len(sources) != 1:
            raise ValueError("Choose exactly one completed archived app TLOG chunk as the alignment reference.")
        source = sources[0]["source"]
        path = self.store.raw_path(source["sha256"])
        if hash_file(path, cancel_check) != source["sha256"]:
            raise ValueError("Managed original failed SHA-256 verification.")
        with self.store._connect() as db:
            prior = db.execute("SELECT data FROM documents WHERE kind='log' AND json_extract(data,'$.source.sha256')=? ORDER BY id LIMIT 1",
                               (source["sha256"],)).fetchone()
        if prior:
            imported = self.logs.summary(json.loads(prior[0]))
        else:
            imported = self.logs.import_log({"path": str(path), "label": "Managed reference " + source["original_name"],
                "synthetic": capture["profile"]["connection"].get("synthetic", False)}, cancel_check=cancel_check, progress=progress)["log"]
        return {"log": imported, "reference_choices": [{"log_id": imported["id"], "source_key": s["source_key"],
                  "boot_epoch": s["boot_epoch"]} for s in imported["streams"]]}

    def _catalog(self):
        with self.store._connect() as db:
            rows = db.execute("SELECT data FROM documents WHERE kind='log' ORDER BY id LIMIT ?", (MAX_LOGS + 1,)).fetchall()
            acquisitions = db.execute("SELECT data FROM documents WHERE kind='acquisition' ORDER BY id LIMIT 513").fetchall()
        logs = [json.loads(row[0]) for row in rows]
        signature = _hash({"logs": [{k: log[k] for k in ("id", "source", "derived_sha256")} for log in logs],
                           "acquisitions": [hashlib.sha256(row[0].encode()).hexdigest() for row in acquisitions]})
        return logs[:MAX_LOGS], len(logs) > MAX_LOGS or len(acquisitions) > 512, signature, [json.loads(r[0]) for r in acquisitions[:512]]

    def _provenance(self, evidence, acquisitions):
        if evidence.log["format"] not in ("bin", "log"):
            return
        for acquisition in acquisitions:
            if acquisition.get("kind") != "log_download" or acquisition.get("state") != "complete":
                continue
            sources = [s for s in acquisition.get("source_refs", []) if s.get("role") == "onboard_log_candidate" and s.get("sha256") == evidence.log["source"]["sha256"]]
            if not sources:
                continue
            download = acquisition.get("download", {})
            if (acquisition.get("synthetic") != evidence.log["synthetic"] or download.get("transfer_complete") is not True
                    or download.get("sha256") != evidence.log["source"]["sha256"] or not download.get("eof_evidence")
                    or download.get("contiguous_prefix_bytes") != download.get("reported_size")
                    or not download.get("before_entry_evidence") or not download.get("after_entry_evidence")):
                evidence.issue("Download identity provenance lacks complete matching transfer/inventory/EOF evidence or synthetic scope.")
                continue
            transport = acquisition.get("transport_evidence", {})
            first = (transport.get("initial_gate_observations") or {}).get("identity") or {}
            last = (transport.get("final_gate_observations") or {}).get("identity") or {}
            if (first.get("uid") != last.get("uid") or not first.get("uid") or not isinstance(first.get("firmware_identity"), dict)
                    or first.get("firmware_identity") != last.get("firmware_identity")):
                evidence.issue("Acquisition provenance lacks matching initial/final identity gates.")
                continue
            evidence.uids.add(str(first["uid"]))
            if first["firmware_identity"] not in evidence.firmware:
                evidence.firmware.append(first["firmware_identity"])
            evidence.identity_provenance.append({"acquisition_id": acquisition["id"], "source_ids": [s["id"] for s in sources],
                                                 "basis": "downloaded_original_from_observed_UID_controller_not_authenticated"})

    def analyze(self, params, cancel_check=None, progress=None, *, persist=True):
        chosen = selector(params.get("reference"))
        candidates = params.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 16:
            raise ValueError("Select 1..16 candidate source/clock segments.")
        selected = [selector(value) for value in candidates]
        if len({key(value) for value in selected}) != len(selected) or key(chosen) in {key(value) for value in selected}:
            raise ValueError("Reference/candidate selectors must be distinct.")
        catalog, truncated, signature, acquisitions = self._catalog()
        reducers, event_count, retained = {}, 0, 0
        for log in catalog:
            check_cancel(cancel_check)
            self.logs.verify_artifact(log, cancel_check)
            if hash_file(self.store.raw_path(log["source"]["sha256"]), cancel_check) != log["source"]["sha256"]:
                raise ValueError("Original log bytes failed SHA-256 verification.")
            local = {}
            for stream in log["streams"]:
                if len(reducers) >= MAX_STREAMS:
                    truncated = True
                    break
                value = Evidence(log, stream)
                local[(stream["source_key"], stream["boot_epoch"])] = value
                reducers[key(value.selection)] = value
            for event in self.logs.events(log, cancel_check=cancel_check):
                event_count += 1
                if event_count > MAX_EVENTS:
                    truncated = True
                    break
                value = local.get((source_key(event), event.get("boot_epoch", 0)))
                if value:
                    before = len(value.anchors) + len(value.states) + len(value.parameters)
                    value.observe(event)
                    retained += len(value.anchors) + len(value.states) + len(value.parameters) - before
                    if retained > MAX_RETAINED:
                        truncated = True
                        break
                elif event.get("kind") == "diagnostic":
                    for value in local.values():
                        value.issue("Unscoped decoder diagnostic prevents whole-file continuity verification.")
            for value in local.values():
                self._provenance(value, acquisitions)
            if progress:
                progress("aligning catalog evidence", event_count, None)
            if event_count > MAX_EVENTS or retained > MAX_RETAINED:
                break
        if key(chosen) not in reducers or any(key(value) not in reducers for value in selected):
            raise ValueError("Selected source was not found within the bounded imported catalog.")
        reference = reducers[key(chosen)].finish()
        universe = [match(reference, value.finish()) for scope, value in reducers.items() if scope != key(chosen)]
        for item in universe:
            if item["state"] != "verified":
                continue
            peers = [other for other in universe if other is not item and other["state"] in ("verified", "ambiguous")
                     and other["format"] == item["format"] and other["artifact_sha256"] != item["artifact_sha256"]
                     and any(a["reference_interval_index"] == b["reference_interval_index"] for a in item["matches"] for b in other["matches"])]
            if peers:
                item.update(state="ambiguous", competing_candidates=[other["selection"] for other in peers])
        if truncated:
            for item in universe:
                if item["state"] == "verified":
                    item["state"] = "provisional"
                item["missing_evidence"].append("Candidate universe was bounded/truncated; uniqueness is unestablished.")
        result = [next(item for item in universe if key(item["selection"]) == key(value)) for value in selected]
        report = {"algorithm_version": RULE, "inputs": {"reference": chosen, "candidates": selected,
                   "flight_record_id": params.get("flight_record_id")}, "reference": reference, "candidates": result,
                  "candidate_universe": {"basis": "all imported log source/clock segments in this catalog at analysis time",
                    "log_count": len(catalog), "stream_count": len(reducers), "truncated": truncated,
                    "signature": signature, "alternatives": [{k: item[k] for k in ("selection", "state", "contradictions")} for item in universe]},
                  "parameter_timeline": self._timeline(reducers[key(chosen)], [reducers[key(item["selection"])] for item in result if item["state"] == "verified"]),
                  "limitations": LIMITATIONS, "hardware_qualified": False, "immutable": True}
        report["parameter_links"] = self._parameter_links(reference, report["parameter_timeline"], acquisitions, cancel_check)
        report["evidence_hash"] = _hash(report)
        return self.store.add_document("alignment_report", report) if persist else report

    def _parameter_links(self, reference, timeline, acquisitions, cancel_check):
        """Link evidence generations by identity/boot/receipt bounds, not role labels."""
        links = []
        model = reference["clock_model"]
        for acquisition in acquisitions:
            check_cancel(cancel_check)
            if acquisition.get("kind") != "parameters":
                continue
            transport = acquisition.get("transport_evidence", {})
            first = transport.get("initial_gate_observations") or {}
            last = transport.get("final_gate_observations") or {}
            identities = [gate.get("identity") or {} for gate in (first, last)]
            if not any(str(identity.get("uid")) in reference["uids"] for identity in identities):
                continue
            capture = acquisition.get("parameter_capture", {})
            link = {"acquisition_id": acquisition["id"], "state": "provisional", "matches": [], "missing_evidence": [],
                    "synthetic": acquisition.get("synthetic", False),
                    "contradictions": [], "completeness": capture.get("completeness", "unknown"),
                    "consistency": capture.get("consistency", "unknown"), "atomic": False,
                    "flight_time_applicability": "unknown", "declared_role": capture.get("capture_role"),
                    "source_refs": [{k: s[k] for k in ("id", "sha256", "role")} for s in acquisition.get("source_refs", [])
                                    if s.get("role") in ("parameter_readback_snapshot", "parameter_readback_export")],
                    "identity_basis": "reported_initial_final_UID_and_firmware_not_authenticated"}
            links.append(link)
            if reference["quality_issues"] or reference["limits_reached"]:
                link["missing_evidence"].append("Reference continuity or bounded evidence is insufficient for a supported temporal role.")
            connection = acquisition.get("connection", {})
            if (acquisition.get("synthetic") != reference["synthetic"] or
                    reference["selection"]["source_key"] != f"{connection.get('system_id')}:{connection.get('component_id')}"):
                link["missing_evidence"].append("Synthetic provenance or selected system/component scope differs.")
            if any(not i.get("uid") or not isinstance(i.get("firmware_identity"), dict) for i in identities):
                link["missing_evidence"].append("Both initial and final observed acquisition identities are required.")
                continue
            if (len(reference["uids"]) != 1 or any(str(i.get("uid")) != reference["uids"][0] for i in identities)
                    or len(reference["firmware"]) != 1 or any(i.get("firmware_identity") != reference["firmware"][0] for i in identities)):
                link["contradictions"].append("Initial/final acquisition UID or firmware does not match the reference source.")
                link["state"] = "rejected"
                continue
            clocks = [gate.get("boot_clock") or {} for gate in (first, last)]
            boots = [integer(c.get("time_boot_ms")) for c in clocks]
            monos = [integer(c.get("received_monotonic_ns")) for c in clocks]
            utcs = [integer(c.get("received_utc_us")) for c in clocks]
            bounds = capture.get("start_end_clock_bounds", {})
            observed = [integer(bounds.get(k)) for k in ("start_monotonic_ns", "end_monotonic_ns")]
            if (any(v is None for v in boots + monos + utcs + observed) or model["state"] != "supported_reported_clock"
                    or boots[1] < boots[0] or monos[1] < monos[0] or observed[1] < observed[0]):
                link["missing_evidence"].append("Comparable acquisition boot/monotonic bounds and a supported reference clock are required.")
                continue
            if abs((boots[1] - boots[0]) * 1000 - (monos[1] - monos[0]) / 1000) > CLOCK_TOLERANCE_US:
                link["contradictions"].append("Acquisition monotonic and boot-clock elapsed times disagree.")
                link["state"] = "rejected"
                continue
            offsets = model.get("receive_clock_offset_range_us")
            if not offsets:
                link["missing_evidence"].append("Reference ground receipt/FC clock bridge is unavailable.")
                continue
            gate_utc = [mapped(model, boot * 1000) for boot in boots]
            if any(value is None for value in gate_utc):
                link["missing_evidence"].append("Acquisition boot times lie outside reference anchor support; clock extrapolation is prohibited.")
                continue
            if any(not min(offsets) - MAX_GAP_US <= utc - estimate <= max(offsets) + MAX_GAP_US for utc, estimate in zip(utcs, gate_utc)):
                link["contradictions"].append("Acquisition receipt/boot evidence contradicts this reference boot epoch.")
                link["state"] = "rejected"
                continue
            if not monos[0] - MAX_GAP_US * 1000 <= observed[0] <= observed[1] <= monos[1] + MAX_GAP_US * 1000:
                link["missing_evidence"].append("Parameter receipt bounds are not bracketed by the bounded acquisition clock evidence.")
                continue
            parameter_boot = [boots[0] * 1000 + (observed[0] - monos[0]) / 1000,
                              boots[1] * 1000 + (observed[1] - monos[1]) / 1000]
            parameter_utc = [mapped(model, value) for value in parameter_boot]
            if any(value is None for value in parameter_utc) or parameter_utc[1] < parameter_utc[0]:
                link["missing_evidence"].append("Parameter observation bounds lack supported clock membership.")
                continue
            uncertainty = model["uncertainty_us"] + MAX_GAP_US
            interval_range = [parameter_utc[0] - uncertainty, parameter_utc[1] + uncertainty]
            link["observation_range_utc_us"] = interval_range
            link["uncertainty_basis"] = "reference residual allowance plus bounded 2 s acquisition/receipt-clock allowance"
            intervals = [i for i in reference["armed_intervals"] if i["complete_boundaries"]]
            for index, interval in enumerate(intervals):
                previous = intervals[index - 1] if index else None
                following = intervals[index + 1] if index + 1 < len(intervals) else None
                if (0 <= interval["arm_range_us"][0] - interval_range[1]
                        and interval["arm_range_us"][1] - interval_range[0] <= 60_000_000
                        and (previous is None or interval_range[0] > previous["disarm_range_us"][1])):
                    link["matches"].append({"reference_interval_index": interval["index"], "role": "preflight",
                                            "freshness_upper_bound_s": (interval["arm_range_us"][1] - interval_range[0]) / 1e6})
                if (0 <= interval_range[0] - interval["disarm_range_us"][1]
                        and interval_range[1] - interval["disarm_range_us"][0] <= 60_000_000
                        and (following is None or interval_range[1] < following["arm_range_us"][0])):
                    link["matches"].append({"reference_interval_index": interval["index"], "role": "postflight"})
            if not link["matches"]:
                link["missing_evidence"].append("Readback cannot be placed wholly before/after an armed interval inside the 60 s policy.")
            if acquisition.get("state") != "complete" or capture.get("completeness") != "complete_for_scope" or capture.get("consistency") != "no_observed_conflict":
                link["missing_evidence"].append("Readback is partial or has unresolved enumeration consistency.")
            sources = [s for s in acquisition.get("source_refs", []) if s.get("role") == "parameter_readback_snapshot"]
            if len(sources) != 1:
                link["missing_evidence"].append("One immutable rich snapshot original is required.")
                continue
            source = sources[0]
            path = self.store.raw_path(source["sha256"])
            if path.stat().st_size > 16 * 1024 * 1024 or hash_file(path, cancel_check) != source["sha256"]:
                link["contradictions"].append("Snapshot original is too large or fails its recorded SHA-256.")
                link["state"] = "rejected"
                continue
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if (snapshot.get("completeness") != capture.get("completeness") or snapshot.get("consistency") != capture.get("consistency")
                    or snapshot.get("component_scope") != {"system_id": connection.get("system_id"), "component_id": connection.get("component_id")}):
                link["contradictions"].append("Rich snapshot completeness/consistency/component scope contradicts its acquisition manifest.")
                link["state"] = "rejected"
                continue
            values = snapshot.get("values", {})
            if not isinstance(values, dict) or len(values) > MAX_PARAMETERS:
                link["missing_evidence"].append("Snapshot parameter set exceeds supported bounds.")
                continue
            link["snapshot_id"] = snapshot.get("id")
            link["parameter_count"] = len(values)
            link["value_comparison"] = self._compare_values({name: row.get("canonical_value") for name, row in values.items() if isinstance(row, dict)}, timeline)
            if link["matches"] and not link["missing_evidence"]:
                link["state"] = "identity_and_temporal_rule_supported"
            link["effective_from"] = None
        with self.store._connect() as db:
            references = db.execute("SELECT data FROM documents WHERE kind='parameter_reference' ORDER BY id LIMIT 65").fetchall()
        exports = []
        for row in references[:64]:
            check_cancel(cancel_check)
            value = json.loads(row[0])
            intact = hash_file(self.store.raw_path(value["source"]["sha256"]), cancel_check) == value["source"]["sha256"]
            exports.append({"parameter_reference_id": value["id"], "source_sha256": value["source"]["sha256"], "synthetic": value.get("synthetic", False),
                            "canonical_hash": value["canonical_hash"], "state": "reference_only",
                            "identity": "unknown", "capture_time": "unknown", "flight_time_applicability": "unknown",
                            "original_integrity": "verified_sha256" if intact else "failed",
                            "value_comparison": self._compare_values({name: row["value"] for name, row in value["parameters"].items()}, timeline) if intact else None})
        return {"acquisitions": links, "exports": exports, "exports_truncated": len(references) > 64,
                "automatic_effective_configuration": False,
                "limitations": ["Temporal role is inferred from evidence bounds, never the operator's pre/post label.",
                                "Value matches link observations for review; parameter exports alone cannot establish flight membership.",
                                "Even matching complete pre/post readbacks cannot prove no unbroadcast edit or delayed activation occurred."]}

    @staticmethod
    def _compare_values(values, timeline):
        observed = {}
        for row in timeline["observations"]:
            observed.setdefault(row["name"], set()).add(Decimal(row["value"]))
        matched = different = missing = 0
        examples = []
        for name, raw in values.items():
            try:
                value = Decimal(str(raw))
                if not value.is_finite():
                    continue
            except InvalidOperation:
                continue
            if name not in observed:
                missing += 1
            elif value in observed[name]:
                matched += 1
            else:
                different += 1
                if len(examples) < 32:
                    examples.append({"name": name, "reference_value": str(raw), "observed_values": sorted(str(v) for v in observed[name])[:8]})
        return {"matching_observed_value_count": matched, "different_representation_count": different,
                "not_observed_count": missing, "difference_examples": examples,
                "basis": "decimal representation comparison; not proof of identical effective settings or flight identity"}

    @staticmethod
    def _timeline(reference, candidates):
        observations = []
        for value in [reference, *candidates]:
            model = clock_model(value.anchors)
            for row in value.parameters:
                stamp = value.time(row, model)
                observations.append({k: row[k] for k in ("name", "value", "reported_type", "evidence")} |
                                    {"selection": value.selection, "source_sha256": value.log["source"]["sha256"],
                                     "observed_at": stamp, "effective_from": None, "effective_time_basis": "unknown_firmware_activation_semantics"})
        observations.sort(key=lambda row: (row["name"], (row["observed_at"] or {}).get("utc_us", float("inf")), key(row["selection"])))
        changes, previous = [], {}
        for row in observations:
            scope = key(row["selection"]), row["name"]
            old = previous.get(scope)
            if old and Decimal(old["value"]) != Decimal(row["value"]):
                changes.append({"name": row["name"], "selection": row["selection"], "before": old["value"], "after": row["value"],
                                "unknown_change_window": [old["observed_at"], row["observed_at"]],
                                "effective_from": None, "activation_uncertain": True})
            previous[scope] = row
        return {"state": "observed_evidence_only", "observations": observations[:MAX_PARAMETERS],
                "observation_count": len(observations), "truncated": len(observations) > MAX_PARAMETERS,
                "changes": changes[:1024], "change_count": len(changes), "attribution_allowed": False,
                "completeness": "not_established", "lineage_basis": "immutable original hash and decoded source/clock/record",
                "limitations": ["Matching observations do not prove every setting or edit was observed.",
                                "Different decimal representations are not proof of different effective binary values.",
                                "No export or postflight setting is assigned retroactively to flight time."]}

    def attach(self, params, cancel_check=None, progress=None):
        with self.collection._lock, self.store._connect() as db:
            record = self.collection._get(db, "flight_record", params.get("flight_record_id"))
            capture = self.collection._get(db, "capture", record["capture_session_id"])
            prior = self.collection._operation(db, "alignment.attach", params, capture["profile"])
            if prior[2]:
                return prior[2]
        saved = self.store.get_document("alignment_report", _text(params.get("report_id"), "report_id"))
        flight_id = _text(params.get("flight_record_id"), "flight_record_id")
        if saved["inputs"].get("flight_record_id") not in (None, flight_id):
            raise ValueError("Alignment report belongs to a different selected flight.")
        # Never treat a stored report as authority. Re-read immutable originals,
        # parser artifacts and the current complete bounded candidate universe.
        fresh = self.analyze(saved["inputs"], cancel_check, progress, persist=False)
        return self.collection.attach_alignment(params, fresh, saved["id"], self._catalog)
