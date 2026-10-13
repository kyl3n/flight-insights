"""Qualification transport safeguards, separate from actual native GUI CI."""
import importlib.util
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
        assert message.startswith("Native GUI invocation report:")

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
