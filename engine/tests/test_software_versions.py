"""Exact executable metadata, bounded acquisition, and explicit unknown states."""
from copy import deepcopy
import ctypes
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from q22_engine import flight_environment as environment
from q22_engine import software_versions as versions


def process(name="vlc", executable="/usr/bin/vlc", pid=123):
    return SimpleNamespace(info={"name": name}, name=lambda: name, exe=lambda: executable,
                           is_running=lambda: True, create_time=lambda: 1234.0, pid=pid)


def observed(version="3.0.21-1", provenance="linux_dpkg_package"):
    return versions._result("test_fixture", status="observed", version=version,
                            provenance=provenance, version_kind="package_version")


def dpkg_fixture(monkeypatch, tmp_path, *, checksum_matches=True, exact_owner=True):
    executable = tmp_path / "vlc"
    executable.write_bytes(b"fixture executable bytes; never executed")
    # Package ownership uses Linux path syntax even when the test's image
    # lives in a Windows temporary directory. Keep the two inputs separate:
    # dpkg refuses backslash glob syntax, while digest verification reads the
    # actual host file without ever launching it.
    installed_path = "/usr/bin/vlc"
    digest = hashlib.md5(executable.read_bytes()).hexdigest() if checksum_matches else "0" * 32
    calls = []
    def run(command, deadline, *args):
        calls.append(command)
        assert command[0] == versions.DPKG
        assert command[-1] in (installed_path, "vlc-bin")
        if "--search" in command:
            return 0, "vlc-bin: " + (installed_path if exact_owner else installed_path + "-other") + "\n"
        return 0, "vlc-bin\t3.0.21-0+deb12u1\tinstalled\n"
    monkeypatch.setattr(versions, "_run", run)
    monkeypatch.setattr(versions, "_linux_candidates", lambda path: [path])
    def metadata(path, deadline):
        assert path == "/var/lib/dpkg/info/vlc-bin.md5sums"
        return f"{digest}  {installed_path.lstrip('/')}\n"
    monkeypatch.setattr(versions, "_read_metadata", metadata)
    return installed_path, executable, calls


def test_linux_exact_owner_and_running_image_checksum_produce_package_version(monkeypatch, tmp_path):
    installed_path, executable, calls = dpkg_fixture(monkeypatch, tmp_path)
    result = versions._dpkg_version(installed_path, str(executable), versions._signature(executable), time.monotonic() + 1)
    assert result["version"] == "3.0.21-0+deb12u1"
    assert result["version_provenance"] == "linux_dpkg_package"
    assert result["version_kind"] == "package_version"
    assert result["package_name"] == "vlc-bin"
    assert result["version_status"] == "observed"
    assert len(calls) == 2
    assert str(executable) not in json.dumps(result)


@pytest.mark.parametrize("checksum_matches,exact_owner,reason", [
    (False, True, "running_image_differs_from_package_metadata"),
    (True, False, "executable_not_owned_by_installed_package"),
])
def test_linux_does_not_guess_version_from_similar_paths_or_modified_programs(monkeypatch, tmp_path, checksum_matches, exact_owner, reason):
    installed_path, executable, _ = dpkg_fixture(monkeypatch, tmp_path, checksum_matches=checksum_matches, exact_owner=exact_owner)
    with pytest.raises(versions.MetadataUnavailable, match=reason):
        versions._dpkg_version(installed_path, str(executable), versions._signature(executable), time.monotonic() + 1)


def test_linux_rpm_uses_trusted_database_exact_file_and_digest(monkeypatch, tmp_path):
    executable = tmp_path / "mavlink-routerd"
    executable.write_bytes(b"fixture router, never executed")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    monkeypatch.setattr(versions, "_trusted", lambda path: path in ("/usr/lib/rpm/rpmrc", "/usr/lib/rpm/macros"))
    monkeypatch.setattr(versions, "_linux_candidates", lambda path: [path])
    def run(command, deadline, maximum):
        assert command[0] == "/usr/bin/rpm" and command[-2:] == ["--", str(executable)]
        assert command[command.index("--dbpath") + 1] == "/var/lib/rpm"
        assert "--noplugins" in command and "--macros" in command and "--rcfile" in command
        return 0, f"mavlink-router\t0:4.1-2.fc42\t8\n{executable}\t{digest}\n"
    monkeypatch.setattr(versions, "_run", run)
    result = versions._rpm_version(str(executable), str(executable), versions._signature(executable), time.monotonic() + 1, "/var/lib/rpm")
    assert result["version"] == "4.1-2.fc42"
    assert result["version_provenance"] == "linux_rpm_package"


def fake_windows(monkeypatch, *, size=128, valid=True, drive_type=3, attributes=0):
    from ctypes import wintypes
    calls = []
    class Function:
        def __init__(self, callback):
            self.callback = callback
        def __call__(self, *args):
            return self.callback(*args)
    def fill(path, unused, length, buffer):
        words = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        words[0] = 0xFEEF04BD if valid else 0
        words[2], words[3] = (1 << 16) | 3, (82 << 16) | 7
        return 1
    def query(buffer, subblock, pointer, length):
        assert subblock == "\\"
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(buffer)
        ctypes.cast(length, ctypes.POINTER(wintypes.UINT)).contents.value = 52
        return 1
    native = SimpleNamespace(GetFileVersionInfoSizeW=Function(lambda path, unused: size),
        GetFileVersionInfoW=Function(fill), VerQueryValueW=Function(query))
    kernel = SimpleNamespace(GetDriveTypeW=Function(lambda root: drive_type), GetFileAttributesW=Function(lambda path: attributes))
    def library(name, **kwargs):
        assert kwargs == {"use_last_error": True, "winmode": 0x00000800}
        calls.append(name)
        return {"version": native, "kernel32": kernel}[name]
    monkeypatch.setattr(versions.ctypes, "WinDLL", library, raising=False)
    return calls


def test_windows_reads_fixed_version_resource_without_launching_discovered_program(monkeypatch):
    calls = fake_windows(monkeypatch)
    monkeypatch.setattr(versions.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Windows version reading must not execute programs"))
    result = versions._windows_version("C:\\Flight\\MissionPlanner.exe")
    assert result["version"] == "1.3.82.7"
    assert result["version_provenance"] == "windows_file_version_resource"
    assert result["version_kind"] == "file_version"
    assert result["version_status"] == "observed"
    assert calls == ["kernel32", "version"]
    assert "Flight" not in json.dumps(result)


@pytest.mark.parametrize("kwargs,reason", [
    ({"size": 0}, "file_version_resource_missing"),
    ({"size": versions.MAX_METADATA_BYTES + 1}, "file_version_resource_exceeds_limit"),
    ({"valid": False}, "file_version_resource_invalid"),
    ({"drive_type": 4}, "nonlocal_executable_metadata_not_inspected"),
    ({"attributes": 0x400}, "reparse_executable_metadata_not_inspected"),
])
def test_windows_missing_invalid_oversized_or_network_resources_are_unknown(monkeypatch, kwargs, reason):
    fake_windows(monkeypatch, **kwargs)
    with pytest.raises(versions.MetadataUnavailable, match=reason):
        versions._windows_version("C:\\Flight\\MissionPlanner.exe")


def test_remote_linux_mounts_and_windows_unc_paths_are_not_inspected(monkeypatch):
    mountinfo = "1 0 0:1 / / rw - ext4 /dev/root rw\n2 1 0:2 / /network rw - nfs server:/private rw\n"
    def opening(path, *args, **kwargs):
        assert path == "/proc/self/mountinfo"
        return io.StringIO(mountinfo)
    monkeypatch.setattr("builtins.open", opening)
    assert versions._local_linux_path("/usr/bin/vlc")
    assert not versions._local_linux_path("/network/vlc")
    with pytest.raises(versions.MetadataUnavailable, match="network_executable_metadata_not_inspected"):
        versions._windows_local_path("\\\\server\\share\\MissionPlanner.exe")


def resolver_fixture(monkeypatch):
    monkeypatch.setattr(versions.sys, "platform", "linux")
    monkeypatch.setattr(versions, "_local_linux_path", lambda path: True)
    monkeypatch.setattr(versions, "_fresh_process", lambda target: target)
    state = {"identity": (1, 2, 30, 4, 5), "database": ("dpkg", (1, 2, 3, 4, 5)), "version": "1", "lookups": 0}
    monkeypatch.setattr(versions, "_signature", lambda path: state["identity"])
    resolver = versions.SoftwareVersionResolver()
    monkeypatch.setattr(resolver, "_database_identity", lambda: state["database"])
    def lookup(*args):
        state["lookups"] += 1
        return observed(state["version"])
    monkeypatch.setattr(versions, "_dpkg_version", lookup)
    return resolver, state


def test_cache_invalidates_on_executable_identity_database_and_ttl(monkeypatch):
    resolver, state = resolver_fixture(monkeypatch)
    target = process()
    deadline = time.monotonic() + 1
    first = resolver._resolve(target, "vlc", deadline)
    assert not first["version_cache_hit"] and first["version"] == "1"
    assert resolver._resolve(target, "vlc", deadline)["version_cache_hit"]
    assert state["lookups"] == 1
    state.update(identity=(1, 8, 40, 7, 6), version="2")
    second = resolver._resolve(target, "vlc", deadline)
    assert not second["version_cache_hit"] and second["version"] == "2"
    state["database"] = ("dpkg", (1, 8, 3, 4, 5))
    assert not resolver._resolve(target, "vlc", deadline)["version_cache_hit"]
    monkeypatch.setattr(versions, "CACHE_TTL_SECONDS", 0)
    assert not resolver._resolve(target, "vlc", deadline)["version_cache_hit"]
    assert state["lookups"] == 4
    assert first["version"] == "1"
    resolver.close()


def test_process_refresh_discards_cached_executable_path_and_rejects_reused_pid(monkeypatch):
    original = process(executable="/old/vlc")
    current = process(executable="/new/vlc")
    monkeypatch.setattr(versions.psutil, "Process", lambda pid: current)
    assert versions._fresh_process(original).exe() == "/new/vlc"
    current.create_time = lambda: 5678.0
    with pytest.raises(versions.MetadataUnavailable, match="recognized_process_changed_or_exited"):
        versions._fresh_process(original)


def test_running_old_image_after_install_update_is_not_given_new_package_version(monkeypatch):
    resolver, state = resolver_fixture(monkeypatch)
    monkeypatch.setattr(versions, "_signature", lambda path: (1, 2 if path.startswith("/proc/") else 3, 30, 4, 5))
    result = resolver._resolve(process(), "vlc", time.monotonic() + 1)
    assert result["version"] is None
    assert result["version_reason"] == "running_image_differs_from_installed_file"
    assert state["lookups"] == 0
    resolver.close()


def test_runtime_or_spoofed_process_name_does_not_assign_interpreter_version(monkeypatch):
    resolver, state = resolver_fixture(monkeypatch)
    result = resolver._resolve(process("mavproxy", "/usr/bin/python3"), "mavproxy", time.monotonic() + 1)
    assert result["version"] is None
    assert result["version_reason"] == "process_name_does_not_match_executable_name"
    assert state["lookups"] == 0
    resolver.close()


def test_pending_result_is_bounded_and_never_mutated_when_worker_completes(monkeypatch):
    resolver, _ = resolver_fixture(monkeypatch)
    release, entered = threading.Event(), threading.Event()
    def slow_lookup(*args):
        entered.set()
        release.wait(1)
        return observed("2")
    monkeypatch.setattr(versions, "_dpkg_version", slow_lookup)
    try:
        started = time.monotonic()
        pending = resolver.inspect([(process(), "vlc")], wait_seconds=0.005)
        elapsed = time.monotonic() - started
        assert entered.wait(0.1)
        assert elapsed < 0.1 and pending[0]["version_status"] == "pending"
        original = deepcopy(pending)
        release.set()
        later = resolver.inspect([(process(), "vlc")], wait_seconds=0.3)
        assert later[0]["version"] == "2"
        assert pending == original
    finally:
        release.set()
        resolver.close()
    assert resolver.inspect([(process(), "vlc")])[0]["version_reason"] == "version_detector_closed"


def test_queue_is_bounded_under_repeated_snapshot_requests(monkeypatch):
    resolver, _ = resolver_fixture(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def slow_lookup(*args):
        entered.set()
        release.wait(1)
        return observed()
    monkeypatch.setattr(versions, "_dpkg_version", slow_lookup)
    try:
        resolver.inspect([(process(), "vlc")])
        assert entered.wait(0.2)
        resolver.inspect([(process(), "vlc")])
        busy = resolver.inspect([(process(), "vlc")])
        assert busy[0]["version_reason"] == "metadata_worker_busy"
        assert resolver._queue.qsize() == 1
    finally:
        release.set()
        resolver.close()


def test_environment_only_passes_recognized_names_to_executable_inspector(monkeypatch):
    targets = [process("secret-unrelated-app", "/private/unrelated"), process("MissionPlanner.exe", "/private/MissionPlanner.exe"), process("vlc")]
    monkeypatch.setattr(environment.psutil, "process_iter", lambda **kwargs: iter(targets))
    class Resolver:
        def inspect(self, received, *, wait_seconds):
            assert [(item[0], item[1]) for item in received] == [(targets[1], "MissionPlanner.exe"), (targets[2], "vlc")]
            assert wait_seconds == 0.01
            return [observed("1.3.82.7", "windows_file_version_resource"), versions._result("fixture_pending", status="pending")]
    detector = environment.EnvironmentDetector()
    detector._versions = Resolver()
    snapshot = detector.inspect({"gcs_name": "Mission Planner", "gcs_version": "declared-version"}, scope="armed_interval_start")
    detected = snapshot["detected_applications"]
    assert detected[0]["version"] == "1.3.82.7" and detected[0]["provenance"] == "running_process_name"
    assert detected[1]["version"] is None and detected[1]["version_status"] == "pending"
    assert snapshot["declared_context"]["gcs_version"] == "declared-version"
    assert snapshot["coverage"]["versions_observed"] == snapshot["coverage"]["versions_pending"] == 1
    assert snapshot["coverage"]["version_detection"] == "partial"
    assert "secret-unrelated-app" not in json.dumps(snapshot) and "/private/" not in json.dumps(snapshot)


def test_multiple_instances_with_different_versions_remain_ambiguous(monkeypatch):
    monkeypatch.setattr(environment.psutil, "process_iter", lambda **kwargs: iter([process(), process(pid=456)]))
    resolver = SimpleNamespace(inspect=lambda targets, **kwargs: [observed("1"), observed("2")])
    applications, coverage = environment.EnvironmentDetector._applications(resolver)
    assert len(applications) == 1 and applications[0]["version"] is None
    assert applications[0]["version_status"] == "ambiguous"
    assert coverage["versions_unavailable"] == 1


def test_package_queries_cannot_launch_arbitrary_discovered_paths(monkeypatch):
    monkeypatch.setattr(versions.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Unapproved executable must not be launched"))
    with pytest.raises(versions.MetadataUnavailable, match="package_query_tool_untrusted_or_missing"):
        versions._run(["/tmp/vlc", "--version"], time.monotonic() + 1)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux dpkg capability check")
def test_installed_linux_package_metadata_can_be_read_and_verified_on_current_host():
    executable = "/usr/bin/dpkg-query"
    if not versions._trusted(executable) or not Path("/var/lib/dpkg/info/dpkg.md5sums").is_file():
        pytest.skip("This host does not provide trusted dpkg package metadata")
    # This calls only the fixed trusted package utility, never the target with
    # --version. Its version is retained only after installed checksum matching.
    result = versions._dpkg_version(executable, executable, versions._signature(executable), time.monotonic() + 3)
    assert result["version_status"] == "observed"
    assert result["version_provenance"] == "linux_dpkg_package"
    assert result["package_name"] == "dpkg"
    assert isinstance(result["version"], str) and result["version"]
