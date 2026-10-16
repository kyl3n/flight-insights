"""Internal readback access to the recorder's single UDP socket.

Incoming bytes are tailed from the recorder's append-only raw capture, retaining
their original receipt clocks. Small atomic command/receipt files carry outgoing
requests to the owning worker. There is no second network listener, socket reuse,
or GCS-specific integration. The worker independently validates each send.
"""
from __future__ import annotations

import socket
import os
import sys
import time
from uuid import uuid4


class RecorderTransport:
    def __init__(self, recorder, connection, limits, release):
        from .recorder import RAW_HEADER, RAW_MAGIC
        self.recorder, self.connection, self.limits = recorder, connection, limits
        self.directory, self.release = recorder.directory, release
        self.header, self.magic = RAW_HEADER, RAW_MAGIC
        self.started_ns = time.monotonic_ns()
        self.timeout = .05
        self.closed = False
        self.cached = None
        self.receipt = None
        chunks = sorted(self.directory.glob("raw-*.udp"))
        self.chunk = int(chunks[-1].stem.split("-")[1]) if chunks else 1
        self.offset = len(self.magic)
        self.last_check = 0.0

    def settimeout(self, timeout):
        self.timeout = timeout

    def _check(self):
        if self.closed:
            raise OSError("Shared recorder transport is closed.")
        if time.monotonic() - self.last_check >= .05:
            self.last_check = time.monotonic()
            status = self.recorder.status()
            if (self.recorder.directory != self.directory or status.get("state") != "recording"
                    or not status.get("active") or status.get("status_age_s", 99) > 2.5):
                raise OSError("The shared recorder socket is no longer available.")

    def _next(self):
        self._check()
        # At most one complete capture chunk of old data can precede our start.
        # Read from record boundaries, never from a file-size sample taken during
        # a partial write. A partially written final record is retried later.
        inspected = 0
        while inspected <= 32 * 1024 * 1024:
            path = self.directory / f"raw-{self.chunk:06}.udp"
            if path.is_symlink():
                raise OSError("Shared capture must not be a symbolic link.")
            if not path.exists():
                return None
            with path.open("rb") as stream:
                magic = stream.read(len(self.magic))
                if len(magic) < len(self.magic):
                    return None
                if magic != self.magic:
                    raise OSError("Shared capture header is invalid.")
                stream.seek(self.offset)
                while True:
                    header = stream.read(self.header.size)
                    if len(header) != self.header.size:
                        break
                    size, utc_ns, mono_ns, host, port = self.header.unpack(header)
                    if size > 65535:
                        raise OSError("Shared capture datagram exceeds its size bound.")
                    payload = stream.read(size)
                    if len(payload) != size:
                        break
                    self.offset += self.header.size + size
                    inspected += self.header.size + size
                    if mono_ns >= self.started_ns:
                        return payload, (socket.inet_ntoa(host), port), mono_ns, utc_ns // 1000
            following = self.directory / f"raw-{self.chunk + 1:06}.udp"
            if not following.exists():
                return None
            # Rotation only creates the next file after sealing this one.
            if path.stat().st_size != self.offset:
                raise OSError("Shared capture ended with an incomplete datagram.")
            self.chunk += 1
            self.offset = len(self.magic)
        raise OSError("Shared acquisition capture backlog exceeded its bounded read window.")

    def has_pending(self):
        if self.cached is None:
            self.cached = self._next()
        return self.cached is not None

    def recvfrom(self, maximum):
        deadline = time.monotonic() + self.timeout
        while True:
            if self.has_pending():
                payload, endpoint, mono_ns, utc_us = self.cached
                self.cached = None
                if len(payload) > maximum:
                    raise OSError("Shared datagram would be truncated.")
                self.receipt = mono_ns, utc_us
                return payload, endpoint
            if time.monotonic() >= deadline:
                raise socket.timeout()
            time.sleep(min(.005, max(0, deadline - time.monotonic())))

    def sendto(self, payload, endpoint):
        from .recorder import _atomic_json, _read_json
        self._check()
        if endpoint != (self.connection["remote_address"], self.connection["remote_port"]):
            raise ValueError("Shared readback cannot change its selected remote endpoint.")
        if len(payload) > 280:
            raise ValueError("Shared readback requires one bounded MAVLink request.")
        request_id = str(uuid4())
        command = self.directory / "readback-command.json"
        reply = self.directory / "readback-reply.json"
        deadline = time.monotonic() + 1.0
        _atomic_json(command, {"id": request_id, "payload": payload.hex(),
            "expires_monotonic_ns": str(int(deadline * 1e9)),
            "heartbeat_timeout_s": self.limits["heartbeat_timeout_s"],
            "identity_timeout_s": self.limits["identity_timeout_s"],
            "boot_timeout_s": self.limits["boot_timeout_s"]})
        try:
            while time.monotonic() < deadline:
                self._check()
                if reply.is_file() and not reply.is_symlink():
                    try:
                        receipt = _read_json(reply, 4096)
                    except OSError as error:
                        # Atomic replacement can briefly deny a Windows reader
                        # access to the destination. Keep waiting for this exact
                        # receipt within the original deadline. Never resend the
                        # MAVLink request or treat an unreadable receipt as sent.
                        if sys.platform != "win32" or getattr(error, "winerror", None) not in (5, 32, 33):
                            raise
                        time.sleep(.005)
                        continue
                    if receipt.get("id") == request_id:
                        if not receipt.get("sent"):
                            raise OSError(receipt.get("reason", "Recorder withheld shared readback request."))
                        return len(payload)
                time.sleep(.005)
            raise OSError("Recorder did not acknowledge the readback request; no retry was sent.")
        finally:
            cleanup_deadline = time.monotonic() + .5
            while True:
                try:
                    command.unlink(missing_ok=True)
                    break
                except OSError as error:
                    if (os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33)
                            or time.monotonic() >= cleanup_deadline):
                        raise
                    time.sleep(.01)

    def close(self):
        if not self.closed:
            self.closed = True
            self.release()
