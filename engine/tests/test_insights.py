"""Synthetic log diagnostics plus independent numerical FFT/integration oracles."""
from concurrent.futures import CancelledError
import cmath
import json
import math
from pathlib import Path

import pytest

from q22_engine.insights import InsightService, Spectrum, Trace, VibrationDistribution, fft, operating_profile, welch
from q22_engine.logs import LogService
from q22_engine.store import Store

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/logs/synthetic-flight.log"
EXTRA_HEADER = [
    "FMT, 136, 28, VIBE, QBfffI, TimeUS,IMU,VibeX,VibeY,VibeZ,Clip",
    "FMT, 137, 32, GYR, QQBfff, TimeUS,SampleUS,I,GyrX,GyrY,GyrZ",
    "FMT, 138, 15, RCOU, QHH, TimeUS,C1,C2",
    "UNIT, 0, 97, m/s/s", "UNIT, 0, 103, rad/s", "UNIT, 0, 117, us",
    "FMTU, 0, 136, s#aaa-, F-000-",
    "FMTU, 0, 137, ss#ggg, FF-000", "FMTU, 0, 138, suu, F00",
]


@pytest.fixture
def services(tmp_path):
    store = Store(tmp_path / "données")
    yield store, LogService(store), InsightService(store)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


def imported(services, tmp_path, rows, *, units=True):
    header = [line for line in FIXTURE.read_text().splitlines() if line.startswith(("FMT,", "UNIT,", "MULT,", "FMTU,"))]
    extra = [line for line in EXTRA_HEADER if units or not line.startswith("FMTU, 0, 136")]
    path = tmp_path / "synthetic-insights.log"
    path.write_text("\n".join(header + extra + rows) + "\n")
    return services[1].import_log({"path": str(path), "label": "Synthetic diagnostic oracle", "synthetic": True})["log"]


def report(services, log, **changes):
    return services[2].report({"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
                               "interval_start_s": 1, "interval_end_s": 4, "max_gap_s": 1.5, **changes})["report"]


def test_vibration_excursions_and_counter_deltas_do_not_count_initial_total_or_cross_gap(services, tmp_path):
    log = imported(services, tmp_path, [
        "PARM, 0, INS_GYRO_FILTER, 20, 20",
        "VIBE, 1000000, 0, 10, 5, 2, 100", "VIBE, 2000000, 0, 35, 5, 2, 103",
        "VIBE, 3000000, 0, 70, 5, 2, 105", "VIBE, 4000000, 0, 20, 5, 2, 1",
        "VIBE, 8000000, 0, 80, 5, 2, 9"])
    value = report(services, log, interval_end_s=9)
    assert value["vibration"]["clipping"]["observed_deltas"] == {"0:Clip": 5}
    assert value["vibration"]["clipping"]["gap_count"] == 1
    assert value["vibration"]["clipping"]["resets"][0]["after"] == 1
    excursions = value["vibration"]["excursions"]
    assert [(row["details"]["start_s"], row["details"]["end_s"], row["details"]["peak_mps2"]) for row in excursions] == [(2, 3, 70), (8, 8, 80)]
    assert excursions[0]["evidence"]["time_s"] == 3
    assert value["scope"]["synthetic"] is True
    assert value["parameter_evidence"]["state"] == "observed_only"
    assert value["spectrum"]["status"] == "unavailable"
    assert all(action["proposed_values"] is None for action in value["recommendations"])
    assert len(json.dumps(value).encode()) < 4 * 1024 * 1024


def test_unknown_vibration_units_never_create_numeric_axis_findings(services, tmp_path):
    log = imported(services, tmp_path, ["VIBE, 1000000, 0, 900, 900, 900, 0", "VIBE, 2000000, 0, 950, 900, 900, 1"], units=False)
    value = report(services, log)
    assert value["vibration"]["status"] == "unavailable"
    assert value["vibration"]["series"] == [] and value["vibration"]["excursions"] == []
    assert value["vibration"]["clipping"]["observed_deltas"] == {"0:Clip": 1}
    assert value["parameter_evidence"]["attribution_allowed"] is False


def test_vibration_instances_and_boot_epochs_never_merge(services, tmp_path):
    log = imported(services, tmp_path, [
        "VIBE, 1000000, 0, 80, 0, 0, 100", "VIBE, 1000000, 1, 1, 0, 0, 999",
        "VIBE, 2000000, 0, 90, 0, 0, 101", "VIBE, 2000000, 1, 2, 0, 0, 999",
        "VIBE, 1000, 0, 1, 0, 0, 0", "VIBE, 1001000, 0, 2, 0, 0, 1"])
    first = report(services, log)
    second = report(services, log, boot_epoch=1, interval_start_s=0, interval_end_s=2)
    assert first["vibration"]["clipping"]["observed_deltas"] == {"0:Clip": 1}
    assert second["vibration"]["clipping"]["observed_deltas"] == {"0:Clip": 1}
    assert len(first["vibration"]["excursions"]) == 1
    assert second["vibration"]["excursions"] == []


def test_old_clip_layout_remains_separate_from_new_per_imu_layout(services, tmp_path):
    header = [line for line in FIXTURE.read_text().splitlines() if line.startswith(("FMT,", "UNIT,", "MULT,", "FMTU,"))]
    path = tmp_path / "legacy.log"
    path.write_text("\n".join(header + [
        "FMT, 136, 35, VIBE, QfffIII, TimeUS,VibeX,VibeY,VibeZ,Clip0,Clip1,Clip2",
        "UNIT, 0, 97, m/s/s", "FMTU, 0, 136, saaa---, F000---",
        "VIBE, 1000000, 2, 3, 4, 10, 20, 30", "VIBE, 2000000, 2, 3, 4, 11, 22, 33"]) + "\n")
    log = services[1].import_log({"path": str(path), "label": "Synthetic legacy", "synthetic": True})["log"]
    value = report(services, log)
    assert value["vibration"]["clipping"]["observed_deltas"] == {"reported:Clip0": 1, "reported:Clip1": 2, "reported:Clip2": 3}


def test_power_tracking_load_step_and_declared_payload_numerical_oracle(services, tmp_path):
    log = imported(services, tmp_path, [
        "PARM, 0, ATC_RAT_RLL_P, 0.1, 0.1",
        "BAT, 1000000, 0, 24, 10, 20, 80, 100", "ATT, 1000000, 0, 0, 0, 0, 0, 0",
        "BAT, 2000000, 0, 22, 20, 20, 80, 100", "ATT, 2000000, 2, 0, 0, 0, 0, 0"])
    value = report(services, log, interval_end_s=2, thresholds={"roll_error_deg": 1}, operating_profile={
        "aircraft_mass_kg": 10, "payload_mass_kg": 4, "rotor_count": 4, "propeller_diameter_m": .5,
        "rotor_layout": "non_overlapping", "battery_cell_count": 6, "declared_firmware": "Copter-4.7.1"})
    metrics = value["metrics"]
    assert metrics["mean_power_w"]["value"] == 340
    assert metrics["energy_wh"]["value"] == pytest.approx(340 / 3600)
    assert metrics["roll_tracking_rmse_deg"]["value"] == pytest.approx(math.sqrt(4 / 3))
    assert metrics["minimum_pack_voltage_v"]["value"] == 22
    context = value["operating_profile"]["derived_context"]
    assert context["payload_mass_fraction"] == .4
    assert context["mean_electrical_power_w_per_kg"] == 34  # payload is not added a second time
    assert context["disk_loading_n_m2"] == pytest.approx(10 * 9.80665 / (4 * math.pi * .25**2))
    assert context["minimum_mean_cell_voltage_v"] == pytest.approx(22 / 6)
    assert context["endurance_estimate_minutes"] is None
    sag = next(item for item in value["findings"] if item["kind"] == "battery_load_step_drop")
    assert sag["details"]["voltage_drop_v"] == 2 and sag["details"]["current_rise_a"] == 10
    assert value["guidance"]["firmware_compatibility"]["exact_build_validated"] is False


def test_long_gaps_and_corruption_exclude_integration_and_parameter_attribution(services, tmp_path):
    log = imported(services, tmp_path, [
        "PARM, 0, ATC_RAT_RLL_P, 0.1, 0.1", "BAT, 1000000, 0, 24, 10, 20, 80, 100",
        "CORRUPTED_UNRECOGNIZED_RECORD", "BAT, 2000000, 0, 24, 10, 20, 80, 100",
        "BAT, 5000000, 0, 24, 10, 20, 80, 100"])
    value = report(services, log, interval_end_s=5)
    assert value["metrics"]["energy_wh"]["value"] is None
    assert value["parameter_evidence"]["state"] == "uncertain"
    assert value["parameter_evidence"]["attribution_allowed"] is False


def test_parameters_mixed_stale_missing_and_loaded_file_comparison(services, tmp_path):
    log = imported(services, tmp_path, [
        "PARM, 0, ATC_RAT_RLL_P, 0.1, 0.1", "VIBE, 1000000, 0, 1, 1, 1, 0",
        "PARM, 2000000, ATC_RAT_RLL_P, 0.2, 0.1", "VIBE, 3000000, 0, 2, 2, 2, 0",
        "VIBE, 401000000, 0, 3, 3, 3, 0"])
    snapshot = services[0].add_document("snapshot", {"parameters": {"ATC_RAT_RLL_P": {"value": "0.3"}, "INS_GYRO_FILTER": {"value": "20"}}})
    value = report(services, log, parameter_snapshot_id=snapshot["id"])
    assert value["parameter_evidence"]["state"] == "mixed"
    assert value["parameter_evidence"]["changes"][0]["evidence"]["time_s"] == 2
    assert value["parameter_evidence"]["attribution_allowed"] is False
    loaded = value["parameter_evidence"]["loaded_snapshot"]
    assert loaded["different_count"] == 1 and loaded["loaded_not_observed_count"] == 1
    stale = report(services, log, interval_start_s=400, interval_end_s=402)
    assert stale["parameter_evidence"]["state"] == "stale"
    missing = imported(services, tmp_path, ["VIBE, 1000000, 0, 1, 1, 1, 0"])
    assert report(services, missing)["parameter_evidence"]["state"] == "missing"


def test_pwm_headroom_requires_declared_mapping_calibration_and_units(services, tmp_path):
    log = imported(services, tmp_path, ["RCOU, 1000000, 1980, 1500", "RCOU, 2000000, 1990, 1550"])
    assert report(services, log)["metrics"]["command_headroom_fraction"]["value"] is None
    value = report(services, log, operating_profile={"actuator_protocol": "pwm", "motor_output_channels": [1, 2],
                                                    "output_low_us": 1000, "output_high_us": 2000})
    metric = value["metrics"]["command_headroom_fraction"]
    assert metric["value"] == pytest.approx(.01) and metric["sample_count"] == 4
    assert "not thrust" in metric["interpretation"]


def test_loaded_float32_serialization_difference_is_not_claimed_as_setting_change(services, tmp_path):
    log = imported(services, tmp_path, ["PARM, 0, ATC_RAT_RLL_P, 0.10000000149011612, 0.1",
                                       "VIBE, 1000000, 0, 1, 1, 1, 0"])
    snapshot = services[0].add_document("snapshot", {"parameters": {"ATC_RAT_RLL_P": {"value": "0.1"}}})
    value = report(services, log, parameter_snapshot_id=snapshot["id"])["parameter_evidence"]
    assert value["loaded_snapshot"]["different_count"] == 1
    assert "unknown serialization precision" in value["loaded_snapshot"]["comparison_basis"]
    assert "not proof" in value["loaded_snapshot"]["comparison_basis"]
    assert value["change_count"] == 0 and value["attribution_allowed"] is False


@pytest.mark.parametrize("profile", [
    {"aircraft_mass_kg": 2, "payload_mass_kg": 3}, {"aircraft_mass_kg": True}, {"rotor_count": 3.5},
    {"battery_cell_count": 0}, {"propeller_diameter_m": float("nan")}, {"motor_output_channels": [1, 1]},
    {"actuator_protocol": "dshot"}, {"output_low_us": 2000, "output_high_us": 1000}, {"weight_lb": 10},
])
def test_profile_invalid_units_ranges_or_unsupported_calibration_rejected(profile):
    with pytest.raises(ValueError):
        operating_profile(profile)


def test_fft_matches_independent_direct_dft():
    values = [math.sin(i * .7) + i / 11 for i in range(16)]
    actual = fft(values)
    expected = [sum(value * cmath.exp(-2j * math.pi * k * index / len(values)) for index, value in enumerate(values)) for k in range(len(values))]
    assert actual == pytest.approx(expected, rel=1e-11, abs=1e-11)


def test_welch_density_normalization_matches_parseval_and_known_sine():
    n, rate, frequency = 256, 512, 64
    values = [3 + 2 * math.sin(2 * math.pi * frequency * i / rate) for i in range(n)]
    density, count = welch(values, rate, n)
    assert count == 1
    assert max(range(len(density)), key=density.__getitem__) * rate / n == frequency
    weights = [.5 - .5 * math.cos(2 * math.pi * i / (n-1)) for i in range(n)]
    mean = sum(values) / n
    oracle = sum(((value - mean) * weight)**2 for value, weight in zip(values, weights)) / sum(weight**2 for weight in weights)
    assert sum(density) * rate / n == pytest.approx(oracle, rel=1e-12)
    assert oracle == pytest.approx(2, rel=1e-6)  # amplitude² / 2


def spectrum_config(**changes):
    return {"sample_rate_hz": 400, "anti_alias_cutoff_hz": 100,
            "anti_alias_evidence": "Synthetic oracle assumes an ideal acquisition filter; not hardware evidence",
            "sensor_instance": "0", "axis": "x", "window_samples": 256, **changes}


def test_real_gyr_schema_qualifies_conditional_spectrum_and_vibe_never_substitutes(services, tmp_path):
    rows = ["PARM, 0, INS_GYRO_FILTER, 20, 20"]
    for index in range(600):
        stamp = 1_000_000 + index * 2500
        value = math.sin(2 * math.pi * 50 * index / 400)
        rows.append(f"GYR, {stamp}, {stamp}, 0, {value}, 0, 0")
    log = imported(services, tmp_path, rows)
    value = report(services, log, interval_end_s=3, spectrum=spectrum_config())
    spectrum = value["spectrum"]
    assert spectrum["status"] == "conditional"
    assert spectrum["peaks"][0]["frequency_hz"] == pytest.approx(50)
    assert spectrum["sample_count"] == 512 and spectrum["continuous_sequence_samples"] == 600
    assert spectrum["scope_end"]["time_s"] == pytest.approx(1 + 511 / 400)
    assert max(item["frequency_hz"] for item in spectrum["bins"]) <= 100
    assert "not independently verified" in spectrum["anti_alias_basis"]
    assert report(services, log)["spectrum"]["status"] == "unavailable"


def test_spectrum_gap_jitter_units_and_instance_gates(services, tmp_path):
    rows = []
    for index in range(300):
        stamp = 1_000_000 + index * 2500 + (10000 if index >= 150 else 0)
        rows.append(f"GYR, {stamp}, {stamp}, 0, {math.sin(index)}, 0, 0")
    log = imported(services, tmp_path, rows)
    assert report(services, log, spectrum=spectrum_config())["spectrum"]["status"] == "unavailable"
    assert report(services, log, spectrum=spectrum_config(sensor_instance="1"))["spectrum"]["qualifying_samples"] == 0
    assert report(services, log, spectrum=spectrum_config(sample_rate_hz=500))["spectrum"]["status"] == "unavailable"
    with pytest.raises(ValueError, match="45%"):
        report(services, log, spectrum=spectrum_config(anti_alias_cutoff_hz=201))


def test_trace_linear_error_oracle_and_boundary_clipping():
    trace = Trace(.5, 1.5, 3)
    trace.add(0, 0, {})
    trace.add(2, 2, {})
    assert trace.covered == 1
    assert trace.integral == 1
    assert trace.square == pytest.approx((1.5**3 - .5**3) / 3)


def test_vibration_distribution_separates_sample_quantiles_from_time_weighted_exposure():
    distribution = VibrationDistribution(0, 5, 1.5, 30, 60)
    for stamp, value in [(0, 0), (1, 60), (2, 0), (4, 80), (5, 80)]:
        distribution.add(stamp, value, {})
    summary = distribution.summary()
    assert summary["p50_mps2"] == 60
    assert summary["covered_duration_s"] == 3  # two-second gap is excluded
    assert summary["time_above_warning_s"] == pytest.approx(2)
    assert summary["time_above_critical_s"] == pytest.approx(1)
    assert summary["distribution_approximate"] is False
    distribution.trace.barrier()
    distribution.add(1, 1000, {})
    assert distribution.summary()["maximum_mps2"] == 80


def test_vibration_quantile_reservoir_is_bounded_and_declares_sampling():
    distribution = VibrationDistribution(0, 60, .01, 30, 60)
    for index in range(10000): distribution.add(index*.005, index % 100, {})
    value = distribution.summary()
    assert value["sample_count"] == 10000
    assert value["retained_distribution_samples"] == 4096 and value["distribution_approximate"]
    assert value["p50_mps2"] == pytest.approx(49.5, abs=2)


@pytest.mark.parametrize("explicit_barrier", [False, True])
def test_trace_clock_regression_never_reintegrates_covered_time(explicit_barrier):
    trace = Trace(0, 5, 10)
    for stamp in (0, 3):
        trace.add(stamp, 1, {})
    if explicit_barrier:
        trace.barrier()
    for stamp in (1, 2, 3, 4, 5):
        trace.add(stamp, 1, {})
    assert trace.covered == 4 and trace.integral == 4
    assert trace.metric(4, "W", "oracle")["coverage_fraction"] == .8
    assert trace.count == 4  # replayed samples cannot become new extrema/counts


def test_sample_clock_outside_interval_cannot_be_assigned_to_log_time_scope(services, tmp_path):
    rows = [f"GYR, {1_000_000 + i * 2500}, {100_000_000 + i * 2500}, 0, {math.sin(i)}, 0, 0" for i in range(600)]
    log = imported(services, tmp_path, rows)
    value = report(services, log, interval_end_s=3, spectrum=spectrum_config())["spectrum"]
    assert value["status"] == "unavailable" and value["qualifying_samples"] == 0
    assert value["clock_exclusions"] == 600


def test_spectrum_sample_cap_remains_bounded_and_reports_actual_used_clock():
    spectrum = Spectrum(spectrum_config(), start=1, end=20)
    for index in range(5000):
        stamp = 1_000_000 + index * 2500
        spectrum.add({"message_type": "GYR", "source_format": "log", "time_us": stamp,
                      "record_index": index + 1, "raw_fields": {"I": 0, "SampleUS": stamp, "GyrX": math.sin(index)},
                      "field_schema": {"SampleUS": {"unit": "s", "applied_multiplier": 1e-6},
                                       "GyrX": {"unit": "rad/s", "applied_multiplier": 1}}}, stamp / 1e6)
    value = spectrum.finish()
    assert value["status"] == "conditional"
    assert value["sample_count"] == value["continuous_sequence_samples"] == 4096
    assert value["sample_cap_reached"] and value["continuity_boundaries"] == 0
    assert value["measurement_interval_end_s"] == pytest.approx(1 + 4095 / 400)


def test_vibration_coverage_and_bounded_findings_do_not_hide_late_recommendation(services, tmp_path):
    rows = [f"VIBE, {1_000_000 + i * 10000}, 0, {80 if i == 1199 else 2}, 1, 1, {i}" for i in range(1200)]
    value = report(services, imported(services, tmp_path, rows), interval_end_s=14)
    assert len(value["findings"]) == 100 and value["findings_truncated"]
    assert len(value["vibration"]["series"]) <= 3 * 256
    coverage = value["vibration"]["coverage"][0]
    assert coverage["sample_count"] == 1200
    assert coverage["covered_duration_s"] == pytest.approx(11.99)
    assert coverage["coverage_fraction"] == pytest.approx(11.99 / 13)
    assert value["vibration"]["clipping"]["coverage"][0]["valid_pair_count"] == 1199
    assert any(item["id"] == "inspect_vibration" for item in value["recommendations"])
    assert len(json.dumps(value).encode()) < 1024 * 1024


def test_shared_synthetic_insight_fixture_has_stable_diagnostic_oracles(services):
    log = services[1].import_log({"path": str(FIXTURE.with_name("synthetic-insights.log")),
                                  "label": "SYNTHETIC diagnostic oracle", "synthetic": True})["log"]
    value = report(services, log, thresholds={"roll_error_deg": 1})
    assert value["metrics"]["mean_power_w"]["value"] == pytest.approx(1160 / 3)
    assert value["metrics"]["energy_wh"]["value"] == pytest.approx(1160 / 3600)
    assert value["metrics"]["roll_tracking_rmse_deg"]["value"] == pytest.approx(math.sqrt(53 / 9))
    assert value["vibration"]["clipping"]["observed_deltas"] == {"0:Clip": 5}
    assert value["vibration"]["clipping"]["reset_count"] == 1
    assert value["parameter_evidence"]["observations_eligible_for_review"] is True
    assert value["parameter_evidence"]["attribution_allowed"] is False
    assert any("flight status unknown" in warning for warning in value["warnings"])


def test_integrity_cancellation_and_explicit_source_gates(services, tmp_path):
    log = imported(services, tmp_path, ["VIBE, 1000000, 0, 1, 1, 1, 0"])
    with pytest.raises(ValueError, match="explicit source"):
        report(services, log, source_key=None)
    with pytest.raises(CancelledError):
        services[2].report({"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
                             "interval_start_s": 0, "interval_end_s": 2}, cancel_check=lambda: True)
    saved = services[0].get_document("log", log["id"])
    shard = services[1].artifact(saved) / saved["derived_shards"][0]["name"]
    with shard.open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError, match="integrity"):
        report(services, log)
