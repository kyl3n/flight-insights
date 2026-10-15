"""Full readback/catalog bridges using synthetic protocol evidence only."""
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from q22_engine.flight_configuration import FlightConfigurationService, compare_flight_configurations, configuration_parameters, report_configuration
from q22_engine.acquisition import AcquisitionService
from q22_engine.insight_context import attach_flight_context
from q22_engine.parameter_capture import ParameterAccumulator
from q22_engine.parameters import ParameterService
from test_alignment import UTC, analyzed, imported, managed, services
from test_collection import envelope
from test_insight_context import report_scope
from test_acquisition import Peer, durable_limits, params as acquisition_params


FIRMWARE = {"flight_sw_version": 0x040600ff, "board_version": 9, "vendor_id": 1, "product_id": 2}


def readback(services, tmp_path, values=None, *, start=2.4, end=3.4, partial=False, conflict=False, uid="123", requested=True):
    values = values or [("ATC_RAT_RLL_P", "0.1", 9), ("TEST_UINT64", "18446744073709551615", 7)]
    accumulator = ParameterAccumulator("synthetic-aircraft", "single_observed_acquisition_clock_segment",
        {"system_id": 1, "component_id": 1}, FIRMWARE)
    for index, (name, value, kind) in enumerate(values):
        stamp = start + (end - start) * index / max(1, len(values) - 1)
        accumulator.observe({"name": name, "value": value, "type": kind, "encoding_provenance": "typed_numeric", "index": index,
            "count": len(values) + int(partial), "received_monotonic_ns": str(int((1000 + stamp) * 1e9)),
            "received_utc_us": str(UTC + int(stamp * 1e6)), "raw_ref": {"record_index": index}})
    if conflict:
        name, value, kind = values[0]
        for value in ("0.2", value):
            accumulator.observe({"name": name, "value": value, "type": kind, "encoding_provenance": "typed_numeric", "index": 0,
                "count": len(values), "received_monotonic_ns": str(int((1000 + end) * 1e9)),
                "received_utc_us": str(UTC + int(end * 1e6)), "raw_ref": {"record_index": 50}})
    capture = accumulator.finalize(role="unknown")
    capture.update(full_readback_requested=requested, acquisition_basis="explicit_requested_enumeration" if requested else "passive_enumeration_observed")
    rich = accumulator.snapshot()
    rich.update(acquisition_basis=capture["acquisition_basis"], flight_time_applicability="unknown", atomic=False)
    path = tmp_path / f"readback-{uuid4()}.json"
    path.write_text(json.dumps(rich))
    source = services[0].archive_file(path)
    transcript = tmp_path / f"transcript-{uuid4()}.mavudp"
    transcript.write_bytes(b"Synthetic acquisition transcript oracle")
    trace = services[0].archive_file(transcript)

    def gate(stamp):
        receipt = {"received_monotonic_ns": str(int((1000 + stamp) * 1e9)), "received_utc_us": str(UTC + int(stamp * 1e6))}
        return {"heartbeat": {"armed": False, **receipt}, "identity": {"uid": uid, "firmware_identity": FIRMWARE, **receipt},
                "boot_clock": {"time_boot_ms": int((100 + stamp) * 1000), **receipt}}

    return services[0].add_document("acquisition", {"kind": "parameters", "state": "partial" if partial or conflict else "complete",
        "aircraft_id": "synthetic-aircraft", "synthetic": False, "connection": {"system_id": 1, "component_id": 1, "expected_uid": "123"},
        "parameter_capture": capture, "transport_evidence": {"initial_gate_observations": gate(start - .4),
            "final_gate_observations": gate(end + .4), "trace_sha256": trace["sha256"]},
        "source_refs": [source | {"role": "parameter_readback_snapshot"}, trace | {"role": "mavlink_acquisition_transcript"}]})


def report_pair(services, tmp_path, before=None, after=None):
    reference = imported(services, tmp_path, "reference.tlog", arms=((8, 20), (32, 42)), duration=52)
    selected = imported(services, tmp_path, "candidate.tlog", arms=((8, 20), (32, 42)), duration=52, tag=4)
    first = readback(services, tmp_path, before)
    second = readback(services, tmp_path, after, start=25.4, end=26.4)
    readback(services, tmp_path, after, start=46.4, end=47.4)
    profile, _, records = managed(services, reference)
    reports = []
    for record, bounds in zip(sorted(records, key=lambda r: r["display_number"]), [(10, 15), (34, 39)]):
        alignment = analyzed(services, reference, selected, flight_record_id=record["id"])
        services[3].dispatch("alignment.attach", {"report_id": alignment["id"], "flight_record_id": record["id"], **envelope(services[2], profile)})
        report = report_scope(services, selected, *bounds)
        report["flight_context"] = attach_flight_context(services[0], report, record["id"])
        assert report["flight_context"]["status"] == "verified"
        report["configuration"] = report_configuration(services[0], report)
        reports.append(report)
    return reports, (first, second)


def test_promotes_exact_types_into_normal_catalog_without_export_reimport(services, tmp_path):
    acquisition = readback(services, tmp_path)
    catalog = ParameterService(services[0])
    result = catalog.dispatch("parameters.promote_readback", {"acquisition_id": acquisition["id"]})
    summary = result["snapshot"]
    assert summary["usable_for_full_comparison"] and summary["parameter_count"] == 2
    assert summary["source_kind"] == "current_readback" and summary["hardware_uid"] == "123"
    assert summary["effective_configuration"] == summary["flight_time_applicability"] == "unknown"
    assert summary["source_refs"] == acquisition["source_refs"]
    snapshot = services[0].get_document("snapshot", summary["id"])
    assert snapshot["parameters"]["TEST_UINT64"]["value"] == "18446744073709551615"
    assert snapshot["parameters"]["TEST_UINT64"]["canonical_token"] == "integer:18446744073709551615"
    assert snapshot["parameters"]["ATC_RAT_RLL_P"]["value"] == "0.100000001490116119384765625"
    assert catalog.dispatch("parameters.list", {})["snapshots"][0]["id"] == summary["id"]
    assert "parameters" not in summary
    again = catalog.dispatch("parameters.promote_readback", {"acquisition_id": acquisition["id"]})
    assert again["already_preserved"] and again["snapshot"]["id"] == summary["id"]


def test_actual_synthetic_udp_acquisition_promotes_without_format_translation(services):
    acquisition = AcquisitionService(services[0])
    peer = Peer()
    try:
        result = acquisition.dispatch("acquisition.parameters.capture", acquisition_params(acquisition, peer, limits=durable_limits()))["acquisition"]
        assert result["state"] == "complete", result
        promoted = FlightConfigurationService(services[0]).promote({"acquisition_id": result["id"]})["snapshot"]
        assert promoted["usable_for_full_comparison"], promoted["unusable_reasons"]
        assert promoted["synthetic"] and promoted["parameter_count"] == 2
        values = services[0].get_document("snapshot", promoted["id"])["parameters"]
        assert values["TEST_A"]["value"] == "1.5" and values["TEST_B"]["value"] == "7"
        assert all(message.get_type() in {"HEARTBEAT", "COMMAND_LONG", "PARAM_REQUEST_LIST", "PARAM_REQUEST_READ"} for message in peer.requests)
    finally:
        peer.close()


def test_concurrent_promotion_is_idempotent(services, tmp_path):
    acquisition = readback(services, tmp_path)
    service = FlightConfigurationService(services[0])
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.promote({"acquisition_id": acquisition["id"]}), range(2)))
    assert len({r["snapshot"]["id"] for r in results}) == 1
    assert sorted(r["already_preserved"] for r in results) == [False, True]


@pytest.mark.parametrize("options", [{"partial": True}, {"conflict": True}, {"requested": False}, {"uid": "999"}])
def test_partial_conflicting_unrequested_or_wrong_identity_never_becomes_full(services, tmp_path, options):
    acquisition = readback(services, tmp_path, **options)
    summary = FlightConfigurationService(services[0]).promote({"acquisition_id": acquisition["id"]})["snapshot"]
    assert not summary["usable_for_full_comparison"] and summary["unusable_reasons"]
    assert summary["completeness"] == acquisition["parameter_capture"]["completeness"]
    if options.get("conflict"):
        assert summary["readback_evidence"]["observed_change_ids"]
        assert summary["consistency"] == "conflict"  # A -> B -> A stays conflicting.


def test_interruption_downgrade_preserves_original_reducer_hash_without_upgrading(services, tmp_path):
    acquisition = readback(services, tmp_path)
    original = acquisition["source_refs"][0]
    rich = json.loads(services[0].raw_path(original["sha256"]).read_text())
    # Production acquisition finalization may downgrade labels after reducer
    # hashes were computed. This authentic partial form must remain preservable.
    rich.update(completeness="partial", consistency="unknown", interruption_reason="Armed during final drain")
    acquisition["parameter_capture"].update(completeness="partial", consistency="unknown", interruption_reason="Armed during final drain")
    acquisition["state"] = "interrupted"
    path = tmp_path / "interrupted.snapshot.json"
    path.write_text(json.dumps(rich))
    acquisition["source_refs"][0] = services[0].archive_file(path) | {"role": "parameter_readback_snapshot"}
    interrupted = services[0].add_document("acquisition", acquisition)
    snapshot = FlightConfigurationService(services[0]).promote({"acquisition_id": interrupted["id"]})["snapshot"]
    assert snapshot["parameter_count"] == snapshot["readback_evidence"]["expected_count"] == 2
    assert snapshot["completeness"] == "partial" and not snapshot["usable_for_full_comparison"]
    assert snapshot["scope_hash"] == rich["scope_hash"]


def test_numeric_equal_type_changes_and_signed_zero_remain_visible(services, tmp_path):
    service = FlightConfigurationService(services[0])
    first = readback(services, tmp_path, [("TEST_TYPE", "1", 6), ("TEST_ZERO", "0", 9)])
    second = readback(services, tmp_path, [("TEST_TYPE", "1", 9), ("TEST_ZERO", "-0", 9)])
    before, after = [service.promote({"acquisition_id": item["id"]})["snapshot"] for item in (first, second)]
    result = ParameterService(services[0]).dispatch("parameters.diff", {"before_id": before["id"], "after_id": after["id"]})
    assert result["counts"]["changed"] == 2
    assert {item["name"] for item in result["changed"]} == {"TEST_TYPE", "TEST_ZERO"}
    assert all("before_token" in item for item in result["changed"])


def test_unarchived_or_tampered_original_cannot_be_promoted(services, tmp_path):
    acquisition = readback(services, tmp_path)
    raw = services[0].raw_path(acquisition["source_refs"][0]["sha256"])
    raw.chmod(0o600)
    raw.write_bytes(raw.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        FlightConfigurationService(services[0]).promote({"acquisition_id": acquisition["id"]})
    missing = services[0].add_document("acquisition", {**acquisition, "source_refs": []})
    with pytest.raises(ValueError, match="archived"):
        FlightConfigurationService(services[0]).promote({"acquisition_id": missing["id"]})


@pytest.mark.parametrize("change", ["value", "token", "scope", "count", "index"])
def test_forged_rich_schema_or_completeness_is_rejected_or_unusable(services, tmp_path, change):
    acquisition = readback(services, tmp_path)
    source = acquisition["source_refs"][0]
    rich = json.loads(services[0].raw_path(source["sha256"]).read_text())
    row = rich["values"]["ATC_RAT_RLL_P"]
    if change == "value":
        row["canonical_value"] = "500"
    elif change == "token":
        row["canonical_token"] = "ieee754:ffffffff"
    elif change == "scope":
        row["component"] = {"system_id": 2, "component_id": 1}
    elif change == "count":
        acquisition["parameter_capture"]["decoded_count"] += 1
    else:
        row["parameter_index"] = 1
    path = tmp_path / "modified-rich.json"
    path.write_text(json.dumps(rich))
    replaced = services[0].archive_file(path)
    acquisition["source_refs"][0] = replaced | {"role": "parameter_readback_snapshot"}
    modified = services[0].add_document("acquisition", acquisition)
    with pytest.raises(ValueError):
        FlightConfigurationService(services[0]).promote({"acquisition_id": modified["id"]})


def test_report_links_only_exact_verified_flight_and_keeps_full_sets_out_of_payload(services, tmp_path):
    values = [(f"TEST_{index:04d}", str(index), 6) for index in range(150)]
    changed = values[:-1] + [("TEST_0149", "999", 6)]
    reports, acquisitions = report_pair(services, tmp_path, values, changed)
    for report, acquisition in zip(reports, acquisitions):
        configuration = report["configuration"]
        assert configuration["status"] == "flight_bound_readbacks", configuration
        assert len(configuration["preflight"]) == 1
        pre = configuration["preflight"][0]
        assert pre["acquisition_id"] == acquisition["id"] and pre["parameter_count"] == 150
        assert len(pre["parameters_preview"]) == 64 and pre["preview_truncated"]
        assert "parameters" not in pre and len(json.dumps(configuration)) < 100_000
        assert len(configuration_parameters(services[0], report)[0]) == 150
        assert len(configuration["postflight"]) == 1
    difference = compare_flight_configurations(services[0], *reports)
    assert difference["status"] == "compared" and difference["eligible_for_parameter_review"]
    assert difference["counts"]["compared"] == 150 and difference["all_parameters_compared"]
    assert difference["changed"][0]["name"] == "TEST_0149"
    assert difference["effective_configuration"] == "unknown" and not difference["causal_attribution"]


def test_imported_unlinked_log_does_not_select_a_current_readback(services, tmp_path):
    readback(services, tmp_path)
    assert report_configuration(services[0], {"flight_context": {"status": "unavailable"}})["status"] == "unavailable"
    assert not services[0].list_documents("snapshot")


def test_configuration_consumption_rejects_report_scope_change_and_partial_substitution(services, tmp_path):
    reports, _ = report_pair(services, tmp_path)
    modified = deepcopy(reports[0])
    modified["scope"]["interval_end_s"] += 1
    with pytest.raises(ValueError, match="exact"):
        configuration_parameters(services[0], modified)
    modified = deepcopy(reports[0])
    modified["configuration"]["preflight"][0]["usable_for_full_comparison"] = False
    with pytest.raises(ValueError, match="complete"):
        configuration_parameters(services[0], modified)
    modified = deepcopy(reports[0])
    modified["configuration"]["preflight"].append(deepcopy(modified["configuration"]["preflight"][0]))
    assert compare_flight_configurations(services[0], modified, reports[1])["status"] == "unavailable"


def test_cancellation_and_changed_historical_original_are_not_silently_ignored(services, tmp_path):
    acquisition = readback(services, tmp_path)
    with pytest.raises(CancelledError):
        FlightConfigurationService(services[0]).promote({"acquisition_id": acquisition["id"]}, cancel_check=lambda: True)
    reports, _ = report_pair(services, tmp_path)
    # The extra earlier acquisition makes preflight ambiguous, not an automatic
    # nearest-time choice. It must remain unavailable for full-set comparison.
    assert compare_flight_configurations(services[0], *reports)["status"] == "unavailable"


def test_exact_subnormal_values_compare_fully_while_rpc_rows_remain_bounded(services, tmp_path):
    before = [(f"TEST_{index:04d}", "5e-324", 10) for index in range(800)]
    after = [(f"TEST_{index:04d}", "-5e-324", 10) for index in range(800)]
    reports, _ = report_pair(services, tmp_path, before, after)
    result = compare_flight_configurations(services[0], *reports)
    assert result["status"] == "compared" and result["counts"]["changed"] == 800
    assert result["truncated"] and result["all_parameters_compared"]
    assert len(result["changed"]) < 800 and len(json.dumps(result)) < 1_100_000
    snapshots = [report["configuration"]["preflight"][0]["snapshot_id"] for report in reports]
    normal = ParameterService(services[0]).dispatch("parameters.diff", {"before_id": snapshots[0], "after_id": snapshots[1]})
    assert normal["truncated"] and normal["counts"]["changed"] == 800 and len(json.dumps(normal)) < 1_100_000


def test_saved_full_configuration_comparison_reverifies_originals(services, tmp_path):
    reports, acquisitions = report_pair(services, tmp_path)
    raw = services[0].raw_path(acquisitions[0]["source_refs"][0]["sha256"])
    raw.chmod(0o600)
    raw.write_bytes(raw.read_bytes() + b"changed")
    result = compare_flight_configurations(services[0], *reports)
    assert result["status"] == "unavailable" and not result["eligible_for_parameter_review"]
    assert "SHA-256" in result["reasons"][0]
