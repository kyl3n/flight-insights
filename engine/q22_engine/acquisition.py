"""Explicit, bounded MAVLink readback over a dedicated or recorder-owned socket.

Only generated dialect code is used. This is not an autopilot controller and does
not provide a GCS heartbeat. Receipt/UID checks do not authenticate unsigned UDP.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import select
import struct
import threading
import tempfile
import time
from uuid import uuid4

import psutil

from .parameter_capture import ParameterAccumulator, MAX_PARAMETERS
from .store import Store

VERSION = "q22-readonly-mavlink-acquisition/1"
MAGIC = b"Q22ACQ1\0"
RECORD = struct.Struct(">BQQ4sHI")  # direction, receipt/send clocks, endpoint, payload length
WINDOW = 65520  # multiple of LOG_DATA's 90 byte payload
ALLOWED_MESSAGES = frozenset({20, 21, 76, 117, 119, 122})
APP_SYSTEM_ID = 254
APP_COMPONENT_ID = 191
LIMITATIONS = [
    "Experimental direct UDP adapter; real Q22 hardware and firmware behavior are unqualified.",
    "CRC, source endpoint and matching reported UID do not authenticate the vehicle or universally prevent replay; signatures are not verified.",
    "MAVLink parameter reads have no request transaction ID, are not atomic, and do not establish historical in-flight applicability.",
    "Log IDs, timestamps and sizes are listing observations, not filenames or flight identities. Downloaded artifacts remain provisional candidates.",
    "LOG_REQUEST operations can suspend onboard logging on some firmware. Verify logging has resumed before the next flight; request cleanup is not proof of resumption.",
    "A local SHA-256 identifies received bytes; the transfer protocol supplies no authenticated remote checksum or proof the onboard logger closed the file.",
]
DEFAULT_LIMITS = {"timeout_s": 120.0, "bootstrap_timeout_s": 10.0, "heartbeat_timeout_s": 3.0,
                  "boot_timeout_s": 5.0, "identity_timeout_s": 15.0, "retry_wait_s": .5,
                  "request_interval_s": .05, "settle_s": .3, "retries": 3,
                  "max_log_bytes": 128 * 1024 * 1024, "max_evidence_bytes": 256 * 1024 * 1024}


def _write_metadata(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".attempt-", delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + .5
        while True:
            try:
                os.replace(temporary, path)
                temporary = None
                break
            except OSError as error:
                if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class AcquisitionStopped(ValueError):
    """Evidence remains useful but the live gate/transfer no longer permits work."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}.")
    return value


def _text(value, name, size=160):
    if not isinstance(value, str) or not value.strip() or len(value) > size:
        raise ValueError(f"{name} must be a nonempty string of at most {size} characters.")
    return value


def _connection(value):
    if not isinstance(value, dict) or value.get("feed_origin") != "direct_bidirectional":
        raise ValueError("Use an explicitly configured direct_bidirectional endpoint, not a one-way forwarded recorder feed.")
    result = {"feed_origin": "direct_bidirectional"}
    transport = value.get("transport")
    if transport is not None:
        if transport not in {"shared_recorder", "dedicated_udp"}:
            raise ValueError("transport must be shared_recorder or dedicated_udp.")
        result["transport"] = transport
    for name in ("local_address", "remote_address"):
        try:
            address = ipaddress.IPv4Address(value.get(name))
        except (ValueError, TypeError):
            raise ValueError(f"{name} must be numeric IPv4.") from None
        if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
            raise ValueError("Select explicit unicast local and remote IPv4 addresses.")
        result[name] = str(address)
    for name in ("local_port", "remote_port"):
        result[name] = _integer(value.get(name), name, 1, 65535)
    for name in ("system_id", "component_id"):
        result[name] = _integer(value.get(name), name, 1, 255)
    # Keep omitted defaults omitted in saved fingerprints, including historical
    # policies. Effective client IDs are separately recorded in each attempt.
    for name in ("client_system_id", "client_component_id"):
        if value.get(name) is not None:
            result[name] = _integer(value[name], name, 1, 255)
    if _client_identity(result)[0] == result["system_id"]:
        raise ValueError("App MAVLink system ID must differ from the selected vehicle system ID; change the advanced app identity.")
    uid = value.get("expected_uid")
    if uid is None and transport == "shared_recorder":
        # Discover on each new collection; never write the observed UID back
        # into the saved connection or silently reuse a previous aircraft.
        return result
    if not isinstance(uid, str) or not uid.isascii() or not uid.isdecimal() or len(uid) > 20 or not 0 < int(uid) < 2**64:
        raise ValueError("expected_uid must be an explicitly declared nonzero uint64 decimal string.")
    result["expected_uid"] = str(int(uid))
    return result


def _client_identity(connection):
    return (connection.get("client_system_id", APP_SYSTEM_ID),
            connection.get("client_component_id", APP_COMPONENT_ID))


def _same_connection(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    defaults = {"client_system_id": APP_SYSTEM_ID, "client_component_id": APP_COMPONENT_ID}
    return (defaults | left) == (defaults | right)


def _validate_outgoing(message, connection, bootstrap=False, *, check_source=False):
    """The adapter and the recorder's socket owner enforce the same allowlist."""
    message_id = message.get_msgId()
    if check_source and (message.get_srcSystem(), message.get_srcComponent()) != _client_identity(connection):
        raise ValueError("Outgoing request source differs from the configured app MAVLink identity.")
    if message_id not in ALLOWED_MESSAGES:
        raise ValueError("Outgoing MAVLink message is outside the read-only allowlist.")
    if message.target_system != connection["system_id"] or message.target_component != connection["component_id"]:
        raise ValueError("Outgoing readback target differs from the selected source.")
    if message_id == 76:
        if message.command != 512 or message.confirmation != 0 or message.param1 != 148 or any(getattr(message, f"param{i}") != 0 for i in range(2, 8)):
            raise ValueError("Only hardcoded AUTOPILOT_VERSION message requests are permitted.")
    elif bootstrap:
        raise ValueError("Only the version bootstrap may omit matching UID evidence.")


def _limits(value):
    if value is None:
        return dict(DEFAULT_LIMITS)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS):
        raise ValueError("Unsupported acquisition limits.")
    result = DEFAULT_LIMITS | value
    bounds = {"timeout_s": (.2, 3600), "bootstrap_timeout_s": (.1, 30), "heartbeat_timeout_s": (.05, 5),
              "boot_timeout_s": (.05, 10), "identity_timeout_s": (.2, 60), "retry_wait_s": (.03, 5),
              "request_interval_s": (.005, 2), "settle_s": (.03, 2)}
    for name, (low, high) in bounds.items():
        number = result[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{name} must be finite in {low}..{high}.")
    _integer(result["retries"], "retries", 1, 5)
    _integer(result["max_log_bytes"], "max_log_bytes", 1, 512 * 1024 * 1024)
    _integer(result["max_evidence_bytes"], "max_evidence_bytes", 1024, 2 * 1024**3)
    return result


class _Link:
    def __init__(self, directory, connection, limits, kind, cancel_check, external_gate, shared_transport=None):
        from pymavlink.dialects.v20 import ardupilotmega as mavlink
        mavlink.MAVLINK_IGNORE_CRC = False
        self.mavlink, self.connection, self.limits = mavlink, connection, limits
        self.kind, self.cancel_check, self.external_gate = kind, cancel_check, external_gate
        self.remote = (connection["remote_address"], connection["remote_port"])
        self.started = time.monotonic()
        self.deadline = self.started + limits["timeout_s"]
        self.socket = None
        self.journal_path = directory / "exchange.mavudp"
        self.journal = self.journal_path.open("wb")
        self.journal.write(MAGIC)
        self.trace_bytes = len(MAGIC)
        self.trace_hash = hashlib.sha256(MAGIC)
        self.decoder = mavlink.MAVLink(None)
        self.decoder.robust_parsing = True
        self.client_system_id, self.client_component_id = _client_identity(connection)
        self.encoder = mavlink.MAVLink(None, srcSystem=self.client_system_id, srcComponent=self.client_component_id)
        self.heartbeat = self.identity = self.boot = None
        self.capabilities = 0
        self.firmware_identity = None
        self.last_boot_by_field = {}
        self.signed = None
        self.last_send = self.last_version_request = 0.0
        self.sent, self.received, self.frames = 0, 0, 0
        self.gate_reason = None
        self.log_transfer_started = False
        self.log_request_end_sent = False
        self.version_requests = 0
        self.send_types = {}
        self.pending = []
        self.late_data_validator = None
        self.last_external_check = 0.0
        self.initial_gate_observations = None
        self.shared_transport = shared_transport
        self.transport_kind = "dedicated_udp"

    def open(self):
        self.check_external()
        connection = self.connection
        if connection.get("transport") == "shared_recorder":
            if self.shared_transport is None:
                raise ValueError("Shared recorder access is unavailable in this acquisition service.")
            self.socket = self.shared_transport(connection, self.limits)
            if self.socket is not None:
                self.transport_kind = "shared_recorder"
                return
            if not connection.get("expected_uid"):
                raise AcquisitionStopped("Automatic aircraft identification requires an active collection. Start collecting, or use an explicit UID lock for a separate post-stop acquisition.")
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.socket.bind((connection["local_address"], connection["local_port"]))
        self.socket.settimeout(.05)

    def check_external(self, force=False):
        if self.cancel_check and self.cancel_check():
            raise CancelledError("Readback cancelled; received evidence retained.")
        if time.monotonic() > self.deadline:
            raise AcquisitionStopped("Acquisition time budget expired.")
        if self.external_gate and (force or time.monotonic() - self.last_external_check >= .1):
            self.last_external_check = time.monotonic()
            result = self.external_gate(self.kind, self.connection)
            if not isinstance(result, dict) or result.get("allowed") is not True:
                raise AcquisitionStopped(str((result or {}).get("reason", "External live safety gate is unknown or blocked.")))

    def gate(self, *, bootstrap=False):
        self.check_external()
        if self.gate_reason:
            raise AcquisitionStopped(self.gate_reason)
        now = time.monotonic()
        if self.heartbeat is None or now - self.heartbeat["at"] > self.limits["heartbeat_timeout_s"]:
            raise AcquisitionStopped("Fresh disarmed autopilot heartbeat is unavailable.")
        if self.heartbeat["armed"]:
            raise AcquisitionStopped("Autopilot is armed; readback stopped without sending further requests.")
        if bootstrap:
            return
        if self.identity is None or now - self.identity["at"] > self.limits["identity_timeout_s"]:
            raise AcquisitionStopped("Fresh matching vehicle UID evidence is unavailable.")
        if self.boot is None or now - self.boot["at"] > self.limits["boot_timeout_s"]:
            raise AcquisitionStopped("Fresh boot-clock evidence is unavailable; stream SYSTEM_TIME or another time_boot_ms message.")

    def _record(self, direction, payload, endpoint, mono_ns, utc_us):
        header = RECORD.pack(direction, mono_ns, utc_us, socket.inet_aton(endpoint[0]), endpoint[1], len(payload))
        if self.trace_bytes + len(header) + len(payload) > self.limits["max_evidence_bytes"]:
            raise AcquisitionStopped("Acquisition transcript byte limit reached.")
        offset = self.trace_bytes
        self.journal.write(header)
        self.journal.write(payload)
        self.trace_hash.update(header)
        self.trace_hash.update(payload)
        self.trace_bytes += len(header) + len(payload)
        return {"file": self.journal_path.name, "offset": offset, "length": len(header) + len(payload)}

    def send(self, message, *, bootstrap=False):
        # Drain already-received evidence before deciding to send. In particular,
        # an armed packet queued behind a parameter reply must revoke the gate.
        self.check_external(force=True)
        drained = 0
        self.socket.settimeout(0)
        try:
            while (self.socket.has_pending() if self.transport_kind == "shared_recorder"
                   else bool(select.select([self.socket], [], [], 0)[0])):
                self.pending.extend(self.poll(require_ready=False, refresh_identity=False, drain=True))
                drained += 1
                if drained >= 256 or len(self.pending) > 4096:
                    raise AcquisitionStopped("Incoming evidence backlog prevents a current safe send decision.")
        finally:
            self.socket.settimeout(.05)
        self.gate(bootstrap=bootstrap)
        message_id = message.get_msgId()
        _validate_outgoing(message, self.connection, bootstrap)
        if self.sent >= 50000:
            raise AcquisitionStopped("Acquisition request budget reached.")
        encoded = message.pack(self.encoder)
        self.encoder.seq = (self.encoder.seq + 1) % 256
        mono, utc = time.monotonic_ns(), time.time_ns() // 1000
        self._record(1, encoded, self.remote, mono, utc)
        self.socket.sendto(encoded, self.remote)
        self.sent += 1
        self.send_types[message.get_type()] = self.send_types.get(message.get_type(), 0) + 1
        self.last_send = time.monotonic()
        if message_id in (117, 119):
            self.log_transfer_started = True
        elif message_id == 122:
            self.log_request_end_sent = True

    def request_version(self):
        self.gate(bootstrap=True)
        if time.monotonic() - self.last_version_request < max(.1, self.limits["identity_timeout_s"] / 3):
            return
        message = self.mavlink.MAVLink_command_long_message(self.connection["system_id"], self.connection["component_id"], 512, 0,
                                                          148, 0, 0, 0, 0, 0, 0)
        self.send(message, bootstrap=True)
        self.last_version_request = time.monotonic()
        self.version_requests += 1

    def poll(self, *, require_ready=True, refresh_identity=True, drain=False):
        self.check_external()
        if self.gate_reason:
            raise AcquisitionStopped(self.gate_reason)
        if self.heartbeat is not None and self.heartbeat["armed"]:
            self.gate(bootstrap=True)
        if refresh_identity and self.heartbeat and (self.identity is None or time.monotonic() - self.identity["at"] > self.limits["identity_timeout_s"] / 2):
            self.request_version()
        if require_ready:
            self.gate()
        if self.pending and not drain:
            pending, self.pending = self.pending, []
            if self.late_data_validator:
                for message, _ in pending:
                    if message.get_type() == "LOG_DATA":
                        self.late_data_validator(message)
            return pending
        try:
            payload, endpoint = self.socket.recvfrom(65535)
        except (socket.timeout, BlockingIOError):
            return []
        mono_ns, utc_us = (self.socket.receipt if self.transport_kind == "shared_recorder"
                           else (time.monotonic_ns(), time.time_ns() // 1000))
        at = mono_ns / 1e9
        raw_ref = self._record(0, payload, endpoint, mono_ns, utc_us)
        self.received += 1
        if endpoint[0] != self.remote[0]:
            return []
        if endpoint != self.remote:
            self.gate_reason = "Competing UDP endpoint on the selected remote address; source identity is ambiguous."
            raise AcquisitionStopped(self.gate_reason)
        try:
            messages = self.decoder.parse_buffer(payload) or []
        except Exception:
            self.gate_reason = "Invalid MAVLink framing/CRC from the selected endpoint; continuity is unknown."
            raise AcquisitionStopped(self.gate_reason) from None
        accepted = []
        for message in messages:
            name = message.get_type()
            if name == "BAD_DATA":
                self.gate_reason = "Corrupt or unsupported MAVLink bytes from the selected endpoint; continuity is unknown."
                raise AcquisitionStopped(self.gate_reason)
            if (message.get_srcSystem(), message.get_srcComponent()) == (self.client_system_id, self.client_component_id):
                self.gate_reason = "Incoming traffic uses the app MAVLink identity; another sender or routed echo conflicts with this connection. Change the advanced app identity or router configuration."
                raise AcquisitionStopped(self.gate_reason)
            if (message.get_srcSystem(), message.get_srcComponent()) != (self.connection["system_id"], self.connection["component_id"]):
                continue
            self.frames += 1
            frame = bytes(message.get_msgbuf())
            signed = frame[0] == 0xFD and bool(frame[2] & 1)
            if self.signed is not None and self.signed != signed:
                self.gate_reason = "Selected source changed signed/unsigned framing; identity continuity is unknown."
                raise AcquisitionStopped(self.gate_reason)
            self.signed = signed
            raw = message.to_dict()
            receipt = {"received_monotonic_ns": str(mono_ns), "received_utc_us": str(utc_us),
                       "raw_ref": raw_ref, "message_type": name}
            if name == "LOG_DATA" and self.late_data_validator:
                self.late_data_validator(message)
            if name == "HEARTBEAT":
                if raw["autopilot"] == 8 or raw["type"] == 6:
                    self.gate_reason = "Selected source is not an identified autopilot heartbeat."
                    raise AcquisitionStopped(self.gate_reason)
                self.heartbeat = {"at": at, "armed": bool(raw["base_mode"] & 128), "autopilot": raw["autopilot"], **receipt}
                if self.heartbeat["armed"]:
                    self.gate_reason = "Autopilot became armed; requests stopped."
                    raise AcquisitionStopped(self.gate_reason)
            elif name == "AUTOPILOT_VERSION":
                uid = str(raw.get("uid", 0))
                expected_uid = self.connection.get("expected_uid") or (self.identity or {}).get("uid")
                if uid == "0" and expected_uid is None:
                    continue  # No stable identity: retain telemetry, never request parameters.
                if expected_uid is not None and uid != expected_uid:
                    self.gate_reason = "Observed vehicle UID contradicts the explicitly selected identity."
                    raise AcquisitionStopped(self.gate_reason)
                firmware = {key: raw.get(key) for key in ("flight_sw_version", "board_version", "vendor_id", "product_id")}
                if self.firmware_identity is not None and firmware != self.firmware_identity:
                    self.gate_reason = "Firmware identity changed during acquisition."
                    raise AcquisitionStopped(self.gate_reason)
                if self.identity is not None and self.capabilities != int(raw.get("capabilities", 0)):
                    self.gate_reason = "Parameter encoding capability evidence changed during acquisition."
                    raise AcquisitionStopped(self.gate_reason)
                self.firmware_identity = firmware
                self.identity = {"at": at, "uid": uid, "firmware_identity": firmware, **receipt}
                self.capabilities = int(raw.get("capabilities", 0))
            if type(raw.get("time_boot_ms")) is int:
                value = raw["time_boot_ms"]
                # Different message streams may be delayed differently; compare
                # the same message clock, and reject large cross-message reversals.
                previous = self.last_boot_by_field.get(name)
                if (previous is not None and value < previous) or (self.boot is not None and value + 1000 < self.boot["time_boot_ms"]):
                    self.gate_reason = "Boot clock regressed; restart/wrap/reordering is unclassified and acquisition generation ended."
                    raise AcquisitionStopped(self.gate_reason)
                self.last_boot_by_field[name] = value
                self.boot = {"at": at, "time_boot_ms": value, **receipt}
            accepted.append((message, {"raw_ref": raw_ref, "received_monotonic_ns": str(mono_ns), "received_utc_us": str(utc_us)}))
        if require_ready:
            self.gate()
        return accepted

    def ready(self):
        deadline = min(self.deadline, time.monotonic() + self.limits["bootstrap_timeout_s"])
        while time.monotonic() < deadline:
            self.poll(require_ready=False)
            try:
                self.gate()
            except AcquisitionStopped:
                if self.gate_reason:
                    raise
            else:
                self.initial_gate_observations = self.gate_observations()
                return
        raise AcquisitionStopped("Aircraft identification timed out: require a reported nonzero AUTOPILOT_VERSION UID, fresh disarmed heartbeat and boot-clock stream. Recording and imported-log analysis remain available without a UID.")

    def cleanup(self):
        if self.log_transfer_started and not self.log_request_end_sent:
            try:
                self.send(self.mavlink.MAVLink_log_request_end_message(self.connection["system_id"], self.connection["component_id"]))
            except (AcquisitionStopped, CancelledError, OSError):
                pass
        if self.socket:
            self.socket.close()
        try:
            self.journal.flush()
            os.fsync(self.journal.fileno())
        finally:
            self.journal.close()

    def gate_observations(self):
        return {"heartbeat": {k: v for k, v in self.heartbeat.items() if k != "at"} if self.heartbeat else None,
                "identity": {k: v for k, v in self.identity.items() if k != "at"} if self.identity else None,
                "boot_clock": {k: v for k, v in self.boot.items() if k != "at"} if self.boot else None,
                "authenticity": "unverified", "utc_mapping": "ground_receipt_only"}

    def evidence(self):
        return {"received_datagrams": self.received, "decoded_selected_frames": self.frames,
                "transport": self.transport_kind,
                "client_identity": {"system_id": self.client_system_id, "component_id": self.client_component_id},
                "receive_clock": "recorder_original_receipt" if self.transport_kind == "shared_recorder" else "socket_receipt",
                "outgoing_clock": "request_submission" if self.transport_kind == "shared_recorder" else "socket_submission",
                "sent_messages": self.sent, "outgoing_types": self.send_types,
                "signature_present": self.signed, "signature_verified": False,
                "identity_basis": ("reported_uid_match_not_authenticated" if self.connection.get("expected_uid")
                                   else "reported_uid_discovered_not_authenticated"),
                "boot_epoch": "single_observed_acquisition_clock_segment",
                "initial_gate_observations": self.initial_gate_observations,
                "final_gate_observations": self.gate_observations(),
                "gate_interruption_reason": self.gate_reason,
                "log_request_end_sent": self.log_request_end_sent,
                "logging_resumption_verified": False,
                "recovery_warning": "Remote transfer/logging state is unknown; verify onboard logging before flight." if self.log_transfer_started else None,
                "trace_sha256": self.trace_hash.hexdigest(), "trace_bytes": self.trace_bytes}


class AcquisitionService:
    def __init__(self, store: Store, external_gate=None, shared_transport=None):
        self.store, self.external_gate = store, external_gate
        self.shared_transport = shared_transport
        self.directory = store.root / "acquisitions"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.store._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS acquisition_operations (operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result_id TEXT)")
        self.recovery_warnings = []
        self._recover_attempts()

    def _recover_attempts(self):
        """Discover dead-owner evidence without binding sockets or replaying jobs."""
        checked = 0
        for directory in self.directory.iterdir():
            if checked >= 1000:
                self.recovery_warnings.append("Startup recovery inspected at most 1000 attempt directories.")
                break
            checked += 1
            if directory.is_symlink() or not directory.is_dir():
                continue
            metadata = directory / "attempt.json"
            if metadata.is_symlink() or not metadata.is_file():
                continue
            try:
                with metadata.open("rb") as stream:
                    encoded = stream.read(4 * 1024 * 1024 + 1)
                if len(encoded) > 4 * 1024 * 1024:
                    raise ValueError("Attempt metadata exceeds the recovery size limit.")
                document = json.loads(encoded)
                operation_id = _text(document.get("operation_id"), "operation_id")
                with self.store._connect() as db:
                    row = db.execute("SELECT result_id FROM acquisition_operations WHERE operation_id=?", (operation_id,)).fetchone()
                    existing = db.execute("SELECT id FROM documents WHERE kind='acquisition' AND json_extract(data,'$.operation_id')=? ORDER BY created_at DESC LIMIT 1", (operation_id,)).fetchone()
                if row is None or row[0] is not None:
                    continue
                if existing:
                    with self.store._connect() as db:
                        db.execute("UPDATE acquisition_operations SET result_id=? WHERE operation_id=? AND result_id IS NULL", (existing[0], operation_id))
                    continue
                owner = document.get("owner_process", {})
                try:
                    process = psutil.Process(int(owner["pid"]))
                    if abs(process.create_time() - float(owner["created_at"])) < .01 and process.status() != psutil.STATUS_ZOMBIE:
                        continue
                except psutil.NoSuchProcess:
                    pass
                except (psutil.AccessDenied, KeyError, ValueError, TypeError):
                    raise ValueError("Cannot establish the prior acquisition process is dead; recovery withheld.") from None
                document.update(state="interrupted", recovered_after_process_loss=True,
                                interruption_reason="Engine process ended before acquisition finalization; partial local evidence retained without replay.")
                document["retained_files"] = []
                roles = {"exchange.mavudp": "mavlink_acquisition_transcript", "parameters.snapshot.json": "parameter_readback_snapshot",
                         "download.partial.bin": "onboard_log_candidate", "download.coverage.json": "download_byte_coverage"}
                for name, role in roles.items():
                    path = directory / name
                    if path.is_file() and not path.is_symlink():
                        document["retained_files"].append({"path": str(path), "role": role, "byte_size": path.stat().st_size,
                            "sealed": False, "durability": "crash_recovered_unverified", "archived": False})
                with self.store._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute("SELECT result_id FROM acquisition_operations WHERE operation_id=?", (operation_id,)).fetchone()
                    if current is None or current[0] is not None:
                        continue
                    existing = db.execute("SELECT id FROM documents WHERE kind='acquisition' AND json_extract(data,'$.operation_id')=? LIMIT 1", (operation_id,)).fetchone()
                    if existing:
                        result_id = existing[0]
                    else:
                        recovered = document | {"id": str(uuid4()), "created_at": self.store._now(), "schema_version": 1}
                        db.execute("INSERT INTO documents VALUES (?,?,?,?)", (recovered["id"], "acquisition", recovered["created_at"], _json(recovered)))
                        result_id = recovered["id"]
                    db.execute("UPDATE acquisition_operations SET result_id=? WHERE operation_id=?", (result_id, operation_id))
            except (ValueError, TypeError, OSError, KeyError) as error:
                if len(self.recovery_warnings) < 32:
                    self.recovery_warnings.append(f"Attempt {directory.name}: recovery withheld ({str(error)[:200]}); original files remain available for explicit review/import.")

    def preview(self, params):
        connection = _connection(params.get("connection"))
        limits = _limits(params.get("limits"))
        return {"connection": connection, "limits": limits,
                "applicability_fingerprint": hashlib.sha256(_json(connection).encode()).hexdigest(),
                "prerequisites": ["Shared mode uses the configured recorder socket; legacy dedicated mode requires a separate exclusive local UDP port.",
                                  "Remote endpoint must already stream selected disarmed autopilot heartbeats and time_boot_ms data.",
                                  "A read-only AUTOPILOT_VERSION request discovers the aircraft UID before parameters are requested; an optional UID lock must match. No reported UID means recording only.",
                                  "Explicit log-transfer side-effect confirmation is required for listing/downloading."],
                "outgoing_allowlist": ["PARAM_REQUEST_LIST", "PARAM_REQUEST_READ", "AUTOPILOT_VERSION request only", "LOG_REQUEST_LIST", "LOG_REQUEST_DATA", "LOG_REQUEST_END"],
                "limitations": LIMITATIONS}

    def dispatch(self, method, params, cancel_check=None, progress=None):
        if method == "acquisition.preview":
            return self.preview(params)
        if method == "acquisition.list":
            limit = _integer(params.get("limit", 50), "limit", 1, 100)
            offset = _integer(params.get("offset", 0), "offset", 0, 1_000_000)
            documents = self.store.list_documents("acquisition", limit, offset)
            with self.store._connect() as db:
                total = db.execute("SELECT count(*) FROM documents WHERE kind='acquisition'").fetchone()[0]
            return {"acquisitions": [self.summary(value) for value in documents], "offset": offset, "limit": limit,
                    "total": total, "next_offset": offset + limit if offset + len(documents) < total else None, "recovery_warnings": self.recovery_warnings}
        if method == "acquisition.detail":
            return {"acquisition": self.store.get_document("acquisition", params.get("acquisition_id"))}
        kinds = {"acquisition.parameters.capture": "parameters", "acquisition.logs.list": "log_list", "acquisition.logs.download": "log_download"}
        if method not in kinds:
            raise ValueError("Unknown acquisition method.")
        return self.run(kinds[method], params, cancel_check, progress)

    @staticmethod
    def summary(document):
        result = {key: value for key, value in document.items() if key not in ("entries", "parameter_capture")}
        if "entries" in document:
            result["entry_count"] = len(document["entries"])
        if "parameter_capture" in document:
            capture = document["parameter_capture"]
            result["parameter_capture"] = {key: capture.get(key) for key in ("snapshot_id", "completeness", "consistency", "freshness", "flight_time_applicability", "decoded_count", "expected_count", "atomic")}
        return result

    def run(self, kind, params, cancel_check=None, progress=None):
        connection, limits = _connection(params.get("connection")), _limits(params.get("limits"))
        aircraft_id = _text(params.get("aircraft_id"), "aircraft_id")
        synthetic = params.get("synthetic", False)
        if type(synthetic) is not bool:
            raise ValueError("synthetic must be boolean.")
        if kind.startswith("log_") and params.get("confirm_log_transfer_side_effects") is not True:
            raise ValueError("Explicit confirmation of possible onboard logging suspension is required before a log transfer.")
        role = params.get("role", "unknown")
        if role not in ("preflight", "postflight", "unknown"):
            raise ValueError("Parameter role must be preflight, postflight or unknown.")
        if not self._lock.acquire(blocking=False):
            raise ValueError("Another acquisition owns the transport; finish or cancel it first.")
        try:
            return self._run_owned(kind, params, connection, limits, aircraft_id, synthetic, role, cancel_check, progress)
        finally:
            self._lock.release()

    def _run_owned(self, kind, params, connection, limits, aircraft_id, synthetic, role, cancel_check, progress):
        operation_id = _text(params.get("operation_id"), "operation_id")
        fingerprint = hashlib.sha256(_json(connection).encode()).hexdigest()
        if params.get("applicability_fingerprint") != fingerprint:
            raise ValueError("Connection applicability fingerprint changed; preview the endpoint again.")
        payload_hash = hashlib.sha256(_json({"kind": kind, "params": params}).encode()).hexdigest()
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload_hash,result_id FROM acquisition_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if prior:
                if prior[0] != payload_hash:
                    raise ValueError("operation_id is already bound to another acquisition payload.")
                if prior[1] is None:
                    raise ValueError("This operation previously started without a terminal result; inspect retained evidence and retry explicitly with a new operation ID.")
                result = self.store.get_document("acquisition", prior[1])
                return {"acquisition": result, "operation_replayed": True}
            db.execute("INSERT INTO acquisition_operations VALUES (?,?,NULL)", (operation_id, payload_hash))
        directory = self.directory / str(uuid4())
        directory.mkdir()
        document = {"operation_id": operation_id, "kind": kind, "aircraft_id": aircraft_id,
                    "connection": connection, "synthetic": synthetic, "state": "partial",
                    "protocol_version": VERSION, "source_refs": [], "retained_files": [],
                    "owner_process": {"pid": os.getpid(), "created_at": psutil.Process().create_time()},
                    "association_state": "provisional", "historical_applicability": "unknown", "limitations": LIMITATIONS}
        _write_metadata(directory / "attempt.json", document | {"state": "in_progress"})
        link = _Link(directory, connection, limits, kind, cancel_check, self.external_gate, self.shared_transport)
        cancelled = None
        try:
            link.open()
            if progress:
                progress("waiting_for_disarmed_identity_and_boot_clock", 0, None)
            link.ready()
            if kind == "parameters":
                self._parameters(link, document, aircraft_id, role, directory, progress)
            elif kind == "log_list":
                self._list_logs(link, document, progress)
            else:
                self._download(link, document, params, directory, progress)
            document["state"] = "complete"
        except CancelledError as error:
            cancelled = error
            document.update(state="interrupted", interruption_reason="Operator cancellation; partial evidence retained.")
        except (AcquisitionStopped, ValueError, OSError) as error:
            document.update(state="interrupted" if link.gate_reason else "partial", interruption_reason=str(error)[:2000])
        finally:
            try:
                link.cleanup()
                # Cleanup drains queued telemetry and can still discover a
                # contradiction after the download routine wrote its coverage.
                coverage_path = directory / "download.coverage.json"
                if document.get("download") and coverage_path.exists():
                    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
                    coverage["transfer_complete"] = document["download"]["transfer_complete"]
                    _write_metadata(coverage_path, coverage)
            except OSError as error:
                document.update(state="partial", finalization_error=str(error)[:1000])
            document["transport_evidence"] = link.evidence()
            roles = {"exchange.mavudp": "mavlink_acquisition_transcript", "parameters.snapshot.json": "parameter_readback_snapshot",
                     "download.partial.bin": "onboard_log_candidate", "download.coverage.json": "download_byte_coverage"}
            paths = [path for path in sorted(directory.iterdir()) if path.name != "attempt.json" and path.is_file()]
            # Enumerate every original before archiving; a cancellation while
            # copying one file must not hide other retained partial evidence.
            for path in paths:
                retained = {"path": str(path), "role": roles.get(path.name, "acquisition_evidence"),
                            "byte_size": path.stat().st_size, "sealed": False, "durability": "unconfirmed", "archived": False}
                document["retained_files"].append(retained)
                try:
                    with path.open("r+b") as stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(path, 0o444)
                    retained.update(sealed=True, durability="fsynced")
                except OSError:
                    retained["sealed"] = False
            try:
                for path, retained in zip(paths, document["retained_files"]):
                    if cancelled or (cancel_check and cancel_check()):
                        if cancelled is None:
                            cancelled = CancelledError("Cancelled before evidence archival.")
                        break
                    source = self.store.archive_file(path, cancel_check=cancel_check,
                        acquisition_method="app_owned_readonly_mavlink_acquisition")
                    document["source_refs"].append(source | {"role": retained["role"]})
                    retained.update(archived=True, sha256=source["sha256"], source_id=source["id"])
            except CancelledError as error:
                cancelled = error
            except (OSError, ValueError) as error:
                document.update(state="partial", archive_error=str(error)[:1000])
            if cancelled:
                document.update(state="interrupted", interruption_reason="Cancelled; local sealed partial evidence retained.")
            _write_metadata(directory / "attempt.json", document)
            document = self.store.add_document("acquisition", document)
            with self.store._connect() as db:
                db.execute("UPDATE acquisition_operations SET result_id=? WHERE operation_id=?", (document["id"], operation_id))
        if cancelled:
            raise CancelledError(f"Acquisition {document['id']} interrupted; inspect acquisition.list for retained evidence.") from cancelled
        return {"acquisition": document, "operation_replayed": False}

    def _parameters(self, link, document, aircraft_id, role, directory, progress):
        accumulator = ParameterAccumulator(aircraft_id, "single_observed_acquisition_clock_segment",
                    {"system_id": link.connection["system_id"], "component_id": link.connection["component_id"]}, link.firmware_identity)
        expected, received = None, set()
        retries, semantic_errors, requested = {}, [], []
        target = (link.connection["system_id"], link.connection["component_id"])
        link.send(link.mavlink.MAVLink_param_request_list_message(*target))
        last_parameter = time.monotonic()
        complete_at = None
        rounds = 0
        interruption = None
        try:
            while True:
                for message, evidence in link.poll():
                    if message.get_type() != "PARAM_VALUE":
                        continue
                    last_parameter = time.monotonic()
                    raw = message.to_dict()
                    count, index = int(raw["param_count"]), int(raw["param_index"])
                    if not 1 <= count <= MAX_PARAMETERS or not 0 <= index < count:
                        raise AcquisitionStopped("Unindexed/unsupported parameter update prevents complete enumeration for this generation.")
                    if expected is not None and count != expected:
                        raise AcquisitionStopped("Parameter count changed during enumeration; configuration generation is inconsistent.")
                    expected = count
                    value, encoding, error = self._parameter_value(message, link.capabilities)
                    event = {"name": raw["param_id"], "value": value, "type": int(raw["param_type"]),
                             "index": index, "count": count, "encoding_provenance": encoding,
                             "system_id": target[0], "component_id": target[1], **evidence}
                    if error:
                        if len(semantic_errors) < 32:
                            semantic_errors.append(error)
                        event["encoding_provenance"] = None
                    try:
                        outcome = accumulator.observe(event)
                        if not outcome["accepted"] and len(semantic_errors) < 32:
                            semantic_errors.append(outcome["reason"])
                    except ValueError as error:
                        if len(semantic_errors) < 32:
                            semantic_errors.append(str(error))
                    received.add(index)
                    if progress:
                        progress("receiving_parameters", len(received), expected)
                now = time.monotonic()
                if expected is not None and len(received) == expected:
                    complete_at = complete_at or now
                    if now - complete_at >= link.limits["settle_s"]:
                        break
                elif now - last_parameter >= link.limits["retry_wait_s"] and now - link.last_send >= link.limits["request_interval_s"]:
                    if expected is None:
                        if rounds >= link.limits["retries"]:
                            raise AcquisitionStopped("No parameter enumeration response within retry bounds.")
                        link.send(link.mavlink.MAVLink_param_request_list_message(*target))
                        rounds += 1
                    else:
                        missing = next((i for i in range(min(expected, 32768)) if i not in received and retries.get(i, 0) < link.limits["retries"]), None)
                        if missing is None:
                            raise AcquisitionStopped("Parameter enumeration remains incomplete after bounded retries; indices above 32767 cannot use PARAM_REQUEST_READ.")
                        link.send(link.mavlink.MAVLink_param_request_read_message(*target, b"", missing))
                        retries[missing] = retries.get(missing, 0) + 1
                        if len(requested) < 64:
                            requested.append(missing)
                    last_parameter = now
            summary = accumulator.summary()
            if summary["completeness"] != "complete_for_scope" or semantic_errors:
                raise AcquisitionStopped("All reported indices arrived but parameter decoding/consistency is incomplete.")
        except BaseException as error:
            interruption = str(error) or type(error).__name__
            raise
        finally:
            capture = accumulator.finalize(role=role)
            capture.update(transport="MAVLink_PARAM_REQUEST_LIST_and_READ", acquisition_basis="explicit_requested_enumeration",
                           full_readback_requested=True, preflight_full_request="requested" if role == "preflight" else "not_preflight",
                           atomic=False, freshness="unknown", flight_time_applicability="unknown",
                           missing_index_retry_examples=requested, semantic_errors=semantic_errors)
            if semantic_errors or interruption or (expected is not None and len(received) != expected):
                capture["completeness"] = "partial"
            if interruption:
                capture["interruption_reason"] = interruption
                if "count changed" in interruption:
                    capture["consistency"] = "conflict"
                elif capture["consistency"] == "no_observed_conflict":
                    capture["consistency"] = "unknown"
            document["parameter_capture"] = capture
            snapshot = accumulator.snapshot()
            snapshot.update(acquisition_basis="explicit_requested_enumeration", flight_time_applicability="unknown", atomic=False,
                            completeness=capture["completeness"], consistency=capture["consistency"],
                            interruption_reason=interruption, semantic_errors=semantic_errors)
            path = directory / "parameters.snapshot.json"
            path.write_text(_json(snapshot), encoding="utf-8")
            document["parameter_snapshot_file"] = path.name

    @staticmethod
    def _parameter_value(message, capabilities):
        raw = message.to_dict()
        kind = int(raw["param_type"])
        value = float(raw["param_value"])
        if kind == 9:
            return str(value), None, None if math.isfinite(value) else "Nonfinite REAL32 parameter."
        bytewise, cast = bool(capabilities & 16), bool(capabilities & 131072)
        if bytewise == cast:
            return str(value), None, "Parameter encoding capabilities are absent or contradictory."
        if kind in (7, 8, 10):
            return str(value), None, "64-bit parameter types are not losslessly carried by the legacy four-byte field."
        if bytewise:
            frame = bytes(message.get_msgbuf())
            header = 10 if frame[0] == 0xFD else 6
            encoded = frame[header:header + 4]
            formats = {1: "<B", 2: "<b", 3: "<H", 4: "<h", 5: "<I", 6: "<i"}
            if kind not in formats or len(encoded) != 4:
                return str(value), None, "Unsupported parameter wire type."
            size = struct.calcsize(formats[kind])
            return str(struct.unpack(formats[kind], encoded[:size])[0]), "mavlink_bytewise_decoded", None
        if not math.isfinite(value) or value != int(value) or abs(value) >= 2**24:
            return str(value), None, "C-cast integer precision/range is not independently lossless."
        return str(int(value)), "mavlink_c_cast", None

    def _list_logs(self, link, document, progress):
        target = (link.connection["system_id"], link.connection["component_id"])
        entries, metadata = {}, None
        last = time.monotonic()
        retries = 0
        complete_at = None
        link.send(link.mavlink.MAVLink_log_request_list_message(*target, 0, 65535))
        try:
            while True:
                for message, evidence in link.poll():
                    if message.get_type() != "LOG_ENTRY":
                        continue
                    raw = message.to_dict()
                    current = (int(raw["num_logs"]), int(raw["last_log_num"]))
                    if current[0] > 2048:
                        raise AcquisitionStopped("Remote log inventory exceeds 2048-entry acquisition bound.")
                    if metadata is not None and metadata != current:
                        raise AcquisitionStopped("Remote log inventory changed during listing.")
                    metadata = current
                    if current[0] == 0:
                        document["entries"] = []
                        document["listing_complete"] = True
                        return
                    item = {"log_id": int(raw["id"]), "reported_size": int(raw["size"]),
                            "reported_time_utc": int(raw["time_utc"]) or None, "remote_filename": None,
                            "num_logs": current[0], "last_log_num": current[1], "evidence": evidence}
                    old = entries.get(item["log_id"])
                    if old and any(old[key] != item[key] for key in ("reported_size", "reported_time_utc", "num_logs", "last_log_num")):
                        raise AcquisitionStopped("A remote log entry changed during listing.")
                    entries[item["log_id"]] = item
                    if len(entries) > current[0]:
                        raise AcquisitionStopped("More remote log IDs than the declared inventory count.")
                    last = time.monotonic()
                    if progress:
                        progress("listing_onboard_logs", len(entries), current[0])
                now = time.monotonic()
                if metadata is not None and len(entries) == metadata[0]:
                    complete_at = complete_at or now
                    if now - complete_at >= link.limits["settle_s"]:
                        document["listing_complete"] = True
                        return
                elif now - last >= link.limits["retry_wait_s"]:
                    if retries >= link.limits["retries"]:
                        raise AcquisitionStopped("Remote log listing remains incomplete after bounded retries.")
                    link.send(link.mavlink.MAVLink_log_request_list_message(*target, 0, 65535))
                    retries += 1
                    last = now
        finally:
            document["entries"] = sorted(entries.values(), key=lambda item: item["log_id"])
            document.setdefault("listing_complete", False)

    def _entry(self, link, log_id, expected):
        target = (link.connection["system_id"], link.connection["component_id"])
        for attempt in range(link.limits["retries"] + 1):
            link.send(link.mavlink.MAVLink_log_request_list_message(*target, log_id, log_id))
            until = time.monotonic() + link.limits["retry_wait_s"]
            while time.monotonic() < until:
                for message, evidence in link.poll():
                    if message.get_type() == "LOG_ENTRY" and int(message.id) == log_id:
                        if int(message.size) != expected["reported_size"] or (int(message.time_utc) or None) != expected["reported_time_utc"]:
                            raise AcquisitionStopped("Selected remote log size/time changed; listing identity is no longer stable.")
                        return evidence
        raise AcquisitionStopped("Selected log ID was not re-observed in the current inventory.")

    def _download(self, link, document, params, directory, progress):
        listing = self.store.get_document("acquisition", params.get("listing_id"))
        log_id = _integer(params.get("log_id"), "log_id", 0, 65535)
        if listing["kind"] != "log_list" or listing["state"] != "complete" or not listing.get("listing_complete"):
            raise ValueError("Choose a completed observed log listing.")
        if listing["connection"] != link.connection or listing["aircraft_id"] != document["aircraft_id"] or listing["synthetic"] != document["synthetic"]:
            raise ValueError("Listing source/profile does not match this acquisition.")
        entries = [entry for entry in listing["entries"] if entry["log_id"] == log_id]
        if len(entries) != 1:
            raise ValueError("Select an actual returned log ID; guessing IDs is not supported.")
        selected = entries[0]
        size = _integer(selected["reported_size"], "reported log size", 0, link.limits["max_log_bytes"])
        document["download"] = {"listing_id": listing["id"], "log_id": log_id, "reported_size": size,
                                "remote_filename": None, "received_bytes": 0, "contiguous_prefix_bytes": 0,
                                "transfer_complete": False, "logger_closed": "unknown", "remote_checksum": None,
                                "historical_applicability": "unknown", "association_state": "provisional"}
        document["download"]["before_entry_evidence"] = self._entry(link, log_id, selected)
        target = (link.connection["system_id"], link.connection["component_id"])
        path = directory / "download.partial.bin"
        digest = hashlib.sha256()
        position = 0
        buffer, covered = bytearray(), bytearray()
        received = 0
        try:
            with path.open("w+b") as output:
                while position < size:
                    length = min(WINDOW, size - position)
                    buffer, covered = bytearray(length), bytearray(length)
                    filled = 0
                    for attempt in range(link.limits["retries"] + 1):
                        missing = covered.find(0)
                        if missing < 0:
                            break
                        # One bounded contiguous suffix request; duplicate prefix
                        # packets are checked, never counted twice.
                        link.send(link.mavlink.MAVLink_log_request_data_message(*target, log_id, position + missing, length - missing))
                        until = time.monotonic() + link.limits["retry_wait_s"]
                        while time.monotonic() < until and filled < length:
                            for message, evidence in link.poll():
                                if message.get_type() != "LOG_DATA":
                                    continue
                                if int(message.id) != log_id:
                                    raise AcquisitionStopped("LOG_DATA belongs to a different log ID; transfer source is ambiguous.")
                                offset, count = int(message.ofs), int(message.count)
                                if count > 90:
                                    raise AcquisitionStopped("LOG_DATA count exceeds its wire payload.")
                                if count == 0:
                                    if offset != size or position + filled < size:
                                        raise AcquisitionStopped("Premature or inconsistent log EOF; download is truncated.")
                                    continue
                                data = bytes(message.data[:count])
                                if 0 <= offset and offset + count <= position:
                                    output.flush()
                                    output.seek(offset)
                                    if output.read(count) != data:
                                        raise AcquisitionStopped("Conflicting late retransmission of a completed log byte range.")
                                    continue
                                if offset < position or offset + count > position + length:
                                    raise AcquisitionStopped("LOG_DATA lies outside the currently requested bounded window.")
                                begin = offset - position
                                for index, byte in enumerate(data, begin):
                                    if covered[index] and buffer[index] != byte:
                                        raise AcquisitionStopped("Conflicting duplicate/overlapping LOG_DATA bytes; candidate is inconsistent.")
                                    if not covered[index]:
                                        filled += 1
                                        received += 1
                                        covered[index] = 1
                                    buffer[index] = byte
                                output.seek(offset)
                                output.write(data)
                                document["download"]["received_bytes"] = received
                                if progress:
                                    progress("downloading_onboard_log", received, size)
                                until = time.monotonic() + link.limits["retry_wait_s"]
                        if filled == length:
                            break
                    if filled != length:
                        raise AcquisitionStopped("Log byte window incomplete after bounded retries; partial bytes retained.")
                    digest.update(buffer)
                    position += length
                    document["download"]["contiguous_prefix_bytes"] = position
                # Require explicit EOF for this observed size. A short data packet
                # or silence does not prove the requested file prefix is complete.
                eof = False
                for attempt in range(link.limits["retries"] + 1):
                    link.send(link.mavlink.MAVLink_log_request_data_message(*target, log_id, size, 90))
                    until = time.monotonic() + link.limits["retry_wait_s"]
                    while time.monotonic() < until and not eof:
                        for message, evidence in link.poll():
                            if message.get_type() == "LOG_DATA":
                                if int(message.id) != log_id:
                                    raise AcquisitionStopped("LOG_DATA belongs to another ID while waiting for EOF.")
                                count, offset = int(message.count), int(message.ofs)
                                if 0 < count <= 90 and offset + count <= size:
                                    output.flush()
                                    output.seek(offset)
                                    if output.read(count) != bytes(message.data[:count]):
                                        raise AcquisitionStopped("Conflicting late retransmission before EOF.")
                                    continue
                                if count != 0 or offset != size:
                                    raise AcquisitionStopped("Remote log grew or returned a mismatched EOF boundary.")
                                document["download"]["eof_evidence"] = evidence
                                eof = True
                    if eof:
                        break
                if not eof:
                    raise AcquisitionStopped("Explicit EOF was not observed; complete transfer is unconfirmed.")
                output.flush()

                def validate_late_data(message):
                    # Keep validating during the final inventory recheck and the
                    # receive drain before LOG_REQUEST_END. EOF does not permit
                    # contradictory bytes to disappear from the transfer result.
                    count, offset = int(message.count), int(message.ofs)
                    valid = int(message.id) == log_id and 0 <= count <= 90
                    if valid and count == 0:
                        valid = offset == size
                    elif valid:
                        valid = 0 <= offset and offset + count <= size
                        if valid:
                            with path.open("rb") as retained:
                                retained.seek(offset)
                                valid = retained.read(count) == bytes(message.data[:count])
                    if not valid:
                        reason = "Conflicting LOG_DATA after EOF during final transfer validation."
                        document.update(state="partial", interruption_reason=reason)
                        document["download"]["transfer_complete"] = False
                        raise AcquisitionStopped(reason)

                link.late_data_validator = validate_late_data
                document["download"]["after_entry_evidence"] = self._entry(link, log_id, selected)
                output.flush()
                os.fsync(output.fileno())
                document["download"].update(transfer_complete=True, sha256=digest.hexdigest())
        finally:
            document["download"]["local_file"] = path.name
            document["download"]["partial_window_offset"] = position
            document["download"]["partial_window_received_bytes"] = int(sum(covered)) if position < size else 0
            # Exact sparse coverage is retained separately; RPC result stays small.
            ranges = []
            start = None
            if position < size:
                for index, valid in enumerate(covered):
                    if valid and start is None:
                        start = index
                    if start is not None and (not valid or index == len(covered) - 1):
                        end = index if not valid else index + 1
                        ranges.append([position + start, position + end])
                        start = None
            (directory / "download.coverage.json").write_text(_json({"contiguous_prefix_bytes": position,
                "received_ranges_half_open": ranges, "reported_size": size,
                "transfer_complete": document["download"]["transfer_complete"]}), encoding="utf-8")
