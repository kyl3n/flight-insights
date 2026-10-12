"""Synthetic loopback and fault injection; no aircraft or hardware qualification."""
import hashlib
import json
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from q22_engine import recorder
from q22_engine.log_decoder import iter_log_events


def configuration(**changes):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    return {"bind_host": "127.0.0.1", "bind_port": port, "allowed_sources": ["127.0.0.1"],
            "expected_system_id": 42, "expected_component_id": 1,
            "feed_origin": "live_forwarded", "synthetic": True, **changes}


def eventually(predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("Loopback recorder condition did not complete before its deadline")


class Wire:
    def __init__(self, system=42, component=1):
        self.mav = mavlink.MAVLink(None, srcSystem=system, srcComponent=component)

    def pack(self, message):
        value = message.pack(self.mav)
        self.mav.seq = (self.mav.seq + 1) % 256
        return value

    def heartbeat(self, armed=False, *, autopilot=3, vehicle=2):
        return self.pack(mavlink.MAVLink_heartbeat_message(vehicle, autopilot, 128 if armed else 0, 0, 4, 3))


@pytest.fixture
def service(tmp_path):
    instance = recorder.RecorderService(tmp_path / "Unicode opérateur capture")
    yield instance
    instance.close()


def journal(directory):
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


def local_worker(tmp_path, **changes):
    directory = tmp_path / str(uuid4())
    directory.mkdir()
    recorder._atomic_json(directory / "config.json", {
        "capture_session_id": directory.name, "config": configuration(**changes),
        "parent_pid": os.getpid(), "parent_created": psutil.Process().create_time()})
    worker = recorder._Worker(directory)
    worker.journal = recorder._Output(directory / "events.jsonl", "recorder_journal")
    worker.rotate()
    return worker


def finish_worker(worker):
    return [output.finish(True) for output in (worker.raw, worker.tlog, worker.journal) if output]


def test_passive_process_capture_timestamps_hashes_tlog_and_record_ranges(service):
    events = []
    service.on_event = lambda capture_id, event: events.append(event)
    config = configuration()
    status = service.start(str(uuid4()), config)
    assert status["state"] == "recording"
    assert status["worker_pid"] != os.getpid()
    wire = Wire()
    messages = [wire.heartbeat(False), wire.heartbeat(True), wire.pack(
        mavlink.MAVLink_param_value_message(b"ATC_RAT_RLL_P", .12, 9, 1, 0))]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.bind(("127.0.0.1", 0))
        sender.settimeout(.2)
        before = time.monotonic_ns()
        for message in messages:
            sender.sendto(message, ("127.0.0.1", config["bind_port"]))
        eventually(lambda: len([event for event in events if event["kind"] in {"heartbeat", "parameter"}]) == 3)
        after = time.monotonic_ns()
        with pytest.raises(socket.timeout):
            sender.recvfrom(65535)  # recorder must not send even a heartbeat
    result = service.stop()
    assert result["state"] == "stopped" and result["event_delivery_complete"]
    assert result["valid_frames"] == 3
    evidence = [event for event in events if event["kind"] in {"heartbeat", "parameter"}]
    assert all(before <= int(event["received_monotonic_ns"]) <= after for event in evidence)
    assert all(event["origin"] == "synthetic" and event["live"] is False for event in evidence)
    assert [event["record_index"] for event in evidence] == [1, 2, 3]
    for artifact in result["artifacts"]:
        path = Path(artifact["path"])
        assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert artifact["bytes"] == artifact["durable_bytes"] == path.stat().st_size
        assert artifact["complete"] is True
        assert (artifact["record_start"], artifact["record_end"]) == (1, 3)
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    tlog = next(Path(item["path"]) for item in result["artifacts"] if item["role"] == "app_mavlink_capture")
    decoded = list(iter_log_events(tlog))
    assert len(decoded) == 3 and any(event["kind"] == "parameter" for event in decoded)
    raw = next(Path(item["path"]) for item in result["artifacts"] if item["role"] == "raw_udp_capture").read_bytes()
    assert raw.startswith(recorder.RAW_MAGIC)
    offset = len(recorder.RAW_MAGIC)
    for message in messages:
        size, utc_ns, mono_ns, address, port = recorder.RAW_HEADER.unpack_from(raw, offset)
        offset += recorder.RAW_HEADER.size
        assert raw[offset:offset + size] == message
        assert before <= mono_ns <= after and utc_ns > 0 and socket.inet_ntoa(address) == "127.0.0.1"
        offset += size
    assert offset == len(raw)


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
def test_refuses_existing_gcs_bind_without_reuse(service, host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as gcs:
        if os.name == "nt":
            gcs.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        gcs.bind((host, 0))
        state = service.start(str(uuid4()), configuration(bind_port=gcs.getsockname()[1]))
        assert state["state"] == "failed"
        assert state["owns_udp"] is False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"controlling GCS remains reachable", ("127.0.0.1", gcs.getsockname()[1]))
        gcs.settimeout(1)
        assert gcs.recv(100) == b"controlling GCS remains reachable"


def test_catalog_reservation_blocks_other_coordinator_and_releases_on_stop(tmp_path):
    first, other = recorder.RecorderService(tmp_path), recorder.RecorderService(tmp_path)
    try:
        first.start(str(uuid4()), configuration())
        with pytest.raises(ValueError, match="Another recorder"):
            other.start(str(uuid4()), configuration())
        first.stop()
        assert other.start(str(uuid4()), configuration())["state"] == "recording"
    finally:
        first.close()
        other.close()


def test_worker_lock_closes_startup_gap_before_first_status_checkpoint(service):
    ownership = recorder._exclusive_file_lock(service.root / "worker.lock")
    config = configuration()
    try:
        state = service.start(str(uuid4()), config)
        assert state["state"] == "failed" and state["owns_udp"] is False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as endpoint:
            endpoint.bind((config["bind_host"], config["bind_port"]))
    finally:
        recorder._unlock_file(ownership)
    assert service.start(str(uuid4()), configuration())["state"] == "recording"


def test_launch_failure_releases_reservation_and_diagnostic_file(service, monkeypatch):
    launch = recorder.subprocess.Popen

    def failure(*args, **kwargs):
        raise OSError("injected executable startup failure")

    monkeypatch.setattr(recorder.subprocess, "Popen", failure)
    with pytest.raises(OSError, match="startup failure"):
        service.start(str(uuid4()), configuration())
    assert service._stderr is None and not service._owns_reservation
    monkeypatch.setattr(recorder.subprocess, "Popen", launch)
    assert service.start(str(uuid4()), configuration())["state"] == "recording"


def test_worker_crash_is_interrupted_and_never_auto_restarted(service):
    config = configuration()
    service.start(str(uuid4()), config)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(Wire().heartbeat(True), ("127.0.0.1", config["bind_port"]))
    eventually(lambda: any(event["kind"] == "heartbeat" for event in journal(service.directory)))
    pid = service.process.pid
    service.process.kill()
    service.process.wait(timeout=3)
    assert service.status()["state"] == "interrupted"
    recovery = recorder.RecorderService(service.root.parent)
    assert recovery.recovered()[0]["state"] == "interrupted"
    assert recovery.process is None
    assert not psutil.pid_exists(pid)
    assert any(service.directory.glob("telemetry-*.tlog"))
    assert (service.directory / "events.jsonl").stat().st_size > 0


def test_slow_callback_does_not_stop_process_receiving_and_preserves_receipt_time(service):
    entered, release = threading.Event(), threading.Event()
    events = []

    def callback(capture, event):
        if event["kind"] == "heartbeat" and not entered.is_set():
            entered.set()
            release.wait(8)
        events.append(event)

    service.on_event = callback
    config = configuration(synthetic=False)
    service.start(str(uuid4()), config)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            wire = Wire()
            sender.sendto(wire.heartbeat(True), ("127.0.0.1", config["bind_port"]))
            assert entered.wait(3)
            for _ in range(150):
                sender.sendto(wire.heartbeat(True), ("127.0.0.1", config["bind_port"]))
                time.sleep(.001)
        state = eventually(lambda: value if (value := service.status()).get("valid_frames", 0) == 151 else None)
        assert state["event_backlog_bytes"] > 0
        assert events and not any(event["kind"] == "heartbeat" for event in events)
        original = next(event for event in journal(service.directory) if event["kind"] == "heartbeat")
        release.set()
        eventually(lambda: len([event for event in events if event["kind"] == "heartbeat"]) == 151)
        delivered = next(event for event in events if event["kind"] == "heartbeat")
        assert delivered["received_monotonic_ns"] == original["received_monotonic_ns"]
        assert time.monotonic_ns() - int(delivered["received_monotonic_ns"]) > 100_000_000
    finally:
        release.set()


def test_silence_never_becomes_disarm(service):
    config = configuration(synthetic=False)
    service.start(str(uuid4()), config)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(Wire().heartbeat(True), ("127.0.0.1", config["bind_port"]))
    eventually(lambda: service.status().get("sources"))
    state = eventually(lambda: value if (value := service.status())["sources"][0]["armed_state"] == "unknown" else None, 5)
    assert state["sources"][0]["armed"] is True
    eventually(lambda: any(event.get("reason") == "heartbeat_stale" for event in journal(service.directory)))
    assert not any(event.get("armed") is False for event in journal(service.directory))


def test_frame_validation_source_selection_and_full_size_malformed_datagram(tmp_path):
    worker = local_worker(tmp_path)
    try:
        wire = Wire()
        corrupted = bytearray(wire.heartbeat(True))
        corrupted[-1] ^= 1
        frames = [bytes(corrupted), Wire(system=7).heartbeat(True), wire.heartbeat(True, autopilot=8),
                  wire.heartbeat(True, vehicle=6), wire.heartbeat(True)]
        for frame in frames:
            worker.datagram(frame, ("127.0.0.1", 40000), time.monotonic_ns(), time.time_ns(), mavlink)
        worker.datagram(b"\0" * 65507, ("127.0.0.1", 40000), time.monotonic_ns(), time.time_ns(), mavlink)
        worker.datagram(wire.heartbeat(), ("127.0.0.2", 40000), time.monotonic_ns(), time.time_ns(), mavlink)
        assert worker.valid_frames == 4 and worker.invalid_frames >= 2 and worker.rejected_senders == 1
        events = journal(worker.directory)
        heartbeats = [event for event in events if event["kind"] == "heartbeat"]
        assert len(heartbeats) == 1 and heartbeats[0]["system_id"] == 42
        assert worker.raw.bytes > 65507  # malformed payload retained intact
    finally:
        finish_worker(worker)


def test_fragment_offsets_and_chunk_record_ranges_are_exact(tmp_path, monkeypatch):
    worker = local_worker(tmp_path)
    monkeypatch.setattr(recorder, "CHUNK_BYTES", 100)
    wire = Wire()
    first, fragmented, last = wire.heartbeat(), wire.heartbeat(True), wire.heartbeat()
    payloads = [first, fragmented[:8], fragmented[8:], last]
    try:
        for index, payload in enumerate(payloads):
            worker.datagram(payload, ("127.0.0.1", 40000), 1_000_000 + index, 2_000_000 + index, mavlink)
        artifacts = worker.artifacts + finish_worker(worker)
        events = [event for event in journal(worker.directory) if event["kind"] == "heartbeat"]
        assert [event["record_index"] for event in events] == [1, 2, 3]
        spans = events[1]["raw_ref"]["wire_spans"]
        assert len(spans) == 2 and spans[0]["chunk_id"] != spans[1]["chunk_id"]
        assert b"".join((worker.directory / span["chunk_id"]).read_bytes()[span["byte_offset"]:span["byte_offset"] + span["bytes"]]
                        for span in spans) == fragmented
        raw = [item for item in artifacts if item["role"] == "raw_udp_capture"]
        tlogs = [item for item in artifacts if item["role"] == "app_mavlink_capture"]
        assert [(item["record_start"], item["record_end"]) for item in raw] == [(1, 2), (2, 3)]
        assert [(item["record_start"], item["record_end"]) for item in tlogs] == [(1, 1), (2, 3)]
    finally:
        finish_worker(worker)


def test_storage_reserve_refuses_start_and_retains_failure_journal(service):
    state = service.start(str(uuid4()), configuration(reserve_bytes=1024 ** 4))
    assert state["state"] == "failed" and state["owns_udp"] is False
    assert "storage" in state["failure"]["message"].lower()
    assert any(event["kind"] == "recorder_error" for event in journal(service.directory))


def test_failed_fsync_preserves_exact_provisional_file_metadata(tmp_path, monkeypatch):
    output = recorder._Output(tmp_path / "evidence.tlog", "app_mavlink_capture")
    output.write(b"prior complete frame")
    output.note_record(1)

    def full(*args):
        raise OSError("injected disk full")

    monkeypatch.setattr(recorder.os, "fsync", full)
    artifact = output.finish(True)
    assert artifact["complete"] is False and artifact["durable_bytes"] == 0
    assert artifact["checkpoint_error"] == "injected disk full"
    assert artifact["sha256"] == hashlib.sha256(output.path.read_bytes()).hexdigest()
    assert artifact["bytes"] == len(b"prior complete frame")
    assert output.finish(True) == artifact


def test_metadata_checkpoint_failure_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    recorder._atomic_json(path, {"state": "recording"})

    def full(*args):
        raise OSError("injected metadata disk exhaustion")

    monkeypatch.setattr(recorder.os, "fsync", full)
    with pytest.raises(OSError, match="disk exhaustion"):
        recorder._atomic_json(path, {"state": "stopped"})
    assert json.loads(path.read_text()) == {"state": "recording"}
    assert not list(tmp_path.glob(".checkpoint-*"))


def replacement_fault(monkeypatch, replace, *, windows=True):
    """Simulate Windows error classification without changing pathlib's host OS."""
    clock = {"now": 0.0, "waits": []}

    def sleep(seconds):
        clock["waits"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(recorder, "os", SimpleNamespace(**{
        **vars(os), "name": "nt" if windows else "posix", "replace": replace}))
    monkeypatch.setattr(recorder, "time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=sleep))
    return clock


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_windows_checkpoint_sharing_preserves_prior_bytes_until_replace(tmp_path, monkeypatch, winerror):
    path = tmp_path / "status.json"
    path.write_bytes(b'{"state":"recording"}')
    real_replace, attempts = os.replace, []
    denied = PermissionError("injected sharing violation")
    denied.winerror = winerror

    def replace(temporary, destination):
        attempts.append(Path(temporary).read_bytes())
        assert destination.read_bytes() == b'{"state":"recording"}'
        if len(attempts) < 3:
            raise denied
        real_replace(temporary, destination)

    clock = replacement_fault(monkeypatch, replace)
    recorder._atomic_json(path, {"state": "stopped"})
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert 0 < sum(clock["waits"]) < .5
    assert json.loads(path.read_text()) == {"state": "stopped"}
    assert not list(tmp_path.glob(".checkpoint-*"))


def test_windows_checkpoint_sharing_deadline_preserves_original_and_error(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    path.write_bytes(b'{"state":"recording"}')
    denied, attempts = PermissionError("injected persistent sharing violation"), []
    denied.winerror = 32

    def replace(temporary, destination):
        attempts.append(Path(temporary).read_bytes())
        assert destination.read_bytes() == b'{"state":"recording"}'
        raise denied

    clock = replacement_fault(monkeypatch, replace)
    with pytest.raises(PermissionError) as caught:
        recorder._atomic_json(path, {"state": "stopped"})
    assert caught.value is denied
    assert len(attempts) > 1 and sum(clock["waits"]) == pytest.approx(.5)
    assert max(clock["waits"]) <= .05
    assert path.read_bytes() == b'{"state":"recording"}'
    assert not list(tmp_path.glob(".checkpoint-*"))


@pytest.mark.parametrize("windows,winerror", [(False, 32), (True, 112), (True, None)])
def test_checkpoint_other_os_and_nonsharing_errors_never_retry(tmp_path, monkeypatch, windows, winerror):
    path = tmp_path / "status.json"
    path.write_bytes(b"prior evidence")
    failure, attempts = OSError("injected non-retryable replacement failure"), []
    if winerror is not None:
        failure.winerror = winerror

    def replace(temporary, destination):
        attempts.append(temporary)
        raise failure

    clock = replacement_fault(monkeypatch, replace, windows=windows)
    with pytest.raises(OSError) as caught:
        recorder._atomic_json(path, {"state": "stopped"})
    assert caught.value is failure and len(attempts) == 1 and clock["waits"] == []
    assert path.read_bytes() == b"prior evidence"
    assert not list(tmp_path.glob(".checkpoint-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows read-handle rename sharing semantics")
def test_windows_live_read_handle_can_close_during_checkpoint_retry(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    path.write_bytes(b'{"state":"recording"}')
    held = path.open("rb")
    conflict = threading.Event()
    real_replace = os.replace

    def replace(temporary, destination):
        try:
            return real_replace(temporary, destination)
        except OSError as error:
            if getattr(error, "winerror", None) in {5, 32, 33}:
                conflict.set()
            raise

    monkeypatch.setattr(recorder, "os", SimpleNamespace(**{**vars(os), "replace": replace}))

    def release():
        conflict.wait(2)
        held.close()

    reader = threading.Thread(target=release)
    reader.start()
    try:
        recorder._atomic_json(path, {"state": "stopped"})
        assert conflict.is_set(), "An actual Windows read handle must deny the first rename"
        assert json.loads(path.read_text()) == {"state": "stopped"}
        assert not list(tmp_path.glob(".checkpoint-*"))
    finally:
        reader.join(timeout=3)
        held.close()
    assert not reader.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows read-handle rename sharing semantics")
def test_windows_persistent_read_handle_rejects_checkpoint_without_losing_prior_bytes(tmp_path):
    path = tmp_path / "status.json"
    path.write_bytes(b'{"state":"recording"}')
    with path.open("rb") as held:
        with pytest.raises(OSError) as caught:
            recorder._atomic_json(path, {"state": "stopped"})
        assert caught.value.winerror in {5, 32, 33}
        assert held.read() == b'{"state":"recording"}'
    assert path.read_bytes() == b'{"state":"recording"}'
    assert not list(tmp_path.glob(".checkpoint-*"))


def test_partial_disk_write_preserves_prefix_and_incremental_digest(tmp_path):
    output = recorder._Output(tmp_path / "partial.udp", "raw_udp_capture")
    stream = output.stream

    class PartialDisk:
        first = True

        def write(self, data):
            if self.first:
                self.first = False
                return stream.write(data[:3])
            raise OSError("injected disk exhaustion after partial write")

        def __getattr__(self, name):
            return getattr(stream, name)

    output.stream = PartialDisk()
    with pytest.raises(OSError, match="disk exhaustion"):
        output.write(b"abcdef")
    result = output.finish(False)
    assert output.path.read_bytes() == b"abc"
    assert result["bytes"] == 3 and result["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert result["complete"] is False


@pytest.mark.parametrize("changes", [
    {"feed_origin": "replay"}, {"bind_host": "239.0.0.1"}, {"allowed_sources": ["0.0.0.0"]},
    {"expected_system_id": None}, {"expected_component_id": None}, {"synthetic": "false"},
])
def test_invalid_source_configuration_is_rejected_before_bind(changes):
    with pytest.raises(ValueError):
        recorder.validate_config(configuration(**changes))
