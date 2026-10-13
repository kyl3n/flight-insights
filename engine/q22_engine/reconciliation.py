"""Bounded explicit-folder recovery and content-based candidate reports.

This imports evidence; it never changes the selected directory, starts a watcher,
downloads aircraft data, or promotes a candidate to a verified flight association.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .collection import CollectionService
from .logs import LogService, check_cancel, source_key
from .parameters import parse_parameters, text_field
from .store import MAX_RAW_BYTES, Store


ALGORITHM = "q22-offline-reconciliation/1"
EXTENSIONS = {".bin", ".log", ".tlog", ".param", ".params"}
MAX_ENTRIES = 4096
MAX_CANDIDATES = 256
MAX_SAMPLES = 12
LIMITATIONS = [
    "Filename, directory, modification date, system ID and parameter equality are not flight identity.",
    "UID and CRC observations are not cryptographic authentication; replay remains possible.",
    "Boot segment numbers are local to each decoder, not cross-file boot identities.",
    "Recorded receive UTC and boot clocks have no validated cross-source transform here.",
    "Candidates remain provisional or rejected; this report does not complete a flight bundle or a retrieval request.",
    "A parameter export is reference evidence with unknown capture time and historical applicability.",
]


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _bound(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}.")
    return value


def _numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _sample(values, value, maximum=MAX_SAMPLES):
    if value not in values and len(values) < maximum:
        values.append(value)


def _indirection(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _canonical_decimal(value):
    number = Decimal(value)
    sign, digits, exponent = number.as_tuple()
    text = "".join(str(digit) for digit in digits)
    trimmed = text.rstrip("0") or "0"
    return [sign, trimmed, 0 if trimmed == "0" else exponent + len(text) - len(trimmed)]


class ReconciliationService:
    def __init__(self, store: Store, logs=None, collection=None):
        self.store = store
        self.logs = logs or LogService(store)
        self.collection = collection or CollectionService(store)
        with self.store._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS reconciliation_operations "
                       "(operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, state TEXT NOT NULL, report_id TEXT)")

    def dispatch(self, method, params, *, cancel_check=None, progress=None):
        if not isinstance(params, dict):
            raise ValueError("Reconciliation params must be an object.")
        if method == "reconciliation.scan_folder":
            return self.scan_folder(params, cancel_check=cancel_check, progress=progress)
        if method == "reconciliation.index_source":
            return self.index_source(params, cancel_check=cancel_check, progress=progress)
        if method == "reconciliation.get":
            return {"report": self.store.get_document("reconciliation_report", text_field(params, "report_id"))}
        if method == "reconciliation.list":
            limit = _bound(params.get("limit", 20), "limit", 1, 100)
            offset = _bound(params.get("offset", 0), "offset", 0, 1_000_000)
            reports = self.store.list_documents("reconciliation_report", limit, offset)
            return {"reports": [{k: v for k, v in report.items() if k not in ("candidates", "files", "target", "skipped_examples")}
                                for report in reports], "next_offset": offset + len(reports) if len(reports) == limit else None}
        raise ValueError("Unknown reconciliation method.")

    def _target(self, params):
        identity = text_field(params, "flight_record_id")
        with self.collection._lock, self.store._connect() as db:
            record = self.collection._get(db, "flight_record", identity)
            interval = self.collection._get(db, "interval", record["armed_interval_id"])
            capture = self.collection._get(db, "capture", record["capture_session_id"])
            profile = capture["profile"]
            if params.get("target_profile_version") != profile["aircraft_profile_version"]:
                raise ValueError("Reconciliation profile version is stale or incorrect.")
            if params.get("applicability_fingerprint") != capture["applicability_fingerprint"]:
                raise ValueError("Reconciliation applicability fingerprint is stale or incorrect.")
            # Read all matching identity observations, with a hard bound. Summary
            # identity lists in imported logs are deliberately not authoritative.
            rows = db.execute("SELECT data FROM collection_events WHERE capture_id=? AND kind='identity' LIMIT 129",
                              (capture["uuid"],)).fetchall()
        identities = [json.loads(row[0]) for row in rows[:128]]
        expected_uid = profile["connection"].get("expected_uid") or (interval.get("identity_evidence") or {}).get("uid")
        firmware = []
        end_record = (interval.get("disarm_evidence") or {}).get("record_index", interval.get("membership_end_record"))
        for observation in identities:
            if (observation.get("source_id") == interval["source_id"] and observation.get("firmware_identity")
                    and str(observation.get("boot_epoch", "unknown")) in {str(epoch) for epoch in interval["boot_epochs"]}
                    and (end_record is None or observation.get("record_index", end_record + 1) <= end_record)
                    and (not expected_uid or str(observation.get("uid")) == str(expected_uid))):
                _sample(firmware, observation["firmware_identity"])
        return {"flight_record_id": identity, "armed_interval_id": interval["uuid"],
                "capture_session_id": capture["uuid"], "manifest_revision": record["latest_manifest_revision"],
                "aircraft_id": profile["aircraft_id"], "expected_uid": expected_uid,
                "declared_system_id": profile["connection"]["system_id"],
                "declared_component_id": profile["connection"]["component_id"],
                "source_id": interval["source_id"], "boot_epochs": interval["boot_epochs"],
                "arm_evidence": interval["arm_evidence"], "disarm_evidence": interval["disarm_evidence"],
                "membership_end_record": interval.get("membership_end_record"),
                "firmware_observations": firmware, "identity_observations_truncated": len(rows) > 128,
                "coverage_warnings": interval["coverage_warnings"],
                "identity_verification": "declared_and_passively_observed_not_authenticated"}

    def _normalize(self, params):
        folder = Path(text_field(params, "folder", 4096)).expanduser()
        if not folder.is_absolute():
            raise ValueError("Select an explicit absolute folder path.")
        if _indirection(folder) or any(_indirection(parent) for parent in folder.parents):
            raise ValueError("Selected folder and its parents must not be symbolic links.")
        folder = folder.resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("Select an existing SD/export folder.")
        synthetic = params.get("synthetic", False)
        if type(synthetic) is not bool:
            raise ValueError("synthetic must be boolean.")
        return {"folder": str(folder), "max_files": _bound(params.get("max_files", 32), "max_files", 1, 128),
                "max_depth": _bound(params.get("max_depth", 0), "max_depth", 0, 4),
                "max_file_bytes": _bound(params.get("max_file_bytes", 2 * 1024**3), "max_file_bytes", 1, MAX_RAW_BYTES),
                "max_total_bytes": _bound(params.get("max_total_bytes", 2 * 1024**3), "max_total_bytes", 1, 16 * 1024**3),
                "synthetic": synthetic}

    def _walk(self, folder, config, report, cancel_check):
        # No watcher and no implicit recursion, mount discovery, symlink traversal
        # or directory-list accumulation. Every visited entry consumes the bound.
        pending = [(folder, 0)]
        seen = 0
        selected_device = folder.stat().st_dev
        while pending:
            directory, depth = pending.pop()
            if _indirection(directory) or not directory.resolve(strict=True).is_relative_to(folder):
                report["skipped_count"] += 1
                _sample(report["skipped_examples"], {"path": str(directory), "reason": "directory_changed_or_symlink"})
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_cancel(cancel_check)
                    seen += 1
                    if seen > MAX_ENTRIES:
                        report["limits_reached"].append("directory_entry_limit")
                        return
                    path = Path(entry.path)
                    if entry.is_symlink() or _indirection(path):
                        report["skipped_count"] += 1
                        _sample(report["skipped_examples"], {"path": str(path), "reason": "symbolic_link"})
                        continue
                    # Windows DirEntry.stat() omits file identity (st_dev and
                    # st_ino are zero). Use fresh, non-following path metadata
                    # both for filesystem boundaries and the later fstat check.
                    stamp = path.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        if stamp.st_dev != selected_device:
                            report["skipped_count"] += 1
                            _sample(report["skipped_examples"], {"path": str(path), "reason": "different_filesystem_requires_explicit_selection"})
                        elif depth < config["max_depth"]:
                            pending.append((path, depth + 1))
                        continue
                    if path.suffix.lower() not in EXTENSIONS:
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        report["skipped_count"] += 1
                        _sample(report["skipped_examples"], {"path": str(path), "reason": "not_regular_file"})
                        continue
                    yield path, stamp

    @staticmethod
    def _stage(path, selected, expected, destination, remaining, cancel_check, progress, on_bytes):
        # Copy through a checked descriptor. The immutable importer only receives
        # this private, stable copy, so a later replacement/symlink cannot cause it
        # to read a different file outside the explicitly selected tree.
        if _indirection(path) or any(_indirection(parent) for parent in path.parents):
            raise ValueError("Candidate became a symbolic link.")
        if not path.resolve(strict=True).is_relative_to(selected):
            raise ValueError("Candidate escaped the selected folder.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        count = 0
        with os.fdopen(descriptor, "rb") as source, destination.open("xb") as output:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns):
                raise ValueError("Candidate changed since folder enumeration.")
            while data := source.read(1024 * 1024):
                check_cancel(cancel_check)
                count += len(data)
                on_bytes(len(data))
                if count > remaining:
                    raise ValueError("Candidate exceeds the selected byte budget.")
                output.write(data)
                if progress:
                    progress("copying selected evidence", count, before.st_size)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Candidate changed while being copied; its bytes are not accepted as a stable original.")
            output.flush()
            os.fsync(output.fileno())
        return count

    def scan_folder(self, params, *, cancel_check=None, progress=None):
        operation = text_field(params, "operation_id", 160)
        digest = _hash(params)
        # A completed operation remains retrievable when its removable source
        # folder is no longer mounted. Payload conflicts are still refused.
        with self.store._connect() as db:
            prior = db.execute("SELECT payload_hash,state,report_id FROM reconciliation_operations WHERE operation_id=?", (operation,)).fetchone()
        if prior:
            if prior[0] != digest:
                raise ValueError("operation_id already belongs to a different reconciliation request.")
            if prior[2]:
                return {"report": self.store.get_document("reconciliation_report", prior[2]), "operation_replayed": True}
            raise ValueError("This operation is running or was interrupted; inspect imported evidence and use a new operation ID to retry explicitly.")
        target = self._target(params)
        config = self._normalize(params)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload_hash,state,report_id FROM reconciliation_operations WHERE operation_id=?", (operation,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ValueError("operation_id already belongs to a different reconciliation request.")
                if prior[2]:
                    return {"report": self.store.get_document("reconciliation_report", prior[2]), "operation_replayed": True}
                raise ValueError("This operation is running or was interrupted; inspect imported evidence and use a new operation ID to retry explicitly.")
            db.execute("INSERT INTO reconciliation_operations VALUES (?,?,'running',NULL)", (operation, digest))
        report = {"algorithm_version": ALGORITHM, "operation_id": operation, "target": target,
                  "selected_folder": config["folder"], "bounds": config, "synthetic": config["synthetic"],
                  "state": "completed", "files": [], "candidates": [], "skipped_count": 0,
                  "skipped_examples": [], "limits_reached": [], "bytes_copied": 0,
                  "verified_candidate_count": 0, "bundle_completeness": "not_established",
                  "limitations": LIMITATIONS, "immutable": True}
        saved = None
        error = None
        try:
            staging = self.store.root / "scratch"
            if _indirection(staging):
                raise ValueError("Reconciliation scratch directory must not be a symbolic link.")
            staging.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="reconcile-", dir=staging) as directory:
                for path, stamp in self._walk(Path(config["folder"]), config, report, cancel_check):
                    check_cancel(cancel_check)
                    if len(report["files"]) >= config["max_files"]:
                        report["limits_reached"].append("file_count_limit")
                        break
                    remaining = min(config["max_file_bytes"], config["max_total_bytes"] - report["bytes_copied"])
                    if stamp.st_size > remaining:
                        report["skipped_count"] += 1
                        _sample(report["skipped_examples"], {"path": str(path), "reason": "selected_byte_budget"})
                        if "byte_budget" not in report["limits_reached"]:
                            report["limits_reached"].append("byte_budget")
                        continue
                    item = {"selected_path": str(path), "original_name": path.name, "state": "pending"}
                    report["files"].append(item)
                    # Preserve the original basename; source.original_path points
                    # to the stable staging copy. selected_path above records the
                    # explicit acquisition location, never an observed SD log ID.
                    subdir = Path(directory) / str(len(report["files"]))
                    subdir.mkdir(mode=0o700)
                    staged = subdir / path.name
                    try:
                        def count_bytes(amount):
                            report["bytes_copied"] += amount
                        self._stage(path, Path(config["folder"]), stamp, staged, remaining, cancel_check, progress, count_bytes)
                        if path.suffix.lower() in {".param", ".params"}:
                            candidate = self._parameter(staged, item, config["synthetic"], cancel_check)
                            candidates = [candidate]
                        else:
                            imported = self.logs.import_log({"path": str(staged), "label": path.name[:512],
                                                             "synthetic": config["synthetic"]}, cancel_check=cancel_check, progress=progress)["log"]
                            item.update(log_id=imported["id"], source=imported["source"], state="imported")
                            log = self.store.get_document("log", imported["id"])
                            candidates = self._log_candidates(log, target, cancel_check)
                        available = MAX_CANDIDATES - len(report["candidates"])
                        report["candidates"].extend(candidates[:available])
                        if len(candidates) > available:
                            if "candidate_count_limit" not in report["limits_reached"]:
                                report["limits_reached"].append("candidate_count_limit")
                        item["candidate_count"] = min(len(candidates), available)
                    except CancelledError:
                        item["state"] = "interrupted"
                        raise
                    except (OSError, ValueError, UnicodeError) as failure:
                        item.update(state="failed", error=str(failure)[:600])
                    finally:
                        if "source" not in item:
                            # The existing decoder may fail/cancel after immutable
                            # publication. Keep that original discoverable in this
                            # partial report instead of losing its catalog handle.
                            with self.store._connect() as db:
                                retained = db.execute("SELECT data FROM sources WHERE json_extract(data,'$.original_path')=? ORDER BY created_at DESC LIMIT 1",
                                                      (str(staged.resolve()),)).fetchone()
                            if retained:
                                item["source"] = json.loads(retained[0])
                        staged.unlink(missing_ok=True)
            if report["limits_reached"] or any(item["state"] != "imported" for item in report["files"]):
                report["state"] = "partial"
        except BaseException as failure:
            error = failure
            report.update(state="cancelled" if isinstance(failure, CancelledError) else "interrupted", error=str(failure)[:600])
        finally:
            report["candidate_count"] = len(report["candidates"])
            report["imported_count"] = sum(item["state"] == "imported" for item in report["files"])
            report["candidate_states"] = {state: sum(c["state"] == state for c in report["candidates"]) for state in ("provisional", "rejected")}
            saved = self.store.add_document("reconciliation_report", report)
            with self.store._connect() as db:
                db.execute("UPDATE reconciliation_operations SET state=?,report_id=? WHERE operation_id=?", (report["state"], saved["id"], operation))
        if error:
            raise error
        return {"report": saved, "operation_replayed": False}

    def index_source(self, params, *, cancel_check=None, progress=None):
        """Index an existing immutable acquired original, without another device read."""
        operation = text_field(params, "operation_id", 160)
        digest = _hash(params)
        with self.store._connect() as db:
            prior = db.execute("SELECT payload_hash,state,report_id FROM reconciliation_operations WHERE operation_id=?", (operation,)).fetchone()
        if prior:
            if prior[0] != digest:
                raise ValueError("operation_id already belongs to a different reconciliation request.")
            if prior[2]:
                return {"report": self.store.get_document("reconciliation_report", prior[2]), "operation_replayed": True}
            raise ValueError("This reconciliation operation is running/interrupted; retry explicitly with a new operation ID.")
        target = self._target(params)
        source_id = text_field(params, "source_id")
        kind = params.get("source_kind", "log")
        if kind not in ("log", "parameter"):
            raise ValueError("source_kind must be log or parameter.")
        synthetic = params.get("synthetic", False)
        if type(synthetic) is not bool:
            raise ValueError("synthetic must be boolean.")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                raise ValueError("Select an existing archived source ID.")
            source = json.loads(row[0])
            try:
                db.execute("INSERT INTO reconciliation_operations VALUES (?,?,'running',NULL)", (operation, digest))
            except sqlite3.IntegrityError as failure:
                raise ValueError("This operation was concurrently claimed; query the prior report or retry explicitly.") from failure
        report = {"algorithm_version": ALGORITHM, "operation_id": operation, "target": target,
                  "selected_source_id": source_id, "selected_folder": None, "synthetic": synthetic,
                  "state": "completed", "files": [], "candidates": [], "skipped_count": 0,
                  "skipped_examples": [], "limits_reached": [], "bytes_copied": 0,
                  "verified_candidate_count": 0, "bundle_completeness": "not_established",
                  "limitations": LIMITATIONS, "immutable": True}
        item = {"source": source, "original_name": source["original_name"], "state": "pending", "parent_source_id": source_id}
        report["files"].append(item)
        error = None
        try:
            check_cancel(cancel_check)
            path = self.store.raw_path(source["sha256"])
            if kind == "parameter":
                report["candidates"] = [self._parameter(path, item, synthetic, cancel_check)]
                if item["source"]["sha256"] != source["sha256"]:
                    raise ValueError("Archived source bytes no longer match their original digest.")
            else:
                imported = self.logs.import_log({"path": str(path), "label": source["original_name"][:512], "synthetic": synthetic},
                                                cancel_check=cancel_check, progress=progress)["log"]
                item.update(log_id=imported["id"], state="imported")
                if imported["source"]["sha256"] != source["sha256"]:
                    raise ValueError("Archived source bytes no longer match their original digest.")
                report["candidates"] = self._log_candidates(self.store.get_document("log", imported["id"]), target, cancel_check)
            item["candidate_count"] = len(report["candidates"])
        except BaseException as failure:
            error = failure
            report["candidates"] = []
            item.update(state="interrupted" if isinstance(failure, CancelledError) else "failed", error=str(failure)[:600])
            report.update(state="cancelled" if isinstance(failure, CancelledError) else "interrupted", error=str(failure)[:600])
        finally:
            report["candidate_count"] = len(report["candidates"])
            report["imported_count"] = int(item["state"] == "imported")
            report["candidate_states"] = {state: sum(c["state"] == state for c in report["candidates"]) for state in ("provisional", "rejected")}
            saved = self.store.add_document("reconciliation_report", report)
            with self.store._connect() as db:
                db.execute("UPDATE reconciliation_operations SET state=?,report_id=? WHERE operation_id=?", (report["state"], saved["id"], operation))
        if error:
            raise error
        return {"report": saved, "operation_replayed": False}

    def _parameter(self, path, item, synthetic, cancel_check):
        source = self.store.archive_file(path, cancel_check=cancel_check)
        item["source"] = source
        check_cancel(cancel_check)
        values, metadata = parse_parameters(self.store.raw_path(source["sha256"]))
        check_cancel(cancel_check)
        canonical = [[name, entry.get("system_id"), entry.get("component_id"), entry.get("mav_param_type"),
                      _canonical_decimal(entry["value"])] for name, entry in sorted(values.items())]
        reference = self.store.add_document("parameter_reference", {"source": source, "parameters": values,
            **metadata, "parameter_count": len(values), "canonical_hash": _hash(canonical),
            "canonicalization": "component-name-reported-type-lossless-decimal-tuple/1",
            "identity": None, "observed_at": None, "completeness": "unknown",
            "flight_time_applicability": "unknown", "source_kind": "file_export_reference", "immutable": True,
            "synthetic": synthetic})
        item.update(state="imported", parameter_reference_id=reference["id"])
        return {"kind": "parameter_export", "state": "provisional", "synthetic": synthetic, "parameter_reference_id": reference["id"],
                "artifact_sha256": source["sha256"], "parameter_count": len(values),
                "canonical_hash": reference["canonical_hash"], "supporting_evidence": [], "contradictions": [],
                "missing_evidence": ["Aircraft identity, firmware, capture time, acquisition completeness and historical applicability are unknown."],
                "proposed_association": None}

    def _log_candidates(self, log, target, cancel_check):
        self.logs.verify_artifact(log, cancel_check)
        reducers = {(stream["source_key"], stream["boot_epoch"]): _Evidence(stream) for stream in log["streams"]}
        for event in self.logs.events(log, cancel_check=cancel_check):
            check_cancel(cancel_check)
            key = source_key(event), event.get("boot_epoch", 0)
            if key in reducers:
                reducers[key].observe(event)
        return [value.candidate(log, target) for value in reducers.values()]


class _Evidence:
    def __init__(self, stream):
        self.stream = stream
        self.uids, self.firmware, self.clock_anchors, self.intervals = [], [], [], []
        self.uid_conflicts = False
        self.record_start = self.record_end = self.boot_start = self.boot_end = self.receive_start = self.receive_end = None
        self.armed = None
        self.open_arm = None
        self.arm_count = self.identity_count = self.parameter_count = self.quality_count = 0
        self.truncated = False
        self.continuity_uncertain = False

    def observe(self, event):
        record = _numeric(event.get("record_index"))
        if record is not None:
            self.record_start = record if self.record_start is None else min(self.record_start, record)
            self.record_end = record if self.record_end is None else max(self.record_end, record)
        for key, first, last in (("boot_time_us", "boot_start", "boot_end"), ("receive_time_us", "receive_start", "receive_end")):
            value = _numeric(event.get(key))
            if value is not None:
                setattr(self, first, value if getattr(self, first) is None else min(getattr(self, first), value))
                setattr(self, last, value if getattr(self, last) is None else max(getattr(self, last), value))
        diagnostic = bool(event.get("quality_flags")) or event.get("kind") == "diagnostic"
        self.quality_count += diagnostic
        if diagnostic:
            self.continuity_uncertain = True
            if self.open_arm:
                self.open_arm["coverage"] = "uncertain_diagnostic_within_interval"
        self.parameter_count += event.get("kind") == "parameter"
        raw = event.get("raw_fields", {})
        name = event.get("message_type")
        if name == "AUTOPILOT_VERSION":
            self.identity_count += 1
            uid = _numeric(raw.get("uid"))
            if uid:
                if self.uids and str(uid) not in self.uids:
                    self.uid_conflicts = True
                _sample(self.uids, str(uid))
            firmware = {key: raw[key] for key in ("flight_sw_version", "board_version", "vendor_id", "product_id") if raw.get(key) is not None}
            if firmware:
                _sample(self.firmware, firmware)
        if name == "SYSTEM_TIME" and _numeric(raw.get("time_unix_usec")) and _numeric(raw.get("time_boot_ms")) is not None:
            _sample(self.clock_anchors, {"record_index": record, "unix_us": str(raw["time_unix_usec"]),
                                       "boot_us": str(int(raw["time_boot_ms"]) * 1000),
                                       "validation": "reported_anchor_not_independently_validated"})
        armed = None
        if name == "HEARTBEAT" and raw.get("autopilot") != 8 and raw.get("type") != 6 and type(raw.get("base_mode")) is int:
            armed = bool(raw["base_mode"] & 128)
        elif name == "ARM" and raw.get("ArmState") in (0, 1):
            armed = bool(raw["ArmState"])
        if armed is not None:
            if armed and self.armed is not True:
                self.arm_count += 1
                self.open_arm = {"start_record": record, "end_record": None,
                                 "start_status": "unknown" if self.armed is None or self.continuity_uncertain else "observed_transition",
                                 "coverage": "uncertain" if self.continuity_uncertain else "observed_without_decoder_diagnostic",
                                 "start_time_us": str(event["time_us"]) if event.get("time_us") is not None else None}
            elif not armed and self.open_arm:
                self.open_arm.update(end_record=record, end_time_us=str(event["time_us"]) if event.get("time_us") is not None else None)
                if len(self.intervals) < MAX_SAMPLES:
                    self.intervals.append(self.open_arm)
                else:
                    self.truncated = True
                self.open_arm = None
            self.armed = armed

    def candidate(self, log, target):
        supporting, contradictions, missing = [], [], []
        expected = target.get("expected_uid")
        if self.uid_conflicts:
            contradictions.append("This source/clock segment contains conflicting nonzero hardware UID observations.")
        if expected and self.uids:
            if any(uid != str(expected) for uid in self.uids):
                contradictions.append("Observed nonzero hardware UID conflicts with the target aircraft's declared/observed UID.")
            else:
                supporting.append({"dimension": "hardware_uid", "value": str(expected), "basis": "matching_nonzero_passive_observation"})
        else:
            missing.append("Comparable nonzero target and candidate hardware UID evidence is absent.")
        common_firmware = [item for item in self.firmware if item in target["firmware_observations"]]
        if common_firmware:
            supporting.append({"dimension": "firmware_board", "matches": common_firmware})
        else:
            missing.append("No exact comparable firmware/board observation supports this candidate.")
        # Receive clocks and boot counters are reported independently. Equal or
        # disjoint values cannot alone prove a common boot or a hard contradiction.
        arm = target["arm_evidence"]
        disarm = target.get("disarm_evidence") or {}
        start, end = _numeric(arm.get("received_utc_us")), _numeric(disarm.get("received_utc_us"))
        receive_overlap = None
        if None not in (start, end, self.receive_start, self.receive_end):
            receive_overlap = max(start, self.receive_start) <= min(end, self.receive_end)
        if receive_overlap:
            supporting.append({"dimension": "recorded_receive_time", "basis": "overlap_with_unverified_clock_mapping"})
        missing.append("Independent boot/UTC alignment and flight-specific cross-source corroboration are not established.")
        intervals = self.intervals + ([self.open_arm] if self.open_arm and len(self.intervals) < MAX_SAMPLES else [])
        state = "rejected" if contradictions or log["status"] == "unsupported" else "provisional"
        if log["status"] == "unsupported":
            contradictions.append("No supported content was decoded from this artifact.")
        if self.stream.get("system_id") is not None and (self.stream["system_id"], self.stream.get("component_id")) != (target["declared_system_id"], target["declared_component_id"]):
            missing.append("Source system/component differs from the target declaration; this is not treated as unique hardware identity.")
        options = []
        role = {"bin": "onboard_bin", "tlog": "gcs_tlog"}.get(log["format"])
        if role and state != "rejected":
            for index, interval in enumerate(intervals):
                end = interval["end_record"] if interval["end_record"] is not None else self.record_end
                start = interval["start_record"]
                if start is None or end is None or end < start:
                    continue
                options.append({"observed_interval_index": index,
                    "boundary_status": "observed_disarm" if interval["end_record"] is not None else "unknown_end_observed_prefix_only",
                    "start_status": interval["start_status"], "coverage": interval["coverage"],
                    "requires_explicit_review": True, "maximum_association_state": "provisional",
                    "params": {"log_id": log["id"], "source_key": self.stream["source_key"], "boot_epoch": self.stream["boot_epoch"],
                               "role": role, "time_or_record_spans": [{"kind": "records", "start": start, "end": end}]}})
        return {"kind": "flight_log", "state": state, "synthetic": log["synthetic"], "log_id": log["id"], "artifact_sha256": log["source"]["sha256"],
                "format": log["format"], "source_key": self.stream["source_key"], "boot_epoch": self.stream["boot_epoch"],
                "record_span": {"start": self.record_start, "end": self.record_end},
                "observed_uids": self.uids, "firmware_observations": self.firmware,
                "boot_time_span_us": [str(value) if value is not None else None for value in (self.boot_start, self.boot_end)],
                "receive_time_span_us": [str(value) if value is not None else None for value in (self.receive_start, self.receive_end)],
                "recorded_receive_overlap": receive_overlap, "reported_clock_anchors": self.clock_anchors,
                "armed_intervals": intervals, "armed_interval_count": self.arm_count,
                "armed_intervals_truncated": self.truncated or self.arm_count > len(intervals),
                "parameter_observation_count": self.parameter_count, "quality_issue_count": self.quality_count,
                "supporting_evidence": supporting, "contradictions": contradictions, "missing_evidence": missing,
                "association_options": options, "proposed_association": None, "independent_flight_match": False}
