"""Qualification transport safeguards, separate from actual native GUI CI."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]

def script(relative):
    spec = importlib.util.spec_from_file_location("qualification_" + Path(relative).stem.replace("-", "_"), ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("platform,locked_attempts,expected_calls,raises", [
    ("win32", 2, 3, False),
    ("win32", 100, 31, True),
    ("linux", 2, 1, True),
])
def test_qualification_cleanup_waits_for_transient_windows_locks_only(
        monkeypatch, platform, locked_attempts, expected_calls, raises):
    verify = script("scripts/verify-native-bundle.py")
    directory = verify.QualificationDirectory()
    original_cleanup = verify.tempfile.TemporaryDirectory.cleanup
    calls = []
    waits = []
    def locked_cleanup(instance):
        calls.append(instance.name)
        if len(calls) <= locked_attempts:
            raise PermissionError("synthetic WebView cache lock")
        original_cleanup(instance)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(verify.sys, "platform", platform)
            patch.setattr(verify.tempfile.TemporaryDirectory, "cleanup", locked_cleanup)
            patch.setattr(verify.time, "sleep", waits.append)
            if raises:
                with pytest.raises(PermissionError, match="WebView cache lock"):
                    directory.cleanup()
            else:
                directory.cleanup()
        assert len(calls) == expected_calls
        assert waits == [0.5] * (expected_calls - 1)
        assert os.path.exists(directory.name) is raises
    finally:
        original_cleanup(directory)


def test_qualification_cleanup_does_not_retry_other_errors(monkeypatch):
    verify = script("scripts/verify-native-bundle.py")
    directory = verify.QualificationDirectory()
    original_cleanup = verify.tempfile.TemporaryDirectory.cleanup
    def broken_cleanup(instance):
        raise OSError("synthetic filesystem failure")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(verify.tempfile.TemporaryDirectory, "cleanup", broken_cleanup)
            patch.setattr(verify.time, "sleep", lambda _: pytest.fail("unexpected cleanup retry"))
            with pytest.raises(OSError, match="filesystem failure"):
                directory.cleanup()
    finally:
        original_cleanup(directory)

def test_gui_qualification_rejects_stale_report_from_another_invocation(tmp_path, monkeypatch):
    verify = script("scripts/verify-native-bundle.py")
    engine = tmp_path / "q22-engine"
    engine.write_bytes(b"synthetic engine path only")
    def fake_process(command, **kwargs):
        # A previous invocation's otherwise successful report must not qualify
        # the currently launched installed executable.
        Path(command[command.index("--report") + 1]).write_text(json.dumps({
            "schema_version": 1, "nonce": "0" * 64, "native_gui_rendering": "verified",
            "production_webview": True, "isolated_data": True,
            "owned_engine_shutdown_completed": True, "security_bypasses": False,
            "engine_path": str(engine), "evidence": {}, "limitations": [],
        }))
    monkeypatch.setattr(verify, "run", fake_process)
    monkeypatch.setattr(verify.secrets, "token_hex", lambda count: "a" * (count * 2))
    with pytest.raises(RuntimeError, match="WebView qualification failed"):
        verify.verify_gui(tmp_path / "application", tmp_path, expected_engine=engine)

def test_gui_qualification_rejects_sidecar_outside_installed_bundle(tmp_path, monkeypatch):
    verify = script("scripts/verify-native-bundle.py")
    expected = tmp_path / "installed-engine"
    external = tmp_path / "unrelated-engine"
    expected.write_bytes(b"expected"); external.write_bytes(b"other")
    def fake_process(command, **kwargs):
        Path(command[command.index("--report") + 1]).write_text(json.dumps({
            "schema_version": 1, "nonce": command[command.index("--nonce") + 1],
            "native_gui_rendering": "verified", "production_webview": True,
            "isolated_data": True, "owned_engine_shutdown_completed": True,
            "security_bypasses": False, "engine_path": str(external),
            "evidence": {}, "limitations": [],
        }))
    monkeypatch.setattr(verify, "run", fake_process)
    with pytest.raises(RuntimeError, match="outside its verified installed bundle"):
        verify.verify_gui(tmp_path / "application", tmp_path, expected_engine=expected)

@pytest.mark.parametrize("matching_nonce", [True, False])
def test_gui_failure_exposes_only_matching_bounded_report_before_process_output(tmp_path, monkeypatch, matching_nonce):
    verify = script("scripts/verify-native-bundle.py")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    def fake_process(command, **kwargs):
        nonce = command[command.index("--nonce") + 1] if matching_nonce else "stale"
        Path(command[command.index("--report") + 1]).write_text(json.dumps({
            "schema_version": 1, "nonce": nonce, "native_gui_rendering": "failed",
            "error": "WebView self-test failed during engine_restart",
            "diagnostic": {"phase": "engine_restart", "error": "bounded failure"},
            "engine_path": "not included in diagnostic summary", "evidence": {"private": "omit"},
            "owned_engine_shutdown_completed": True,
        }))
        raise RuntimeError("Verification command exited 1")
    monkeypatch.setattr(verify, "run", fake_process)
    with pytest.raises(RuntimeError) as caught:
        verify.verify_gui(tmp_path / "application", tmp_path, expected_engine=None)
    message = str(caught.value)
    assert "Verification command exited 1" in message
    assert ("engine_restart" in message) is matching_nonce
    assert "engine_path" not in message and "private" not in message
    if matching_nonce:
        assert message.index("Native GUI invocation report:") < message.index("Verification command exited 1")

@pytest.mark.parametrize("fail_at", [None, 2])
def test_gui_repetitions_require_fresh_launches_and_preserve_order_without_retry(tmp_path, monkeypatch, fail_at):
    verify = script("scripts/verify-native-bundle.py")
    monkeypatch.setattr(verify, "GUI_REPETITIONS", 3)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    invocations = []
    def fake_process(command, **kwargs):
        report_path = Path(command[command.index("--report") + 1])
        data_dir = Path(command[command.index("--data-dir") + 1])
        nonce = command[command.index("--nonce") + 1]
        assert not list(data_dir.iterdir())
        invocations.append((data_dir, report_path, nonce))
        if len(invocations) == fail_at:
            raise RuntimeError("intentional launch failure")
        report_path.write_text(json.dumps({
            "schema_version": 1, "nonce": nonce, "native_gui_rendering": "verified",
            "production_webview": True, "isolated_data": True,
            "owned_engine_shutdown_completed": True, "security_bypasses": False,
            "evidence": {"synthetic_launch": len(invocations)}, "limitations": [],
        }))
    monkeypatch.setattr(verify, "run", fake_process)
    if fail_at:
        with pytest.raises(RuntimeError, match="required launch 2/3 failed; no retry"):
            verify.verify_gui(tmp_path / "application", tmp_path, expected_engine=None)
        assert len(invocations) == 2
    else:
        result = verify.verify_gui(tmp_path / "application", tmp_path, expected_engine=None)
        assert result["required_launches"] == result["verified_launches"] == 3
        assert len(result["launches"]) == 3
    assert len(set(invocations)) == len(invocations)
    assert len({item[0] for item in invocations}) == len(invocations)
    summary_path, = tmp_path.glob("native-verification-gui-attempts-*.json")
    summary = json.loads(summary_path.read_text())
    assert summary["all_launches_verified"] is (fail_at is None)
    assert [item["attempt"] for item in summary["attempts"]] == list(range(1, len(invocations) + 1))
    assert [item["outcome"] for item in summary["attempts"]] == (
        ["verified", "failed"] if fail_at else ["verified"] * 3)

def test_unprotected_signing_ref_stops_before_touching_os_credential_tools(monkeypatch):
    preflight = script("src-tauri/signing/preflight.py")
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.1.0")
    monkeypatch.setenv("GITHUB_REF_PROTECTED", "false")
    calls = []
    monkeypatch.setattr(preflight, "command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="protected reviewed release tag"):
        preflight.inspect()
    assert calls == []

def test_empty_verification_error_preserves_exception_class_and_traceback(monkeypatch, capsys):
    verify = script("scripts/verify-native-bundle.py")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    try:
        raise AssertionError()
    except AssertionError as error:
        verify.emit_error_annotation(error)
    output = capsys.readouterr().out
    assert "::error title=Native artifact verification failed" in output
    assert "AssertionError" in output and "Traceback" in output
    assert "test_empty_verification_error_preserves_exception_class_and_traceback" in output

def test_verification_annotation_preserves_unicode_with_restrictive_stdout(monkeypatch):
    verify = script("scripts/verify-native-bundle.py")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    with monkeypatch.context() as context:
        context.setattr(verify.sys, "stdout", stream)
        verify.emit_error_annotation(RuntimeError("Installed Δ path failed\n::notice::100%"))
        output = raw.getvalue().decode("utf-8")
    assert "RuntimeError: Installed Δ path failed" in output
    assert "%0A::notice::100%25" in output
    assert output.count("\n") == 1


@pytest.mark.skipif(sys.platform != "linux", reason="Linux AppImage launch contract")
def test_appimage_keeps_direct_supervisor_and_uses_both_real_gui_entrypoints(tmp_path, monkeypatch):
    verify = script("scripts/verify-native-bundle.py")
    artifact = tmp_path / "Synthetic.AppImage"
    artifact.write_bytes(b"synthetic launch-contract fixture; not an application")
    expected = tmp_path / "expected-engine"
    expected.write_bytes(b"synthetic engine")
    calls = []

    def fake_run(command, **kwargs):
        if "--appimage-extract" in command:
            appdir = kwargs["cwd"] / "squashfs-root"
            binary = appdir / "usr/bin/q22-flight-lab"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"synthetic inner executable")
            entrypoint = appdir / "AppRun"
            entrypoint.write_bytes(b"synthetic packaged entrypoint")
            entrypoint.chmod(0o755)
        else:
            assert command[0] == str(artifact) and command[1] == "--verify-engine"
            assert kwargs["env"]["APPIMAGE_EXTRACT_AND_RUN"] == "1"
            calls.append("wrapped-supervisor")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"native_supervisor_verified": True}))

    def layout(binary, expected_engine, directory, *, gui_entrypoint=None):
        assert binary.name == "q22-flight-lab" and binary.parent.name == "bin"
        assert expected_engine == expected
        assert gui_entrypoint == directory / "squashfs-root/AppRun"
        calls.append("direct-supervisor-and-extracted-AppRun-GUI")
        return {"native_gui": {"native_gui_rendering": "synthetic_fixture_only"}}

    def wrapped_gui(binary, directory, *, expected_engine, env):
        assert binary == artifact and expected_engine is None
        assert env["APPIMAGE_EXTRACT_AND_RUN"] == "1"
        calls.append("actual-AppImage-GUI")
        return {"native_gui_rendering": "synthetic_fixture_only"}

    monkeypatch.setattr(verify, "REQUIRE_GUI", True)
    monkeypatch.setattr(verify, "run", fake_run)
    monkeypatch.setattr(verify, "verify_layout", layout)
    monkeypatch.setattr(verify, "verify_gui", wrapped_gui)
    result = verify.verify_artifact(artifact, expected)
    assert calls == ["direct-supervisor-and-extracted-AppRun-GUI", "wrapped-supervisor", "actual-AppImage-GUI"]
    assert result["appimage_launcher_gui"]["native_gui_rendering"] == "synthetic_fixture_only"

@pytest.mark.skipif(sys.platform != "linux", reason="Linux offline bench wrapper")
def test_linux_offline_bench_retains_failed_launch_evidence(tmp_path):
    output = tmp_path / "failed bench evidence"
    result = subprocess.run(["bash", str(ROOT / "scripts/qualify-installed-linux.sh"),
        "/bin/false", str(output)], capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "Native GUI process exit code: 1" in (output / "host.txt").read_text()
    assert (output / "READ-ME.txt").is_file()
    assert output.with_name(output.name + ".tar.gz").is_file()

@pytest.mark.skipif(sys.platform != "win32", reason="Windows offline bench wrapper")
def test_windows_offline_bench_retains_failed_launch_evidence(tmp_path):
    # A real non-application executable rejects the GUI arguments immediately.
    # This qualifies failure packaging only, never claims native GUI success.
    system = Path(os.environ["SystemRoot"]) / "System32"
    output = tmp_path / "failed bench evidence"
    result = subprocess.run([str(system / "WindowsPowerShell/v1.0/powershell.exe"),
        "-NoProfile", "-File", str(ROOT / "scripts/qualify-installed-windows.ps1"),
        "-ApplicationPath", str(system / "where.exe"), "-OutputDirectory", str(output)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    evidence = json.loads((output / "host.json").read_text(encoding="utf-8-sig"))
    assert evidence.get("qualification_error") or evidence["native_gui_exit_code"] != 0
    assert (output / "READ-ME.txt").is_file()
    assert output.with_name(output.name + ".zip").is_file()
