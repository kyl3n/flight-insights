"""Connect recorder-owned observation and readback to the durable flight catalog."""
from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import nullcontext
import json
import logging
from pathlib import Path
import re
import threading

from .collection import CollectionService
from .recorder import RecorderService


class CollectionController:
    def __init__(self, store, jobs, *, recording_guard=None):
        self.store, self.jobs = store, jobs
        # Constructor-owned guard only; renderer data cannot supply callbacks.
        # Native repair leases must be revoked before opening a recorder socket.
        self._recording_guard = recording_guard or nullcontext
        self._lock = threading.RLock()
        self.collection = CollectionService(store)
        self.recorder = RecorderService(store.root, on_event=self.collection.ingest_event)
        self._archive_jobs = {}
        recovered = self.recorder.recovered()
        live_ids = {item["capture_session_id"] for item in recovered if item.get("active")}
        self.collection.recover_interrupted(exclude_capture_ids=live_ids)

    @staticmethod
    def _worker_config(profile):
        connection = profile["connection"]
        config = {"bind_host": connection["local_address"], "bind_port": connection["listen_port"],
                "allowed_sources": [connection["expected_sender"]],
                "expected_system_id": connection["system_id"],
                "expected_component_id": connection["component_id"],
                "feed_origin": connection["feed_origin"], "synthetic": connection["synthetic"]}
        if connection.get("readback"):
            config["readback"] = {"feed_origin": "direct_bidirectional", "transport": "shared_recorder",
                "local_address": connection["local_address"], "local_port": connection["listen_port"],
                "remote_address": connection["expected_sender"], "remote_port": connection["readback"]["remote_port"],
                "system_id": connection["system_id"], "component_id": connection["component_id"],
                "expected_uid": connection["expected_uid"]}
            for key in ("client_system_id", "client_component_id"):
                if key in connection["readback"]:
                    config["readback"][key] = connection["readback"][key]
        return config

    def _refresh(self):
        current = self.recorder.status()
        capture_id = current.get("capture_session_id")
        if capture_id:
            self.collection.update_recorder_status(capture_id, current)
        # Cancellation can happen while a job is queued, before our handler has
        # a chance to record the failed archive. Reconcile the durable job state.
        for capture in self.collection.pending_archives():
            if not capture.get("archive_job_id"):
                continue
            job = self.jobs.dispatch("jobs.get", {"job_id": capture["archive_job_id"]})["job"]
            if job["state"] in {"failed", "cancelled", "interrupted"}:
                self.collection.mark_archive_failed(capture["uuid"],
                    job.get("error", {}).get("message", f"Archive job {job['state']}; recorder files remain available."))
        return current

    def safety_state(self):
        current = self.recorder.status()
        result = self.collection.safety_state(current.get("capture_session_id"))
        result["flight_state"] = result.get("armed_state", "unknown")
        result.setdefault("identity_verified", False)
        result["recording_active"] = bool(current.get("active"))
        result["owned_udp"] = current.get("bindings", []) if current.get("active") else []
        # A second coordinator's recorder must also retain its socket ownership.
        for recovered in self.recorder.recovered():
            if recovered.get("active") and recovered.get("capture_session_id") != current.get("capture_session_id"):
                result["recording_active"] = True
                result["owned_udp"].extend(recovered.get("bindings", []))
        if (current.get("callback_error") or current.get("state") != "recording"
                or current.get("event_backlog_bytes", 0) > 256 * 1024):
            result.update(flight_state="unknown", identity_verified=False, heartbeat_age_s=None)
        return result

    def shared_bootstrap_ready(self, connection):
        if not self.recorder.shared_bootstrap_ready(connection):
            return False
        capture_id = self.recorder.status().get("capture_session_id")
        with self.collection._lock, self.store._connect() as db:
            capture = self.collection._get(db, "capture", capture_id)
            source = capture.get("source_states", {}).get(capture.get("selected_source_id"), {})
            return (capture["status"] == "observing" and bool(capture.get("selected_source_id"))
                    and not capture.get("source_ambiguity") and not capture.get("active_armed_interval_id")
                    and not source.get("heartbeat_evidence") and not source.get("identity_conflict")
                    and not source.get("continuity_lost")
                    and source.get("uid") in (None, connection["expected_uid"]))

    def dispatch(self, method, params):
        if method == "session.status":
            recorder = self._refresh()
            result = self.collection.status(params)
            result["recorder"] = recorder
            result["recovered_recorders"] = self.recorder.recovered()
            return result
        if method == "session.prepare_and_watch":
            with self._lock:
                result = self.collection.prepare(params)
                capture = result["capture_session"]
                if not result.get("operation_replayed"):
                    try:
                        with self._recording_guard():
                            state = self.recorder.start(capture["uuid"], self._worker_config(capture["profile"]))
                            if state.get("state") != "recording":
                                raise ValueError(str(state.get("failure") or "Recorder did not establish its socket."))
                            self.collection.mark_recorder_started(capture["uuid"], state)
                    except Exception as error:
                        # If catalog publication fails after the socket opened,
                        # do not leave an unrepresented recorder running.
                        try:
                            current = self.recorder.status()
                            if current.get("capture_session_id") == capture["uuid"]:
                                self.recorder.stop("capture_start_failed")
                        except Exception:
                            logging.exception("Failed capture start requires recorder recovery")
                        self.collection.mark_recorder_failed(capture["uuid"], str(error))
                updated = self.dispatch("session.status", {"capture_session_id": capture["uuid"]})
                return {**result, **updated}
        if method == "session.stop":
            with self._lock:
                validated = self.collection.validate_stop(params)
                capture = validated["capture_session"]
                capture_id = capture["uuid"]
                if validated.get("operation_replayed"):
                    result = self.collection.stop_capture(params)
                    return {**result, **self.dispatch("session.status", {"capture_session_id": capture_id})}
                current = self.recorder.status()
                if current.get("capture_session_id") == capture_id:
                    stopped = self.recorder.stop("operator")
                    self.collection.update_recorder_status(capture_id, stopped)
                    if not stopped.get("artifacts"):
                        stopped = self._recovered_artifacts(capture_id)
                else:
                    saved = next((item for item in self.recorder.recovered()
                                  if item["capture_session_id"] == capture_id), None)
                    if saved and saved.get("active"):
                        raise ValueError("Another running coordinator owns this capture. Stop it from that application.")
                    stopped = self._recovered_artifacts(capture_id)
                result = self.collection.stop_capture(params)
                try:
                    job = self._queue_archive(capture_id, stopped)
                except ValueError as error:
                    # Capture is stopped and originals retained even when the job
                    # queue is full. A new explicit stop operation retries archival.
                    self.collection.mark_archive_failed(capture_id, str(error))
                    result["archive_error"] = str(error)
                else:
                    result["archive_job"] = job
                return {**result, **self.dispatch("session.status", {"capture_session_id": capture_id})}
        return self.collection.dispatch(method, params)

    def _recovered_artifacts(self, capture_id):
        directory = self.recorder.root / capture_id
        if directory.is_symlink():
            raise ValueError("Recovered capture directory must not be a symbolic link.")
        manifest = directory / "artifacts.json"
        if manifest.is_file() and not manifest.is_symlink():
            with manifest.open("rb") as stream:
                encoded = stream.read(4 * 1024 * 1024 + 1)
            if len(encoded) > 4 * 1024 * 1024:
                raise ValueError("Recorder artifact metadata exceeds its size limit.")
            value = json.loads(encoded)
            return {"artifacts": value["artifacts"], "state": "interrupted"}
        # A crash can precede the final manifest. Retain actual completed bytes
        # but do not invent record spans or mark these chunks complete.
        artifacts = []
        if directory.is_dir() and not directory.is_symlink():
            for path in sorted(directory.iterdir()):
                if re.fullmatch(r"raw-\d{6}\.udp", path.name):
                    role = "raw_udp_capture"
                elif re.fullmatch(r"telemetry-\d{6}\.tlog", path.name):
                    role = "app_mavlink_capture"
                elif path.name == "events.jsonl":
                    role = "recorder_journal"
                else:
                    continue
                if path.is_file() and not path.is_symlink():
                    artifacts.append({"path": str(path), "role": role, "complete": False})
        return {"artifacts": artifacts, "state": "interrupted"}

    def _queue_archive(self, capture_id, stopped):
        capture = self.collection.status({"capture_session_id": capture_id})["capture_session"]
        existing = self._archive_jobs.get(capture_id) or capture.get("archive_job_id")
        if existing:
            job = self.jobs.dispatch("jobs.get", {"job_id": existing})["job"]
            if job["state"] in {"queued", "running", "succeeded"}:
                return job
        self.collection.mark_finalizing(capture_id)
        job = self.jobs.start("session.archive", {"capture_session_id": capture_id,
                             "artifacts": stopped.get("artifacts", [])}, self._archive)["job"]
        self._archive_jobs[capture_id] = job["id"]
        self.collection.mark_finalizing(capture_id, job_id=job["id"])
        return job

    def _archive(self, method, params, *, cancel_check=None, progress=None):
        capture_id = params["capture_session_id"]
        directory = self.recorder.root / capture_id
        if directory.is_symlink():
            raise ValueError("Capture directory must not be a symbolic link.")
        directory = directory.resolve()
        attached = 0
        try:
            if not params["artifacts"]:
                raise ValueError("No recorder artifacts were retained; this capture has no archived stream evidence.")
            for artifact in params["artifacts"]:
                if cancel_check and cancel_check():
                    raise CancelledError("Capture archival cancelled; local recorder files remain.")
                path = Path(artifact["path"])
                if path.is_symlink() or path.resolve().parent != directory:
                    raise ValueError("Recorder artifact path is outside its capture directory.")
                role = artifact["role"]
                patterns = {"raw_udp_capture": r"raw-\d{6}\.udp",
                            "app_mavlink_capture": r"telemetry-\d{6}\.tlog",
                            "recorder_journal": r"events\.jsonl"}
                if role not in patterns or not re.fullmatch(patterns[role], path.name):
                    raise ValueError("Recorder artifact role does not match its expected capture filename.")
                digest = artifact.get("sha256")
                if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                    raise ValueError("Invalid recorder artifact digest.")
                trusted_receipt = bool(digest) and type(artifact.get("bytes")) is int and artifact["bytes"] >= 0
                source = self.store.archive_file(path, cancel_check=cancel_check, progress=progress,
                                                acquisition_method="app_owned_passive_udp_capture")
                if digest and source["sha256"] != digest:
                    raise ValueError("Recorder artifact digest changed after capture; association withheld.")
                if trusted_receipt and source["byte_size"] != artifact["bytes"]:
                    raise ValueError("Recorder artifact byte count changed after capture; association withheld.")
                item = {"source": source, "role": role,
                        "coverage": {"complete_chunk": trusted_receipt and artifact.get("complete") is True,
                                     "original_chunk": path.name, "record_scope": "capture_global"}}
                for field in ("record_start", "record_end"):
                    if trusted_receipt and artifact.get(field) is not None:
                        item[field] = artifact[field]
                if (type(item.get("record_start")) is int and type(item.get("record_end")) is int
                        and item["record_start"] > item["record_end"]):
                    # A valid empty chunk has no MAVLink record membership (it
                    # may still contain malformed/raw UDP worth retaining).
                    item.pop("record_start")
                    item.pop("record_end")
                    item["coverage"]["empty_record_span"] = True
                # Missing ranges from an interrupted file are never promoted to
                # a claimed interval membership; retain the capture-level source.
                if "record_start" not in item or "record_end" not in item:
                    item["coverage"]["interval_membership"] = "unknown"
                self.collection.attach_capture_artifacts(capture_id, [item])
                attached += 1
            self.collection.mark_archive_complete(capture_id)
            return {"capture_session_id": capture_id, "archived_artifact_count": attached}
        except BaseException as error:
            self.collection.mark_archive_failed(capture_id, str(error) or type(error).__name__)
            raise

    def close(self):
        # Native shutdown has a short deadline. Seal the worker quickly and leave
        # potentially multi-gigabyte archival for explicit recovery, never pretend
        # closing the app was a disarm event.
        try:
            stopped = self.recorder.close()
            capture_id = stopped.get("capture_session_id")
            if capture_id:
                self.collection.update_recorder_status(capture_id, stopped)
                self.collection.recover_interrupted(capture_ids=[capture_id])
        except Exception:
            logging.exception("Recorder shutdown needs recovery on the next launch")
