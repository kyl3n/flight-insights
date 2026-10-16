"""One app UDP socket with a synthetic peer; no real GCS/hardware claim."""
from pathlib import Path
import json
import socket
import time
from uuid import uuid4

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mav

from q22_engine.acquisition import AcquisitionService, ALLOWED_MESSAGES, APP_SYSTEM_ID, APP_COMPONENT_ID
from q22_engine.collection_controller import CollectionController
from q22_engine.recorder import RAW_HEADER, RAW_MAGIC, RecorderService, _atomic_json
from q22_engine.store import Store
from test_acquisition import Peer, durable_limits, params
from test_recorder import eventually, local_worker, finish_worker


def shared_connection(peer):
    return peer.connection | {"transport": "shared_recorder"}


def observer_profile(peer):
    connection = peer.connection
    return {"aircraft_id": "SYNTHETIC SHARED ENDPOINT", "aircraft_profile_version": "test-v1",
        "connection_profile_version": "test-v1", "connection": {
            "local_address": connection["local_address"], "listen_port": connection["local_port"],
            "expected_sender": connection["remote_address"], "system_id": 1, "component_id": 1,
            "expected_uid": "123", "feed_origin": "live_forwarded", "synthetic": False,
            "readback": {"remote_port": connection["remote_port"]}}}


def records(directory):
    result = []
    for path in sorted(directory.glob("raw-*.udp")):
        with path.open("rb") as stream:
            assert stream.read(len(RAW_MAGIC)) == RAW_MAGIC
            while header := stream.read(RAW_HEADER.size):
                assert len(header) == RAW_HEADER.size
                size, utc_ns, mono_ns, host, port = RAW_HEADER.unpack(header)
                data = stream.read(size)
                assert len(data) == size
                result.append((data, mono_ns, utc_ns // 1000, (socket.inet_ntoa(host), port)))
    return result


@pytest.fixture
def shared(tmp_path):
    peer = Peer()
    recorder = RecorderService(tmp_path / "catalog")
    store = Store(tmp_path / "catalog")
    service = AcquisitionService(store, shared_transport=recorder.open_shared_transport)
    try:
        recorder.start(str(uuid4()), CollectionController._worker_config(observer_profile(peer)))
        yield peer, recorder, service
    finally:
        recorder.close()
        peer.close()


def test_single_socket_records_all_parameter_replies_and_preserves_receipt_clocks(shared):
    peer, recorder, service = shared
    request_peers = []
    original_receive = peer.receive
    def receive():
        payload, endpoint = original_receive()
        request_peers.append(endpoint)
        return payload, endpoint
    peer.receive = receive
    arguments = params(service, peer, connection=shared_connection(peer), limits=durable_limits())
    result = service.dispatch("acquisition.parameters.capture", arguments)["acquisition"]
    assert result["state"] == "complete", result
    assert result["parameter_capture"]["decoded_count"] == 2
    assert result["transport_evidence"]["transport"] == "shared_recorder"
    assert set(request_peers) == {peer.target}
    assert {message.get_msgId() for message in peer.requests} <= ALLOWED_MESSAGES
    assert {(message.get_srcSystem(), message.get_srcComponent()) for message in peer.requests} == {(254, 191)}
    assert all((message.target_system, message.target_component) == (1, 1) for message in peer.requests)
    assert recorder.status()["active"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as competing:
        with pytest.raises(OSError):
            competing.bind(peer.target)
    stopped = recorder.stop()
    assert stopped["state"] == "stopped"
    raw = records(recorder.directory)
    decoder = mav.MAVLink(None)
    frames = [message for payload, *_ in raw for message in decoder.parse_buffer(payload) or []]
    assert len([frame for frame in frames if frame.get_type() == "PARAM_VALUE"]) == 2
    assert stopped["packets"] == len(raw)
    from q22_engine.acquisition import RECORD, MAGIC
    trace = next(Path(item["path"]) for item in result["retained_files"] if Path(item["path"]).name == "exchange.mavudp")
    incoming = []
    with trace.open("rb") as stream:
        assert stream.read(len(MAGIC)) == MAGIC
        while header := stream.read(RECORD.size):
            direction, mono_ns, utc_us, host, port, size = RECORD.unpack(header)
            payload = stream.read(size)
            if direction == 0:
                incoming.append((payload, mono_ns, utc_us, (socket.inet_ntoa(host), port)))
    assert incoming and all(item in raw for item in incoming)


def test_shared_request_stops_on_arming_but_recorder_continues(shared):
    peer, recorder, service = shared
    def arm_during_readback(_message):
        peer.value(0)
        peer.armed = True
        peer.status()
    peer.readback_hook = arm_during_readback
    result = service.dispatch("acquisition.parameters.capture",
        params(service, peer, connection=shared_connection(peer), limits=durable_limits()))["acquisition"]
    assert result["state"] != "complete"
    assert "armed" in result["interruption_reason"].lower(), result
    assert not any(message.get_type() in {"PARAM_REQUEST_READ", "LOG_REQUEST_LIST", "LOG_REQUEST_DATA"}
                   for message in peer.requests)
    eventually(lambda: any(source.get("armed_state") == "armed" for source in recorder.status().get("sources", [])))
    assert recorder.status()["state"] == "recording"
    recorder.stop()
    journal = [json.loads(line) for line in (recorder.directory / "events.jsonl").read_text().splitlines()]
    assert any(item["kind"] == "heartbeat" and item.get("armed") for item in journal)
    assert any(item["kind"] == "parameter" and item["name"] == "TEST_A" for item in journal)


def test_same_endpoint_can_be_used_directly_only_after_recorder_stops(shared):
    peer, recorder, service = shared
    recorder.stop()
    result = service.dispatch("acquisition.logs.list", params(service, peer,
        connection=shared_connection(peer), limits=durable_limits(),
        confirm_log_transfer_side_effects=True))["acquisition"]
    assert result["state"] == "complete", result
    assert result["transport_evidence"]["transport"] == "dedicated_udp"


def test_observed_app_identity_collision_blocks_worker_sends(shared):
    peer, recorder, service = shared
    other_app = mav.MAVLink(None, srcSystem=APP_SYSTEM_ID, srcComponent=APP_COMPONENT_ID)
    frame = mav.MAVLink_heartbeat_message(6, 8, 0, 0, 3, 3).pack(other_app)
    peer.socket.sendto(frame, peer.target)
    eventually(lambda: any(source.get("system_id") == APP_SYSTEM_ID
                           for source in recorder.status().get("sources", [])))
    result = service.dispatch("acquisition.parameters.capture",
        params(service, peer, connection=shared_connection(peer), limits=durable_limits()))["acquisition"]
    assert result["state"] != "complete"
    assert "app MAVLink identity" in result["interruption_reason"]
    assert not peer.requests
    assert recorder.status()["state"] == "recording"


def test_shared_custom_app_identity_can_coexist_with_usual_gcs_identity(tmp_path):
    peer = Peer()
    recorder = RecorderService(tmp_path / "catalog")
    service = AcquisitionService(Store(tmp_path / "catalog"), shared_transport=recorder.open_shared_transport)
    connection = shared_connection(peer) | {"client_system_id": 250, "client_component_id": 192}
    profile = observer_profile(peer)
    profile["connection"]["readback"].update(client_system_id=250, client_component_id=192)
    try:
        recorder.start(str(uuid4()), CollectionController._worker_config(profile))
        gcs = mav.MAVLink(None, srcSystem=255, srcComponent=190)
        peer.socket.sendto(mav.MAVLink_heartbeat_message(6, 8, 0, 0, 3, 3).pack(gcs), peer.target)
        result = service.dispatch("acquisition.parameters.capture",
            params(service, peer, connection=connection, limits=durable_limits()))["acquisition"]
        assert result["state"] == "complete", result
        assert {(message.get_srcSystem(), message.get_srcComponent()) for message in peer.requests} == {(250, 192)}
        assert all((message.target_system, message.target_component) == (1, 1) for message in peer.requests)
        assert result["transport_evidence"]["client_identity"] == {"system_id": 250, "component_id": 192}
    finally:
        recorder.close()
        peer.close()


def test_app_identity_cannot_share_vehicle_system_id(shared):
    peer, _recorder, service = shared
    with pytest.raises(ValueError, match="differ"):
        service.preview({"connection": shared_connection(peer) | {"client_system_id": 1}})


def test_automatic_identity_is_rediscovered_between_collections(tmp_path):
    from q22_engine.flight_configuration import FlightConfigurationService
    peer = Peer()
    recorder = RecorderService(tmp_path / "catalog")
    service = AcquisitionService(Store(tmp_path / "catalog"), shared_transport=recorder.open_shared_transport)
    profile = observer_profile(peer)
    profile["connection"].pop("expected_uid")
    connection = shared_connection(peer)
    connection.pop("expected_uid")
    try:
        for uid in (123, 456):
            peer.uid = uid
            recorder.start(str(uuid4()), CollectionController._worker_config(profile))
            result = service.dispatch("acquisition.parameters.capture",
                params(service, peer, connection=connection, limits=durable_limits()))["acquisition"]
            assert result["state"] == "complete", result
            evidence = result["transport_evidence"]
            assert evidence["identity_basis"] == "reported_uid_discovered_not_authenticated"
            assert evidence["initial_gate_observations"]["identity"]["uid"] == str(uid)
            assert evidence["final_gate_observations"]["identity"]["uid"] == str(uid)
            assert "expected_uid" not in result["connection"]
            snapshot = FlightConfigurationService(service.store).promote({"acquisition_id": result["id"]})["snapshot"]
            assert snapshot["usable_for_full_comparison"] and snapshot["hardware_uid"] == str(uid)
            recorder.stop()
        assert "expected_uid" not in profile["connection"] and "expected_uid" not in connection
        sent_before = len(peer.requests)
        stopped = service.dispatch("acquisition.parameters.capture",
            params(service, peer, connection=connection, limits=durable_limits()))["acquisition"]
        assert stopped["state"] != "complete" and "active collection" in stopped["interruption_reason"]
        assert len(peer.requests) == sent_before  # No unpinned post-stop fallback.
    finally:
        recorder.close()
        peer.close()


@pytest.mark.parametrize("condition", ["missing_uid", "changed_uid", "locked_uid"])
def test_identity_discovery_never_ignores_missing_or_conflicting_identity(tmp_path, condition):
    peer = Peer()
    recorder = RecorderService(tmp_path / "catalog")
    service = AcquisitionService(Store(tmp_path / "catalog"), shared_transport=recorder.open_shared_transport)
    profile = observer_profile(peer)
    connection = shared_connection(peer)
    if condition != "locked_uid":
        profile["connection"].pop("expected_uid")
        connection.pop("expected_uid")
    if condition == "missing_uid":
        peer.uid = 0
    elif condition == "locked_uid":
        peer.uid = 456
    else:
        def change_during_readback(_message):
            peer.value(0)
            for uid in (456, 123):  # Returning to the first UID must not reopen the gate.
                peer.send(mav.MAVLink_autopilot_version_message(peer.capabilities, 1, 0, 0, 1,
                    [0] * 8, [0] * 8, [0] * 8, 1, 1, uid))
        peer.readback_hook = change_during_readback
    try:
        recorder.start(str(uuid4()), CollectionController._worker_config(profile))
        result = service.dispatch("acquisition.parameters.capture", params(service, peer,
            connection=connection, limits=durable_limits() | {"bootstrap_timeout_s": 1.5}))["acquisition"]
        assert result["state"] != "complete", result
        assert "UID" in result["interruption_reason"], result
        assert recorder.status()["active"]
        if condition != "changed_uid":
            assert not any(message.get_type().startswith("PARAM_REQUEST") for message in peer.requests)
        else:
            before = len(peer.requests)
            second = service.dispatch("acquisition.parameters.capture",
                params(service, peer, connection=connection, limits=durable_limits()))["acquisition"]
            assert second["state"] != "complete", second
            assert len(peer.requests) == before  # Recorder's identity lock survives new acquisitions.
    finally:
        recorder.close()
        peer.close()


def test_socket_owner_rejects_parameter_writes_even_if_internal_caller_bypasses_adapter(tmp_path):
    peer = Peer()
    connection = shared_connection(peer)
    worker = local_worker(tmp_path, bind_port=connection["local_port"], expected_system_id=1,
                          readback=connection)
    worker.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    worker.socket.bind(peer.target)
    try:
        encoder = mav.MAVLink(None, srcSystem=APP_SYSTEM_ID, srcComponent=APP_COMPONENT_ID)
        frame = mav.MAVLink_param_set_message(1, 1, b"TEST_A", 99, 9).pack(encoder)
        _atomic_json(worker.directory / "readback-command.json", {"id": str(uuid4()), "payload": frame.hex(),
            "expires_monotonic_ns": str(time.monotonic_ns() + 1_000_000_000),
            "heartbeat_timeout_s": 3, "identity_timeout_s": 15, "boot_timeout_s": 5})
        worker._shared_request(mav)
        reply = json.loads((worker.directory / "readback-reply.json").read_text())
        assert not reply["sent"] and "allowlist" in reply["reason"]
        assert not peer.requests
    finally:
        worker.socket.close()
        finish_worker(worker)
        peer.close()


def test_socket_owner_drains_queued_armed_heartbeat_before_allowed_request(tmp_path):
    peer = Peer()
    connection = shared_connection(peer)
    worker = local_worker(tmp_path, bind_port=connection["local_port"], expected_system_id=1,
                          readback=connection)
    worker.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    worker.socket.bind(peer.target)
    try:
        peer.armed = True
        peer.status()  # armed evidence is in the socket queue, not yet decoded
        encoder = mav.MAVLink(None, srcSystem=APP_SYSTEM_ID, srcComponent=APP_COMPONENT_ID)
        frame = mav.MAVLink_param_request_list_message(1, 1).pack(encoder)
        _atomic_json(worker.directory / "readback-command.json", {"id": str(uuid4()), "payload": frame.hex(),
            "expires_monotonic_ns": str(time.monotonic_ns() + 1_000_000_000),
            "heartbeat_timeout_s": 3, "identity_timeout_s": 15, "boot_timeout_s": 5})
        worker._shared_request(mav)
        reply = json.loads((worker.directory / "readback-reply.json").read_text())
        assert not reply["sent"] and "disarmed" in reply["reason"]
        assert not peer.requests
        assert any(source.get("armed") for source in worker.sources.values())
        retained = records(worker.directory)
        assert retained  # the drained evidence was captured, not consumed by readback
    finally:
        worker.socket.close()
        finish_worker(worker)
        peer.close()


@pytest.mark.parametrize("automatic_uid,vehicle_uid", [(False, 123), (True, 123), (True, 456)])
def test_shared_policy_bootstraps_uid_without_a_second_forwarded_stream(tmp_path, automatic_uid, vehicle_uid):
    from q22_engine.__main__ import Engine
    peer = Peer()
    peer.uid = vehicle_uid
    connection = shared_connection(peer)
    if automatic_uid:
        connection.pop("expected_uid")
    # Lower-rate production-like telemetry, with no unsolicited version packet.
    original_status, last_status = peer.status, [0.0]
    def status():
        now = time.monotonic()
        if now - last_status[0] >= .2:
            last_status[0] = now
            original_status()
    peer.status = status
    engine = Engine(tmp_path / "engine")
    try:
        orchestrator = engine._orchestrator()
        policy = {"aircraft_id": "SYNTHETIC SHARED ENDPOINT", "connection": connection,
                  "synthetic": False, "preflight_parameters": True, "postflight_parameters": False,
                  "automatic_preflight_refresh": True, "limits": durable_limits()}
        saved = orchestrator.save_policy({"policy": policy, "operation_id": str(uuid4())})["policy"]
        profile = observer_profile(peer)
        if automatic_uid:
            profile["connection"].pop("expected_uid")
        preview = engine.collection.collection.preview(profile)
        reply = engine.handle({"jsonrpc": "2.0", "id": 1, "method": "session.prepare_and_watch", "params": {
            **preview["profile"], "operation_id": str(uuid4()), "target_profile_version": "test-v1",
            "applicability_fingerprint": preview["applicability_fingerprint"], "acquisition_policy": {
                "policy_id": saved["id"], "policy_fingerprint": saved["policy_fingerprint"],
                "confirm_automatic_readback": True, "confirm_experimental_adapter": True}}})
        assert "error" not in reply, reply
        capture_id = reply["result"]["capture_session"]["uuid"]
        deadline, latest = time.monotonic() + 25, None
        while time.monotonic() < deadline:
            latest = orchestrator.dispatch("orchestration.status", {"capture_session_id": capture_id})
            if latest["requests"] and latest["requests"][0]["state"] in {"complete", "failed", "interrupted", "partial", "cancelled"}:
                break
            time.sleep(.05)
        assert latest["requests"] and latest["requests"][0]["state"] == "complete", latest
        assert latest["runs"][0]["preflight_readiness"]["current_for_next_flight"], latest
        result = engine.store.get_document("acquisition", latest["requests"][0]["acquisition_id"])
        assert result["transport_evidence"]["initial_gate_observations"]["identity"]["uid"] == str(vehicle_uid)
        assert ("expected_uid" not in result["connection"]) is automatic_uid
        completed_id = latest["requests"][0]["id"]
        # Our own parameter replies have caught up to the catalog; they must not
        # cause an immediate invalidation/re-enumeration loop.
        time.sleep(.3)
        latest = orchestrator.dispatch("orchestration.status", {"capture_session_id": capture_id})
        assert len(latest["requests"]) == 1 and latest["requests"][0]["id"] == completed_id
        assert latest["runs"][0]["preflight_readiness"]["current_for_next_flight"]
        assert sum(message.get_type() == "PARAM_REQUEST_LIST" for message in peer.requests) == 1
        assert engine.collection.recorder.status()["bindings"] == [{"local_address": "127.0.0.1", "port": peer.target[1]}]
        assert engine._acquisition_gate("log_list", shared_connection(peer))["allowed"] is False
    finally:
        engine.close()
        peer.close()
