"""Preserve requested readbacks and link their observations to exact flights.

Count-complete readback is not atomic or proof of effective in-flight settings.
Every catalog value remains traceable to an immutable rich acquisition original.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from uuid import uuid4

from .alignment import AlignmentService
from .insight_context import _CollectionReader
from .logs import check_cancel, hash_file
from .parameter_capture import MAX_PARAMETERS, NAME, _decode, _hash
from .parameters import snapshot_summary, text_field


VERSION = "q22-preserved-flight-readbacks/1"
MAX_RICH_BYTES = 16 * 1024 * 1024
MAX_LINKED_READBACKS = 16
MAX_DIFF_ROWS = 1000
MAX_DIFF_BYTES = 1024 * 1024
SCOPE_KEYS = ("log_id", "source_sha256", "source_key", "boot_epoch", "interval_start_s", "interval_end_s")
LIMITATIONS = [
    "A complete readback covers the reported parameter enumeration for one component; it is not atomic or proof of a complete effective flight configuration.",
    "Stored values, their activation times and reboot requirements are different evidence. Activation and reboot applicability remain unknown.",
    "Even matching full pre/postflight readbacks cannot exclude an unbroadcast edit or delayed activation during flight.",
    "Reported UID and firmware are not authenticated hardware identity; parameter differences alone do not establish performance causation.",
]


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _integer(value):
    return value if type(value) is int else None


def _decoded_row(row, name, rich):
    _require(isinstance(row, dict) and row.get("name") == name and row.get("component") == rich["component_scope"]
             and row.get("aircraft_identity") == rich["aircraft_identity"] and row.get("boot_epoch") == rich["boot_epoch"]
             and row.get("capture_id_or_null") == rich["capture_id_or_reconstruction_evidence"],
             "Rich parameter observation contradicts its name or acquisition scope.")
    raw = row.get("raw_value", {})
    text = raw.get("text")
    _require(isinstance(text, str) and len(text) <= 256, "Rich parameter raw numeric evidence is missing or too long.")
    kind = raw.get("kind")
    _require(kind in {"str", "int", "float", "Decimal"}, "Unsupported rich parameter raw representation.")
    value = {"str": str, "int": int, "float": float, "Decimal": Decimal}[kind](text)
    if kind == "float" and raw.get("python_float_hex") is not None:
        _require(value.hex() == raw["python_float_hex"], "Rich floating-point raw evidence disagrees.")
    decoded, _ = _decode(value, row.get("mav_param_type"), row.get("encoding_provenance"))
    if decoded is None:
        _require(all(row.get(key) is None for key in ("canonical_value", "canonical_token", "decoded_type")),
                 "An undecodable parameter was assigned a canonical value.")
        return None
    _require(all(row.get(key) == decoded[key] for key in decoded), "Parameter type, canonical value and exact token disagree.")
    return decoded


def _readback(store, acquisition_id, cancel_check=None, progress=None):
    check_cancel(cancel_check)
    acquisition = store.get_document("acquisition", acquisition_id)
    _require(acquisition.get("kind") == "parameters", "Select a parameter readback acquisition.")
    sources = acquisition.get("source_refs", [])
    _require(isinstance(sources, list) and len(sources) <= 32, "Acquisition source manifest is invalid or exceeds supported bounds.")
    rich_sources = [s for s in sources if s.get("role") == "parameter_readback_snapshot"]
    _require(len(rich_sources) == 1, "One archived rich parameter snapshot original is required; retained temporary files are insufficient.")
    source = rich_sources[0]
    for index, original in enumerate(sources):
        check_cancel(cancel_check)
        if progress:
            progress("verifying_readback_originals", index, len(sources))
        path = store.raw_path(original["sha256"])
        _require(hash_file(path, cancel_check) == original["sha256"], "Acquisition original failed SHA-256 integrity verification.")
    check_cancel(cancel_check)
    if progress:
        progress("verifying_readback_originals", len(sources), len(sources))
    path = store.raw_path(source["sha256"])
    _require(path.stat().st_size <= MAX_RICH_BYTES, "Rich parameter snapshot exceeds 16 MiB.")
    rich = json.loads(path.read_text(encoding="utf-8"))
    capture = acquisition.get("parameter_capture", {})
    connection = acquisition.get("connection", {})
    _require(isinstance(rich, dict) and rich.get("schema_version") == 1 and rich.get("kind") == "captured", "Unsupported rich parameter snapshot schema.")
    _require(rich.get("component_scope") == {"system_id": connection.get("system_id"), "component_id": connection.get("component_id")},
             "Rich parameter component scope contradicts its acquisition.")
    _require(all(rich.get(key) == capture.get(key) for key in (
        "boot_epoch", "component_scope", "firmware_identity", "completeness", "consistency", "typed_hash", "scope_hash", "start_end_clock_bounds"))
        and rich.get("id") == capture.get("snapshot_id")
        and rich.get("capture_id_or_reconstruction_evidence") == capture.get("id")
        and rich.get("aircraft_identity") == capture.get("aircraft_id") == acquisition.get("aircraft_id"),
        "Rich snapshot identity, counts or capture evidence disagree with the acquisition manifest.")
    values = rich.get("values")
    _require(isinstance(values, dict) and len(values) <= MAX_PARAMETERS, "Rich parameter set exceeds the supported bound.")
    parameters, indices, counts, row_conflicts = {}, [], set(), False
    for index, (name, row) in enumerate(sorted(values.items())):
        check_cancel(cancel_check)
        if progress and index % 256 == 0:
            progress("verifying_typed_parameters", index, len(values))
        _require(isinstance(name, str) and NAME.fullmatch(name), "Invalid rich parameter name.")
        decoded = _decoded_row(row, name, rich)
        latest = row.get("latest_observation")
        latest_decoded = _decoded_row(latest, name, rich)
        row_conflicts |= decoded != latest_decoded or any(row.get(key) != latest.get(key) for key in (
            "mav_param_type", "parameter_index", "parameter_count"))
        index, count = _integer(row.get("parameter_index")), _integer(row.get("parameter_count"))
        if count is not None:
            counts.add(count)
        if decoded:
            indices.append(index)
            parameters[name] = {"value": decoded["canonical_value"], "canonical_token": decoded["canonical_token"],
                "mav_param_type": row["mav_param_type"], "decoded_type": decoded["decoded_type"],
                "parameter_index": index, "observed_at": deepcopy(row.get("observed_at")),
                "effective_time_bounds": {"earliest": None, "latest": None, "basis": "unknown"}, "reboot_requirement": "unknown"}
    typed_hash = _hash({"canonicalization": "component-name-type-ieee-bits-or-integer/1", "values": [
        [rich["component_scope"], name, value["mav_param_type"], value["canonical_token"]] for name, value in sorted(parameters.items())]})
    check_cancel(cancel_check)
    if progress:
        progress("verifying_typed_parameters", len(values), len(values))
    _require(typed_hash == rich.get("typed_hash"), "Rich parameter typed content hash failed verification.")
    _require(capture.get("decoded_count") == len(parameters) and capture.get("observed_name_count") == len(values),
             "Acquisition decoded/name counts disagree with the rich snapshot.")
    transport = acquisition.get("transport_evidence", {})
    gates = [transport.get(key) or {} for key in ("initial_gate_observations", "final_gate_observations")]
    identities = [gate.get("identity") or {} for gate in gates]
    uid = str(identities[0].get("uid", ""))
    firmware = rich.get("firmware_identity")
    identity_supported = (uid not in {"", "0", "None"} and all(str(i.get("uid")) == uid and i.get("firmware_identity") == firmware for i in identities)
        and isinstance(firmware, dict) and all(key in firmware for key in ("flight_sw_version", "board_version", "vendor_id", "product_id"))
        and connection.get("expected_uid", uid) == uid)
    expected = _integer(capture.get("expected_count"))
    index_complete = (expected is not None and 1 <= expected <= MAX_PARAMETERS and len(parameters) == len(values) == expected
                      and counts == {expected} and all(i is not None for i in indices) and sorted(indices) == list(range(expected)))
    _require(capture.get("completeness") != "complete_for_scope" or index_complete,
             "A claimed complete snapshot does not contain the exact full reported index range.")
    if capture.get("completeness") == "complete_for_scope" and capture.get("consistency") == "no_observed_conflict":
        expected_scope_hash = _hash({"component_scope": rich["component_scope"], "completeness": "complete_for_scope",
            "reported_counts": sorted(counts), "names": sorted(values), "decoded_names": sorted(parameters), "consistency": "no_observed_conflict"})
        _require(expected_scope_hash == rich["scope_hash"], "Complete parameter scope hash failed verification.")
    transcript = [s for s in sources if s.get("role") == "mavlink_acquisition_transcript"]
    transcript_supported = len(transcript) == 1 and transcript[0]["sha256"] == transport.get("trace_sha256")
    bounds = rich["start_end_clock_bounds"]
    try:
        mono = [int(bounds[key]) for key in ("start_monotonic_ns", "end_monotonic_ns")]
        utc = [int(bounds[key]) for key in ("start_utc_us", "end_utc_us")]
        clock_bounds_supported = 0 <= mono[0] <= mono[1] and 0 <= utc[0] <= utc[1]
    except (ValueError, TypeError, KeyError):
        clock_bounds_supported = False
    reasons = []
    for supported, reason in (
        (acquisition.get("state") == "complete", "Acquisition did not complete."),
        (capture.get("completeness") == "complete_for_scope" and capture.get("consistency") == "no_observed_conflict", "Readback is partial, uncertain or has observed conflicts."),
        (index_complete, "Decoded values do not cover exactly one complete reported index range."),
        (not row_conflicts and not capture.get("issues") and not capture.get("semantic_errors") and not rich.get("semantic_errors")
         and not capture.get("interruption_reason") and not rich.get("interruption_reason") and not transport.get("gate_interruption_reason")
         and capture.get("monotonic_clock_regression_count") == 0, "Readback contains decoding, change, clock-regression or interruption evidence."),
        (capture.get("full_readback_requested") is True and capture.get("acquisition_basis") == rich.get("acquisition_basis") == "explicit_requested_enumeration", "A complete requested enumeration is not established."),
        (identity_supported, "Matching initial/final reported UID and firmware are unavailable."),
        (all((gate.get("heartbeat") or {}).get("armed") is False for gate in gates), "Disarmed initial/final acquisition gates are unavailable."),
        (transcript_supported, "The archived acquisition transcript does not match its recorded digest."),
        (clock_bounds_supported, "Complete monotonic and UTC receipt bounds are unavailable."),
    ):
        if not supported:
            reasons.append(reason)
    _require(isinstance(acquisition.get("synthetic"), bool), "Acquisition synthetic provenance must be explicit.")
    try:
        observed_at = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(bounds["end_utc_us"]))).isoformat()
    except (ValueError, TypeError, OverflowError):
        observed_at = None
        reasons.append("Readback receipt time is unavailable.")
    evidence = {key: deepcopy(capture.get(key)) for key in (
        "expected_count", "decoded_count", "observed_name_count", "completeness", "consistency", "start_end_clock_bounds",
        "observed_change_ids", "issues", "semantic_errors", "interruption_reason", "capture_role", "full_readback_requested")}
    return {"algorithm_version": VERSION, "aircraft_id": acquisition["aircraft_id"],
        "board": "reported_board_version:" + str((firmware or {}).get("board_version", "unknown")),
        "firmware": json.dumps(firmware, sort_keys=True, separators=(",", ":")),
        "firmware_identity": deepcopy(firmware), "hardware_uid": uid if identity_supported else None,
        "label": "Requested parameter readback · " + acquisition_id[:8], "source_kind": "current_readback", "observed_at": observed_at,
        "identity_verification": "reported_initial_final_UID_and_firmware_not_authenticated" if identity_supported else "unknown",
        "source": source, "source_refs": deepcopy(sources), "synthetic": acquisition["synthetic"], "immutable": True,
        "acquisition_id": acquisition_id, "rich_snapshot_id": rich["id"], "capture_id": capture["id"],
        "component_scope": rich["component_scope"], "boot_epoch": rich["boot_epoch"], "typed_hash": typed_hash,
        "scope_hash": rich["scope_hash"], "parameters": parameters, "parameter_count": len(parameters),
        "completeness": capture["completeness"], "consistency": capture["consistency"], "readback_evidence": evidence,
        "freshness": "historical_observation_at_recorded_receipt_time",
        "usable_for_full_comparison": not reasons, "unusable_reasons": reasons,
        "atomic": False, "effective_configuration": "unknown", "flight_time_applicability": "unknown",
        "limitations": list(LIMITATIONS)}


class FlightConfigurationService:
    def __init__(self, store):
        self.store = store

    def promote(self, params, *, cancel_check=None, progress=None):
        acquisition_id = text_field(params, "acquisition_id")
        snapshot = _readback(self.store, acquisition_id, cancel_check, progress)
        check_cancel(cancel_check)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_cancel(cancel_check)
            rows = db.execute("SELECT data FROM documents WHERE kind='snapshot' AND json_extract(data,'$.acquisition_id')=? ORDER BY id LIMIT 2", (acquisition_id,)).fetchall()
            if rows:
                _require(len(rows) == 1, "Multiple catalog snapshots claim the same acquisition; review preserved evidence.")
                saved = json.loads(rows[0][0])
                _require(all(saved.get(key) == value for key, value in snapshot.items()), "Preserved catalog snapshot contradicts its verified readback original.")
                return {"snapshot": snapshot_summary(saved), "already_preserved": True}
            saved = {**snapshot, "id": str(uuid4()), "created_at": self.store._now(), "schema_version": 1}
            check_cancel(cancel_check)
            db.execute("INSERT INTO documents VALUES (?,?,?,?)", (saved["id"], "snapshot", saved["created_at"], json.dumps(saved, allow_nan=False, sort_keys=True)))
        return {"snapshot": snapshot_summary(saved), "already_preserved": False}


def _unavailable(reason):
    return {"status": "unavailable", "reason": reason, "preflight": [], "postflight": [],
            "effective_configuration": "unknown", "limitations": list(LIMITATIONS)}


def report_configuration(store, report, *, cancel_check=None):
    """Attach only fresh machine-linked readbacks; keep full values in catalog."""
    context = report.get("flight_context", {})
    if context.get("status") != "verified":
        return _unavailable("Exact managed-flight linkage is required to select historical readbacks.")
    try:
        scope = {key: report["scope"][key] for key in SCOPE_KEYS}
        linkage = context["linkage"]
        _require(linkage["scope"] == {key: scope[key] for key in SCOPE_KEYS if key != "log_id"}
                 and linkage["log_id"] == scope["log_id"], "Flight linkage does not cover this exact report.")
        saved = store.get_document("alignment_report", linkage["alignment_report_id"])
        alignment = AlignmentService(store, collection=_CollectionReader())
        fresh = alignment.analyze(saved["inputs"], cancel_check=cancel_check, persist=False)
        _require(fresh["evidence_hash"] == linkage["fresh_evidence_hash"], "Alignment evidence changed; generate a fresh report before attaching readbacks.")
        selected = {key: scope[key] for key in ("log_id", "source_key", "boot_epoch")}
        candidate = next(c for c in fresh["candidates"] if c["selection"] == selected and c["state"] == "verified")
        matches = [m for m in candidate["matches"] if m["candidate_record_span"] == linkage["record_span"]]
        _require(len(matches) == 1, "The report does not identify one exact aligned armed interval.")
        reference_index = matches[0]["reference_interval_index"]
        links = [(link, match["role"]) for link in fresh["parameter_links"]["acquisitions"]
                 if link["state"] == "identity_and_temporal_rule_supported"
                 for match in link["matches"] if match["reference_interval_index"] == reference_index]
        _require(len(links) <= MAX_LINKED_READBACKS, "Too many readbacks map to this interval; choose a narrower evidence set.")
        result = {"status": "flight_bound_readbacks", "flight_record_id": context["flight_record_id"], "scope": scope,
            "association_id": linkage["association_id"], "alignment_evidence_hash": fresh["evidence_hash"],
            "preflight": [], "postflight": [], "effective_configuration": "unknown", "limitations": list(LIMITATIONS)}
        service = FlightConfigurationService(store)
        for link, role in links:
            summary = service.promote({"acquisition_id": link["acquisition_id"]}, cancel_check=cancel_check)["snapshot"]
            _require(summary["hardware_uid"] == context["hardware_uid"], "Readback UID contradicts the linked flight.")
            snapshot = store.get_document("snapshot", summary["id"])
            parameters = snapshot["parameters"]
            result[role].append({"snapshot_id": summary["id"], "acquisition_id": summary["acquisition_id"],
                **{key: summary[key] for key in ("source", "parameter_count", "hardware_uid", "firmware_identity", "typed_hash",
                    "completeness", "consistency", "usable_for_full_comparison", "unusable_reasons", "readback_evidence")},
                "role": role, "role_basis": "identity_and_temporal_rule_supported", "observation_range_utc_us": link["observation_range_utc_us"],
                "parameters_preview": [{"name": name, **value} for name, value in list(parameters.items())[:64]],
                "preview_truncated": len(parameters) > 64, "effective_configuration": "unknown"})
        if not links:
            result["reason"] = "No complete readback has supported identity and bounded temporal membership in this exact flight."
        with store._connect() as db:
            association = _CollectionReader()._get(db, "association", linkage["association_id"])
        _require(association.get("state") == "verified" and association.get("flight_record_id") == context["flight_record_id"]
                 and linkage["record_span"] in association.get("time_or_record_spans", [])
                 and alignment._catalog()[2] == fresh["candidate_universe"]["signature"],
                 "Managed association or acquisition evidence changed while attaching readbacks; generate a fresh report.")
        return result
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError, StopIteration) as error:
        return _unavailable("Flight readback evidence could not be verified: " + str(error))


def configuration_parameters(store, report, *, cancel_check=None, progress=None):
    """Return a complete preserved preflight set or fail; never substitute files."""
    check_cancel(cancel_check)
    configuration = report.get("configuration", {})
    context = report.get("flight_context", {})
    _require(configuration.get("status") == "flight_bound_readbacks" and context.get("status") == "verified"
        and configuration.get("flight_record_id") == context.get("flight_record_id")
        and configuration.get("association_id") == context.get("linkage", {}).get("association_id")
        and configuration.get("alignment_evidence_hash") == context.get("linkage", {}).get("fresh_evidence_hash")
        and configuration.get("scope") == {key: report.get("scope", {}).get(key) for key in SCOPE_KEYS},
        "Report lacks exact preserved flight-bound readback evidence.")
    preflight = configuration.get("preflight", [])
    _require(len(preflight) == 1 and preflight[0].get("usable_for_full_comparison") is True,
             "One unambiguous complete preflight readback is required; missing, partial or multiple sets cannot be substituted.")
    reference = preflight[0]
    snapshot = store.get_document("snapshot", reference["snapshot_id"])
    checked = _readback(store, reference["acquisition_id"], cancel_check, progress)
    _require(checked["usable_for_full_comparison"] and all(snapshot.get(key) == value for key, value in checked.items())
        and reference["source"]["sha256"] == snapshot["source"]["sha256"]
        and reference["typed_hash"] == snapshot["typed_hash"] and reference["hardware_uid"] == context["hardware_uid"],
        "Preserved preflight parameters no longer match their immutable acquisition evidence.")
    return snapshot["parameters"], reference


def compare_flight_configurations(store, before_report, after_report, *, cancel_check=None, progress=None):
    check_cancel(cancel_check)
    result = {"status": "unavailable", "basis": "complete_typed_preflight_readbacks", "eligible_for_parameter_review": False,
        "changed": [], "before_only": [], "after_only": [], "counts": {"changed": 0, "before_only": 0, "after_only": 0, "compared": 0},
        "truncated": False, "all_parameters_compared": False, "effective_configuration": "unknown", "causal_attribution": False,
        "reasons": [], "limitations": list(LIMITATIONS)}
    try:
        old, left = configuration_parameters(store, before_report, cancel_check=cancel_check, progress=progress)
        new, right = configuration_parameters(store, after_report, cancel_check=cancel_check, progress=progress)
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as error:
        result["reasons"].append(str(error))
        return result
    changed, before_only, after_only = [], [], []
    names = sorted(old.keys() | new.keys())
    for index, name in enumerate(names):
        check_cancel(cancel_check)
        if progress and index % 256 == 0:
            progress("comparing_typed_parameters", index, len(names))
        if name not in new or name not in old:
            value = old[name] if name in old else new[name]
            (before_only if name in old else after_only).append({"name": name, "value": value["value"], "mav_param_type": value["mav_param_type"]})
        elif (old[name]["mav_param_type"], old[name]["canonical_token"]) != (new[name]["mav_param_type"], new[name]["canonical_token"]):
            changed.append({"name": name, "before": old[name]["value"], "after": new[name]["value"],
                "before_type": old[name]["mav_param_type"], "after_type": new[name]["mav_param_type"],
                "before_token": old[name]["canonical_token"], "after_token": new[name]["canonical_token"],
                "type_changed": old[name]["mav_param_type"] != new[name]["mav_param_type"]})
    if progress:
        progress("comparing_typed_parameters", len(names), len(names))
    if left["hardware_uid"] != right["hardware_uid"] or left["firmware_identity"] != right["firmware_identity"]:
        result["reasons"].append("Reported aircraft UID or exact firmware differs between readbacks.")
    if left["acquisition_id"] == right["acquisition_id"]:
        result["reasons"].append("The same acquisition is not independent before/after evidence.")
    retained, remaining = [], MAX_DIFF_BYTES
    for rows in (changed, before_only, after_only):
        output = []
        for row in rows[:MAX_DIFF_ROWS]:
            check_cancel(cancel_check)
            size = len(json.dumps(row, separators=(",", ":")).encode())
            if size > remaining:
                break
            output.append(row)
            remaining -= size
        retained.append(output)
    result.update(status="compared", eligible_for_parameter_review=not result["reasons"], all_parameters_compared=True,
        before={key: left[key] for key in ("snapshot_id", "acquisition_id", "parameter_count", "hardware_uid", "firmware_identity", "typed_hash")},
        after={key: right[key] for key in ("snapshot_id", "acquisition_id", "parameter_count", "hardware_uid", "firmware_identity", "typed_hash")},
        changed=retained[0], before_only=retained[1], after_only=retained[2],
        counts={"changed": len(changed), "before_only": len(before_only), "after_only": len(after_only), "compared": len(old.keys() & new.keys())},
        truncated=any(len(rows) != len(output) for rows, output in zip((changed, before_only, after_only), retained)))
    return result
