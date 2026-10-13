from datetime import datetime, timedelta, timezone
import json
import re

import pytest

from q22_engine.guidance import (
    FIRMWARE_REVISION, REGISTRY_VERSION, RETRIEVED_AT, WIKI_REVISION,
    citation, registry,
)


RETRIEVED = datetime.fromisoformat(RETRIEVED_AT.replace("Z", "+00:00"))


def test_source_provenance_is_complete_and_pinned():
    document = registry(now=RETRIEVED)
    assert document["version"] == REGISTRY_VERSION
    assert document["mode"] == "bundled_offline"
    for source in document["sources"].values():
        assert source["revision"] in {WIKI_REVISION, FIRMWARE_REVISION}
        assert f"/blob/{source['revision']}/" in source["url"]
        assert re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
        assert source["retrieved_at"] == RETRIEVED_AT
    for entry in document["entries"]:
        assert entry["evidence_requirements"] and entry["limitations"]
        for reference in entry["references"]:
            assert reference["source_id"] in document["sources"]
            assert reference["line_end"] >= reference["line_start"] > 0
    json.dumps(document, allow_nan=False)


@pytest.mark.parametrize("offset,status", [
    (-1, "clock_before_retrieval"), (0, "within_review_window"),
    (89, "within_review_window"), (90, "review_due"), (1000, "review_due"),
])
def test_freshness_does_not_claim_current_remote_guidance(offset, status):
    document = registry(now=RETRIEVED + timedelta(days=offset))
    assert document["freshness"]["status"] == status
    assert document["entries"]  # Historical references remain reviewable.


@pytest.mark.parametrize("firmware", [None, "4.7.1", "Copter-4.6.3", "Plane-4.7.1", "Copter-4.7.1-custom"])
def test_unmatched_firmware_never_inherits_reviewed_applicability(firmware):
    match = registry(firmware, now=RETRIEVED)["firmware_compatibility"]
    assert not match["release_label_match"]
    assert not match["exact_build_validated"]
    assert not match["parameter_application_allowed"]


def test_matching_release_label_is_not_exact_build_validation():
    match = registry("Copter-4.7.1", now=RETRIEVED)["firmware_compatibility"]
    assert match["release_label_match"]
    assert not match["exact_build_validated"]
    assert not match["parameter_application_allowed"]


def test_callers_cannot_mutate_shared_references():
    first = citation("clipping")
    first["references"][0]["sha256"] = "changed"
    first["limitations"].clear()
    assert citation("clipping")["references"][0]["sha256"] != "changed"
    assert citation("clipping")["limitations"]
    first_registry = registry(now=RETRIEVED)
    first_registry["sources"]["imu"]["revision"] = "changed"
    assert registry(now=RETRIEVED)["sources"]["imu"]["revision"] == FIRMWARE_REVISION


def test_invalid_requests_fail_closed():
    with pytest.raises(ValueError, match="Unknown guidance"):
        citation("invented_source")
    with pytest.raises(ValueError, match="timezone"):
        registry(now=datetime(2026, 10, 13))
    with pytest.raises(ValueError, match="firmware"):
        registry({"version": "Copter-4.7.1"}, now=datetime.now(timezone.utc))
