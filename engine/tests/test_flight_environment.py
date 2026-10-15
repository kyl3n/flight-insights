"""Host context is acquisition evidence, not aircraft or timing qualification."""
from copy import deepcopy
import hashlib
import json
import platform
import sqlite3
from types import SimpleNamespace

import pytest

from q22_engine import __version__
from q22_engine.__main__ import Engine
from q22_engine.collection import CollectionService
from q22_engine import flight_environment as environment
from q22_engine.store import Store
from test_collection import arm, event, prepare, profile


def assert_hash(snapshot):
    payload = {key: value for key, value in snapshot.items() if key not in {"id", "content_sha256"}}
    assert snapshot["content_sha256"] == hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def test_current_snapshot_has_actual_host_metadata_and_explicit_version_sources():
    detector = environment.EnvironmentDetector()
    snapshot = detector.inspect({"gcs_name": "Mission Planner", "gcs_version": "operator-entered-1.3",
                                 "applications": [{"name": "Video player", "version": "declared-2"}],
                                 "notes": "Declared runtime settings; not automatically discovered"})
    assert snapshot["schema_version"] == 1
    assert snapshot["id"] and snapshot["observed_at"]
    assert snapshot["status"] in {"captured", "partial"}
    assert snapshot["host"]["os"] == platform.system()
    assert snapshot["host"]["architecture"] == platform.machine()
    if platform.system() != "Windows":
        assert snapshot["host"]["release"] == platform.release()
    assert snapshot["software"]["engine"]["version"] == __version__
    assert snapshot["software"]["engine"]["provenance"] == "engine_module_version"
    assert snapshot["software"]["engine"]["build"] is None
    assert snapshot["software"]["application"]["version"] is None
    assert snapshot["declared_context"]["gcs_version"] == "operator-entered-1.3"
    assert snapshot["declared_context"]["provenance"] == "operator_declared"
    assert all((isinstance(item["version"], str) and item["version_provenance"] is not None)
               if item["version_status"] == "observed" else item["version"] is None
               for item in snapshot["detected_applications"])
    assert snapshot["load"]["available_memory_bytes"] > 0
    assert 0 <= snapshot["load"]["memory_percent"] <= 100
    assert snapshot["load"]["engine_rss_bytes"] > 0
    assert_hash(snapshot)
    detector.close()


def test_detector_only_retains_relevant_names_and_never_claims_declared_versions(monkeypatch):
    names = ["secret-unrelated-app", "MissionPlanner.exe", "QGroundControl", "qgroundcontrol.exe", "vlc.exe", "python"]
    def processes(*, attrs, ad_value):
        assert attrs == ["name"] and ad_value is None
        return iter(SimpleNamespace(info={"name": name}) for name in names)
    monkeypatch.setattr(environment.psutil, "process_iter", processes)
    monkeypatch.setattr(environment.time, "monotonic", lambda: 1.0)
    snapshot = environment.EnvironmentDetector().inspect({"gcs_name": "QGroundControl", "gcs_version": "99.1"})
    assert [item["name"] for item in snapshot["detected_applications"]] == ["Mission Planner", "QGroundControl", "VLC"]
    assert all(item["version"] is None and item["provenance"] == "running_process_name" for item in snapshot["detected_applications"])
    assert "secret-unrelated-app" not in json.dumps(snapshot)
    assert "python" not in [item["process_name"] for item in snapshot["detected_applications"]]
    assert snapshot["coverage"]["names_checked"] == len(names)
    assert snapshot["coverage"]["process_scan_status"] == "complete"
    assert snapshot["declared_context"]["gcs_version"] == "99.1"


def test_process_name_discovery_has_both_count_and_time_bounds(monkeypatch):
    seen = []
    def endless(**kwargs):
        while True:
            seen.append(1)
            yield SimpleNamespace(info={"name": "irrelevant"})
    monkeypatch.setattr(environment.psutil, "process_iter", endless)
    monkeypatch.setattr(environment.time, "monotonic", lambda: 0)
    applications, coverage = environment.EnvironmentDetector._applications()
    assert not applications
    assert len(seen) == environment.MAX_PROCESS_NAMES
    assert coverage["process_scan_status"] == "partial"
    ticks = iter([0.0, 0.02, 0.04])
    seen.clear()
    monkeypatch.setattr(environment.time, "monotonic", lambda: next(ticks))
    _, coverage = environment.EnvironmentDetector._applications()
    assert len(seen) == 1
    assert coverage["process_scan_status"] == "partial"


def test_host_distro_config_is_read_afresh_without_executing_programs(monkeypatch):
    import io
    import subprocess
    config = {"text": 'NAME="Synthetic test Linux"\nVERSION_ID="1"\nSECRET="not retained"\n'}
    monkeypatch.setattr(environment.os, "uname", lambda: SimpleNamespace(sysname="Linux", release="test-kernel", version="test-build", machine="test-arch"), raising=False)
    monkeypatch.setattr(environment, "Path", lambda location: SimpleNamespace(open=lambda *args, **kwargs: io.StringIO(config["text"])))
    def forbidden(*args, **kwargs):
        raise AssertionError("Environment detection must never run a program")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with monkeypatch.context() as linux:
        linux.setattr(environment.os, "name", "posix")
        first = environment.EnvironmentDetector._host()
        config["text"] = config["text"].replace('VERSION_ID="1"', 'VERSION_ID="2"')
        second = environment.EnvironmentDetector._host()
    assert first["distribution"] == "Synthetic test Linux"
    assert first["distribution_version"] == "1" and second["distribution_version"] == "2"
    assert "SECRET" not in json.dumps(first)


def test_windows_host_metadata_uses_os_apis_without_shell_fallback(monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("Host metadata must never invoke a subprocess")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(environment.sys, "getwindowsversion", lambda: SimpleNamespace(platform_version=(10, 0, 26100)), raising=False)
    monkeypatch.setattr(environment.EnvironmentDetector, "_windows_architecture", lambda: "ARM64")
    with monkeypatch.context() as windows:
        windows.setattr(environment.os, "name", "nt")
        host = environment.EnvironmentDetector._host()
    assert host["os"] == "Windows" and host["version"] == "10.0.26100"
    assert host["architecture"] == "ARM64"


def test_macos_product_release_is_separate_from_darwin_kernel_without_shell(monkeypatch):
    import io
    import plistlib
    import subprocess
    monkeypatch.setattr(environment.os, "uname", lambda: SimpleNamespace(sysname="Darwin", release="24.6.0", version="kernel-build", machine="arm64"), raising=False)
    def opening(path, mode):
        assert path == "/System/Library/CoreServices/SystemVersion.plist" and mode == "rb"
        return io.BytesIO(plistlib.dumps({"ProductVersion": "15.7", "ProductName": "macOS"}))
    monkeypatch.setattr("builtins.open", opening)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("OS metadata must not invoke a program"))
    with monkeypatch.context() as macos:
        macos.setattr(environment.os, "name", "posix")
        host = environment.EnvironmentDetector._host()
    assert host["release"] == "24.6.0"
    assert host["distribution"] == "macOS" and host["distribution_version"] == "15.7"
    assert host["provenance"] == "os.uname;system_version_plist"


def test_detection_failures_are_explicit_and_do_not_expose_exception_details(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private-path-and-secret-must-not-be-retained")
    detector = environment.EnvironmentDetector()
    monkeypatch.setattr(detector, "_host", fail)
    monkeypatch.setattr(environment.psutil, "process_iter", fail)
    snapshot = detector.inspect()
    assert snapshot["status"] == "partial"
    assert snapshot["host"]["os"] is None
    assert snapshot["coverage"]["process_scan_status"] == "unavailable"
    assert snapshot["errors"] == ["host_detection_failed:OSError", "process_scan_failed:OSError"]
    assert "private-path" not in json.dumps(snapshot)
    assert_hash(snapshot)


@pytest.mark.parametrize("context", [
    [], {"notes": "x" * 2001}, {"gcs_version": 3}, {"gcs_name": "x" * 161},
    {"detected_applications": []}, {"applications": [{"name": "x", "version": "v"}] * 9},
    {"applications": [{"name": "", "version": "v"}]},
    {"applications": [{"name": "x", "version": "v", "provenance": "detected"}]},
])
def test_declared_context_is_bounded_and_cannot_assert_detected_provenance(context):
    with pytest.raises(ValueError):
        environment.validate_context(context)


def test_context_changes_are_in_the_collection_applicability_fingerprint(tmp_path):
    service = CollectionService(Store(tmp_path))
    base = service.preview(profile())
    first = service.preview({**profile(), "environment_context": {"gcs_name": "QGroundControl", "gcs_version": "4.4"}})
    second = service.preview({**profile(), "environment_context": {"gcs_name": "QGroundControl", "gcs_version": "4.5"}})
    assert "environment_context" not in base["profile"]
    assert len({item["applicability_fingerprint"] for item in (base, first, second)}) == 3
    assert first["profile"]["environment_context"]["applications"] == []
    assert environment.validate_context({"applications": [{"name": " ", "version": ""}]})["applications"] == []


def test_each_new_interval_keeps_its_own_immutable_environment_across_revisions_and_restart(tmp_path, monkeypatch):
    detector = environment.EnvironmentDetector()
    original = detector.inspect
    current = {"version": "kernel-a", "calls": 0}
    monkeypatch.setattr(detector, "_host", lambda: {"os": "Synthetic OS", "release": current["version"], "version": "build",
                        "architecture": "test-arch", "distribution": "Test distro", "distribution_version": "1", "provenance": "test_fixture"})
    monkeypatch.setattr(detector, "_applications", lambda *args, **kwargs: ([], {"process_scan_status": "complete", "names_checked": 0, "version_detection": "complete"}))
    def inspect(context=None, *, scope="current_host"):
        current["calls"] += 1
        return original(context, scope=scope)
    monkeypatch.setattr(detector, "inspect", inspect)
    store = Store(tmp_path)
    service = CollectionService(store, environment=detector)
    capture_id, _ = prepare(service, {**profile(), "environment_context": {"gcs_name": "Mission Planner", "gcs_version": "declared-1.3"}})
    assert current["calls"] == 1
    capture_snapshot = service.status()["capture_session"]["environment_snapshot"]
    first = arm(service, capture_id)
    first_detail = service.detail({"flight_record_id": first["id"]})
    first_snapshot = deepcopy(first_detail["environment_snapshot"])
    assert current["calls"] == 2
    assert first_snapshot["id"] != capture_snapshot["id"]
    assert first_snapshot["scope"] == "armed_interval_start"
    assert first_snapshot["declared_context"]["gcs_version"] == "declared-1.3"
    for container in (first_detail["manifest"], first_detail["flight_record"], first_detail["armed_interval"]):
        assert container["environment_snapshot"] == first_snapshot
    current["version"] = "kernel-b"
    service.ingest_event(capture_id, event(2))
    armed = event(3, True)
    service.ingest_event(capture_id, armed)
    service.ingest_event(capture_id, armed)
    assert current["calls"] == 3
    records = service.status()["flight_records"]
    second = next(record for record in records if record["id"] != first["id"])
    second_snapshot = service.detail({"flight_record_id": second["id"]})["environment_snapshot"]
    assert second_snapshot["host"]["release"] == "kernel-b"
    assert second_snapshot["id"] != first_snapshot["id"]
    for revision in (1, None):
        query = {"flight_record_id": first["id"], **({"manifest_revision": revision} if revision else {})}
        assert service.detail(query)["environment_snapshot"] == first_snapshot
    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE collection_versions SET data='{}' WHERE kind='manifest' AND id=?", (first["id"],))
    restarted = CollectionService(store)
    monkeypatch.setattr(restarted.environment, "inspect", lambda *args, **kwargs: pytest.fail("Reading historical evidence must not detect the current host"))
    assert restarted.detail({"flight_record_id": first["id"]})["environment_snapshot"] == first_snapshot
    assert restarted.status()["capture_session"]["environment_snapshot"] == capture_snapshot
    assert_hash(first_snapshot)
    assert_hash(second_snapshot)


def test_detector_failure_cannot_drop_a_flight_and_preserves_declared_context(tmp_path):
    class FailedDetector:
        def inspect(self, *args, **kwargs):
            raise RuntimeError("secret detail")
    service = CollectionService(Store(tmp_path), environment=FailedDetector())
    capture_id, _ = prepare(service, {**profile(), "environment_context": {"gcs_name": "QGroundControl"}})
    record = arm(service, capture_id)
    snapshot = service.detail({"flight_record_id": record["id"]})["environment_snapshot"]
    assert snapshot["status"] == "unavailable"
    assert snapshot["errors"] == ["detector_failed:RuntimeError"]
    assert snapshot["id"] and snapshot["observed_at"]
    assert snapshot["declared_context"]["gcs_name"] == "QGroundControl"
    assert "secret detail" not in json.dumps(snapshot)
    assert_hash(snapshot)


def test_flight_records_keep_observed_version_or_pending_without_later_rewrite(tmp_path, monkeypatch):
    from q22_engine.software_versions import _result
    current = {"version": "1.3.82.0"}
    monkeypatch.setattr(environment.psutil, "process_iter", lambda **kwargs: iter([SimpleNamespace(info={"name": "MissionPlanner.exe"})]))
    class Resolver:
        def inspect(self, targets, **kwargs):
            if current["version"] is None:
                return [_result("metadata_lookup_pending", status="pending")]
            return [_result("version_resource_of_recognized_running_executable", status="observed", version=current["version"],
                            provenance="windows_file_version_resource", version_kind="file_version")]
    detector = environment.EnvironmentDetector()
    detector._versions = Resolver()
    service = CollectionService(Store(tmp_path), environment=detector)
    capture_id, _ = prepare(service, {**profile(), "environment_context": {"gcs_version": "operator declaration"}})
    first = arm(service, capture_id)
    original = deepcopy(service.detail({"flight_record_id": first["id"]})["environment_snapshot"])
    assert original["detected_applications"][0]["version"] == "1.3.82.0"
    current["version"] = None
    service.ingest_event(capture_id, event(2))
    service.ingest_event(capture_id, event(3, True))
    second = next(record for record in service.status()["flight_records"] if record["id"] != first["id"])
    pending = deepcopy(service.detail({"flight_record_id": second["id"]})["environment_snapshot"])
    assert pending["detected_applications"][0]["version_status"] == "pending"
    current["version"] = "1.3.83.0"
    service.ingest_event(capture_id, event(4))
    service.ingest_event(capture_id, event(5, True))
    third = next(record for record in service.status()["flight_records"] if record["id"] not in {first["id"], second["id"]})
    latest = service.detail({"flight_record_id": third["id"]})["environment_snapshot"]
    assert latest["detected_applications"][0]["version"] == "1.3.83.0"
    assert latest["declared_context"]["gcs_version"] == "operator declaration"
    assert service.detail({"flight_record_id": first["id"]})["environment_snapshot"] == original
    assert service.detail({"flight_record_id": second["id"]})["environment_snapshot"] == pending
    for snapshot in (original, pending, latest):
        assert_hash(snapshot)


def test_legacy_rows_explicitly_report_not_recorded_without_rewriting_originals(tmp_path):
    store = Store(tmp_path)
    service = CollectionService(store)
    # Insert the shape of an earlier stored record; no detector is invoked to
    # manufacture history for it, and the original row is left byte-for-byte.
    original = json.dumps({"id": "old-flight", "revision": 1})
    with store._connect() as db:
        db.execute("INSERT INTO collection_versions VALUES ('flight_record','old-flight',1,'old',?)", (original,))
        db.execute("INSERT INTO collection_heads VALUES ('flight_record','old-flight',1)")
        record = service._get(db, "flight_record", "old-flight")
        snapshot = record["environment_snapshot"]
        assert snapshot["status"] == "unavailable"
        assert snapshot["errors"] == ["not_recorded"]
        assert snapshot["id"] is snapshot["content_sha256"] is snapshot["observed_at"] is None
        assert db.execute("SELECT data FROM collection_versions WHERE id='old-flight'").fetchone()[0] == original


def test_inspection_rpc_is_read_only_and_app_version_is_client_reported(tmp_path):
    engine = Engine(tmp_path)
    try:
        handshake = engine.call("handshake", {"client": "q22-desktop", "client_version": "0.1.0-test"})
        assert "environment.inspect" in handshake["capabilities"]
        result = engine.call("environment.inspect", {"environment_context": {"gcs_name": "Mission Planner", "gcs_version": "declared"}})
        assert result["scope"] == "current_host"
        assert result["software"]["application"]["version"] == "0.1.0-test"
        assert result["software"]["application"]["provenance"] == "desktop_handshake_client_reported"
        assert not engine.collection.recorder.status()["active"]
        assert engine.call("session.status", {})["capture_sessions"] == []
        assert engine.call("jobs.list", {})["jobs"] == []
    finally:
        engine.close()


def test_snapshot_comparison_separates_declared_changes_and_unknown_history():
    left = environment.EnvironmentDetector().inspect({"gcs_name": "Mission Planner", "gcs_version": "1"})
    right = deepcopy(left)
    right["host"]["release"] = "different-kernel"
    right["declared_context"]["gcs_version"] = "2"
    result = environment.diff_snapshots(left, right)
    assert {item["field"] for item in result["changes"]} == {"host.release", "declared_context.gcs_version"}
    declaration = next(item for item in result["changes"] if item["field"].startswith("declared_context"))
    assert declaration["provenance"] == "operator_declared"
    assert environment.diff_snapshots(left, environment.unavailable_snapshot())["status"] == "unavailable"
    assert any("Unknown software versions" in item for item in result["limitations"])
