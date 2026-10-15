"""Existing snapshot/session RPC comparisons carry bounded researched context."""
from pathlib import Path

import pytest

from q22_engine.parameters import ParameterService
from q22_engine.store import Store


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/performance"


@pytest.fixture
def sessions(tmp_path):
    store = Store(tmp_path / "store")
    service = ParameterService(store)
    count = 0

    def create(parameters, *, firmware="Copter-4.7.1", after=False):
        nonlocal count
        count += 1
        path = tmp_path / f"parameters-{count}.param"
        path.write_text("\n".join(f"{name},{value}" for name, value in parameters.items()))
        identity = {"aircraft_id": "synthetic-aircraft", "board": "synthetic-board", "firmware": firmware}
        snapshot = service.dispatch("parameters.import", {
            **identity, "path": str(path), "label": "Synthetic advice test snapshot", "synthetic": True,
            "source_kind": "flight_readback", "observed_at": "2026-10-15T08:00:00Z",
        })["snapshot"]
        session = service.dispatch("sessions.import", {
            **identity, "path": str(FIXTURES / ("synthetic-lower-power.csv" if after else "synthetic-constant.csv")),
            "snapshot_id": snapshot["id"], "label": "Synthetic advice test flight", "synthetic": True,
            "phase": "operator-confirmed hover", "interval_start_s": 0, "interval_end_s": 4, "max_gap_s": 1.5,
            "parameter_evidence": "operator_attested", "parameter_evidence_note": "Synthetic test only",
            "parameter_changes": "none_reported", "conditions": {
                "mass_kg": 10, "payload_kg": 0, "battery_id": "synthetic-pack", "battery_soc_pct": 80,
                "battery_temperature_c": 20, "wind_mps": 0, "hardware_revision": "synthetic-v1", "props": "synthetic-props",
            },
        })["session"]
        return session

    yield service, create
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


def compare(service, before, after):
    return service.dispatch("comparison.run", {"before_session_id": before["id"], "after_session_id": after["id"]})


def test_session_comparison_keeps_exact_settings_and_measurement_separate_from_explanation(sessions):
    service, create = sessions
    before = create({"ATC_RAT_RLL_P": "0.100000", "INS_GYRO_FILTER": "20", "UNCHANGED": "1.00", "REMOVED": "3"})
    after = create({"ATC_RAT_RLL_P": "0.200000", "INS_GYRO_FILTER": "40", "UNCHANGED": "1", "ADDED": "9"}, after=True)
    result = compare(service, before, after)
    diff = result["parameter_diff"]
    assert result["status"] == "descriptive_only"
    assert diff == service.dispatch("parameters.diff", {"before_id": before["snapshot_id"], "after_id": after["snapshot_id"]})
    assert diff["counts"] == {"changed": 2, "added": 1, "removed": 1}
    explanations = {item["parameter"]: item for item in result["parameter_explanations"]}
    assert set(explanations) == {"ATC_RAT_RLL_P", "INS_GYRO_FILTER"}
    gain = explanations["ATC_RAT_RLL_P"]
    assert gain["before"] == "0.100000" and gain["after"] == "0.200000"
    assert gain["direction"] == "increased"
    assert gain["causal_attribution"] is False and gain["proposed_values"] is None
    assert "conditional hypotheses" in gain["interpretation"]
    assert gain["firmware_scope"]["declared"] == "Copter-4.7.1"
    assert gain["references"]
    power = next(item for item in result["metrics"] if item["name"] == "mean_power_w")
    assert power["absolute_change"] == -20
    assert "power" not in gain["expected_effect"]  # No fabricated gain-to-power causal explanation.
    assert not result["parameter_explanations_truncated"]


@pytest.mark.parametrize("changed", [129, 1001])
def test_explanation_cap_preserves_existing_parameter_diff_counts_and_cap(sessions, changed):
    service, create = sessions
    before = create({f"CUSTOM_{index:04d}": "1" for index in range(changed)})
    after = create({f"CUSTOM_{index:04d}": "2" for index in range(changed)}, after=True)
    result = compare(service, before, after)
    assert len(result["parameter_explanations"]) == 128
    assert result["parameter_explanations_limit"] == 128
    assert result["parameter_explanations_truncated"] is True
    assert result["parameter_diff"]["counts"]["changed"] == changed
    assert len(result["parameter_diff"]["changed"]) == min(changed, 1000)
    assert result["parameter_diff"]["truncated"] is (changed > 1000)
    assert [item["parameter"] for item in result["parameter_explanations"]] == [f"CUSTOM_{index:04d}" for index in range(128)]
    assert all(item["status"] == "unmapped_parameter" for item in result["parameter_explanations"])
    assert all(not item["causal_attribution"] for item in result["parameter_explanations"])


def test_firmware_mismatch_remains_confounded_and_explanation_does_not_claim_matching_build(sessions):
    service, create = sessions
    before = create({"INS_HNTCH_MODE": "1"})
    after = create({"INS_HNTCH_MODE": "3"}, firmware="custom-build-2", after=True)
    result = compare(service, before, after)
    assert result["status"] == "confounded"
    assert "firmware" in {item["field"] for item in result["confounders"]}
    explanation = result["parameter_explanations"][0]
    assert explanation["firmware_scope"]["declared"] is None
    assert "unverified" in explanation["firmware_scope"]["scope"]
    assert explanation["causal_attribution"] is False


def test_numerically_unchanged_parameters_do_not_get_change_explanations(sessions):
    service, create = sessions
    before = create({"INS_GYRO_FILTER": "20.0"})
    after = create({"INS_GYRO_FILTER": "2e1"}, after=True)
    result = compare(service, before, after)
    assert result["parameter_explanations"] == []
    assert not result["parameter_explanations_truncated"]
    assert result["parameter_diff"]["counts"]["changed"] == 0
