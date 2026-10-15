"""Saved-report comparisons preserve uncertainty and historical evidence."""
from copy import deepcopy
import json

import pytest

from q22_engine.insight_comparison import compare_reports
from q22_engine.insight_service import InsightReports
from q22_engine.store import Store
from test_alignment import services


def report(*, identifier="before", digest="a", value="0.1", state="observed_only", power=100):
    return {"id": identifier, "algorithm_version": "test-metrics/1",
        "scope": {"source_sha256": digest * 64, "source_key": "1:1", "boot_epoch": 0,
                  "time_domain": "flight_controller_boot", "origin_time_us": "1000000",
                  "interval_start_s": 0, "interval_end_s": 10, "max_gap_s": 1, "battery_id": "0", "synthetic": False},
        "operating_profile": {"aircraft_mass_kg": 20, "payload_mass_kg": 4, "rotor_count": 4,
            "propeller_diameter_m": .5, "battery_cell_count": 6, "battery_capacity_ah": 20,
            "rotor_layout": "non_overlapping", "flight_phase": "hover", "declared_firmware": "Copter-4.7.1",
            "actuator_protocol": "pwm", "motor_output_channels": [1, 2], "output_low_us": 1000, "output_high_us": 2000},
        "metrics": {
            "mean_power_w": {"value": power, "unit": "W", "sample_count": 10, "coverage_fraction": .9, "exclusion_count": 0},
            "roll_tracking_rmse_deg": {"value": 2, "unit": "deg", "sample_count": 10, "coverage_fraction": .9},
            "command_headroom_fraction": {"value": .2, "unit": "fraction", "sample_count": 10}},
        "parameter_evidence": {"state": state, "observations_eligible_for_review": state == "observed_only",
            "observations": [{"name": "ATC_RAT_RLL_P", "value": value, "age_at_start_s": 5,
                              "evidence": {"record_index": 10, "time_s": -5}}]},
        "vibration": {"distributions": [{"instance_id": "0", "axis": "x", "p95_mps2": 20, "maximum_mps2": 22}],
                      "coverage": [{"instance_id": "0", "axis": "x", "sample_count": 10, "coverage_fraction": .9}]},
        "control": {"status": "observed", "axis": "roll", "sample_count": 10,
                    "tracking": {"rmse_deg_s": 3, "coverage_fraction": .9}}}


def metric(result, name):
    return next(row for row in result["metrics"] if row["name"] == name)


def codes(result):
    return {item["code"] for item in result["confounders"]}


def verified_environment(report, *, identifier="environment-a", release="24.04", gcs="4.4", flight="flight-a"):
    snapshot = {"id": identifier, "status": "captured", "host": {"os": "Linux", "release": release},
                "declared_context": {"gcs_name": "QGroundControl", "gcs_version": gcs},
                "software": {}, "detected_applications": [], "load": {}}
    report["flight_context"] = {"status": "verified", "flight_record_id": flight,
        "hardware_uid": "board-a", "aircraft_id": "drone-a", "environment_snapshot": snapshot,
        "linkage": {"association_id": "association-a", "scope": {key: report["scope"][key]
            for key in ("source_sha256", "source_key", "boot_epoch", "interval_start_s", "interval_end_s")}}}
    return snapshot


def test_actual_numeric_changes_have_related_metric_observations_not_causal_claims():
    before, after = report(), report(identifier="after", digest="b", value="0.2", power=120)
    after["metrics"]["roll_tracking_rmse_deg"]["value"] = 1
    original = deepcopy((before, after))
    result = compare_reports(before, after)
    assert metric(result, "mean_power_w")["delta"] == 20
    assert metric(result, "mean_power_w")["percent_change"] == 20
    assert result["parameter_diff"]["observations_eligible_for_review"]
    assert result["parameter_diff"]["counts"] == {"changed": 1, "before_only": 0, "after_only": 0, "compared": 1}
    explanation = result["parameter_explanations"][0]
    assert explanation["direction"] == "increased"
    assert "Proportional" in explanation["mechanism"]
    assert explanation["references"] and explanation["proposed_values"] is None
    assert {row["name"] for row in explanation["related_metrics"]} == {
        "roll_tracking_rmse_deg", "control.roll.tracking_rmse_deg_s", "command_headroom_fraction"}
    assert metric({"metrics": explanation["related_metrics"]}, "roll_tracking_rmse_deg")["delta"] == -1
    assert not explanation["causal_attribution"] and not result["causal_attribution"]
    assert result["status"] == "confounded"
    assert "aircraft_identity_unresolved" in codes(result)
    assert "incomplete_effective_configuration" in codes(result)
    assert (before, after) == original


def test_observation_gaps_are_not_added_or_removed_settings_and_decimal_equivalence():
    before, after = report(), report(identifier="after", digest="b", value="0.1000")
    before["parameter_evidence"]["observations"].append({"name": "INS_GYRO_FILTER", "value": "20"})
    after["parameter_evidence"]["observations"].append({"name": "MOT_SPIN_MIN", "value": "0.12"})
    diff = compare_reports(before, after)["parameter_diff"]
    assert diff["changed"] == []
    assert diff["before_only"] == [{"name": "INS_GYRO_FILTER", "value": "20"}]
    assert diff["after_only"] == [{"name": "MOT_SPIN_MIN", "value": "0.12"}]
    assert "added" not in diff and "removed" not in diff


@pytest.mark.parametrize("state", ["mixed", "missing", "stale", "uncertain"])
def test_unreliable_parameter_intervals_keep_the_context_but_gate_review(state):
    result = compare_reports(report(), report(identifier="after", digest="b", value="0.2", state=state))
    assert not result["parameter_diff"]["observations_eligible_for_review"]
    assert not result["parameter_explanations"][0]["observations_eligible_for_review"]
    assert "parameter_observations_uncertain" in codes(result)
    assert result["parameter_explanations"][0]["proposed_values"] is None


def test_semantic_mismatches_withhold_deltas_and_keep_both_measurements():
    before, after = report(), report(identifier="after", digest="b", power=120)
    after["metrics"]["mean_power_w"]["unit"] = "kW"
    result = compare_reports(before, after)
    assert metric(result, "mean_power_w")["status"] == "not_comparable"
    assert metric(result, "mean_power_w")["before"] == 100
    assert metric(result, "mean_power_w")["after"] == 120
    assert metric(result, "mean_power_w")["before_unit"] == "W"
    assert metric(result, "mean_power_w")["after_unit"] == "kW"
    assert metric(result, "mean_power_w")["delta"] is None
    del after["metrics"]["mean_power_w"]["unit"]
    assert metric(compare_reports(before, after), "mean_power_w")["after_unit"] is None
    after["algorithm_version"] = "another/2"
    result = compare_reports(before, after)
    assert metric(result, "roll_tracking_rmse_deg")["delta"] is None
    assert "algorithm_mismatch" in codes(result)
    after["algorithm_version"] = before["algorithm_version"]
    after["scope"]["time_domain"] = "utc_receive_us"
    result = compare_reports(before, after)
    assert metric(result, "roll_tracking_rmse_deg")["delta"] is None
    assert {"time_domain_mismatch", "receive_clock"} <= codes(result)


def test_monitor_and_pwm_calibration_changes_withhold_affected_metrics():
    before, after = report(), report(identifier="after", digest="b", power=120)
    after["scope"]["battery_id"] = "1"
    after["operating_profile"]["output_high_us"] = 1900
    result = compare_reports(before, after)
    assert metric(result, "mean_power_w")["delta"] is None
    assert metric(result, "command_headroom_fraction")["delta"] is None
    assert metric(result, "roll_tracking_rmse_deg")["delta"] == 0


def test_same_evidence_synthetic_conditions_and_coverage_are_explicit():
    before, after = report(), report(identifier="after", value="0.2")
    after["scope"]["synthetic"] = True
    after["operating_profile"]["payload_mass_kg"] = 5
    after["metrics"]["mean_power_w"].update(coverage_fraction=.3, exclusion_count=2)
    result = compare_reports(before, after)
    assert {"same_artifact", "overlapping_interval", "synthetic_mismatch", "condition_changed_payload_mass_kg",
            "limited_metric_coverage", "metric_exclusions"} <= codes(result)
    assert metric(result, "mean_power_w")["after_coverage"]["coverage_fraction"] == .3
    assert "same_report" in codes(compare_reports(before, before))


def test_only_exact_preserved_flight_linkage_can_provide_historical_environment():
    before, after = report(), report(identifier="after", digest="b")
    verified_environment(before)
    verified_environment(after, identifier="environment-b", release="26.04", gcs="5.0", flight="flight-b")
    result = compare_reports(before, after)
    assert result["environment_diff"]["status"] == "compared"
    assert {row["field"] for row in result["environment_diff"]["changes"]} == {"host.release", "declared_context.gcs_version"}
    assert "flight_environment_changed" in codes(result)
    assert "aircraft_identity_unresolved" not in codes(result)
    after["scope"]["interval_start_s"] = 2  # preserved association is now for the wrong interval
    result = compare_reports(before, after)
    assert result["environment_diff"]["status"] == "unavailable"
    assert "flight_environment_unknown" in codes(result)
    after["environment_snapshot"] = after.pop("flight_context")["environment_snapshot"]
    assert compare_reports(before, after)["environment_diff"]["status"] == "unavailable"


def test_historical_reports_never_inspect_current_host(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Comparing history must not inspect the current computer")
    import q22_engine.flight_environment as environment
    monkeypatch.setattr(environment.EnvironmentDetector, "inspect", forbidden)
    result = compare_reports(report(), report(identifier="after", digest="b"))
    assert result["environment_diff"]["status"] == "unavailable"


def test_missing_values_are_unavailable_and_zero_has_no_percent_change():
    before, after = report(power=0), report(identifier="after", digest="b", power=20)
    result = compare_reports(before, after)
    assert metric(result, "mean_power_w")["delta"] == 20
    assert metric(result, "mean_power_w")["percent_change"] is None
    assert metric(result, "energy_wh")["status"] == "unavailable"
    assert metric(result, "energy_wh")["before_unit"] is None
    assert metric(result, "energy_wh")["after_unit"] is None
    json.dumps(result, allow_nan=False)


def test_spectral_delta_requires_known_matching_acquisition_and_analysis_semantics():
    before, after = report(), report(identifier="after", digest="b")
    spectrum = {"status": "conditional", "method": "Welch", "source": "batch_imu", "filter_stage": "pre_filter",
        "axis": "x", "sensor_instance": "0", "window_samples": 256, "sample_rate_hz": 256,
        "frequency_resolution_hz": 1, "anti_alias_cutoff_hz": 80, "measurement_clock_field": "SampleUS",
        "unit": "(rad/s)^2/Hz", "sample_count": 256, "measurement_interval_start_s": 0, "measurement_interval_end_s": 1,
        "bins": [{"frequency_hz": 0, "psd": 0}, {"frequency_hz": 1, "psd": 2}, {"frequency_hz": 2, "psd": 4}]}
    before["spectrum"], after["spectrum"] = deepcopy(spectrum), deepcopy(spectrum)
    after["spectrum"]["bins"][2]["psd"] = 2
    result = compare_reports(before, after)
    row = metric(result, "spectrum.retained_band_power")
    assert row["before"] == 6 and row["after"] == 4 and row["delta"] == -2
    for key, value in (("filter_stage", "unknown"), ("axis", "y"), ("sample_rate_hz", 255.9)):
        changed = deepcopy(after)
        changed["spectrum"][key] = value
        result = compare_reports(before, changed)
        assert metric(result, "spectrum.retained_band_power")["delta"] is None
        assert "spectrum_semantics_unmatched" in codes(result)
    after["spectrum"]["bins"][2]["frequency_hz"] = 2.001
    assert metric(compare_reports(before, after), "spectrum.retained_band_power")["delta"] is None


def test_real_decoded_reports_can_be_compared_without_historical_baseline(tmp_path):
    from pathlib import Path
    from q22_engine.logs import LogService
    store = Store(tmp_path / "catalog")
    service = InsightReports(store)
    fixture = Path(__file__).resolve().parents[2] / "fixtures/logs/synthetic-insights.log"
    reports = []
    for index, value in enumerate(("20", "30")):
        path = tmp_path / f"current-flight-{index}.log"
        path.write_text(fixture.read_text().replace("INS_GYRO_FILTER, 20, 20", f"INS_GYRO_FILTER, {value}, 20"))
        log = LogService(store).import_log({"path": str(path), "label": f"Flight {index}", "synthetic": True})["log"]
        reports.append(service.dispatch("insights.report", {"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
            "interval_start_s": 1, "interval_end_s": 4, "max_gap_s": 1.1})["report"])
    result = service.dispatch("insights.compare", {"before_report_id": reports[0]["id"], "after_report_id": reports[1]["id"]})
    assert result["parameter_diff"]["observations_eligible_for_review"]
    assert result["parameter_diff"]["counts"]["changed"] == 1
    assert result["parameter_explanations"][0]["parameter"] == "INS_GYRO_FILTER"
    assert "Low-pass" in result["parameter_explanations"][0]["mechanism"]
    assert metric(result, "mean_power_w")["delta"] == 0
    assert metric(result, "vibration.0.x.p95_mps2")["delta"] == 0
    assert result["environment_diff"]["status"] == "unavailable"


def test_bounded_observations_and_explanations_do_not_silently_claim_complete_diff():
    before, after = report(), report(identifier="after", digest="b")
    before["parameter_evidence"]["observations"] = [{"name": f"TEST_{index}", "value": "1"} for index in range(150)]
    after["parameter_evidence"]["observations"] = [{"name": f"TEST_{index}", "value": "2"} for index in range(150)]
    result = compare_reports(before, after)
    assert len(result["parameter_diff"]["changed"]) == 128
    assert result["parameter_diff"]["truncated"]
    assert not result["parameter_diff"]["observations_eligible_for_review"]
    assert len(result["parameter_explanations"]) == 32
    assert result["parameter_explanations_truncated"]
    assert all(item["status"] == "unmapped_parameter" for item in result["parameter_explanations"])


def test_dispatch_uses_immutable_documents_and_rejects_bad_identifiers(tmp_path):
    store = Store(tmp_path / "catalog")
    before = store.add_document("insight_report", report())
    after = store.add_document("insight_report", report(identifier="after", digest="b", value="0.2"))
    service = InsightReports(store)
    result = service.dispatch("insights.compare", {"before_report_id": before["id"], "after_report_id": after["id"]})
    assert result["before_report_id"] == before["id"]
    assert result["after_report_id"] == after["id"]
    assert store.get_document("insight_report", before["id"]) == before
    assert store.get_document("insight_report", after["id"]) == after
    for value in (None, True, "", "x" * 101):
        with pytest.raises(ValueError, match="before_report_id"):
            service.dispatch("insights.compare", {"before_report_id": value, "after_report_id": after["id"]})
    with pytest.raises(ValueError, match="Unknown"):
        service.dispatch("insights.compare", {"before_report_id": "missing", "after_report_id": after["id"]})


def test_complete_preserved_readbacks_take_precedence_without_discarding_logged_observations(services, tmp_path):
    from test_flight_configuration import report_pair
    values = [(f"TEST_{index:04d}", str(index), 6) for index in range(150)]
    changed = values[:-1] + [("TEST_0149", "999", 6)]
    reports, _ = report_pair(services, tmp_path, values, changed)
    result = compare_reports(*reports, store=services[0])
    assert result["configuration_comparison"]["eligible_for_parameter_review"]
    assert result["parameter_diff"]["basis"] == "complete_typed_preflight_readbacks"
    assert result["parameter_diff"]["counts"]["compared"] == 150
    assert result["parameter_diff"]["changed"][0]["name"] == "TEST_0149"
    assert result["parameter_explanations"][0]["parameter"] == "TEST_0149"
    assert not result["parameter_explanations"][0]["parameter_type_changed"]
    assert result["observed_parameter_diff"]["basis"] == "observed_at_interval_start"
    assert result["parameter_diff"]["complete_effective_configuration"] is False
    assert result["parameter_explanations"][0]["status"] == "unmapped_parameter"


def test_incompatible_full_readbacks_keep_logged_fallback_explicit(services, tmp_path):
    from test_flight_configuration import report_pair
    reports, _ = report_pair(services, tmp_path)
    reports[1]["configuration"]["preflight"] = []
    result = compare_reports(*reports, store=services[0])
    assert not result["configuration_comparison"]["eligible_for_parameter_review"]
    assert result["parameter_diff"]["basis"] == "observed_at_interval_start"
    assert "complete_readback_comparison_unavailable" in codes(result)
