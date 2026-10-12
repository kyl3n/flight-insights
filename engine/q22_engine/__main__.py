"""Bounded, versioned stdio JSON-RPC. Protocol output goes only to stdout."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

from . import __version__
from .collection_controller import CollectionController
from .jobs import JobService
from .network import NetworkService
from .parameters import ParameterService
from .store import Store

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
METHODS = (
    "handshake", "engine.status", "sources.capabilities", "network.inspect",
    "network.probe", "network.plan", "network.apply", "network.rollback", "network.recovery",
    "network.profile", "network.capabilities",
    "session.preview", "session.prepare_and_watch", "session.status", "session.detail", "session.stop",
    "associations.evaluate", "associations.review",
    "parameters.import", "parameters.list", "parameters.diff", "sessions.import",
    "sessions.list", "comparison.run", "logs.import", "logs.list", "logs.detail", "logs.session",
    "jobs.get", "jobs.list", "jobs.cancel",
)
LIMITATIONS = [
    "Foundation preview; the complete handoff acceptance plan has not passed.",
    "No real-aircraft or Windows/macOS hardware validation has been performed.",
    "Log metrics use only validated source/epoch/unit mappings; incomplete parameter timelines cannot establish historical settings.",
    "Passive UDP capture is available; complete readbacks, automatic BIN retrieval, DSP and tuning recommendations are not implemented.",
    "Automatic host repairs require a qualified native helper and are diagnosis-only in this release.",
    "A network response is not proof of video decoding, bidirectional delivery, or aircraft health.",
]


class Engine:
    def __init__(self, data_dir: Path):
        self.store = Store(data_dir)
        self.parameters = ParameterService(self.store)
        self.jobs = JobService(data_dir)
        self.collection = CollectionController(self.store, self.jobs)
        self.network = NetworkService(data_dir=data_dir, safety_state=self.collection.safety_state)
        self.logs = None
        self.started = time.monotonic()

    def close(self):
        self.collection.close()
        self.jobs.close()

    def call(self, method: str, params: dict) -> dict:
        if method == "handshake":
            return {"protocol_version": 1, "engine_version": __version__,
                    "product": "Q22 Flight Lab", "milestone": "passive-collection-preview",
                    "capabilities": list(METHODS), "limitations": LIMITATIONS}
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
                {"kind": "live_recording", "state": "available", "limits": "Explicit separate passive UDP feed; no full readback, automatic BIN retrieval or authenticated replay protection."},
                {"kind": "skypulse_normalization", "state": "unvalidated", "recovery": "Supply radio firmware and actual raw responses."},
            ]}
        if method.startswith("network."):
            return self.network.dispatch(method, params)
        if method.startswith("jobs."):
            return self.jobs.dispatch(method, params)
        if method.startswith(("session.", "associations.")):
            return self.collection.dispatch(method, params)
        if method.startswith("logs."):
            if self.logs is None:
                from .logs import LogService
                self.logs = LogService(self.store)
            if method in ("logs.import", "logs.session"):
                return self.jobs.start(method, params, self.logs.dispatch)
            return self.logs.dispatch(method, params)
        return self.parameters.dispatch(method, params)

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
