"""Descriptive control metrics: analytic dynamics and explicit rejection cases."""
import math
import random
from pathlib import Path

import pytest

from q22_engine.control_analysis import ControlAnalyzer, MAX_SAMPLES, correlation_lag, quantile
from q22_engine.insights import InsightService
from q22_engine.logs import LogService
from q22_engine.store import Store


def rate(stamp, desired, actual, *, units="deg/s", flags=0):
    return {"message_type": "RATE", "time_us": round(stamp*1e6), "record_index": round(stamp*100),
            "raw_fields": {"RDes": desired, "R": actual},
            "field_schema": {name: {"format_code": "f", "unit": units, "applied_multiplier": 1} for name in ("RDes", "R")}}


def pid(stamp, flags=0):
    raw = {"P": .3, "I": .2, "D": .1, "FF": .05, "DFF": .01, "Dmod": 1, "SRate": .4, "Flags": flags}
    return {"message_type": "PIDR", "raw_fields": raw, "record_index": round(stamp*100)+1,
            "field_schema": {name: {"format_code": "B" if name == "Flags" else "f"} for name in raw}}


def first_order(*, disturbance=None, firmware="Copter-4.7.1", phase="hover"):
    analyzer = ControlAnalyzer({"step": {"time_s": 2, "pre_s": .5, "post_s": 2}}, 0, 4, .1, firmware, phase)
    for index in range(401):
        stamp = index/100
        desired = 0 if stamp < 2 else 10
        actual = 0 if stamp < 2 else 10 * (1-math.exp(-(stamp-2)/.2))
        flag = 0
        if disturbance == "overlap" and 2.5 < stamp < 2.7: desired = 20
        if disturbance == "saturation" and 2.2 < stamp < 2.4: flag = 1
        if disturbance == "reset" and index == 210: flag = 4
        if disturbance == "gap" and 220 <= index < 235: continue
        if disturbance == "undertracking": actual *= .7
        if disturbance == "unexcited": desired = actual = 0
        analyzer.add(rate(stamp, desired, actual), stamp)
        if disturbance != "absent_flags": analyzer.add(pid(stamp, flag), stamp)
    return analyzer


def test_first_order_step_matches_analytic_rise_and_settling_oracle():
    value = first_order().finish()
    response = value["step"]
    assert response["status"] == "observed_response"
    assert response["rise_time_s"] == pytest.approx(.2*math.log(9), abs=.01)
    assert response["settling_time_s"] == pytest.approx(-.2*math.log(.05), abs=.01)
    assert response["overshoot_fraction"] == 0
    assert response["crossing_resolution_s"] == pytest.approx(.01)
    assert value["pid_terms"]["statistics"]["D"]["rms"] == pytest.approx(.1)


@pytest.mark.parametrize("disturbance", ["overlap", "saturation", "reset", "gap", "undertracking", "unexcited", "absent_flags"])
def test_invalid_step_evidence_withholds_response_classification(disturbance):
    assert first_order(disturbance=disturbance).finish()["step"]["status"] == "unavailable"


def test_unknown_firmware_phase_or_clipping_withholds_step_not_descriptive_tracking():
    for value in (first_order(firmware=None).finish(), first_order(phase="unknown").finish(), first_order().finish(clipping=True)):
        assert value["status"] == "observed"
        assert value["tracking"]["rmse_deg_s"] > 0
        assert value["step"]["status"] == "unavailable"


def test_tracking_linear_error_matches_exact_integral_not_sample_average():
    analyzer = ControlAnalyzer({}, 0, 1, 2, None)
    analyzer.add(rate(0, 0, 0), 0)
    analyzer.add(rate(1, 2, 0), 1)
    result = analyzer.finish()["tracking"]
    assert result["bias_deg_s"] == 1
    assert result["rmse_deg_s"] == pytest.approx(math.sqrt(4/3))
    assert result["absolute_error_p95_deg_s"] == pytest.approx(1.9)


def test_missing_units_never_become_rate_metrics_and_radians_convert_once():
    invalid = ControlAnalyzer({}, 0, 1, 1, None)
    invalid.add(rate(0, 2, 1, units="unknown"), 0)
    assert invalid.finish()["status"] == "unavailable"
    valid = ControlAnalyzer({}, 0, 1, 1, None)
    valid.add(rate(0, math.pi, 0, units="rad/s"), 0)
    assert valid.finish()["series"][0]["desired_deg_s"] == pytest.approx(180)
    event = rate(0, 1e6, 1)
    event["field_schema"]["RDes"]["applied_multiplier"] = 1e308
    invalid.add(event, 0)
    assert invalid.finish()["status"] == "unavailable"


def test_lag_matches_known_integer_delay_and_rejects_periodic_ambiguity():
    rng = random.Random(74391)
    desired = [rng.gauss(0, 10) for _ in range(512)]
    delayed = [0]*7 + desired[:-7]
    rows = [(i*.01, a, b, {}, 0) for i, (a,b) in enumerate(zip(desired, delayed))]
    lag = correlation_lag(rows)
    assert lag["status"] == "available" and lag["seconds"] == pytest.approx(.07)
    assert lag["correlation"] == pytest.approx(1)
    periodic = [(i*.01, 10*math.sin(i*math.pi/5), 10*math.sin((i-3)*math.pi/5), {}, 0) for i in range(512)]
    assert correlation_lag(periodic)["status"] == "ambiguous"


def test_clock_replay_gaps_and_bounded_capture_never_inflate_coverage():
    analyzer = ControlAnalyzer({}, 0, 100, .1, None)
    for index in range(MAX_SAMPLES+100):
        analyzer.add(rate(index*.01, 1, 0), index*.01)
    analyzer.barrier()
    for index in range(100): analyzer.add(rate(index*.01, 100, 0), index*.01)
    value = analyzer.finish()
    assert value["truncated"] and value["sample_count"] == MAX_SAMPLES
    assert value["tracking"]["coverage_fraction"] <= 1
    assert value["tracking"]["rmse_deg_s"] == pytest.approx(1)
    assert len(value["series"]) <= 512


@pytest.mark.parametrize("config", [{"axis": "up"}, {"step": {"time_s": 2, "pre_s": 8}}, {"step": {"time_s": 2, "settling_band_fraction": 0}}, {"step": {"time_s": float('nan')}}, {"gain": 1}])
def test_control_input_bounds(config):
    with pytest.raises(ValueError): ControlAnalyzer(config, 0, 4, 1, None)


def test_real_log_to_batch_control_and_filter_report_end_to_end(tmp_path):
    store = Store(tmp_path / "store")
    path = Path(__file__).resolve().parents[2] / "fixtures/logs/synthetic-control-batch.log"
    log = LogService(store).import_log({"path": str(path), "label": "SYNTHETIC numerical oracle", "synthetic": True})["log"]
    value = InsightService(store).report({"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
        "interval_start_s": 0, "interval_end_s": 4, "operating_profile": {"declared_firmware": "Copter-4.7.1", "flight_phase": "hover"},
        "control": {"axis": "roll", "step": {"time_s": 2, "pre_s": .5, "post_s": 2}},
        "spectrum": {"source": "batch_imu", "sensor_instance": "0", "axis": "x", "filter_stage": "pre",
            "sample_rate_hz": 400, "window_samples": 256, "anti_alias_cutoff_hz": 100,
            "anti_alias_evidence": "Synthetic reference only; no hardware anti-alias evidence"},
        "filter_model": {"sample_rate_hz": 400, "stages": [{"kind": "ardupilot_static_notch", "center_hz": 50, "bandwidth_hz": 20, "attenuation_db": 30}]}})["report"]
    assert value["scope"]["synthetic"] is True
    assert value["algorithm_version"] == "q22-offline-insights/2"
    assert value["control"]["step"]["status"] == "observed_response"
    assert value["control"]["step"]["rise_time_s"] == pytest.approx(.44)
    assert value["control"]["step"]["settling_time_s"] == pytest.approx(.6)
    assert value["spectrum"]["peaks"][0]["frequency_hz"] == pytest.approx(50)
    assert value["filter_model"]["status"] == "modeled_static"
    center = next(row for row in value["filter_model"]["response"] if row["frequency_hz"] == 50)
    assert center["attenuation_db"] == pytest.approx(30)
    assert value["filter_model"]["qualification"]["exact_firmware_parity"] is False
    modeled = value["modeled_spectrum"]
    assert modeled["status"] == "hypothetical_static_application"
    center_psd = next(row for row in modeled["bins"] if row["frequency_hz"] == 50)
    assert center_psd["modeled_psd"] / center_psd["observed_psd"] == pytest.approx(10**(-30/10))
    assert 0 < modeled["modeled_band_power"] < modeled["observed_band_power"]
    for path in tmp_path.rglob("*"):
        if path.is_file(): path.chmod(0o600)


@pytest.mark.parametrize("disturbance", ["parameter_change", "new_parameter_observation", "clipping_reset"])
def test_logged_configuration_change_or_clipping_reset_withholds_step(tmp_path, disturbance):
    original = Path(__file__).resolve().parents[2] / "fixtures/logs/synthetic-control-batch.log"
    lines = original.read_text().splitlines()
    if disturbance == "parameter_change":
        inserted = {"RATE, 2100000,": "PARM, 2100000, INS_GYRO_FILTER, 40, 20"}
    elif disturbance == "new_parameter_observation":
        inserted = {"RATE, 2100000,": "PARM, 2100000, ATC_RAT_RLL_P, 0.2, 0.1"}
    else:
        inserted = {"RATE, 1900000,": "VIBE, 1900000, 0, 1, 2, 3, 50",
                    "RATE, 2100000,": "VIBE, 2100000, 0, 1, 2, 3, 0"}
    output = []
    for line in lines:
        output.extend(extra for prefix, extra in inserted.items() if line.startswith(prefix))
        output.append(line)
    assert len(output) == len(lines) + len(inserted)
    path = tmp_path / f"synthetic-{disturbance}.log"
    path.write_text("\n".join(output) + "\n")
    store = Store(tmp_path / "store")
    log = LogService(store).import_log({"path": str(path), "label": f"Synthetic {disturbance}", "synthetic": True})["log"]
    report = InsightService(store).report({"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
        "interval_start_s": 0, "interval_end_s": 4,
        "operating_profile": {"declared_firmware": "Copter-4.7.1", "flight_phase": "hover"},
        "control": {"axis": "roll", "step": {"time_s": 2, "pre_s": .5, "post_s": 2}}})["report"]
    assert report["control"]["step"]["status"] == "unavailable"
    assert report["control"]["tracking"]["rmse_deg_s"] > 0
    if disturbance == "clipping_reset":
        assert report["vibration"]["clipping"]["reset_count"] == 1
    else:
        assert report["parameter_evidence"]["state"] == "mixed"
