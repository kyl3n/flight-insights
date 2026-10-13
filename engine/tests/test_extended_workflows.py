"""Real local job/report integration using explicitly synthetic archived logs."""
from copy import deepcopy
from pathlib import Path
import time

import pytest

from q22_engine.__main__ import Engine
from test_acquisition import Peer, params as acquisition_params
from test_collection import arm, envelope, event, prepare, profile


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "logs"


def finished(engine, job):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = engine.call("jobs.get", {"job_id": job["id"]})["job"]
        if result["state"] not in {"queued", "running"}:
            assert result["state"] == "succeeded", result
            return result["result"]
        time.sleep(.02)
    raise AssertionError("Synthetic integration job missed its deadline")


def test_import_insights_persistence_and_explicit_review_export(tmp_path):
    engine = Engine(tmp_path / "catalog")
    try:
        log = finished(engine, engine.call("logs.import", {"path": str(FIXTURES / "synthetic-flight.bin"),
                       "label": "Synthetic integration", "synthetic": True})["job"])["log"]
        report = finished(engine, engine.call("insights.report", {
            "log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
            "interval_start_s": 1, "interval_end_s": 2.5, "max_gap_s": 1.5, "battery_id": "0",
            "operating_profile": {"aircraft_mass_kg": 20, "payload_mass_kg": 4}})["job"])["report"]
        assert report["metrics"]["mean_power_w"]["value"] == pytest.approx(240)
        assert report["scope"]["synthetic"] is True
        assert engine.call("insights.list", {})["reports"][0]["id"] == report["id"]
    finally:
        engine.close()
    restarted = Engine(tmp_path / "catalog")
    try:
        assert restarted.call("insights.detail", {"report_id": report["id"]})["report"] == report
        preview = restarted.call("insights.review_packet", {"report_id": report["id"]})
        assert preview["packet"]["scope"]["synthetic"] is True
        exported = restarted.call("insights.export_review", {"report_id": report["id"], "folder": str(tmp_path)})
        assert Path(exported["path"]).is_file()
        assert exported["byte_size"] == preview["byte_size"]
        assert restarted.call("jobs.list", {})["jobs"][0]["state"] == "succeeded"
    finally:
        restarted.close()


def test_active_capture_gate_requires_disarmed_uid_and_separate_socket(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    connection = {"local_address": "127.0.0.1", "local_port": 14552,
                  "system_id": 1, "component_id": 1, "expected_uid": "123"}
    current = {"state": "recording", "active": True, "capture_session_id": "fixture",
               "bindings": [{"local_address": "127.0.0.1", "port": 14551}],
               "sources": [{"system_id": 1, "component_id": 1, "uid": "123", "armed_state": "disarmed"}]}
    status = deepcopy(current)
    monkeypatch.setattr(engine.collection.recorder, "status", lambda: status)
    monkeypatch.setattr(engine.collection.recorder, "recovered", lambda: [])
    monkeypatch.setattr(engine.collection.collection, "safety_state", lambda *_: {"armed_state": "disarmed"})
    try:
        assert engine._acquisition_gate("parameters", connection)["allowed"] is True
        assert engine._acquisition_gate("log_list", connection)["allowed"] is False
        assert engine._acquisition_gate("parameters", {**connection, "local_port": 14551})["allowed"] is False
        status["sources"][0]["armed_state"] = "unknown"
        assert engine._acquisition_gate("parameters", connection)["allowed"] is False
        status["sources"][0]["armed_state"] = "armed"
        assert engine._acquisition_gate("parameters", connection)["allowed"] is False
        status = deepcopy(current)
        status["sources"][0]["uid"] = "456"
        assert engine._acquisition_gate("parameters", connection)["allowed"] is False
        status = deepcopy(current)
        status["sources"].append(deepcopy(status["sources"][0]))
        assert engine._acquisition_gate("parameters", connection)["allowed"] is False
        status = {"active": False}
        monkeypatch.setattr(engine.collection.recorder, "recovered", lambda: [{"active": True, "capture_session_id": "other"}])
        assert engine._acquisition_gate("parameters", connection)["allowed"] is False
    finally:
        monkeypatch.undo()
        engine.close()


def test_explicit_readback_download_and_index_are_real_persistent_jobs(tmp_path):
    engine = Engine(tmp_path / "catalog")
    peer = Peer()
    peer.logs = {7: (FIXTURES / "synthetic-flight.bin").read_bytes()}
    try:
        preview = engine.call("acquisition.preview", {"connection": peer.connection})
        assert preview["applicability_fingerprint"]
        arguments = acquisition_params(engine.acquisition, peer, role="preflight")
        acquired = finished(engine, engine.call("acquisition.parameters.capture", arguments)["job"])["acquisition"]
        assert acquired["state"] == "complete"
        assert acquired["parameter_capture"]["flight_time_applicability"] == "unknown"
        listing = finished(engine, engine.call("acquisition.logs.list", acquisition_params(
            engine.acquisition, peer, confirm_log_transfer_side_effects=True))["job"])["acquisition"]
        download = finished(engine, engine.call("acquisition.logs.download", acquisition_params(
            engine.acquisition, peer, listing_id=listing["id"], log_id=7,
            confirm_log_transfer_side_effects=True))["job"])["acquisition"]
        assert download["download"]["transfer_complete"]
        source = next(item for item in download["source_refs"] if item["role"] == "onboard_log_candidate")
        assert engine.store.raw_path(source["sha256"]).read_bytes() == peer.logs[7]
        collection = engine.collection.collection
        chosen = profile(expected_uid="123")
        capture_id, _ = prepare(collection, chosen)
        collection.ingest_event(capture_id, event(0, kind="identity", uid="123", boot_epoch="0"))
        flight = arm(collection, capture_id, 1)
        result = finished(engine, engine.call("reconciliation.index_source", {
            "source_id": source["id"], "source_kind": "log", "synthetic": True,
            "flight_record_id": flight["id"], **envelope(collection, chosen)})["job"])["report"]
        assert result["imported_count"] == 1 and result["verified_candidate_count"] == 0
        assert result["bundle_completeness"] == "not_established"
        assert collection.detail({"flight_record_id": flight["id"]})["associations"] == []
    finally:
        peer.close()
        engine.close()
    restarted = Engine(tmp_path / "catalog")
    try:
        assert restarted.call("acquisition.detail", {"acquisition_id": download["id"]})["acquisition"] == download
        assert restarted.call("reconciliation.get", {"report_id": result["id"]})["report"] == result
    finally:
        restarted.close()
