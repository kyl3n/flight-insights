"""Managed observation sessions and immutable, evidence-scoped flight manifests.

This service consumes trusted recorder callbacks, never renderer-supplied heartbeats.
UUID membership proves capture provenance; it does not authenticate a UDP aircraft,
prove airborne flight, or establish historical parameter applicability.
"""
from __future__ import annotations

import copy
import hashlib
import time
import ipaddress
import json
import threading
from uuid import uuid4

from .store import Store


REQUIRED_SOURCES = ("app_mavlink_capture", "onboard_bin", "preflight_parameters", "postflight_parameters")
LIMITATIONS = [
    "UDP receipt and CRC do not authenticate a vehicle or universally detect replay; signatures are not verified.",
    "Aircraft/profile identity is operator-declared; source IDs and capture UUIDs alone do not prove physical identity.",
    "Passive parameter observations are not an active full readback or an atomic configuration snapshot.",
    "Explicit acquisition and reported-clock corroboration are separate workflows; manual matches stay provisional and missing evidence remains visible.",
    "Arming is observed activity, not proof of airborne flight. Missing heartbeats never establish disarm.",
]
RULE_VERSION = "q22-managed-capture-v1"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, label, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be a nonempty string of at most {limit} characters.")
    return value


def _integer(value, label, low=0, high=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"Invalid {label}.")
    return value


def _clock(value, label, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label} must be a decimal integer.")
    value = str(value)
    if not value.isascii() or not value.isdecimal() or len(value) > 24:
        raise ValueError(f"Invalid {label}.")
    return str(int(value))


class CollectionService:
    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.RLock()
        self._parameters = {}
        with store._connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_schema'").fetchone():
                versions = [row[0] for row in db.execute("SELECT version FROM collection_schema")]
                if versions != [1]:
                    raise ValueError("Unsupported collection schema; open this catalog with a compatible application.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS collection_schema (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO collection_schema VALUES (1);
                CREATE TABLE IF NOT EXISTS collection_versions (
                    kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(kind,id,revision)
                );
                CREATE TABLE IF NOT EXISTS collection_heads (
                    kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(kind,id)
                );
                CREATE TABLE IF NOT EXISTS collection_runtime (
                    capture_id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_operations (
                    operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                    response TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_counters (
                    aircraft_id TEXT PRIMARY KEY, last_number INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_events (
                    capture_id TEXT NOT NULL, event_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(capture_id,event_key)
                );
                CREATE TRIGGER IF NOT EXISTS collection_versions_no_update BEFORE UPDATE ON collection_versions
                    BEGIN SELECT RAISE(ABORT,'Collection evidence versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS collection_versions_no_delete BEFORE DELETE ON collection_versions
                    BEGIN SELECT RAISE(ABORT,'Collection evidence versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS collection_events_no_update BEFORE UPDATE ON collection_events
                    BEGIN SELECT RAISE(ABORT,'Collection events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS collection_events_no_delete BEFORE DELETE ON collection_events
                    BEGIN SELECT RAISE(ABORT,'Collection events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS collection_operations_no_update BEFORE UPDATE ON collection_operations
                    BEGIN SELECT RAISE(ABORT,'Collection operations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS collection_operations_no_delete BEFORE DELETE ON collection_operations
                    BEGIN SELECT RAISE(ABORT,'Collection operations are immutable'); END;
            """)

    def _get(self, db, kind, identity, revision=None):
        if revision is None:
            row = db.execute("SELECT v.data FROM collection_versions v JOIN collection_heads h "
                             "ON v.kind=h.kind AND v.id=h.id AND v.revision=h.revision "
                             "WHERE v.kind=? AND v.id=?", (kind, identity)).fetchone()
        else:
            row = db.execute("SELECT data FROM collection_versions WHERE kind=? AND id=? AND revision=?",
                             (kind, identity, revision)).fetchone()
        if row is None:
            raise ValueError(f"Unknown collection {kind} or revision.")
        value = json.loads(row[0])
        if kind == "capture" and revision is None:
            runtime = db.execute("SELECT data FROM collection_runtime WHERE capture_id=?", (identity,)).fetchone()
            if runtime:
                value.update(json.loads(runtime[0]))
        return value

    def _put(self, db, kind, data):
        identity = data.get("id", data.get("uuid"))
        data = dict(data)
        runtime = {}
        if kind == "capture":
            for key in ("source_states", "journal_checkpoint", "latest_event", "rejected_event_count", "last_rejection", "recorder_status"):
                if key in data:
                    runtime[key] = data.pop(key)
            db.execute("INSERT INTO collection_runtime VALUES (?,?) ON CONFLICT(capture_id) DO UPDATE SET data=excluded.data", (identity, _json(runtime)))
            old = db.execute("SELECT v.data FROM collection_versions v JOIN collection_heads h ON v.kind=h.kind AND v.id=h.id AND v.revision=h.revision WHERE v.kind='capture' AND v.id=?", (identity,)).fetchone()
            if old:
                old_data = json.loads(old[0])
                clean = lambda value: {k: v for k, v in value.items() if k not in ("revision", "updated_at", "created_at", "schema_version")}
                if clean(old_data) == clean(data):
                    return old_data | runtime
        row = db.execute("SELECT revision FROM collection_heads WHERE kind=? AND id=?", (kind, identity)).fetchone()
        value = dict(data, revision=(row[0] + 1 if row else 1), updated_at=self.store._now(), schema_version=2)
        value.setdefault("created_at", value["updated_at"])
        db.execute("INSERT INTO collection_versions VALUES (?,?,?,?,?)",
                   (kind, identity, value["revision"], value["updated_at"], _json(value)))
        db.execute("INSERT INTO collection_heads VALUES (?,?,?) ON CONFLICT(kind,id) "
                   "DO UPDATE SET revision=excluded.revision", (kind, identity, value["revision"]))
        return value | runtime

    def _list(self, db, kind, limit=100):
        rows = db.execute("SELECT v.data FROM collection_versions v JOIN collection_heads h "
                          "ON v.kind=h.kind AND v.id=h.id AND v.revision=h.revision "
                          "WHERE v.kind=? ORDER BY v.created_at DESC,v.id DESC LIMIT ?", (kind, limit)).fetchall()
        return [self._get(db, kind, json.loads(row[0]).get("id", json.loads(row[0]).get("uuid"))) for row in rows]

    def _active_captures(self, db, statuses=("observing",)):
        marks = ",".join("?" for _ in statuses)
        rows = db.execute("SELECT v.id FROM collection_versions v JOIN collection_heads h ON v.kind=h.kind AND v.id=h.id AND v.revision=h.revision WHERE v.kind='capture' AND json_extract(v.data,'$.status') IN (" + marks + ") ORDER BY v.created_at DESC", statuses)
        return [self._get(db, "capture", row[0]) for row in rows]

    @staticmethod
    def readiness(capture=None):
        reasons = ["This observer alone does not establish complete pre/post readbacks or effective flight-time configuration.",
                   "Downloaded/imported evidence needs the separate identity, clock and armed-boundary association rule; missing evidence remains partial."]
        if capture is None or not capture.get("recorder_started"):
            reasons.append("A recorder is not currently confirmed running.")
        if capture and capture.get("watcher_state") == "LINK_UNCERTAIN":
            reasons.append("Source continuity is uncertain; no disarm has been inferred.")
        return {"state": "degraded", "reasons": reasons, "can_observe": True, "can_claim_complete": False}

    def preview(self, params):
        profile = {key: _text(params.get(key), key) for key in
                   ("aircraft_id", "aircraft_profile_version", "connection_profile_version")}
        config = params.get("connection")
        if not isinstance(config, dict):
            raise ValueError("A connection profile is required.")
        connection = {}
        for key in ("local_address", "expected_sender"):
            try:
                address = ipaddress.IPv4Address(config.get(key))
            except (ValueError, TypeError, ipaddress.AddressValueError):
                raise ValueError(f"{key} must be an explicit numeric IPv4 address.") from None
            if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
                raise ValueError(f"{key} must identify one local interface or sender.")
            connection[key] = str(address)
        connection["listen_port"] = _integer(config.get("listen_port"), "listen_port", 1, 65535)
        connection["system_id"] = _integer(config.get("system_id"), "system_id", 1, 255)
        connection["component_id"] = _integer(config.get("component_id"), "component_id", 1, 255)
        if config.get("feed_origin") != "live_forwarded":
            raise ValueError("Managed observation requires an explicitly live_forwarded feed, never a replay import.")
        connection["feed_origin"] = "live_forwarded"
        synthetic = config.get("synthetic", False)
        if not isinstance(synthetic, bool):
            raise ValueError("synthetic must be boolean.")
        connection["synthetic"] = synthetic
        if config.get("expected_uid") is not None:
            uid = _clock(config["expected_uid"], "expected_uid")
            if int(uid) <= 0 or int(uid) > 2**64 - 1:
                raise ValueError("expected_uid must be a nonzero uint64 decimal string.")
            connection["expected_uid"] = uid
        required = params.get("required_sources", list(REQUIRED_SOURCES))
        if not isinstance(required, list) or not required or any(x not in REQUIRED_SOURCES for x in required):
            raise ValueError("required_sources must contain supported evidence roles.")
        profile.update(connection=connection, required_sources=sorted(set(required)))
        return {"profile": profile, "applicability_fingerprint": _hash(profile),
                "capabilities": {"managed_observation": True, "automatic_armed_intervals": True,
                                 "active_parameter_readback": False, "onboard_log_retrieval": False,
                                 "verified_retrospective_match": True},
                "readiness": self.readiness(), "limitations": LIMITATIONS}

    def _operation(self, db, method, params, profile):
        operation_id = _text(params.get("operation_id"), "operation_id")
        if params.get("target_profile_version") != profile["aircraft_profile_version"]:
            raise ValueError("Operation target profile version is stale or incorrect.")
        if params.get("applicability_fingerprint") != _hash(profile):
            raise ValueError("Operation applicability fingerprint is stale or incorrect; preview again.")
        digest = _hash({"method": method, "params": params})
        row = db.execute("SELECT payload_hash,response FROM collection_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError("operation_id was already used for a different payload or profile.")
            return operation_id, digest, dict(json.loads(row[1]), operation_replayed=True)
        return operation_id, digest, None

    def _complete(self, db, operation, result):
        db.execute("INSERT INTO collection_operations VALUES (?,?,?)", (operation[0], operation[1], _json(result)))
        return result

    def prepare(self, params):
        preview = self.preview(params)
        profile = preview["profile"]
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            operation = self._operation(db, "session.prepare_and_watch", params, profile)
            if operation[2]:
                return operation[2]
            if self._active_captures(db, ("observing",)):
                raise ValueError("Stop the existing observation session before preparing another.")
            capture = self._put(db, "capture", {
                "uuid": str(uuid4()), "profile": profile,
                "aircraft_profile_version": profile["aircraft_profile_version"],
                "connection_profile_version": profile["connection_profile_version"],
                "applicability_fingerprint": preview["applicability_fingerprint"],
                "status": "observing", "watcher_state": "PREPARING", "recorder_started": False,
                "clock_maps": [], "source_ids": [], "armed_interval_ids": [], "active_armed_interval_id": None,
                "journal_checkpoint": None, "coverage": {"state": "unknown", "gap_count": 0},
                "independent_collection_job_ids": [], "source_states": {}, "latest_event": None,
                "rejected_event_count": 0, "artifact_sources": [], "limitations": LIMITATIONS,
            })
            return self._complete(db, operation, {"capture_session": capture, "operation_replayed": False,
                                                  "readiness": self.readiness(capture), "limitations": LIMITATIONS})

    def mark_recorder_started(self, capture_id, status=None):
        with self._lock, self.store._connect() as db:
            capture = self._get(db, "capture", capture_id)
            if capture["status"] != "observing":
                raise ValueError("Observation session is not active.")
            capture.update(recorder_started=True, watcher_state="WATCHING_DEGRADED")
            if status:
                capture["recorder_status"] = status
            if status and status.get("state") in ("failed", "interrupted") and capture["status"] == "observing":
                capture.update(status=status["state"], recorder_started=False, watcher_state="LINK_UNCERTAIN")
                self._interrupt_open(db, capture, "recorder_" + status["state"])
                self._release_parameters(capture_id)
            return self._put(db, "capture", capture)

    def mark_recorder_failed(self, capture_id, error):
        with self._lock, self.store._connect() as db:
            capture = self._get(db, "capture", capture_id)
            capture.update(status="failed", recorder_started=False, watcher_state="LINK_UNCERTAIN", recorder_error=str(error)[:1000])
            self._interrupt_open(db, capture, "recorder_failed")
            self._release_parameters(capture_id)
            return self._put(db, "capture", capture)

    def recover_interrupted(self, capture_ids=None, exclude_capture_ids=None):
        """Called once by Engine on startup; never infer a disarm across process loss."""
        with self._lock, self.store._connect() as db:
            recovered = []
            for capture in self._active_captures(db, ("observing", "finalizing")):
                if capture["status"] in ("observing", "finalizing") and (capture_ids is None or capture["uuid"] in capture_ids) and capture["uuid"] not in (exclude_capture_ids or []):
                    capture.update(status="interrupted", recorder_started=False, watcher_state="LINK_UNCERTAIN")
                    self._interrupt_open(db, capture, "engine_restart_unknown_end")
                    self._release_parameters(capture["uuid"])
                    recovered.append(self._put(db, "capture", capture))
            return recovered

    def _interrupt_open(self, db, capture, reason):
        if capture["active_armed_interval_id"]:
            interval = self._get(db, "interval", capture["active_armed_interval_id"])
            interval.update(processing_state="AWAITING_EVIDENCE", end_status="unknown")
            if reason not in interval["coverage_warnings"]:
                interval["coverage_warnings"].append(reason)
            self._put(db, "interval", interval)
            self._manifest(db, interval, reason)

    @staticmethod
    def _evidence(event):
        keys = ("source_id", "system_id", "component_id", "record_index", "received_monotonic_ns", "received_utc_us",
                "raw_ref", "sender", "boot_epoch", "boot_time_us", "armed", "uid", "signature_present")
        return {key: event.get(key) for key in keys if key in event}

    def _parameter_capture(self, db, capture, source_id, role):
        accumulator = self._parameters.get((capture["uuid"], source_id))
        if accumulator is None:
            return None
        detached = copy.deepcopy(accumulator)
        detached.id = str(uuid4())
        result = detached.finalize(role=role)
        snapshot = detached.snapshot()
        source_state = capture["source_states"].get(source_id, {})
        if source_state.get("parameter_window_invalid"):
            result.update(completeness="partial", flight_time_applicability="unknown")
            snapshot.update(completeness="partial", flight_time_applicability="unknown")
            result["semantic_rejections"] = source_state.get("parameter_diagnostics", [])
        if source_state.get("parameter_window_gap"):
            result.update(freshness="gap_since_capture", flight_time_applicability="unknown")
            snapshot["flight_time_applicability"] = "unknown"
        self._put(db, "parameter_snapshot", snapshot)
        result.update(capture_session_id=capture["uuid"], role=role)
        return self._put(db, "parameter_capture", result)

    def _open_interval(self, db, capture, event, source):
        aircraft_id = capture["profile"]["aircraft_id"]
        db.execute("INSERT INTO collection_counters VALUES (?,1) ON CONFLICT(aircraft_id) "
                   "DO UPDATE SET last_number=last_number+1", (aircraft_id,))
        number = db.execute("SELECT last_number FROM collection_counters WHERE aircraft_id=?", (aircraft_id,)).fetchone()[0]
        interval_id, record_id = str(uuid4()), str(uuid4())
        already = source.get("armed") is None or source.get("continuity_lost", False)
        self._checkpoint_postflight(db, capture, event["source_id"], source)
        parameter = None if already else self._parameter_capture(db, capture, event["source_id"], "preflight")
        self._parameters.pop((capture["uuid"], event["source_id"]), None)
        interval = self._put(db, "interval", {
            "uuid": interval_id, "capture_session_id": capture["uuid"], "aircraft_id": aircraft_id,
            "display_number": number, "boot_epochs": [event.get("boot_epoch", "unknown")],
            "source_id": event["source_id"], "identity_evidence": source.get("identity_evidence"), "arm_evidence": self._evidence(event), "disarm_evidence": None,
            "started_already_armed": already, "start_status": "unknown" if already else "observed_transition",
            "end_status": "unknown", "flight_segments": [], "parameter_evidence_id": parameter["id"] if parameter else None,
            "parameter_evidence": {"preflight": parameter, "postflight": None, "applicability": "unknown",
                                   "limitations": ["Passive observations do not establish the historical effective configuration."]},
            "flight_record_id": record_id, "processing_state": "RECORDING", "collection_job_ids": [],
            "coverage_warnings": ["Capture started already armed; arm boundary and preflight evidence are unknown."] if already else [],
        })
        self._put(db, "flight_record", {
            "id": record_id, "armed_interval_id": interval_id, "capture_session_id": capture["uuid"],
            "aircraft_profile_version": capture["profile"]["aircraft_profile_version"],
            "classification": "unknown", "airborne_segment_ids": [], "evidence_manifest_revisions": [],
            "notes": [], "comparison_context": {"eligibility": "unknown", "reasons": ["Airborne segments and historical parameter applicability are unverified."]},
            "association_ids": [], "retrieval_request_ids": [],
        })
        record = self._get(db, "flight_record", record_id)
        for role in capture["profile"]["required_sources"]:
            if role == "app_mavlink_capture":
                continue
            request = self._put(db, "retrieval", {
                "id": str(uuid4()), "role": role, "aircraft_identity": {"aircraft_id": aircraft_id, "expected_uid": capture["profile"]["connection"].get("expected_uid")},
                "observed_remote_id": None, "observed_path_or_null": None,
                "mapping_provenance": "No remote listing or validated filename mapping observed.",
                "expected_size_and_stability": None, "clock_quality": "receipt_only_no_vehicle_UTC_mapping",
                "required_interval": {"start": self._evidence(event), "end": None}, "state": "missing",
                "recovery_action": "select_sd_log_folder_and_content_match" if role == "onboard_bin" else "obtain_full_parameter_readback_with_capture_provenance",
                "resolved_artifact_id": None, "flight_record_id": record_id, "armed_interval_id": interval_id,
                "capture_session_id": capture["uuid"],
            })
            record["retrieval_request_ids"].append(request["id"])
        self._put(db, "flight_record", record)
        capture["armed_interval_ids"].append(interval_id)
        capture["active_armed_interval_id"] = interval_id
        self._manifest(db, interval, "armed_interval_observed")
        return interval

    def ingest_event(self, capture_id, event):
        """Trusted recorder callback; exact receipt clocks and raw references preserved."""
        if not isinstance(event, dict) or len(_json(event)) > 32768:
            raise ValueError("Recorder event must be bounded structured evidence.")
        event = dict(event)
        event["system_id"] = event.get("system_id", event.get("sysid"))
        event["component_id"] = event.get("component_id", event.get("compid"))
        event["received_monotonic_ns"] = _clock(event.get("received_monotonic_ns"), "received_monotonic_ns")
        event["received_utc_us"] = _clock(event.get("received_utc_us"), "received_utc_us", nullable=True)
        source_id = _text(event.get("source_id", "recorder"), "source_id", 256)
        event["source_id"] = source_id
        record_index = event.get("record_index")
        if record_index is not None:
            record_index = _integer(record_index, "record_index")
        kind = event.get("kind")
        if kind not in ("heartbeat", "parameter", "identity", "link_gap", "coverage", "recorder_error"):
            raise ValueError("Unsupported recorder event kind.")
        if event.get("event_id") is not None:
            event_key = "journal:" + str(_integer(event["event_id"], "event_id"))
        elif record_index is not None:
            event_key = f"frame:{source_id}:{record_index}:{kind}"
        else:
            raise ValueError("Administrative events require a stable journal event_id.")
        if kind in ("heartbeat", "parameter", "identity") and record_index is None:
            raise ValueError("Frame evidence requires its actual recorder record_index.")
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            capture = self._get(db, "capture", capture_id)
            digest = _hash(event)
            prior = db.execute("SELECT payload_hash FROM collection_events WHERE capture_id=? AND event_key=?",
                               (capture_id, event_key)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ValueError("Recorder event identity was reused with conflicting content.")
                return {"accepted": True, "duplicate": True, "capture_session_id": capture_id}
            if capture["status"] != "observing":
                return {"accepted": False, "reason": "Observation session is no longer active."}
            db.execute("INSERT INTO collection_events VALUES (?,?,?,?,?)",
                       (capture_id, event_key, digest, kind, _json(event)))
            capture["journal_checkpoint"] = {"source_id": source_id, "record_index": record_index, "event_id": event.get("event_id")}
            capture["latest_event"] = self._evidence(event)
            config = capture["profile"]["connection"]
            if kind in ("link_gap", "coverage", "recorder_error"):
                if kind == "link_gap" and event.get("system_id") is not None and (event["system_id"], event["component_id"]) != (config["system_id"], config["component_id"]):
                    return {"accepted": False, "reason": "Gap belongs to a different telemetry source."}
                if kind in ("link_gap", "recorder_error"):
                    for state in capture["source_states"].values():
                        state["continuity_lost"] = True
                        state["parameter_window_gap"] = True
                    capture["watcher_state"] = "LINK_UNCERTAIN"
                    capture["coverage"]["gap_count"] += 1
                    capture["coverage"]["state"] = "gaps_observed"
                    self._separate_clock_boundary(db, capture, event)
                    # Start a new passive acquisition generation after any
                    # observed loss. Preserve the old generation as historical
                    # evidence, never carry its values into a fresh preflight.
                    for observed_source in list(capture["source_states"]):
                        historical = self._parameter_capture(db, capture, observed_source, "unknown")
                        if historical:
                            capture["historical_parameter_capture_count"] = capture.get("historical_parameter_capture_count", 0) + 1
                            capture.setdefault("historical_parameter_capture_ids", [])
                            if len(capture["historical_parameter_capture_ids"]) < 100:
                                capture["historical_parameter_capture_ids"].append(historical["id"])
                        self._parameters.pop((capture_id, observed_source), None)
                    self._interrupt_open(db, capture, kind)
                else:
                    capture["coverage"]["recorder"] = event.get("coverage", {})
                self._put(db, "capture", capture)
                return {"accepted": True, "capture_session_id": capture_id}
            reason = None
            if event.get("origin") != "live_forwarded" or event.get("live") is not True or config["synthetic"]:
                reason = "Replay/synthetic/unknown origin cannot create managed live intervals."
            elif event.get("crc_valid") is not True:
                reason = "CRC validity is not established."
            elif event.get("sender") != config["expected_sender"] or (event["system_id"], event["component_id"]) != (config["system_id"], config["component_id"]):
                reason = "Source does not match the explicitly selected sender/system/component."
            source = capture["source_states"].get(source_id, {})
            if source.get("last_monotonic_ns") is not None and int(event["received_monotonic_ns"]) < int(source["last_monotonic_ns"]):
                reason = "Receipt clock moved backward; stale event cannot alter interval state."
            if kind == "heartbeat" and (type(event.get("autopilot")) is not int or event.get("autopilot") == 8 or type(event.get("vehicle_type")) is not int or event.get("vehicle_type") == 6 or not isinstance(event.get("armed"), bool)):
                reason = "Only a selected autopilot heartbeat with a decoded arming flag can change interval state."
            if time.monotonic_ns() - int(event["received_monotonic_ns"]) > 10_000_000_000:
                reason = "Recorder receipt is older than the 10-second callback freshness policy."
            if reason:
                capture["rejected_event_count"] += 1
                capture["last_rejection"] = reason
                self._put(db, "capture", capture)
                return {"accepted": False, "reason": reason}
            if source_id not in capture["source_ids"]:
                if len(capture["source_ids"]) >= 64:
                    raise ValueError("Observation source limit reached.")
                capture["source_ids"].append(source_id)
            source["last_monotonic_ns"] = event["received_monotonic_ns"]
            if kind == "identity" and event.get("uid") not in (None, "0", 0):
                uid = _clock(event["uid"], "uid")
                if (source.get("uid") and source["uid"] != uid) or (config.get("expected_uid") and uid != config["expected_uid"]):
                    source["identity_conflict"] = True
                    capture["watcher_state"] = "LINK_UNCERTAIN"
                    self._cutoff_membership(db, capture, event, "Conflicting vehicle identity")
                    self._interrupt_open(db, capture, "vehicle_identity_conflict")
                source["uid"] = uid
                source["identity_evidence"] = self._evidence(event)
            if kind == "parameter":
                from .parameter_capture import ParameterAccumulator
                key = (capture_id, source_id)
                epoch = str(event.get("boot_epoch", "unknown"))
                previous = self._parameters.get(key)
                if previous is None or source.get("parameter_epoch") != epoch:
                    previous = ParameterAccumulator(capture["profile"]["aircraft_id"], epoch, {"system_id": event["system_id"], "component_id": event["component_id"]})
                    self._parameters[key] = previous
                    source["parameter_epoch"] = epoch
                    source["parameter_window_invalid"] = False
                    source["parameter_window_gap"] = False
                try:
                    outcome = previous.observe(event)
                    if not outcome.get("accepted"):
                        source["parameter_window_invalid"] = True
                except ValueError as error:
                    # Valid wire packets may have unindexed updates (65535),
                    # unknown counts or unsupported names/types. Retain the raw
                    # journal event and continue delivering subsequent heartbeats.
                    source["parameter_window_invalid"] = True
                    source.setdefault("parameter_diagnostics", [])
                    if len(source["parameter_diagnostics"]) < 8:
                        source["parameter_diagnostics"].append(str(error)[:500])
            if kind == "heartbeat":
                self._separate_clock_boundary(db, capture, event)
                if capture.get("selected_source_id") not in (None, source_id):
                    if not capture.get("source_ambiguity"):
                        self._cutoff_membership(db, capture, event, "Competing source endpoint")
                        self._interrupt_open(db, capture, "source_endpoint_ambiguity")
                    capture["source_ambiguity"] = True
                elif not capture.get("selected_source_id"):
                    capture["selected_source_id"] = source_id
                qualified = not capture.get("source_ambiguity") and not source.get("identity_conflict") and (not config.get("expected_uid") or source.get("uid") == config["expected_uid"])
                # A different endpoint reusing the selected IDs must not close or
                # continue the old source's interval, even if its flags agree.
                active = self._get(db, "interval", capture["active_armed_interval_id"]) if capture["active_armed_interval_id"] else None
                if active and active["source_id"] != source_id:
                    qualified = False
                if not qualified:
                    capture["watcher_state"] = "LINK_UNCERTAIN"
                    source["qualification_reason"] = "Identity or source continuity is unresolved."
                else:
                    if event["armed"] and active is None:
                        active = self._open_interval(db, capture, event, source)
                    elif not event["armed"] and active:
                        active.update(disarm_evidence=self._evidence(event), end_status="observed_transition", processing_state="AWAITING_EVIDENCE")
                        active["parameter_evidence"]["inflight"] = self._parameter_capture(db, capture, source_id, "inflight")
                        # No postflight readback is claimed from values observed before disarm.
                        self._put(db, "interval", active)
                        self._manifest(db, active, "disarm_observed_missing_required_evidence")
                        source["postflight_interval_id"] = active["uuid"]
                        self._parameters.pop((capture_id, source_id), None)
                        capture["active_armed_interval_id"] = None
                        active = None
                    source["continuity_lost"] = False
                    source["armed"] = event["armed"]
                    source["heartbeat_evidence"] = self._evidence(event)
                    capture["watcher_state"] = "ARMED_RECORDING" if active else "WATCHING_DEGRADED"
            capture["source_states"][source_id] = source
            self._put(db, "capture", capture)
            return {"accepted": True, "capture_session_id": capture_id,
                    "armed_interval_id": capture["active_armed_interval_id"], "watcher_state": capture["watcher_state"]}

    def _manifest(self, db, interval, reason):
        record = self._get(db, "flight_record", interval["flight_record_id"])
        capture = self._get(db, "capture", interval["capture_session_id"])
        associations = [self._get(db, "association", identity) for identity in record["association_ids"]]
        present = sorted({a["role"] for a in associations if a["state"] in ("verified", "provisional")})
        # A candidate artifact is collected even when association is provisional.
        # Parameter completeness is separate and never inferred from a param hash.
        required = capture["profile"]["required_sources"]
        completeness = {"state": "complete" if set(required) <= set(present) else "partial", "required": required,
                        "present": present, "missing": sorted(set(required) - set(present))}
        manifest = self._put(db, "manifest", {
            "id": record["id"], "flight_record_id": record["id"], "armed_interval_id": interval["uuid"],
            "capture_session_id": capture["uuid"], "reason": reason, "artifacts": associations,
            "collection_completeness": completeness, "parameter_evidence": interval["parameter_evidence"],
            "association_summary": {state: sum(a["state"] == state for a in associations) for state in ("verified", "provisional", "rejected", "missing")},
            "coverage_warnings": interval["coverage_warnings"], "limitations": LIMITATIONS,
            "analysis_eligibility": {"state": "blocked", "reasons": ["Historical parameter applicability and airborne segmentation are not corroborated."]},
        })
        record["evidence_manifest_revisions"].append(manifest["revision"])
        record["latest_manifest_revision"] = manifest["revision"]
        self._put(db, "flight_record", record)
        return manifest

    def stop_capture(self, params):
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            capture = self._get(db, "capture", params.get("capture_session_id"))
            operation = self._operation(db, "session.stop", params, capture["profile"])
            if operation[2]:
                return operation[2]
            for source_id, source in capture["source_states"].items():
                self._checkpoint_postflight(db, capture, source_id, source)
            capture.update(status="stopped", recorder_started=False,
                           watcher_state="LINK_UNCERTAIN" if capture["active_armed_interval_id"] else "WATCHING_DEGRADED")
            self._interrupt_open(db, capture, "recording_stopped_unknown_end")
            capture = self._put(db, "capture", capture)
            for key in [key for key in self._parameters if key[0] == capture["uuid"]]:
                self._parameters.pop(key, None)
            return self._complete(db, operation, {"capture_session": capture, "operation_replayed": False,
                                                  "readiness": self.readiness(capture), "limitations": LIMITATIONS})

    def attach_capture_artifacts(self, capture_id, sources):
        """Attach archived completed recorder chunks; root owns actual file archiving."""
        if not isinstance(sources, list) or len(sources) > 4096:
            raise ValueError("Recorder artifact batch exceeds bounds.")
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            capture = self._get(db, "capture", capture_id)
            for item in sources:
                source = item.get("source", {})
                row = db.execute("SELECT data FROM sources WHERE id=?", (source.get("id"),)).fetchone()
                if row is None or json.loads(row[0])["sha256"] != source.get("sha256"):
                    raise ValueError("Recorder artifact must reference an original archived by Store.")
                if item.get("role") not in ("app_mavlink_capture", "recorder_journal", "raw_udp_capture"):
                    raise ValueError("Unsupported managed recorder artifact role.")
                if any(s["source"]["sha256"] == source["sha256"] and s["role"] == item["role"] for s in capture["artifact_sources"]):
                    continue
                capture["artifact_sources"].append(item)
                if (item.get("coverage", {}).get("interval_membership") == "unknown"
                        or item.get("record_start") is None or item.get("record_end") is None):
                    continue
                _integer(item["record_start"], "artifact record_start")
                _integer(item["record_end"], "artifact record_end")
                if item["record_end"] < item["record_start"]:
                    # Recorder empty chunks convention: no valid MAVLink records.
                    # Preserve their bytes at capture scope without an invented span.
                    continue
                for identity in capture["armed_interval_ids"]:
                    interval = self._get(db, "interval", identity)
                    if item.get("source_id") and item["source_id"] != interval["source_id"]:
                        continue
                    first = interval["arm_evidence"]["record_index"]
                    last = interval["disarm_evidence"]["record_index"] if interval["disarm_evidence"] else interval.get("membership_end_record")
                    start = max(first, item.get("record_start", first))
                    end_candidates = [v for v in (last, item.get("record_end")) if v is not None]
                    end = min(end_candidates) if end_candidates else None
                    if end is not None and end < start:
                        continue
                    association = self._put(db, "association", {
                        "id": str(uuid4()), "artifact_sha256": source["sha256"], "artifact_source_id": source["id"], "role": item["role"],
                        "vehicle_filter": {"source_id": interval["source_id"], "system_id": capture["profile"]["connection"]["system_id"], "component_id": capture["profile"]["connection"]["component_id"]},
                        "boot_epoch": interval["boot_epochs"], "time_or_record_spans": [{"kind": "records", "start": start, "end": end}],
                        "state": "provisional", "capture_provenance": "managed_recorder_same_capture_uuid",
                        "supporting_evidence": ["Recorder journal references this capture UUID and observed interval record bounds."],
                        "contradictions": [], "candidate_alternatives": [], "rule_version": RULE_VERSION,
                        "review_history": [], "flight_record_id": interval["flight_record_id"], "armed_interval_id": identity,
                        "capture_session_id": capture_id,
                        "limitations": ["Record membership is capture provenance, not authenticated physical identity or independent flight matching."],
                    })
                    record = self._get(db, "flight_record", interval["flight_record_id"])
                    record["association_ids"].append(association["id"])
                    self._put(db, "flight_record", record)
                    self._manifest(db, interval, "recorder_artifact_archived")
            capture = self._put(db, "capture", capture)
            return {"capture_session": capture, "attached_artifact_count": len(capture["artifact_sources"])}

    def evaluate_association(self, params):
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._get(db, "flight_record", params.get("flight_record_id"))
            capture = self._get(db, "capture", record["capture_session_id"])
            operation = self._operation(db, "associations.evaluate", params, capture["profile"])
            if operation[2]:
                return operation[2]
            row = db.execute("SELECT data FROM documents WHERE kind='log' AND id=?", (params.get("log_id"),)).fetchone()
            if row is None:
                raise ValueError("Select an imported log ID.")
            log = json.loads(row[0])
            if log.get("synthetic") and not capture["profile"]["connection"].get("synthetic"):
                raise ValueError("Synthetic log evidence cannot be associated with a declared-live flight record.")
            streams = [s for s in log["streams"] if s["source_key"] == params.get("source_key") and s["boot_epoch"] == params.get("boot_epoch")]
            if len(streams) != 1:
                raise ValueError("Select one explicit log source and clock segment.")
            role = params.get("role")
            if role not in ("onboard_bin", "gcs_tlog"):
                raise ValueError("Manual log association requires onboard_bin or gcs_tlog role.")
            expected_format = "bin" if role == "onboard_bin" else "tlog"
            if log.get("format") != expected_format:
                raise ValueError(f"{role} requires an imported {expected_format.upper()} artifact.")
            spans = params.get("time_or_record_spans")
            if not isinstance(spans, list) or not 1 <= len(spans) <= 16:
                raise ValueError("Provide 1..16 explicit source record spans.")
            for span in spans:
                if not isinstance(span, dict) or span.get("kind") != "records":
                    raise ValueError("This association rule supports explicit record spans only.")
                start = _integer(span.get("start"), "span start")
                end = _integer(span.get("end"), "span end")
                if end < start:
                    raise ValueError("Record span is reversed.")
            contradictions = []
            expected_uid = capture["profile"]["connection"].get("expected_uid")
            observed_uids = set()
            for observation in streams[0].get("identity_observations", []):
                raw = observation.get("raw_fields", observation)
                if raw.get("uid") not in (None, 0, "0"):
                    observed_uids.add(str(raw["uid"]))
            interval = self._get(db, "interval", record["armed_interval_id"])
            expected_uid = expected_uid or (interval.get("identity_evidence") or {}).get("uid")
            if expected_uid and any(uid != str(expected_uid) for uid in observed_uids):
                contradictions.append("Observed nonzero vehicle UID conflicts with the declared expected UID.")
            association = self._put(db, "association", {
                "id": str(uuid4()), "artifact_sha256": log["source"]["sha256"], "artifact_source_id": log["source"]["id"],
                "log_id": log["id"], "role": role, "synthetic": log.get("synthetic"), "vehicle_filter": {"source_id": params["source_key"], "system_id": streams[0]["system_id"], "component_id": streams[0]["component_id"]},
                "boot_epoch": params["boot_epoch"], "time_or_record_spans": spans,
                "state": "rejected" if contradictions else "provisional",
                "supporting_evidence": ["Operator selected this archived artifact, source, clock segment and record span."],
                "contradictions": contradictions, "candidate_alternatives": [], "rule_version": "q22-manual-candidate-v1",
                "review_history": [], "flight_record_id": record["id"], "armed_interval_id": record["armed_interval_id"],
                "capture_session_id": capture["uuid"],
                "limitations": ["No independently supported identity + interval + corroboration match has been established.",
                                "The selected log clock segment is not mapped to the live recorder's clock by this manual selection.",
                                "Record spans are operator-declared; filtered source membership has not been independently checked."],
            })
            record["association_ids"].append(association["id"])
            self._put(db, "flight_record", record)
            manifest = self._manifest(db, self._get(db, "interval", record["armed_interval_id"]), "manual_candidate_evaluated")
            return self._complete(db, operation, {"association": association, "manifest": manifest, "operation_replayed": False})

    def review_association(self, params):
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            association = self._get(db, "association", params.get("association_id"))
            capture = self._get(db, "capture", association["capture_session_id"])
            operation = self._operation(db, "associations.review", params, capture["profile"])
            if operation[2]:
                return operation[2]
            decision = params.get("decision")
            if decision not in ("accept_provisional", "reject"):
                raise ValueError("Review may accept a provisional candidate or reject; it cannot manufacture verified evidence.")
            if decision == "accept_provisional" and association["contradictions"]:
                raise ValueError("A candidate with hard identity contradictions cannot be accepted.")
            reason = _text(params.get("reason"), "reason", 2000)
            association["review_history"].append({"decision": decision, "reason": reason, "created_at": self.store._now()})
            association["state"] = "provisional" if decision == "accept_provisional" else "rejected"
            association = self._put(db, "association", association)
            manifest = self._manifest(db, self._get(db, "interval", association["armed_interval_id"]), "operator_candidate_review")
            return self._complete(db, operation, {"association": association, "manifest": manifest, "operation_replayed": False})

    def attach_alignment(self, params, report, report_id, catalog_reader):
        """Internal machine-rule path; renderer evidence/state is never accepted."""
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._get(db, "flight_record", params.get("flight_record_id"))
            capture = self._get(db, "capture", record["capture_session_id"])
            operation = self._operation(db, "alignment.attach", params, capture["profile"])
            if operation[2]:
                return operation[2]
            interval = self._get(db, "interval", record["armed_interval_id"])
            if interval["start_status"] != "observed_transition" or not interval["disarm_evidence"] or interval["coverage_warnings"]:
                raise ValueError("Automatic association requires complete, uninterrupted managed arm/disarm evidence.")
            if catalog_reader()[2] != report["candidate_universe"]["signature"] or report["candidate_universe"]["truncated"]:
                raise ValueError("Imported candidate universe changed or is incomplete; analyze again.")
            reference = report["reference"]
            if reference["synthetic"] and not capture["profile"]["connection"].get("synthetic"):
                raise ValueError("Synthetic alignment cannot be attached to a declared-live flight record.")
            connection = capture["profile"]["connection"]
            expected_uid = connection.get("expected_uid") or (interval.get("identity_evidence") or {}).get("uid")
            if not expected_uid or reference["uids"] != [str(expected_uid)]:
                raise ValueError("Aligned reference identity does not match the managed interval's observed/declared UID.")
            if reference["selection"]["source_key"] != f"{connection['system_id']}:{connection['component_id']}":
                raise ValueError("Aligned reference source does not match the managed recorder's zero-link source.")
            archived = [item for item in capture["artifact_sources"] if item["role"] == "app_mavlink_capture"
                        and item["source"]["sha256"] == reference["artifact_sha256"]
                        and item.get("coverage", {}).get("complete_chunk") is True and item.get("coverage", {}).get("record_scope") == "capture_global"
                        and item.get("record_start") is not None and item.get("record_end") is not None]
            if len(archived) != 1:
                raise ValueError("Reference must be one complete original TLOG chunk archived by this managed capture.")
            archive = archived[0]
            log = json.loads(db.execute("SELECT data FROM documents WHERE kind='log' AND id=?", (reference["selection"]["log_id"],)).fetchone()[0])
            if log["record_count"] != archive["record_end"] - archive["record_start"] + 1:
                raise ValueError("Decoded/reference recorder record ranges are inconsistent.")
            expected_span = {"kind": "records", "start": interval["arm_evidence"]["record_index"] - archive["record_start"],
                             "end": interval["disarm_evidence"]["record_index"] - archive["record_start"]}
            relevant = []
            for candidate in report["candidates"]:
                if candidate["state"] != "verified":
                    continue
                if candidate["evidence"]["synthetic"] and not connection.get("synthetic"):
                    continue
                spans = [match["candidate_record_span"] for match in candidate["matches"] if match["reference_record_span"] == expected_span]
                role = {"bin": "onboard_bin", "tlog": "gcs_tlog", "log": "onboard_text_log"}.get(candidate["format"])
                if spans and role:
                    relevant.append((candidate, spans, role))
            if not relevant:
                raise ValueError("No freshly verified candidate has exact reference membership in this managed armed interval.")
            associations = []
            for candidate, spans, role in relevant:
                candidate_log = json.loads(db.execute("SELECT data FROM documents WHERE kind='log' AND id=?", (candidate["selection"]["log_id"],)).fetchone()[0])
                duplicate = None
                for identity in record["association_ids"]:
                    existing = self._get(db, "association", identity)
                    if (existing["artifact_sha256"] == candidate["artifact_sha256"] and existing["role"] == role
                            and existing["time_or_record_spans"] == spans and existing.get("rule_version") == report["algorithm_version"]):
                        duplicate = existing
                        break
                if duplicate:
                    associations.append(duplicate)
                    continue
                association = self._put(db, "association", {
                    "id": str(uuid4()), "artifact_sha256": candidate["artifact_sha256"], "artifact_source_id": candidate_log["source"]["id"],
                    "log_id": candidate_log["id"], "role": role, "synthetic": candidate_log["synthetic"],
                    "vehicle_filter": {"source_id": candidate["selection"]["source_key"]}, "boot_epoch": candidate["selection"]["boot_epoch"],
                    "time_or_record_spans": spans, "state": "verified", "rule_version": report["algorithm_version"],
                    "supporting_evidence": ["Original-hash-bound managed capture membership; matching UID/firmware, independent reported-clock models and both observed armed boundaries."],
                    "alignment_report_id": report_id, "fresh_evidence_hash": report["evidence_hash"],
                    "candidate_universe": report["candidate_universe"], "clock_model": candidate["evidence"]["clock_model"],
                    "contradictions": [], "candidate_alternatives": candidate["competing_candidates"], "review_history": [],
                    "flight_record_id": record["id"], "armed_interval_id": interval["uuid"], "capture_session_id": capture["uuid"],
                    "limitations": report["limitations"], "verification_meaning": "documented_content_rule_not_authenticated_identity_or_full_flight_completeness"})
                record["association_ids"].append(association["id"])
                associations.append(association)
            timeline = self._put(db, "parameter_timeline", {"id": str(uuid4()), "flight_record_id": record["id"],
                "alignment_report_id": report_id, "fresh_evidence_hash": report["evidence_hash"], **report["parameter_timeline"]})
            interval["parameter_evidence"]["timeline"] = {"id": timeline["id"], "state": timeline["state"],
                "observation_count": timeline["observation_count"], "change_count": timeline["change_count"], "attribution_allowed": False}
            interval["parameter_evidence"]["aligned_acquisitions"] = [
                {k: link[k] for k in ("acquisition_id", "state", "matches", "completeness", "consistency", "source_refs", "flight_time_applicability")}
                for link in report["parameter_links"]["acquisitions"] if any(
                    next((item["record_span"] for item in reference["armed_intervals"] if item["index"] == match["reference_interval_index"]), None) == expected_span
                    for match in link["matches"])]
            self._put(db, "interval", interval)
            self._put(db, "flight_record", record)
            manifest = self._manifest(db, interval, "machine_content_alignment_attached")
            return self._complete(db, operation, {"associations": associations, "manifest": manifest,
                                                  "parameter_timeline_id": timeline["id"], "operation_replayed": False})

    @staticmethod
    def _compact_parameter_evidence(evidence):
        value = copy.deepcopy(evidence)
        for role in ("preflight", "postflight", "inflight"):
            if isinstance(value.get(role), dict):
                for key in ("raw_refs", "issue_examples", "observed_change_ids", "missing_index_examples"):
                    value[role].pop(key, None)
                value[role]["details_in_manifest"] = True
        return value

    def _summary(self, db, record):
        interval = self._get(db, "interval", record["armed_interval_id"])
        manifest = self._get(db, "manifest", record["id"])
        return {key: record[key] for key in ("id", "armed_interval_id", "capture_session_id", "classification", "latest_manifest_revision")} | {
            key: interval[key] for key in ("display_number", "processing_state", "started_already_armed", "arm_evidence", "disarm_evidence")
        } | {key: manifest[key] for key in ("collection_completeness", "association_summary")} | {"parameter_evidence": self._compact_parameter_evidence(manifest["parameter_evidence"]), "missing_sources": manifest["collection_completeness"]["missing"]}

    def status(self, params=None):
        params = params or {}
        limit = _integer(params.get("limit", 100), "limit", 1, 100)
        offset = _integer(params.get("offset", 0), "offset", 0, 1_000_000)
        capture_offset = _integer(params.get("capture_offset", 0), "capture_offset", 0, 1_000_000)
        with self._lock, self.store._connect() as db:
            captures = [self._get(db, "capture", row[0]) for row in db.execute(
                "SELECT id FROM collection_versions WHERE kind='capture' AND revision=1 ORDER BY created_at DESC,id DESC LIMIT 25 OFFSET ?", (capture_offset,))]
            capture_total = db.execute("SELECT count(*) FROM collection_heads WHERE kind='capture'").fetchone()[0]
            capture = self._get(db, "capture", params["capture_session_id"]) if params.get("capture_session_id") else (captures[0] if captures else None)
            query = "FROM collection_versions v JOIN collection_heads h ON v.kind=h.kind AND v.id=h.id AND v.revision=h.revision WHERE v.kind='flight_record'"
            args = []
            if capture:
                query += " AND json_extract(v.data,'$.capture_session_id')=?"
                args.append(capture["uuid"])
            total = db.execute("SELECT count(*) " + query, args).fetchone()[0]
            records = [self._summary(db, json.loads(row[0])) for row in db.execute(
                "SELECT v.data " + query + " ORDER BY v.created_at DESC,v.id DESC LIMIT ? OFFSET ?", [*args, limit, offset])]
            active = self._active_captures(db)
            return {"capture_sessions": [self._public_capture(c) for c in captures],
                    "active_capture_session": self._public_capture(active[0], detailed=True) if active else None,
                    "capture_session": self._public_capture(capture, detailed=True), "flight_records": records,
                    "capture_total": capture_total, "capture_offset": capture_offset,
                    "captures_truncated": capture_offset + len(captures) < capture_total,
                    "flight_record_total": total, "offset": offset, "limit": limit,
                    "flight_records_truncated": offset + len(records) < total,
                    "readiness": self.readiness(capture), "limitations": LIMITATIONS}

    def detail(self, params):
        with self._lock, self.store._connect() as db:
            record = self._get(db, "flight_record", params.get("flight_record_id"))
            manifest = self._get(db, "manifest", record["id"], params.get("manifest_revision"))
            association_offset = _integer(params.get("association_offset", 0), "association_offset", 0, 1_000_000)
            association_limit = _integer(params.get("association_limit", 100), "association_limit", 1, 100)
            association_total = len(manifest["artifacts"])
            manifest["artifacts"] = manifest["artifacts"][association_offset:association_offset + association_limit]
            revisions = [{"revision": row[0], "created_at": row[1], "reason": json.loads(row[2])["reason"]} for row in db.execute(
                "SELECT revision,created_at,data FROM collection_versions WHERE kind='manifest' AND id=? ORDER BY revision DESC LIMIT 100", (record["id"],))]
            summary = self._summary(db, record)
            for key in ("collection_completeness", "association_summary", "parameter_evidence"):
                summary[key] = manifest[key]
            summary["missing_sources"] = manifest["collection_completeness"]["missing"]
            summary["latest_manifest_revision"] = manifest["revision"]
            return {"flight_record": record | summary, "armed_interval": self._get(db, "interval", record["armed_interval_id"]),
                    "capture_session": self._public_capture(self._get(db, "capture", record["capture_session_id"]), detailed=True),
                    "manifest": manifest, "revisions": revisions, "revision_total": record["latest_manifest_revision"],
                    "revisions_truncated": record["latest_manifest_revision"] > len(revisions),
                    "association_total": association_total, "association_offset": association_offset,
                    "associations_truncated": association_offset + len(manifest["artifacts"]) < association_total,
                    "associations": manifest["artifacts"],
                    "retrieval_requests": [self._get(db, "retrieval", identity) for identity in record["retrieval_request_ids"]]}

    def _cutoff_membership(self, db, capture, event, reason):
        if not capture.get("active_armed_interval_id") or event.get("record_index") is None:
            return
        interval = self._get(db, "interval", capture["active_armed_interval_id"])
        cutoff = event["record_index"] - 1
        interval["membership_end_record"] = min(cutoff, interval.get("membership_end_record", cutoff))
        interval.setdefault("membership_end_basis", reason + "; exclude this and later records without inferring disarm.")
        self._put(db, "interval", interval)

    def _separate_clock_boundary(self, db, capture, event):
        epoch = event.get("boot_epoch")
        if epoch is None or not capture.get("active_armed_interval_id"):
            return
        interval = self._get(db, "interval", capture["active_armed_interval_id"])
        if interval["source_id"] != event["source_id"] or str(epoch) == str(interval["boot_epochs"][-1]):
            return
        interval.update(processing_state="AWAITING_EVIDENCE", end_status="unknown")
        if event.get("record_index") is not None:
            interval["membership_end_record"] = min(event["record_index"] - 1, interval.get("membership_end_record", event["record_index"] - 1))
            interval.setdefault("membership_end_basis", "Last record before observed clock-segment discontinuity; not a disarm boundary.")
        interval["coverage_warnings"].append("Clock segment discontinuity: old interval end is unknown; later activity belongs to a separate interval.")
        interval["parameter_evidence"]["inflight"] = self._parameter_capture(db, capture, event["source_id"], "inflight")
        self._put(db, "interval", interval)
        self._manifest(db, interval, "clock_discontinuity_unknown_end")
        capture["active_armed_interval_id"] = None
        source = capture["source_states"].get(event["source_id"], {})
        source.update(armed=None, continuity_lost=True, parameter_window_gap=True)
        self._parameters.pop((capture["uuid"], event["source_id"]), None)

    def _release_parameters(self, capture_id):
        for key in [key for key in self._parameters if key[0] == capture_id]:
            self._parameters.pop(key, None)

    def _checkpoint_postflight(self, db, capture, source_id, source):
        interval_id = source.get("postflight_interval_id")
        if not interval_id:
            return
        evidence = self._parameter_capture(db, capture, source_id, "postflight")
        if evidence:
            interval = self._get(db, "interval", interval_id)
            interval["parameter_evidence"]["postflight"] = evidence
            self._put(db, "interval", interval)
            self._manifest(db, interval, "passive_postflight_parameter_evidence")
        source.pop("postflight_interval_id", None)

    def validate_stop(self, params):
        with self._lock, self.store._connect() as db:
            capture = self._get(db, "capture", params.get("capture_session_id"))
            operation = self._operation(db, "session.stop", params, capture["profile"])
            if operation[2]:
                return operation[2]
            return {"capture_session": capture, "operation_replayed": False}

    def mark_finalizing(self, capture_id, job_id=None):
        with self._lock, self.store._connect() as db:
            capture = self._get(db, "capture", capture_id)
            linking_terminal = job_id is not None and capture.get("archive_state") in ("complete", "failed")
            if not linking_terminal:
                capture.update(status="finalizing", recorder_started=False, archive_state="pending")
            if job_id:
                capture["archive_job_id"] = job_id
            if not linking_terminal:
                self._interrupt_open(db, capture, "recorder_finalizing_unknown_end")
            return self._put(db, "capture", capture)

    def pending_archives(self):
        with self._lock, self.store._connect() as db:
            rows = db.execute("SELECT v.data FROM collection_versions v JOIN collection_heads h ON v.kind=h.kind AND v.id=h.id AND v.revision=h.revision WHERE v.kind='capture' AND json_extract(v.data,'$.archive_state')='pending'")
            return [{"uuid": value["uuid"], "capture_session_id": value["uuid"], "archive_job_id": value.get("archive_job_id"), "status": value["status"]}
                    for value in (json.loads(row[0]) for row in rows)]

    def mark_archive_failed(self, capture_id, error):
        with self._lock, self.store._connect() as db:
            capture = self._get(db, "capture", capture_id)
            capture.update(status="stopped", archive_state="failed", archive_error=str(error)[:1000])
            return self._put(db, "capture", capture)

    def mark_archive_complete(self, capture_id):
        with self._lock, self.store._connect() as db:
            capture = self._get(db, "capture", capture_id)
            capture.update(status="stopped", archive_state="complete")
            self._release_parameters(capture_id)
            return self._put(db, "capture", capture)

    def update_recorder_status(self, capture_id, status):
        if not isinstance(status, dict) or len(_json(status)) > 131072:
            raise ValueError("Recorder status exceeds bounds.")
        with self._lock, self.store._connect() as db:
            capture = self._get(db, "capture", capture_id)
            capture["recorder_status"] = status
            if status and status.get("state") in ("failed", "interrupted") and capture["status"] == "observing":
                capture.update(status=status["state"], recorder_started=False, watcher_state="LINK_UNCERTAIN")
                self._interrupt_open(db, capture, "recorder_" + status["state"])
                self._release_parameters(capture_id)
            return self._put(db, "capture", capture)

    def safety_state(self, capture_id=None):
        with self._lock, self.store._connect() as db:
            captures = self._active_captures(db)
            capture = self._get(db, "capture", capture_id) if capture_id else (captures[0] if captures else None)
            armed_state, reason = "unknown", "No current qualified autopilot heartbeat establishes disarmed state."
            if capture and capture["status"] == "observing" and capture["recorder_started"] and capture["watcher_state"] != "LINK_UNCERTAIN":
                states = list(capture["source_states"].values())
                current = [s for s in states if s.get("heartbeat_evidence") and not s.get("identity_conflict")
                           and 0 <= time.monotonic_ns() - int(s["heartbeat_evidence"]["received_monotonic_ns"]) <= 5_000_000_000]
                if len(current) == 1:
                    armed_state = "armed" if current[0].get("armed") else "disarmed"
                    reason = "Fresh selected autopilot heartbeat; UDP identity and replay limitations still apply."
            return {"capture_session_id": capture["uuid"] if capture else None,
                    "watcher_state": capture["watcher_state"] if capture else "LINK_UNCERTAIN",
                    "armed_state": armed_state, "flight_state": armed_state, "identity_verified": False,
                    "heartbeat_age_s": None, "flight_sensitive": armed_state != "disarmed", "reason": reason}

    @staticmethod
    def _public_capture(capture, detailed=False):
        if capture is None:
            return None
        value = {k: v for k, v in capture.items() if k not in ("source_states", "artifact_sources", "recorder_status") or (k == "recorder_status" and detailed)}
        value["artifact_count"] = len(capture.get("artifact_sources", []))
        value["artifact_sources"] = [{"role": item["role"], "source": item["source"],
            "coverage": {key: item.get("coverage", {}).get(key) for key in ("complete_chunk", "record_scope", "interval_membership")},
            "record_start": item.get("record_start"), "record_end": item.get("record_end")}
            for item in capture.get("artifact_sources", [])[:64 if detailed else 0]]
        value["artifact_sources_truncated"] = len(capture.get("artifact_sources", [])) > (64 if detailed else 0)
        value["armed_interval_count"] = len(capture["armed_interval_ids"])
        value["armed_interval_ids"] = capture["armed_interval_ids"][-100:]
        value["armed_interval_ids_truncated"] = value["armed_interval_count"] > 100
        return value

    def dispatch(self, method, params):
        methods = {"session.preview": self.preview, "session.prepare_and_watch": self.prepare,
                   "session.status": self.status, "session.detail": self.detail, "session.stop": self.stop_capture,
                   "associations.evaluate": self.evaluate_association, "associations.review": self.review_association}
        if method not in methods:
            raise ValueError("Unknown collection method.")
        return methods[method](params)
