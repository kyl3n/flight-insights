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
