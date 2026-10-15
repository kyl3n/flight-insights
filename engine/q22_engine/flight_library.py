"""Flight-centered, local projections of immutable log and collection evidence.

The library never treats an import date, filename, system number or current host
as historical flight evidence. Automatic reports retain the existing diagnostic
and configuration rules; this module selects scopes, not aircraft settings.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

from .alignment import Evidence, mapped
from .insight_comparison import compare_reports
from .insight_service import InsightReports
from .insights import VERSION as REPORT_VERSION
from .logs import LogService, check_cancel, hash_file
from .parameters import text_field

VERSION = "q22-flight-library/1"
MAX_ROWS = 10000
MAX_INTERVALS = 256
MAX_REPORTS_PER_REFRESH = 10
MAX_DETAIL_BYTES = 3 * 1024 * 1024


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _bound(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}.")
    return value


def _iso(microseconds):
    try:
        value = float(microseconds)
        if not 946684800000000 <= value < 4102444800000000:
            return None
        return datetime.fromtimestamp(value / 1e6, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _public(row):
    result = {key: value for key, value in row.items() if not key.startswith("_")}
    result["metrics"] = {name: {key: metric.get(key) for key in ("value", "unit", "sample_count", "covered_duration_s", "coverage_fraction")}
                         for name, metric in row.get("metrics", {}).items()}
    return result


class FlightLibrary:
    """Rebuildable bounded index; immutable originals and reports stay authoritative."""

    def __init__(self, store, logs=None, reports=None, collection=None):
        self.store = store
        self.logs = logs or LogService(store)
        self.reports = reports or InsightReports(store)
        self.collection = collection
        self._lock = threading.RLock()
        self._verified_originals = {}
        self._alignment_pending = 0
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS flight_library_rows (
                    id TEXT PRIMARY KEY, log_id TEXT, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flight_library_index (
                    log_id TEXT PRIMARY KEY, signature TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flight_library_alignment_attempts (
                    flight_id TEXT PRIMARY KEY, signature TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flight_library_comparisons (
                    cache_key TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flight_library_row_settings (
                    flight_id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flight_library_settings (
                    source_sha256 TEXT PRIMARY KEY, data TEXT NOT NULL
                );
            """)

    def dispatch(self, method, params, cancel_check=None, progress=None):
        if not isinstance(params, dict):
            raise ValueError("Library params must be an object.")
        if method == "library.list":
            return self.list_flights(params)
        if method == "library.detail":
            return self.detail(params, cancel_check=cancel_check, progress=progress)
        if method == "library.refresh":
            return self.refresh(params, cancel_check=cancel_check, progress=progress)
        if method == "library.import":
            return self.import_log(params, cancel_check=cancel_check, progress=progress)
        if method == "library.analyze":
            return self.analyze(params, cancel_check=cancel_check, progress=progress)
        raise ValueError("Unknown library method.")

    def _rows(self):
        with self.store._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM flight_library_rows ORDER BY id LIMIT ?", (MAX_ROWS + 1,))]

    def _put(self, row):
        with self.store._connect() as db:
            db.execute("INSERT INTO flight_library_rows VALUES (?,?,?) ON CONFLICT(id) "
                       "DO UPDATE SET log_id=excluded.log_id,data=excluded.data",
                       (row["id"], row.get("log_id"), json.dumps(row, allow_nan=False)))

    def _get(self, identity):
        with self.store._connect() as db:
            row = db.execute("SELECT data FROM flight_library_rows WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown library flight ID. Refresh the flight library.")
        return json.loads(row[0])

    def _settings(self, digest):
        with self.store._connect() as db:
            row = db.execute("SELECT data FROM flight_library_settings WHERE source_sha256=?", (digest,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _save_settings(self, digest, settings):
        with self.store._connect() as db:
            db.execute("INSERT INTO flight_library_settings VALUES (?,?) ON CONFLICT(source_sha256) "
                       "DO UPDATE SET data=excluded.data", (digest, json.dumps(settings, allow_nan=False)))

    def _row_settings(self, identity):
        with self.store._connect() as db:
            row = db.execute("SELECT data FROM flight_library_row_settings WHERE flight_id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _save_row_settings(self, identity, settings):
        with self.store._connect() as db:
            db.execute("INSERT INTO flight_library_row_settings VALUES (?,?) ON CONFLICT(flight_id) "
                       "DO UPDATE SET data=excluded.data", (identity, json.dumps(settings, allow_nan=False)))

    def _catalog(self):
        with self.store._connect() as db:
            rows = db.execute("SELECT json_object('id',id,'created_at',created_at,'source',json_extract(data,'$.source'),'synthetic',json_extract(data,'$.synthetic'),'derived_sha256',json_extract(data,'$.derived_sha256')) FROM documents WHERE kind='log' ORDER BY created_at,id LIMIT ?",
                              (MAX_ROWS + 1,)).fetchall()
        logs = [json.loads(row[0]) for row in rows]
        # Reimporting identical bytes does not create another physical flight.
        unique = {}
        for log in logs:
            unique.setdefault((log["source"]["sha256"], log.get("synthetic", False)), log)
        return list(unique.values()), len(logs) > MAX_ROWS

    def _signature(self, log):
        return _hash([VERSION, REPORT_VERSION, log["source"]["sha256"], log.get("derived_sha256"),
                      self._settings(log["source"]["sha256"])])

    @staticmethod
    def _ordered(rows):
        # Unknown dates are their own group, ordered by import only and labelled so.
        return sorted(rows, key=lambda row: (bool(row.get("started_at")), row.get("started_at") or "",
                                             row.get("imported_at") or "", row["id"]), reverse=True)

    def list_flights(self, params):
        limit = _bound(params.get("limit", 30), "limit", 1, 50)
        offset = _bound(params.get("offset", 0), "offset", 0, MAX_ROWS)
        rows = self._ordered(self._visible(self._current_rows()))
        dated = [row for row in rows if row.get("started_at")]
        latest = max((row["started_at"] for row in dated), default=None)
        cutoff = (datetime.fromisoformat(latest) - timedelta(days=30)).isoformat() if latest else None
        recent = [row for row in dated if row["started_at"] >= cutoff] if cutoff else []
        pending = self._pending_logs()
        summary = {"total": len(rows), "analyzed": sum(bool(row.get("report_id")) for row in rows),
                   "needs_review": sum(row["status"] == "needs_review" for row in rows),
                   "awaiting_log": sum(row["status"] == "awaiting_log" for row in rows),
                   "dated_flights": len(dated), "recent_flights": len(recent), "unknown_dates": len(rows) - len(dated),
                   "pending_logs": pending, "period_start": cutoff, "period_end": latest,
                   "recent_period_basis": "last_30_days_from_latest_dated_flight"}
        return {"flights": [_public(row) for row in rows[offset:offset + limit]], "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None, "summary": summary,
                "truncated": len(rows) > MAX_ROWS, "algorithm_version": VERSION}

    def _pending_logs(self):
        logs, _ = self._catalog()
        with self.store._connect() as db:
            index = dict(db.execute("SELECT log_id,signature FROM flight_library_index"))
        pending = {log["id"] for log in logs if index.get(log["id"]) != self._signature(log)}
        rows = self._visible(self._rows())
        pending.update(row.get("log_id") for row in rows if row.get("status") == "pending" and row.get("log_id"))
        with self.store._connect() as db:
            cached = {row[0] for row in db.execute("SELECT cache_key FROM flight_library_comparisons")}
        for row in rows:
            if row.get("report_id"):
                before = self._comparison_selection(row, rows)[0]
                if before and self._comparison_key(before, row) not in cached:
                    pending.add("pair:" + row["id"])
        return len(pending) + self._alignment_pending

    @staticmethod
    def _visible(rows):
        linked = {row["flight_record_id"] for row in rows if row.get("log_id") and row.get("flight_record_id")}
        return [row for row in rows if row["kind"] != "observed_flight" or row.get("flight_record_id") not in linked]

    def _index_log(self, log, cancel_check=None, progress=None):
        log = self.store.get_document("log", log["id"])
        self.logs.verify_artifact(log, cancel_check)
        settings = self._settings(log["source"]["sha256"])
        indexed = []
        for stream in log["streams"]:
            check_cancel(cancel_check)
            evidence = Evidence(log, stream)
            boundary_times = {}
            meaningful = 0
            for event in self.logs.events(log, stream, cancel_check=cancel_check):
                check_cancel(cancel_check)
                evidence.observe(event)
                if event.get("message_type") in {"HEARTBEAT", "ARM"}:
                    # Keep only compacted boundary observations after each update.
                    if evidence.states:
                        current = {row["evidence"]["record_index"] for row in evidence.states}
                        boundary_times = {k: v for k, v in boundary_times.items() if k in current}
                        boundary_times[event.get("record_index")] = event.get("time_us")
                meaningful += event.get("kind") in {"parameter", "power", "attitude"} or event.get("message_type") in {"VIBE", "RATE", "GPS", "ARM"}
            result = evidence.finish()
            if not meaningful and not result["armed_intervals"]:
                continue  # Schema-only and GCS heartbeat streams are not flights.
            intervals = result["armed_intervals"]
            if len(intervals) > MAX_INTERVALS:
                intervals = []  # Do not silently label a bounded prefix a whole flight.
            scopes = intervals or [None]
            for number, interval in enumerate(scopes):
                check_cancel(cancel_check)
                start, end = stream.get("start_s"), stream.get("end_s")
                reasons = []
                complete = False
                record_span = None
                utc, end_utc, basis = None, None, "unknown"
                if interval:
                    first = interval["start"]["evidence"]["record_index"]
                    last = interval["end"]["evidence"]["record_index"] if interval.get("end") else None
                    raw_start, raw_end = boundary_times.get(first), boundary_times.get(last)
                    if raw_start is not None:
                        start = (raw_start - int(stream["origin_time_us"])) / 1e6
                    if raw_end is not None:
                        end = (raw_end - int(stream["origin_time_us"])) / 1e6
                    complete = bool(interval.get("prior_disarmed") and interval.get("end")
                                    and raw_start is not None and raw_end is not None)
                    record_span = {"start": first, "end": last}
                    if interval["start"].get("time"):
                        utc, basis = _iso(interval["start"]["time"]["utc_us"]), "reported_utc"
                        if interval.get("end") and interval["end"].get("time"):
                            end_utc = _iso(interval["end"]["time"]["utc_us"])
                    elif stream.get("time_domain") == "utc_receive_us" and raw_start is not None:
                        utc, basis = _iso(raw_start), "receive_utc"
                        end_utc = _iso(raw_end)
                    if not complete:
                        reasons.append("An arm or disarm boundary is missing; this is an observed interval, not a confirmed complete flight.")
                else:
                    reasons.append("No unambiguous complete armed interval was recorded. Analysis covers this source and clock segment; flight boundaries need review.")
                    if start is not None and stream.get("time_domain") == "utc_receive_us":
                        utc, basis = _iso(int(stream["origin_time_us"]) + start * 1e6), "receive_utc"
                    elif start is not None:
                        utc = _iso(mapped(result["clock_model"], int(stream["origin_time_us"]) + start * 1e6))
                        basis = "reported_utc" if utc else "unknown"
                if not utc:
                    basis = "unknown"
                    reasons.append("The flight date is unknown. Import time and filename are not used as flight time.")
                if result["quality_issues"]:
                    reasons.extend(result["quality_issues"][:4])
                    complete = False
                if result["limits_reached"]:
                    reasons.append("Evidence indexing reached a bound; review this source before comparing flights.")
                    complete = False
                identity = "log-" + _hash([log["source"]["sha256"], log.get("synthetic", False), stream["source_key"], stream["boot_epoch"], record_span])[:32]
                settings = {**self._settings(log["source"]["sha256"]), **self._row_settings(identity)}
                uid = result["uids"][0] if len(result["uids"]) == 1 else None
                aircraft_id = settings.get("aircraft_id")
                aircraft = {"id": uid or aircraft_id, "label": aircraft_id or (f"Aircraft {uid}" if uid else "Aircraft not identified"),
                            "identity_basis": "observed_hardware_uid" if uid else "operator_declared" if aircraft_id else "unknown"}
                if len(result["uids"]) > 1:
                    reasons.append("Conflicting hardware identities occur in this source; automatic cross-flight comparison is withheld.")
                    aircraft = {"id": None, "label": "Conflicting aircraft identities", "identity_basis": "conflicting"}
                batteries = stream.get("battery_ids", [])
                battery = settings.get("battery_id")
                if battery is None and len(batteries) == 1:
                    battery = batteries[0]
                blocked = bool(batteries and battery not in batteries)
                if blocked:
                    reasons.append("Choose the battery monitor to analyze; different monitors are never combined.")
                valid_scope = type(start) in (int, float) and type(end) in (int, float) and 0 <= start < end
                if not valid_scope:
                    reasons.append("This source has no increasing timed interval to analyze.")
                old = None
                try:
                    old = self._get(identity)
                except ValueError:
                    pass
                row = {"id": identity, "kind": "log_interval" if interval else "log_segment",
                       "label": log["label"] + (f" · interval {number + 1}" if len(scopes) > 1 else ""),
                       "flight_record_id": None, "log_id": log["id"], "source_key": stream["source_key"],
                       "boot_epoch": stream["boot_epoch"], "interval_start_s": start, "interval_end_s": end,
                       "started_at": utc, "ended_at": end_utc, "time_basis": basis, "imported_at": log["created_at"],
                       "aircraft": aircraft, "status": "needs_review" if blocked or not valid_scope else "pending",
                       "report_id": None, "metrics": {}, "finding_count": 0, "review_reasons": reasons,
                       "synthetic": log.get("synthetic", False), "available_battery_ids": batteries, "battery_id": battery,
                       "boundary_status": "observed_arm_and_disarm" if complete else "incomplete_or_unknown",
                       "_record_span": record_span, "_source_sha256": log["source"]["sha256"],
                       "_settings": settings, "_index_signature": self._signature(log),
                       "_report_version": REPORT_VERSION, "_complete_boundaries": complete,
                       "_time_domain": stream.get("time_domain"), "_blocked": blocked or not valid_scope,
                       "_comparison_identity": ("uid:" + uid) if uid else ("declared:" + aircraft_id) if aircraft_id and len(result["uids"]) <= 1 else None}
                row["_base_aircraft"] = dict(aircraft)
                row["_base_comparison_identity"] = row["_comparison_identity"]
                if old and old.get("_index_signature") == row["_index_signature"]:
                    # Preserve settings and an analysis already completed before interruption.
                    row = old
                self._put(row)
                indexed.append(identity)
        with self.store._connect() as db:
            db.execute("INSERT INTO flight_library_index VALUES (?,?) ON CONFLICT(log_id) DO UPDATE SET signature=excluded.signature",
                       (log["id"], self._signature(log)))
        return indexed

    def _managed(self):
        with self.store._connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_heads'").fetchone():
                return {}, {}, {}
            rows = db.execute("SELECT v.kind,v.id,v.data FROM collection_versions v JOIN collection_heads h "
                              "ON v.kind=h.kind AND v.id=h.id AND v.revision=h.revision "
                              "WHERE v.kind IN ('flight_record','interval','capture','association') LIMIT ?", (MAX_ROWS * 4,)).fetchall()
        data = {kind: {} for kind in ("flight_record", "interval", "capture", "association")}
        for kind, identity, payload in rows:
            data[kind][identity] = json.loads(payload)
        return data, data["flight_record"], data["association"]

    def _verify_original(self, digest, cancel_check):
        path = self.store.raw_path(digest)
        before = path.stat()
        stamp = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if self._verified_originals.get(digest) == stamp:
            return True
        if hash_file(path, cancel_check) != digest:
            return False
        after = path.stat()
        if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return False
        if len(self._verified_originals) >= 128:
            self._verified_originals.clear()
        self._verified_originals[digest] = stamp
        return True

    def _capture_context(self, row, data, records, cancel_check=None):
        """Display membership in this app's own original, not independent alignment."""
        span = row.get("_record_span")
        if not span or type(span.get("start")) is not int or type(span.get("end")) is not int:
            return None
        candidates = []
        log = None
        for record in records.values():
            check_cancel(cancel_check)
            interval = data["interval"].get(record["armed_interval_id"], {})
            capture = data["capture"].get(record["capture_session_id"], {})
            if (interval.get("flight_record_id") != record["id"] or interval.get("capture_session_id") != capture.get("uuid")
                    or interval.get("uuid") != record["armed_interval_id"]):
                continue
            connection = capture.get("profile", {}).get("connection", {})
            if row.get("source_key") != f"{connection.get('system_id')}:{connection.get('component_id')}":
                continue
            if row.get("synthetic") != connection.get("synthetic", False):
                continue
            arm, disarm = interval.get("arm_evidence") or {}, interval.get("disarm_evidence") or {}
            if not arm or not disarm or interval.get("started_already_armed") or interval.get("coverage_warnings"):
                continue
            for artifact in capture.get("artifact_sources", []):
                source, coverage = artifact.get("source", {}), artifact.get("coverage", {})
                if (artifact.get("role") != "app_mavlink_capture" or source.get("sha256") != row["_source_sha256"]
                        or source.get("acquisition_method") != "app_owned_passive_udp_capture"
                        or coverage.get("complete_chunk") is not True or coverage.get("record_scope") != "capture_global"
                        or type(artifact.get("record_start")) is not int or type(artifact.get("record_end")) is not int):
                    continue
                offset = artifact["record_start"]
                if span["start"] + offset != arm.get("record_index") or span["end"] + offset != disarm.get("record_index"):
                    continue
                log = log or self.store.get_document("log", row["log_id"])
                if log["record_count"] != artifact["record_end"] - offset + 1:
                    continue
                with self.store._connect() as db:
                    original = db.execute("SELECT data FROM sources WHERE id=?", (source.get("id"),)).fetchone()
                if not original or json.loads(original[0]) != source:
                    continue
                try:
                    if not self._verify_original(source["sha256"], cancel_check):
                        continue
                except OSError:
                    continue
                from .insight_context import _environment
                from .flight_environment import unavailable_snapshot
                try:
                    environment = _environment(record, interval)
                except ValueError as exc:
                    environment = unavailable_snapshot(str(exc))
                candidates.append({"flight_record_id": record["id"], "capture_session_id": capture["uuid"],
                                   "basis": "capture_provenance", "environment_snapshot": environment,
                                   "aircraft_id": capture["profile"]["aircraft_id"],
                                   "limitations": ["This original's exact recorder record span links it to this app's observed armed interval.",
                                                   "This display grouping does not verify another log, a complete parameter readback or effective flight configuration."]})
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _observed_rows(data, records):
        result = []
        for record in records.values():
            interval = data["interval"].get(record["armed_interval_id"], {})
            capture = data["capture"].get(record["capture_session_id"], {})
            profile = capture.get("profile", {})
            uid = (interval.get("identity_evidence") or {}).get("uid")
            row = {"id": "flight-" + record["id"], "kind": "observed_flight",
                   "label": f"Flight {interval.get('display_number', '')}".strip(), "flight_record_id": record["id"],
                   "log_id": None, "report_id": None, "started_at": _iso((interval.get("arm_evidence") or {}).get("received_utc_us")),
                   "time_basis": "receive_utc", "imported_at": record.get("created_at"),
                   "aircraft": {"id": str(uid) if uid else profile.get("aircraft_id"),
                                "label": profile.get("aircraft_id", "Aircraft not identified"),
                                "identity_basis": "observed_hardware_uid" if uid else "operator_declared"},
                   "status": "awaiting_log", "metrics": {}, "finding_count": 0,
                   "review_reasons": ["Waiting for a completed log with an exact flight association."],
                   "synthetic": profile.get("connection", {}).get("synthetic", False),
                   "available_battery_ids": [], "battery_id": None,
                   "boundary_status": "observed_arm_and_disarm" if interval.get("disarm_evidence") and not interval.get("started_already_armed") else "incomplete_or_unknown"}
            result.append(row)
        return result

    def _current_rows(self):
        data, records, _ = self._managed()
        rows = [row for row in self._rows() if row["kind"] != "observed_flight"]
        return rows + (self._observed_rows(data, records) if records else [])

    def _sync_managed(self, cancel_check=None):
        data, records, associations = self._managed()
        if not records:
            return
        for row in self._observed_rows(data, records):
            self._put(row)
        for row in self._rows():
            if not row.get("log_id"):
                continue
            span = row.get("_record_span")
            matches = [item for item in associations.values() if item.get("state") == "verified"
                       and item.get("log_id") == row["log_id"] and item.get("artifact_sha256") == row["_source_sha256"]
                       and item.get("vehicle_filter", {}).get("source_id") == row["source_key"]
                       and item.get("boot_epoch") == row["boot_epoch"] and not item.get("contradictions")
                       and span and item.get("time_or_record_spans") == [{"kind": "records", **span}]]
            flight_id = matches[0].get("flight_record_id") if len(matches) == 1 else None
            capture_context = self._capture_context(row, data, records, cancel_check)
            record = records.get(flight_id, {})
            interval = data["interval"].get(record.get("armed_interval_id"), {})
            capture = data["capture"].get(record.get("capture_session_id"), {})
            managed_signature = _hash([matches, record, interval, capture.get("profile"), capture.get("artifact_sources"), capture_context,
                                       [(log["id"], log["source"]["sha256"]) for log in self._catalog()[0]]]) if flight_id or capture_context else None
            if row.get("_managed_signature") != managed_signature or row.get("_requested_flight_record_id") != flight_id:
                row["_requested_flight_record_id"] = flight_id
                row["flight_record_id"] = capture_context["flight_record_id"] if capture_context else None
                row["context_linkage"] = "capture_provenance" if capture_context else None
                row["_observed_context"] = capture_context
                row["aircraft"] = dict(row.get("_base_aircraft", row["aircraft"]))
                row["_comparison_identity"] = row.get("_base_comparison_identity", row.get("_comparison_identity"))
                if capture_context:
                    row["aircraft"]["label"] = capture_context["aircraft_id"]
                row["_managed_signature"] = managed_signature
                if not row.get("_blocked"):
                    row["status"] = "pending"
                    row["report_id"] = None
                self._put(row)

    def _auto_align(self, cancel_check=None, progress=None):
        """Run the existing independent-log rule; never upgrade a provisional match."""
        self._alignment_pending = 0
        if self.collection is None:
            from .collection import CollectionService
            # Only create this reader when managed evidence already exists.
            if not self._managed()[1]:
                return 0
            self.collection = CollectionService(self.store)
        from .alignment import AlignmentService
        from .collection import _hash as collection_hash
        alignment = AlignmentService(self.store, logs=self.logs, collection=self.collection)
        catalog, truncated, catalog_signature, _ = alignment._catalog()
        if truncated:
            return 0
        data, records, _ = self._managed()
        rows = self._rows()
        attempts = 0
        # A complete candidate index prevents an early cache entry from skipping
        # a later batch of the same imported catalog.
        with self.store._connect() as db:
            indexed = dict(db.execute("SELECT log_id,signature FROM flight_library_index"))
        if any(indexed.get(log["id"]) != self._signature(log) for log in self._catalog()[0]):
            return 0
        for row in rows:
            check_cancel(cancel_check)
            observed = row.get("_observed_context")
            if not observed or not row.get("_complete_boundaries"):
                continue
            record = records.get(observed["flight_record_id"], {})
            capture = data["capture"].get(record.get("capture_session_id"), {})
            interval = data["interval"].get(record.get("armed_interval_id"), {})
            if capture.get("status") in {"observing", "starting"}:
                continue
            candidates = {}
            for candidate in rows:
                if (not candidate.get("log_id") or candidate.get("_source_sha256") == row.get("_source_sha256")
                        or candidate.get("synthetic") != row.get("synthetic")):
                    continue
                # Date overlap is a search filter only; the independent content
                # rule still establishes identity, clock and exact boundaries.
                if (row.get("started_at") and row.get("ended_at") and candidate.get("started_at") and candidate.get("ended_at")
                        and row.get("time_basis") == candidate.get("time_basis")
                        and (candidate["ended_at"] < row["started_at"] or row["ended_at"] < candidate["started_at"])):
                    continue
                selected = {key: candidate[key] for key in ("log_id", "source_key", "boot_epoch")}
                candidates[tuple(selected.values())] = selected
            if not 1 <= len(candidates) <= 16:
                continue
            signature = _hash([VERSION, catalog_signature, row["log_id"], row["source_key"], row["boot_epoch"],
                               capture.get("profile"), capture.get("artifact_sources"), interval.get("arm_evidence"), interval.get("disarm_evidence")])
            with self.store._connect() as db:
                prior = db.execute("SELECT signature FROM flight_library_alignment_attempts WHERE flight_id=?", (record["id"],)).fetchone()
            if prior and prior[0] == signature:
                continue
            if attempts >= 2:
                self._alignment_pending += 1
                continue
            params = {"reference": {key: row[key] for key in ("log_id", "source_key", "boot_epoch")},
                      "candidates": list(candidates.values()), "flight_record_id": record["id"]}
            receipt = {"state": "needs_review", "reason": "Independent evidence did not establish a unique complete flight association."}
            try:
                report = alignment.analyze(params, cancel_check, progress)
                receipt["alignment_report_id"] = report["id"]
                span = {"kind": "records", **row["_record_span"]}
                verified = [candidate for candidate in report["candidates"] if candidate["state"] == "verified"
                            and any(match["reference_record_span"] == span for match in candidate["matches"])]
                if verified and not report["candidate_universe"]["truncated"]:
                    profile = capture["profile"]
                    result = alignment.attach({"report_id": report["id"], "flight_record_id": record["id"],
                                               "operation_id": str(uuid4()), "target_profile_version": profile["aircraft_profile_version"],
                                               "applicability_fingerprint": collection_hash(profile)}, cancel_check, progress)
                    verified_associations = [association for association in result.get("associations", []) if association.get("state") == "verified"]
                    receipt = {"state": "attached" if verified_associations else "needs_review", "alignment_report_id": report["id"],
                               "reason": "Existing identity, supported clocks and armed-boundary rules passed and were rechecked before attachment." if verified_associations
                                         else "Existing rejected or provisional associations were preserved; no verified link was restored."}
            except CancelledError:
                raise
            except (OSError, ValueError) as exc:
                receipt["reason"] = str(exc)[:500]
            with self.store._connect() as db:
                db.execute("INSERT INTO flight_library_alignment_attempts VALUES (?,?,?) ON CONFLICT(flight_id) "
                           "DO UPDATE SET signature=excluded.signature,data=excluded.data",
                           (record["id"], signature, json.dumps(receipt, allow_nan=False)))
            attempts += 1
        return attempts

    def _import_completed_captures(self, maximum, cancel_check, progress):
        data, _, _ = self._managed()
        if not data:
            return 0
        logs, _ = self._catalog()
        known = {(log["source"]["sha256"], log.get("synthetic", False)) for log in logs}
        imported = 0
        for capture in data["capture"].values():
            for artifact in capture.get("artifact_sources", []):
                check_cancel(cancel_check)
                source = artifact.get("source", {})
                synthetic = capture.get("profile", {}).get("connection", {}).get("synthetic", False)
                if (artifact.get("role") != "app_mavlink_capture" or not artifact.get("coverage", {}).get("complete_chunk")
                        or (source.get("sha256"), synthetic) in known):
                    continue
                if imported >= maximum:
                    return imported
                try:
                    result = self.logs.import_log({"path": str(self.store.raw_path(source["sha256"])),
                                                   "label": source.get("original_name") or "Recorded telemetry",
                                                   "synthetic": synthetic}, cancel_check, progress)
                except (OSError, ValueError):
                    # Existing catalog evidence remains accessible; an absent or unsupported
                    # original cannot be replaced by a guessed flight report.
                    continue
                known.add((result["log"]["source"]["sha256"], synthetic))
                imported += 1
        return imported

    def refresh(self, params, cancel_check=None, progress=None):
        maximum = _bound(params.get("max_logs", 10), "max_logs", 1, 50)
        with self._lock:
            self._import_completed_captures(maximum, cancel_check, progress)
            logs, truncated = self._catalog()
            with self.store._connect() as db:
                indexed = dict(db.execute("SELECT log_id,signature FROM flight_library_index"))
            pending = [log for log in logs if indexed.get(log["id"]) != self._signature(log)]
            count = analyzed = 0
            for log in pending[:maximum]:
                check_cancel(cancel_check)
                if progress:
                    progress("indexing_flights", count, min(len(pending), maximum))
                self._index_log(log, cancel_check, progress)
                count += 1
            self._sync_managed(cancel_check)
            aligned = self._auto_align(cancel_check, progress)
            if aligned:
                self._sync_managed(cancel_check)
            rows = self._ordered(self._rows())
            for row in [row for row in rows if row["status"] == "pending"][:MAX_REPORTS_PER_REFRESH]:
                check_cancel(cancel_check)
                self._analyze_row(row, cancel_check, progress)
                analyzed += 1
            compared = self._cache_comparisons(cancel_check, progress)
            result = self.list_flights({})
            return {**result, "indexed_logs": count, "analyzed_flights": analyzed, "compared_flights": compared, "alignment_attempts": aligned,
                    "pending_logs": self._pending_logs(), "catalog_truncated": truncated}

    def _analyze_row(self, row, cancel_check=None, progress=None):
        if row.get("_blocked") or not row.get("log_id"):
            return row
        settings = row.get("_settings", {})
        params = {key: row[key] for key in ("log_id", "source_key", "boot_epoch", "interval_start_s", "interval_end_s")}
        params.update(max_gap_s=1, battery_id=row.get("battery_id"), operating_profile=settings.get("operating_profile", {}))
        if row.get("_requested_flight_record_id"):
            params["flight_record_id"] = row["_requested_flight_record_id"]
        try:
            result = self.reports.dispatch("insights.report", params, cancel_check, progress)
            report = result["report"]
            check_cancel(cancel_check)
            row.update(report_id=report["id"], metrics=report.get("metrics", {}), finding_count=report.get("finding_count", len(report.get("findings", []))),
                       status="ready" if row.get("_complete_boundaries") else "needs_review")
            context = report.get("flight_context", {})
            observed = row.get("_observed_context") or {}
            row["flight_record_id"] = context.get("flight_record_id") if context.get("status") == "verified" else observed.get("flight_record_id")
            row["context_linkage"] = "verified" if context.get("status") == "verified" else "capture_provenance" if observed else None
            if context.get("status") == "verified" and context.get("hardware_uid"):
                row["aircraft"] = {"id": context["hardware_uid"], "label": context.get("aircraft_id") or "Aircraft " + context["hardware_uid"],
                                   "identity_basis": "verified_flight_context"}
                row["_comparison_identity"] = "uid:" + context["hardware_uid"]
            row.pop("analysis_error", None)
        except CancelledError:
            raise
        except (ValueError, OSError) as exc:
            row.update(status="needs_review", analysis_error=str(exc)[:500])
            row["review_reasons"] = [*row["review_reasons"], "Automatic analysis could not finish. Review the source and its analysis settings."][:12]
        self._put(row)
        return row

    def import_log(self, params, cancel_check=None, progress=None):
        path = text_field(params, "path", 4096)
        label = text_field(params, "label") if params.get("label") else Path(path).name
        aircraft = text_field(params, "aircraft_id", 160) if params.get("aircraft_id") else None
        with self._lock:
            result = self.logs.import_log({"path": path, "label": label, "synthetic": params.get("synthetic", False)}, cancel_check, progress)
            log = result["log"]
            if aircraft:
                settings = self._settings(log["source"]["sha256"])
                self._save_settings(log["source"]["sha256"], {**settings, "aircraft_id": aircraft})
            refreshed = self.refresh({"max_logs": 10}, cancel_check, progress)
            return {**refreshed, "log": {key: log[key] for key in ("id", "label", "synthetic")}
                    | {"source": {key: log["source"][key] for key in ("sha256", "original_name")}}}

    def analyze(self, params, cancel_check=None, progress=None):
        with self._lock:
            row = self._get(text_field(params, "flight_id"))
            if not row.get("log_id"):
                raise ValueError("This flight needs a completed associated log before analysis.")
            settings = dict(row.get("_settings", {}))
            if "aircraft_id" in params:
                settings["aircraft_id"] = text_field(params, "aircraft_id", 160) if params["aircraft_id"] else None
                if row["aircraft"]["identity_basis"] not in {"observed_hardware_uid", "verified_flight_context", "conflicting"}:
                    identity = settings["aircraft_id"]
                    row["aircraft"] = {"id": identity, "label": identity or "Aircraft not identified", "identity_basis": "operator_declared" if identity else "unknown"}
                    row["_comparison_identity"] = "declared:" + identity if identity else None
                    row["_base_aircraft"] = dict(row["aircraft"])
                    row["_base_comparison_identity"] = row["_comparison_identity"]
                elif row["aircraft"]["identity_basis"] != "conflicting" and settings["aircraft_id"]:
                    row["aircraft"]["label"] = settings["aircraft_id"]
                    row.setdefault("_base_aircraft", dict(row["aircraft"]))["label"] = settings["aircraft_id"]
            if "battery_id" in params:
                if params["battery_id"] not in row.get("available_battery_ids", []):
                    raise ValueError("Select one battery monitor recorded in this source.")
                row["battery_id"] = settings["battery_id"] = params["battery_id"]
            if "operating_profile" in params:
                from .insights import operating_profile
                operating_profile(params["operating_profile"])
                settings["operating_profile"] = params["operating_profile"]
            self._save_row_settings(row["id"], settings)
            row["_settings"] = settings
            row["_blocked"] = not (type(row.get("interval_start_s")) in (int, float) and type(row.get("interval_end_s")) in (int, float)
                                       and 0 <= row["interval_start_s"] < row["interval_end_s"]) or bool(row["available_battery_ids"] and row.get("battery_id") not in row["available_battery_ids"])
            row["review_reasons"] = [reason for reason in row["review_reasons"] if not reason.startswith("Choose the battery monitor")]
            self._put(row)
            self._analyze_row(row, cancel_check, progress)
            compared = self._cache_comparisons(cancel_check, progress, selected_id=row["id"])
            return {"flight_id": row["id"], "report_id": row.get("report_id"), "analyzed_flights": 1,
                    "compared_flights": compared, "pending_logs": self._pending_logs()}

    def _comparison_selection(self, row, rows=None):
        report = bool(row.get("report_id"))
        rows = self._visible(self._rows()) if rows is None else rows
        identity = row.get("_comparison_identity")
        compatible = [candidate for candidate in rows if candidate["id"] != row["id"] and candidate.get("report_id")
                      and identity and candidate.get("_comparison_identity") == identity
                      and candidate.get("synthetic") == row.get("synthetic")
                      and candidate.get("_time_domain") == row.get("_time_domain")
                      and candidate.get("time_basis") == row.get("time_basis")
                      and candidate.get("_complete_boundaries") and row.get("_complete_boundaries")]
        previous, basis, history = None, None, []
        reason = "A previous flight with matching aircraft identity and an established order is not available."
        if report and row.get("started_at"):
            earlier = self._ordered([item for item in compatible if item.get("started_at") and item.get("ended_at") and item["ended_at"] < row["started_at"]])
            previous = earlier[0] if earlier else None
            cutoff = (datetime.fromisoformat(row["started_at"]) - timedelta(days=30)).isoformat()
            history = [item for item in earlier if item["started_at"] >= cutoff][:30]
            basis = "recorded_dates_and_" + row["aircraft"]["identity_basis"] if previous else None
        elif report and identity:
            earlier = [item for item in compatible if item.get("_source_sha256") == row.get("_source_sha256")
                       and item.get("source_key") == row.get("source_key") and item.get("boot_epoch") == row.get("boot_epoch")
                       and item.get("_record_span") and row.get("_record_span")
                       and item["_record_span"].get("end") is not None and item["_record_span"]["end"] < row["_record_span"]["start"]]
            previous = max(earlier, key=lambda item: item["_record_span"]["end"], default=None)
            basis = "same_source_clock_and_record_order" if previous else None
        if previous and report:
            reason = "Descriptive comparison with the preceding compatible flight. Conditions and causation still need review."
            if row["aircraft"]["identity_basis"] == "operator_declared":
                reason += " Aircraft grouping was declared by the user; hardware identity is not verified."
        elif not identity:
            reason = "Identify this aircraft in Flight context to group its flights. System numbers and filenames are not aircraft identity."
        elif not row.get("_complete_boundaries"):
            reason = "Flight boundaries are incomplete or unknown; an automatic previous-flight comparison would mix uncertain intervals."
        elif not row.get("started_at"):
            reason = "Flight date is unknown. A previous interval can only be selected within this same source and clock segment."
        return previous, reason, basis, history

    @staticmethod
    def _comparison_key(before, after):
        return _hash([VERSION, before["report_id"], after["report_id"]])

    def _cached_comparison(self, before, after):
        if not before or not after.get("report_id"):
            return None
        with self.store._connect() as db:
            row = db.execute("SELECT data FROM flight_library_comparisons WHERE cache_key=?",
                             (self._comparison_key(before, after),)).fetchone()
        return json.loads(row[0]) if row else None

    def _comparison_pending(self, row):
        if not row.get("report_id"):
            return False
        before = self._comparison_selection(row)[0]
        return bool(before and self._cached_comparison(before, row) is None)

    def _cache_comparisons(self, cancel_check=None, progress=None, selected_id=None):
        count = 0
        rows = self._ordered(self._visible(self._rows()))
        if selected_id:
            rows.sort(key=lambda row: row["id"] != selected_id)
        for row in rows:
            check_cancel(cancel_check)
            if count >= MAX_REPORTS_PER_REFRESH:
                break
            if not row.get("report_id"):
                continue
            before = self._comparison_selection(row, rows)[0]
            if not before or self._cached_comparison(before, row) is not None:
                continue
            left = self.store.get_document("insight_report", before["report_id"])
            right = self.store.get_document("insight_report", row["report_id"])
            comparison = compare_reports(left, right, self.store, cancel_check=cancel_check, progress=progress)
            check_cancel(cancel_check)
            with self.store._connect() as db:
                db.execute("INSERT OR IGNORE INTO flight_library_comparisons VALUES (?,?)",
                           (self._comparison_key(before, row), json.dumps(comparison, allow_nan=False)))
            count += 1
        return count

    def detail(self, params, cancel_check=None, progress=None):
        identity = text_field(params, "flight_id")
        if identity.startswith("flight-"):
            row = next((row for row in self._current_rows() if row["id"] == identity), None)
            if row is None:
                raise ValueError("Unknown library flight ID. Refresh the flight library.")
        else:
            row = self._get(identity)
        report = self.store.get_document("insight_report", row["report_id"]) if row.get("report_id") else None
        previous, reason, basis, history = self._comparison_selection(row)
        comparison = self._cached_comparison(previous, row)
        if comparison and isinstance(comparison.get("configuration_comparison"), dict):
            comparison["configuration_comparison"] = {key: comparison["configuration_comparison"].get(key) for key in
                                                       ("status", "eligible_for_parameter_review", "reasons", "limitations")}
        result = {"flight": _public(row), "report": report, "comparison": comparison,
                "previous_flight": _public(previous) if previous else None, "comparison_reason": reason,
                "comparison_basis": basis, "comparison_pending": bool(previous and comparison is None),
                "recent_history": [_public(item) for item in history], "review_reasons": row.get("review_reasons", []),
                "observed_context": row.get("_observed_context")}
        if len(json.dumps(result, allow_nan=False).encode()) > MAX_DETAIL_BYTES:
            result["comparison"] = None
            result["detail_omitted_reason"] = "The combined detail exceeds the response limit. The saved comparison remains available through the report comparison tools."
            result["comparison_pending"] = False
        if len(json.dumps(result, allow_nan=False).encode()) > MAX_DETAIL_BYTES:
            result["report"] = None
            result["detail_omitted_reason"] = "The full report exceeds the flight detail response limit. Open the saved report in advanced analysis."
        return result
