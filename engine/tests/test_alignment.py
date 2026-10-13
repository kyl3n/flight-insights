"""Synthetic content-rule oracles, never hardware or authenticated identity evidence."""
from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
from uuid import uuid4

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mav

from q22_engine.alignment import AlignmentService, Evidence, clock_model, gps_utc, match
from q22_engine.collection import CollectionService
from q22_engine.logs import LogService
from q22_engine.parameter_capture import ParameterAccumulator
from q22_engine.store import Store
from test_collection import envelope, event, prepare, profile


UTC = 1700000000000000


def write_tlog(path, *, uid=123, receive_offset=0, boot_shift=0, tag=0, arms=((3, 9),), parameter_change=True, duration=14):
    codec = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    codec.seq = tag
    packets = []

    def packet(stamp, message):
        packets.append(struct.pack(">Q", (UTC + int(stamp * 1e6) + receive_offset) & ~3) + message.pack(codec))
        codec.seq = (codec.seq + 1) % 256

    packet(0, mav.MAVLink_autopilot_version_message(131072, 0x040600ff, 0, 0, 9, [0]*8, [0]*8, [0]*8, 1, 2, uid))
    for second in range(duration):
        packet(second + .1, mav.MAVLink_system_time_message(UTC + int((second + .1) * 1e6), int((100 + second + .1 + boot_shift) * 1000)))
        armed = any(start <= second < end for start, end in arms)
        packet(second + .2, mav.MAVLink_heartbeat_message(2, 3, 128 if armed else 0, 0, 4, 3))
        if second in (1, 6):
            packet(second + .3, mav.MAVLink_param_value_message(b"ATC_RAT_RLL_P", .2 if second == 6 and parameter_change else .1, 9, 1, 0))
    path.write_bytes(b"".join(packets))
    return path


@pytest.fixture
def services(tmp_path):
    store = Store(tmp_path / "catalog")
    logs = LogService(store)
    collection = CollectionService(store)
    alignment = AlignmentService(store, logs, collection)
    yield store, logs, collection, alignment
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


def imported(services, tmp_path, name, **kwargs):
    log = services[1].import_log({"path": str(write_tlog(tmp_path / name, **kwargs)),
                                 "label": "Synthetic alignment oracle", "synthetic": False})["log"]
    return {"log_id": log["id"], "source_key": "1:1", "boot_epoch": 0}


def analyzed(services, left, right, **extra):
    return services[3].dispatch("alignment.analyze", {"reference": left, "candidates": [right], **extra})["report"]


def managed(services, reference):
    store, logs, collection, _ = services
    log = store.get_document("log", reference["log_id"])
    chosen = profile(expected_uid="123")
    capture, _ = prepare(collection, chosen)
    for row in logs.events(log):
        raw = row["raw_fields"]
        index = row["record_index"] + 1
        if row["message_type"] == "AUTOPILOT_VERSION":
            collection.ingest_event(capture, event(index, kind="identity", uid=str(raw["uid"]), boot_epoch="0"))
        elif row["message_type"] == "HEARTBEAT":
            collection.ingest_event(capture, event(index, bool(raw["base_mode"] & 128), boot_epoch="0",
                received_utc_us=str(row["receive_time_us"])))
    # Exact CollectionController._archive contract: completion is nested in
    # coverage; there is deliberately no top-level `complete` field.
    collection.attach_capture_artifacts(capture, [{"source": log["source"], "role": "app_mavlink_capture",
        "record_start": 1, "record_end": log["record_count"],
        "coverage": {"complete_chunk": True, "original_chunk": "telemetry-000001.tlog", "record_scope": "capture_global"}}])
    records = collection.status({"capture_session_id": capture})["flight_records"]
    return chosen, capture, records


def test_clock_model_reports_residuals_and_refuses_drift_regression_and_short_span():
    anchors = [{"boot_us": i * 1_000_000, "utc_us": UTC + i * 1_000_010} for i in range(4)]
    value = clock_model(anchors)
    assert value["state"] == "supported_reported_clock"
    assert value["slope"] == pytest.approx(1.00001)
    assert value["maximum_residual_us"] == pytest.approx(0, abs=1e-6)
    assert value["uncertainty_us"] >= 100000 and not value["absolute_time_authenticated"]
    assert clock_model(anchors[:2])["state"] == "unavailable"
    assert clock_model([anchors[0], anchors[1], anchors[1]])["state"] == "unavailable"
    changed = deepcopy(anchors)
    changed[-1]["utc_us"] += 1000000
    assert clock_model(changed)["state"] == "unavailable"


def test_report_sampling_keeps_full_clock_fit_and_receipt_extrema():
    anchors = [{"boot_us": i * 100000, "utc_us": UTC + i * 100000,
                "receive_us": UTC + i * 100000 + (12345 if i == 334 else 0)} for i in range(1000)]
    value = clock_model(anchors)
    assert value["state"] == "supported_reported_clock"
    assert value["anchor_count"] == value["residual_count"] == 1000
    assert len(value["anchors"]) == len(value["residuals_us"]) == 64
    assert value["anchor_sample_indices"][0] == 0 and value["anchor_sample_indices"][-1] == 999
    assert 334 not in value["anchor_sample_indices"]
    assert value["receive_clock_offset_range_us"] == [0, 12345]
    changed = deepcopy(anchors)
    changed[334]["utc_us"] += 90000
    assert clock_model(changed)["state"] == "unavailable"
    changed[334]["utc_us"] = changed[333]["utc_us"]
    assert clock_model(changed)["state"] == "unavailable"


def test_long_real_wire_capture_compacts_heartbeats_without_losing_boundaries(services, tmp_path):
    left = imported(services, tmp_path, "long-reference.tlog", duration=2500, arms=((3, 2490),))
    right = imported(services, tmp_path, "long-candidate.tlog", duration=2500, arms=((3, 2490),), tag=4)
    report = analyzed(services, left, right)
    assert report["candidates"][0]["state"] == "verified"
    for evidence in (report["reference"], report["candidates"][0]["evidence"]):
        counts = evidence["observation_counts"]
        assert counts["armed"] == counts["clock_anchors"] == 2500
        assert counts["retained_armed_boundaries"] == 6
        assert not evidence["limits_reached"]
        assert evidence["clock_model"]["anchor_count"] == 2500
        assert len(evidence["clock_model"]["anchors"]) == 64
        interval = evidence["armed_intervals"][0]
        assert interval["complete_boundaries"]
        assert interval["start"]["time"]["boot_us"] == pytest.approx(103200000)
        assert interval["end"]["time"]["boot_us"] == pytest.approx(2590200000)
    assert len(json.dumps(report)) < 65000


@pytest.mark.parametrize("clock_delta", [-1500000, 4000000])
def test_compaction_does_not_hide_middle_heartbeat_clock_regression_or_gap(clock_delta):
    log = {"id": "synthetic", "source": {"sha256": "a" * 64}, "format": "tlog", "synthetic": True}
    evidence = Evidence(log, {"source_key": "1:1", "boot_epoch": 0})
    for i in range(2500):
        evidence.observe({"message_type": "HEARTBEAT", "record_index": i,
            "receive_time_us": UTC + i * 1000000 + (clock_delta if i == 1000 else 0),
            "raw_fields": {"autopilot": 3, "type": 2, "base_mode": 128}})
    assert len(evidence.states) == 2
    result = evidence.finish()
    assert result["observation_counts"]["armed"] == 2500
    assert result["quality_issues"]


def test_internal_anchor_bound_remains_visible_and_blocks_match(monkeypatch):
    monkeypatch.setattr("q22_engine.alignment.MAX_ANCHORS", 600)
    log = {"id": "synthetic", "source": {"sha256": "a" * 64}, "format": "tlog", "synthetic": True}
    evidence = Evidence(log, {"source_key": "1:1", "boot_epoch": 0})
    for i in range(700):
        evidence.observe({"message_type": "SYSTEM_TIME", "record_index": i,
            "boot_time_us": i * 100000, "raw_fields": {"time_unix_usec": UTC + i * 100000}})
    result = evidence.finish()
    assert result["observation_counts"]["clock_anchors"] == 700
    assert result["observation_counts"]["retained_clock_anchors"] == 600
    assert result["limits_reached"] == ["clock_anchor_limit"]
    assert match(result, result)["state"] == "provisional"


def test_gps_wire_schema_fix_validity_and_leap_horizon_are_mandatory():
    schema = {k: {"format_code": v} for k, v in {"TimeUS": "Q", "GWk": "H", "GMS": "I", "Status": "B"}.items()}
    raw = {"TimeUS": 1000, "GWk": 2288, "GMS": 259218000, "Status": 3}
    # GPS week 2288 starts 2023-11-12; Wednesday + 18 GPS seconds is
    # 2023-11-15 00:00:00 UTC under the separately pinned leap policy.
    assert datetime.fromtimestamp(gps_utc(raw, schema) / 1e6, timezone.utc).isoformat() == "2023-11-15T00:00:00+00:00"
    assert gps_utc(raw | {"Status": 2}, schema) is None
    assert gps_utc(raw | {"GWk": 2500}, schema) is None
    assert gps_utc(raw, {}) is None
    assert gps_utc(raw | {"GMS": 604800000}, schema) is None


def test_content_alignment_uses_identity_clocks_both_boundaries_and_timeline(services, tmp_path):
    left = imported(services, tmp_path, "misleading-2028.tlog")
    right = imported(services, tmp_path, "unrelated-name.tlog", receive_offset=300000, tag=21)
    result = analyzed(services, left, right)
    candidate = result["candidates"][0]
    assert candidate["state"] == "verified"
    assert len(candidate["matches"]) == 1
    assert result["reference"]["clock_model"]["anchor_count"] == 14
    assert candidate["evidence"]["clock_model"]["boot_epoch_utc_us"] == result["reference"]["clock_model"]["boot_epoch_utc_us"]
    timeline = result["parameter_timeline"]
    assert timeline["observation_count"] == 4 and timeline["change_count"] == 2
    assert all(row["effective_from"] is None for row in timeline["observations"])
    assert not timeline["attribution_allowed"] and not result["hardware_qualified"]
    assert services[3].dispatch("alignment.get", {"report_id": result["id"]})["report"] == result


@pytest.mark.parametrize("changes, expected", [({"uid": 999}, "rejected"), ({"uid": 0}, "provisional"),
                                               ({"boot_shift": 20}, "rejected"), ({"arms": ((10, 12),)}, "rejected")])
def test_uid_boot_and_supported_interval_contradictions_never_match(services, tmp_path, changes, expected):
    left = imported(services, tmp_path, "left.tlog")
    right = imported(services, tmp_path, "right.tlog", tag=4, **changes)
    assert analyzed(services, left, right)["candidates"][0]["state"] == expected


def test_unselected_catalog_competitor_makes_selected_match_ambiguous(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    right = imported(services, tmp_path, "right.tlog", tag=4)
    hidden = imported(services, tmp_path, "hidden-candidate.tlog", tag=88)
    candidate = analyzed(services, left, right)["candidates"][0]
    assert candidate["state"] == "ambiguous"
    assert hidden in candidate["competing_candidates"]


def test_duplicate_original_is_not_independent_evidence_or_a_competitor(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    duplicate = imported(services, tmp_path, "copy.tlog")
    assert analyzed(services, left, duplicate)["candidates"][0]["state"] == "provisional"
    right = imported(services, tmp_path, "right.tlog", tag=4)
    assert analyzed(services, left, right)["candidates"][0]["state"] == "verified"


def test_one_original_supports_two_intervals_without_losing_source_spans(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog", arms=((2, 4), (8, 11)))
    right = imported(services, tmp_path, "right.tlog", arms=((2, 4), (8, 11)), tag=4)
    chosen, _, records = managed(services, left)
    report = analyzed(services, left, right)
    assert report["candidates"][0]["state"] == "verified" and len(report["candidates"][0]["matches"]) == 2
    spans = []
    for record in records:
        args = {"report_id": report["id"], "flight_record_id": record["id"], **envelope(services[2], chosen)}
        attached = services[3].dispatch("alignment.attach", args)
        spans.append(attached["associations"][0]["time_or_record_spans"])
    assert spans[0] != spans[1]


def test_verified_attachment_preserves_earlier_manifest_and_replays_idempotently(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    right = imported(services, tmp_path, "right.tlog", tag=4)
    chosen, _, records = managed(services, left)
    record = records[0]
    before = services[2].detail({"flight_record_id": record["id"]})["manifest"]
    report = analyzed(services, left, right, flight_record_id=record["id"])
    args = {"report_id": report["id"], "flight_record_id": record["id"], **envelope(services[2], chosen)}
    result = services[3].dispatch("alignment.attach", args)
    assert result["associations"][0]["state"] == "verified"
    assert result["manifest"]["collection_completeness"]["state"] == "partial"
    assert result["manifest"]["analysis_eligibility"]["state"] == "blocked"
    old = services[2].detail({"flight_record_id": record["id"], "manifest_revision": before["revision"]})["manifest"]
    assert old == before
    assert services[3].dispatch("alignment.attach", args)["operation_replayed"]


def test_attachment_rechecks_new_competitors_and_profile_before_mutation(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    right = imported(services, tmp_path, "right.tlog", tag=4)
    chosen, _, records = managed(services, left)
    report = analyzed(services, left, right)
    imported(services, tmp_path, "later.tlog", tag=22)
    args = {"report_id": report["id"], "flight_record_id": records[0]["id"], **envelope(services[2], chosen)}
    with pytest.raises(ValueError, match="No freshly verified"):
        services[3].dispatch("alignment.attach", args)
    with pytest.raises(ValueError, match="fingerprint"):
        services[3].dispatch("alignment.attach", args | {"applicability_fingerprint": "wrong"})


def test_reference_import_is_owned_hash_checked_and_cancel_is_observed(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    _, _, records = managed(services, left)
    result = services[3].dispatch("alignment.reference", {"flight_record_id": records[0]["id"]})
    assert result["reference_choices"] == [{"log_id": result["log"]["id"], "source_key": "1:1", "boot_epoch": 0}]
    right = imported(services, tmp_path, "right.tlog", tag=4)
    with pytest.raises(CancelledError):
        services[3].dispatch("alignment.analyze", {"reference": left, "candidates": [right]}, cancel_check=lambda: True)


def acquisition(services, tmp_path, *, start=2.4, end=3.4, boot_shift=0, partial=False):
    firmware = {"flight_sw_version": 0x040600ff, "board_version": 9, "vendor_id": 1, "product_id": 2}
    accumulator = ParameterAccumulator("synthetic-aircraft", "acquisition-segment", {"system_id": 1, "component_id": 1}, firmware)
    for stamp in (start, end):
        accumulator.observe({"name": "ATC_RAT_RLL_P", "value": .1, "param_type": 9, "index": 0,
                             "count": 2 if partial else 1, "received_monotonic_ns": str(int((1000 + stamp) * 1e9)),
                             "received_utc_us": str(UTC + int(stamp * 1e6)), "raw_ref": {"offset": int(stamp * 1000)}})
    capture = accumulator.finalize(role="unknown")
    path = tmp_path / f"readback-{uuid4()}.json"
    path.write_text(json.dumps(accumulator.snapshot()))
    source = services[0].archive_file(path)

    def gate(stamp):
        receipt = {"received_monotonic_ns": str(int((1000 + stamp) * 1e9)), "received_utc_us": str(UTC + int(stamp * 1e6))}
        return {"identity": {"uid": "123", "firmware_identity": firmware, **receipt},
                "boot_clock": {"time_boot_ms": int((100 + stamp + boot_shift) * 1000), **receipt}}

    return services[0].add_document("acquisition", {"kind": "parameters", "state": "partial" if partial else "complete",
        "synthetic": False, "connection": {"system_id": 1, "component_id": 1}, "parameter_capture": capture,
        "transport_evidence": {"initial_gate_observations": gate(start - .4), "final_gate_observations": gate(end + .4)},
        "source_refs": [source | {"role": "parameter_readback_snapshot"}]})


def test_acquisition_readbacks_link_by_boot_identity_and_bounds_not_declared_role(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog", arms=((8, 20),), duration=32)
    right = imported(services, tmp_path, "right.tlog", arms=((8, 20),), duration=32, tag=4)
    pre = acquisition(services, tmp_path)
    post = acquisition(services, tmp_path, start=25.4, end=26.4)
    result = analyzed(services, left, right)
    links = {row["acquisition_id"]: row for row in result["parameter_links"]["acquisitions"]}
    assert links[pre["id"]]["state"] == "identity_and_temporal_rule_supported"
    assert links[pre["id"]]["declared_role"] == "unknown" and links[pre["id"]]["matches"][0]["role"] == "preflight"
    assert links[post["id"]]["matches"][0]["role"] == "postflight"
    assert all(row["effective_from"] is None and row["flight_time_applicability"] == "unknown" for row in links.values())
    assert not result["parameter_links"]["automatic_effective_configuration"]


def test_failed_bootstrap_partial_readback_and_wrong_boot_cannot_be_promoted(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog", arms=((8, 20),), duration=32)
    right = imported(services, tmp_path, "right.tlog", arms=((8, 20),), duration=32, tag=4)
    services[0].add_document("acquisition", {"kind": "parameters", "state": "partial",
        "transport_evidence": {"initial_gate_observations": None,
            "final_gate_observations": {"heartbeat": None, "identity": None, "boot_clock": None}}})
    partial = acquisition(services, tmp_path, partial=True)
    foreign = acquisition(services, tmp_path, boot_shift=5)
    result = analyzed(services, left, right)
    links = {row["acquisition_id"]: row for row in result["parameter_links"]["acquisitions"]}
    assert links[partial["id"]]["state"] == "provisional" and links[partial["id"]]["completeness"] == "partial"
    assert links[foreign["id"]]["state"] == "rejected"


def test_equal_parameter_export_values_remain_unknown_identity_reference(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    right = imported(services, tmp_path, "right.tlog", tag=4)
    path = tmp_path / "misleading-date-and-aircraft.param"
    path.write_text("ATC_RAT_RLL_P,0.10000000149011612\n")
    source = services[0].archive_file(path)
    reference = services[0].add_document("parameter_reference", {"source": source, "canonical_hash": "synthetic-canonical",
        "synthetic": True, "parameters": {"ATC_RAT_RLL_P": {"value": "0.10000000149011612"}}})
    result = analyzed(services, left, right)["parameter_links"]["exports"][0]
    assert result["parameter_reference_id"] == reference["id"] and result["value_comparison"]["matching_observed_value_count"] == 1
    assert result["state"] == "reference_only" and result["identity"] == result["capture_time"] == result["flight_time_applicability"] == "unknown"
    assert result["synthetic"]


def test_truncated_candidate_universe_cannot_verify(services, tmp_path, monkeypatch):
    import q22_engine.alignment as module
    left = imported(services, tmp_path, "left.tlog")
    right = imported(services, tmp_path, "right.tlog", tag=4)
    original = services[3]._catalog

    def truncated():
        logs, _, signature, acquisitions = original()
        return logs, True, signature, acquisitions

    monkeypatch.setattr(services[3], "_catalog", truncated)
    result = analyzed(services, left, right)
    assert result["candidate_universe"]["truncated"]
    assert result["candidates"][0]["state"] == "provisional"


def test_managed_reference_must_have_trusted_complete_chunk_receipt(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    _, capture_id, records = managed(services, left)
    with services[2]._lock, services[0]._connect() as db:
        capture = services[2]._get(db, "capture", capture_id)
        capture["artifact_sources"][0]["coverage"]["complete_chunk"] = False
        services[2]._put(db, "capture", capture)
    with pytest.raises(ValueError, match="completed archived"):
        services[3].dispatch("alignment.reference", {"flight_record_id": records[0]["id"]})


@pytest.mark.parametrize("duration, rate", [(14, 1), (70, 10)])
def test_gps_dataflash_clock_and_download_provenance_can_corroborate_tlog(services, tmp_path, duration, rate):
    left = imported(services, tmp_path, "reference.tlog", duration=duration)
    lines = ["FMT, 130, 19, GPS, QBHIB, TimeUS,I,GWk,GMS,Status",
             "FMT, 131, 12, ARM, QB, TimeUS,ArmState"]
    for tick in range(duration * rate):
        elapsed_us = tick * (1000000 // rate)
        utc = UTC + elapsed_us + 100000
        gps_ms = (utc - 315964800000000 + 18000000) // 1000
        week, millis = divmod(gps_ms, 604800000)
        lines.append(f"GPS, {100100000 + elapsed_us}, 0, {week}, {millis}, 3")
        lines.append(f"ARM, {100200000 + elapsed_us}, {int(3000000 <= elapsed_us < 9000000)}")
    path = tmp_path / "onboard-any-filename.log"
    path.write_text("\n".join(lines) + "\n")
    log = services[1].import_log({"path": str(path), "label": "Synthetic GPS/onboard oracle", "synthetic": False})["log"]
    right = {"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0}
    unsupported_identity = analyzed(services, left, right)["candidates"][0]
    assert unsupported_identity["evidence"]["clock_model"]["state"] == "supported_reported_clock"
    assert unsupported_identity["state"] == "provisional"
    firmware = {"flight_sw_version": 0x040600ff, "board_version": 9, "vendor_id": 1, "product_id": 2}
    gate = {"identity": {"uid": "123", "firmware_identity": firmware}}
    acquisition_doc = services[0].add_document("acquisition", {"kind": "log_download", "state": "complete",
        "synthetic": False, "transport_evidence": {"initial_gate_observations": gate, "final_gate_observations": gate},
        "download": {"transfer_complete": True, "sha256": log["source"]["sha256"], "reported_size": path.stat().st_size,
                     "contiguous_prefix_bytes": path.stat().st_size, "eof_evidence": {"synthetic": True},
                     "before_entry_evidence": {"synthetic": True}, "after_entry_evidence": {"synthetic": True}},
        "source_refs": [log["source"] | {"role": "onboard_log_candidate"}]})
    supported = analyzed(services, left, right)["candidates"][0]
    assert supported["state"] == "verified"
    assert supported["evidence"]["identity_provenance"][0]["acquisition_id"] == acquisition_doc["id"]
    assert supported["evidence"]["clock_model"]["anchor_count"] == duration * rate
    assert len(supported["evidence"]["clock_model"]["anchors"]) <= 64
    assert not supported["evidence"]["limits_reached"]


def test_edited_saved_report_cannot_inject_verified_evidence(services, tmp_path):
    left = imported(services, tmp_path, "left.tlog")
    right = imported(services, tmp_path, "wrong-aircraft.tlog", uid=999, tag=4)
    chosen, _, records = managed(services, left)
    report = analyzed(services, left, right)
    forged = {k: deepcopy(v) for k, v in report.items() if k not in ("id", "created_at")}
    forged["candidates"][0]["state"] = "verified"
    saved = services[0].add_document("alignment_report", forged)
    with pytest.raises(ValueError, match="No freshly verified"):
        services[3].dispatch("alignment.attach", {"report_id": saved["id"], "flight_record_id": records[0]["id"],
            **envelope(services[2], chosen)})
