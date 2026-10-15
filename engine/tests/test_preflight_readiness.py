"""Fresh-install and arm-cycle regressions using synthetic loopback aircraft.

No historical tuning files are supplied. The real acquisition service must read
the current peer's parameters, retain its own evidence, and never treat an earlier
arm cycle's readback as the next flight's current configuration.
"""
from copy import deepcopy
import time

import pytest

from test_orchestration import Harness


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / "fresh-install")
    yield harness
    harness.close()


def readiness(h):
    return h.service.dispatch("orchestration.status", {
        "capture_session_id": h.capture["uuid"]})["runs"][0]["preflight_readiness"]


def complete_preflight(h):
    h.qualify()
    h.tick()
    result = h.wait("preflight_parameters")
    h.tick()
    return result


def test_fresh_install_reads_current_parameters_without_historical_baselines(h):
    with h.store._connect() as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    h.prepare()
    assert readiness(h)["state"] == "waiting_for_live_evidence"
    assert not h.peer.requests

    result = complete_preflight(h)
    status = readiness(h)
    assert status["current_for_next_flight"]
    assert status["request_id"] == result["id"]
    assert status["decoded_count"] == status["expected_count"] == 2
    assert status["completeness"] == "complete_for_scope"
    assert status["historical_applicability"] == "unknown"
    acquisition = h.store.get_document("acquisition", result["acquisition_id"])
    assert not acquisition["parameter_capture"]["atomic"]
    assert acquisition["parameter_capture"]["flight_time_applicability"] == "unknown"
    assert any(source["role"] == "parameter_readback_snapshot" for source in acquisition["source_refs"])
    assert {message.get_type() for message in h.peer.requests} <= {"COMMAND_LONG", "PARAM_REQUEST_LIST", "PARAM_REQUEST_READ"}


def test_rearm_without_intermediate_monitor_tick_requires_a_new_readback(h):
    h.prepare()
    first = complete_preflight(h)
    original = deepcopy(h.store.get_document("acquisition", first["acquisition_id"]))
    # Both transitions can arrive between two monitor ticks. The next disarmed
    # heartbeat must not make the prior cycle's complete readback eligible again.
    h.emit(armed=True)
    h.emit()
    assert not readiness(h)["current_for_next_flight"]
    h.peer.parameters[0] = (b"TEST_A", 3.5, 9)
    h.tick()
    assert next(item for item in h.rows() if item["id"] == first["id"])["freshness"] == "invalidated"
    post = h.wait("postflight_parameters")
    assert post["flight_record_id"]
    h.emit()
    h.tick()
    second = h.wait("preflight_parameters")
    h.tick()
    assert second["id"] != first["id"]
    assert second["acquisition_id"] != first["acquisition_id"]
    assert second["operation_id"] != first["operation_id"]
    assert second["anchor"].startswith("after:")
    assert readiness(h)["request_id"] == second["id"]
    assert readiness(h)["current_for_next_flight"]
    current = h.store.get_document("acquisition", second["acquisition_id"])
    assert current["parameter_capture"]["typed_hash"] != original["parameter_capture"]["typed_hash"]
    assert h.store.get_document("acquisition", first["acquisition_id"]) == original


def test_completed_readback_crossing_observed_arm_cycle_is_never_current(h):
    h.prepare(postflight_parameters=False)
    h.qualify()

    def cross_cycle(_message):
        # The independent recorder observes a rapid arm/disarm that the policy
        # timer has not yet inspected. The request socket stays disarmed in this
        # fixture so the observer continuity guard is tested independently.
        h.emit(armed=True)
        h.emit()
        for index in range(len(h.peer.parameters)):
            h.peer.value(index)

    h.peer.readback_hook = cross_cycle
    h.tick()
    result = h.wait("preflight_parameters")
    assert result["state"] == "complete"  # Bytes are retained, not relabelled.
    assert result["revoked"] and result["freshness"] == "invalidated"
    assert not readiness(h)["current_for_next_flight"]
    assert "arm cycle" in result["reason"]
    assert h.recorder.status()["active"]


def test_partial_current_readback_reports_coverage_and_is_not_retried(h):
    h.prepare()
    h.qualify()
    h.peer.omit_index = 1
    h.peer.on_request = lambda message: message.get_type() == "PARAM_REQUEST_READ"
    h.tick()
    result = h.wait("preflight_parameters", "partial")
    status = readiness(h)
    assert status["state"] == "partial"
    assert not status["current_for_next_flight"]
    assert status["decoded_count"] == 1 and status["expected_count"] == 2
    sent = len(h.peer.requests)
    h.peer.omit_index = h.peer.on_request = None
    h.emit()
    h.tick()
    assert len(h.rows("preflight_parameters")) == 1
    assert h.rows("preflight_parameters")[0]["acquisition_id"] == result["acquisition_id"]
    assert len(h.peer.requests) == sent


def test_expired_current_readback_has_actionable_status_without_reusing_it(h):
    h.prepare()
    result = complete_preflight(h)
    current = h.service._get("request", result["id"])
    h.service._save("request", current | {"finished_monotonic_ns": str(time.monotonic_ns() - 61_000_000_000)})
    status = readiness(h)
    assert status["state"] == "stale"
    assert not status["current_for_next_flight"]
    assert "new Prepare & watch" in status["reason"]
    h.tick()
    assert h.rows("preflight_parameters")[0]["freshness"] == "invalidated"


def test_telemetry_only_policy_cannot_claim_current_full_parameters(h):
    h.prepare(preflight_parameters=False, postflight_parameters=False)
    h.qualify()
    h.tick()
    assert readiness(h)["state"] == "not_enabled"
    assert not readiness(h)["current_for_next_flight"]
    assert not h.peer.requests
