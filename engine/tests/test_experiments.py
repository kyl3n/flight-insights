"""Repeated evidence stays immutable and does not turn confounding into causality."""
import json

import pytest

from q22_engine.experiments import ExperimentService
from q22_engine.store import Store
from test_alignment import services


def flight(store, index, value, *, parameter="20", verified=True, phase="hover", coverage=.95):
    scope = {"source_sha256": f"{index:064x}", "source_key": "1:1", "boot_epoch": 0, "time_domain": "flight_controller_boot",
             "origin_time_us": "1000000", "interval_start_s": 0, "interval_end_s": 10, "max_gap_s": 1, "battery_id": "0", "synthetic": True}
    report = {"algorithm_version": "test/1", "scope": scope,
        "operating_profile": {"flight_phase": phase, "declared_firmware": "Copter-4.7.1"},
        "metrics": {"mean_power_w": {"value": value, "unit": "W", "sample_count": 100, "coverage_fraction": coverage},
                    "roll_tracking_rmse_deg": {"value": 2, "unit": "deg", "sample_count": 100, "coverage_fraction": coverage}},
        "parameter_evidence": {"state": "observed_only", "observations_eligible_for_review": True,
                               "observations": [{"name": "INS_GYRO_FILTER", "value": parameter, "age_at_start_s": 1}]}}
    if verified:
        report["flight_context"] = {"status": "verified", "flight_record_id": f"flight-{index}", "aircraft_id": "aircraft-a", "hardware_uid": "board-a",
            "environment_snapshot": {"id": f"env-{index}", "status": "captured", "host": {"os": "Linux", "release": "6.6"},
                                     "software": {}, "detected_applications": [], "declared_context": {}, "load": {}},
            "linkage": {"association_id": f"association-{index}", "scope": {key: scope[key] for key in (
                "source_sha256", "source_key", "boot_epoch", "interval_start_s", "interval_end_s")}}}
    return store.add_document("insight_report", report)


def plan(references=(), candidates=(), **changes):
    return {"name": "Repeated comparable hover", "hypothesis": "Lower measured load at matching hover after a reviewed configuration change.",
        "parameter_hypothesis": [{"name": "INS_GYRO_FILTER", "before": "20", "after": "30"}],
        "reference_report_ids": [report["id"] for report in references], "candidate_report_ids": [report["id"] for report in candidates],
        "flight_phase": "hover", "minimum_coverage": .8, "minimum_flights_per_group": 2,
        "goals": [{"metric": "mean_power_w", "unit": "W", "direction": "lower", "minimum_change": 10}],
        "regression_limits": [{"metric": "roll_tracking_rmse_deg", "unit": "deg", "direction": "lower", "maximum_regression": .5}], **changes}


def setup(tmp_path, candidates=(80, 85), **flight_options):
    store = Store(tmp_path / "catalog")
    service = ExperimentService(store)
    refs = [flight(store, index, value) for index, value in enumerate((100, 105), 1)]
    after = [flight(store, index, value, parameter="30", **flight_options) for index, value in enumerate(candidates, 3)]
    return store, service, refs, after


def evaluate(service, payload):
    experiment = service.dispatch("experiments.create", payload)["experiment"]
    result = service.dispatch("experiments.evaluate", {"experiment_id": experiment["id"]})["evaluation"]
    return experiment, result


def test_repeat_goals_and_regression_limits_support_observed_improvement_not_causality(tmp_path):
    store, service, refs, after = setup(tmp_path)
    experiment, result = evaluate(service, plan(refs, after))
    assert result["status"] == "observed_improvement"
    assert result["groups"]["reference"]["distinct_verified_flight_count"] == 2
    assert result["groups"]["candidate"]["distinct_verified_flight_count"] == 2
    assert result["groups"]["candidate"]["independent_flight_count"] is None
    assert result["metric_summaries"][0]["delta"] == -20
    assert result["criteria"][0]["status"] == "passed"
    assert result["counterexamples"] == []
    assert not result["causal_attribution"]
    assert result["synthetic"]
    assert any(row["code"] == "incomplete_effective_configuration" for row in result["confounders"])
    assert all(row["effective_configuration"] == "unknown" for row in result["parameter_evidence"])
    assert store.get_document("experiment", experiment["id"]) == experiment


def test_counterexample_is_retained_even_when_group_median_meets_goal(tmp_path):
    _store, service, refs, after = setup(tmp_path, candidates=(60, 65, 120))
    _experiment, result = evaluate(service, plan(refs, after))
    assert result["status"] == "mixed_results"
    assert result["criteria"][0]["status"] == "passed"
    assert result["counterexamples"][0]["report_id"] == after[2]["id"]
    assert result["counterexamples"][0]["candidate_value"] == 120
    assert result["metric_summaries"][0]["candidate"]["maximum"] == 120
    assert result["groups"]["candidate"]["report_count"] == 3


def test_zero_threshold_does_not_label_unchanged_measurements_an_improvement(tmp_path):
    _store, service, refs, after = setup(tmp_path, candidates=(100, 105))
    _experiment, result = evaluate(service, plan(refs, after,
        goals=[{"metric": "mean_power_w", "unit": "W", "direction": "lower", "minimum_change": 0}]))
    assert result["status"] == "mixed_results"
    assert result["criteria"][0]["status"] == "failed"
    assert result["criteria"][0]["observed_change"] == 0


def test_regression_is_not_hidden_by_improved_primary_metric(tmp_path):
    store, service, refs, after = setup(tmp_path)
    after[1]["metrics"]["roll_tracking_rmse_deg"]["value"] = 5
    after[1] = store.add_document("insight_report", after[1])
    _experiment, result = evaluate(service, plan(refs, after))
    assert result["status"] == "mixed_results"
    assert any(row["kind"] == "regression_limit" and row["status"] == "failed" for row in result["criteria"])
    assert any(row["metric"] == "roll_tracking_rmse_deg" for row in result["counterexamples"])


@pytest.mark.parametrize("options,code", [({"verified": False}, "flight_identity_unknown"),
    ({"phase": "cruise"}, "phase_mismatch"), ({"coverage": .4}, "metric_evidence_insufficient")])
def test_missing_identity_phase_or_coverage_blocks_repeat_claim(tmp_path, options, code):
    _store, service, refs, after = setup(tmp_path, **options)
    _experiment, result = evaluate(service, plan(refs, after))
    assert result["status"] == "insufficient_evidence"
    assert code in result["decision_blockers"]
    assert all(row["status"] == "insufficient_evidence" for row in result["criteria"])


def test_same_flight_multiple_reports_are_not_counted_as_repeats(tmp_path):
    store, service, refs, after = setup(tmp_path)
    after[1]["flight_context"]["flight_record_id"] = after[0]["flight_context"]["flight_record_id"]
    after[1] = store.add_document("insight_report", after[1])
    _experiment, result = evaluate(service, plan(refs, after))
    assert result["status"] == "insufficient_evidence"
    assert result["groups"]["candidate"]["report_count"] == 2
    assert result["groups"]["candidate"]["distinct_verified_flight_count"] == 1
    assert "repeated_intervals_one_flight" in result["decision_blockers"]


def test_wrong_parameter_and_mixed_interval_preserve_failed_expectations(tmp_path):
    store, service, refs, after = setup(tmp_path)
    after[0]["parameter_evidence"]["observations"][0]["value"] = "25"
    after[1]["parameter_evidence"]["state"] = "mixed"
    after = [store.add_document("insight_report", report) for report in after]
    _experiment, result = evaluate(service, plan(refs, after))
    assert result["status"] == "insufficient_evidence"
    assert "planned_parameters_unverified" in result["decision_blockers"]
    observed = [row for row in result["parameter_evidence"] if row["group"] == "candidate"]
    assert observed[0]["checks"][0]["observed"] == "25"
    assert observed[0]["checks"][0]["expected"] == "30"
    assert observed[1]["checks"][0]["status"] == "unverified_or_mismatched"


def test_explicit_pairing_uses_matched_changes_and_retains_every_pair(tmp_path):
    _store, service, refs, after = setup(tmp_path)
    pairs = [{"reference_report_id": reference["id"], "candidate_report_id": candidate["id"]}
             for reference, candidate in zip(refs, after)]
    _experiment, result = evaluate(service, plan(refs, after, pairs=pairs))
    assert result["analysis_mode"] == "explicit_paired"
    assert [pair["delta"] for pair in result["metric_summaries"][0]["pair_deltas"]] == [-20, -20]
    assert result["status"] == "observed_improvement"
    with pytest.raises(ValueError, match="cover every selected report"):
        service.dispatch("experiments.create", plan(refs, after, pairs=pairs[:1]))


def test_plans_evaluations_and_revisions_are_immutable_and_empty_plan_is_allowed(tmp_path):
    store, service, refs, after = setup(tmp_path)
    empty, initial = evaluate(service, plan())
    assert initial["status"] == "insufficient_evidence"
    updated, outcome = evaluate(service, plan(refs, after, parent_experiment_id=empty["id"]))
    assert updated["revision"] == 2 and updated["parent_experiment_id"] == empty["id"]
    repeated = service.dispatch("experiments.evaluate", {"experiment_id": updated["id"]})["evaluation"]
    assert outcome["id"] != repeated["id"]
    detail = service.dispatch("experiments.detail", {"experiment_id": updated["id"]})
    assert {row["id"] for row in detail["evaluations"]} == {outcome["id"], repeated["id"]}
    assert store.get_document("experiment", empty["id"]) == empty
    assert store.get_document("experiment_evaluation", initial["id"]) == initial
    page = service.dispatch("experiments.list", {"limit": 1})
    assert len(page["experiments"]) == 1 and page["next_offset"] == 1
    assert len(service.dispatch("experiments.list", {"offset": 1})["experiments"]) == 1
    json.dumps(detail, allow_nan=False)


@pytest.mark.parametrize("patch", [{"minimum_flights_per_group": 1}, {"minimum_flights_per_group": True},
    {"minimum_coverage": float("nan")}, {"flight_phase": "unknown"}, {"name": ""},
    {"goals": []}, {"goals": [{"metric": "arbitrary", "unit": "W", "direction": "lower", "minimum_change": 1}]},
    {"goals": [{"metric": "mean_power_w", "unit": "kW", "direction": "lower", "minimum_change": 1}]},
    {"parameter_hypothesis": [{"name": "BAD", "before": "NaN", "after": "2"}]}])
def test_invalid_plans_do_not_create_documents(tmp_path, patch):
    store = Store(tmp_path / "catalog")
    service = ExperimentService(store)
    with pytest.raises(ValueError):
        service.dispatch("experiments.create", plan(**patch))
    assert store.list_documents("experiment") == []


def test_units_and_algorithm_changes_do_not_mix_numeric_values(tmp_path):
    store, service, refs, after = setup(tmp_path)
    after[0]["metrics"]["mean_power_w"]["unit"] = "kW"
    after[1]["algorithm_version"] = "other/2"
    after = [store.add_document("insight_report", report) for report in after]
    _experiment, result = evaluate(service, plan(refs, after))
    assert result["status"] == "insufficient_evidence"
    metric = result["metric_summaries"][0]
    assert metric["candidate"]["count"] == 0
    assert metric["delta"] is None
    assert any(row["unit"] == "kW" and not row["eligible"] for row in metric["observations"])


def test_complete_readback_supports_planned_values_beyond_logged_subset(services, tmp_path):
    from test_flight_configuration import report_pair
    reports, _ = report_pair(services, tmp_path, [("INS_GYRO_FILTER", "20", 9)], [("INS_GYRO_FILTER", "30", 9)])
    saved = [services[0].add_document("insight_report", report) for report in reports]
    service = ExperimentService(services[0])
    _experiment, result = evaluate(service, plan(saved[:1], saved[1:]))
    assert result["status"] == "insufficient_evidence"  # One flight per group is not a repeat experiment.
    assert "planned_parameters_unverified" not in result["decision_blockers"]
    assert all(row["basis"] == "complete_typed_preflight_readback" and row["snapshot_id"] for row in result["parameter_evidence"])
    assert [row["checks"][0]["observed"] for row in result["parameter_evidence"]] == ["20", "30"]
    assert all(row["effective_configuration"] == "unknown" for row in result["parameter_evidence"])


def test_expected_readback_values_use_the_recorded_mavlink_type(services, tmp_path):
    from test_flight_configuration import report_pair
    reports, _ = report_pair(services, tmp_path, [("ATC_RAT_RLL_P", "0.1", 9)], [("ATC_RAT_RLL_P", "0.2", 9)])
    saved = [services[0].add_document("insight_report", report) for report in reports]
    service = ExperimentService(services[0])
    _experiment, result = evaluate(service, plan(saved[:1], saved[1:],
        parameter_hypothesis=[{"name": "ATC_RAT_RLL_P", "before": "0.1", "after": "0.2"}]))
    checks = [row["checks"][0] for row in result["parameter_evidence"]]
    assert all(check["status"] == "matched_observation" for check in checks)
    assert checks[0]["expected"] == "0.1"
    assert checks[0]["expected_canonical"] == checks[0]["observed"] == "0.100000001490116119384765625"


def test_cancelled_evaluation_does_not_publish_partial_outcome(tmp_path):
    from concurrent.futures import CancelledError
    store, service, refs, after = setup(tmp_path)
    experiment = service.dispatch("experiments.create", plan(refs, after))["experiment"]
    with pytest.raises(CancelledError):
        service.dispatch("experiments.evaluate", {"experiment_id": experiment["id"]}, cancel_check=lambda: True)
    assert store.list_documents("experiment_evaluation") == []
