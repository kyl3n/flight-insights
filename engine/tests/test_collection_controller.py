"""Synthetic isolated loopback transport exercises the production coordinator.

The fixture declares the live transport path to exercise interval orchestration;
none of these temporary records are aircraft evidence or hardware qualification.
"""
import socket
import time
from uuid import uuid4

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from q22_engine.__main__ import Engine


def setup_capture(engine, expected_state="recording"):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    profile = {"aircraft_id": "SYNTHETIC LOOPBACK TEST ONLY", "aircraft_profile_version": "fixture-v1",
               "connection_profile_version": "fixture-v1", "connection": {
                   "local_address": "127.0.0.1", "listen_port": port, "expected_sender": "127.0.0.1",
                   "system_id": 42, "component_id": 1, "feed_origin": "live_forwarded", "synthetic": False}}
    preview = engine.call("session.preview", profile)
    arguments = {**preview["profile"], "operation_id": str(uuid4()),
                 "target_profile_version": profile["aircraft_profile_version"],
                 "applicability_fingerprint": preview["applicability_fingerprint"]}
    result = engine.call("session.prepare_and_watch", arguments)
    assert result["recorder"]["state"] == expected_state, result
    capture = result["capture_session"]
    stop = {"capture_session_id": capture["uuid"], "operation_id": str(uuid4()),
            "target_profile_version": capture["profile"]["aircraft_profile_version"],
            "applicability_fingerprint": capture["applicability_fingerprint"]}
    return port, arguments, stop, result


def eventually(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError("Synthetic loopback condition did not complete within its deadline")


def heartbeats(sender, port, flags, sequence=0):
    wire = mavlink.MAVLink(None, srcSystem=42, srcComponent=1)
    wire.seq = sequence
    for armed in flags:
        message = mavlink.MAVLink_heartbeat_message(
            mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            mavlink.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0, 0, mavlink.MAV_STATE_ACTIVE, 3)
        sender.sendto(message.pack(wire), ("127.0.0.1", port))
        wire.seq = (wire.seq + 1) % 256


def test_real_worker_idempotence_stop_validation_and_source_membership(tmp_path):
    engine = Engine(tmp_path)
    try:
        port, arguments, stop, started = setup_capture(engine)
        replay = engine.call("session.prepare_and_watch", arguments)
        assert replay["operation_replayed"] is True
        assert replay["recorder"]["worker_pid"] == started["recorder"]["worker_pid"]
        with pytest.raises(ValueError, match="fingerprint"):
            engine.call("session.stop", {**stop, "applicability_fingerprint": "stale"})
        assert engine.collection.recorder.status()["active"] is True
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            heartbeats(sender, port, [False, True, False])
        records = eventually(lambda: engine.call("session.status", {})["flight_records"])
        assert len(records) == 1
        result = engine.call("session.stop", stop)
        job_id = result["archive_job"]["id"]
        job = eventually(lambda: (value if value["state"] not in {"queued", "running"} else None)
                         if (value := engine.call("jobs.get", {"job_id": job_id})["job"]) else None)
        assert job["state"] == "succeeded", job
        detail = engine.call("session.detail", {"flight_record_id": records[0]["id"]})
        tlogs = [item for item in detail["associations"] if item["role"] == "app_mavlink_capture"]
        assert len(tlogs) == 1
        assert tlogs[0]["state"] == "provisional"
        assert tlogs[0]["time_or_record_spans"] == [{"kind": "records", "start": 2, "end": 3}]
        assert detail["armed_interval"]["disarm_evidence"]["armed"] is False
        assert detail["manifest"]["collection_completeness"]["state"] != "complete"
        assert engine.call("session.stop", stop)["operation_replayed"] is True
        state = engine.call("session.status", {})
        assert state["capture_session"]["archive_state"] == "complete"
        assert state["readiness"]["can_claim_complete"] is False
        assert {item["source"]["acquisition_method"] for item in state["capture_session"]["artifact_sources"]} == {"app_owned_passive_udp_capture"}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as free:
            free.bind(("127.0.0.1", port))
    finally:
        engine.close()


def test_shutdown_retains_unknown_end_and_explicit_archive_recovery(tmp_path):
    engine = Engine(tmp_path)
    port, _, stop, _ = setup_capture(engine)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            heartbeats(sender, port, [True])
        records = eventually(lambda: engine.call("session.status", {})["flight_records"])
    finally:
        engine.close()
    restarted = Engine(tmp_path)
    try:
        status = restarted.call("session.status", {})
        assert status["recorder"]["active"] is False
        assert status["capture_session"]["status"] == "interrupted"
        detail = restarted.call("session.detail", {"flight_record_id": records[0]["id"]})
        assert detail["armed_interval"]["end_status"] == "unknown"
        assert detail["armed_interval"]["disarm_evidence"] is None
        result = restarted.call("session.stop", stop)
        job_id = result["archive_job"]["id"]
        job = eventually(lambda: (value if value["state"] not in {"queued", "running"} else None)
                         if (value := restarted.call("jobs.get", {"job_id": job_id})["job"]) else None)
        assert job["state"] == "succeeded", job
        assert restarted.call("session.status", {})["capture_session"]["artifact_count"] >= 3
    finally:
        restarted.close()


def test_wire_valid_unindexed_parameter_cannot_block_later_flight_events(tmp_path):
    engine = Engine(tmp_path)
    try:
        port, _, _, _ = setup_capture(engine)
        wire = mavlink.MAVLink(None, srcSystem=42, srcComponent=1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            frame = mavlink.MAVLink_param_value_message(b"TEST_PARAM", 1.0, 9, 0, 65535).pack(wire)
            sender.sendto(frame, ("127.0.0.1", port))
            heartbeats(sender, port, [False, True, False], sequence=1)
        records = eventually(lambda: engine.call("session.status", {})["flight_records"])
        assert len(records) == 1
        eventually(lambda: engine.call("session.detail", {"flight_record_id": records[0]["id"]})
                   ["armed_interval"]["disarm_evidence"])
        # The catalog commit is visible before the callback returns and its
        # journal offset is acknowledged by the recorder's delivery thread.
        def drained_or_failed():
            state = engine.call("session.status", {})["recorder"]
            return state if state["callback_error"] or state["event_backlog_bytes"] == 0 else None
        recorder = eventually(drained_or_failed)
        assert recorder["callback_error"] is None
        assert recorder["event_backlog_bytes"] == 0
    finally:
        engine.close()


def test_failed_catalog_start_publication_does_not_leave_hidden_worker(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    try:
        def fail(*args, **kwargs):
            raise ValueError("Injected catalog publication failure after socket creation")
        monkeypatch.setattr(engine.collection.collection, "mark_recorder_started", fail)
        port, _, _, result = setup_capture(engine, expected_state="stopped")
        assert result["capture_session"]["status"] == "failed"
        assert result["recorder"]["active"] is False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as free:
            free.bind(("127.0.0.1", port))
    finally:
        engine.close()


def prepare_catalog_only(engine):
    """A durable capture fixture with no worker, sockets or aircraft packets."""
    service = engine.collection.collection
    preview = service.preview({
        "aircraft_id": "SYNTHETIC CATALOG ONLY", "aircraft_profile_version": "fixture-v1",
        "connection_profile_version": "fixture-v1", "connection": {
            "local_address": "127.0.0.1", "listen_port": 14551, "expected_sender": "127.0.0.1",
            "system_id": 42, "component_id": 1, "feed_origin": "live_forwarded", "synthetic": True}})
    arguments = {**preview["profile"], "operation_id": str(uuid4()),
                 "target_profile_version": "fixture-v1", "applicability_fingerprint": preview["applicability_fingerprint"]}
    capture = service.prepare(arguments)["capture_session"]
    stop = {"capture_session_id": capture["uuid"], "operation_id": str(uuid4()),
            "target_profile_version": "fixture-v1", "applicability_fingerprint": preview["applicability_fingerprint"]}
    return capture, stop


def terminal_job(engine, job_id):
    return eventually(lambda: value if (value := engine.call("jobs.get", {"job_id": job_id})["job"])["state"]
                      not in {"queued", "running"} else None)


def test_queued_archive_cancellation_reconciles_and_explicit_retry(tmp_path):
    import threading
    entered, release = threading.Event(), threading.Event()
    engine = Engine(tmp_path)
    try:
        capture, stop = prepare_catalog_only(engine)
        directory = engine.collection.recorder.root / capture["uuid"]
        directory.mkdir()
        (directory / "events.jsonl").write_bytes(b"")
        def blocker(*_args, **_kwargs):
            entered.set()
            assert release.wait(5), "Test worker was not released"
            return {"fixture": True}
        engine.jobs.start("fixture.blocker", {}, blocker)
        assert entered.wait(2)
        result = engine.call("session.stop", stop)
        archive_id = result["archive_job"]["id"]
        assert result["archive_job"]["state"] == "queued"
        engine.call("jobs.cancel", {"job_id": archive_id})
        release.set()
        assert terminal_job(engine, archive_id)["state"] == "cancelled"
        state = engine.call("session.status", {"capture_session_id": capture["uuid"]})["capture_session"]
        assert state["status"] == "stopped"
        assert state["archive_state"] == "failed"
        assert "cancelled" in state["archive_error"]
        replay = engine.call("session.stop", stop)
        assert replay["operation_replayed"] is True
        assert replay["capture_session"]["archive_state"] == "failed"
        retried = engine.call("session.stop", {**stop, "operation_id": str(uuid4())})
        assert retried["archive_job"]["id"] != archive_id
        assert terminal_job(engine, retried["archive_job"]["id"])["state"] == "succeeded"
    finally:
        release.set()
        engine.close()


def test_startup_finalizing_capture_is_interrupted_without_auto_restart(tmp_path):
    engine = Engine(tmp_path)
    capture, _ = prepare_catalog_only(engine)
    engine.collection.collection.mark_finalizing(capture["uuid"])
    engine.close()
    restarted = Engine(tmp_path)
    try:
        result = restarted.call("session.status", {"capture_session_id": capture["uuid"]})
        assert result["capture_session"]["status"] == "interrupted"
        assert result["capture_session"]["recorder_started"] is False
        assert result["recorder"]["active"] is False
        assert restarted.call("jobs.list", {})["jobs"] == []
    finally:
        restarted.close()


def test_archive_job_finishing_before_job_link_preserves_terminal_status(tmp_path):
    engine = Engine(tmp_path)
    class InlineJobs:
        def start(self, method, params, handler):
            handler(method, params)
            return {"job": {"id": "fixture-inline-job", "state": "succeeded"}}
    try:
        capture, _ = prepare_catalog_only(engine)
        engine.collection.jobs = InlineJobs()
        directory = engine.collection.recorder.root / capture["uuid"]
        directory.mkdir()
        journal = directory / "events.jsonl"
        journal.write_bytes(b"")
        engine.collection._queue_archive(capture["uuid"], {"artifacts": [
            {"path": str(journal), "role": "recorder_journal", "complete": False}]})
        result = engine.collection.collection.status({"capture_session_id": capture["uuid"]})["capture_session"]
        assert result["status"] == "stopped"
        assert result["archive_state"] == "complete"
        assert result["archive_job_id"] == "fixture-inline-job"
    finally:
        engine.close()


def test_empty_recorder_chunks_retain_originals_without_inventing_record_spans(tmp_path):
    from q22_engine.recorder import _Output, RAW_MAGIC
    engine = Engine(tmp_path)
    try:
        capture, _ = prepare_catalog_only(engine)
        directory = engine.collection.recorder.root / capture["uuid"]
        directory.mkdir()
        raw = _Output(directory / "raw-000001.udp", "raw_udp_capture")
        raw.write(RAW_MAGIC)
        tlog = _Output(directory / "telemetry-000001.tlog", "app_mavlink_capture")
        journal = _Output(directory / "events.jsonl", "recorder_journal")
        artifacts = [output.finish(True) for output in (raw, tlog, journal)]
        engine.collection._archive("session.archive", {"capture_session_id": capture["uuid"], "artifacts": artifacts})
        result = engine.call("session.status", {"capture_session_id": capture["uuid"]})
        assert result["capture_session"]["artifact_count"] == 3
        assert result["capture_session"]["archive_state"] == "complete"
        assert result["flight_records"] == []
        with engine.store._connect() as db:
            stored = engine.collection.collection._get(db, "capture", capture["uuid"])
        for artifact in stored["artifact_sources"]:
            assert artifact["coverage"]["interval_membership"] == "unknown"
            assert "record_start" not in artifact
            assert "record_end" not in artifact
    finally:
        engine.close()


def test_archive_rejects_outside_path_and_digest_changed_original(tmp_path):
    engine = Engine(tmp_path)
    try:
        capture, _ = prepare_catalog_only(engine)
        outside = tmp_path / "outside.tlog"
        outside.write_bytes(b"fixture outside capture")
        with pytest.raises(ValueError, match="outside"):
            engine.collection._archive("session.archive", {"capture_session_id": capture["uuid"],
                "artifacts": [{"path": str(outside), "role": "app_mavlink_capture"}]})
        directory = engine.collection.recorder.root / capture["uuid"]
        directory.mkdir()
        changed = directory / "telemetry-000001.tlog"
        changed.write_bytes(b"changed local fixture")
        with pytest.raises(ValueError, match="digest changed"):
            engine.collection._archive("session.archive", {"capture_session_id": capture["uuid"],
                "artifacts": [{"path": str(changed), "role": "app_mavlink_capture", "sha256": "0" * 64}]})
        result = engine.call("session.status", {"capture_session_id": capture["uuid"]})["capture_session"]
        assert result["artifact_count"] == 0
        assert result["archive_state"] == "failed"
        assert changed.read_bytes() == b"changed local fixture"
    finally:
        engine.close()


def test_no_artifact_capture_does_not_claim_successful_archival(tmp_path):
    engine = Engine(tmp_path)
    try:
        capture, stop = prepare_catalog_only(engine)
        result = engine.call("session.stop", stop)
        job = terminal_job(engine, result["archive_job"]["id"])
        assert job["state"] == "failed"
        state = engine.call("session.status", {"capture_session_id": capture["uuid"]})["capture_session"]
        assert state["status"] == "stopped"
        assert state["archive_state"] == "failed"
        assert state["artifact_count"] == 0
    finally:
        engine.close()
