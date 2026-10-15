"""Exact, optional managed-flight provenance for an already scoped insight report.

An operator selection is only a lookup key. It is never evidence that an imported
log belongs to a flight, and the analysis computer is never historical context.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3

from .alignment import AlignmentService, RULE, integer
from .collection import CollectionService
from .flight_environment import unavailable_snapshot
from .logs import LogService, check_cancel


SCOPE_KEYS = ("source_sha256", "source_key", "boot_epoch", "interval_start_s", "interval_end_s")
LIMITATIONS = [
    "Verified linkage means the documented content rule passed for this exact imported source, clock segment and report interval; it is not authenticated aircraft identity.",
    "Observed arm/disarm bounds do not prove airborne flight or complete flight-wide collection.",
    "The environment is the recorded host observation at the managed arm transition, not the analysis computer or continuous monitoring; it does not establish causation.",
]


class _CollectionReader:
    # Reuse the collection's version/head and legacy-snapshot semantics without
    # running its schema initialization or enabling any collection mutations.
    def _get(self, db, kind, identity):
        revision = None
        if kind == "capture":
            row = db.execute("SELECT revision FROM collection_heads WHERE kind=? AND id=?", (kind, identity)).fetchone()
            _require(row is not None, "The managed capture no longer exists.")
            revision = row[0]  # Omit volatile current receiver runtime.
        return CollectionService._get(self, db, kind, identity, revision)


def _unavailable(reason, flight_record_id=None):
    value = {"status": "unavailable", "reason": reason, "limitations": list(LIMITATIONS)}
    if isinstance(flight_record_id, str) and flight_record_id:
        value["flight_record_id"] = flight_record_id
    return value


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _read(store, reader, flight_record_id):
    with store._connect() as db:
        record = reader._get(db, "flight_record", flight_record_id)
        interval = reader._get(db, "interval", record["armed_interval_id"])
        capture = reader._get(db, "capture", record["capture_session_id"])
        identities = record.get("association_ids", [])
        _require(len(identities) <= 128, "Flight association evidence exceeds the bounded linkage review; no context was attached.")
        associations = [reader._get(db, "association", identity) for identity in identities]
    return record, interval, capture, associations


def _environment(record, interval):
    snapshot = interval.get("environment_snapshot") or unavailable_snapshot()
    _require(isinstance(snapshot, dict), "The recorded environment snapshot is malformed.")
    _require(snapshot == record.get("environment_snapshot", snapshot),
             "The flight and interval environment snapshots disagree.")
    _require(snapshot.get("scope") == "armed_interval_start", "The stored environment was not observed for this armed interval.")
    digest = snapshot.get("content_sha256")
    if digest:
        payload = {key: value for key, value in snapshot.items() if key not in {"id", "content_sha256"}}
        actual = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        _require(actual == digest, "The recorded environment snapshot failed content integrity verification.")
    else:
        _require(snapshot.get("status") == "unavailable", "The recorded environment snapshot has no content integrity evidence.")
    return deepcopy(snapshot)


def _contained_span(logs, log, selected, scope, span, cancel_check):
    """Check report bounds in the actual selected time domain, without a fit."""
    origin = integer(selected.get("origin_time_us"))
    _require(origin is not None and str(scope.get("origin_time_us")) == str(origin)
             and scope.get("time_domain") == selected.get("time_domain")
             and selected.get("time_domain") in {"boot_us", "flight_controller_boot", "utc_receive_us"},
             "Report time origin/domain does not match the exact imported stream.")
    start, end = (Decimal(str(scope.get(key))) for key in ("interval_start_s", "interval_end_s"))
    _require(start.is_finite() and end.is_finite() and 0 <= start < end,
             "A finite, increasing report interval is required for flight context.")
    lower, upper = Decimal(origin) + start * 1_000_000, Decimal(origin) + end * 1_000_000
    first, last = integer(span.get("start")), integer(span.get("end"))
    _require(span.get("kind") == "records" and first is not None and last is not None and 0 <= first < last,
             "The verified association lacks an exact completed record span.")
    boundary_times, previous, previous_index, count = {}, None, None, 0
    for event in logs.events(log, selected, cancel_check=cancel_check):
        check_cancel(cancel_check)
        count += 1
        _require(count <= 4_000_000, "Report record membership exceeded the bounded evidence review.")
        index = integer(event.get("record_index"))
        _require(index is not None and (previous_index is None or index >= previous_index),
                 "Selected stream record order is not supported.")
        previous_index = index
        stamp = integer(event.get("time_us"))
        if stamp is None:
            continue
        _require(previous is None or stamp >= previous, "Selected stream timestamps regress; report membership is uncertain.")
        _require(event.get("time_domain", selected["time_domain"]) == selected["time_domain"],
                 "Selected stream contains incompatible timestamp domains.")
        previous = stamp
        if index in (first, last):
            boundary_times[index] = stamp
        if lower <= stamp <= upper:
            _require(first <= index <= last, "Report timestamps include records outside the verified armed interval.")
    _require(first in boundary_times and last in boundary_times
             and boundary_times[first] <= lower < upper <= boundary_times[last],
             "The full report interval is not contained within this flight's verified arm/disarm record span.")
    return {"time_domain": selected["time_domain"], "origin_time_us": str(origin),
            "start_time_us": str(boundary_times[first]), "end_time_us": str(boundary_times[last])}


def attach_flight_context(store, report, flight_record_id=None, *, cancel_check=None):
    """Return provenance without changing the report, catalog or live collection.

Absent or insufficient evidence yields unavailable. This intentionally requires
an explicit flight selection and a previously attached machine-rule association.
    """
    if flight_record_id is None or flight_record_id == "":
        return _unavailable("No managed flight selected. Imported logs do not establish historical host context.")
    if not isinstance(flight_record_id, str) or len(flight_record_id) > 200:
        return _unavailable("Select one managed flight record ID.")
    try:
        scope = report["scope"]
        _require(all(scope.get(key) is not None for key in (*SCOPE_KEYS, "log_id")),
                 "Report lacks an exact imported source, clock segment or interval.")
        logs, reader = LogService(store), _CollectionReader()
        log = store.get_document("log", scope["log_id"])
        selected = logs.select_stream(log, scope)
        _require(scope["source_sha256"] == log["source"]["sha256"] and scope.get("synthetic") == log.get("synthetic"),
                 "Report original hash or synthetic provenance does not match the imported log.")
        record, interval, capture, associations = _read(store, reader, flight_record_id)
        _require(record["id"] == interval["flight_record_id"] and record["armed_interval_id"] == interval["uuid"]
                 and record["capture_session_id"] == interval["capture_session_id"] == capture["uuid"]
                 and interval["aircraft_id"] == capture["profile"]["aircraft_id"],
                 "Managed flight, interval and capture references disagree.")
        _require(interval.get("start_status") == interval.get("end_status") == "observed_transition"
                 and interval.get("arm_evidence") and interval.get("disarm_evidence") and not interval.get("coverage_warnings"),
                 "Managed flight needs uninterrupted observed arm and disarm boundaries.")
        matching = [a for a in associations if a.get("state") == "verified" and a.get("rule_version") == RULE
                    and a.get("log_id") == log["id"] and a.get("artifact_sha256") == scope["source_sha256"]
                    and a.get("vehicle_filter", {}).get("source_id") == scope["source_key"]
                    and a.get("boot_epoch") == scope["boot_epoch"] and a.get("alignment_report_id")
                    and a.get("flight_record_id") == record["id"] and a.get("armed_interval_id") == interval["uuid"]
                    and a.get("capture_session_id") == capture["uuid"] and not a.get("contradictions")]
        _require(len(matching) == 1, "Select a flight with one exact, currently verified alignment association for this log/source/clock segment.")
        association = matching[0]
        saved = store.get_document("alignment_report", association["alignment_report_id"])
        _require(saved["inputs"].get("flight_record_id") in (None, record["id"]), "The alignment was scoped to a different managed flight.")
        alignment = AlignmentService(store, logs=logs, collection=reader)
        fresh = alignment.analyze(saved["inputs"], cancel_check=cancel_check, persist=False)
        _require(not fresh["candidate_universe"]["truncated"], "Current alignment candidate universe is incomplete.")
        reference = fresh["reference"]
        connection = capture["profile"]["connection"]
        expected_uid = connection.get("expected_uid") or (interval.get("identity_evidence") or {}).get("uid")
        _require(expected_uid and reference["uids"] == [str(expected_uid)], "Managed flight UID does not match the fresh reference evidence.")
        _require(reference["selection"]["source_key"] == f"{connection['system_id']}:{connection['component_id']}",
                 "Reference source does not match the managed capture.")
        _require(not reference["synthetic"] or connection.get("synthetic"), "Synthetic evidence cannot establish context for a declared-live flight.")
        archives = [a for a in capture["artifact_sources"] if a.get("role") == "app_mavlink_capture"
                    and a.get("source", {}).get("sha256") == reference["artifact_sha256"]
                    and a.get("coverage", {}).get("complete_chunk") is True
                    and a.get("coverage", {}).get("record_scope") == "capture_global"
                    and integer(a.get("record_start")) is not None and integer(a.get("record_end")) is not None]
        _require(len(archives) == 1, "Reference is not one completed original chunk from this managed capture.")
        archive = archives[0]
        reference_log = store.get_document("log", reference["selection"]["log_id"])
        _require(reference_log["record_count"] == archive["record_end"] - archive["record_start"] + 1,
                 "Managed reference record coverage is inconsistent.")
        expected_span = {"kind": "records", "start": interval["arm_evidence"]["record_index"] - archive["record_start"],
                         "end": interval["disarm_evidence"]["record_index"] - archive["record_start"]}
        selection = {key: scope[key] for key in ("log_id", "source_key", "boot_epoch")}
        candidates = [c for c in fresh["candidates"] if c["selection"] == selection and c["state"] == "verified"
                      and c["artifact_sha256"] == scope["source_sha256"]]
        _require(len(candidates) == 1, "The exact source is no longer uniquely verified by current UID, clock and armed-boundary evidence.")
        candidate = candidates[0]
        spans = [m["candidate_record_span"] for m in candidate["matches"] if m["reference_record_span"] == expected_span]
        _require(len(spans) == 1 and spans == association["time_or_record_spans"],
                 "Fresh alignment does not reproduce the exact managed flight record span.")
        _require(candidate["evidence"]["clock_model"]["state"] == reference["clock_model"]["state"] == "supported_reported_clock",
                 "Both reported-clock models must remain supported.")
        bounds = _contained_span(logs, log, selected, scope, spans[0], cancel_check)
        snapshot = _environment(record, interval)
        # Analysis is bounded but may take time. Refuse evidence changed while
        # it ran, including an operator rejection or a new competing log.
        _require(_read(store, reader, flight_record_id) == (record, interval, capture, associations)
                 and alignment._catalog()[2] == fresh["candidate_universe"]["signature"],
                 "Managed flight or imported evidence changed during linkage; generate the report again.")
        limitations = list(LIMITATIONS)
        if snapshot.get("status") == "unavailable":
            limitations.append("No usable host environment was recorded for this flight; current host details are not substituted.")
        return {"status": "verified", "flight_record_id": record["id"], "aircraft_id": interval["aircraft_id"],
                "hardware_uid": str(expected_uid), "environment_snapshot": snapshot,
                "linkage": {"association_id": association["id"], "scope": {key: scope[key] for key in SCOPE_KEYS},
                    "log_id": log["id"], "armed_interval_id": interval["uuid"], "capture_session_id": capture["uuid"],
                    "alignment_report_id": association["alignment_report_id"], "rule_version": fresh["algorithm_version"],
                    "fresh_evidence_hash": fresh["evidence_hash"], "candidate_universe_signature": fresh["candidate_universe"]["signature"],
                    "record_span": spans[0], "record_time_bounds": bounds, "verified_at": store._now()},
                "limitations": limitations}
    except ValueError as error:
        return _unavailable(str(error), flight_record_id)
    except (KeyError, TypeError, AttributeError, InvalidOperation, OverflowError, OSError, sqlite3.Error):
        return _unavailable("Stored flight, log or alignment evidence is missing, malformed or unreadable; historical context could not be verified.", flight_record_id)
