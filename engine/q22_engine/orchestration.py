"""Explicitly approved acquisition policies; passive recording remains independent.

No construction, policy save, restart, or status read authorizes network traffic.
Only a trusted prepare callback can activate a runtime policy. Aircraft state is
read from the recorder/catalog, never accepted through the renderer RPC surface.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
import copy
import hashlib
import json
import logging
import os
import threading
import time
from uuid import uuid4

import psutil

from .acquisition import _connection, _limits, _text, _integer, _client_identity
from .jobs import TERMINAL as JOB_TERMINAL

VERSION = 1
ADAPTER = {"id": "explicit_direct_udp_v1", "qualification": "experimental_not_hardware_qualified",
           "request_channel": "shared_recorder_or_dedicated_bidirectional_udp", "one_way_forwarding_supported": False,
           "vehicle_authentication": "unverified", "requires_operator_opt_in": True}
LIMITATIONS = [
    "Saved policies never authorize startup or restart traffic; each Prepare and watch requires explicit approval.",
    "The direct UDP adapter has synthetic protocol validation only; no hardware/GCS adapter is qualified.",
    "Parameter enumeration is non-atomic; completeness and freshness do not establish historical flight settings.",
    "Unknown/stale/armed observations revoke pending parameter requests while passive recording continues.",
    "Legacy log requests run only after an explicit observer stop and can suspend onboard logging.",
    "Selected log closure is an operator declaration; bytes remain provisional until separate content reconciliation.",
]
TERMINAL = {"complete", "partial", "interrupted", "failed", "cancelled"}
CONTINUITY_KEYS = ("source_id", "uid", "boot_epoch", "gap_count", "firmware_identity", "gap_observation", "latest_armed_interval_id")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class OrchestrationService:
    def __init__(self, store, jobs, acquisition, controller, *, start_monitor=True, poll_interval=.25):
        self.store, self.jobs, self.acquisition, self.controller = store, jobs, acquisition, controller
        self._lock = threading.RLock()
        self._active = {}
        self._closed = threading.Event()
        self._poll_interval = poll_interval
        self.recovery_warnings = []
        with store._connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestration_schema'").fetchone():
                versions = [row[0] for row in db.execute("SELECT version FROM orchestration_schema")]
                if versions != [VERSION]:
                    raise ValueError("Unsupported orchestration catalog version; this build will not modify or recover it.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orchestration_schema(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO orchestration_schema VALUES(1);
                CREATE TABLE IF NOT EXISTS orchestration_state(kind TEXT,id TEXT,revision INTEGER,data TEXT,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS orchestration_revisions(kind TEXT,id TEXT,revision INTEGER,data TEXT,
                    PRIMARY KEY(kind,id,revision));
                CREATE TABLE IF NOT EXISTS orchestration_operations(id TEXT PRIMARY KEY,payload_hash TEXT,response TEXT);
                CREATE TRIGGER IF NOT EXISTS orchestration_revisions_no_update BEFORE UPDATE ON orchestration_revisions
                    BEGIN SELECT RAISE(ABORT,'Orchestration revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS orchestration_revisions_no_delete BEFORE DELETE ON orchestration_revisions
                    BEGIN SELECT RAISE(ABORT,'Orchestration revisions are immutable'); END;
                CREATE INDEX IF NOT EXISTS orchestration_collection_events ON collection_events(capture_id,kind);
            """)
        self._recover()
        self._thread = threading.Thread(target=self._monitor, name="q22-policy-monitor", daemon=True) if start_monitor else None
        if self._thread:
            self._thread.start()

    def _recover(self):
        runs, total = self._rows("run", limit=1000)
        if total > 1000:
            self.recovery_warnings.append("Recovery inspected the newest 1000 policy runs; older runs remain unauthorized.")
        for run in runs:
            if run["state"] not in ("watching", "awaiting_selected_log"):
                continue
            owner = run.get("owner", {})
            try:
                process = psutil.Process(owner["pid"])
                if abs(process.create_time() - owner["created_at"]) < .01 and process.status() != psutil.STATUS_ZOMBIE:
                    continue
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, KeyError, TypeError, ValueError):
                if len(self.recovery_warnings) < 32:
                    self.recovery_warnings.append("Policy owner liveness could not be established; run remains unauthorized in this process.")
                continue
            self._save("run", run | {"state": "interrupted", "reason": "Prior owner ended; no requests were replayed."})
            requests, _ = self._rows("request", run["id"], limit=256)
            for request in requests:
                if request["state"] in ("queued", "running"):
                    with self.store._connect() as db:
                        row = db.execute("SELECT data FROM documents WHERE kind='acquisition' AND json_extract(data,'$.operation_id')=? ORDER BY created_at DESC LIMIT 1", (request["operation_id"],)).fetchone()
                    acquisition = json.loads(row[0]) if row else None
                    updates = {"state": "interrupted", "reason": "Prior runtime ended; explicit new operation required.", "applicability": "unknown", "freshness": "invalidated"}
                    if acquisition:
                        updates.update(acquisition_id=acquisition["id"], source_refs=acquisition.get("source_refs", []))
                    self._save("request", request | updates)

    def _get(self, kind, identity):
        with self.store._connect() as db:
            row = db.execute("SELECT data FROM orchestration_state WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if not row:
            raise ValueError(f"Unknown orchestration {kind}.")
        return json.loads(row[0])

    def _save(self, kind, value, db=None):
        if db is None:
            with self.store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._save(kind, value, connection)
        value = copy.deepcopy(value)
        old = db.execute("SELECT revision,data FROM orchestration_state WHERE kind=? AND id=?", (kind, value["id"])).fetchone()
        clean = lambda item: {key: val for key, val in item.items() if key not in ("revision", "updated_at")}
        if old and clean(json.loads(old[1])) == clean(value):
            return json.loads(old[1])
        value.update(revision=old[0] + 1 if old else 1, updated_at=self.store._now())
        value.setdefault("created_at", value["updated_at"])
        encoded = _json(value)
        db.execute("INSERT INTO orchestration_revisions VALUES(?,?,?,?)", (kind, value["id"], value["revision"], encoded))
        db.execute("INSERT INTO orchestration_state VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                   (kind, value["id"], value["revision"], encoded))
        return value

    def _rows(self, kind, capture_id=None, limit=100, offset=0):
        query, args = "FROM orchestration_state WHERE kind=?", [kind]
        if capture_id:
            query += " AND json_extract(data,'$.capture_session_id')=?"
            args.append(capture_id)
        with self.store._connect() as db:
            total = db.execute("SELECT count(*) " + query, args).fetchone()[0]
            rows = db.execute("SELECT data " + query + " ORDER BY json_extract(data,'$.created_at') DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
            return [json.loads(row[0]) for row in rows], total

    @staticmethod
    def preview(params):
        raw = params.get("policy")
        if not isinstance(raw, dict):
            raise ValueError("An explicit acquisition policy is required.")
        policy = {"aircraft_id": _text(raw.get("aircraft_id"), "aircraft_id"),
                  "connection": _connection(raw.get("connection")), "limits": _limits(raw.get("limits"))}
        for key, default in (("synthetic", False), ("preflight_parameters", False), ("postflight_parameters", False),
                             ("automatic_preflight_refresh", False),
                             ("confirm_log_transfer_side_effects", False)):
            if type(raw.get(key, default)) is not bool:
                raise ValueError(f"{key} must be boolean.")
            policy[key] = raw.get(key, default)
        policy["log_retrieval"] = raw.get("log_retrieval", "disabled")
        if policy["log_retrieval"] not in ("disabled", "selected_after_stop"):
            raise ValueError("log_retrieval must be disabled or selected_after_stop.")
        if policy["log_retrieval"] != "disabled" and not policy["confirm_log_transfer_side_effects"]:
            raise ValueError("Log policy requires explicit acknowledgment that transfers can suspend onboard logging.")
        policy["preflight_freshness_s"] = _integer(raw.get("preflight_freshness_s", 60), "preflight_freshness_s", 1, 300)
        policy["max_requests_per_capture"] = _integer(raw.get("max_requests_per_capture", 64), "max_requests_per_capture", 1, 256)
        return {"policy": policy, "policy_fingerprint": _hash(policy), "adapter": ADAPTER, "limitations": LIMITATIONS}

    def save_policy(self, params):
        preview = self.preview(params)
        operation_id = _text(params.get("operation_id"), "operation_id")
        fingerprint = _hash({"method": "policy.save", "policy": preview["policy"]})
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload_hash,response FROM orchestration_operations WHERE id=?", (operation_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError("operation_id belongs to a different policy payload.")
                return json.loads(old[1]) | {"operation_replayed": True}
            policy = {"id": str(uuid4()), "schema_version": VERSION, "created_at": self.store._now(), **preview}
            db.execute("INSERT INTO documents VALUES(?,?,?,?)", (policy["id"], "acquisition_policy", policy["created_at"], _json(policy)))
            result = {"policy": policy, "operation_replayed": False}
            db.execute("INSERT INTO orchestration_operations VALUES(?,?,?)", (operation_id, fingerprint, _json(result)))
            return result

    def validate_prepare(self, profile, approval):
        if approval is None:
            return None
        if len(self._active) >= 16:
            raise ValueError("Too many unresolved approved captures; finish their retrieval requests before another policy activation.")
        if not isinstance(approval, dict) or approval.get("confirm_automatic_readback") is not True or approval.get("confirm_experimental_adapter") is not True:
            raise ValueError("Prepare requires explicit automatic-readback and experimental-adapter approval.")
        if len(_json(approval).encode()) > 4096:
            raise ValueError("Policy approval exceeds its bounded metadata size.")
        saved = self.store.get_document("acquisition_policy", approval.get("policy_id"))
        if approval.get("policy_fingerprint") != saved["policy_fingerprint"]:
            raise ValueError("Policy fingerprint changed; review the saved policy again.")
        policy, observe = saved["policy"], profile["connection"]
        connection = policy["connection"]
        if policy["aircraft_id"] != profile["aircraft_id"] or policy["synthetic"] != observe["synthetic"]:
            raise ValueError("Policy aircraft/provenance does not match the observation profile.")
        if any(str(connection.get(key)) != str(observe.get(key)) for key in ("system_id", "component_id", "expected_uid")):
            raise ValueError("Both endpoint profiles must explicitly select the same UID/system/component.")
        shared = connection.get("transport") == "shared_recorder"
        if shared:
            if (observe["listen_port"] != connection["local_port"] or observe["local_address"] != connection["local_address"]
                    or observe["expected_sender"] != connection["remote_address"]
                    or observe.get("readback", {}).get("remote_port") != connection["remote_port"]
                    or _client_identity(observe.get("readback", {})) != _client_identity(connection)):
                raise ValueError("Shared readback policy must match the single configured app connection.")
        elif observe["listen_port"] == connection["local_port"] and observe["local_address"] == connection["local_address"]:
            raise ValueError("Acquisition and passive recording require separate local ports.")
        if policy["log_retrieval"] != "disabled" and approval.get("confirm_log_transfer_side_effects") is not True:
            raise ValueError("This prepare must acknowledge possible onboard logging suspension.")
        return saved

    def on_prepared(self, capture, approval):
        """Trusted controller callback after an explicitly approved prepare only."""
        saved = self.validate_prepare(capture["profile"], approval)
        if saved is None:
            return None
        with self._lock:
            try:
                previous = self._get("run", capture["uuid"])
            except ValueError:
                previous = None
            if previous:
                if previous["policy_id"] != saved["id"]:
                    raise ValueError("Capture is already bound to another acquisition policy.")
                return previous  # Retrying prepare never rearms an old runtime.
            if not capture.get("recorder_started") or capture["status"] != "observing":
                raise ValueError("A running recorder is required before policy activation.")
            run = self._save("run", {"id": capture["uuid"], "capture_session_id": capture["uuid"],
                "policy_id": saved["id"], "policy_fingerprint": saved["policy_fingerprint"],
                "state": "watching", "approval": approval, "stop_approved": False,
                "owner": {"pid": os.getpid(), "created_at": psutil.Process().create_time()},
                "gate": {"allowed": False, "reason": "Waiting for fresh selected disarmed identity evidence."},
                "adapter": ADAPTER, "historical_applicability": "unknown", "limitations": LIMITATIONS})
            self._active[run["id"]] = {"stop_approved": False, "revoked": False}
            return run

    def validate_prepare_operation(self, profile, approval, operation_id):
        """Bind the reviewed opt-in before recorder start, including absent opt-in."""
        saved = self.validate_prepare(profile, approval)
        operation_id = _text(operation_id, "operation_id")
        digest = _hash({"method": "prepare_approval", "profile": profile, "approval": approval})
        with self._lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload_hash FROM orchestration_operations WHERE id=?", (operation_id,)).fetchone()
            if old and old[0] != digest:
                raise ValueError("Prepare operation_id is bound to a different policy approval/profile.")
            if not old:
                db.execute("INSERT INTO orchestration_operations VALUES(?,?,?)", (operation_id, digest,
                    _json({"kind": "prepare_approval", "policy_id": saved["id"] if saved else None})))
        return saved

    def on_stopped(self, capture_id):
        """Called only for an explicit operator stop, not EOF or process loss."""
        with self._lock:
            if capture_id not in self._active:
                return
            self._active[capture_id]["stop_approved"] = True
            run = self._get("run", capture_id)
            self._save("run", run | {"stop_approved": True, "state": "awaiting_selected_log"})
            self._cancel_requests(capture_id, parameters_only=True)

    def _context(self, capture_id):
        collection = self.controller.collection
        with collection._lock, self.store._connect() as db:
            capture = collection._get(db, "capture", capture_id)
            if len(capture["armed_interval_ids"]) > 1000:
                raise ValueError("Capture interval limit reached; stop and begin a new observation session.")
            intervals = [collection._get(db, "interval", identity) for identity in capture["armed_interval_ids"]]
            observations = {}
            for kind in ("parameter", "identity", "link_gap"):
                row = db.execute("SELECT rowid,data FROM collection_events WHERE capture_id=? AND kind=? ORDER BY rowid DESC LIMIT 1", (capture_id, kind)).fetchone()
                observations[kind] = {"sequence": row[0], "event": json.loads(row[1])} if row else None
        recorder = self.controller.recorder.status()
        safety = collection.safety_state(capture_id)
        states = capture.get("source_states", {})
        source = states.get(capture.get("selected_source_id"), {})
        return {"capture": capture, "intervals": intervals, "observations": observations,
                "recorder": recorder, "safety": safety, "source": source}

    @staticmethod
    def _token(context):
        capture, source = context["capture"], context["source"]
        heartbeat = source.get("heartbeat_evidence", {})
        identity = context["observations"]["identity"]
        boot_epoch = heartbeat.get("boot_epoch")
        if boot_epoch is None and capture["profile"]["connection"].get("readback"):
            selected = [item for item in context["recorder"].get("sources", [])
                        if item.get("source_id") == capture.get("selected_source_id")]
            if len(selected) == 1 and selected[0].get("boot_epoch") is not None:
                boot_epoch = str(selected[0]["boot_epoch"])
        return {"source_id": capture.get("selected_source_id"), "uid": source.get("uid"),
                "latest_armed_interval_id": context["intervals"][-1]["uuid"] if context["intervals"] else None,
                "boot_epoch": boot_epoch, "gap_count": capture.get("coverage", {}).get("gap_count", 0),
                "firmware_identity": _hash(identity["event"].get("firmware_identity")) if identity else None,
                "parameter_observation": context["observations"]["parameter"]["sequence"] if context["observations"]["parameter"] else None,
                "gap_observation": context["observations"]["link_gap"]["sequence"] if context["observations"]["link_gap"] else None}

    @staticmethod
    def _preflight_anchor(context):
        return "after:" + context["intervals"][-1]["uuid"] if context["intervals"] else "watch_start"

    def _preflight_eligible(self, request, context, policy):
        age = (time.monotonic_ns() - int(request["finished_monotonic_ns"])) / 1e9
        return (request["anchor"] == self._preflight_anchor(context)
                and self._gate(context, policy)["allowed"]
                and request.get("completion_token") == self._token(context)
                and request.get("completeness") == "complete_for_scope"
                and request.get("consistency") == "no_observed_conflict"
                and 0 <= age <= policy["preflight_freshness_s"]
                and not request.get("revoked") and request.get("freshness") != "invalidated")

    @staticmethod
    def _bootstrap_token(request, token, policy):
        """Permit the one initially missing identity to become the declared UID.

        Shared readback must be able to request AUTOPILOT_VERSION before the GCS
        has broadcast it. Once observed, this identity is pinned like every other
        acquisition; a mismatch or a later firmware change still revokes it.
        """
        original = request.get("trigger_token", {})
        if (policy["connection"].get("transport") == "shared_recorder"
                and original.get("uid") is None and original.get("firmware_identity") is None
                and token.get("uid")
                and (not policy["connection"].get("expected_uid") or token["uid"] == policy["connection"]["expected_uid"])):
            return original | {"uid": token["uid"], "firmware_identity": token.get("firmware_identity")}
        return original

    def _preflight_status(self, run):
        """Describe the current arm cycle without starting or reviving requests."""
        policy = self.store.get_document("acquisition_policy", run["policy_id"])["policy"]
        context = self._context(run["id"])
        requests, _ = self._rows("request", run["id"], limit=256)
        anchor = self._preflight_anchor(context)
        current = next((item for item in requests if item["role"] == "preflight_parameters" and item["anchor"] == anchor), None)
        result = {"state": "waiting", "current_for_next_flight": False, "anchor": anchor,
                  "request_id": current["id"] if current else None,
                  "decoded_count": current.get("decoded_count") if current else None,
                  "expected_count": current.get("expected_count") if current else None,
                  "completeness": current.get("completeness", "unknown") if current else "unknown",
                  "historical_applicability": "unknown"}
        if not run["runtime_authorized"]:
            result.update(state="suspended", reason="This policy is no longer authorized. Review and approve a new Prepare & watch to request current parameters.")
        elif not policy["preflight_parameters"]:
            result.update(state="not_enabled", reason="Preflight readback is disabled. Select a policy with preflight readback for the next Prepare & watch.")
        elif not self._gate(context, policy)["allowed"]:
            result.update(state="waiting_for_live_evidence", reason=self._gate(context, policy)["reason"] + " Readback waits; passive recording continues.")
        elif current is None:
            result["reason"] = "A separate current readback will be requested for this arm cycle after any postflight readback finishes. No earlier flight snapshot is reused."
        elif current["state"] == "complete" and self._preflight_eligible(current, context, policy):
            result.update(state="complete", current_for_next_flight=True,
                          reason="All parameters reported for this scope were read within the freshness budget. This non-atomic readback does not prove the settings effective throughout a flight.")
        elif current["state"] == "complete":
            reason = ("This readback is no longer current. Automatic refresh waits for fresh disarmed evidence and a 30-second minimum cooldown; the capture request budget still applies."
                      if policy.get("automatic_preflight_refresh") else
                      "This readback is no longer current. While disarmed, stop this watch and approve a new Prepare & watch for a fresh readback. Earlier evidence is retained.")
            result.update(state="stale", reason=reason)
        elif current["state"] in TERMINAL:
            result.update(state=current["state"], reason="Current parameters are incomplete or unavailable. Resolve the request error below, then approve a new Prepare & watch while disarmed; no failed request is silently retried.")
        else:
            result.update(state=current["state"], reason=current.get("reason") or "Reading the current settings through the app connection. Keep the aircraft disarmed.")
        return result

    @staticmethod
    def _gate(context, policy):
        capture, recorder, source = context["capture"], context["recorder"], context["source"]
        if (capture["status"] != "observing" or not recorder.get("active") or recorder.get("state") != "recording"
                or recorder.get("capture_session_id") != capture["uuid"] or recorder.get("callback_error")
                or recorder.get("event_backlog_bytes", 0) > 256 * 1024):
            return {"allowed": False, "reason": "Recorder health/ownership is not current."}
        if (capture.get("selected_source_id") and not source.get("heartbeat_evidence")
                and not capture.get("source_ambiguity") and not capture.get("active_armed_interval_id")
                and not source.get("identity_conflict") and not source.get("continuity_lost")
                and (not policy["connection"].get("expected_uid") or source.get("uid") in (None, policy["connection"]["expected_uid"]))):
            from .recorder import shared_bootstrap_ready
            if shared_bootstrap_ready(recorder, policy["connection"]):
                return {"allowed": True, "reason": "Fresh disarmed shared socket permits initial UID discovery; parameter requests still require independently verified UID and boot clock."}
        if (context["safety"].get("armed_state") != "disarmed" or capture.get("source_ambiguity")
                or source.get("identity_conflict") or source.get("continuity_lost")
                or (policy["connection"].get("expected_uid") and str(source.get("uid")) != policy["connection"]["expected_uid"]
                    and not (policy["connection"].get("transport") == "shared_recorder" and source.get("uid") is None))
                or source.get("heartbeat_evidence", {}).get("boot_epoch") in (None, "unknown")):
            return {"allowed": False, "reason": "Fresh disarmed source/UID/clock-segment evidence is unavailable."}
        return {"allowed": True, "reason": "Observer is freshly disarmed; direct acquisition independently checks UID and boot clock."}

    def _request(self, run, role, anchor, flight_record_id=None, refresh_attempt=0):
        key = {"run": run["id"], "role": role, "anchor": anchor}
        if refresh_attempt:
            key["refresh_attempt"] = refresh_attempt
        identity = _hash(key)
        try:
            return self._get("request", identity)
        except ValueError:
            rows, total = self._rows("request", run["id"], limit=1)
            policy = self.store.get_document("acquisition_policy", run["policy_id"])["policy"]
            if total >= policy["max_requests_per_capture"]:
                raise ValueError("Approved request limit reached; no further automatic requests will be started.")
            return self._save("request", {"id": identity, "capture_session_id": run["id"],
                "flight_record_id": flight_record_id, "role": role, "anchor": anchor, "state": "waiting",
                "refresh_attempt": refresh_attempt,
                "operation_id": str(uuid4()), "job_id": None, "acquisition_id": None,
                "association_state": "provisional", "historical_applicability": "unknown",
                "freshness": "unknown", "applicability": "unknown"})

    def _cancel_requests(self, capture_id, parameters_only=False):
        rows, _ = self._rows("request", capture_id, limit=256)
        for request in rows:
            if request["state"] in ("queued", "running") and (not parameters_only or "parameters" in request["role"]):
                if request.get("job_id"):
                    self.jobs.dispatch("jobs.cancel", {"job_id": request["job_id"]})
                self._save("request", request | {"revoked": True, "freshness": "invalidated", "applicability": "unknown"})

    def _start(self, run, request, policy, context, method, extra):
        if request["state"] != "waiting":
            return request
        if self.acquisition.external_gate:
            kind = "parameters" if method == "acquisition.parameters.capture" else "log_list" if method == "acquisition.logs.list" else "log_download"
            admission = self.acquisition.external_gate(kind, policy["connection"])
            if not isinstance(admission, dict) or admission.get("allowed") is not True:
                return self._save("request", request | {"reason": str((admission or {}).get("reason", "Application acquisition gate is not ready."))[:1000]})
        request = self._save("request", request | {"state": "queued", "trigger_token": self._token(context),
            "started_monotonic_ns": str(time.monotonic_ns()), "revoked": False})
        params = {"connection": policy["connection"], "limits": policy["limits"],
                  "aircraft_id": policy["aircraft_id"], "synthetic": policy["synthetic"],
                  "operation_id": request["operation_id"], "applicability_fingerprint": _hash(policy["connection"]), **extra}
        try:
            job = self.jobs.start(method, params, lambda m, p, **kwargs: self._execute(request["id"], m, p, **kwargs))["job"]
        except ValueError as error:
            return self._save("request", request | {"state": "failed", "reason": str(error)})
        latest = self._get("request", request["id"])
        return self._save("request", latest | {"job_id": job["id"]})

    def _execute(self, request_id, method, params, cancel_check=None, progress=None):
        with self._lock:
            request = self._get("request", request_id)
            self._save("request", request | {"state": "running"})

        def cancelled():
            if self._closed.is_set() or (cancel_check and cancel_check()):
                return True
            with self._lock:
                runtime = self._active.get(request["capture_session_id"])
                current = self._get("request", request_id)
                return runtime is None or runtime["revoked"] or current.get("revoked", False)

        result = None
        try:
            if cancelled():
                raise CancelledError()
            result = self.acquisition.dispatch(method, params, cancel_check=cancelled, progress=progress)
            return {"request_id": request_id, **result}
        finally:
            with self._lock:
                latest = self._get("request", request_id)
                acquisition = result.get("acquisition") if result else None
                if acquisition is None:
                    with self.store._connect() as db:
                        row = db.execute("SELECT data FROM documents WHERE kind='acquisition' AND json_extract(data,'$.operation_id')=? ORDER BY created_at DESC LIMIT 1", (request["operation_id"],)).fetchone()
                        acquisition = json.loads(row[0]) if row else None
                changed = {"state": acquisition["state"] if acquisition else "interrupted",
                           "finished_monotonic_ns": str(time.monotonic_ns())}
                if acquisition:
                    parameter_capture = acquisition.get("parameter_capture", {})
                    changed.update(acquisition_id=acquisition["id"], source_refs=acquisition.get("source_refs", []),
                                   completeness=parameter_capture.get("completeness"),
                                   consistency=parameter_capture.get("consistency"),
                                   decoded_count=parameter_capture.get("decoded_count"),
                                   expected_count=parameter_capture.get("expected_count"),
                                   atomic=False, recovery_warning=acquisition.get("transport_evidence", {}).get("recovery_warning"),
                                   reason=acquisition.get("interruption_reason") or acquisition.get("archive_error"))
                    if "parameters" in latest["role"]:
                        if params["connection"].get("transport") == "shared_recorder":
                            # The recorder journal and catalog reader are separate.
                            # Capture our own reply events before freezing the token
                            # so they do not trigger a spurious refresh immediately.
                            synchronize = getattr(self.controller.recorder, "synchronize_events", None)
                            if synchronize is None or not synchronize():
                                changed.update(freshness="invalidated", applicability="unknown", revoked=True,
                                               reason="Recorder event processing has not caught up with this readback; current settings remain unconfirmed.")
                        context = self._context(request["capture_session_id"])
                        token = self._token(context)
                        run = self._get("run", request["capture_session_id"])
                        policy = self.store.get_document("acquisition_policy", run["policy_id"])["policy"]
                        trigger = self._bootstrap_token(latest, token, policy)
                        changed["trigger_token"] = trigger
                        if any(trigger.get(key) != token[key] for key in CONTINUITY_KEYS):
                            changed.update(revoked=True, freshness="invalidated", applicability="unknown",
                                           reason="Observed arm cycle or source continuity changed during this readback; retained evidence is not a current preflight snapshot.")
                        if latest["role"] == "preflight_parameters":
                            changed["completion_token"] = token
                self._save("request", latest | changed)

    def _finish_stopped_run(self, run, listing=None):
        """Release approval after one-shot work ends, never while a worker owns it."""
        if listing is not None and listing["state"] not in TERMINAL:
            return
        requests, _ = self._rows("request", run["id"], limit=256)
        selection_unavailable = None
        if listing is not None:
            if listing["state"] != "complete":
                selection_unavailable = "Approved listing did not complete; no selectable result exists. No automatic retry."
            elif not self.store.get_document("acquisition", listing["acquisition_id"]).get("entries"):
                selection_unavailable = "Approved listing returned no log entries; no selectable result exists. No automatic retry."
        if selection_unavailable:
            for request in requests:
                if request["role"] == "onboard_log" and request["state"] == "waiting":
                    self._save("request", request | {"state": "interrupted", "reason": selection_unavailable,
                        "revoked": True, "freshness": "invalidated", "applicability": "unknown"})
            requests, _ = self._rows("request", run["id"], limit=256)
        if any(row["role"] in ("onboard_log", "log_listing") and row["state"] not in TERMINAL for row in requests):
            return
        for request in requests:
            if request["state"] in ("queued", "running"):
                return
            if request.get("job_id"):
                # The request result is durable before JobService publishes its
                # terminal outcome. Keep approval owned until that worker ends.
                job = self.jobs.dispatch("jobs.get", {"job_id": request["job_id"]})["job"]
                if job["state"] not in JOB_TERMINAL:
                    return
        for request in requests:
            if request["state"] == "waiting":
                self._save("request", request | {"state": "cancelled", "revoked": True,
                    "reason": "Explicit observation stop ended this unstarted request; no replay.",
                    "freshness": "invalidated", "applicability": "unknown"})
        self._save("run", run | {"state": "completed",
            "reason": "Explicit observation stop; approved attempts ended. Individual evidence outcomes are retained."})
        self._active.pop(run["id"])

    def tick(self, capture_id):
        """Trusted timer hook; deterministic tests may call without a monitor thread."""
        with self._lock:
            if capture_id not in self._active or self._active[capture_id]["revoked"]:
                return
            run = self._get("run", capture_id)
            policy = self.store.get_document("acquisition_policy", run["policy_id"])["policy"]
            context = self._context(capture_id)
            gate = self._gate(context, policy)
            run = self._save("run", run | {"gate": gate})
            requests, _ = self._rows("request", capture_id, limit=256)
            for request in requests:
                if "parameters" in request["role"] and request["state"] in ("queued", "running"):
                    token = self._token(context)
                    trigger = self._bootstrap_token(request, token, policy)
                    if trigger != request.get("trigger_token"):
                        request = self._save("request", request | {"trigger_token": trigger})
                    if any(trigger.get(key) != token[key]
                           for key in CONTINUITY_KEYS):
                        self._save("request", request | {"revoked": True, "freshness": "invalidated", "applicability": "unknown"})
                        if request.get("job_id"):
                            self.jobs.dispatch("jobs.cancel", {"job_id": request["job_id"]})
                if request.get("job_id") and request["state"] not in TERMINAL:
                    job = self.jobs.dispatch("jobs.get", {"job_id": request["job_id"]})["job"]
                    if job["state"] in ("failed", "cancelled", "interrupted"):
                        self._save("request", request | {"state": job["state"], "reason": job.get("error", {}).get("message")})
                if request["role"] == "preflight_parameters" and request["state"] == "complete":
                    eligible = self._preflight_eligible(request, context, policy)
                    self._save("request", request | {"freshness": "within_declared_budget" if eligible else "invalidated",
                        "applicability": "provisional_review_only" if eligible else "unknown",
                        "freshness_budget_s": policy["preflight_freshness_s"]})
            if not gate["allowed"]:
                self._cancel_requests(capture_id, parameters_only=True)
            capture, intervals = context["capture"], context["intervals"]
            closed = [interval for interval in intervals if interval.get("end_status") == "observed_transition" and interval.get("disarm_evidence")]
            if policy["log_retrieval"] != "disabled":
                for interval in closed:
                    self._request(run, "onboard_log", interval["uuid"], interval["flight_record_id"])
            if self._active[capture_id]["stop_approved"]:
                if (policy["log_retrieval"] != "disabled" and closed and not context["recorder"].get("active")
                        and capture["status"] in ("stopped", "finalizing")):
                    request = self._request(run, "log_listing", capture_id)
                    request = self._start(run, request, policy, context, "acquisition.logs.list", {"confirm_log_transfer_side_effects": True})
                    self._finish_stopped_run(run, request)
                elif policy["log_retrieval"] == "disabled" or not closed:
                    self._finish_stopped_run(run)
                return
            if not gate["allowed"]:
                return
            pending, _ = self._rows("request", capture_id, limit=256)
            if any(row["state"] in ("queued", "running") for row in pending):
                return
            if policy["postflight_parameters"]:
                for interval in closed:
                    # Do not fetch an older flight's postflight configuration
                    # after another arm; the settings may already have changed.
                    if interval["uuid"] != intervals[-1]["uuid"]:
                        continue
                    request = self._request(run, "postflight_parameters", interval["uuid"], interval["flight_record_id"])
                    if request["state"] == "waiting":
                        self._start(run, request, policy, context, "acquisition.parameters.capture", {"role": "postflight"})
                        return
            if policy["preflight_parameters"]:
                anchor = self._preflight_anchor(context)
                candidates = [row for row in pending if row["role"] == "preflight_parameters" and row["anchor"] == anchor]
                request = candidates[0] if candidates else self._request(run, "preflight_parameters", anchor)
                if (policy.get("automatic_preflight_refresh") and request["state"] == "complete"
                        and not self._preflight_eligible(request, context, policy)
                        and (time.monotonic_ns() - int(request["finished_monotonic_ns"])) / 1e9 >= 30):
                    if len(pending) >= policy["max_requests_per_capture"]:
                        self._save("run", run | {"reason": "Automatic readback request budget reached; existing evidence is retained."})
                        return
                    request = self._request(run, "preflight_parameters", anchor,
                                            refresh_attempt=request.get("refresh_attempt", 0) + 1)
                self._start(run, request, policy, context, "acquisition.parameters.capture", {"role": "preflight"})

    def select_retrieval(self, params):
        if params.get("confirm_selected_log_closed") is not True:
            raise ValueError("Explicitly confirm the selected log is closed; this remains an operator declaration.")
        operation = _text(params.get("operation_id"), "operation_id")
        digest = _hash(params)
        with self._lock:
            with self.store._connect() as db:
                old = db.execute("SELECT payload_hash,response FROM orchestration_operations WHERE id=?", (operation,)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError("operation_id belongs to a different retrieval selection.")
                return json.loads(old[1]) | {"operation_replayed": True}
            request = self._get("request", params.get("request_id"))
            if request["role"] != "onboard_log" or request["state"] != "waiting":
                raise ValueError("Choose a waiting per-flight onboard-log request.")
            run = self._get("run", request["capture_session_id"])
            runtime = self._active.get(run["id"])
            if not runtime or runtime["revoked"] or not runtime["stop_approved"]:
                raise ValueError("This capture has no active explicit stop/transfer authorization; no restart replay is permitted.")
            context = self._context(run["id"])
            if context["recorder"].get("active"):
                raise ValueError("Stop the observer before selecting a legacy onboard transfer.")
            listings, _ = self._rows("request", run["id"], limit=256)
            if not any(row["role"] == "log_listing" and row["state"] == "complete" and row.get("acquisition_id") == params.get("listing_id") for row in listings):
                raise ValueError("Choose a completed listing produced by this approved capture policy.")
            listing = self.store.get_document("acquisition", params["listing_id"])
            log_id = _integer(params.get("log_id"), "log_id", 0, 65535)
            if not any(row["log_id"] == log_id for row in listing.get("entries", [])):
                raise ValueError("Select an actual returned log ID.")
            policy = self.store.get_document("acquisition_policy", run["policy_id"])["policy"]
            request = self._save("request", request | {"selected_listing_id": listing["id"], "selected_log_id": log_id,
                "closure_basis": "operator_declared_not_protocol_verified", "selected_at": self.store._now()})
            request = self._start(run, request, policy, context, "acquisition.logs.download", {
                "listing_id": listing["id"], "log_id": log_id, "confirm_log_transfer_side_effects": True})
            result = {"request": request, "operation_replayed": False}
            with self.store._connect() as db:
                db.execute("INSERT INTO orchestration_operations VALUES(?,?,?)", (operation, digest, _json(result)))
            return result

    def dispatch(self, method, params):
        if method == "orchestration.preview":
            return self.preview(params)
        if method == "orchestration.policy.save":
            return self.save_policy(params)
        if method == "orchestration.retrieval.select":
            return self.select_retrieval(params)
        limit = _integer(params.get("limit", 50), "limit", 1, 100)
        offset = _integer(params.get("offset", 0), "offset", 0, 1_000_000)
        if method == "orchestration.policy.list":
            # Read a bounded lookahead in one snapshot without exceeding the
            # public Store.list_documents maximum page size of 100.
            with self.store._connect() as db:
                rows = db.execute("SELECT data FROM documents WHERE kind='acquisition_policy' "
                    "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            policies = [json.loads(row[0]) for row in rows]
            return {"policies": policies[:limit], "next_offset": offset + limit if len(policies) > limit else None}
        if method == "orchestration.status":
            capture_id = params.get("capture_session_id")
            requests, total = self._rows("request", capture_id, limit, offset)
            runs, run_total = self._rows("run", capture_id, limit, offset)
            for run in runs:
                run.update(runtime_authorized=run["id"] in self._active and not self._active[run["id"]]["revoked"],
                           restart_replay_allowed=False,
                           authorization_state="runtime_approved" if run["id"] in self._active and not self._active[run["id"]]["revoked"] else "suspended_requires_new_prepare")
                run["preflight_readiness"] = self._preflight_status(run)
            return {"runs": runs, "requests": requests, "request_total": total, "run_total": run_total,
                    "next_offset": offset + limit if max(total, run_total) > offset + limit else None,
                    "adapter": ADAPTER, "limitations": LIMITATIONS, "recovery_warnings": self.recovery_warnings}
        raise ValueError("Unsupported orchestration method.")

    def _monitor(self):
        while not self._closed.wait(self._poll_interval):
            for identity in list(self._active):
                try:
                    self.tick(identity)
                except Exception as error:
                    logging.exception("Acquisition policy monitor withheld further requests")
                    with self._lock:
                        self._active[identity]["revoked"] = True
                        self._cancel_requests(identity)
                        run = self._get("run", identity)
                        self._save("run", run | {"state": "revoked", "reason": str(error)[:1000]})

    def close(self):
        self._closed.set()
        if self._thread:
            self._thread.join(2)
        with self._lock:
            for identity in self._active:
                self._active[identity]["revoked"] = True
                self._cancel_requests(identity)
                run = self._get("run", identity)
                self._save("run", run | {"state": "suspended", "reason": "Runtime ended; explicit new prepare required. No replay."})
