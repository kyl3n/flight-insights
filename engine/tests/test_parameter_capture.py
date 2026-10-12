"""Revision-2 passive evidence semantics, without claiming a readback transport."""
import json
import struct

import pytest

from q22_engine.parameter_capture import ParameterAccumulator


def accumulator(**overrides):
    args = {"aircraft_id": "synthetic-aircraft", "boot_epoch": 0,
            "component_scope": {"system_id": 1, "component_id": 1},
            "firmware_identity": {"build": "synthetic-build"}, **overrides}
    return ParameterAccumulator(**args)


def event(name="TEST_P", value="0.1", index=0, count=1, **extra):
    return {"name": name, "value": value, "index": index, "count": count,
            "param_type": 9, "component_id": 1, "system_id": 1,
            "received_monotonic_ns": 10_000_000_001,
            "received_utc_us": 1_760_000_000_000_001,
            "raw_ref": {"sha256": "a" * 64, "record_index": index}, **extra}


def test_complete_passive_scope_is_separate_from_applicability_freshness_atomicity():
    capture = accumulator()
    capture.observe(event("A", 1, 0, 2))
    capture.observe(event("B", 2, 1, 2))
    summary = capture.summary()
    assert summary["completeness"] == "complete_for_scope"
    assert summary["consistency"] == "no_observed_conflict"
    assert summary["expected_count"] == summary["decoded_count"] == 2
    assert summary["freshness"] == summary["flight_time_applicability"] == "unknown"
    assert summary["atomic"] is False and summary["full_readback_requested"] is False
    assert summary["acquisition_basis"] == "passive_enumeration_observed"
    assert summary["preflight_full_request"] == "unavailable"
    assert "values" not in summary
    final = capture.finalize(role="preflight", snapshot_id="saved-snapshot")
    assert final["flight_time_applicability"] == "provisional"
    assert final["freshness"] == "unknown" and final["atomic"] is False
    assert capture.snapshot()["id"] == "saved-snapshot"
    assert final["snapshot_id"] == "saved-snapshot"


def test_identical_retransmissions_do_not_change_values_hash_or_completeness():
    capture = accumulator()
    assert capture.observe(event())["duplicate"] is False
    original = capture.summary()
    result = capture.observe(event(received_monotonic_ns=20_000_000_009))
    assert result["duplicate"] is True
    summary = capture.summary()
    assert summary["typed_hash"] == original["typed_hash"]
    assert summary["scope_hash"] == original["scope_hash"]
    assert summary["decoded_count"] == 1
    assert summary["duplicate_retransmission_count"] == 1
    value = capture.snapshot()["values"]["TEST_P"]
    assert value["observed_at"]["received_monotonic_ns"] == "10000000001"
    assert value["latest_observation"]["observed_at"]["received_monotonic_ns"] == "20000000009"
    assert value["effective_time_bounds"]["basis"] == "unknown"


@pytest.mark.parametrize("second,reason", [
    (event(value="0.2"), "conflicting_parameter_value_or_type"),
    (event(name="OTHER"), "conflicting_index_to_name"),
    (event(index=1, count=2), "conflicting_name_to_index"),
    (event(count=2), "changing_parameter_count"),
])
def test_conflicts_remain_partial_and_never_silently_overwrite(second, reason):
    capture = accumulator()
    capture.observe(event())
    first_value = capture.snapshot()["values"]["TEST_P"]["canonical_value"]
    capture.observe(second)
    summary = capture.summary()
    assert summary["completeness"] == "partial"
    assert summary["consistency"] == "conflict"
    assert reason in summary["issues"]
    assert capture.snapshot()["values"]["TEST_P"]["canonical_value"] == first_value


def test_missing_indexes_can_be_filled_by_valid_retry():
    capture = accumulator()
    capture.observe(event("A", 1, 0, 3))
    capture.observe(event("C", 3, 2, 3))
    assert capture.summary()["missing_index_examples"] == [1]
    assert capture.summary()["completeness"] == "partial"
    capture.observe(event("B", 2, 1, 3))
    assert capture.summary()["completeness"] == "complete_for_scope"


def test_index_outside_count_and_unenumerated_observations_are_not_complete():
    capture = accumulator()
    capture.observe(event(index=2, count=2))
    assert capture.summary()["completeness"] == "partial"
    assert "index_outside_reported_count" in capture.summary()["issues"]
    capture = accumulator()
    capture.observe(event(index=None, count=None))
    assert capture.summary()["completeness"] == "partial"
    assert capture.summary()["expected_count"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "Infinity", "1e100"])
def test_nonfinite_or_overflow_values_are_retained_but_ineligible(value):
    capture = accumulator()
    result = capture.observe(event(value=value))
    assert result["accepted"] is False
    assert capture.summary()["decoded_count"] == 0
    assert capture.summary()["completeness"] == "partial"
    snapshot = capture.snapshot()
    assert snapshot["values"]["TEST_P"]["canonical_value"] is None
    json.dumps(snapshot, allow_nan=False)


def test_unknown_type_or_integer_encoding_cannot_complete():
    for overrides in ({"param_type": None}, {"param_type": "mystery"}, {"param_type": 6},
                      {"param_type": 6, "encoding_provenance": {"validated": False, "encoding": "mavlink_c_cast"}}):
        capture = accumulator()
        assert capture.observe(event(value=1, **overrides))["accepted"] is False
        assert capture.summary()["completeness"] == "partial"
        assert capture.summary()["consistency"] == "unknown"


def test_unresolved_unknown_observation_is_not_invented_into_positive_conflict():
    capture = accumulator()
    capture.observe(event(value=1, param_type=None))
    capture.observe(event(value=1, param_type=6, encoding_provenance="typed_numeric"))
    assert capture.summary()["completeness"] == "partial"
    assert capture.summary()["consistency"] == "unknown"
    assert "conflicting_parameter_value_or_type" not in capture.summary()["issues"]


@pytest.mark.parametrize("type_code,value,accepted", [
    (1, 255, True), (1, 256, False), (1, -1, False), (2, -128, True),
    (2, -129, False), (6, "1.5", False), (7, str(2**64 - 1), True),
    (7, str(2**64), False), (8, str(-2**63), True), (8, float(2**63), False),
])
def test_strict_integer_ranges_and_lossless_representation(type_code, value, accepted):
    capture = accumulator()
    result = capture.observe(event(value=value, param_type=type_code, encoding_provenance="typed_numeric"))
    assert result["accepted"] is accepted
    if accepted:
        assert capture.snapshot()["values"]["TEST_P"]["canonical_value"] == str(int(value))


def test_uint64_neighbor_values_hash_differ_without_binary64_rounding():
    captures = [accumulator(), accumulator()]
    for capture, value in zip(captures, (2**64 - 2, 2**64 - 1)):
        capture.observe(event(value=str(value), param_type=7, encoding_provenance="typed_numeric"))
    assert captures[0].summary()["typed_hash"] != captures[1].summary()["typed_hash"]


def test_real32_hash_uses_wire_bits_not_rounded_ui_text():
    a, b = accumulator(), accumulator()
    low = struct.unpack(">f", struct.pack(">I", 0x3DCCCCCD))[0]
    high = struct.unpack(">f", struct.pack(">I", 0x3DCCCCCE))[0]
    assert f"{low:.3f}" == f"{high:.3f}"
    a.observe(event(value=low))
    b.observe(event(value=high))
    assert a.summary()["typed_hash"] != b.summary()["typed_hash"]
    c = accumulator()
    c.observe(event(value="0.1"))
    assert c.summary()["typed_hash"] == a.summary()["typed_hash"]
    assert c.snapshot()["values"]["TEST_P"]["canonical_value"] == "0.100000001490116119384765625"


def test_negative_zero_and_positive_zero_keep_distinct_wire_tokens():
    a, b = accumulator(), accumulator()
    a.observe(event(value=-0.0))
    b.observe(event(value=0.0))
    assert a.summary()["typed_hash"] != b.summary()["typed_hash"]


def test_hashes_are_order_independent_but_scope_and_type_sensitive():
    a, b = accumulator(), accumulator()
    ordered = [event("A", 1, 0, 2), event("B", 2, 1, 2)]
    for observation in ordered:
        a.observe(observation)
    for observation in reversed(ordered):
        b.observe(observation)
    assert a.summary()["typed_hash"] == b.summary()["typed_hash"]
    assert a.summary()["scope_hash"] == b.summary()["scope_hash"]
    partial = accumulator()
    partial.observe(event("A", 1, 0, 2))
    complete = accumulator()
    complete.observe(event("A", 1, 0, 1))
    assert partial.summary()["typed_hash"] == complete.summary()["typed_hash"]
    assert partial.summary()["scope_hash"] != complete.summary()["scope_hash"]
    integer = accumulator()
    integer.observe(event("A", 1, 0, 1, param_type=6, encoding_provenance="typed_numeric"))
    assert integer.summary()["typed_hash"] != complete.summary()["typed_hash"]


@pytest.mark.parametrize("change", [{"component_id": 2}, {"system_id": 2}, {"aircraft_id": "other"}, {"boot_epoch": 1}])
def test_wrong_component_aircraft_or_epoch_is_rejected_before_mutation(change):
    capture = accumulator()
    with pytest.raises(ValueError):
        capture.observe(event(**change))
    assert capture.summary()["observed_name_count"] == 0


def test_decoder_field_aliases_and_explicit_type_provenance():
    capture = accumulator()
    capture.observe({"name": "A", "value": 5, "parameter_index": 0, "parameter_count": 1,
                     "mav_param_type": 6, "value_encoding": "ArduPilot C-cast, heartbeat-identified"})
    assert capture.summary()["completeness"] == "complete_for_scope"
    assert capture.snapshot()["values"]["A"]["type_provenance"] == "declared_in_observation"


def test_json_time_precision_clock_regression_and_immutable_output():
    capture = accumulator()
    capture.observe(event(received_monotonic_ns="18446744073709551614"))
    first_snapshot = capture.snapshot()
    capture.observe(event(received_monotonic_ns="18446744073709551613"))
    assert capture.summary()["monotonic_clock_regression_count"] == 1
    assert capture.summary()["freshness"] == "unknown"
    assert capture.summary()["start_end_clock_bounds"]["end_monotonic_ns"] == "18446744073709551614"
    first_snapshot["values"]["TEST_P"]["canonical_value"] = "tampered"
    assert capture.snapshot()["values"]["TEST_P"]["canonical_value"] != "tampered"
    first_snapshot["component_scope"]["component_id"] = 99
    assert capture.summary()["component_scope"]["component_id"] == 1


def test_returned_observations_support_durable_full_lineage_without_guessing_activation():
    capture = accumulator()
    original = capture.observe(event())["observation"]
    changed = capture.observe(event(value="0.2", received_monotonic_ns=20_000_000_000))["observation"]
    assert original["id"] != changed["id"]
    assert changed["id"] in capture.summary()["observed_change_ids"]
    assert changed["effective_time_bounds"] == {"earliest": None, "latest": None, "basis": "unknown"}
    changed["canonical_value"] = "tampered"
    assert capture.snapshot()["values"]["TEST_P"]["latest_observation"]["canonical_value"] != "tampered"


def test_finalized_generation_cannot_change_role_snapshot_or_observations():
    capture = accumulator()
    capture.observe(event())
    first = capture.finalize(role="postflight")
    assert capture.finalize(role="postflight") == first
    assert capture.snapshot()["id"] == first["snapshot_id"]
    assert first["flight_time_applicability"] == "unknown"
    with pytest.raises(ValueError):
        capture.observe(event())
    with pytest.raises(ValueError):
        capture.finalize(role="preflight")
    with pytest.raises(ValueError):
        capture.finalize(role="postflight", snapshot_id="replacement")


def test_summary_samples_are_bounded_and_missing_scope_remains_explicit():
    capture = accumulator()
    for index in range(80):
        capture.observe(event(name=f"P{index}", value=1, index=index, count=50000))
    summary = capture.summary()
    assert summary["missing_index_count"] == 49920
    assert len(summary["missing_index_examples"]) == 32
    assert len(summary["raw_refs"]) == 32
    assert "values" not in summary
    assert summary["completeness"] == "partial"
    with pytest.raises(ValueError):
        capture.observe(event(count=50001))
    with pytest.raises(ValueError):
        capture.observe(event(raw_ref={"too_large": "x" * 2049}))


def test_name_cap_is_enforced_before_growing_reducer(monkeypatch):
    import q22_engine.parameter_capture as module
    monkeypatch.setattr(module, "MAX_PARAMETERS", 2)
    capture = accumulator()
    capture.observe(event("A", 1, 0, 2))
    capture.observe(event("B", 2, 1, 2))
    with pytest.raises(ValueError, match="name limit"):
        capture.observe(event("C", 3, 0, 2))
    assert capture.summary()["observed_name_count"] == 2


def test_empty_scope_unknown_and_no_hidden_all_components_claim():
    capture = accumulator(component_scope=1)
    summary = capture.summary()
    assert summary["completeness"] == summary["consistency"] == "unknown"
    assert summary["component_scope"] == {"component_id": 1}
    for invalid in ({"component_id": [1, 2]}, [{"component_id": 1}], {"component_id": 0}):
        with pytest.raises(ValueError):
            accumulator(component_scope=invalid)
