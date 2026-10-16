"""Exclusive UDP capture, optionally sharing its socket with gated readback.

The coordinator alone owns the catalog; this worker owns append-only capture files
and the UDP socket. Read-only requests require an explicit shared configuration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
import select
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID

import psutil

VERSION = "q22-passive-recorder/1"
CHUNK_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 ** 3
MAX_SOURCES = 64
MAX_FRAME_BYTES = 280
CHECKPOINT_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 3.0
RAW_HEADER = struct.Struct(">IQQ4sH")
RAW_MAGIC = b"Q22UDP1\0"
LIMITATIONS = [
    "Receive-only unless shared readback is configured and a runtime acquisition submits an allowlisted, disarmed request; no GCS routing or controlling heartbeats.",
    "CRC validates framing, not aircraft identity or authenticity; signatures are retained but not verified.",
    "An unsigned live UDP feed cannot prove anti-replay; explicitly configured replay feeds are rejected.",
    "Forwarded capture may omit GCS-sent traffic; preserve native GCS logs as complementary evidence.",
    "Coordinator or worker failure ends capture; restart records a gap and never silently resumes it.",
    "Kernel UDP loss is not fully observable; sequence discontinuities and recorder stalls remain explicit.",
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _replace_checkpoint(temporary: Path, path: Path) -> None:
    # Windows' normal read handles do not share delete/rename access. A short
    # status read (or antivirus scan) can therefore overlap an atomic replace.
    # Keep the fsynced temporary and prior checkpoint intact throughout retries;
    # persistent locks/ACL failures still surface within a bounded retry window.
    deadline, delay = time.monotonic() + .5, .01
    while True:
        try:
            os.replace(temporary, path)
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if (os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33}
                    or remaining <= 0):
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, .05)


def _atomic_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".checkpoint-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        _replace_checkpoint(temporary, path)
    finally:
        # Windows cannot unlink an open NamedTemporaryFile. The stream has
        # closed before cleanup, including when a checkpoint runs out of space.
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path: Path, maximum: int = 4 * 1024 * 1024) -> dict:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Recorder metadata exceeded its bounded size.")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Invalid recorder metadata.")
    return value


def _process_alive(pid, created) -> bool | None:
    try:
        process = psutil.Process(int(pid))
        return abs(process.create_time() - float(created)) < .01 and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return False
    except psutil.AccessDenied:
        return None


def _exclusive_file_lock(path: Path):
    """Lifetime lock, released by the OS even after an ungraceful exit."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        if os.name == "nt":
            import msvcrt
            if not stream.read(1):
                stream.write(b"\0")
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        stream.close()
        raise
    return stream


def shared_bootstrap_ready(current, connection):
    """Wire evidence for initial version discovery; never parameter authority."""
    from .acquisition import _same_connection
    selected = [source for source in current.get("sources", [])
                if source.get("system_id") == connection.get("system_id")
                and source.get("component_id") == connection.get("component_id")]
    return (connection.get("transport") == "shared_recorder"
            and _same_connection(current.get("shared_readback"), connection) and current.get("active")
            and current.get("state") == "recording" and not current.get("callback_error")
            and current.get("event_backlog_bytes", 0) <= 256 * 1024
            and current.get("status_age_s") is not None and 0 <= current["status_age_s"] <= 2.5
            and len(selected) == 1 and selected[0].get("armed_state") == "disarmed"
            and not selected[0].get("readback_identity_conflict")
            and (not connection.get("expected_uid") or selected[0].get("uid") in (None, connection["expected_uid"]))
            and selected[0].get("sender") == connection.get("remote_address")
            and selected[0].get("sender_port") == connection.get("remote_port"))


def _unlock_file(stream):
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        stream.close()


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Recorder configuration must be an object.")
    if config.get("feed_origin", "live_forwarded") != "live_forwarded":
        raise ValueError("Only an explicitly configured live forwarded feed can drive recording; replay is unsupported.")
    host = str(ipaddress.IPv4Address(config.get("bind_host", "")))
    if ipaddress.IPv4Address(host).is_multicast:
        raise ValueError("This recorder supports exclusive unicast IPv4 receiving only.")
    sources = config.get("allowed_sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise ValueError("Configure one to eight explicit allowed sender IPv4 addresses.")
    sources = sorted({str(ipaddress.IPv4Address(item)) for item in sources})
    if any(ipaddress.IPv4Address(item).is_multicast or item == "0.0.0.0" for item in sources):
        raise ValueError("Allowed senders must be explicit unicast IPv4 addresses.")
    result = {"bind_host": host, "allowed_sources": sources, "feed_origin": "live_forwarded"}
    for key, default, low, high in (("bind_port", None, 1, 65535),
                                   ("expected_system_id", None, 1, 255),
                                   ("expected_component_id", None, 1, 255),
                                   ("reserve_bytes", 64 * 1024 ** 2, 64 * 1024 ** 2, 1024 ** 4),
                                   ("max_capture_bytes", 2 * 1024 ** 3, 1024 ** 2, MAX_CAPTURE_BYTES)):
        value = config.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer in {low}..{high}.")
        result[key] = value
    synthetic = config.get("synthetic", False)
    if type(synthetic) is not bool:
        raise ValueError("synthetic must be boolean.")
    result["synthetic"] = synthetic
    readback = config.get("readback")
    if readback is not None:
        from .acquisition import _connection
        connection = _connection(readback)
        if (connection.get("transport") != "shared_recorder" or connection["local_address"] != host
                or connection["local_port"] != result["bind_port"]
                or connection["remote_address"] not in sources
                or connection["system_id"] != result["expected_system_id"]
                or connection["component_id"] != result["expected_component_id"]):
            raise ValueError("Shared readback must use the selected recorder socket and aircraft.")
        result["readback"] = connection
    return result


class _Output:
    def __init__(self, path: Path, role: str, first_record: int = 1):
        self.path, self.role = path, role
        self.stream = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                       getattr(os, "O_BINARY", 0), 0o600), "wb", buffering=0)
        self.digest, self.bytes = hashlib.sha256(), 0
        self.record_start, self.record_end = first_record, first_record - 1
        self.durable_bytes, self.complete, self.checkpoint_error = 0, False, None

    def note_record(self, record: int):
        self.record_start = record if self.record_end < self.record_start else min(self.record_start, record)
        self.record_end = max(self.record_end, record)

    def write(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            count = self.stream.write(remaining)
            if not count:
                raise OSError("Capture write made no progress.")
            self.digest.update(remaining[:count])
            self.bytes += count
            remaining = remaining[count:]

    def checkpoint(self):
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.durable_bytes = self.bytes

    def finish(self, complete: bool) -> dict:
        if not self.stream.closed:
            try:
                self.checkpoint()
                self.complete = complete
            except OSError as error:
                self.checkpoint_error = str(error)[:500]
            finally:
                self.stream.close()
        return {"path": str(self.path), "role": self.role, "bytes": self.bytes,
                "sha256": self.digest.hexdigest(), "complete": self.complete,
                "durable_bytes": self.durable_bytes, "checkpoint_error": self.checkpoint_error,
                "record_start": self.record_start, "record_end": self.record_end}


class _Worker:
    def __init__(self, directory: Path):
        self.directory = directory
        document = _read_json(directory / "config.json")
        self.config = validate_config(document["config"])
        self.capture_id = str(UUID(document["capture_session_id"]))
        self.parent_pid, self.parent_created = document["parent_pid"], document["parent_created"]
        self.state = "starting"
        self.failure = None
        self.raw = self.tlog = self.journal = None
        self.artifacts = []
        self.artifacts_by_name = {}
        self.chunk = self.records = self.events = self.total_bytes = 0
        self.packets = self.received_bytes = self.valid_frames = self.invalid_frames = self.rejected_senders = 0
        self.sources, self.buffers = {}, {}
        self.started_ns = time.monotonic_ns()
        self.started_utc_ns = time.time_ns()
        self.last_packet_ns = None
        self.durable_bytes = 0
        self.checkpoint_ns = None
        self.last_loop_ns = self.started_ns
        self.last_clock_offset = self.started_utc_ns - self.started_ns
        self.socket = None
        self.actual_rcvbuf = None
        self.process_created = psutil.Process().create_time()
        self.ownership = None
        self.last_readback_id = None
        self.readback_sends = 0
        self.last_readback_send = 0.0

    def status(self) -> dict:
        now = time.monotonic_ns()
        written = {item["path"]: item["bytes"] for item in self.artifacts}
        written.update({str(output.path): output.bytes for output in (self.raw, self.tlog, self.journal)
                        if output is not None})
        sources = []
        for value in self.sources.values():
            item = {key: val for key, val in value.items() if not key.startswith("_")}
            stamp = value.get("last_heartbeat_monotonic_ns")
            age = (now - int(stamp)) / 1e9 if stamp is not None else None
            item.update(heartbeat_age_s=age,
                        armed_state="unknown" if age is None or age > HEARTBEAT_STALE_SECONDS else
                        ("armed" if value.get("armed") else "disarmed"))
            sources.append(item)
        return {"version": VERSION, "capture_session_id": self.capture_id,
                "state": self.state, "active": self.state in {"starting", "recording", "stopping"},
                "worker_pid": os.getpid(), "worker_created": self.process_created,
                "parent_pid": self.parent_pid, "parent_created": self.parent_created,
                "bindings": [{"local_address": self.config["bind_host"], "port": self.config["bind_port"]}],
                "owns_udp": self.socket is not None, "synthetic": self.config["synthetic"],
                "shared_readback": self.config.get("readback"), "readback_sent_messages": self.readback_sends,
                "packets": self.packets, "received_bytes": self.received_bytes,
                "valid_frames": self.valid_frames, "invalid_frames": self.invalid_frames,
                "rejected_senders": self.rejected_senders, "records": self.records,
                "written_bytes": sum(written.values()), "durable_bytes": self.durable_bytes,
                "last_checkpoint_monotonic_ns": str(self.checkpoint_ns) if self.checkpoint_ns else None,
                "last_packet_monotonic_ns": str(self.last_packet_ns) if self.last_packet_ns else None,
                "status_monotonic_ns": str(now), "updated_at": _utc(),
                "kernel_receive_buffer_bytes": self.actual_rcvbuf,
                "sources": sources, "failure": self.failure,
                "completed_chunks": len(self.artifacts), "limits": LIMITATIONS}

    def event(self, kind: str, received_ns: int | None = None, utc_ns: int | None = None, **fields):
        self.events += 1
        document = {"event_id": self.events, "kind": kind, "capture_session_id": self.capture_id,
                    "origin": "synthetic" if self.config["synthetic"] else "live_forwarded",
                    "live": not self.config["synthetic"],
                    "received_monotonic_ns": str(received_ns if received_ns is not None else time.monotonic_ns()),
                    "received_utc_us": str((utc_ns if utc_ns is not None else time.time_ns()) // 1000),
                    "clock_scope": f"capture:{self.capture_id}", **fields}
        data = json.dumps(document, allow_nan=False, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(data) > 16 * 1024:
            raise ValueError("Recorder event exceeded 16 KiB.")
        if kind != "recorder_error" and self.total_bytes + len(data) > self.config["max_capture_bytes"]:
            raise OSError("Capture storage budget reached; prior evidence preserved.")
        self.journal.write(data)
        self.total_bytes += len(data)

    def rotate(self):
        previous, self.raw, self.tlog = (self.raw, self.tlog), None, None
        checkpoint_failed = False
        for output in previous:
            if output is not None:
                artifact = output.finish(True)
                self.artifacts.append(artifact)
                self.artifacts_by_name[output.path.name] = artifact
                checkpoint_failed |= bool(artifact["checkpoint_error"])
        if checkpoint_failed:
            raise OSError("Capture rotation checkpoint failed; prior files remain provisional.")
        self.chunk += 1
        self.raw = _Output(self.directory / f"raw-{self.chunk:06}.udp", "raw_udp_capture", self.records + 1)
        self.tlog = _Output(self.directory / f"telemetry-{self.chunk:06}.tlog", "app_mavlink_capture", self.records + 1)
        self.raw.write(RAW_MAGIC)
        self.total_bytes += len(RAW_MAGIC)

    def checkpoint(self):
        started = time.monotonic()
        for output in (self.raw, self.tlog, self.journal):
            if output is not None:
                output.checkpoint()
        self.durable_bytes = self.total_bytes
        self.checkpoint_ns = time.monotonic_ns()
        _atomic_json(self.directory / "status.json", self.status())
        elapsed = time.monotonic() - started
        if elapsed > .25:
            self.event("link_gap", reason="disk_checkpoint_stall", duration_s=elapsed,
                       coverage="possible_udp_loss")

    def _frame(self, frame: bytes, sender: tuple, mono_ns: int, utc_ns: int, raw_ref: dict, mavlink):
        parser = mavlink.MAVLink(None)
        try:
            message = parser.parse_char(frame)
        except Exception:
            self.invalid_frames += 1
            return
        if message is None or message.get_type() == "BAD_DATA" or message.get_type().startswith("UNKNOWN"):
            self.invalid_frames += 1
            return
        tlog_offset = self.tlog.bytes
        # The two low bits are a declared zero capture-link ID, not aircraft time.
        record = struct.pack(">Q", (utc_ns // 1000) & ~3) + frame
        if self.total_bytes + len(record) > self.config["max_capture_bytes"]:
            raise OSError("Capture storage budget reached; prior evidence preserved.")
        self.tlog.write(record)
        self.total_bytes += len(record)
        self.valid_frames += 1
        self.records += 1
        self.tlog.note_record(self.records)
        self.journal.note_record(self.records)
        for span in raw_ref.get("wire_spans", []):
            if span["chunk_id"] == self.raw.path.name:
                self.raw.note_record(self.records)
            else:
                artifact = self.artifacts_by_name[span["chunk_id"]]
                if artifact["record_end"] < artifact["record_start"]:
                    artifact["record_start"] = self.records
                artifact["record_end"] = max(artifact["record_end"], self.records)
        system, component = message.get_srcSystem(), message.get_srcComponent()
        key = f"{sender[0]}:{sender[1]}/{system}:{component}"
        if key not in self.sources:
            if len(self.sources) >= MAX_SOURCES:
                raise ValueError("More than 64 source identities; recording stopped without merging aircraft.")
            self.sources[key] = {"source_id": key, "sender": sender[0], "sender_port": sender[1],
                                 "system_id": system, "component_id": component, "boot_epoch": 0,
                                 "uid": None, "identity_state": "unverified", "messages": 0}
        source = self.sources[key]
        source["messages"] += 1
        source["last_received_monotonic_ns"] = str(mono_ns)
        raw = message.to_dict()
        boot_ms = raw.get("time_boot_ms")
        base = {"source_id": key, "sender": sender[0], "sender_port": sender[1],
                "system_id": system, "component_id": component, "record_index": self.records,
                "byte_offset": tlog_offset, "crc_valid": True,
                "signature_present": frame[0] == 0xFD and bool(frame[2] & 1), "signature_verified": False,
                "raw_ref": {**raw_ref, "tlog_chunk": self.tlog.path.name, "tlog_offset": tlog_offset,
                            "wire_bytes": len(frame)}, "boot_epoch": str(source["boot_epoch"]),
                "boot_time_us": str(boot_ms * 1000) if type(boot_ms) is int else None}
        if type(boot_ms) is int:
            previous = source.get("_boot_ms")
            if previous is not None and boot_ms < previous:
                source["boot_epoch"] += 1
                base["boot_epoch"] = str(source["boot_epoch"])
                source.pop("last_heartbeat_monotonic_ns", None)
                self.event("link_gap", mono_ns, utc_ns, **base,
                           reason="boot_time_regression_unclassified", coverage="unknown_boot_boundary")
            source["_boot_ms"] = boot_ms
            source["_boot_received_ns"] = mono_ns
        sequence = message.get_seq()
        previous_seq = source.get("_sequence")
        if previous_seq is not None and sequence != (previous_seq + 1) % 256:
            self.event("link_gap", mono_ns, utc_ns, **base, reason="sequence_discontinuity",
                       previous_sequence=previous_seq, sequence=sequence,
                       coverage="loss_reordering_or_duplicates_unclassified")
        source["_sequence"] = sequence
        # Raw/TLOG retain every validated source. Only explicitly selected source
        # IDs can produce normalized evidence used by the live orchestrator.
        if system != self.config["expected_system_id"] or component != self.config["expected_component_id"]:
            return
        kind = message.get_type()
        if kind == "HEARTBEAT":
            autopilot, vehicle = int(raw["autopilot"]), int(raw["type"])
            if autopilot == mavlink.MAV_AUTOPILOT_INVALID or vehicle == mavlink.MAV_TYPE_GCS:
                return
            armed = bool(int(raw["base_mode"]) & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            source.update(armed=armed, autopilot=autopilot, vehicle_type=vehicle,
                          last_heartbeat_monotonic_ns=str(mono_ns), _stale_reported=False)
            self.event("heartbeat", mono_ns, utc_ns, **base, armed=armed, autopilot=autopilot,
                       vehicle_type=vehicle)
        elif kind == "AUTOPILOT_VERSION":
            uid = str(raw.get("uid")) if raw.get("uid") else None
            uid2 = bytes(raw.get("uid2", [])).hex() if any(raw.get("uid2", [])) else None
            firmware = {key: raw.get(key) for key in ("flight_sw_version", "board_version", "vendor_id", "product_id")}
            # Pin the first observed identity for this recorder lifetime, even
            # between acquisitions and before catalog callbacks catch up.
            if source.get("_readback_uid") and uid != source["_readback_uid"]:
                source["readback_identity_conflict"] = True
            if uid and not source.get("_readback_uid"):
                source["_readback_uid"] = uid
            source.update(uid=uid, uid2=uid2, identity_state="observed_uid" if uid or uid2 else "uid_unavailable")
            source["_identity_received_ns"] = mono_ns
            self.event("identity", mono_ns, utc_ns, **base, uid=uid, uid2=uid2, firmware_identity=firmware)
        elif kind == "PARAM_VALUE":
            name = raw["param_id"]
            if isinstance(name, bytes):
                name = name.split(b"\0", 1)[0].decode("ascii", errors="replace")
            value = float(raw["param_value"])
            import math
            if math.isfinite(value):
                self.event("parameter", mono_ns, utc_ns, **base, name=str(name)[:64], value=str(value),
                           type=int(raw["param_type"]), index=int(raw["param_index"]), count=int(raw["param_count"]))

    def datagram(self, payload: bytes, sender: tuple, mono_ns: int, utc_ns: int, mavlink):
        self.packets += 1
        self.received_bytes += len(payload)
        self.last_packet_ns = mono_ns
        if sender[0] not in self.config["allowed_sources"]:
            self.rejected_senders += 1
            return
        if self.raw.bytes + len(payload) + RAW_HEADER.size > CHUNK_BYTES:
            self.rotate()
        offset = self.raw.bytes
        raw = RAW_HEADER.pack(len(payload), utc_ns, mono_ns, socket.inet_aton(sender[0]), sender[1]) + payload
        if self.total_bytes + len(raw) > self.config["max_capture_bytes"]:
            raise OSError("Capture storage budget reached; prior evidence preserved.")
        self.raw.write(raw)
        self.total_bytes += len(raw)
        raw_ref = {"chunk_id": self.raw.path.name, "byte_offset": offset, "datagram_bytes": len(payload)}
        if sender not in self.buffers:
            if len(self.buffers) >= MAX_SOURCES:
                raise ValueError("More than 64 UDP sender endpoints; refusing unbounded parser state.")
            self.buffers[sender] = {"data": bytearray(), "spans": []}
        assembly = self.buffers[sender]
        buffer = assembly["data"]
        if assembly["spans"] and mono_ns - int(assembly["spans"][0]["received_monotonic_ns"]) > 1_000_000_000:
            buffer.clear()
            assembly["spans"].clear()
            self.invalid_frames += 1
            self.event("link_gap", mono_ns, utc_ns, reason="incomplete_frame_expired", sender=sender[0])
        buffer.extend(payload)
        if payload:
            assembly["spans"].append({"chunk_id": self.raw.path.name,
                                      "byte_offset": offset + RAW_HEADER.size, "bytes": len(payload),
                                      "received_monotonic_ns": str(mono_ns)})

        def consume(count):
            del buffer[:count]
            spans = []
            while count and assembly["spans"]:
                span = assembly["spans"][0]
                take = min(count, span["bytes"])
                spans.append({**span, "bytes": take})
                span["bytes"] -= take
                span["byte_offset"] += take
                count -= take
                if not span["bytes"]:
                    assembly["spans"].pop(0)
            return spans

        while buffer:
            if buffer[0] not in (0xFD, 0xFE):
                positions = [index for magic in (b"\xfd", b"\xfe") if (index := buffer.find(magic)) >= 0]
                consume(min(positions) if positions else len(buffer))
                self.invalid_frames += 1
                continue
            if len(buffer) < (10 if buffer[0] == 0xFD else 6):
                break
            length = (12 if buffer[0] == 0xFD else 8) + buffer[1]
            if buffer[0] == 0xFD and buffer[2] & 1:
                length += 13
            if len(buffer) < length:
                break
            frame = bytes(buffer[:length])
            spans = consume(length)
            if len(spans) > 32:
                self.invalid_frames += 1
                self.event("link_gap", mono_ns, utc_ns, reason="excessive_datagram_fragmentation", sender=sender[0])
                continue
            self._frame(frame, sender, mono_ns, utc_ns, {**raw_ref, "wire_spans": spans}, mavlink)
        if len(buffer) > MAX_FRAME_BYTES:
            consume(len(buffer))
            self.invalid_frames += 1

    def _shared_request(self, mavlink):
        """Validate again at the actual socket owner, immediately before send."""
        connection = self.config.get("readback")
        path = self.directory / "readback-command.json"
        if not connection or not path.exists() or path.is_symlink():
            return
        try:
            command = _read_json(path, 4096)
            identity = str(UUID(command.get("id")))
        except (ValueError, TypeError, AttributeError, OSError):
            return  # invalid internal control metadata cannot interrupt capture
        if identity == self.last_readback_id:
            return
        self.last_readback_id = identity  # no replay, including an ambiguous send failure
        reply = {"id": identity, "sent": False}
        # Capture failures must propagate to recorder finalization. They must
        # never be caught as a mere readback failure after consuming a datagram.
        drained = 0
        while select.select([self.socket], [], [], 0)[0] and drained < 256:
            payload_in, sender = self.socket.recvfrom(65535)
            self.datagram(payload_in, sender, time.monotonic_ns(), time.time_ns(), mavlink)
            drained += 1
        try:
            if select.select([self.socket], [], [], 0)[0]:
                raise ValueError("Incoming recorder backlog prevents a current readback send decision.")
            now_ns = time.monotonic_ns()
            expires = int(command["expires_monotonic_ns"])
            if not now_ns < expires <= now_ns + 2_000_000_000:
                raise ValueError("Readback command expired; no packet sent.")
            payload = bytes.fromhex(command["payload"])
            if not 1 <= len(payload) <= MAX_FRAME_BYTES:
                raise ValueError("Readback command has invalid frame size.")
            parser = mavlink.MAVLink(None)
            messages = parser.parse_buffer(payload) or []
            if len(messages) != 1 or bytes(messages[0].get_msgbuf()) != payload:
                raise ValueError("Readback must contain exactly one valid MAVLink frame.")
            message = messages[0]
            from .acquisition import _validate_outgoing, _limits, _client_identity
            bootstrap = message.get_msgId() == 76
            _validate_outgoing(message, connection, bootstrap, check_source=True)
            if any((source["system_id"], source["component_id"]) == _client_identity(connection)
                   for source in self.sources.values()):
                raise ValueError("Incoming traffic conflicts with the configured app MAVLink identity; change the advanced app identity or router configuration.")
            if message.get_msgId() not in {20, 21, 76}:
                raise ValueError("Onboard log transfers require the observer to be stopped.")
            limits = _limits({key: command[key] for key in
                              ("heartbeat_timeout_s", "identity_timeout_s", "boot_timeout_s")})
            selected = [source for source in self.sources.values()
                        if source["system_id"] == connection["system_id"]
                        and source["component_id"] == connection["component_id"]]
            if len(selected) != 1:
                raise ValueError("Shared readback source is missing or ambiguous.")
            source = selected[0]
            now_ns = time.monotonic_ns()
            def fresh(field, seconds):
                stamp = source.get(field)
                return stamp is not None and 0 <= now_ns - int(stamp) <= seconds * 1e9
            if (source["sender"] != connection["remote_address"] or source["sender_port"] != connection["remote_port"]
                    or source.get("armed") is not False
                    or not fresh("last_heartbeat_monotonic_ns", limits["heartbeat_timeout_s"])):
                raise ValueError("Shared socket lacks a fresh selected disarmed heartbeat.")
            expected_uid = connection.get("expected_uid") or source.get("_readback_uid")
            if source.get("readback_identity_conflict") or source.get("uid") not in (None, expected_uid):
                raise ValueError("Shared socket reported a different vehicle UID.")
            if not bootstrap and (not expected_uid or str(source.get("uid")) != expected_uid
                    or not fresh("_identity_received_ns", limits["identity_timeout_s"])
                    or not fresh("_boot_received_ns", limits["boot_timeout_s"])):
                raise ValueError("Shared socket lacks fresh matching UID and boot-clock evidence.")
            if (self.readback_sends >= 50000 or time.monotonic() - self.last_readback_send < .005
                    or time.monotonic_ns() >= expires):
                raise ValueError("Readback send budget or deadline reached.")
            sent_ns, sent_utc_ns = time.monotonic_ns(), time.time_ns()
            self.socket.sendto(payload, (connection["remote_address"], connection["remote_port"]))
            self.readback_sends += 1
            self.last_readback_send = time.monotonic()
            reply.update(sent=True, sent_monotonic_ns=str(sent_ns), sent_utc_us=str(sent_utc_ns // 1000))
        except Exception as error:
            reply["reason"] = str(error)[:500]
        _atomic_json(self.directory / "readback-reply.json", reply)

    def run(self) -> int:
        from pymavlink.dialects.v20 import ardupilotmega as mavlink
        mavlink.MAVLINK_IGNORE_CRC = False
        try:
            self.journal = _Output(self.directory / "events.jsonl", "recorder_journal")
            self.event("coverage", action="starting", limits=LIMITATIONS)
            # An orphaned worker can briefly outlive its coordinator. A second
            # worker must not record elsewhere in the same catalog during that
            # window, even if no first status checkpoint exists yet.
            self.ownership = _exclusive_file_lock(self.directory.parent / "worker.lock")
            if _process_alive(self.parent_pid, self.parent_created) is not True:
                raise OSError("Coordinator ownership is unavailable; refusing capture startup.")
            if shutil.disk_usage(self.directory).free < self.config["reserve_bytes"] + 1024 ** 2:
                raise OSError("Insufficient free storage above the configured capture reserve.")
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 ** 2)
            self.actual_rcvbuf = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            self.socket.bind((self.config["bind_host"], self.config["bind_port"]))
            self.socket.settimeout(.02 if self.config.get("readback") else .2)
            self.rotate()
            self.state = "recording"
            self.event("coverage", action="recording", receive_only=not bool(self.config.get("readback")))
            self.checkpoint()
            while not (self.directory / "stop.json").exists():
                try:
                    payload, sender = self.socket.recvfrom(65535)
                    mono_ns, utc_ns = time.monotonic_ns(), time.time_ns()
                    self.datagram(payload, sender, mono_ns, utc_ns, mavlink)
                except socket.timeout:
                    pass
                self._shared_request(mavlink)
                now, wall = time.monotonic_ns(), time.time_ns()
                if now - self.last_loop_ns > 2_000_000_000:
                    self.event("link_gap", now, wall, reason="receiver_stall_or_host_sleep",
                               duration_s=(now - self.last_loop_ns) / 1e9, coverage="unknown")
                offset = wall - now
                if abs(offset - self.last_clock_offset) > 2_000_000_000:
                    self.event("link_gap", now, wall, reason="utc_clock_jump", coverage="utc_mapping_uncertain")
                self.last_loop_ns, self.last_clock_offset = now, offset
                for source in self.sources.values():
                    stamp = source.get("last_heartbeat_monotonic_ns")
                    if stamp and now - int(stamp) > HEARTBEAT_STALE_SECONDS * 1e9 and not source.get("_stale_reported"):
                        source["_stale_reported"] = True
                        self.event("link_gap", now, wall, source_id=source["source_id"],
                                   reason="heartbeat_stale", armed_state="unknown", coverage="heartbeat_gap")
                if self.total_bytes >= self.config["max_capture_bytes"]:
                    raise OSError("Capture storage budget reached; prior evidence preserved.")
                if now - (self.checkpoint_ns or 0) >= CHECKPOINT_SECONDS * 1e9:
                    if _process_alive(self.parent_pid, self.parent_created) is not True:
                        raise OSError("Coordinator ownership lost; capture interrupted and requires explicit restart.")
                    if shutil.disk_usage(self.directory).free < self.config["reserve_bytes"]:
                        raise OSError("Storage reserve reached; capture interrupted without deleting prior evidence.")
                    self.checkpoint()
            self.state = "stopped"
            stop = _read_json(self.directory / "stop.json")
            self.event("coverage", action="stopped", reason=str(stop.get("reason", "operator"))[:200])
        except Exception as error:
            self.state = "failed"
            self.failure = {"type": type(error).__name__, "message": str(error)[:1000]}
            try:
                if self.journal:
                    self.event("recorder_error", failure=self.failure, coverage="interrupted")
            except OSError:
                pass
        finally:
            if self.socket:
                self.socket.close()
                self.socket = None
            for output in (self.raw, self.tlog, self.journal):
                if output is not None:
                    artifact = output.finish(self.state == "stopped")
                    self.artifacts.append(artifact)
                    if artifact["checkpoint_error"]:
                        self.failure = self.failure or {"message": "Final checkpoint failed; files remain provisional."}
                        self.state = "failed"
            try:
                _atomic_json(self.directory / "artifacts.json", {"artifacts": self.artifacts})
                self.durable_bytes = sum(item["durable_bytes"] for item in self.artifacts)
                self.checkpoint_ns = time.monotonic_ns()
                _atomic_json(self.directory / "status.json", self.status())
            except OSError:
                pass  # Parent reports stale/failed state; raw files are never deleted.
            if self.ownership:
                _unlock_file(self.ownership)
                self.ownership = None
        return 0 if self.state == "stopped" else 1


class RecorderService:
    """A bounded journal reader supervises capture without sharing the worker GIL."""
    def __init__(self, data_dir: Path, on_event=None):
        self.root = Path(data_dir).resolve() / "captures"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("Capture storage must not be a symbolic link.")
        if os.name != "nt":
            os.chmod(self.root, 0o700)
        self.on_event = on_event
        self.process = None
        self.directory = None
        self.capture_id = None
        self.reader_offset = 0
        self.callback_error = None
        self._lock = threading.RLock()
        self._event_lock = threading.Lock()
        self._reader_done = threading.Event()
        self._reader = None
        self._stderr = None
        self._owns_reservation = False
        self._reservation = None
        self._dispatch_started_ns = None
        self._shared_lock = threading.Lock()

    def matches_shared_connection(self, connection):
        from .acquisition import _same_connection
        current = self.status()
        return (connection.get("transport") == "shared_recorder" and current.get("active")
                and _same_connection(current.get("shared_readback"), connection))

    def shared_bootstrap_ready(self, connection):
        return shared_bootstrap_ready(self.status(), connection)

    def open_shared_transport(self, connection, limits):
        """Constructor-owned callback, never a renderer-supplied transport."""
        current = self.status()
        if not current.get("active"):
            # After an explicit stop the same app endpoint can be owned directly
            # by the existing bounded log/readback adapter.
            return None
        if not self.matches_shared_connection(connection):
            raise ValueError("Shared readback does not match the active recorder connection.")
        if not self._shared_lock.acquire(blocking=False):
            raise ValueError("Another acquisition owns the shared recorder readback channel.")
        try:
            from .shared_transport import RecorderTransport
            return RecorderTransport(self, connection, limits, self._shared_lock.release)
        except BaseException:
            self._shared_lock.release()
            raise

    def _claim(self, capture_id):
        # A lifetime OS lock closes startup races between independent app
        # instances and is automatically released on coordinator failure.
        try:
            stream = _exclusive_file_lock(self.root / "capture.lock")
        except OSError as error:
            raise ValueError("Another recorder coordinator owns this catalog's capture reservation.") from error
        try:
            _atomic_json(self.root / "owner.json", {"capture_session_id": capture_id, "pid": os.getpid(),
                                                   "created": psutil.Process().create_time()})
        except BaseException:
            _unlock_file(stream)
            raise
        self._reservation, self._owns_reservation = stream, True

    def _release(self):
        if not self._owns_reservation:
            return
        try:
            (self.root / "owner.json").unlink(missing_ok=True)
        finally:
            _unlock_file(self._reservation)
            self._reservation, self._owns_reservation = None, False

    def start(self, capture_session_id: str, config: dict) -> dict:
        capture_session_id = str(UUID(capture_session_id))
        config = validate_config(config)
        if self.directory is not None:
            if (self.process is not None and self.process.poll() is None
                    and self.status()["state"] not in {"stopped", "failed"}):
                raise ValueError("A recorder already owns its configured UDP endpoint; stop it explicitly first.")
            self.stop("previous_capture_finalized")
            if self._reader and self._reader.is_alive():
                raise ValueError("The prior recorder event reader is still finishing; retry after it exits.")
        if any(item.get("active") for item in self.recovered()):
            raise ValueError("Another recorder still owns this catalog's capture. Use its owning application to stop it first.")
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                raise ValueError("A recorder already owns its configured UDP endpoint; stop it explicitly first.")
            self.capture_id = capture_session_id
            self._claim(capture_session_id)
            self.directory = self.root / capture_session_id
            try:
                self.directory.mkdir(mode=0o700)
            except BaseException:
                self._release()
                self.directory = None
                raise
            try:
                self.capture_id, self.reader_offset, self.callback_error = capture_session_id, 0, None
                self._reader_done = threading.Event()
                _atomic_json(self.directory / "config.json", {"capture_session_id": capture_session_id,
                             "config": config, "parent_pid": os.getpid(),
                             "parent_created": psutil.Process().create_time()})
                command = ([sys.executable, "--recorder-worker", str(self.directory)] if getattr(sys, "frozen", False)
                           else [sys.executable, "-m", "q22_engine.recorder", "--worker", str(self.directory)])
                self._stderr = (self.directory / "worker-stderr.log").open("wb")
                os.chmod(self.directory / "worker-stderr.log", 0o600)
                self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=self._stderr, close_fds=True)
            except BaseException:
                if self._stderr:
                    self._stderr.close()
                    self._stderr = None
                self._release()
                self.directory = None
                raise
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = self.status()
            if current["state"] not in {"starting"}:
                break
            time.sleep(.025)
        else:
            self.stop("startup_timeout")
            raise ValueError("Recorder did not start within ten seconds; prior evidence remains inspectable.")
        if self.on_event:
            self._reader = threading.Thread(target=self._read_events, name="q22-recorder-journal", daemon=True)
            self._reader.start()
        return self.status()

    def _read_events(self):
        while not self._reader_done.is_set():
            try:
                self.drain_events(100)
                self.callback_error = None
            except Exception as error:
                self.callback_error = str(error)[:500]
            self._reader_done.wait(.05)

    def synchronize_events(self, timeout=2.0):
        """Catch up to a fixed journal boundary without waiting on future traffic."""
        if self.directory is None or self.on_event is None:
            return False
        target = (self.directory / "events.jsonl").stat().st_size
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_offset >= target:
                return self.callback_error is None
            self.drain_events(100)
            time.sleep(.005)
        return False

    def drain_events(self, limit=100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Recorder event limit must be 1..100.")
        if not self._event_lock.acquire(blocking=False):
            return []
        try:
            with self._lock:
                directory, capture_id, offset = self.directory, self.capture_id, self.reader_offset
            if directory is None or not (directory / "events.jsonl").exists():
                return []
            result = []
            with (directory / "events.jsonl").open("rb") as stream:
                stream.seek(offset)
                for _ in range(limit):
                    line = stream.readline(16 * 1024 + 1)
                    if len(line) > 16 * 1024:
                        raise ValueError("Recorder journal frame exceeded 16 KiB.")
                    if not line or not line.endswith(b"\n"):
                        break
                    event = json.loads(line)
                    if self.on_event:
                        self._dispatch_started_ns = time.monotonic_ns()
                        try:
                            self.on_event(capture_id, event)
                        finally:
                            self._dispatch_started_ns = None
                    result.append(event)
                    with self._lock:
                        if self.directory != directory:
                            raise RuntimeError("Recorder changed while its event reader was active.")
                        self.reader_offset = stream.tell()
            return result
        finally:
            self._event_lock.release()

    def status(self) -> dict:
        with self._lock:
            if self.directory is None:
                return {"state": "idle", "active": False, "bindings": [], "owns_udp": False, "limits": LIMITATIONS}
            path = self.directory / "status.json"
            if path.exists():
                value = _read_json(path)
            else:
                config = _read_json(self.directory / "config.json")["config"]
                value = {"state": "starting", "active": True, "capture_session_id": self.capture_id,
                         "bindings": [{"local_address": config["bind_host"], "port": config["bind_port"]}],
                         "owns_udp": False, "limits": LIMITATIONS}
            if self.process is not None and self.process.poll() is not None and value["state"] not in {"stopped", "failed"}:
                value.update(state="interrupted", active=False, owns_udp=False,
                             failure={"message": "Recorder process exited before a final checkpoint; retained files are provisional."})
            stamp = value.get("status_monotonic_ns")
            age = (time.monotonic_ns() - int(stamp)) / 1e9 if stamp else None
            backlog = max(0, (self.directory / "events.jsonl").stat().st_size - self.reader_offset) if (self.directory / "events.jsonl").exists() else 0
            dispatch_age = (time.monotonic_ns() - self._dispatch_started_ns) / 1e9 if self._dispatch_started_ns else 0
            value.update(status_age_s=age, event_backlog_bytes=backlog, callback_error=self.callback_error,
                         callback_duration_s=dispatch_age)
            # Recompute receipt freshness at query time; a frozen status file is
            # never allowed to keep a disarmed badge fresh.
            for source in value.get("sources", []):
                received = source.get("last_heartbeat_monotonic_ns")
                heartbeat_age = (time.monotonic_ns() - int(received)) / 1e9 if received else None
                source["heartbeat_age_s"] = heartbeat_age
                if (value["state"] != "recording" or heartbeat_age is None or heartbeat_age > HEARTBEAT_STALE_SECONDS or age is None
                        or age > 2.5 or self.callback_error or backlog > 256 * 1024 or dispatch_age > 2.5):
                    source["armed_state"] = "unknown"
            return value

    def stop(self, reason="operator") -> dict:
        with self._lock:
            if self.directory is None:
                return self.status()
            _atomic_json(self.directory / "stop.json", {"reason": str(reason)[:200], "requested_at": _utc()})
            process = self.process
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # A frozen one-file executable has a launcher and Python child.
                # Stop verified descendants before the launcher can orphan them.
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(children, timeout=1)
                for child in alive:
                    child.kill()
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        self._reader_done.set()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        if self.on_event and not (self._reader and self._reader.is_alive()):
            # Finish a bounded batch; a remaining journal is explicitly reported.
            try:
                self.drain_events(100)
            except Exception as error:
                self.callback_error = str(error)[:500]
        if self._stderr:
            self._stderr.close()
            self._stderr = None
        result = self.status()
        artifacts = self.directory / "artifacts.json"
        result["artifacts"] = _read_json(artifacts)["artifacts"] if artifacts.exists() else []
        result["capture_directory"] = str(self.directory)
        result["event_delivery_complete"] = result.get("event_backlog_bytes", 0) == 0 and not self.callback_error
        if not result["event_delivery_complete"]:
            result["event_delivery_gap"] = "Recorder journal is preserved but coordinator events remain pending; drain/reconcile before claiming complete interval evidence."
        if not (self._reader and self._reader.is_alive()):
            self._release()
        return result

    def recovered(self, limit=100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Recovery list limit must be 1..100.")
        result = []
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name, reverse=True):
            if len(result) >= limit:
                break
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                config = _read_json(directory / "config.json")
                value = _read_json(directory / "status.json") if (directory / "status.json").exists() else {"state": "interrupted"}
            except (OSError, ValueError, KeyError):
                continue
            if value["state"] in {"starting", "recording", "stopping"}:
                alive = _process_alive(value.get("worker_pid"), value.get("worker_created"))
                value.update(state="external_recorder_active" if alive is not False else "interrupted",
                             active=alive is not False, recovered=True,
                             recovery="Review saved evidence; interrupted capture is never restarted automatically.")
            value.update(capture_session_id=config["capture_session_id"], capture_directory=str(directory))
            result.append(value)
        return result

    def close(self):
        return self.stop("coordinator_shutdown")


def worker_main(arguments=None) -> None:
    parser = argparse.ArgumentParser(description="Internal passive recorder worker")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(arguments)
    raise SystemExit(_Worker(args.directory.resolve()).run())


if __name__ == "__main__":
    worker_main(sys.argv[2:] if sys.argv[1:2] == ["--worker"] else sys.argv[1:])
