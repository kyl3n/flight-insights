"""Automatic library integration, with real synthetic MAVLink/DataFlash sources."""
from concurrent.futures import CancelledError
from pathlib import Path
import struct

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mav

from q22_engine.collection import CollectionService
from q22_engine.flight_library import FlightLibrary
from q22_engine.insight_service import InsightReports
from q22_engine.logs import LogService
from q22_engine.store import Store
from test_collection import prepare, event

UTC = 1760000000000000
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/logs/synthetic-flight.log"


def tlog(path, *, day=0, uid=123, arms=((2, 5),), duration=8, battery_count=1, tag=0, receive_offset=0):
    codec = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    codec.seq = tag
    packets = []
    base = UTC + day * 86400000000

    def packet(stamp, message):
        packets.append(struct.pack(">Q", (base + int(stamp * 1e6) + receive_offset) & ~3) + message.pack(codec))
        codec.seq = (codec.seq + 1) % 256

    packet(0, mav.MAVLink_autopilot_version_message(131072, 0x040600ff, 0, 0, 9, [0]*8, [0]*8, [0]*8, 1, 2, uid))
    for second in range(duration):
        packet(second + .1, mav.MAVLink_system_time_message(base + int((second + .1) * 1e6), int((100 + second + .1) * 1000)))
        armed = any(start <= second < end for start, end in arms)
        packet(second + .2, mav.MAVLink_heartbeat_message(2, 3, 128 if armed else 0, 0, 4, 3))
        packet(second + .3, mav.MAVLink_param_value_message(b"ATC_RAT_RLL_P", .1 + day * .01, 9, 1, 0))
        packet(second + .4, mav.MAVLink_sys_status_message(0, 0, 0, 0, 24000, 1000 + day * 100, 80, 0, 0, 0, 0, 0, 0))
        if battery_count > 1:
            packet(second + .5, mav.MAVLink_battery_status_message(0, 0, 0, 2000, [4000]*6 + [65535]*4, 1000, 0, 0, 80))
    path.write_bytes(b"".join(packets))
    return path


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path / "catalog")
    instance = FlightLibrary(store)
    yield instance
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


def import_flight(library, path, **options):
    result = library.dispatch("library.import", {"path": str(path), "synthetic": True, **options})
    return next(row for row in result["flights"] if row["log_id"] == result["log"]["id"])


def test_import_analyzes_chronology_not_import_order_and_compares_same_observed_uid(library, tmp_path):
    latest = import_flight(library, tlog(tmp_path / "first-import.tlog", day=2))
    previous = import_flight(library, tlog(tmp_path / "later-import.tlog", day=1))
    result = library.dispatch("library.list", {})
    assert [row["id"] for row in result["flights"]] == [latest["id"], previous["id"]]
    assert result["summary"]["analyzed"] == result["summary"]["recent_flights"] == 2
    detail = library.dispatch("library.detail", {"flight_id": latest["id"]})
    assert detail["previous_flight"]["id"] == previous["id"]
    assert detail["comparison"] is not None
    assert detail["comparison_basis"] == "recorded_dates_and_observed_hardware_uid"
    assert detail["report"]["metrics"]["mean_power_w"]["value"] == 288
    assert detail["report"]["flight_context"]["status"] == "unavailable"
    assert any(row["code"] == "aircraft_identity_unresolved" for row in detail["comparison"]["confounders"])
    assert len(detail["recent_history"]) == 1


def test_one_source_with_two_armed_intervals_never_becomes_one_flight(library, tmp_path):
    result = library.import_log({"path": str(tlog(tmp_path / "two.tlog", arms=((2, 5), (7, 10)), duration=12)), "synthetic": True})
    assert result["total"] == 2
    rows = sorted(result["flights"], key=lambda row: row["interval_start_s"])
    assert [(row["interval_start_s"], row["interval_end_s"]) for row in rows] == [(2.2, 5.2), (7.2, 10.2)]
    assert all(row["status"] == "ready" and row["report_id"] for row in rows)
    assert library.detail({"flight_id": rows[1]["id"]})["previous_flight"]["id"] == rows[0]["id"]


def test_unknown_dates_not_filled_from_filename_import_or_host(library):
    row = import_flight(library, FIXTURE, label="2026-01-01 flight", aircraft_id="Q22")
    assert row["started_at"] is None
    assert row["time_basis"] == "unknown"
    assert row["kind"] == "log_segment"
    detail = library.detail({"flight_id": row["id"]})
    assert detail["report"] is not None
    assert detail["comparison"] is None
    assert detail["report"]["flight_context"]["status"] == "unavailable"
    assert library.list_flights({})["summary"]["unknown_dates"] == 2  # separate decoded boot segments


def test_same_system_id_different_hardware_or_synthetic_never_automatically_pair(library, tmp_path):
    import_flight(library, tlog(tmp_path / "older.tlog", day=1, uid=111))
    row = import_flight(library, tlog(tmp_path / "newer.tlog", day=2, uid=222))
    assert library.detail({"flight_id": row["id"]})["comparison"] is None
    library.import_log({"path": str(tlog(tmp_path / "live.tlog", day=0, uid=222)), "synthetic": False})
    assert library.detail({"flight_id": row["id"]})["comparison"] is None


def test_overlap_duplicate_capture_of_same_physical_interval_not_previous_flight(library, tmp_path):
    import_flight(library, tlog(tmp_path / "copy-a.tlog", arms=((2, 6),), tag=0))
    row = import_flight(library, tlog(tmp_path / "copy-b.tlog", arms=((3, 5),), tag=7))
    assert library.detail({"flight_id": row["id"]})["comparison"] is None


def test_duplicate_original_and_refresh_reuse_report_without_creating_another_flight(library, tmp_path):
    path = tlog(tmp_path / "flight.tlog")
    first = import_flight(library, path)
    second = library.import_log({"path": str(path), "synthetic": True})
    assert second["total"] == 1
    assert second["flights"][0]["id"] == first["id"]
    assert second["flights"][0]["report_id"] == first["report_id"]
    result = library.refresh({})
    assert result["indexed_logs"] == result["analyzed_flights"] == result["pending_logs"] == 0
    assert len(library.store.list_documents("insight_report")) == 1


def test_multiple_batteries_require_choice_then_analyze_and_persist_across_refresh(library, tmp_path):
    row = import_flight(library, tlog(tmp_path / "monitors.tlog", battery_count=2))
    assert row["report_id"] is None
    assert row["status"] == "needs_review"
    assert set(row["available_battery_ids"]) == {"sys_status", "battery_status:0"}
    receipt = library.analyze({"flight_id": row["id"], "battery_id": "sys_status"})
    assert receipt["report_id"]
    detail = library.detail({"flight_id": row["id"]})
    assert detail["report"]["metrics"]["mean_power_w"]["value"] == 240
    assert detail["flight"]["status"] == "ready"
    saved = detail["report"]["id"]
    library.refresh({})
    assert library.detail({"flight_id": row["id"]})["report"]["id"] == saved
    with pytest.raises(ValueError, match="recorded"):
        library.analyze({"flight_id": row["id"], "battery_id": "invented"})


def test_missing_arming_boundary_does_not_automatically_compare(library, tmp_path):
    import_flight(library, tlog(tmp_path / "older.tlog", day=1))
    row = import_flight(library, tlog(tmp_path / "already-armed.tlog", day=2, arms=((0, 5),)))
    assert row["status"] == "needs_review"
    detail = library.detail({"flight_id": row["id"]})
    assert detail["report"] is not None
    assert detail["comparison"] is None


def test_explicit_aircraft_name_groups_unknown_uid_only_as_declaration(library, tmp_path):
    import_flight(library, tlog(tmp_path / "old.tlog", day=1, uid=0), aircraft_id="Test UAV")
    row = import_flight(library, tlog(tmp_path / "new.tlog", day=2, uid=0), aircraft_id="Test UAV")
    detail = library.detail({"flight_id": row["id"]})
    assert detail["comparison"] is not None
    assert detail["flight"]["aircraft"]["identity_basis"] == "operator_declared"
    assert "declared by the user" in detail["comparison_reason"]
    assert detail["report"]["flight_context"]["status"] == "unavailable"


def test_month_history_excludes_older_and_other_aircraft(library, tmp_path):
    import_flight(library, tlog(tmp_path / "ancient.tlog", day=0))
    recent = import_flight(library, tlog(tmp_path / "recent.tlog", day=35))
    import_flight(library, tlog(tmp_path / "other.tlog", day=36, uid=456))
    row = import_flight(library, tlog(tmp_path / "newest.tlog", day=40))
    detail = library.detail({"flight_id": row["id"]})
    assert [flight["id"] for flight in detail["recent_history"]] == [recent["id"]]
    assert library.list_flights({})["summary"]["recent_flights"] == 3


def test_old_catalog_logs_auto_index_and_cancelled_refresh_retries(library, tmp_path):
    library.logs.import_log({"path": str(tlog(tmp_path / "existing.tlog")), "label": "existing", "synthetic": True})
    assert library.list_flights({})["summary"]["pending_logs"] == 1
    with pytest.raises(CancelledError):
        library.refresh({}, cancel_check=lambda: True)
    assert library.list_flights({})["total"] == 0
    result = library.refresh({})
    assert result["total"] == 1 and result["analyzed_flights"] == 1


def test_managed_record_without_log_remains_visible(library):
    collection = CollectionService(library.store)
    capture, _ = prepare(collection)
    collection.ingest_event(capture, event(0))
    collection.ingest_event(capture, event(1, True))
    collection.ingest_event(capture, event(2))
    result = library.refresh({})
    assert result["total"] == 1
    row = result["flights"][0]
    assert row["kind"] == "observed_flight" and row["status"] == "awaiting_log"
    assert row["report_id"] is None
    assert library.detail({"flight_id": row["id"]})["report"] is None


@pytest.mark.parametrize("method,params", [("library.list", {"limit": True}), ("library.list", {"offset": -1}),
                                           ("library.refresh", {"max_logs": 51}), ("library.analyze", {"flight_id": "missing"})])
def test_request_bounds(library, method, params):
    with pytest.raises(ValueError):
        library.dispatch(method, params)


def test_interval_settings_survive_rebuild_without_leaking_to_another_interval(library, tmp_path, monkeypatch):
    import q22_engine.flight_library as module
    library.import_log({"path": str(tlog(tmp_path / "two-contexts.tlog", uid=0, arms=((2, 5), (7, 10)), duration=12, battery_count=2)), "synthetic": True})
    rows = library.list_flights({})["flights"]
    selected = rows[0]
    library.analyze({"flight_id": selected["id"], "battery_id": "sys_status", "aircraft_id": "Explicit UAV",
                     "operating_profile": {"aircraft_mass_kg": 12}})
    monkeypatch.setattr(module, "VERSION", "test-rebuild-version")
    library.refresh({})
    detail = library.detail({"flight_id": selected["id"]})
    assert detail["flight"]["aircraft"]["label"] == "Explicit UAV"
    assert detail["report"]["operating_profile"]["aircraft_mass_kg"] == 12
    assert detail["report"]["scope"]["battery_id"] == "sys_status"
    other = library.detail({"flight_id": rows[1]["id"]})
    assert other["report"] is None
    assert other["flight"]["aircraft"]["identity_basis"] == "unknown"


def test_read_detail_reuses_comparison_without_running_analysis(library, tmp_path, monkeypatch):
    import q22_engine.flight_library as module
    import_flight(library, tlog(tmp_path / "before.tlog", day=1))
    after = import_flight(library, tlog(tmp_path / "after.tlog", day=2))
    monkeypatch.setattr(module, "compare_reports", lambda *a, **k: pytest.fail("detail must not run comparison on RPC thread"))
    monkeypatch.setattr(library.reports, "dispatch", lambda *a, **k: pytest.fail("detail must not run reports"))
    assert library.detail({"flight_id": after["id"]})["comparison"] is not None
    assert library.list_flights({})["summary"]["pending_logs"] == 0


def test_analysis_batch_reports_pending_work_until_every_interval_finishes(library, tmp_path):
    arms = tuple((2 + index * 4, 4 + index * 4) for index in range(12))
    result = library.import_log({"path": str(tlog(tmp_path / "many.tlog", arms=arms, duration=52)), "synthetic": True})
    assert result["total"] == 12
    assert result["analyzed_flights"] == 10
    assert result["pending_logs"] > 0
    result = library.refresh({})
    assert result["analyzed_flights"] == 2
    assert result["pending_logs"] == 0
    assert result["summary"]["analyzed"] == 12


def test_exact_managed_context_revalidated_when_competing_original_appears(tmp_path):
    from q22_engine.alignment import AlignmentService
    from test_alignment import imported
    from test_insight_context import linked
    store = Store(tmp_path / "linked")
    logs = LogService(store)
    collection = CollectionService(store)
    alignment = AlignmentService(store, logs, collection)
    services = store, logs, collection, alignment
    selected, records, _, _ = linked(services, tmp_path)
    library = FlightLibrary(store, logs=logs, collection=collection)
    library.refresh({})
    row = next(row for row in library.list_flights({})["flights"] if row.get("log_id") == selected["log_id"])
    detail = library.detail({"flight_id": row["id"]})
    assert detail["report"]["flight_context"]["status"] == "verified", detail["report"]["flight_context"]
    assert row["flight_record_id"] == records[0]["id"]
    old_report = row["report_id"]
    imported(services, tmp_path, "new-competing-original.tlog", tag=88)
    library.refresh({})
    detail = library.detail({"flight_id": row["id"]})
    assert detail["report"]["id"] != old_report
    assert detail["report"]["flight_context"]["status"] == "unavailable"
    assert detail["flight"]["flight_record_id"] is None
    assert any(row["kind"] == "observed_flight" for row in library.list_flights({})["flights"])


def attach_owned_capture(library, path, *, global_offset=100, capture_offset=None):
    from test_collection import profile
    log = library.logs.import_log({"path": str(path), "label": "App telemetry", "synthetic": False})["log"]
    source = library.store.archive_file(path, acquisition_method="app_owned_passive_udp_capture")
    collection = CollectionService(library.store)
    capture, _ = prepare(collection, profile(expected_uid="123"))
    original = library.store.get_document("log", log["id"])
    for row in library.logs.events(original):
        raw = row["raw_fields"]
        index = row["record_index"] + (global_offset if capture_offset is None else capture_offset)
        if row["message_type"] == "AUTOPILOT_VERSION":
            collection.ingest_event(capture, event(index, kind="identity", uid=str(raw["uid"]), boot_epoch="0"))
        elif row["message_type"] == "HEARTBEAT":
            collection.ingest_event(capture, event(index, bool(raw["base_mode"] & 128), boot_epoch="0", received_utc_us=str(row["receive_time_us"])))
    collection.attach_capture_artifacts(capture, [{"source": source, "role": "app_mavlink_capture",
        "record_start": global_offset, "record_end": global_offset + log["record_count"] - 1,
        "coverage": {"complete_chunk": True, "original_chunk": "telemetry-000001.tlog", "record_scope": "capture_global"}}])
    record = collection.status({"capture_session_id": capture})["flight_records"][0]
    return log, record


def test_owned_capture_exact_global_record_membership_groups_without_promoting_parameters(library, tmp_path):
    log, record = attach_owned_capture(library, tlog(tmp_path / "owned.tlog"), global_offset=100)
    result = library.refresh({})
    assert result["total"] == 1
    row = result["flights"][0]
    assert row["log_id"] == log["id"] and row["flight_record_id"] == record["id"]
    assert row["context_linkage"] == "capture_provenance"
    detail = library.detail({"flight_id": row["id"]})
    assert detail["observed_context"]["basis"] == "capture_provenance"
    assert detail["observed_context"]["environment_snapshot"] == record["environment_snapshot"]
    assert detail["report"]["flight_context"]["status"] == "unavailable"
    assert detail["report"]["parameter_evidence"]["completeness"] == "observed_only"
    assert detail["report"]["parameter_evidence"]["attribution_allowed"] is False


def test_same_uid_other_capture_wrong_global_membership_does_not_group(library, tmp_path):
    log, record = attach_owned_capture(library, tlog(tmp_path / "wrong-records.tlog"), global_offset=100, capture_offset=900)
    result = library.refresh({})
    assert result["total"] == 2
    row = next(item for item in result["flights"] if item.get("log_id") == log["id"])
    assert row["flight_record_id"] is None
    assert library.detail({"flight_id": row["id"]})["observed_context"] is None


def test_corrupted_owned_original_revokes_display_membership(library, tmp_path):
    log, _ = attach_owned_capture(library, tlog(tmp_path / "corrupted.tlog"))
    result = library.refresh({})
    assert result["total"] == 1
    original = library.store.raw_path(log["source"]["sha256"])
    original.chmod(0o600)
    original.write_bytes(original.read_bytes() + b"corruption")
    result = library.refresh({})
    assert result["total"] == 2
    row = next(item for item in result["flights"] if item.get("log_id") == log["id"])
    assert row["flight_record_id"] is None
    assert library.detail({"flight_id": row["id"]})["observed_context"] is None


def test_detail_explicitly_bounds_large_combined_response_and_job_receipt(library, tmp_path, monkeypatch):
    import json
    import q22_engine.flight_library as module
    import_flight(library, tlog(tmp_path / "size-before.tlog", day=1))
    after = import_flight(library, tlog(tmp_path / "size-after.tlog", day=2))
    detail = library.detail({"flight_id": after["id"]})
    normal_size = len(json.dumps(detail).encode())
    monkeypatch.setattr(module, "MAX_DETAIL_BYTES", normal_size - 100)
    bounded = library.detail({"flight_id": after["id"]})
    assert bounded["comparison"] is None and bounded["detail_omitted_reason"]
    assert len(json.dumps(bounded).encode()) < normal_size - 100
    receipt = library.analyze({"flight_id": after["id"]})
    assert "report" not in receipt and len(json.dumps(receipt)) < 1000


def finish_owned_capture(library, record):
    from test_collection import envelope
    collection = CollectionService(library.store)
    with library.store._connect() as db:
        capture = collection._get(db, "capture", record["capture_session_id"])
    collection.stop_capture({"capture_session_id": record["capture_session_id"], **envelope(collection, capture["profile"])})


def test_independent_log_auto_alignment_only_after_capture_stops_and_caches_attempt(library, tmp_path):
    log, record = attach_owned_capture(library, tlog(tmp_path / "reference-owned.tlog", tag=0))
    candidate = library.logs.import_log({"path": str(tlog(tmp_path / "candidate-independent.tlog", tag=7)), "label": "GCS log", "synthetic": False})["log"]
    result = library.refresh({})
    assert result["alignment_attempts"] == 0
    finish_owned_capture(library, record)
    result = library.refresh({})
    assert result["alignment_attempts"] == 1
    row = next(row for row in result["flights"] if row.get("log_id") == candidate["id"])
    detail = library.detail({"flight_id": row["id"]})
    assert detail["flight"]["context_linkage"] == "verified", detail
    assert detail["report"]["flight_context"]["status"] == "verified"
    assert detail["report"]["flight_context"]["flight_record_id"] == record["id"]
    assert library.refresh({})["alignment_attempts"] == 0
    assert len(library.store.list_documents("alignment_report")) == 1


def test_ambiguous_independent_logs_never_auto_attach_and_missing_evidence_stays_provenance(library, tmp_path):
    log, record = attach_owned_capture(library, tlog(tmp_path / "reference-only.tlog", tag=0))
    finish_owned_capture(library, record)
    only = library.refresh({})
    assert only["alignment_attempts"] == 0
    assert only["flights"][0]["context_linkage"] == "capture_provenance"
    for tag in (7, 8):
        library.logs.import_log({"path": str(tlog(tmp_path / f"ambiguous-{tag}.tlog", tag=tag)), "label": "candidate", "synthetic": False})
    result = library.refresh({})
    assert result["alignment_attempts"] == 1
    assert all(row.get("context_linkage") != "verified" for row in result["flights"])
    report = library.store.list_documents("alignment_report")[0]
    assert all(candidate["state"] == "ambiguous" for candidate in report["candidates"])


def test_new_armed_record_appears_readonly_without_refresh_or_analysis(library, monkeypatch):
    collection = CollectionService(library.store)
    capture, _ = prepare(collection)
    collection.ingest_event(capture, event(0))
    collection.ingest_event(capture, event(1, True))
    with library.store._connect() as db:
        before = db.execute("SELECT count(*) FROM collection_versions").fetchone()[0]
        documents = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    monkeypatch.setattr(library, "refresh", lambda *a, **k: pytest.fail("No background refresh in live read"))
    monkeypatch.setattr(library, "_verify_original", lambda *a, **k: pytest.fail("No hashing in live read"))
    result = library.list_flights({})
    assert result["total"] == 1
    row = result["flights"][0]
    assert row["kind"] == "observed_flight" and row["status"] == "awaiting_log"
    assert library.detail({"flight_id": row["id"]})["flight"]["id"] == row["id"]
    with library.store._connect() as db:
        assert db.execute("SELECT count(*) FROM collection_versions").fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == documents
        assert db.execute("SELECT count(*) FROM flight_library_rows").fetchone()[0] == 0
