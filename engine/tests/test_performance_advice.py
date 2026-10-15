"""Evidence gates and conditional mechanisms for researched performance advice."""
from copy import deepcopy
from concurrent.futures import CancelledError
from decimal import Decimal
import json

import pytest

from q22_engine import guidance
from q22_engine.performance_advice import build_performance_advice, explain_parameter_change
from test_alignment import services
from test_flight_configuration import report_pair


def advice(report):
    return {item["id"]: item for item in build_performance_advice(report)["recommendations"]}


def report_with_params(**parameters):
    return {"parameter_evidence": {"state": "observed_only", "observations": [
        {"name": name, "value": str(value), "age_at_start_s": 1} for name, value in parameters.items()]}}


def test_empty_report_requires_current_configuration_without_factory_baseline():
    result = build_performance_advice({})
    cards = {item["id"]: item for item in result["recommendations"]}
    assert set(cards) == {"establish_flight_configuration", "comparison_context"}
    setup = cards["establish_flight_configuration"]
    assert setup["status"] == "evidence_needed"
    assert "before arming" in setup["candidate_action"]
    assert "no pre-FACt or FACt file is required" in setup["verification_experiment"][0]
    assert all(item["proposed_values"] is None and not item["causal_attribution"] for item in cards.values())
    assert result["firmware_scope"]["exact_build_validated"] is False


def test_low_rate_vibration_prioritizes_mechanics_without_inventing_a_frequency():
    report = {"findings": [{"id": "finding-1", "kind": "accelerometer_clipping"}],
              "vibration": {"series": [{"time_s": 1, "value": 80}], "clipping": {"observed_deltas": {"0:Clip": 12}}},
              "spectrum": {"status": "unavailable"}}
    cards = advice(report)
    assert cards["mechanical_vibration"]["status"] == "inspect_first"
    assert cards["mechanical_vibration"]["priority"] < cards["notch_tracking_source"]["priority"]
    assert "cannot recover" in cards["mechanical_vibration"]["mechanism"]
    assert "VIBE alone cannot locate a notch" in cards["qualify_noise_measurement"]["symptom"]
    assert all(item["proposed_values"] is None for item in cards.values())
    assert cards["mechanical_vibration"]["evidence_ids"] == ["finding-1"]


def test_throttle_notch_suggests_rpm_as_conditional_alternative_not_protocol_prescription():
    report = report_with_params(INS_HNTCH_MODE=1, INS_HNTCH_REF=0.3, SERVO_BLH_POLES=14)
    card = advice(report)["notch_tracking_source"]
    assert "Bidirectional DShot is one way to obtain RPM, not a filter itself" in card["candidate_action"]
    assert any("pole count" in item for item in card["evidence_requirements"])
    assert any("ESC firmware" in item for item in card["evidence_requirements"])
    assert any("DMA/output support" in item for item in card["evidence_requirements"])
    assert any("dropouts" in item for item in card["evidence_requirements"])
    assert any("Serial ESC telemetry or supported CAN" in item for item in card["evidence_requirements"])
    assert any("phase delay" in item for item in card["tradeoffs"])
    assert len(card["alternative_hypotheses"]) >= 3
    assert card["status"] == "evidence_needed"
    assert {item["name"]: item["value"] for item in card["observed_parameters"]}["INS_HNTCH_MODE"] == "1"
    assert card["proposed_values"] is None


def test_spectrum_is_kept_conditional_and_does_not_prove_motor_noise_or_active_notch():
    report = report_with_params(INS_HNTCH_ENABLE=1, INS_HNTCH_MODE=3)
    report["spectrum"] = {"status": "conditional", "sample_rate_hz": 1000, "peaks": [{"frequency_hz": 173, "psd": 42}]}
    report["operating_profile"] = {"declared_firmware": "Copter-4.7.1"}
    cards = advice(report)
    assert cards["qualify_noise_measurement"]["status"] == "conditional_review"
    assert cards["notch_tracking_source"]["status"] == "evidence_needed"
    assert "do not establish tracking accuracy" in cards["notch_tracking_source"]["symptom"]
    assert cards["notch_tracking_source"]["firmware_scope"]["exact_build_validated"] is False
    assert "173" not in json.dumps(cards)
    assert all(item["proposed_values"] is None for item in cards.values())


def test_control_and_power_evidence_offer_multiple_causes_and_calibration_check():
    report = report_with_params(ATC_RAT_RLL_P=0.1, MOT_THST_EXPO=0.65, BATT_VOLT_MULT=10)
    report["control"] = {"status": "observed", "tracking": {"rmse_deg_s": 14}}
    report["findings"] = [{"id": "load", "kind": "battery_load_step_drop"}]
    cards = advice(report)
    assert len(cards["control_response"]["alternative_hypotheses"]) >= 3
    assert any("wind" in value for value in cards["control_response"]["alternative_hypotheses"])
    assert any("Measured thrust curve" in value for value in cards["propulsion_and_power"]["evidence_requirements"])
    assert "calibration" in cards["propulsion_and_power"]["candidate_action"]
    assert any("not measured thrust reserve" in value for value in cards["propulsion_and_power"]["tradeoffs"])
    assert "caused" in cards["comparison_context"]["tradeoffs"][0]


def test_pinned_citations_bound_output_and_do_not_mutate_the_report():
    report = report_with_params(**{f"INS_HNTCH_CUSTOM_{i}": i for i in range(200)})
    report["findings"] = [{"kind": "vibration_excursion", "id": "vibe"}]
    original = deepcopy(report)
    first = build_performance_advice(report)
    assert first == build_performance_advice(report)
    assert original == report
    assert len(first["recommendations"]) <= 8
    assert len(json.dumps(first)) < 100_000
    for card in first["recommendations"]:
        assert len(card["observed_parameters"]) <= 24
        assert card["references"]
        for source in card["references"]:
            assert source["revision"] in {guidance.WIKI_REVISION, guidance.FIRMWARE_REVISION}
            assert source["revision"] in source["url"]
            assert len(source["sha256"]) == 64
    first["recommendations"][0]["references"][0]["sha256"] = "changed"
    assert build_performance_advice(report)["recommendations"][0]["references"][0]["sha256"] != "changed"


@pytest.mark.parametrize("name,before,after,direction,fragment", [
    ("INS_HNTCH_BW", "40", "60", "increased", "phase-delay"),
    ("INS_HNTCH_HMNCS", "3", "5", "increased", "bitmask, not a harmonic count"),
    ("INS_HNTCH_FREQ", "100", "120", "increased", "not always the in-flight center"),
    ("INS_GYRO_FILTER", 40, 20, "decreased", "phase delay"),
    ("SERVO_BLH_POLES", 14, 12, "decreased", "electrical RPM"),
    ("ATC_RAT_RLL_D_FF", 0, 0.01, "increased", "Derivative feedforward"),
    ("ATC_RAT_RLL_D", 0.01, 0.02, "increased", "changing error"),
    ("ATC_RAT_RLL_P", 0.1, 0.1, "unchanged", "current rate error"),
    ("ATC_INPUT_TC", 0.2, 0.3, "increased", "requested response"),
    ("MOT_THST_EXPO", 0.6, 0.7, "increased", "Thrust mapping"),
    ("MOT_BAT_CURR_MAX", 40, 50, "increased", "current limiting"),
    ("BATT2_AMP_PERVLT", 10, 12, "increased", "reported voltage/current"),
    ("INS_LOG_BAT_OPT", 0, 4, "increased", "evidence acquired"),
])
def test_parameter_explanations_separate_observed_direction_from_conditional_effect(name, before, after, direction, fragment):
    result = explain_parameter_change(name, before, after, "unknown custom build")
    assert result["direction"] == direction
    assert result["status"] == "conditional_explanation"
    assert fragment in result["mechanism"]
    assert result["causal_attribution"] is False and result["proposed_values"] is None
    assert "not the direction of performance" in result["interpretation"]
    assert result["references"] and result["evidence_requirements"] and result["verification_metrics"]
    assert "unverified" in result["firmware_scope"]["scope"]


@pytest.mark.parametrize("name", ["UNKNOWN_SETTING", "INS_HNTCH_FUTURE", "CUSTOM_PID_P"])
def test_unknown_parameters_have_no_invented_mechanism(name):
    result = explain_parameter_change(name, 10, 20)
    assert result["status"] == "unmapped_parameter"
    assert "No reviewed mechanism" in result["mechanism"]
    assert "No benefit or detriment is inferred" in result["expected_effect"]


@pytest.mark.parametrize("before,after", [(True, False), ("NaN", "2"), (float("inf"), 2), (None, 2)])
def test_nonfinite_missing_or_boolean_changes_do_not_produce_numeric_direction(before, after):
    result = explain_parameter_change("INS_GYRO_FILTER", before, after)
    assert result["direction"] == "changed"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("name", ["", "A" * 65, "INS_GYRO_FILTER\n", "invalid setting", None])
def test_invalid_parameter_names_fail_closed(name):
    with pytest.raises(ValueError):
        explain_parameter_change(name, 1, 2)


def test_complete_preflight_values_beyond_both_previews_drive_relevant_advice(services, tmp_path):
    values = [(f"AAA_{index:04d}", str(index), 6) for index in range(150)] + [
        ("INS_HNTCH_MODE", "1", 6), ("SERVO_BLH_POLES", "14", 6)]
    reports, _ = report_pair(services, tmp_path, values, values)
    report = reports[0]
    report["parameter_evidence"] = {"state": "observed_only", "observations": [
        {"name": name, "value": value, "age_at_start_s": 0} for name, value, _ in values[:128]],
        "observations_truncated": True}
    assert "INS_HNTCH_MODE" not in {item["name"] for item in report["configuration"]["preflight"][0]["parameters_preview"]}
    untouched = deepcopy(report)
    result = build_performance_advice(report, services[0])
    context = result["parameter_context"]
    assert context["basis"] == "complete_typed_preflight_readback"
    assert context["complete_preflight_readback"] and context["parameter_count"] == 152
    assert context["logged_evidence"]["retained_observation_count"] == 128
    cards = {item["id"]: item for item in result["recommendations"]}
    card = cards["notch_tracking_source"]
    assert card["parameter_context_basis"] == "complete_typed_preflight_readback"
    mode = next(item for item in card["observed_parameters"] if item["name"] == "INS_HNTCH_MODE")
    assert mode["value"] == "1" and mode["mav_param_type"] == 6
    assert mode["canonical_token"] == "integer:1" and mode["effective_time_bounds"]["basis"] == "unknown"
    assert card["logged_parameters"] == []
    assert "review_preflight_readback" in cards and "establish_flight_configuration" not in cards
    assert context["effective_configuration"] == "unknown" and not context["causal_attribution"]
    assert not result["firmware_scope"]["exact_build_validated"]
    assert "parameters" not in context["readback_reference"]
    assert len(json.dumps(result)) < 100_000 and report == untouched


def test_readback_and_later_logged_values_remain_separate_without_causal_conflict_claims(services, tmp_path):
    values = [("INS_HNTCH_MODE", "1", 6), ("ATC_RAT_RLL_P", "0.1", 9), ("INS_HNTCH_BW", "5e-324", 10)]
    reports, _ = report_pair(services, tmp_path, values, values)
    report = reports[0]
    report["parameter_evidence"] = {"state": "mixed", "change_count": 1, "observations": [
        {"name": "INS_HNTCH_MODE", "value": "3"}, {"name": "ATC_RAT_RLL_P", "value": "0.1"},
        {"name": "INS_UNVERIFIED", "value": "99"}]}
    result = build_performance_advice(report, services[0])
    context = result["parameter_context"]
    assert context["numeric_disagreement_count"] == 2  # Includes exact REAL32 vs shortened log serialization.
    assert context["logged_only_names"] == ["INS_UNVERIFIED"]
    assert any("representation precision" in note for note in context["warnings"])
    card = next(item for item in result["recommendations"] if item["id"] == "notch_tracking_source")
    assert next(row for row in card["observed_parameters"] if row["name"] == "INS_HNTCH_MODE")["value"] == "1"
    assert next(row for row in card["logged_parameters"] if row["name"] == "INS_HNTCH_MODE")["value"] == "3"
    wide = next(row for row in card["observed_parameters"] if row["name"] == "INS_HNTCH_BW")
    assert wide["value"] == str(Decimal.from_float(5e-324))
    assert len(wide["value"]) > 256
    assert card["parameter_evidence_state"] == "mixed"
    assert all(not item["causal_attribution"] and item["proposed_values"] is None for item in result["recommendations"])


@pytest.mark.parametrize("failure", ["missing_store", "partial", "ambiguous", "wrong_scope", "corrupted_original"])
def test_advice_never_substitutes_display_previews_for_unverifiable_full_readbacks(services, tmp_path, failure):
    reports, acquisitions = report_pair(services, tmp_path)
    report = reports[0]
    report["parameter_evidence"] = {"state": "observed_only", "observations": [{"name": "INS_GYRO_FILTER", "value": "20"}]}
    reference = report["configuration"]["preflight"][0]
    reference["parameters_preview"] = [{"name": "INS_HNTCH_MODE", "value": "999"}]
    if failure == "partial":
        reference["usable_for_full_comparison"] = False
    elif failure == "ambiguous":
        report["configuration"]["preflight"].append(deepcopy(reference))
    elif failure == "wrong_scope":
        report["scope"]["interval_end_s"] += 1
    elif failure == "corrupted_original":
        raw = services[0].raw_path(acquisitions[0]["source_refs"][0]["sha256"])
        raw.chmod(0o600)
        raw.write_bytes(raw.read_bytes() + b"changed")
    result = build_performance_advice(report, None if failure == "missing_store" else services[0])
    assert result["parameter_context"]["basis"] == "logged_at_interval_start"
    assert not result["parameter_context"]["complete_preflight_readback"]
    assert result["parameter_context"]["warnings"]
    all_rows = [row for card in result["recommendations"] for row in card["observed_parameters"]]
    assert {row["name"] for row in all_rows} == {"INS_GYRO_FILTER"}
    assert all(row["value"] == "20" for row in all_rows)


def test_complete_readback_review_honors_cancellation(services, tmp_path):
    reports, _ = report_pair(services, tmp_path)
    with pytest.raises(CancelledError):
        build_performance_advice(reports[0], services[0], cancel_check=lambda: True)


def test_parameter_explanations_preserve_exact_canonical_subnormal_values():
    before, after = str(Decimal.from_float(5e-324)), str(Decimal.from_float(-5e-324))
    explanation = explain_parameter_change("INS_HNTCH_BW", before, after)
    assert explanation["before"] == before and explanation["after"] == after
    assert explanation["direction"] == "decreased" and explanation["proposed_values"] is None
