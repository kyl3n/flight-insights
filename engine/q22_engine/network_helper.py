"""Unprivileged client for an explicitly installed native bench helper.

No elevation, installation, executable selection, raw commands or safety booleans
are accepted from renderer RPC. The socket/pipe transport authenticates the OS
peer; helper plan IDs and fingerprints are retained in engine memory.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import socket
import stat
import struct
import threading
from typing import Any

SOCKET_PATH = Path("/run/q22-network-helper/control.sock")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class LinuxTransport:
    """Root-owned fixed socket + SO_PEERCRED; no PATH or app-data authority."""
    peer_authenticated = False

    def request(self, method: str, params: dict) -> Any:
        for path in (SOCKET_PATH, *SOCKET_PATH.parents):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
                raise OSError("Native helper socket and ancestors must be root-owned.")
            if path == SOCKET_PATH:
                if not stat.S_ISSOCK(info.st_mode):
                    raise OSError("Native helper endpoint is not a Unix socket.")
            elif not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                raise OSError("Native helper directory is writable by an untrusted principal.")
        frame = json.dumps({"method": method, "params": params}, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(frame) > 65536:
            raise ValueError("Native helper request exceeds the frame limit.")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(3 if method in {"capabilities", "recording_lease"} else 60)
            stream.connect(str(SOCKET_PATH))
            pid, uid, gid = struct.unpack("3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0 or pid <= 0:
                raise OSError("Native helper OS peer is not the administrator service.")
            self.peer_authenticated = True
            stream.sendall(frame)
            reply = bytearray()
            while b"\n" not in reply:
                chunk = stream.recv(min(65536, 1048577 - len(reply)))
                if not chunk:
                    raise OSError("Native helper closed an incomplete response.")
                reply.extend(chunk)
                if len(reply) > 1048576:
                    raise OSError("Native helper response exceeds the frame limit.")
            line, remainder = bytes(reply).split(b"\n", 1)
            if remainder:
                raise OSError("Native helper returned extra response data.")
            value = json.loads(line)
            if not isinstance(value, dict) or set(value) not in ({"result"}, {"error"}):
                raise OSError("Malformed native helper response.")
            if "error" in value:
                raise OSError(str(value["error"])[:1000])
            return value["result"]


def _transport(system: str):
    if system == "Linux":
        return LinuxTransport()
    if system == "Windows":
        try:
            from .network_helper_windows import WindowsTransport
            return WindowsTransport()
        except ImportError:
            return None
    return None


class NativeRepairBackend:
    """Constructor-owned transport; qualification cannot be supplied by RPC."""
    native_authority = True

    def __init__(self, *, transport=None, system: str | None = None):
        self.system = system or platform.system()
        self.transport = transport if transport is not None else _transport(self.system)
        self.bench_opt_in = False
        self.prepared: dict[str, dict] = {}
        self.safety_state = None
        self._lease_thread = None
        self._lease_stop = threading.Event()
        self._lease_lock = threading.RLock()
        self._recording_reserved = False

    def capabilities(self) -> dict:
        fallback = {"platform": self.system, "state": "diagnosis_only", "automatic_apply": False,
                    "bench_available": False, "bench_opt_in": self.bench_opt_in, "qualified": False,
                    "signed_helper": False, "admin_enrolled": False, "manager_aware": False,
                    "supervised_rollback": False, "reason": "No authenticated, administrator-enrolled native helper is available."}
        try:
            if self.transport is None:
                return fallback
            value = self.transport.request("capabilities", {})
            available = (isinstance(value, dict) and value.get("protocol_version") == 1
                         and value.get("platform") == self.system and value.get("state") == "bench_enrolled"
                         and all(value.get(k) is True for k in ("admin_enrolled", "manager_aware", "supervised_rollback"))
                         and self.transport.peer_authenticated is True)
            if not available:
                return fallback
            return {**value, "state": "experimental_enabled" if self.bench_opt_in else "experimental_available",
                    "bench_available": True, "bench_opt_in": self.bench_opt_in, "qualified": False,
                    "automatic_apply": self.bench_opt_in, "peer_authenticated": True,
                    "reason": "Experimental administrator-enrolled helper. Native hardware acceptance and public signing remain unqualified; session opt-in and exact plan consent are required."}
        except (OSError, ValueError, TypeError, AttributeError):
            return fallback

    def opt_in(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("Experimental opt-in must be a boolean.")
        if enabled and not self.capabilities()["bench_available"]:
            raise ValueError("Install and explicitly enroll the private bench helper before opting in.")
        self.bench_opt_in = enabled
        if enabled:
            self._renew_lease()
            if self._lease_thread is None:
                self._lease_thread = threading.Thread(target=self._leases, name="native-repair-recording-lease", daemon=True)
                self._lease_thread.start()
        # Existing temporary repair supervision stays active after opt-out;
        # lease renewal continues until engine exit so it can observe recording.
        return self.capabilities()

    def _renew_lease(self):
        with self._lease_lock:
            active = True
            if callable(self.safety_state):
                value = self.safety_state()
                active = not isinstance(value, dict) or value.get("recording_active") is not False
            active = active or self._recording_reserved
            if self.transport is None:
                raise OSError("Native helper unavailable.")
            result = self.transport.request("recording_lease", {"active": active})
            if not isinstance(result, dict) or result.get("state") != "renewed":
                raise OSError("Native helper did not acknowledge the recording interlock.")

    def before_recording(self):
        # Once opted in, a disappeared helper cannot silently permit a worker
        # to race an already dispatched native repair.
        if not self.bench_opt_in and self._lease_thread is None:
            return
        with self._lease_lock:
            self._recording_reserved = True
            try:
                self._renew_lease()
            except (OSError, ValueError) as exc:
                raise OSError("Recording did not start because the native repair helper could not acknowledge its interlock. Restore the helper or explicitly end the experimental session after reviewing unresolved repairs: " + str(exc)) from exc

    def after_recording(self):
        if not self.bench_opt_in and self._lease_thread is None:
            return
        with self._lease_lock:
            self._recording_reserved = False
            try:
                self._renew_lease()
            except (OSError, ValueError):
                # A successful recorder start must not be reported as failed
                # because renewal lost its reply. Native expiry stays blocked;
                # the next background renewal observes authoritative state.
                pass

    def _leases(self):
        while not self._lease_stop.wait(0.8):
            try:
                self._renew_lease()
            except Exception:
                pass  # Native lease expiry fails closed; never invent inactive.

    def close(self):
        self._lease_stop.set()
        if self._lease_thread is not None:
            self._lease_thread.join(timeout=0.1)

    @staticmethod
    def candidate(descriptor: dict) -> dict:
        if not isinstance(descriptor, dict) or set(descriptor) != {"platform", "kind", "source", "interface", "index", "local_address", "prefix", "zone"}:
            raise ValueError("A server-generated typed network descriptor is required.")
        source = descriptor["source"]
        if not isinstance(source, dict) or set(source) != {"id", "kind", "host", "port", "path"}:
            raise ValueError("Invalid typed network source.")
        remote = str(ipaddress.IPv4Address(source["host"]))
        local = str(ipaddress.IPv4Address(descriptor["local_address"]))
        kind = descriptor["kind"]
        if kind == "host_route":
            operation = {"kind": kind, "target": remote, "local_address": local}
        elif kind == "address_alias":
            if descriptor["prefix"] != 32:
                raise ValueError("Native bench aliases require a separately reviewed /32 operation.")
            operation = {"kind": kind, "address": local, "prefix": 32}
        elif kind == "firewall_udp" and source["kind"] == "udp":
            port = source["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid native UDP port.")
            operation = {"kind": kind, "remote_address": remote, "local_address": local, "port": port}
        else:
            raise ValueError("Unsupported native repair operation.")
        index = descriptor["index"]
        if index is not None and (type(index) is not int or index < 1):
            raise ValueError("Invalid native adapter index.")
        interface = descriptor["interface"]
        if not isinstance(interface, str) or not interface or len(interface) > 256 or any(ord(c) < 32 for c in interface):
            raise ValueError("Invalid native interface name.")
        return {"interface": interface, "interface_index": index, "operation": operation}

    def prepare(self, app_plan_id: str, descriptor: dict) -> dict:
        if not self.capabilities()["automatic_apply"]:
            raise ValueError("Explicit session opt-in to an authenticated native helper is required.")
        candidate = self.candidate(descriptor)
        self._renew_lease()
        plan = self.transport.request("prepare", {"candidate": candidate, "expires_in_s": 120, "rollback_after_s": 30})
        self._validate_plan(plan)
        if plan["candidate"] != candidate:
            raise OSError("Native helper reviewed a different operation.")
        self.prepared[app_plan_id] = copy.deepcopy(plan)
        return {k: copy.deepcopy(plan[k]) for k in ("plan_id", "review_fingerprint", "candidate", "before_fingerprint", "expires_utc_ms", "rollback_after_ms")} | {"authority": "admin_enrolled_helper"}

    @staticmethod
    def _validate_plan(plan):
        if (not isinstance(plan, dict) or not _ID.fullmatch(str(plan.get("plan_id", "")))
                or not _DIGEST.fullmatch(str(plan.get("review_fingerprint", "")))
                or not _DIGEST.fullmatch(str(plan.get("before_fingerprint", "")))
                or type(plan.get("expires_utc_ms")) is not int or type(plan.get("rollback_after_ms")) is not int
                or not 10_000 <= plan["rollback_after_ms"] <= 300_000 or not isinstance(plan.get("candidate"), dict)):
            raise OSError("Invalid native reviewed plan.")

    def execute(self, action: str, app_plan_id: str, descriptor: dict | None = None) -> dict:
        if action not in {"apply", "rollback"} or not self.capabilities()["automatic_apply"]:
            raise ValueError("Native execution requires current experimental opt-in.")
        plan = self.prepared.get(app_plan_id)
        if plan is None:
            raise ValueError("This app plan has no helper-owned reviewed identity.")
        if descriptor is not None and self.candidate(descriptor) != plan["candidate"]:
            raise ValueError("Reviewed native candidate changed.")
        self._renew_lease()
        receipt = self.transport.request(action, {"plan_id": plan["plan_id"], "review_fingerprint": plan["review_fingerprint"], "confirm": True})
        if not isinstance(receipt, dict) or receipt.get("plan") != plan:
            raise OSError("Native receipt does not match the reviewed plan.")
        return copy.deepcopy(receipt)

    def recovery(self) -> list[dict]:
        if not self.capabilities()["bench_available"]:
            return []
        records = self.transport.request("recovery", {})
        if not isinstance(records, list) or len(records) > 64:
            raise OSError("Invalid bounded native recovery list.")
        for entry in records:
            if not isinstance(entry, dict) or set(entry) != {"record", "ownership"} or entry["ownership"] not in {"owned", "absent", "changed", "unknown"}:
                raise OSError("Invalid native receipt.")
            record = entry["record"]
            if not isinstance(record, dict):
                raise OSError("Invalid native receipt record.")
            self._validate_plan(record.get("plan"))
        return copy.deepcopy(records)
