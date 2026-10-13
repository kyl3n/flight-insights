"""Local report exports are explicit, immutable, and never network submissions."""
import hashlib
import json
import math
from pathlib import Path

import pytest

from q22_engine.insight_service import InsightReports
from q22_engine.store import Store


def report_fixture(store):
    return store.add_document("insight_report", {
        "algorithm_version": "synthetic-review-test",
        "scope": {"interval_start_s": 1, "interval_end_s": 2, "boot_epoch": 0,
                  "original_path": "/private/aircraft.bin", "aircraft_id": "private-name"},
        "operating_profile": {"aircraft_mass_kg": 20, "payload_mass_kg": 4,
                              "declared_firmware": "synthetic", "operator_notes": "private-note"},
        "metrics": {"mean_power_w": {"value": 2000, "unit": "W"}},
        "findings": [{"title": "Synthetic finding", "time_s": 1.5,
                      "evidence": {"value": 45, "raw_fields": {"GPS": "private-position"},
                                   "uid": "private-uid", "remote_address": "private-address"}}],
        "parameter_evidence": {"state": "incomplete", "parameters": {"PRIVATE": 1}},
        "recommendations": [{"action": "Collect more evidence", "source": {"url": "https://github.com/ArduPilot/ardupilot"}}],
        "limitations": ["Synthetic fixture only"],
    })


def test_exact_preview_matches_explicit_local_export_without_sensitive_fields(tmp_path):
    store = Store(tmp_path / "catalog")
    service = InsightReports(store)
    report = report_fixture(store)
    preview = service.dispatch("insights.review_packet", {"report_id": report["id"]})
    text = json.dumps(preview["packet"])
    for secret in ("private-position", "private-uid", "private-address", "private-note", "private-name", "/private", "PRIVATE"):
        assert secret not in text
    assert preview["packet"]["metrics"]["mean_power_w"]["value"] == 2000
    assert preview["packet"]["operating_profile"]["payload_mass_kg"] == 4
    assert len(list(tmp_path.iterdir())) == 1, "Preview must not write an export"
    exported = service.dispatch("insights.export_review", {"report_id": report["id"], "folder": str(tmp_path)})
    payload = Path(exported["path"]).read_bytes()
    assert json.loads(payload) == preview["packet"]
    assert len(payload) == preview["byte_size"] == exported["byte_size"]
    assert hashlib.sha256(payload).hexdigest() == exported["sha256"]
    another = service.dispatch("insights.export_review", {"report_id": report["id"], "folder": str(tmp_path)})
    assert another["path"] != exported["path"]
    assert Path(exported["path"]).read_bytes() == payload
    assert store.get_document("insight_report", report["id"]) == report


def test_persistent_reports_pagination_and_export_validation(tmp_path):
    store = Store(tmp_path / "catalog")
    for _ in range(3):
        report_fixture(store)
    service = InsightReports(Store(tmp_path / "catalog"))
    page = service.dispatch("insights.list", {"limit": 2})
    assert len(page["reports"]) == 2 and page["next_offset"] == 2
    last = service.dispatch("insights.list", {"limit": 2, "offset": 2})
    assert len(last["reports"]) == 1 and last["next_offset"] is None
    assert not {row["id"] for row in page["reports"]} & {row["id"] for row in last["reports"]}
    report_id = last["reports"][0]["id"]
    assert service.dispatch("insights.detail", {"report_id": report_id})["report"]["findings"]
    with pytest.raises(ValueError, match="folder"):
        service.dispatch("insights.export_review", {"report_id": report_id})
    with pytest.raises(ValueError, match="Unknown"):
        service.dispatch("insights.review_packet", {"report_id": "missing"})
    with pytest.raises(ValueError, match="limit"):
        service.dispatch("insights.list", {"limit": True})


def test_failed_export_removes_partial_file_and_preserves_report(tmp_path, monkeypatch):
    store = Store(tmp_path / "catalog")
    service = InsightReports(store)
    report = report_fixture(store)
    def fail(_descriptor):
        raise OSError("Injected disk failure")
    monkeypatch.setattr("q22_engine.insight_service.os.fsync", fail)
    with pytest.raises(OSError, match="disk failure"):
        service.dispatch("insights.export_review", {"report_id": report["id"], "folder": str(tmp_path)})
    assert not list(tmp_path.glob("Q22-insight-review-*.json"))
    assert service.dispatch("insights.detail", {"report_id": report["id"]})["report"] == report


def actual_report(store, tmp_path, *, firmware="/private/UID-ALPHA.params"):
    """Decode an actual synthetic DataFlash text log through the production path."""
    from q22_engine.insights import InsightService
    from q22_engine.logs import LogService

    fixture = Path(__file__).resolve().parents[2] / "fixtures/logs/synthetic-flight.log"
    header = [line for line in fixture.read_text().splitlines() if line.startswith(("FMT,", "UNIT,", "MULT,", "FMTU,"))]
    header += [
        "FMT, 136, 28, VIBE, QBfffI, TimeUS,IMU,VibeX,VibeY,VibeZ,Clip",
        "FMT, 137, 32, GYR, QQBfff, TimeUS,SampleUS,I,GyrX,GyrY,GyrZ",
        "FMT, 138, 15, RCOU, QHH, TimeUS,C1,C2",
        "UNIT, 0, 97, m/s/s", "UNIT, 0, 103, rad/s", "UNIT, 0, 117, us",
        "FMTU, 0, 136, s#aaa-, F-000-", "FMTU, 0, 137, ss#ggg, FF-000",
        "FMTU, 0, 138, suu, F00",
    ]
    rows = [
        "PARM, 0, ATC_RAT_RLL_P, 0.1, 0.1",
        "PARM, 0, INS_GYRO_FILTER, 20, 20",
        "PARM, 0, BATT_SERIAL_NUM, 1234567, 0",
        "PARM, 0, INS_ACC_ID, 7654321, 0",
        "PARM, 1400000, PRIVATE_LAT, 37.12345, 0",
        "PARM, 1600000, ATC_RAT_RLL_P, 0.2, 0.1",
        "VIBE, 1000000, 0, 70, 5, 2, 100", "VIBE, 2000000, 0, 80, 5, 2, 103",
        "RCOU, 1000000, 1980, 1500", "RCOU, 2000000, 1990, 1550",
        "BAT, 1000000, 0, 24, 10, 20, 80, 100", "BAT, 2000000, 0, 22, 20, 20, 80, 100",
    ]
    rows += [f"GYR, {1000000 + i * 5000}, {1000000 + i * 5000}, 0, {math.sin(2 * math.pi * 20 * i / 200)}, 0, 0" for i in range(256)]
    rows.sort(key=lambda line: int(line.split(",")[1]))
    path = tmp_path / "private-source.log"
    path.write_text("\n".join(header + rows) + "\n")
    log = LogService(store).import_log({"path": str(path), "label": "Private aircraft name", "synthetic": True})["log"]
    snapshot = store.add_document("snapshot", {"parameters": {
        "ATC_RAT_RLL_P": {"value": "0.3"}, "BATT_SERIAL_NUM": {"value": "9876543"}}})
    return InsightService(store).report({
        "log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
        "interval_start_s": 1, "interval_end_s": 2.5, "max_gap_s": 1.5,
        "parameter_snapshot_id": snapshot["id"],
        "operating_profile": {"aircraft_mass_kg": 20, "payload_mass_kg": 4,
            "rotor_count": 4, "propeller_diameter_m": .5, "rotor_layout": "non_overlapping",
            "battery_cell_count": 6, "actuator_protocol": "pwm", "motor_output_channels": [1, 2],
            "output_low_us": 1000, "output_high_us": 2000, "declared_firmware": firmware},
        "spectrum": {"sample_rate_hz": 200, "anti_alias_cutoff_hz": 80,
            "anti_alias_evidence": "/private/chain.pdf UID-BETA 192.168.9.2",
            "sensor_instance": "0", "axis": "x", "window_samples": 256},
    })["report"]


def test_real_report_export_removes_free_text_identifiers_but_keeps_interpretation(tmp_path):
    store = Store(tmp_path / "catalog")
    raw = actual_report(store, tmp_path)
    assert raw["spectrum"]["status"] == "conditional"
    assert raw["parameter_evidence"]["loaded_snapshot"]["different_count"] == 2
    report = store.add_document("insight_report", raw)
    service = InsightReports(store)
    packet = service.dispatch("insights.review_packet", {"report_id": report["id"]})["packet"]
    encoded = json.dumps(packet)
    for secret in ("/private", "UID-ALPHA", "UID-BETA", "192.168.9.2", "BATT_SERIAL_NUM", "INS_ACC_ID", "PRIVATE_LAT", "1234567", "7654321", "9876543", "Private aircraft name"):
        assert secret not in encoded
    assert packet["scope"]["synthetic"] is True
    assert packet["scope"]["time_domain"] == "flight_controller_boot"
    assert packet["operating_profile"]["declared_firmware"] is None
    assert packet["operating_profile"]["motor_output_channels"] == [1, 2]
    assert packet["operating_profile"]["output_high_us"] == 2000
    assert packet["operating_profile"]["derived_context"]["payload_mass_fraction"] == .2
    assert packet["metrics"]["command_headroom_fraction"]["value"] == pytest.approx(.01)
    assert packet["metrics"]["mean_power_w"]["coverage_fraction"] == raw["metrics"]["mean_power_w"]["coverage_fraction"]
    assert packet["spectrum"]["bins"] == raw["spectrum"]["bins"]
    assert packet["spectrum"]["sample_count"] == 256
    assert packet["vibration"]["clipping"]["observed_deltas"] == {"0:Clip": 3}
    assert packet["vibration"]["coverage"] == raw["vibration"]["coverage"]
    assert packet["vibration"]["clipping"]["coverage"] == raw["vibration"]["clipping"]["coverage"]
    assert packet["spectrum"]["measurement_interval_start_s"] == raw["spectrum"]["measurement_interval_start_s"]
    assert packet["spectrum"]["measurement_interval_end_s"] == raw["spectrum"]["measurement_interval_end_s"]
    assert "sample_clock_start_s" not in packet["spectrum"]
    assert packet["findings"] and packet["recommendations"]
    assert packet["guidance"]["entries"][0]["references"][0]["sha256"]
    params = packet["parameter_evidence"]
    assert {item["name"] for item in params["observations"]} == {"ATC_RAT_RLL_P", "INS_GYRO_FILTER"}
    assert [item["name"] for item in params["changes"]] == ["ATC_RAT_RLL_P"]
    assert [item["name"] for item in params["loaded_snapshot"]["differences"]] == ["ATC_RAT_RLL_P"]
    assert params["omitted_parameter_records"] == 4
    assert params["attribution_allowed"] is False
    assert "historical" in params["loaded_snapshot"]["applicability"]
    assert any("flight status unknown" in item for item in packet["warnings"])
    exported = service.dispatch("insights.export_review", {"report_id": report["id"], "folder": str(tmp_path)})
    assert json.loads(Path(exported["path"]).read_text()) == packet
    assert store.get_document("insight_report", report["id"]) == report


def test_release_label_is_not_build_validation_and_registry_mismatch_does_not_substitute(tmp_path):
    from q22_engine.guidance import REGISTRY_VERSION

    store = Store(tmp_path / "catalog")
    raw = actual_report(store, tmp_path, firmware="Copter-4.7.1")
    raw["scope"]["time_domain"] = "utc_receive_us"
    raw["scope"]["origin_time_us"] = "1729000000000000"
    raw["warnings"] += ["Times describe ground reception, not synchronized sensor measurement time.", "/private/local diagnostic"]
    packet = InsightReports.review_packet(raw)
    assert packet["operating_profile"]["declared_firmware"] == "Copter-4.7.1"
    assert packet["scope"]["time_domain"] == "utc_receive_us"
    assert packet["guidance"]["version"] == REGISTRY_VERSION
    assert packet["guidance"]["firmware_compatibility"]["exact_build_validated"] is False
    assert "1729000000000000" not in json.dumps(packet)
    assert any("ground reception" in item for item in packet["warnings"])
    assert any("omitted" in item for item in packet["warnings"])
    assert "/private" not in json.dumps(packet)
    raw["guidance"]["version"] = "future-private-build"
    packet = InsightReports.review_packet(raw)
    assert packet["guidance"]["status"] == "unresolved_registry_version"
    assert packet["guidance"]["entries"] == []
    assert "future-private-build" not in json.dumps(packet)


def test_unknown_fields_and_narratives_cannot_bypass_projection():
    packet = InsightReports.review_packet({
        "scope": {"synthetic": "false", "time_domain": "private-name"},
        "metrics": {"mean_power_w": {"value": "private-location", "unit": "private-unit", "interpretation": "/private/file"},
                    "gps_latitude": {"value": 37.123456}},
        "findings": [{"id": "finding-1", "kind": "accelerometer_clipping", "title": "/private/file",
            "details": {"counter": "UID-SECRET:Clip", "latitude": 37.123456}}],
        "vibration": {"clipping": {"observed_deltas": {"UID-SECRET:Clip": 22}}},
        "spectrum": {"anti_alias_evidence": "/private/file"},
        "parameter_evidence": {"state": "uncertain", "attribution_allowed": True,
            "observations": [{"name": "INS_ACC_ID", "value": "123"}]},
    })
    serialized = json.dumps(packet, allow_nan=False)
    assert not any(secret in serialized for secret in ("private-", "/private", "UID-SECRET", "37.123456", "INS_ACC_ID"))
    assert packet["scope"]["synthetic"] is None
    assert packet["metrics"]["mean_power_w"]["value"] is None
    assert packet["parameter_evidence"]["state"] == "uncertain"
    assert packet["parameter_evidence"]["attribution_allowed"] is False
