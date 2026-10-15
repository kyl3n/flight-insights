"""Independent values, schema rejection and real decoder-to-health coverage."""
import json

import pytest

from q22_engine.flight_health import FlightHealthAnalyzer, MAX_CHANNELS, MAX_ESC_FRAMES
from q22_engine.insights import InsightService
from q22_engine.logs import LogService
from q22_engine.store import Store


def event(message, stamp, fields, *, source_format="log", boot=0):
    """fields: name -> (raw value, format code, unit, applied scale, unit id)."""
    return {"kind": "event", "message_type": message, "source_format": source_format,
            "time_domain": "flight_controller_boot", "boot_epoch": boot,
            "boot_time_us": round(stamp * 1e6), "time_us": round(stamp * 1e6),
            "record_index": round(stamp * 100), "byte_offset": round(stamp * 200),
            "raw_fields": {key: value[0] for key, value in fields.items()},
            "field_schema": {key: {"source_field": f"{message}.{key}", "format_code": value[1],
                "unit": value[2], "applied_multiplier": value[3], "unit_id": value[4]}
                for key, value in fields.items()}}


def integer(value, code="B"):
    return value, code, "", 1, "-"


def gps(stamp, fix=3, satellites=12, hdop=1.2, instance=0, **kwargs):
    return event("GPS", stamp, {"I": integer(instance), "Status": integer(fix),
        "NSats": integer(satellites), "HDop": (hdop, "c", "", .01 if kwargs.get("source_format") == "bin" else 1, "-")}, **kwargs)


def esc(stamp, instance, rpm=1000, current=5, temperature=30):
    return event("ESC", stamp, {"Instance": integer(instance), "RPM": (rpm, "f", "rpm", 1, "q"),
        "Curr": (current, "f", "A", 1, "A"), "Temp": (temperature, "c", "degC", 1, "O"),
        "Volt": (24, "f", "V", 1, "v"), "MotTemp": (0, "c", "degC", 1, "O")})


def domain(report, name):
    return next(item for item in report["domains"] if item["id"] == name)


def metric(report, domain_id, identifier, *, instance="0", message=None):
    channel = next(item for item in domain(report, domain_id)["channels"]
                   if item["instance_id"] == instance and (message is None or item["message_type"] == message))
    return next(item for item in channel["metrics"] if item["id"] == identifier)


def test_no_data_stays_unknown_in_every_domain_and_report_is_json_safe():
    report = FlightHealthAnalyzer(0, 2).finish()
    assert report["status"] == "unavailable"
    assert len(report["domains"]) == 5
    assert all(item["status"] == "unavailable" for item in report["domains"])
    assert report["findings"] == []
    assert report["firmware_scope"]["exact_build_validated"] is False
    json.dumps(report, allow_nan=False)


def test_gps_fix_codes_satellites_and_hdop_keep_provenance_and_no_categorical_mean():
    analyzer = FlightHealthAnalyzer(0, 3)
    analyzer.observe(gps(1, fix=2, satellites=8, hdop=2.4), 1)
    analyzer.observe(gps(2, fix=3, satellites=14, hdop=.8), 2)
    report = analyzer.finish()
    assert metric(report, "gnss", "hdop")["mean"] == pytest.approx(1.6)
    assert metric(report, "gnss", "satellites")["minimum"] == 8
    codes = metric(report, "gnss", "fix_type")
    assert codes["mean"] is None and codes["observed_codes"] == {"2": 1, "3": 1}
    finding = report["findings"][0]
    assert finding["id"] == "gnss-fix-0"
    assert finding["evidence"][0]["record_index"] == 100
    assert not finding["numeric_tuning_proposed"]


def test_binary_and_text_intrinsic_hdop_scaling_agree_without_double_application():
    values = []
    for source_format, raw in (("bin", 125), ("log", 1.25)):
        analyzer = FlightHealthAnalyzer(0, 2)
        analyzer.observe(gps(1, hdop=raw, source_format=source_format), 1)
        values.append(metric(analyzer.finish(), "gnss", "hdop")["mean"])
    assert values == [1.25, 1.25]


def test_fix_enum_with_contradictory_physical_units_is_not_interpreted():
    row = gps(1, fix=1)
    row["field_schema"]["Status"]["unit"] = "V"
    analyzer = FlightHealthAnalyzer(0, 2)
    analyzer.observe(row, 1)
    report = analyzer.finish()
    assert metric(report, "gnss", "fix_type")["status"] == "unavailable"
    assert report["findings"] == []


@pytest.mark.parametrize("damage", ["unit", "scale", "source_field", "format", "nonfinite", "bool"])
def test_unqualified_hdop_never_becomes_a_metric(damage):
    row = gps(1)
    descriptor = row["field_schema"]["HDop"]
    if damage == "unit": descriptor["unit"] = "m"
    if damage == "scale": descriptor["applied_multiplier"] = .01
    if damage == "source_field": descriptor["source_field"] = "GPS.Other"
    if damage == "format": descriptor["format_code"] = "f"
    if damage == "nonfinite": row["raw_fields"]["HDop"] = float("nan")
    if damage == "bool": row["raw_fields"]["HDop"] = True
    analyzer = FlightHealthAnalyzer(0, 2)
    analyzer.observe(row, 1)
    assert metric(analyzer.finish(), "gnss", "hdop")["status"] == "unavailable"


def test_estimator_core_separation_flags_and_scaled_innovations_are_descriptive():
    analyzer = FlightHealthAnalyzer(0, 3, firmware="unknown")
    for core, value in ((0, 120), (1, 50)):
        row = event("XKF4", 1, {"C": integer(core), "SV": (value, "c", "", .01, "-"),
                    "FS": integer(2 if core == 0 else 0, "H"), "SS": integer(831, "I")}, source_format="bin")
        analyzer.observe(row, 1)
    analyzer.observe(event("XKF3", 2, {"C": integer(0), "IVN": (200, "c", "m/s", .01, "n"),
                                      "IPN": (-150, "c", "m", .01, "m")}, source_format="bin"), 2)
    report = analyzer.finish()
    assert metric(report, "estimator", "sv", message="XKF4")["maximum"] == 1.2
    assert metric(report, "estimator", "sv", instance="1", message="XKF4")["maximum"] == .5
    assert metric(report, "estimator", "fs_flags", message="XKF4")["mean"] is None
    assert metric(report, "estimator", "ivn", message="XKF3")["mean"] == 2
    assert metric(report, "estimator", "ipn", message="XKF3")["mean"] == -1.5
    assert len(report["findings"]) == 2
    assert all("0" in finding["id"] for finding in report["findings"])
    assert report["firmware_scope"]["exact_build_validated"] is False


def test_esc_spreads_use_complete_observed_instance_sets_at_exact_boot_timestamps():
    analyzer = FlightHealthAnalyzer(0, 4)
    for row in [esc(1, 0, 1000, 4, 25), esc(1, 1, 1200, 6, 30),
                esc(2, 0, 1500, 5, 27), esc(2, 1, 1600, 8, 31), esc(3, 0, 9000, 100, 90)]:
        analyzer.observe(row, row["time_us"] / 1e6)
    report = analyzer.finish()
    observed = domain(report, "esc")
    rpm = next(item for item in observed["matched_frame_spreads"] if item["id"] == "rpm_spread")
    assert rpm["sample_count"] == 2 and rpm["mean"] == 150 and rpm["maximum"] == 200
    assert observed["expected_instances"] == "unknown"
    assert observed["frames_observed"] == 3
    assert metric(report, "esc", "motor_temperature_c")["status"] == "unavailable"
    assert analyzer.finish() == report


@pytest.mark.parametrize("fault", ["different_timestamp", "duplicate_instance", "inconsistent_relative_time", "missing_channel"])
def test_esc_incomparable_or_conflicting_frames_never_produce_spread(fault):
    analyzer = FlightHealthAnalyzer(0, 4)
    analyzer.observe(esc(1, 0), 1)
    if fault == "duplicate_instance": analyzer.observe(esc(1, 0, 5000), 1)
    row = esc(1 if fault != "different_timestamp" else 2, 1, rpm=2000)
    if fault == "missing_channel": row["field_schema"]["RPM"]["unit"] = "unknown"
    analyzer.observe(row, 2 if fault in {"different_timestamp", "inconsistent_relative_time"} else 1)
    spreads = domain(analyzer.finish(), "esc")["matched_frame_spreads"]
    assert next(item for item in spreads if item["id"] == "rpm_spread")["sample_count"] == 0


def test_esc_zero_values_do_not_establish_optional_telemetry_support():
    analyzer = FlightHealthAnalyzer(0, 2)
    analyzer.observe(esc(1, 0, rpm=0, current=0, temperature=0), 1)
    report = analyzer.finish()
    for field in ("rpm", "current_a", "temperature_c", "motor_temperature_c"):
        assert metric(report, "esc", field)["sample_count"] == 0


def test_esc_spread_memory_is_bounded_and_truncation_is_visible():
    analyzer = FlightHealthAnalyzer(0, 1000)
    for stamp in range(1, MAX_ESC_FRAMES + 10):
        analyzer.observe(esc(stamp, 0), stamp)
        analyzer.observe(esc(stamp, 1, rpm=1200), stamp)
    report = analyzer.finish()
    result = domain(report, "esc")
    assert result["spreads_truncated"]
    assert result["frames_observed"] == MAX_ESC_FRAMES + 9
    assert max(metric["sample_count"] for metric in result["matched_frame_spreads"]) == MAX_ESC_FRAMES
    assert len(json.dumps(report)) < 100000


def test_scheduler_window_ratio_and_load_use_independent_scales_not_raw_load():
    analyzer = FlightHealthAnalyzer(0, 3)
    analyzer.observe(event("PM", 1, {"NLon": integer(2, "H"), "NL": integer(100, "H"),
        "Load": (250, "H", "%", .1, "%"), "LR": (400, "H", "Hz", 1, "z"),
        "MaxT": (5000, "I", "", 1, "-"), "Mem": (10000, "I", "B", 1, "b")}), 1)
    analyzer.observe(event("PM", 2, {"NLon": integer(200, "H"), "NL": integer(100, "H")}), 2)
    report = analyzer.finish()
    assert metric(report, "scheduler", "long_loop_fraction", instance="board")["mean"] == .02
    assert metric(report, "scheduler", "long_loop_fraction", instance="board")["excluded_count"] == 1
    assert metric(report, "scheduler", "load_percent", instance="board")["mean"] == 25
    assert metric(report, "scheduler", "maximum_loop_us", instance="board")["status"] == "unavailable"
    assert report["findings"][0]["observation_count"] == 1


def test_rail_minima_have_explicit_units_and_never_claim_a_brownout():
    analyzer = FlightHealthAnalyzer(0, 3)
    analyzer.observe(event("POWR", 1, {"Vcc": (5.2, "f", "V", 1, "v"),
        "VServo": (0, "f", "V", 1, "v"), "Flags": integer(3, "H")}), 1)
    analyzer.observe(event("POWR", 2, {"Vcc": (4.8, "f", "V", 1, "v")}), 2)
    analyzer.observe(event("MCU", 2, {"MVolt": (3300, "f", "mV", 1, "v")}), 2)
    report = analyzer.finish()
    assert metric(report, "power_rails", "vcc_v", instance="board", message="POWR")["minimum"] == 4.8
    assert metric(report, "power_rails", "vcc_v", instance="board", message="POWR")["mean"] == 5
    assert metric(report, "power_rails", "mvolt_v", instance="board", message="MCU")["status"] == "unavailable"
    assert report["findings"] == []


def test_clock_scope_gaps_duplicates_and_barriers_do_not_merge_measurements():
    analyzer = FlightHealthAnalyzer(0, 10)
    for stamp, boot in ((1, 0), (2, 0), (2, 0), (6, 0), (7, 1)):
        analyzer.observe(gps(stamp, boot=boot), stamp)
    analyzer.observe({"kind": "diagnostic", "metric_barrier": True}, None)
    analyzer.observe(gps(8), 8)
    report = analyzer.finish()
    summary = metric(report, "gnss", "hdop")
    assert summary["sample_count"] == 4 and summary["excluded_count"] == 1
    assert summary["gap_count"] == 2
    assert report["barrier_count"] == 1
    assert report["excluded_records"]["different_source_or_clock_segment"] == 1


def test_unknown_instances_or_wire_formats_do_not_gain_default_identity():
    analyzer = FlightHealthAnalyzer(0, 2)
    row = gps(1)
    del row["raw_fields"]["I"]
    analyzer.observe(row, 1)
    row = gps(1)
    row["source_format"] = "tlog"
    analyzer.observe(row, 1)
    assert analyzer.finish()["status"] == "unavailable"
    assert analyzer.finish()["excluded_records"] == {"missing_instance_or_schema": 1, "unsupported_schema_or_time_domain": 1}


def test_channel_limits_are_explicit_and_never_create_unbounded_state():
    analyzer = FlightHealthAnalyzer(0, 2)
    for index in range(MAX_CHANNELS + 5):
        analyzer.observe(gps(1, instance=index), 1)
    report = analyzer.finish()
    assert len(domain(report, "gnss")["channels"]) == MAX_CHANNELS
    assert report["excluded_records"]["channel_limit"] == 5


def test_oversized_text_integer_is_unavailable_without_aborting_report():
    analyzer = FlightHealthAnalyzer(0, 2)
    analyzer.observe(event("PM", 1, {"Load": (10**400, "H", "%", .1, "%")}), 1)
    report = analyzer.finish()
    assert metric(report, "scheduler", "load_percent", instance="board")["status"] == "unavailable"
    assert report["findings"] == []
    json.dumps(report, allow_nan=False)


def test_esc_instance_dropped_by_channel_limit_prevents_partial_set_spreads():
    analyzer = FlightHealthAnalyzer(0, 2)
    for index in range(MAX_CHANNELS - 2):
        analyzer.observe(gps(1, instance=index), 1)
    for index, rpm in enumerate((1000, 1100, 9000)):
        analyzer.observe(esc(1, index, rpm=rpm), 1)
    report = analyzer.finish()
    assert domain(report, "esc")["observed_instances"] == ["0", "1", "2"]
    assert all(item["status"] == "unavailable" for item in domain(report, "esc")["matched_frame_spreads"])
    assert report["excluded_records"]["channel_limit"] == 1


@pytest.mark.parametrize("args", [(0, 0), (2, 1), (-1, 1), (0, float("inf")), (0, 1, 0), (False, 2)])
def test_invalid_scope_is_rejected(args):
    with pytest.raises(ValueError): FlightHealthAnalyzer(*args)


def test_real_dataflash_decoder_to_insight_report_keeps_units_and_health_evidence(tmp_path):
    # Use the repository's existing valid schema metadata instead of a guessed
    # UNIT record width; append the health message definitions under unused IDs.
    from pathlib import Path
    base = Path(__file__).resolve().parents[2] / "fixtures/logs/synthetic-flight.log"
    header = [line for line in base.read_text().splitlines() if line.startswith(("FMT,", "FMTU,", "UNIT,", "MULT,"))]
    header += [
        "FMT, 150, 16, GPS, QBBBc, TimeUS,I,Status,NSats,HDop",
        "FMT, 151, 18, GPA, QBCCC, TimeUS,I,HAcc,VAcc,SAcc",
        "FMT, 152, 16, XKF4, QBcH, TimeUS,C,SV,FS",
        "FMT, 153, 22, ESC, QBffc, TimeUS,Instance,RPM,Curr,Temp",
        "FMT, 154, 23, PM, QHHHHI, TimeUS,NLon,NL,Load,LR,MaxT",
        "FMT, 155, 21, POWR, QffH, TimeUS,Vcc,VServo,Flags",
        "FMT, 156, 27, MCU, Qffff, TimeUS,MTemp,MVolt,MVmin,MVmax",
        "UNIT, 0, 35, instance", "UNIT, 0, 83, satellites", "UNIT, 0, 113, rpm",
        "UNIT, 0, 37, %", "UNIT, 0, 122, Hz", "UNIT, 0, 79, degC",
        "UNIT, 0, 109, m", "UNIT, 0, 110, m/s", "UNIT, 0, 45, ",
        "MULT, 0, 65, 0.1", "MULT, 0, 66, 0.01", "MULT, 0, 45, 0",
        "FMTU, 0, 150, s#-S-, F--0B", "FMTU, 0, 151, s#mmn, F-BBB",
        "FMTU, 0, 152, s#--, F---", "FMTU, 0, 153, s#qAO, F-00B",
        "FMTU, 0, 154, s--%z-, F--A0-", "FMTU, 0, 155, svv-, F00-",
        "FMTU, 0, 156, sOvvv, F0000",
    ]
    rows = [
        "GPS, 1000000, 0, 2, 8, 1.25", "GPA, 1000000, 0, 0.5, 0.7, 0.2",
        "XKF4, 1000000, 0, 1.2, 2", "ESC, 1000000, 0, 1000, 4, 25",
        "ESC, 1000000, 1, 1200, 6, 30", "PM, 1000000, 2, 100, 250, 400, 5000",
        "POWR, 1000000, 5.2, 0, 3", "MCU, 1000000, 30, 3.3, 3.2, 3.4",
        "GPS, 2000000, 0, 3, 14, 0.75", "POWR, 2000000, 4.8, 0, 3",
    ]
    path = tmp_path / "synthetic health.log"
    path.write_text("\n".join(header + rows) + "\n", encoding="utf-8")
    store = Store(tmp_path / "data")
    log = LogService(store).import_log({"path": str(path), "label": "Synthetic health decoder oracle", "synthetic": True})["log"]
    report = InsightService(store).report({"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
        "interval_start_s": 0, "interval_end_s": 2})["report"]
    health = report["health"]
    assert all(item["status"] == "observed" for item in health["domains"])
    assert metric(health, "gnss", "hdop", message="GPS")["mean"] == 1
    assert metric(health, "gnss", "horizontal_accuracy_m", message="GPA")["mean"] == .5
    assert metric(health, "estimator", "sv", message="XKF4")["maximum"] == 1.2
    assert metric(health, "scheduler", "load_percent", instance="board")["mean"] == 25
    assert metric(health, "scheduler", "maximum_loop_us", instance="board")["status"] == "unavailable"
    assert metric(health, "power_rails", "vcc_v", instance="board", message="POWR")["minimum"] == 4.8
    assert len(health["findings"]) == 4
    assert report["scope"]["synthetic"]
    json.dumps(report, allow_nan=False)
