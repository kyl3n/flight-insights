"""Explicitly synthetic offline folder recovery; no aircraft/hardware evidence."""
from concurrent.futures import CancelledError
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from q22_engine.collection import CollectionService
from q22_engine.reconciliation import ReconciliationService, _Evidence, _canonical_decimal
from q22_engine.store import Store
from test_collection import arm, envelope, event, prepare, profile


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/logs"


@pytest.fixture
def prepared(tmp_path):
    store = Store(tmp_path / "catalog")
    collection = CollectionService(store)
    chosen = profile(expected_uid="123")
    capture_id, _ = prepare(collection, chosen)
    collection.ingest_event(capture_id, event(0, kind="identity", uid="123", boot_epoch="0",
        firmware_identity={"flight_sw_version": 1, "board_version": 2}))
    record = arm(collection, capture_id, 1)
    collection.ingest_event(capture_id, event(3))
    folder = tmp_path / "SD exports"
    folder.mkdir()
    service = ReconciliationService(store, collection=collection)
    params = {"folder": str(folder), "flight_record_id": record["id"], "synthetic": True,
              **envelope(collection, chosen)}
    return service, folder, params


def test_folder_import_preserves_bytes_and_separate_source_boot_candidates(prepared):
    service, folder, params = prepared
    for extension in ("bin", "log", "tlog"):
        shutil.copyfile(FIXTURES / f"synthetic-flight.{extension}", folder / f"anything.{extension.upper()}")
    (folder / "wrong-flight-name.param").write_text("ATC_RAT_RLL_P,0.1000\n")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    report = service.dispatch("reconciliation.scan_folder", params)["report"]
    assert report["state"] == "completed" and report["imported_count"] == 4
    assert report["verified_candidate_count"] == 0 and report["bundle_completeness"] == "not_established"
    assert all(c["state"] == "provisional" for c in report["candidates"])
    tlog = [c for c in report["candidates"] if c.get("format") == "tlog"]
    assert {(c["source_key"], c["boot_epoch"]) for c in tlog} == {("1:1", 0), ("2:1", 0), ("1:1", 1)}
    for item in report["files"]:
        source = item["source"]
        assert source["sha256"] == hashlib.sha256(before[item["original_name"]]).hexdigest()
        assert service.store.raw_path(source["sha256"]).read_bytes() == before[item["original_name"]]
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    detail = service.collection.detail({"flight_record_id": params["flight_record_id"]})
    assert detail["associations"] == []
    assert detail["manifest"]["collection_completeness"]["state"] == "partial"


def test_parameter_reference_never_invents_identity_or_flight_time(prepared):
    service, folder, params = prepared
    (folder / "current.param").write_text("ATC_RAT_RLL_P,0.1000\n")
    report = service.scan_folder(params)["report"]
    reference = service.store.get_document("parameter_reference", report["files"][0]["parameter_reference_id"])
    assert reference["identity"] is None and reference["observed_at"] is None
    assert reference["completeness"] == reference["flight_time_applicability"] == "unknown"
    assert reference["synthetic"] is True and reference["source_kind"] == "file_export_reference"
    assert _canonical_decimal("0.10000") == _canonical_decimal("1e-1")
    assert _canonical_decimal("1234567890123456789012345678901") != _canonical_decimal("1234567890123456789012345678902")


def test_operation_replay_survives_removed_sd_folder_and_rejects_conflict(prepared):
    service, folder, params = prepared
    (folder / "p.param").write_text("TEST_P,1\n")
    original = service.scan_folder(params)["report"]
    shutil.rmtree(folder)
    replay = service.scan_folder(params)
    assert replay["operation_replayed"] and replay["report"]["id"] == original["id"]
    with pytest.raises(ValueError, match="different reconciliation"):
        service.scan_folder({**params, "max_depth": 1})
    assert service.dispatch("reconciliation.get", {"report_id": original["id"]})["report"] == original


def test_recursive_scan_is_explicit_and_symlinks_are_never_followed(prepared, tmp_path):
    service, folder, params = prepared
    child = folder / "logs"
    child.mkdir()
    (child / "inside.param").write_text("TEST_P,1\n")
    outside = tmp_path / "outside.param"
    outside.write_text("TEST_P,2\n")
    try:
        (folder / "link.param").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("Symlink creation unavailable to this Windows account")
    shallow = service.scan_folder(params)["report"]
    assert shallow["imported_count"] == 0 and shallow["skipped_count"] == 1
    deeper = service.scan_folder({**params, "operation_id": str(uuid4()), "max_depth": 1})["report"]
    assert deeper["imported_count"] == 1 and deeper["files"][0]["original_name"] == "inside.param"
    assert all(f["selected_path"] != str(outside) for f in deeper["files"])
    linked = tmp_path / "linked-folder"
    linked.symlink_to(folder, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        service.scan_folder({**params, "operation_id": str(uuid4()), "folder": str(linked)})


def test_bounds_are_visible_and_do_not_silently_call_scan_complete(prepared):
    service, folder, params = prepared
    for i in range(3):
        (folder / f"{i}.param").write_text("TEST_P,1\n")
    report = service.scan_folder({**params, "max_files": 1})["report"]
    assert report["state"] == "partial" and "file_count_limit" in report["limits_reached"]
    assert report["imported_count"] == 1
    report = service.scan_folder({**params, "operation_id": str(uuid4()), "max_total_bytes": 1})["report"]
    assert report["state"] == "partial" and report["imported_count"] == 0
    assert "byte_budget" in report["limits_reached"]


def test_cancelled_scan_has_durable_partial_report_and_no_silent_restart(prepared):
    service, folder, params = prepared
    (folder / "p.param").write_text("TEST_P,1\n")
    with pytest.raises(CancelledError):
        service.scan_folder(params, cancel_check=lambda: True)
    reports = service.dispatch("reconciliation.list", {})["reports"]
    assert reports[0]["state"] == "cancelled"
    replay = service.scan_folder(params)
    assert replay["operation_replayed"] and replay["report"]["state"] == "cancelled"
    assert replay["report"]["files"] == []


def test_decode_failure_retains_original_in_partial_report(prepared, monkeypatch):
    service, folder, params = prepared
    path = folder / "flight.bin"
    path.write_bytes(b"original bytes must remain discoverable")
    def failed(arguments, **kwargs):
        service.store.archive_file(arguments["path"])
        raise ValueError("synthetic decoder failure after archiving")
    monkeypatch.setattr(service.logs, "import_log", failed)
    report = service.scan_folder(params)["report"]
    assert report["state"] == "partial" and report["files"][0]["state"] == "failed"
    assert service.store.raw_path(report["files"][0]["source"]["sha256"]).read_bytes() == path.read_bytes()


def test_scope_envelope_is_checked_before_import(prepared):
    service, folder, params = prepared
    with pytest.raises(ValueError, match="fingerprint"):
        service.scan_folder({**params, "applicability_fingerprint": "stale"})
    with pytest.raises(ValueError, match="profile version"):
        service.scan_folder({**params, "target_profile_version": "old"})
    assert service.dispatch("reconciliation.list", {})["reports"] == []


def target():
    return {"expected_uid": "123", "firmware_observations": [], "arm_evidence": {"received_utc_us": "100"},
            "disarm_evidence": {"received_utc_us": "900"}, "declared_system_id": 1, "declared_component_id": 1}


def reducer():
    return _Evidence({"source_key": "1:1", "boot_epoch": 0, "system_id": 1, "component_id": 1})


def logged(record, message_type, raw, **extra):
    return {"record_index": record, "message_type": message_type, "raw_fields": raw,
            "kind": "identity" if message_type in ("HEARTBEAT", "AUTOPILOT_VERSION") else "event",
            "time_us": 100 + record * 100, "receive_time_us": 100 + record * 100, **extra}


def candidate(value):
    return value.candidate({"id": "synthetic-log", "source": {"sha256": "a" * 64}, "format": "tlog", "status": "decoded", "synthetic": True}, target())


def test_content_uid_contradictions_override_matching_uid_and_time():
    value = reducer()
    value.observe(logged(0, "AUTOPILOT_VERSION", {"uid": 123}))
    value.observe(logged(1, "AUTOPILOT_VERSION", {"uid": 999}))
    result = candidate(value)
    assert result["state"] == "rejected" and result["contradictions"]
    assert result["recorded_receive_overlap"] is True


def test_content_arm_spans_and_reported_anchors_remain_provisional():
    value = reducer()
    value.observe(logged(0, "AUTOPILOT_VERSION", {"uid": 123}))
    for index, flag in enumerate((0, 128, 128, 0, 128), 1):
        value.observe(logged(index, "HEARTBEAT", {"base_mode": flag, "autopilot": 3, "type": 2}))
    value.observe(logged(6, "SYSTEM_TIME", {"time_boot_ms": 1000, "time_unix_usec": 1700000000000000}, boot_time_us=1000000))
    result = candidate(value)
    assert result["state"] == "provisional" and result["independent_flight_match"] is False
    assert [(i["start_record"], i["end_record"]) for i in result["armed_intervals"]] == [(2, 4), (5, None)]
    assert result["reported_clock_anchors"][0]["validation"] == "reported_anchor_not_independently_validated"
    assert any(e["dimension"] == "hardware_uid" for e in result["supporting_evidence"])
    assert result["association_options"][0]["params"] == {"log_id": "synthetic-log", "source_key": "1:1", "boot_epoch": 0,
        "role": "gcs_tlog", "time_or_record_spans": [{"kind": "records", "start": 2, "end": 4}]}
    assert result["association_options"][1]["boundary_status"] == "unknown_end_observed_prefix_only"


def test_matching_systemid_filename_or_clock_cannot_verify_candidate():
    value = reducer()
    value.observe(logged(0, "HEARTBEAT", {"base_mode": 0, "autopilot": 3, "type": 2}))
    result = candidate(value)
    assert result["state"] == "provisional" and not result["independent_flight_match"]
    assert result["observed_uids"] == [] and result["proposed_association"] is None


def test_nonregular_candidate_cannot_block_scan(prepared):
    import os
    service, folder, params = prepared
    if not hasattr(os, "mkfifo"):
        pytest.skip("POSIX FIFO only")
    os.mkfifo(folder / "pipe.bin")
    report = service.scan_folder(params)["report"]
    assert report["imported_count"] == 0 and report["skipped_examples"][0]["reason"] == "not_regular_file"


def test_acquired_source_can_be_indexed_without_a_folder(prepared):
    service, folder, params = prepared
    source = service.store.archive_file(FIXTURES / "synthetic-flight.bin")
    args = {key: value for key, value in params.items() if key != "folder"}
    args.update(source_id=source["id"], source_kind="log")
    report = service.dispatch("reconciliation.index_source", args)["report"]
    assert report["imported_count"] == 1 and report["selected_source_id"] == source["id"]
    assert report["selected_folder"] is None and len(report["candidates"]) == 2
    assert report["files"][0]["parent_source_id"] == source["id"]
    assert service.dispatch("reconciliation.index_source", args)["operation_replayed"]


def test_index_source_refuses_changed_archived_content(prepared):
    service, folder, params = prepared
    path = folder / "params.param"
    path.write_text("TEST_P,1\n")
    source = service.store.archive_file(path)
    raw = service.store.raw_path(source["sha256"])
    raw.chmod(0o600)
    raw.write_text("TEST_P,2\n")
    args = {key: value for key, value in params.items() if key != "folder"}
    args.update(source_id=source["id"], source_kind="parameter")
    with pytest.raises(ValueError, match="original digest"):
        service.dispatch("reconciliation.index_source", args)
    report = service.dispatch("reconciliation.list", {})["reports"][0]
    assert report["state"] == "interrupted" and report["candidate_count"] == 0


def test_corruption_never_becomes_an_exact_observed_arm_boundary():
    value = reducer()
    value.observe(logged(0, "HEARTBEAT", {"base_mode": 0, "autopilot": 3, "type": 2}))
    value.observe(logged(1, "BAD", {}, kind="diagnostic"))
    value.observe(logged(2, "HEARTBEAT", {"base_mode": 128, "autopilot": 3, "type": 2}))
    result = candidate(value)
    assert result["armed_intervals"][0]["start_status"] == "unknown"
    assert result["association_options"][0]["coverage"] == "uncertain"
