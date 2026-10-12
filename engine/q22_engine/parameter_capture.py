"""Bounded passive parameter-enumeration evidence; no transport or aircraft writes.

Collection completeness, observed consistency, freshness and flight-time
applicability are separate. Count-complete enumeration is never atomic and
never proves historical subsystem activation. Original recorder references
remain necessary to reconstruct every retransmission or conflicting packet.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import struct
from uuid import uuid4


ALGORITHM = "q22-passive-parameter-capture/1"
MAX_PARAMETERS = 50_000
MAX_EXAMPLES = 32
NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
INTEGER_TYPES = {1: ("UINT8", 0, 2**8 - 1), 2: ("INT8", -2**7, 2**7 - 1),
                 3: ("UINT16", 0, 2**16 - 1), 4: ("INT16", -2**15, 2**15 - 1),
                 5: ("UINT32", 0, 2**32 - 1), 6: ("INT32", -2**31, 2**31 - 1),
                 7: ("UINT64", 0, 2**64 - 1), 8: ("INT64", -2**63, 2**63 - 1)}
TYPE_NAMES = {**{name: code for code, (name, _, _) in INTEGER_TYPES.items()}, "REAL32": 9, "REAL64": 10}
ENCODINGS = {"mavlink_c_cast", "mavlink_bytewise_decoded", "typed_numeric",
             "ArduPilot C-cast, heartbeat-identified"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _bounded_copy(value, label, maximum=2048):
    try:
        text = _json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} must be bounded JSON data.") from exc
    if len(text.encode()) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes.")
    return json.loads(text)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer.")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        value = int(value)
    if not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}.")
    return value


def _raw_numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Parameter value must be numeric data, not a boolean or object.")
    text = str(value)
    if len(text) > 256:
        raise ValueError("Parameter value exceeds 256 characters.")
    return {"kind": type(value).__name__, "text": text,
            **({"python_float_hex": value.hex()} if isinstance(value, float) else {})}


def _type_code(value):
    if isinstance(value, str):
        value = TYPE_NAMES.get(value.removeprefix("MAV_PARAM_TYPE_"), value)
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10 else None


def _known_encoding(provenance):
    if isinstance(provenance, str):
        return provenance in ENCODINGS
    return (isinstance(provenance, dict) and provenance.get("validated") is True
            and provenance.get("encoding") in ENCODINGS)


def _decode(value, type_code, encoding):
    if type_code is None:
        return None, "unknown_parameter_type"
    if type_code != 9 and not _known_encoding(encoding):
        return None, "unknown_parameter_encoding"
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None, "invalid_parameter_value"
    if not number.is_finite():
        return None, "nonfinite_parameter_value"
    if type_code in INTEGER_TYPES:
        label, minimum, maximum = INTEGER_TYPES[type_code]
        if number != number.to_integral_value() or not minimum <= number <= maximum:
            return None, "integer_value_out_of_range_or_fractional"
        # An already-rounded binary64 value is not lossless uint64/int64 evidence.
        if isinstance(value, float) and abs(value) > 2**53:
            return None, "integer_float_precision_unverifiable"
        canonical = str(int(number))
        return {"decoded_type": label, "canonical_value": canonical,
                "canonical_token": f"integer:{canonical}"}, None
    try:
        converted = float(number)
        encoded = struct.pack(">f" if type_code == 9 else ">d", converted)
        decoded = struct.unpack(">f" if type_code == 9 else ">d", encoded)[0]
    except (OverflowError, ValueError, struct.error):
        return None, "real_value_out_of_range"
    if not math.isfinite(decoded):
        return None, "real_value_out_of_range"
    return {"decoded_type": "REAL32" if type_code == 9 else "REAL64",
            "canonical_value": str(Decimal.from_float(decoded)),
            "canonical_token": f"ieee754:{encoded.hex()}"}, None


class ParameterAccumulator:
    """One aircraft/boot/component acquisition generation.

    `observe` returns an acceptance diagnostic. `summary` excludes parameter
    values and bounds samples of missing indexes, references and conflicts.
    `snapshot` materializes detached evidence, including all bounded values.
    `finalize` seals the generation; subsequent observations are rejected.

    Per-name first/latest observations preserve receipt clocks and original
    references. Identical retransmissions are deduplicated; complete packet
    history belongs to the caller's immutable recorder, not this reducer.
    """

    def __init__(self, aircraft_id, boot_epoch, component_scope, firmware_identity=None):
        if not isinstance(aircraft_id, str) or not aircraft_id.strip() or len(aircraft_id) > 512:
            raise ValueError("aircraft_id requires a nonempty identifier of at most 512 characters.")
        if (isinstance(boot_epoch, bool) or not isinstance(boot_epoch, (str, int))
                or len(str(boot_epoch)) > 128 or (isinstance(boot_epoch, str) and not boot_epoch.strip())
                or (isinstance(boot_epoch, int) and boot_epoch < 0)):
            raise ValueError("boot_epoch requires a bounded explicit identifier.")
        if isinstance(component_scope, int) and not isinstance(component_scope, bool):
            component_scope = {"component_id": component_scope}
        if not isinstance(component_scope, dict) or set(component_scope) - {"system_id", "component_id"}:
            raise ValueError("component_scope must identify exactly one component, optionally with system_id.")
        self.component_scope = {"component_id": _integer(component_scope.get("component_id"), "component_id", 1, 255)}
        if component_scope.get("system_id") is not None:
            self.component_scope["system_id"] = _integer(component_scope["system_id"], "system_id", 1, 255)
        self.aircraft_id, self.boot_epoch = aircraft_id, boot_epoch
        self.firmware_identity = _bounded_copy(firmware_identity, "firmware_identity")
        self.id = str(uuid4())
        self._values, self._indices, self._name_indices, self._counts = {}, {}, {}, set()
        self._issues, self._examples, self._raw_refs = {}, [], []
        self._change_ids = []
        self._observation_count = self._duplicates = 0
        self._start_mono = self._end_mono = self._start_utc = self._end_utc = None
        self._last_mono = None
        self._clock_regressions = 0
        self._sealed, self._role, self._snapshot_id = False, "unknown", None

    def _issue(self, code, name, index, raw_ref):
        self._issues[code] = self._issues.get(code, 0) + 1
        if len(self._examples) < MAX_EXAMPLES:
            self._examples.append({"reason": code, "name": name, "index": index, "source_ref": raw_ref})

    def observe(self, event):
        if self._sealed:
            raise ValueError("Parameter capture is finalized; begin a new acquisition generation.")
        if not isinstance(event, dict):
            raise ValueError("Parameter observation must be an object.")
        name = event.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("Invalid parameter name.")
        if name not in self._values and len(self._values) >= MAX_PARAMETERS:
            raise ValueError("Parameter capture exceeds the 50,000-name limit.")
        for key, expected in (("aircraft_id", self.aircraft_id), ("boot_epoch", self.boot_epoch),
                              ("component_id", self.component_scope["component_id"])):
            supplied = event.get(key, event.get("component") if key == "component_id" else None)
            if key in {"boot_epoch", "component_id"} and isinstance(supplied, bool):
                raise ValueError(f"Observation {key} must not be boolean.")
            if supplied is not None and supplied != expected:
                raise ValueError(f"Observation {key} differs from this capture generation.")
        if event.get("system_id") is not None:
            if isinstance(event["system_id"], bool) or self.component_scope.get("system_id") != event["system_id"]:
                raise ValueError("Observation system_id is outside the declared component scope.")
        if event.get("firmware_identity") is not None and event["firmware_identity"] != self.firmware_identity:
            raise ValueError("Observation firmware_identity differs from this acquisition generation.")
        raw_value = _raw_numeric(event.get("value"))
        raw_ref = _bounded_copy(event.get("raw_ref"), "raw_ref")
        encoding = _bounded_copy(event.get("encoding_provenance", event.get("value_encoding")), "encoding_provenance")
        type_source = event.get("param_type", event.get("type", event.get("mav_param_type")))
        type_code = _type_code(type_source)
        # Unknown type labels are retained within a bounded representation.
        type_source = _bounded_copy(type_source, "parameter type", 128)
        mono, utc = event.get("received_monotonic_ns"), event.get("received_utc_us")
        mono = None if mono is None else _integer(mono, "received_monotonic_ns", 0, 2**64 - 1)
        utc = None if utc is None else _integer(utc, "received_utc_us", 0, 2**64 - 1)
        raw_count = event.get("count", event.get("parameter_count"))
        raw_index = event.get("index", event.get("parameter_index"))
        count = None if raw_count is None else _integer(raw_count, "parameter_count", 1, MAX_PARAMETERS)
        index = None if raw_index is None else _integer(raw_index, "parameter_index", 0, MAX_PARAMETERS - 1)
        decoded, issue = _decode(event["value"], type_code, encoding)
        observed = {"received_monotonic_ns": str(mono) if mono is not None else None,
                    "received_utc_us": str(utc) if utc is not None else None}
        observation = {"id": str(uuid4()), "aircraft_identity": self.aircraft_id, "boot_epoch": self.boot_epoch,
                       "component": deepcopy(self.component_scope), "name": name,
                       "transport_type": "normalized_passive_parameter_observation",
                       "parameter_index": index, "parameter_count": count,
                       "mav_param_type": type_code, "reported_type": type_source,
                       "type_provenance": "declared_in_observation" if type_code else "unknown",
                       "raw_value": raw_value, "canonical_value": None, "decoded_type": None,
                       "canonical_token": None, "source_ref": raw_ref, "observed_at": observed,
                       "effective_time_bounds": {"earliest": None, "latest": None, "basis": "unknown"},
                       "reboot_requirement": "unknown", "encoding_provenance": encoding or ("MAVLink REAL32 wire representation" if type_code == 9 else None),
                       "capture_id_or_null": self.id, **(decoded or {})}
        self._observation_count += 1
        for value, first_attr, last_attr in ((mono, "_start_mono", "_end_mono"), (utc, "_start_utc", "_end_utc")):
            if value is not None:
                first, last = getattr(self, first_attr), getattr(self, last_attr)
                setattr(self, first_attr, value if first is None else min(first, value))
                setattr(self, last_attr, value if last is None else max(last, value))
        if mono is not None:
            if self._last_mono is not None and mono < self._last_mono:
                self._clock_regressions += 1
            self._last_mono = mono
        if raw_ref is not None and raw_ref not in self._raw_refs and len(self._raw_refs) < MAX_EXAMPLES:
            self._raw_refs.append(raw_ref)
        if count is not None:
            self._counts.add(count)
            if len(self._counts) > 1:
                self._issue("changing_parameter_count", name, index, raw_ref)
        if index is None or count is None:
            self._issue("missing_enumeration_index_or_count", name, index, raw_ref)
        elif index >= count:
            self._issue("index_outside_reported_count", name, index, raw_ref)
        else:
            if index in self._indices and self._indices[index] != name:
                self._issue("conflicting_index_to_name", name, index, raw_ref)
            if name in self._name_indices and self._name_indices[name] != index:
                self._issue("conflicting_name_to_index", name, index, raw_ref)
            self._indices.setdefault(index, name)
            self._name_indices.setdefault(name, index)
        if issue:
            self._issue(issue, name, index, raw_ref)
        previous = self._values.get(name)
        duplicate = False
        if previous is None:
            self._values[name] = {"first_observation": observation, "latest_observation": observation,
                                  "retransmission_count": 0}
        else:
            first = previous["first_observation"]
            token = (type_code, observation["canonical_token"], index, count)
            old_token = (first["mav_param_type"], first["canonical_token"], first["parameter_index"], first["parameter_count"])
            if decoded is not None and token == old_token:
                self._duplicates += 1
                previous["retransmission_count"] += 1
                duplicate = True
            elif (first["canonical_token"] is not None and decoded is not None
                  and (first["canonical_token"] != observation["canonical_token"] or first["mav_param_type"] != type_code)):
                self._issue("conflicting_parameter_value_or_type", name, index, raw_ref)
                if decoded is not None and len(self._change_ids) < MAX_EXAMPLES:
                    self._change_ids.append(observation["id"])
            previous["latest_observation"] = observation
        return {"accepted": issue is None, "duplicate": duplicate, "reason": issue,
                "observation": deepcopy(observation)}

    def _evidence(self):
        expected = next(iter(self._counts)) if len(self._counts) == 1 else None
        valid = {name: item["first_observation"] for name, item in self._values.items()
                 if item["first_observation"]["canonical_token"] is not None}
        missing = [] if expected is None else [index for index in range(expected)
                  if index not in self._indices or self._indices[index] not in valid]
        complete = expected is not None and len(valid) == expected and not missing and not self._issues
        completeness = "complete_for_scope" if complete else "partial" if self._values else "unknown"
        conflicts = any(code.startswith("conflicting_") or code == "changing_parameter_count" for code in self._issues)
        consistency = "conflict" if conflicts else "unknown" if self._issues or not self._values else "no_observed_conflict"
        typed_rows = [[self.component_scope, name, entry["mav_param_type"], entry["canonical_token"]]
                      for name, entry in sorted(valid.items())]
        typed_hash = _hash({"canonicalization": "component-name-type-ieee-bits-or-integer/1", "values": typed_rows})
        scope_hash = _hash({"component_scope": self.component_scope, "completeness": completeness,
                            "reported_counts": sorted(self._counts), "names": sorted(self._values),
                            "decoded_names": sorted(valid), "consistency": consistency})
        return expected, valid, missing, completeness, consistency, typed_hash, scope_hash

    def summary(self):
        expected, valid, missing, completeness, consistency, typed_hash, scope_hash = self._evidence()
        bounds = {"start_monotonic_ns": None if self._start_mono is None else str(self._start_mono),
                  "end_monotonic_ns": None if self._end_mono is None else str(self._end_mono),
                  "start_utc_us": None if self._start_utc is None else str(self._start_utc),
                  "end_utc_us": None if self._end_utc is None else str(self._end_utc)}
        return deepcopy({"id": self.id, "schema_version": 1, "algorithm_version": ALGORITHM,
            "aircraft_id": self.aircraft_id, "boot_epoch": self.boot_epoch,
            "component_scope": self.component_scope, "firmware_identity": self.firmware_identity,
            "start_end_clock_bounds": bounds, "transport": "passive_normalized_observations",
            "acquisition_basis": "passive_enumeration_observed", "capture_role": self._role,
            "expected_count": expected, "decoded_count": len(valid), "observed_name_count": len(self._values),
            "observation_count": self._observation_count, "duplicate_retransmission_count": self._duplicates,
            "reported_counts": sorted(self._counts)[:MAX_EXAMPLES], "reported_count_variants": len(self._counts),
            "missing_index_count": len(missing), "missing_index_examples": missing[:MAX_EXAMPLES],
            "completeness": completeness, "consistency": consistency, "freshness": "unknown",
            "flight_time_applicability": "provisional" if completeness == "complete_for_scope" and self._role == "preflight" else "unknown",
            "atomic": False, "full_readback_requested": False, "preflight_full_request": "unavailable",
            "raw_refs": self._raw_refs, "raw_refs_are_bounded_examples": True,
            "typed_hash": typed_hash, "scope_hash": scope_hash,
            "observed_change_ids": self._change_ids, "observed_change_ids_are_bounded_examples": True,
            "snapshot_id": self._snapshot_id,
            "issues": self._issues, "issue_examples": self._examples,
            "monotonic_clock_regression_count": self._clock_regressions, "finalized": self._sealed,
            "limitations": ["Passive index coverage does not prove a requested, atomic or fresh full readback.",
                            "No parameter/log transport or packed-parameter FTP adapter is implemented here.",
                            "Receipt time does not establish effective activation; firmware/reboot applicability is unknown.",
                            "All source packets and conflicts must remain in the caller's immutable recorder; this reducer retains bounded examples and first/latest observations."]})

    def snapshot(self):
        summary = self.summary()
        values = {name: {**deepcopy(item["first_observation"]),
                         "latest_observation": deepcopy(item["latest_observation"]),
                         "retransmission_count": item["retransmission_count"]}
                  for name, item in sorted(self._values.items())}
        return {"id": self._snapshot_id or str(uuid4()), "schema_version": 1, "kind": "captured",
                "aircraft_identity": self.aircraft_id, "firmware_identity": deepcopy(self.firmware_identity),
                "boot_epoch": self.boot_epoch, "component_scope": deepcopy(self.component_scope),
                "completeness": summary["completeness"], "consistency": summary["consistency"],
                "values": values, "raw_refs": deepcopy(self._raw_refs),
                "capture_id_or_reconstruction_evidence": self.id,
                "typed_hash": summary["typed_hash"], "scope_hash": summary["scope_hash"],
                "atomic": False, "flight_time_applicability": summary["flight_time_applicability"],
                "effective_time_bounds": {"earliest": None, "latest": None, "basis": "unknown"},
                "start_end_clock_bounds": summary["start_end_clock_bounds"], "immutable": True}

    def finalize(self, role="unknown", snapshot_id=None):
        if role not in {"preflight", "postflight", "inflight", "unknown"}:
            raise ValueError("capture role must be preflight, postflight, inflight or unknown.")
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 512):
            raise ValueError("snapshot_id must be a bounded nonempty string.")
        if self._sealed:
            if role != self._role or snapshot_id not in (None, self._snapshot_id):
                raise ValueError("Finalized capture role and snapshot reference cannot change.")
            return self.summary()
        self._sealed, self._role, self._snapshot_id = True, role, snapshot_id or str(uuid4())
        return self.summary()
