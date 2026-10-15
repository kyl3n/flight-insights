"""Synthetic managed callbacks and real loopback acquisition; no hardware claim."""
from copy import deepcopy
import json
from types import SimpleNamespace
import threading
import time
from uuid import uuid4

import pytest

from q22_engine.acquisition import AcquisitionService
from q22_engine.collection import CollectionService
from q22_engine.jobs import JobService
from q22_engine.orchestration import OrchestrationService
from q22_engine.store import Store
from test_acquisition import Peer, delay_first_heartbeat_persistence, durable_limits
from test_collection import event


class Recorder:
    def __init__(self):
        self.value = {"active": False}

    def status(self):
        return deepcopy(self.value)


def paced_policy_status(peer, *, clock=time.monotonic):
    """Status at 5 Hz for the durable recorder/catalog integration fixture.

    Peer normally emits every 20 ms for short transport-only tests. Mirroring
    that rate into real heartbeat/identity catalog commits needlessly floods
    Windows disk I/O. Keep live evidence periodic, without a catch-up burst
    after scheduler delays, and advance the synthetic boot clock with time.
    """
    from pymavlink.dialects.v20 import ardupilotmega as mav

    original_status = peer.status
    started = clock()
    last_sent = None

    def status():
        nonlocal last_sent
        now = clock()
        if last_sent is not None and now - last_sent < .2:
            return
        last_sent = now
        peer.boot_ms = 1000 + int((now - started) * 1000)
        original_status()
        peer.send(mav.MAVLink_autopilot_version_message(131072, 1, 0, 0, 1,
            [0] * 8, [0] * 8, [0] * 8, 1, 1, 123))

    return status


def test_durable_policy_peer_paces_identity_and_advances_boot_without_catchup():
    now = [100.0]
    boot_times, identities = [], []
    peer = SimpleNamespace(boot_ms=0, send=identities.append)
    peer.status = lambda: boot_times.append(peer.boot_ms)
    status = paced_policy_status(peer, clock=lambda: now[0])
    for instant in (100.0, 100.02, 100.18, 100.25, 100.26, 101.25, 101.26):
        now[0] = instant
        status()
    assert boot_times == [1000, 1250, 2250]
    assert len(identities) == 3
    assert all(message.get_type() == "AUTOPILOT_VERSION" for message in identities)


class Harness:
    def __init__(self, root):
        self.peer = Peer()
        self.store = Store(root)
        self.collection = CollectionService(self.store)
        self.recorder = Recorder()
        self.controller = SimpleNamespace(collection=self.collection, recorder=self.recorder)
        self.jobs = JobService(root)
        self.acquisition = AcquisitionService(self.store)
        self.service = OrchestrationService(self.store, self.jobs, self.acquisition, self.controller, start_monitor=False)
        self.profile = {"aircraft_id": "synthetic-policy", "aircraft_profile_version": "fixture-v1",
            "connection_profile_version": "fixture-v1", "connection": {
                "local_address": "127.0.0.1", "listen_port": 14551, "expected_sender": "127.0.0.1",
                "system_id": 1, "component_id": 1, "expected_uid": "123", "feed_origin": "live_forwarded", "synthetic": False}}
        # The catalog callback fixture follows the live path to exercise real
        # interval semantics; all transport/aircraft evidence is synthetic here.
        self.policy = {"aircraft_id": self.profile["aircraft_id"], "connection": self.peer.connection,
            "synthetic": False, "preflight_parameters": True, "postflight_parameters": True,
            "log_retrieval": "disabled", "preflight_freshness_s": 60,
            "limits": durable_limits()}
        self.index = 0

    def prepare(self, **changes):
        self.policy.update(changes)
        saved = self.service.save_policy({"policy": self.policy, "operation_id": str(uuid4())})["policy"]
        self.approval = {"policy_id": saved["id"], "policy_fingerprint": saved["policy_fingerprint"],
            "confirm_automatic_readback": True, "confirm_experimental_adapter": True,
            "confirm_log_transfer_side_effects": self.policy.get("confirm_log_transfer_side_effects", False)}
        preview = self.collection.preview(self.profile)
        self.capture = self.collection.prepare({**self.profile, "operation_id": str(uuid4()),
            "target_profile_version": "fixture-v1", "applicability_fingerprint": preview["applicability_fingerprint"]})["capture_session"]
        self.capture = self.collection.mark_recorder_started(self.capture["uuid"])
        self.recorder.value = {"capture_session_id": self.capture["uuid"], "active": True, "state": "recording", "event_backlog_bytes": 0}
        self.service.on_prepared(self.capture, self.approval)
        return self.capture

    def emit(self, kind="heartbeat", armed=False, **changes):
        self.index += 1
        value = event(self.index, armed, kind=kind, boot_epoch="0", **changes)
        return self.collection.ingest_event(self.capture["uuid"], value)

    def qualify(self):
        self.emit("identity", uid="123", firmware_identity={"flight_sw_version": 1, "board_version": 1, "vendor_id": 1, "product_id": 1})
        self.emit()

    def tick(self):
        self.service.tick(self.capture["uuid"])

    def rows(self, role=None):
        rows = self.service.dispatch("orchestration.status", {"capture_session_id": self.capture["uuid"]})["requests"]
        return [row for row in rows if role is None or row["role"] == role]

    def wait(self, role, state="complete"):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = self.rows(role)
            if rows and rows[0]["state"] == state:
                return rows[0]
            if rows and rows[0]["state"] in {"complete", "partial", "failed", "interrupted", "cancelled"}:
                break
            time.sleep(.05)
        raise AssertionError({"expected_state": state, "requests": self.rows(role),
                              "peer_alive": self.peer.thread.is_alive(), "peer_error": repr(self.peer.error)})

    def wait_run(self, state):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.tick()
            run = self.service._get("run", self.capture["uuid"])
            if run["state"] == state:
                return run
            time.sleep(.01)
        raise AssertionError(run)

    def stop(self):
        self.collection.stop_capture({"capture_session_id": self.capture["uuid"], "operation_id": str(uuid4()),
            "target_profile_version": "fixture-v1", "applicability_fingerprint": self.capture["applicability_fingerprint"]})
        self.recorder.value.update(active=False, state="stopped")
        self.service.on_stopped(self.capture["uuid"])

    def close(self):
        self.service.close()
        self.jobs.close()
        self.peer.close()


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path / "catalog")
    yield value
    value.close()


def test_saved_policy_and_restart_never_send_without_prepare_approval(h):
    arguments = {"policy": h.policy, "operation_id": str(uuid4())}
    first = h.service.save_policy(arguments)
    assert h.service.save_policy(arguments)["operation_replayed"]
    with pytest.raises(ValueError, match="different policy"):
        h.service.save_policy(arguments | {"policy": h.policy | {"preflight_parameters": False}})
    assert not h.peer.requests
    assert first["policy"]["adapter"]["qualification"] == "experimental_not_hardware_qualified"
    reopened = OrchestrationService(h.store, h.jobs, h.acquisition, h.controller)
    try:
        assert reopened.dispatch("orchestration.policy.list", {})["policies"][0] == first["policy"]
        assert reopened.dispatch("orchestration.status", {})["runs"] == []
        assert not h.peer.requests
    finally:
        reopened.close()


def test_policy_list_maximum_page_preserves_order_and_continuation(h):
    saved = [h.service.save_policy({"policy": h.policy, "operation_id": str(uuid4())})["policy"]
             for _ in range(101)]
    expected = sorted(saved, key=lambda row: (row["created_at"], row["id"]), reverse=True)
    first = h.service.dispatch("orchestration.policy.list", {"limit": 100})
    assert first["policies"] == expected[:100]
    assert first["next_offset"] == 100
    last = h.service.dispatch("orchestration.policy.list", {"limit": 100, "offset": first["next_offset"]})
    assert last == {"policies": expected[100:], "next_offset": None}
    empty = h.service.dispatch("orchestration.policy.list", {"limit": 100, "offset": 101})
    assert empty == {"policies": [], "next_offset": None}
    assert not h.peer.requests


@pytest.mark.parametrize("change", ["approval", "fingerprint", "uid", "same_port", "one_way"])
def test_policy_activation_rejects_inapplicable_or_unapproved_endpoint(h, change):
    h.prepare()
    profile, approval = deepcopy(h.profile), deepcopy(h.approval)
    if change == "approval":
        approval["confirm_experimental_adapter"] = False
    elif change == "fingerprint":
        approval["policy_fingerprint"] = "changed"
    elif change == "uid":
        profile["connection"]["expected_uid"] = "999"
    elif change == "same_port":
        profile["connection"]["listen_port"] = h.peer.connection["local_port"]
    else:
        with pytest.raises(ValueError, match="direct_bidirectional"):
            h.service.preview({"policy": h.policy | {"connection": h.peer.connection | {"feed_origin": "live_forwarded"}}})
        return
    with pytest.raises(ValueError):
        h.service.validate_prepare(profile, approval)
    assert not h.peer.requests


def test_preflight_real_readback_exactly_once_and_observed_change_invalidates(h):
    h.prepare()
    h.tick()
    assert h.rows() == [] and not h.peer.requests
    h.qualify()
    h.tick()
    request = h.wait("preflight_parameters")
    h.tick()
    reviewed = h.rows("preflight_parameters")[0]
    assert reviewed["freshness"] == "within_declared_budget"
    assert reviewed["applicability"] == "provisional_review_only"
    acquisition = h.store.get_document("acquisition", request["acquisition_id"])
    assert acquisition["parameter_capture"]["completeness"] == "complete_for_scope"
    assert acquisition["parameter_capture"]["flight_time_applicability"] == "unknown"
    evidence = acquisition["transport_evidence"]
    assert evidence["initial_gate_observations"]["identity"]["uid"] == "123"
    assert evidence["final_gate_observations"]["boot_clock"]["time_boot_ms"] >= 1000
    assert evidence["initial_gate_observations"]["identity"]["raw_ref"]
    sent = len(h.peer.requests)
    h.emit("parameter", name="TEST_A", value=9, type=9, count=2, index=0)
    h.tick()
    assert h.rows("preflight_parameters")[0]["freshness"] == "invalidated"
    assert len(h.peer.requests) == sent
    assert h.recorder.status()["active"]


@pytest.mark.parametrize("change", ["stale", "gap", "identity", "boot", "budget"])
def test_preflight_freshness_is_revoked_without_mutating_originals(h, change, monkeypatch):
    # Deliberately exceed the old 150 ms transport-only budget before proving
    # that durable success and every subsequent revocation remain independent.
    delays = delay_first_heartbeat_persistence(monkeypatch)
    h.prepare()
    h.qualify()
    h.tick()
    request = h.wait("preflight_parameters")
    assert len(delays) == 1 and delays[0]["elapsed_s"] >= .25
    original = h.store.get_document("acquisition", request["acquisition_id"])
    if change == "stale":
        with h.collection._lock, h.store._connect() as db:
            capture = h.collection._get(db, "capture", h.capture["uuid"])
            source = capture["source_states"][capture["selected_source_id"]]
            source["heartbeat_evidence"]["received_monotonic_ns"] = str(time.monotonic_ns() - 20_000_000_000)
            h.collection._put(db, "capture", capture)
    elif change == "gap":
        h.emit("link_gap", reason="sequence_discontinuity")
    elif change == "identity":
        h.emit("identity", uid="123", firmware_identity={"flight_sw_version": 2})
    elif change == "boot":
        h.index += 1
        h.collection.ingest_event(h.capture["uuid"], event(h.index, boot_epoch="1"))
    else:
        h.service._save("request", request | {"finished_monotonic_ns": str(time.monotonic_ns() - 61_000_000_000)})
    h.tick()
    assert h.rows("preflight_parameters")[0]["freshness"] == "invalidated"
    assert h.store.get_document("acquisition", request["acquisition_id"]) == original
    assert h.recorder.status()["active"]
    if change == "stale":
        h.emit()
        h.tick()
        assert h.rows("preflight_parameters")[0]["freshness"] == "invalidated", "A fresh heartbeat must not revive stale evidence."


def test_arming_cancels_requests_recording_continues_and_postflight_is_separate(h):
    h.prepare()
    h.qualify()
    h.peer.readback_hook = lambda _message: None
    h.peer.on_request = lambda message: message.get_type() == "PARAM_REQUEST_READ"
    h.tick()
    deadline = time.monotonic() + 2
    while not any(message.get_type() == "PARAM_REQUEST_LIST" for message in h.peer.requests):
        assert time.monotonic() < deadline
        time.sleep(.01)
    h.emit(armed=True)
    h.tick()
    interrupted = h.wait("preflight_parameters", "interrupted")
    assert interrupted["revoked"] and h.recorder.status()["active"]
    h.peer.readback_hook = h.peer.on_request = None
    h.emit()
    h.tick()
    post = h.wait("postflight_parameters")
    assert post["flight_record_id"]
    assert post["association_state"] == "provisional" and post["historical_applicability"] == "unknown"
    assert len(h.collection.status()["flight_records"]) == 1


def test_explicit_stop_listing_and_selected_log_retrieval_are_per_flight_and_idempotent(h):
    h.prepare(preflight_parameters=False, postflight_parameters=False, log_retrieval="selected_after_stop",
              confirm_log_transfer_side_effects=True)
    h.qualify()
    h.emit(armed=True)
    h.emit()
    h.tick()
    intent = h.rows("onboard_log")[0]
    assert intent["state"] == "waiting" and intent["flight_record_id"]
    assert not h.peer.requests
    h.stop()
    h.tick()
    listed = h.wait("log_listing")
    selection = {"request_id": intent["id"], "listing_id": listed["acquisition_id"], "log_id": 7,
                 "operation_id": str(uuid4()), "confirm_selected_log_closed": True}
    with pytest.raises(ValueError, match="actual returned"):
        h.service.select_retrieval(selection | {"log_id": 99})
    result = h.service.select_retrieval(selection)
    repeated = h.service.select_retrieval(selection)
    assert repeated["operation_replayed"] and result["request"]["job_id"] == repeated["request"]["job_id"]
    downloaded = h.wait("onboard_log")
    assert downloaded["closure_basis"] == "operator_declared_not_protocol_verified"
    assert downloaded["flight_record_id"] == intent["flight_record_id"]
    assert downloaded["association_state"] == "provisional" and downloaded["recovery_warning"]
    assert any(row["role"] == "onboard_log_candidate" for row in downloaded["source_refs"])
    detail = h.collection.detail({"flight_record_id": intent["flight_record_id"]})
    assert detail["manifest"]["collection_completeness"]["state"] != "complete"


def test_finished_retrieval_releases_runtime_only_after_every_flight_selection(h):
    h.prepare(preflight_parameters=False, postflight_parameters=False, log_retrieval="selected_after_stop",
              confirm_log_transfer_side_effects=True)
    h.qualify()
    for _ in range(2):
        h.emit(armed=True)
        h.emit()
    h.tick()
    intents = h.rows("onboard_log")
    assert len(intents) == 2 and all(row["state"] == "waiting" for row in intents)
    h.stop()
    h.tick()
    listed = h.wait("log_listing")
    for index, intent in enumerate(intents):
        selection = {"request_id": intent["id"], "listing_id": listed["acquisition_id"], "log_id": 7,
                     "operation_id": str(uuid4()), "confirm_selected_log_closed": True}
        h.service.select_retrieval(selection)
        deadline = time.monotonic() + 4
        while True:
            selected = h.service._get("request", intent["id"])
            if selected["state"] == "complete":
                break
            assert time.monotonic() < deadline, selected
            time.sleep(.01)
        h.tick()
        run = h.service.dispatch("orchestration.status", {"capture_session_id": h.capture["uuid"]})["runs"][0]
        if index == 0:
            assert run["state"] == "awaiting_selected_log" and run["runtime_authorized"]
            assert h.service._get("request", intents[1]["id"])["state"] == "waiting"
        else:
            h.wait_run("completed")
            run = h.service.dispatch("orchestration.status", {"capture_session_id": h.capture["uuid"]})["runs"][0]
            assert run["state"] == "completed" and not run["runtime_authorized"]
            assert h.capture["uuid"] not in h.service._active
        assert h.service.select_retrieval(selection)["operation_replayed"]
    sent = len(h.peer.requests)
    h.tick()
    assert h.service.on_prepared(h.capture, h.approval)["state"] == "completed"
    assert h.capture["uuid"] not in h.service._active and len(h.peer.requests) == sent


def test_stop_waits_for_queued_parameter_job_cancellation_without_sending(h):
    entered, release = threading.Event(), threading.Event()

    def occupying_job(*_args, **_kwargs):
        entered.set()
        assert release.wait(4)
        return {}

    h.jobs.start("synthetic.analysis", {}, occupying_job)
    assert entered.wait(2)
    try:
        h.prepare()
        h.qualify()
        h.tick()
        request = h.rows("preflight_parameters")[0]
        assert request["state"] == "queued"
        h.stop()
        h.tick()
        assert h.capture["uuid"] in h.service._active
        assert h.jobs.dispatch("jobs.get", {"job_id": request["job_id"]})["job"]["cancel_requested"]
        assert not h.peer.requests
    finally:
        release.set()
    h.wait_run("completed")
    assert h.rows("preflight_parameters")[0]["state"] == "cancelled"
    assert h.capture["uuid"] not in h.service._active and not h.peer.requests


def test_final_request_result_does_not_release_approval_before_worker_terminal(h, monkeypatch):
    h.prepare(preflight_parameters=False, postflight_parameters=False, log_retrieval="selected_after_stop",
              confirm_log_transfer_side_effects=True)
    h.qualify()
    h.emit(armed=True)
    h.emit()
    h.stop()
    h.tick()
    listed = h.wait("log_listing")
    intent = h.rows("onboard_log")[0]
    entered, release = threading.Event(), threading.Event()
    original_update = h.jobs._update

    def delay_terminal(job_id, **changes):
        if changes.get("state") == "succeeded" and h.jobs._read(job_id)["method"] == "acquisition.logs.download":
            entered.set()
            assert release.wait(4)
        return original_update(job_id, **changes)

    monkeypatch.setattr(h.jobs, "_update", delay_terminal)
    try:
        h.service.select_retrieval({"request_id": intent["id"], "listing_id": listed["acquisition_id"], "log_id": 7,
            "operation_id": str(uuid4()), "confirm_selected_log_closed": True})
        assert entered.wait(3)
        request = h.wait("onboard_log")
        assert h.jobs.dispatch("jobs.get", {"job_id": request["job_id"]})["job"]["state"] == "running"
        h.tick()
        assert h.capture["uuid"] in h.service._active
        assert h.service._get("run", h.capture["uuid"])["state"] == "awaiting_selected_log"
    finally:
        release.set()
    h.wait_run("completed")
    assert h.capture["uuid"] not in h.service._active


@pytest.mark.parametrize("listing_failure", [False, True], ids=["empty_listing", "adapter_failure"])
def test_unavailable_listing_closes_blocked_intents_without_retry(h, monkeypatch, listing_failure):
    h.prepare(preflight_parameters=False, postflight_parameters=False, log_retrieval="selected_after_stop",
              confirm_log_transfer_side_effects=True)
    h.qualify()
    h.emit(armed=True)
    h.emit()
    if listing_failure:
        def reject_adapter(*_args, **_kwargs):
            raise ValueError("Synthetic adapter refused listing before any request")
        monkeypatch.setattr(h.acquisition, "dispatch", reject_adapter)
    else:
        h.peer.logs = {}
    h.stop()
    h.wait_run("completed")
    intent = h.rows("onboard_log")[0]
    assert intent["state"] == "interrupted" and "no selectable result" in intent["reason"]
    assert intent["acquisition_id"] is None and intent["job_id"] is None
    listing = h.rows("log_listing")[0]
    assert listing["state"] == ("interrupted" if listing_failure else "complete")
    if listing_failure:
        job = h.jobs.dispatch("jobs.get", {"job_id": listing["job_id"]})["job"]
        assert job["state"] == "failed" and "Synthetic adapter" in job["error"]["message"]
        assert not h.peer.requests
    sent = len(h.peer.requests)
    h.tick()
    assert h.capture["uuid"] not in h.service._active and len(h.peer.requests) == sent


def test_close_restart_preserves_requests_but_never_reactivates_saved_run(h):
    h.prepare()
    h.qualify()
    h.tick()
    h.wait("preflight_parameters")
    h.service.close()
    count = len(h.peer.requests)
    reopened = OrchestrationService(h.store, h.jobs, h.acquisition, h.controller, start_monitor=False)
    try:
        reopened.tick(h.capture["uuid"])
        reopened.on_prepared(h.capture, h.approval)  # Replayed prepare UUID cannot reactivate.
        status = reopened.dispatch("orchestration.status", {})
        assert status["requests"] and not status["runs"][0]["runtime_authorized"]
        assert status["runs"][0]["state"] == "suspended"
        assert len(h.peer.requests) == count
    finally:
        reopened.close()


def test_prepare_operation_binds_even_absent_opt_in_before_network(h):
    operation = str(uuid4())
    assert h.service.validate_prepare_operation(h.profile, None, operation) is None
    h.prepare()
    with pytest.raises(ValueError, match="different policy approval"):
        h.service.validate_prepare_operation(h.profile, h.approval, operation)
    another = str(uuid4())
    h.service.validate_prepare_operation(h.profile, h.approval, another)
    h.service.validate_prepare_operation(h.profile, h.approval, another)
    with pytest.raises(ValueError, match="different policy approval"):
        h.service.validate_prepare_operation(h.profile, h.approval | {"operator_note": "changed"}, another)
    assert not h.peer.requests


def test_dead_owner_recovery_is_interrupted_live_owner_is_untouched(h):
    h.prepare()
    run = h.service._get("run", h.capture["uuid"])
    live = OrchestrationService(h.store, h.jobs, h.acquisition, h.controller, start_monitor=False)
    assert live._get("run", run["id"])["state"] == "watching"
    live.close()
    request = h.service._request(run, "preflight_parameters", "watch_start")
    h.service._save("request", request | {"state": "running"})
    h.service._save("run", run | {"owner": {"pid": run["owner"]["pid"], "created_at": 0}})
    recovered = OrchestrationService(h.store, h.jobs, h.acquisition, h.controller, start_monitor=False)
    try:
        assert recovered._get("run", run["id"])["state"] == "interrupted"
        assert recovered._get("request", request["id"])["state"] == "interrupted"
        assert not h.peer.requests
    finally:
        recovered.close()


def test_monitor_starts_approved_readback_without_status_polling(h):
    h.service.close()
    h.service = OrchestrationService(h.store, h.jobs, h.acquisition, h.controller, poll_interval=.02)
    h.prepare()
    h.qualify()
    result = h.wait("preflight_parameters")
    assert result["acquisition_id"] and h.recorder.status()["active"]


def test_external_admission_wait_does_not_consume_one_shot_request(h):
    allowed = False
    h.acquisition.external_gate = lambda *_args: {"allowed": allowed, "reason": "Waiting for recorder checkpoint"}
    h.prepare()
    h.qualify()
    h.tick()
    waiting = h.rows("preflight_parameters")[0]
    assert waiting["state"] == "waiting" and waiting["job_id"] is None and not h.peer.requests
    allowed = True
    h.tick()
    complete = h.wait("preflight_parameters")
    assert complete["id"] == waiting["id"]


def test_future_schema_guard_precedes_recovery_writes(h):
    h.prepare()
    before = h.service._get("run", h.capture["uuid"])
    with h.store._connect() as db:
        db.execute("UPDATE orchestration_schema SET version=2")
    with pytest.raises(ValueError, match="Unsupported orchestration catalog version"):
        OrchestrationService(h.store, h.jobs, h.acquisition, h.controller, start_monitor=False)
    assert h.service._get("run", h.capture["uuid"]) == before
    with h.store._connect() as db:
        assert db.execute("SELECT version FROM orchestration_schema").fetchall()[0][0] == 2
        db.execute("UPDATE orchestration_schema SET version=1")


def test_opt_in_refresh_keeps_prior_evidence_respects_cooldown_and_request_budget(h):
    h.prepare(automatic_preflight_refresh=True, preflight_freshness_s=1, max_requests_per_capture=2)
    h.qualify()
    h.tick()
    first = h.wait("preflight_parameters")
    h.service._save("request", first | {"finished_monotonic_ns": str(time.monotonic_ns() - 2_000_000_000)})
    h.tick()
    assert len(h.rows()) == 1  # stale, but below the minimum refresh cooldown
    first = h.rows()[0]
    h.service._save("request", first | {"finished_monotonic_ns": str(time.monotonic_ns() - 31_000_000_000)})
    h.qualify()
    h.tick()
    refreshed = h.wait("preflight_parameters")
    assert refreshed["id"] != first["id"] and refreshed["anchor"] == first["anchor"]
    assert refreshed["refresh_attempt"] == 1
    assert h.service._get("request", first["id"])["acquisition_id"] == first["acquisition_id"]
    h.service._save("request", refreshed | {"finished_monotonic_ns": str(time.monotonic_ns() - 31_000_000_000)})
    h.qualify()
    h.tick()
    assert len(h.rows()) == 2
    assert "budget reached" in h.service._get("run", h.capture["uuid"])["reason"]


def test_actual_engine_rpc_policy_lifecycle_and_restart(tmp_path):
    """Actual recorder subprocess plus UDP peer; every aircraft value is synthetic."""
    import socket
    from q22_engine.acquisition import DEFAULT_LIMITS
    from q22_engine.__main__ import Engine

    peer = Peer()
    engine = Engine(tmp_path / "engine")
    sequence = 0

    def rpc(method, params):
        nonlocal sequence
        sequence += 1
        reply = engine.handle({"jsonrpc": "2.0", "id": sequence, "method": method, "params": params})
        assert "error" not in reply, reply
        return reply["result"]

    def wait_request(role, state="complete"):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = rpc("orchestration.status", {"capture_session_id": capture["uuid"]})["requests"]
            found = next((row for row in rows if row["role"] == role and row["state"] == state), None)
            if found:
                return found
            failures = [row for row in rows if row["role"] == role
                        and row["state"] in {"partial", "failed", "interrupted", "cancelled"}]
            if failures:
                raise AssertionError({"role": role, "requests": failures,
                    "peer_alive": peer.thread.is_alive(), "peer_error": repr(peer.error),
                    "recorder": engine.collection.recorder.status()})
            time.sleep(.1)
        raise AssertionError({"role": role, "requests": rows, "peer_alive": peer.thread.is_alive(),
            "peer_error": repr(peer.error), "recorder": engine.collection.recorder.status()})

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserved:
            reserved.bind(("127.0.0.1", 0))
            observer_port = reserved.getsockname()[1]

        def mirrored_send(message):
            payload = message.pack(peer.encoder)
            peer.encoder.seq = (peer.encoder.seq + 1) % 256
            peer.socket.sendto(payload, peer.target)
            peer.socket.sendto(payload, ("127.0.0.1", observer_port))

        peer.send = mirrored_send
        peer.status = paced_policy_status(peer)
        policy = {"aircraft_id": "SYNTHETIC RPC POLICY", "connection": peer.connection,
            "synthetic": False, "preflight_parameters": True, "postflight_parameters": True,
            "log_retrieval": "selected_after_stop", "confirm_log_transfer_side_effects": True,
            # Exercise shipped freshness/boot/bootstrap gates. Focused tests
            # above separately use short budgets to verify stale revocation.
            "limits": {"timeout_s": 20, "retry_wait_s": .25,
                       "request_interval_s": .02, "settle_s": .08, "retries": 2}}
        saved = rpc("orchestration.policy.save", {"policy": policy, "operation_id": str(uuid4())})["policy"]
        for key in ("heartbeat_timeout_s", "boot_timeout_s", "bootstrap_timeout_s", "identity_timeout_s"):
            assert saved["policy"]["limits"][key] == DEFAULT_LIMITS[key]
        profile = {"aircraft_id": policy["aircraft_id"], "aircraft_profile_version": "synthetic-v1",
            "connection_profile_version": "synthetic-v1", "connection": {"local_address": "127.0.0.1",
            "listen_port": observer_port, "expected_sender": "127.0.0.1", "system_id": 1, "component_id": 1,
            "expected_uid": "123", "feed_origin": "live_forwarded", "synthetic": False}}
        preview = rpc("session.preview", profile)
        arguments = {**preview["profile"], "operation_id": str(uuid4()), "target_profile_version": "synthetic-v1",
            "applicability_fingerprint": preview["applicability_fingerprint"], "acquisition_policy": {
                "policy_id": saved["id"], "policy_fingerprint": saved["policy_fingerprint"],
                "confirm_automatic_readback": True, "confirm_experimental_adapter": True,
                "confirm_log_transfer_side_effects": True}}
        capture = rpc("session.prepare_and_watch", arguments)["capture_session"]
        preflight = wait_request("preflight_parameters")
        assert preflight["acquisition_id"] and preflight["historical_applicability"] == "unknown"
        assert rpc("session.prepare_and_watch", arguments)["operation_replayed"]
        peer.armed = True
        deadline = time.monotonic() + 5
        while not rpc("session.status", {"capture_session_id": capture["uuid"]})["flight_records"]:
            assert time.monotonic() < deadline
            time.sleep(.03)
        peer.armed = False
        postflight = wait_request("postflight_parameters")
        assert postflight["flight_record_id"]
        stopped = rpc("session.stop", {"capture_session_id": capture["uuid"], "operation_id": str(uuid4()),
            "target_profile_version": "synthetic-v1", "applicability_fingerprint": capture["applicability_fingerprint"]})
        listed = wait_request("log_listing")
        intent = wait_request("onboard_log", "waiting")
        selected = rpc("orchestration.retrieval.select", {"request_id": intent["id"], "listing_id": listed["acquisition_id"],
            "log_id": 7, "confirm_selected_log_closed": True, "operation_id": str(uuid4())})["request"]
        assert selected["job_id"]
        downloaded = wait_request("onboard_log")
        assert downloaded["acquisition_id"] and downloaded["association_state"] == "provisional"
        assert rpc("session.detail", {"flight_record_id": intent["flight_record_id"]})["manifest"]["collection_completeness"]["state"] != "complete"
        engine.close()
        sent = len(peer.requests)
        engine = Engine(tmp_path / "engine")
        restored = rpc("orchestration.status", {"capture_session_id": capture["uuid"]})
        assert restored["requests"] and all(not run["runtime_authorized"] for run in restored["runs"])
        assert len(peer.requests) == sent
    finally:
        engine.close()
        peer.close()
