"""Explicit bounded SkyPulse HTTP history; originals only, no RF normalization.

No capture resumes at construction, list or detail. HTTP entity bytes and failed
attempts are distinct evidence; receipt times do not date vendor history samples.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import math
import os
import re
from pathlib import Path
import socket
import tempfile
import time
from uuid import UUID, uuid4

import psutil

from .network import _integer, _source
from .recorder import _replace_checkpoint

PATHS = ("/scatter_data.json", "/events.json")
BODY_LIMIT = 1024 * 1024
TOTAL_BODY_LIMIT = 32 * 1024 * 1024
HEADER_LIMIT = 65536
MANIFEST_LIMIT = 4 * 1024 * 1024
LIMITATIONS = [
    "Raw replies are not vendor-schema validation, RF-health analysis or deduplicated radio samples.",
    "UTC and monotonic times describe request/receipt at this computer, not the time of each point in the radio's rolling history.",
    "HTTP failure, malformed JSON and missed polling cycles do not prove an RF failure or a firewall block.",
    "Only the explicitly selected radio was polled; access to a ground radio does not establish access to its air unit or camera.",
    "No radio setting, learned MAVLink destination, aircraft parameter or host network setting is changed.",
]


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _http_get(source, path, timeout_s, cancel_check):
    """Bound headers/body/time, including slow drip; retain partial body bytes."""
    start = time.monotonic()
    deadline = start + timeout_s
    wire = bytearray()
    failure = None
    response_complete = False
    try:
        if cancel_check():
            raise CancelledError()
        with socket.create_connection((source["host"], source["port"]), timeout=max(.001, timeout_s)) as connection:
            connection.settimeout(max(.001, deadline - time.monotonic()))
            connection.sendall(f"GET {path} HTTP/1.1\r\nHost: {source['host']}:{source['port']}\r\nAccept: application/json\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n".encode("ascii"))
            target_length = None
            while len(wire) <= BODY_LIMIT + HEADER_LIMIT:
                if cancel_check():
                    raise CancelledError()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP receipt deadline exceeded.")
                connection.settimeout(min(.2, remaining))
                try:
                    chunk = connection.recv(min(65536, BODY_LIMIT + HEADER_LIMIT + 1 - len(wire)))
                except socket.timeout:
                    continue
                if not chunk:
                    response_complete = True
                    break
                wire.extend(chunk)
                header_end = wire.find(b"\r\n\r\n")
                if header_end < 0 and len(wire) > HEADER_LIMIT or header_end > HEADER_LIMIT:
                    raise ValueError("HTTP headers exceeded 64 KiB.")
                if header_end >= 0 and target_length is None:
                    header_rows = bytes(wire[:header_end]).split(b"\r\n")
                    lengths = [row.split(b":", 1)[1].strip() for row in header_rows if row.lower().startswith(b"content-length:")]
                    chunked = any(row.lower().startswith(b"transfer-encoding:") for row in header_rows)
                    if lengths and not chunked:
                        if len(lengths) != 1 or not lengths[0].isdigit():
                            raise ValueError("HTTP Content-Length is ambiguous.")
                        if int(lengths[0]) > BODY_LIMIT:
                            raise ValueError("HTTP entity exceeds the 1 MiB body limit.")
                        target_length = header_end + 4 + int(lengths[0])
                if target_length is not None and len(wire) >= target_length:
                    response_complete = True
                    break
            if len(wire) > BODY_LIMIT + HEADER_LIMIT:
                raise ValueError("HTTP response exceeded the bounded wire limit.")
    except CancelledError:
        failure = "capture_cancelled"
    except (OSError, ValueError, http.client.HTTPException) as error:
        failure = str(error)[:500] or type(error).__name__
    body = b""
    status = None
    body_representation = "http_entity"
    content_type = content_encoding = None
    header_end = wire.find(b"\r\n\r\n")
    if 0 <= header_end <= HEADER_LIMIT:
        # Header values beyond this allowlist (cookies, authentication challenges,
        # credentials) never enter logs or manifests. No credentials are sent.
        class MemorySocket:
            def makefile(self, *_args, **_kwargs):
                return io.BytesIO(wire)
        reply = http.client.HTTPResponse(MemorySocket())
        try:
            reply.begin()
            status = reply.status
            content_type = (reply.getheader("Content-Type") or "")[:180]
            content_encoding = (reply.getheader("Content-Encoding") or "identity")[:80]
            body = reply.read(BODY_LIMIT + 1)
            if reply.length not in {None, 0}:
                response_complete = False
                failure = failure or "Incomplete HTTP entity; partial bytes retained."
            if len(body) > BODY_LIMIT:
                body = body[:BODY_LIMIT]
                failure = "HTTP entity exceeded the 1 MiB body limit; partial bytes retained."
                response_complete = False
        except http.client.IncompleteRead as error:
            body = error.partial[:BODY_LIMIT]
            response_complete = False
            failure = failure or "Incomplete HTTP entity; partial bytes retained."
        except (ValueError, OSError, http.client.HTTPException) as error:
            # No undecodable HTTP headers are archived. If body bytes arrived,
            # retain their transfer representation without calling them JSON.
            body = bytes(wire[header_end + 4:header_end + 4 + BODY_LIMIT])
            body_representation = "partial_http_transfer_body"
            response_complete = False
            failure = failure or "Malformed HTTP response framing: " + type(error).__name__
        finally:
            reply.close()
    else:
        response_complete = False
        failure = failure or "No complete bounded HTTP headers were received."
    if failure:
        response_complete = False
    valid_json = False
    json_parse_error = None
    if response_complete and body_representation == "http_entity" and content_encoding in {None, "identity", ""}:
        try:
            json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-JSON constant")))
            valid_json = True
        except RecursionError:
            json_parse_error = "parser_depth_limit"
        except (ValueError, UnicodeError):
            json_parse_error = "invalid_json_syntax_or_encoding"
    return {"http_status": status, "complete_response": response_complete, "body": body,
            "body_representation": body_representation, "content_type": content_type,
            "content_encoding": content_encoding, "json_valid": valid_json, "json_parse_error": json_parse_error,
            "error": failure, "cancelled": failure == "capture_cancelled"}


class RadioHistoryService:
    def __init__(self, store, *, fetcher=None):
        self.store = store
        self.root = store.root / "radio-history"
        if self.root.is_symlink():
            raise ValueError("Radio history storage must not be a symbolic link.")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fetcher = fetcher or _http_get
        self.owner = {"pid": os.getpid(), "created": psutil.Process().create_time(), "instance_id": str(uuid4())}
        self.active = set()

    @staticmethod
    def _id(value):
        if not isinstance(value, str):
            raise ValueError("A radio capture_id is required.")
        try:
            canonical = str(UUID(value))
        except ValueError as error:
            raise ValueError("Invalid radio capture_id.") from error
        if canonical != value:
            raise ValueError("Radio capture_id must be a canonical UUID.")
        return canonical

    def _directory(self, capture_id):
        directory = self.root / self._id(capture_id)
        if self.root.is_symlink() or directory.is_symlink():
            raise ValueError("Radio history paths must not be symbolic links.")
        return directory

    def _save(self, record):
        directory = self._directory(record["capture_id"])
        payload = json.dumps(record, allow_nan=False, ensure_ascii=True, separators=(",", ":")).encode()
        if len(payload) > MANIFEST_LIMIT:
            raise ValueError("Radio capture manifest exceeded its limit; existing evidence remains on disk.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".manifest-", delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_checkpoint(temporary, directory / "manifest.json")
            if os.name != "nt":
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            # A failed flush must close the temporary before Windows can unlink
            # it. Preserve the prior checkpoint through bounded replace retries.
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read(self, capture_id, *, detail=False):
        path = self._directory(capture_id) / "manifest.json"
        if path.is_symlink():
            raise ValueError("Radio manifest must not be a symbolic link.")
        with path.open("rb") as stream:
            payload = stream.read(MANIFEST_LIMIT + 1)
        if len(payload) > MANIFEST_LIMIT:
            raise ValueError("Radio capture manifest exceeds its size limit.")
        try:
            record = json.loads(payload)
        except (ValueError, RecursionError) as error:
            raise ValueError("Radio capture manifest is not supported bounded JSON.") from error
        if not isinstance(record, dict) or record.get("version") != 1 or record.get("capture_id") != capture_id:
            raise ValueError("Radio capture manifest identity or version is invalid.")
        if record.get("state") == "running":
            owner = record.get("owner", {})
            try:
                process = psutil.Process(int(owner.get("pid")))
                alive = abs(process.create_time() - float(owner.get("created"))) < .01 and process.status() != psutil.STATUS_ZOMBIE
            except (psutil.NoSuchProcess, ValueError, TypeError):
                alive = False
            except psutil.AccessDenied:
                alive = None
            if owner == self.owner and capture_id not in self.active:
                alive = False
            if alive is not True:
                record["state"] = "interrupted" if alive is False else "unknown"
                record["recovery"] = "Retained replies are partial evidence. Capture is never resumed automatically; start a new explicit capture if needed."
        if detail:
            indexed = {poll.get("body_source", {}).get("original_name") for poll in record.get("polls", []) if isinstance(poll, dict)}
            retained = []
            for original in self._directory(capture_id).iterdir():
                if re.fullmatch(r"body-\d{6}-[01]\.bin", original.name) and original.name not in indexed and original.is_file() and not original.is_symlink():
                    retained.append({"name": original.name, "path": str(original), "byte_size": original.stat().st_size,
                                     "state": "preserved_not_associated"})
            record["unindexed_bodies"] = retained[:32]
            record["unindexed_body_count"] = len(retained)
        return record

    @staticmethod
    def _summary(record):
        return {key: value for key, value in record.items() if key not in {"polls", "gaps", "owner"}}

    def dispatch(self, method, params, *, cancel_check=None, progress=None):
        if not isinstance(params, dict):
            raise ValueError("Radio parameters must be an object.")
        if method == "radio.capture":
            return self.capture(params, cancel_check=cancel_check, progress=progress)
        if method == "radio.detail":
            if set(params) != {"capture_id"}:
                raise ValueError("Radio detail requires only capture_id.")
            return {"capture": self._read(self._id(params["capture_id"]), detail=True)}
        if method == "radio.list":
            if set(params) - {"limit", "offset"}:
                raise ValueError("Radio list only accepts limit and offset.")
            limit = _integer(params.get("limit", 25), 1, 100, "List limit")
            offset = _integer(params.get("offset", 0), 0, 100000, "List offset")
            records, errors = [], []
            for directory in self.root.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    continue
                try:
                    records.append(self._read(self._id(directory.name)))
                except (OSError, ValueError) as error:
                    errors.append({"capture_id": directory.name[:100], "error": str(error)[:300]})
                if len(records) + len(errors) >= 1000:
                    break
            records.sort(key=lambda item: item.get("started_utc", ""), reverse=True)
            return {"captures": [self._summary(item) for item in records[offset:offset + limit]],
                    "next_offset": offset + limit if len(records) > offset + limit else None,
                    "errors": errors[:25], "scan_limit": 1000, "scan_truncated": len(records) + len(errors) >= 1000, "limitations": LIMITATIONS}
        raise ValueError("Unknown radio method.")

    def capture(self, params, *, cancel_check=None, progress=None):
        if set(params) - {"source", "duration_s", "interval_ms", "timeout_ms"}:
            raise ValueError("Radio capture accepts only source, duration_s, interval_ms and timeout_ms.")
        supplied = params.get("source")
        if not isinstance(supplied, dict) or set(supplied) - {"id", "kind", "host", "port", "interface"}:
            raise ValueError("Select a SkyPulse source with only id, kind, host, port and optional interface; credentials and custom paths are not accepted.")
        try:
            source = _source(supplied)
        except (ValueError, TypeError) as error:
            raise ValueError("Radio source requires a numeric unicast IPv4 address, a valid port and kind skypulse; URL credentials and custom HTTP fields are unsupported.") from error
        if source["kind"] != "skypulse":
            raise ValueError("Radio capture requires an explicitly configured SkyPulse source.")
        source = {key: source[key] for key in ("id", "kind", "host", "port")}
        duration = _integer(params.get("duration_s", 30), 1, 300, "Capture duration")
        interval = _integer(params.get("interval_ms", 5000), 1000, 30000, "Poll interval") / 1000
        timeout = _integer(params.get("timeout_ms", 1500), 100, 5000, "HTTP timeout") / 1000
        cancelled = cancel_check or (lambda: False)
        capture_id = str(uuid4())
        directory = self._directory(capture_id)
        directory.mkdir(mode=0o700)
        started = time.monotonic()
        record = {"version": 1, "capture_id": capture_id, "source": source, "state": "running", "owner": self.owner,
                  "started_utc": _utc(), "started_monotonic_ns": str(time.monotonic_ns()),
                  "duration_s": duration, "interval_ms": int(interval * 1000), "timeout_ms": int(timeout * 1000),
                  "polls": [], "gaps": [], "poll_count": 0, "failure_count": 0, "body_bytes": 0,
                  "missed_poll_cycles": 0, "normalization": "raw_only_schema_unvalidated", "limitations": LIMITATIONS}
        self.active.add(capture_id)
        try:
            self._save(record)
        except BaseException:
            self.active.discard(capture_id)
            raise
        deadline, next_due, cycle = started + duration, started, 0
        try:
            while time.monotonic() < deadline and record["state"] == "running":
                if cancelled():
                    raise CancelledError("Radio capture cancelled; existing raw replies remain archived.")
                if time.monotonic() < next_due:
                    time.sleep(min(.1, next_due - time.monotonic(), max(0, deadline - time.monotonic())))
                    continue
                cycle += 1
                for slot, endpoint in enumerate(PATHS):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        record["gaps"].append({"cycle": cycle, "endpoint": endpoint, "reason": "capture_deadline_before_request"})
                        break
                    if cancelled():
                        raise CancelledError("Radio capture cancelled; existing raw replies remain archived.")
                    poll = {"cycle": cycle, "endpoint": endpoint, "started_utc": _utc(),
                            "started_monotonic_ns": str(time.monotonic_ns())}
                    record["pending_poll"] = dict(poll)
                    self._save(record)
                    reply = self.fetcher(source, endpoint, min(timeout, remaining), cancelled)
                    body = reply.pop("body")
                    if not isinstance(body, bytes) or len(body) > BODY_LIMIT:
                        raise ValueError("HTTP adapter exceeded its bounded entity contract.")
                    available = TOTAL_BODY_LIMIT - record["body_bytes"]
                    if len(body) > available:
                        body = body[:available]
                        reply.update(complete_response=False, json_valid=False, error="Capture total body budget reached; partial bytes retained.")
                    poll.update(reply, received_utc=_utc(), received_monotonic_ns=str(time.monotonic_ns()),
                                body_bytes=len(body), response_sha256=hashlib.sha256(body).hexdigest() if body or reply["complete_response"] else None,
                                schema_validated=False, sample_times_validated=False)
                    poll["state"] = "json_response" if reply["complete_response"] and reply["http_status"] == 200 and reply["json_valid"] else "http_error" if reply["complete_response"] and reply["http_status"] != 200 else ("json_unvalidated" if reply.get("json_parse_error") == "parser_depth_limit" else "unparsed_body" if reply.get("content_encoding") not in {None, "identity", ""} else "malformed_json") if reply["complete_response"] else "transport_incomplete"
                    # Preserve even an empty complete HTTP entity. Cancellation
                    # stops requests but does not discard the already received body.
                    if body or reply["complete_response"]:
                        original = directory / f"body-{cycle:06d}-{slot}.bin"
                        fd = os.open(original, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(body)
                            stream.flush()
                            os.fsync(stream.fileno())
                        poll["body_source"] = self.store.archive_file(original, acquisition_method="app_owned_readonly_radio_capture")
                        if poll["body_source"]["sha256"] != poll["response_sha256"]:
                            raise ValueError("Radio response bytes changed before archival; response association withheld.")
                    record["body_bytes"] += len(body)
                    record["poll_count"] += 1
                    record["failure_count"] += poll["state"] != "json_response"
                    record["polls"].append(poll)
                    record.pop("pending_poll", None)
                    self._save(record)
                    if progress:
                        progress("radio_capture", min(duration * 1000, int((time.monotonic() - started) * 1000)), duration * 1000)
                    if reply.get("cancelled") or cancelled():
                        raise CancelledError("Radio capture cancelled; prior and partial replies remain archived.")
                    if record["body_bytes"] >= TOTAL_BODY_LIMIT:
                        record["state"] = "budget_exhausted"
                        break
                next_due += interval
                if next_due < deadline and time.monotonic() > next_due:
                    # Polls due at or after the configured capture end are not
                    # missing history. Only count missed slots inside its window.
                    missed = min(math.ceil((deadline - next_due) / interval),
                                 int((time.monotonic() - next_due) // interval) + 1)
                    record["gaps"].append({"cycle": cycle, "reason": "request_or_archive_overrun", "missed_cycles": missed,
                                           "observed_utc": _utc(), "observed_monotonic_ns": str(time.monotonic_ns())})
                    record["missed_poll_cycles"] += missed
                    next_due += missed * interval
                    self._save(record)
            if record["state"] == "running":
                if record["poll_count"] == 0:
                    record["gaps"].append({"reason": "capture_deadline_before_first_poll"})
                record["state"] = "partial" if record["failure_count"] or record["gaps"] else "complete"
        except CancelledError:
            record["state"] = "cancelled"
            raise
        except Exception as error:
            record.update(state="failed", error=str(error)[:500])
            raise
        finally:
            try:
                record.update(finished_utc=_utc(), finished_monotonic_ns=str(time.monotonic_ns()))
                self._save(record)
            finally:
                self.active.discard(capture_id)
        return {"capture": self._summary(record)}
