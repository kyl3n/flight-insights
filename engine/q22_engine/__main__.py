"""Bounded, versioned stdio JSON-RPC. Protocol output goes only to stdout."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import sys
import time

from . import __version__
from .catalog import CatalogService
from .collection_controller import CollectionController
from .jobs import JobService
from .insight_service import InsightReports
from .network import NetworkService
from .network_helper import NativeRepairBackend
from .parameters import ParameterService
from .store import Store

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
METHODS = (
    "handshake", "engine.status", "sources.capabilities", "network.inspect", "environment.inspect",
    "catalog.status", "catalog.verify",
    "network.probe", "network.plan", "network.apply", "network.rollback", "network.recovery",
    "network.profile", "network.capabilities", "network.bench_opt_in",
    "session.preview", "session.prepare_and_watch", "session.status", "session.detail", "session.stop",
    "associations.evaluate", "associations.review",
    "parameters.import", "parameters.list", "parameters.diff", "parameters.promote_readback", "sessions.import",
    "sessions.list", "comparison.run", "logs.import", "logs.list", "logs.detail", "logs.session",
    "insights.report", "insights.list", "insights.detail", "insights.compare", "insights.review_packet", "insights.export_review",
    "experiments.create", "experiments.list", "experiments.detail", "experiments.evaluate",
    "research.status", "research.query", "research.refresh",
    "acquisition.preview", "acquisition.parameters.capture", "acquisition.logs.list", "acquisition.logs.download",
    "acquisition.list", "acquisition.detail",
    "orchestration.preview", "orchestration.policy.save", "orchestration.policy.list",
    "orchestration.status", "orchestration.retrieval.select",
    "reconciliation.scan_folder", "reconciliation.index_source", "reconciliation.get", "reconciliation.list",
    "alignment.reference", "alignment.analyze", "alignment.get", "alignment.list", "alignment.attach",
    "radio.capture", "radio.list", "radio.detail",
    "jobs.get", "jobs.list", "jobs.cancel",
)
LIMITATIONS = [
    "Managed-analysis preview for Windows and Linux; the complete handoff acceptance plan has not passed. macOS is deferred.",
    "No real Q22 aircraft, DigiQ SkyPulse or SIYI A8 Mini bench qualification has been performed.",
    "Log metrics use only validated source/epoch/unit mappings; incomplete parameter timelines cannot establish historical settings.",
    "Approved collection policies, bounded content alignment and local RATE/IMU/static-filter analysis are available; complete historical configuration, dynamic firmware parity and aircraft-specific numerical tuning remain unqualified.",
    "Legacy onboard log transfer can suspend logging; verify resumption before another flight. A complete readback is not an atomic flight-time configuration.",
    "Host repairs default to diagnosis only. An administrator-enrolled experimental Windows/Linux helper requires explicit session opt-in, per-plan approval and independent fresh safety evidence; hardware qualification remains open.",
    "A network response is not proof of video decoding, bidirectional delivery, or aircraft health.",
]


class Engine:
    def __init__(self, data_dir: Path):
        self.store = Store(data_dir)
        self.catalog = CatalogService(self.store)
        self.parameters = ParameterService(self.store)
        self.jobs = JobService(data_dir)
        self.collection = CollectionController(self.store, self.jobs, recording_guard=self._recording_guard)
        self.network = NetworkService(data_dir=data_dir, safety_state=self.collection.safety_state,
                                      repair_backend=NativeRepairBackend())
        self.logs = None
        self.insights = InsightReports(self.store)
        self.experiments = None
        self.research = None
        self.acquisition = None
        self.orchestration = None
        self.reconciliation = None
        self.alignment = None
        self.radio = None
        self.started = time.monotonic()

    @contextmanager
    def _recording_guard(self):
        try:
            self.network.before_recording()
            yield
        finally:
            self.network.after_recording()

    def close(self):
        self.network.close()
        if self.orchestration is not None:
            self.orchestration.close()
        self.collection.close()
        self.collection.collection.environment.close()
        self.jobs.close()

    def call(self, method: str, params: dict) -> dict:
        if method == "handshake":
            self.collection.collection.environment.note_client(params)
            return {"protocol_version": 1, "engine_version": __version__,
                    "product": "Q22 Flight Lab", "milestone": "managed-analysis-preview",
                    "capabilities": list(METHODS), "limitations": LIMITATIONS}
        if method == "environment.inspect":
            return self.collection.collection.environment.inspect(params.get("environment_context"))
        if method == "engine.status":
            return {"state": "available", "protocol_version": 1,
                    "engine_version": __version__, "uptime_s": round(time.monotonic() - self.started, 2),
                    "recording": self.collection.recorder.status(), "limitations": LIMITATIONS}
        if method == "sources.capabilities":
            return {"sources": [
                {"kind": "parameters", "state": "available", "formats": ["Mission Planner", "QGroundControl"]},
                {"kind": "performance_csv", "state": "available", "schema": "q22.performance.v1"},
                {"kind": "network", "state": "available", "limits": "Configured sources only; explicit stream probes."},
                {"kind": "dataflash", "state": "available", "formats": ["BIN", "LOG"], "limits": "Bounded offline import; fields without validated units remain unavailable."},
                {"kind": "tlog", "state": "available", "limits": "Recorded MAVLink only; source and boot epochs remain separate."},
                {"kind": "live_recording", "state": "available", "limits": "Explicit separate passive UDP feed; read-only requests use a separate reviewed bidirectional endpoint. UDP replay protection remains unavailable."},
                {"kind": "insights", "state": "available", "limits": "Scoped measurements, versioned guidance and explicit evidence gaps; no automatic tuning or aircraft writes."},
                {"kind": "flight_environment", "state": "available", "limits": "Immutable capture/flight OS and allowlisted software observations; supported native metadata only, with pending or unavailable versions kept explicit."},
                {"kind": "flight_configuration", "state": "available", "limits": "Preserved typed readbacks linked through revalidated flight alignment; complete enumeration does not establish atomic effective in-flight configuration."},
                {"kind": "flight_experiments", "state": "available", "limits": "Immutable plans, repeated-flight and paired comparisons, goals, regressions and counterexamples; no causal or statistical-independence claim."},
                {"kind": "research_library", "state": "available", "limits": "Offline official excerpts and explicit optional source updates; downloaded material remains unreviewed and cannot replace tuning rules."},
                {"kind": "read_only_acquisition", "state": "experimental", "limits": "Explicit direct bidirectional endpoint, fresh disarmed identity/clock gates; real GCS/hardware qualification pending."},
                {"kind": "collection_policies", "state": "experimental", "limits": "Explicit per-capture approval; gated pre/postflight readbacks and selected post-stop log transfers. No startup/restart replay or automatic aircraft commands."},
                {"kind": "evidence_alignment", "state": "available", "limits": "Bounded imported catalog, identity/clock/armed-interval corroboration and reviewed attachment. Verified rule matches do not establish cryptographic identity or complete historical settings."},
                {"kind": "catalog_integrity", "state": "available", "limits": "Versioned catalog migrations and explicit original-file verification; catalog backup does not include every evidence file."},
                {"kind": "folder_reconciliation", "state": "available", "limits": "Explicit selected folder, content inspection and provisional candidates; no filename-based verified associations."},
                {"kind": "skypulse_normalization", "state": "unvalidated", "recovery": "Supply radio firmware and actual raw responses."},
                {"kind": "skypulse_raw_history", "state": "available", "limits": "Explicit bounded capture of documented HTTP endpoints; original responses and receipt times, no normalized RF metrics."},
            ]}
        if method.startswith("network."):
            return self.network.dispatch(method, params)
        if method.startswith("catalog."):
            if method == "catalog.verify":
                return self.jobs.start(method, params, self.catalog.dispatch)
            return self.catalog.dispatch(method, params)
        if method.startswith("radio."):
            if self.radio is None:
                from .radio_history import RadioHistoryService
                self.radio = RadioHistoryService(self.store)
            if method == "radio.capture":
                return self.jobs.start(method, params, self.radio.dispatch)
            return self.radio.dispatch(method, params)
        if method.startswith("jobs."):
            return self.jobs.dispatch(method, params)
        if method.startswith("orchestration."):
            return self._orchestrator().dispatch(method, params)
        if method == "session.prepare_and_watch":
            orchestrator = self._orchestrator()
            profile = self.collection.collection.preview(params)["profile"]
            approval = params.get("acquisition_policy")
            orchestrator.validate_prepare_operation(profile, approval, params.get("operation_id"))
            result = self.collection.dispatch(method, params)
            capture = result["capture_session"]
            recorder = result.get("recorder", {})
            if (not result.get("operation_replayed") and recorder.get("active")
                    and recorder.get("capture_session_id") == capture["uuid"]
                    and capture.get("recorder_started")):
                try:
                    result["orchestration_run"] = orchestrator.on_prepared(capture, approval)
                except Exception:
                    # Readback-policy failure must not stop independently useful
                    # passive recording or obscure that recording did start.
                    logging.exception("Acquisition policy activation failed after recorder start")
                    result["acquisition_policy_warning"] = "Recording started, but the acquisition policy could not be activated. Review collection status and retained evidence."
            return result
        if method == "session.stop":
            result = self.collection.dispatch(method, params)
            if self.orchestration is not None and not result.get("operation_replayed"):
                self.orchestration.on_stopped(result["capture_session"]["uuid"])
            return result
        if method.startswith(("session.", "associations.")):
            return self.collection.dispatch(method, params)
        if method.startswith("insights."):
            if method in {"insights.report", "insights.compare"}:
                return self.jobs.start(method, params, self.insights.dispatch)
            return self.insights.dispatch(method, params)
        if method.startswith("experiments."):
            if self.experiments is None:
                from .experiments import ExperimentService
                self.experiments = ExperimentService(self.store)
            if method == "experiments.evaluate":
                return self.jobs.start(method, params, self.experiments.dispatch)
            return self.experiments.dispatch(method, params)
        if method.startswith("research."):
            if self.research is None:
                from .research import ResearchService
                self.research = ResearchService(self.store)
            if method == "research.refresh":
                if self.collection.recorder.status().get("active"):
                    raise ValueError("Stop recording before updating reference sources. Cached research remains available during recording.")
                return self.jobs.start(method, params, self._research_job)
            return self.research.dispatch(method, params)
        if method.startswith("acquisition."):
            self._acquirer()
            if method in {"acquisition.parameters.capture", "acquisition.logs.list", "acquisition.logs.download"}:
                return self.jobs.start(method, params, self.acquisition.dispatch)
            return self.acquisition.dispatch(method, params)
        if method.startswith("reconciliation."):
            if self.reconciliation is None:
                from .reconciliation import ReconciliationService
                self.reconciliation = ReconciliationService(self.store, collection=self.collection.collection)
            if method in {"reconciliation.scan_folder", "reconciliation.index_source"}:
                return self.jobs.start(method, params, self.reconciliation.dispatch)
            return self.reconciliation.dispatch(method, params)
        if method.startswith("alignment."):
            if self.alignment is None:
                from .alignment import AlignmentService
                self.alignment = AlignmentService(self.store, collection=self.collection.collection)
            if method in {"alignment.reference", "alignment.analyze", "alignment.attach"}:
                return self.jobs.start(method, params, self.alignment.dispatch)
            return self.alignment.dispatch(method, params)
        if method.startswith("logs."):
            if self.logs is None:
                from .logs import LogService
                self.logs = LogService(self.store)
            if method in ("logs.import", "logs.session"):
                return self.jobs.start(method, params, self.logs.dispatch)
            return self.logs.dispatch(method, params)
        if method == "parameters.promote_readback":
            return self.jobs.start(method, params, self.parameters.dispatch)
        return self.parameters.dispatch(method, params)

    def _research_job(self, method, params, cancel_check=None, progress=None):
        # A queued or running internet update must yield the shared worker when
        # recording begins, so it cannot hold up a preflight readback policy.
        def cancelled():
            return bool((cancel_check and cancel_check()) or self.collection.recorder.status().get("active"))

        return self.research.dispatch(method, params, cancel_check=cancelled, progress=progress)

    def _acquirer(self):
        if self.acquisition is None:
            from .acquisition import AcquisitionService
            self.acquisition = AcquisitionService(self.store, external_gate=self._acquisition_gate)
        return self.acquisition

    def _orchestrator(self):
        if self.orchestration is None:
            from .orchestration import OrchestrationService
            self.orchestration = OrchestrationService(self.store, self.jobs, self._acquirer(), self.collection)
        return self.orchestration

    def _acquisition_gate(self, kind, connection):
        """Additional app-owned capture guard; acquisition verifies its own wire peer."""
        current = self.collection.recorder.status()
        # Recovered workers retain their ownership even when a second app cannot
        # observe their current aircraft state. Do not use them as proof of safety.
        if any(item.get("active") and item.get("capture_session_id") != current.get("capture_session_id")
               for item in self.collection.recorder.recovered()):
            return {"allowed": False, "reason": "Another recorder owns this catalog; stop it in its owning app first."}
        if not current.get("active"):
            return {"allowed": True, "reason": "The acquisition must independently establish fresh disarmed identity and clock evidence."}
        if kind != "parameters":
            return {"allowed": False, "reason": "Stop the app observer before legacy onboard log transfer; it can suspend onboard logging."}
        for binding in current.get("bindings", []):
            if (binding.get("port") == connection.get("local_port") and
                    (binding.get("local_address") in {"0.0.0.0", connection.get("local_address")}
                     or connection.get("local_address") == "0.0.0.0")):
                return {"allowed": False, "reason": "Readback requires a separate endpoint from the passive recorder."}
        selected = [source for source in current.get("sources", [])
                    if source.get("system_id") == connection.get("system_id")
                    and source.get("component_id") == connection.get("component_id")]
        safety = self.collection.collection.safety_state(current.get("capture_session_id"))
        if (current.get("state") != "recording" or current.get("callback_error")
                or current.get("event_backlog_bytes", 0) > 256 * 1024
                or safety.get("armed_state") != "disarmed" or len(selected) != 1
                or selected[0].get("armed_state") != "disarmed"
                or str(selected[0].get("uid")) != str(connection.get("expected_uid"))):
            return {"allowed": False, "reason": "The active observer lacks a fresh, unambiguous disarmed heartbeat and matching UID."}
        return {"allowed": True, "reason": "Separate readback endpoint; observer matches declared UID and is freshly disarmed. UDP identity is not authenticated."}

    def handle(self, value: object) -> dict:
        request_id = value.get("id") if isinstance(value, dict) else None
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return error(None, -32600, "Expected JSON-RPC 2.0 request with a string or integer id")
        method, params = value.get("method"), value.get("params", {})
        if not isinstance(method, str) or method not in METHODS:
            return error(request_id, -32601, "Unsupported method")
        if not isinstance(params, dict):
            return error(request_id, -32602, "params must be an object")
        try:
            result = self.call(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, KeyError, TypeError, FileNotFoundError) as exc:
            return error(request_id, -32602, str(exc))
        except PermissionError:
            return error(request_id, -32001, "Access denied. Review file or operating system permissions.")
        except Exception:
            logging.exception("Engine operation failed: %s", method)
            return error(request_id, -32603, "Operation failed; inspect the engine diagnostic log.")


def error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> None:
    # PyInstaller workers re-enter this executable; never start a second RPC
    # engine or accept a renderer-selected program for the passive recorder.
    if sys.argv[1:2] == ["--recorder-worker"]:
        from .recorder import worker_main
        worker_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="Q22 Flight Lab stdio engine")
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    engine = Engine(args.data_dir)
    try:
        while line := sys.stdin.buffer.readline(MAX_REQUEST + 1):
            if len(line) > MAX_REQUEST:
                # Consume one rejected frame without allocating it in memory.
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
                response = error(None, -32600, "Request exceeds 1 MiB limit")
            else:
                try:
                    value = json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
                    response = engine.handle(value)
                except (ValueError, UnicodeDecodeError):
                    response = error(None, -32700, "Invalid JSON")
            try:
                output = json.dumps(response, allow_nan=False, separators=(",", ":")).encode("utf-8")
                if len(output) > MAX_RESPONSE:
                    output = json.dumps(error(response.get("id"), -32002, "Result exceeds 4 MiB; narrow the query")).encode()
                sys.stdout.buffer.write(output + b"\n")
                sys.stdout.buffer.flush()
            except BrokenPipeError:
                return
    finally:
        engine.close()


if __name__ == "__main__":
    main()
