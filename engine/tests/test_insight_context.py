"""Synthetic wire evidence proves conservative linkage, not hardware behavior."""
from concurrent.futures import CancelledError
from copy import deepcopy

import pytest

from q22_engine.alignment import AlignmentService
from q22_engine.flight_environment import EnvironmentDetector
from q22_engine.insight_context import attach_flight_context
from q22_engine.logs import LogService
from q22_engine.store import Store
from test_alignment import UTC, analyzed, imported, managed, services
from test_collection import envelope


def report_scope(services, selection, start=4, end=8):
    log = services[0].get_document("log", selection["log_id"])
    stream = LogService.select_stream(log, selection)
    return {"scope": {**selection, "source_sha256": log["source"]["sha256"], "synthetic": log["synthetic"],
                      "time_domain": stream["time_domain"], "origin_time_us": stream["origin_time_us"],
                      "interval_start_s": start, "interval_end_s": end}}


def linked(services, tmp_path, **wire):
    reference = imported(services, tmp_path, "reference.tlog", **wire)
    selected = imported(services, tmp_path, "candidate.tlog", tag=4, **wire)
    profile, capture, records = managed(services, reference)
    for record in records:
        saved = analyzed(services, reference, selected, flight_record_id=record["id"])
        services[3].dispatch("alignment.attach", {"report_id": saved["id"], "flight_record_id": record["id"],
                                                    **envelope(services[2], profile)})
    return selected, records, profile, capture


def test_no_selection_does_not_invent_history_or_initialize_collection(tmp_path, monkeypatch):
    store = Store(tmp_path / "fresh")
    monkeypatch.setattr(EnvironmentDetector, "inspect", lambda *a, **k: pytest.fail("Current host must not be inspected"))
    result = attach_flight_context(store, {"scope": {}})
    assert result["status"] == "unavailable" and "No managed flight selected" in result["reason"]
    assert "environment_snapshot" not in result and "hardware_uid" not in result
    with store._connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='collection_versions'").fetchone() is None


def test_exact_link_copies_recorded_environment_without_writes_or_detection(services, tmp_path, monkeypatch):
    selected, records, _, _ = linked(services, tmp_path)
    record = services[2].detail({"flight_record_id": records[0]["id"]})["flight_record"]
    report = report_scope(services, selected)
    untouched = deepcopy(report)
    with services[0]._connect() as db:
        before = db.execute("SELECT count(*) FROM documents").fetchone()[0]
        versions = db.execute("SELECT count(*) FROM collection_versions").fetchone()[0]
    monkeypatch.setattr(EnvironmentDetector, "inspect", lambda *a, **k: pytest.fail("Current host must not be inspected"))
    result = attach_flight_context(services[0], report, record["id"])
    assert result["status"] == "verified", result
    assert result["hardware_uid"] == "123" and result["aircraft_id"]
    assert result["environment_snapshot"] == record["environment_snapshot"]
    assert result["linkage"]["scope"] == {key: report["scope"][key] for key in (
        "source_sha256", "source_key", "boot_epoch", "interval_start_s", "interval_end_s")}
    assert result["linkage"]["record_time_bounds"]["time_domain"] == "utc_receive_us"
    assert report == untouched
    with services[0]._connect() as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM collection_versions").fetchone()[0] == versions


@pytest.mark.parametrize("change", [
    {"source_sha256": "0" * 64}, {"source_key": "2:1"}, {"boot_epoch": 1},
    {"origin_time_us": "0"}, {"time_domain": "boot_us"}, {"synthetic": True},
    {"interval_start_s": 0}, {"interval_end_s": 12}, {"interval_end_s": float("nan")},
])
def test_mismatched_source_clock_or_report_extent_never_gets_context(services, tmp_path, change):
    selected, records, _, _ = linked(services, tmp_path)
    report = report_scope(services, selected)
    report["scope"].update(change)
    result = attach_flight_context(services[0], report, records[0]["id"])
    assert result["status"] == "unavailable", result
    assert "environment_snapshot" not in result


def test_same_original_other_flight_and_cross_flight_interval_are_rejected(services, tmp_path):
    selected, records, _, _ = linked(services, tmp_path, arms=((2, 4), (8, 11)))
    first, second = sorted(records, key=lambda r: r["display_number"])
    report = report_scope(services, selected, 2.5, 3.5)
    assert attach_flight_context(services[0], report, first["id"])["status"] == "verified"
    assert attach_flight_context(services[0], report, second["id"])["status"] == "unavailable"
    crossing = report_scope(services, selected, 2.5, 10)
    assert attach_flight_context(services[0], crossing, first["id"])["status"] == "unavailable"
    assert attach_flight_context(services[0], crossing, second["id"])["status"] == "unavailable"


def test_current_rejection_wins_over_previously_verified_alignment(services, tmp_path):
    selected, records, profile, _ = linked(services, tmp_path)
    record = records[0]
    report = report_scope(services, selected)
    verified = attach_flight_context(services[0], report, record["id"])
    services[2].review_association({"association_id": verified["linkage"]["association_id"], "decision": "reject",
                                     "reason": "Operator rejected this candidate", **envelope(services[2], profile)})
    assert attach_flight_context(services[0], report, record["id"])["status"] == "unavailable"


def test_later_competing_original_invalidates_previously_verified_link(services, tmp_path):
    selected, records, _, _ = linked(services, tmp_path)
    imported(services, tmp_path, "new-competitor.tlog", tag=88)
    result = attach_flight_context(services[0], report_scope(services, selected), records[0]["id"])
    assert result["status"] == "unavailable" and "no longer uniquely verified" in result["reason"]


def test_active_capture_runtime_update_does_not_replace_historical_context(services, tmp_path, monkeypatch):
    selected, records, _, capture_id = linked(services, tmp_path)
    analyze = AlignmentService.analyze

    def with_runtime_update(self, *args, **kwargs):
        result = analyze(self, *args, **kwargs)
        with services[0]._connect() as db:
            capture = services[2]._get(db, "capture", capture_id)
            capture["latest_event"] = {"kind": "heartbeat", "received_utc_us": "9999999999999999"}
            services[2]._put(db, "capture", capture)
        return result

    monkeypatch.setattr(AlignmentService, "analyze", with_runtime_update)
    result = attach_flight_context(services[0], report_scope(services, selected), records[0]["id"])
    assert result["status"] == "verified", result


@pytest.mark.parametrize("change", ["incomplete_archive", "boundary_record", "coverage_gap"])
def test_changed_managed_membership_cannot_inherit_prior_alignment(services, tmp_path, change):
    selected, records, _, capture_id = linked(services, tmp_path)
    with services[0]._connect() as db:
        record = services[2]._get(db, "flight_record", records[0]["id"])
        interval = services[2]._get(db, "interval", record["armed_interval_id"])
        if change == "incomplete_archive":
            capture = services[2]._get(db, "capture", capture_id)
            capture["artifact_sources"][0]["coverage"]["complete_chunk"] = False
            services[2]._put(db, "capture", capture)
        else:
            if change == "boundary_record":
                interval["arm_evidence"]["record_index"] += 1
            else:
                interval["coverage_warnings"].append("Observed telemetry gap")
            services[2]._put(db, "interval", interval)
    result = attach_flight_context(services[0], report_scope(services, selected), records[0]["id"])
    assert result["status"] == "unavailable", result


def test_onboard_boot_domain_uses_exact_armed_records_and_acquisition_identity(services, tmp_path):
    reference = imported(services, tmp_path, "reference.tlog")
    lines = ["FMT, 130, 19, GPS, QBHIB, TimeUS,I,GWk,GMS,Status", "FMT, 131, 12, ARM, QB, TimeUS,ArmState"]
    for second in range(14):
        utc = UTC + second * 1_000_000 + 100_000
        week, millis = divmod((utc - 315964800000000 + 18000000) // 1000, 604800000)
        lines.extend([f"GPS, {100100000 + second * 1000000}, 0, {week}, {millis}, 3",
                      f"ARM, {100200000 + second * 1000000}, {int(3 <= second < 9)}"])
    path = tmp_path / "onboard.log"
    path.write_text("\n".join(lines) + "\n")
    log = services[1].import_log({"path": str(path), "label": "Synthetic onboard oracle", "synthetic": False})["log"]
    selected = {"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0}
    gate = {"identity": {"uid": "123", "firmware_identity": {
        "flight_sw_version": 0x040600ff, "board_version": 9, "vendor_id": 1, "product_id": 2}}}
    services[0].add_document("acquisition", {"kind": "log_download", "state": "complete", "synthetic": False,
        "transport_evidence": {"initial_gate_observations": gate, "final_gate_observations": gate},
        "download": {"transfer_complete": True, "sha256": log["source"]["sha256"], "reported_size": path.stat().st_size,
                     "contiguous_prefix_bytes": path.stat().st_size, "eof_evidence": {"synthetic": True},
                     "before_entry_evidence": {"synthetic": True}, "after_entry_evidence": {"synthetic": True}},
        "source_refs": [log["source"] | {"role": "onboard_log_candidate"}]})
    profile, _, records = managed(services, reference)
    record = records[0]
    saved = analyzed(services, reference, selected, flight_record_id=record["id"])
    services[3].dispatch("alignment.attach", {"report_id": saved["id"], "flight_record_id": record["id"],
                                              **envelope(services[2], profile)})
    result = attach_flight_context(services[0], report_scope(services, selected, 104, 108), record["id"])
    assert result["status"] == "verified", result
    assert result["linkage"]["record_time_bounds"] == {
        "time_domain": "flight_controller_boot", "origin_time_us": "0", "start_time_us": "103200000", "end_time_us": "109200000"}


def test_missing_recording_snapshot_stays_unknown_after_exact_link(services, tmp_path, monkeypatch):
    selected, records, _, _ = linked(services, tmp_path)
    with services[0]._connect() as db:
        record = services[2]._get(db, "flight_record", records[0]["id"])
        interval = services[2]._get(db, "interval", record["armed_interval_id"])
        record.pop("environment_snapshot")
        interval.pop("environment_snapshot")
        services[2]._put(db, "flight_record", record)
        services[2]._put(db, "interval", interval)
    monkeypatch.setattr(EnvironmentDetector, "inspect", lambda *a, **k: pytest.fail("No historical snapshot replacement"))
    result = attach_flight_context(services[0], report_scope(services, selected), record["id"])
    assert result["status"] == "verified", result
    assert result["environment_snapshot"]["status"] == "unavailable"
    assert result["environment_snapshot"]["observed_at"] is None


def test_tampered_snapshot_content_cannot_be_presented_as_verified_history(services, tmp_path):
    selected, records, _, _ = linked(services, tmp_path)
    with services[0]._connect() as db:
        record = services[2]._get(db, "flight_record", records[0]["id"])
        interval = services[2]._get(db, "interval", record["armed_interval_id"])
        for value in (record, interval):
            value["environment_snapshot"]["host"]["version"] = "forged"
        services[2]._put(db, "flight_record", record)
        services[2]._put(db, "interval", interval)
    result = attach_flight_context(services[0], report_scope(services, selected), record["id"])
    assert result["status"] == "unavailable" and "integrity" in result["reason"]


def test_changed_original_and_cancellation_are_respected(services, tmp_path):
    selected, records, _, _ = linked(services, tmp_path)
    report = report_scope(services, selected)
    with pytest.raises(CancelledError):
        attach_flight_context(services[0], report, records[0]["id"], cancel_check=lambda: True)
    raw = services[0].raw_path(report["scope"]["source_sha256"])
    raw.chmod(0o600)
    raw.write_bytes(raw.read_bytes() + b"changed")
    result = attach_flight_context(services[0], report, records[0]["id"])
    assert result["status"] == "unavailable" and "SHA-256" in result["reason"]


def test_reanalysis_requires_supported_clock_evidence(services, tmp_path, monkeypatch):
    selected, records, _, _ = linked(services, tmp_path)
    analyze = AlignmentService.analyze

    def unsupported(self, *args, **kwargs):
        result = analyze(self, *args, **kwargs)
        result["candidates"][0]["evidence"]["clock_model"]["state"] = "unavailable"
        return result

    monkeypatch.setattr(AlignmentService, "analyze", unsupported)
    result = attach_flight_context(services[0], report_scope(services, selected), records[0]["id"])
    assert result["status"] == "unavailable" and "clock" in result["reason"]
