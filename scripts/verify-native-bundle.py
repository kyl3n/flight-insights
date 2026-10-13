#!/usr/bin/env python3
"""Verify native artifacts on their own OS, including relocated bundled IPC.

Windows verification requires a clean runner/VM: it refuses any existing Q22
uninstall registration, installs into a temporary per-user directory, and
uninstalls it afterwards. macOS mounts the DMG read-only.
Linux extracts the Debian package and AppImage without a system installation.
--require-gui opens the actual installed production WebView and verifies its
rendered UI and IPC using an isolated data directory; hardware acceptance is
not implied. Publisher trust is checked
only when --require-publisher-signature is supplied on Windows/macOS.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("q22_packager", ROOT / "scripts" / "package-engine.py")
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)
REQUIRE_PUBLISHER = False
REQUIRE_GUI = False
GUI_REPETITIONS = 1
GUI_SUMMARY_DIRECTORY: Path | None = None


def run(command, **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True,
                              encoding="utf-8", timeout=kwargs.pop("timeout", 120), **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # Commands here contain artifact paths and public verification options,
        # never credentials. Do not dump the inherited environment or full logs.
        displayed = json.dumps(command, ensure_ascii=True)[:2000]
        outcome = (f"exited {error.returncode}" if isinstance(error, subprocess.CalledProcessError)
                   else f"timed out after {error.timeout} seconds")
        details = [f"Verification command {outcome}: {displayed}"]
        for name, output in (("stdout", error.stdout), ("stderr", error.stderr)):
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            if output:
                details.append(f"{name} (last 2000 characters):\n{output[-2000:]}")
        raise RuntimeError("\n".join(details)) from error


def emit_error_annotation(error: Exception) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Workflow command data requires escaping; subprocess output must never
        # inject another command through a newline or an encoded delimiter.
        message = str(error)[:7000].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Native artifact verification failed::{message}", flush=True)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def payload_digest(path: Path) -> str:
    """Signing can change a Mach-O executable; its frozen runtime must stay identical."""
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(path))
    digest = hashlib.sha256()
    digest.update(json.dumps(archive.options, ensure_ascii=True).encode("ascii"))
    for name in sorted(archive.toc):
        data = archive.extract(name)
        digest.update(json.dumps([name, archive.toc[name][-1], len(data)], ensure_ascii=True).encode("ascii"))
        digest.update(data)
    return digest.hexdigest()


def verify_gui_once(binary: Path, directory: Path, *, expected_engine: Path | None, env=None) -> dict:
    isolated = directory / ("Native GUI qualification " + secrets.token_hex(8))
    isolated.mkdir(mode=0o700)
    report_path = isolated / "native-gui.json"
    nonce = secrets.token_hex(32)
    try:
        run([str(binary), "--verify-gui", "--data-dir", str(isolated.resolve()),
             "--report", str(report_path.resolve()), "--nonce", nonce], cwd=directory, env=env, timeout=90)
    except RuntimeError as error:
        if report_path.is_file() and report_path.stat().st_size <= 128 * 1024:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                shutil.copyfile(report_path, ROOT / f"native-verification-gui-failure-{nonce[:16]}.json")
            try:
                failed = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                failed = None
            if (isinstance(failed, dict) and failed.get("schema_version") == 1
                    and failed.get("nonce") == nonce):
                # Put bounded report evidence first so GitHub's annotation
                # truncation cannot hide it behind the process invocation.
                summary = {key: failed.get(key) for key in (
                    "native_gui_rendering", "error", "shutdown_error",
                    "owned_engine_shutdown_completed", "diagnostic")}
                details = json.dumps(summary, ensure_ascii=True)[:3000]
                raise RuntimeError(f"Native GUI invocation report: {details}\n{error}") from error
        raise
    if not report_path.is_file() or report_path.stat().st_size > 128 * 1024:
        raise RuntimeError("Installed GUI did not produce bounded invocation-specific evidence")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (report.get("schema_version") != 1 or report.get("nonce") != nonce
            or report.get("native_gui_rendering") != "verified"
            or report.get("production_webview") is not True
            or report.get("isolated_data") is not True
            or report.get("owned_engine_shutdown_completed") is not True
            or report.get("security_bypasses") is not False):
        raise RuntimeError(f"Installed native WebView qualification failed: {report.get('error')}")
    if expected_engine is not None and not os.path.samefile(report["engine_path"], expected_engine):
        raise RuntimeError("Native GUI used an engine outside its verified installed bundle")
    # Keep relevant qualification evidence, not the invocation token or temporary paths.
    return {key: report[key] for key in ("native_gui_rendering", "production_webview", "isolated_data",
        "owned_engine_shutdown_completed", "security_bypasses", "evidence", "limitations")}


def verify_gui(binary: Path, directory: Path, *, expected_engine: Path | None, env=None) -> dict:
    """Independent fresh launches, all required; never retry a failed launch."""
    attempts = []
    summary_path = (GUI_SUMMARY_DIRECTORY or directory) / (
        "native-verification-gui-attempts-" + secrets.token_hex(8) + ".json")
    with summary_path.open("x", encoding="utf-8") as summary:
        def preserve() -> None:
            summary.seek(0)
            summary.truncate()
            json.dump({"schema_version": 1, "required_launches": GUI_REPETITIONS,
                       "all_launches_verified": (len(attempts) == GUI_REPETITIONS
                           and all(item["outcome"] == "verified" for item in attempts)),
                       "attempts": attempts}, summary, indent=2)
            summary.write("\n")
            summary.flush()
            os.fsync(summary.fileno())

        preserve()
        for number in range(1, GUI_REPETITIONS + 1):
            try:
                evidence = verify_gui_once(binary, directory, expected_engine=expected_engine, env=env)
            except Exception as error:
                attempts.append({"attempt": number, "outcome": "failed", "error": str(error)[:5000]})
                preserve()
                raise RuntimeError(f"Native GUI required launch {number}/{GUI_REPETITIONS} failed; "
                                   f"no retry was attempted.\n{error}") from error
            attempts.append({"attempt": number, "outcome": "verified", "evidence": evidence})
            preserve()
    # Retain the prior top-level evidence contract and add the ordered evidence
    # from every required launch, not merely the last successful process.
    return {**attempts[-1]["evidence"], "required_launches": GUI_REPETITIONS,
            "verified_launches": len(attempts), "launches": attempts}


def verify_layout(binary: Path, expected_engine: Path, directory: Path, *, gui_entrypoint: Path | None = None) -> dict:
    engine = binary.with_name("q22-engine.exe" if sys.platform == "win32" else "q22-engine")
    if not binary.is_file() or not engine.is_file():
        raise RuntimeError(f"Native executable or adjacent engine missing from {binary.parent}")
    if payload_digest(engine) != payload_digest(expected_engine):
        raise RuntimeError("The installer contains a different frozen engine payload")
    if sys.platform != "darwin" and not REQUIRE_PUBLISHER and sha256(engine) != sha256(expected_engine):
        raise RuntimeError(f"Unsigned packaged engine bytes differ from the verified source sidecar: {engine}")
    if REQUIRE_PUBLISHER and sys.platform == "win32":
        verify_authenticode(binary)
        verify_authenticode(engine)
    inventories = list(directory.rglob("frozen-inventory.json"))
    if len(inventories) != 1:
        raise RuntimeError("Bundle must include exactly one frozen dependency inventory")
    if inventories[0].read_bytes() != (PACKAGER.RESOURCES / "frozen-inventory.json").read_bytes():
        raise RuntimeError("Packaged dependency inventory differs from the verified build")
    notices = inventories[0].parent / "notices"
    for original in (PACKAGER.RESOURCES / "notices").rglob("*"):
        if original.is_file():
            bundled = notices / original.relative_to(PACKAGER.RESOURCES / "notices")
            if not bundled.is_file() or sha256(original) != sha256(bundled):
                raise RuntimeError(f"Required dependency notice missing or changed: {original.name}")
    application = PACKAGER.RESOURCES / "application-dependencies"
    application_report = json.loads((application / "manifest.json").read_text(encoding="utf-8"))
    frozen_report = json.loads(inventories[0].read_text(encoding="utf-8"))
    if (application_report["target"] != PACKAGER.host_target()
            or application_report["source_commit"] != frozen_report["source"]["commit"]):
        raise RuntimeError("Application dependency inventory does not match the frozen build target/commit")
    for name, expected in application_report["lockfile_sha256"].items():
        if sha256(ROOT / name) != expected:
            raise RuntimeError("Application dependency inventory lockfiles changed after collection")
    for original in application.rglob("*"):
        if original.is_file():
            bundled = inventories[0].parent / "application-dependencies" / original.relative_to(application)
            if not bundled.is_file() or sha256(bundled) != sha256(original):
                raise RuntimeError(f"Application dependency evidence missing or changed: {original.name}")
    PACKAGER.smoke(engine)
    result = run([str(binary), "--verify-engine", "--data-dir", str(directory / "Native data Δ with spaces")],
                 cwd=directory)
    report = json.loads(result.stdout)
    if (report.get("native_supervisor_verified") is not True
            or report.get("owned_engine_shutdown_completed") is not True
            or report["handshake"].get("protocol_version") != 1
            or report["status"].get("state") != "available"
            or report["restart_handshake"].get("product") != "Q22 Flight Lab"):
        raise RuntimeError("Installed native supervisor diagnostic did not pass")
    if not os.path.samefile(report["engine_path"], engine):
        raise RuntimeError("Native shell loaded a sidecar outside its verified bundle")
    gui = verify_gui(gui_entrypoint or binary, directory, expected_engine=engine) if REQUIRE_GUI else None
    return {"engine_sha256": sha256(engine), "engine_payload_sha256": payload_digest(engine),
            "native_supervisor_verified": True, "relocated_unicode_path": True,
            "dependency_inventory_and_notices_verified": True, "native_gui": gui,
            "application_dependency_package_count": application_report["package_count"],
            "unresolved_dependency_notice_count": len(application_report["unresolved_notice_texts"])}


def verify_app(app: Path, expected_engine: Path, directory: Path) -> dict:
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    minimum = config["bundle"]["macOS"]["minimumSystemVersion"]
    if info.get("LSMinimumSystemVersion") != minimum:
        raise RuntimeError("Application metadata and declared macOS minimum do not match")
    if not info.get("NSLocalNetworkUsageDescription"):
        raise RuntimeError("macOS bundle is missing its local-network permission explanation")
    binary = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
    found = PACKAGER.macos_minimum(binary)
    declared = tuple(int(value) for value in minimum.split("."))
    if found + (0,) * (3 - len(found)) > declared + (0,) * (3 - len(declared)):
        raise RuntimeError("Native shell requires a newer macOS than the bundle declares")
    # PyInstaller's sidecar is standalone code. The main executable's signature
    # seals its enclosing app resources, so verification must use bundle context.
    # An ad-hoc preview seal establishes integrity, not Developer ID trust.
    run(["codesign", "--verify", "--strict", str(binary.with_name("q22-engine"))])
    run(["codesign", "--verify", "--strict", str(app)])
    PACKAGER.audit_macos_runtime(binary.with_name("q22-engine"), minimum)
    if REQUIRE_PUBLISHER:
        run(["codesign", "--verify", "--deep", "--strict", str(app)])
        signature = run(["codesign", "--display", "--verbose=4", str(app)])
        team = os.environ.get("APPLE_TEAM_ID", "")
        if not team or f"TeamIdentifier={team}" not in signature.stderr.splitlines():
            raise RuntimeError("Signed application does not match the configured Apple team")
        run(["xcrun", "stapler", "validate", str(app)])
        run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)])
    return verify_layout(binary, expected_engine, directory)


def verify_authenticode(path: Path) -> None:
    expected = os.environ.get("Q22_WINDOWS_CERT_THUMBPRINT", "").replace(" ", "").upper()
    if not expected or any(char not in "0123456789ABCDEF" for char in expected):
        raise RuntimeError("Expected Windows certificate thumbprint is required")
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:Q22_VERIFY_SIGNATURE_PATH; "
              "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Thumbprint -ne "
              "$env:Q22_WINDOWS_CERT_THUMBPRINT) { throw 'Publisher signature validation failed' }")
    run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "Q22_VERIFY_SIGNATURE_PATH": str(path), "Q22_WINDOWS_CERT_THUMBPRINT": expected})


def require_clean_windows_registration() -> None:
    """NSIS checks this product key and matching MSI DisplayName on upgrade.

    Tauri 2.12.1's windows/nsis/installer.nsi defines UNINSTKEY as
    Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${PRODUCTNAME}.
    Inspect both hives/views before executing an installer that might upgrade it.
    """
    import winreg

    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    product = config["productName"].casefold()
    path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                base = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with base:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(base, index)
                    except OSError as error:
                        if error.winerror == 259:  # ERROR_NO_MORE_ITEMS
                            break
                        raise
                    index += 1
                    try:
                        with winreg.OpenKey(base, name, 0, winreg.KEY_READ | view) as key:
                            display = str(winreg.QueryValueEx(key, "DisplayName")[0]).casefold()
                    except FileNotFoundError:
                        display = ""
                    if name.casefold() == product or display == product:
                        raise RuntimeError("Existing Q22 Flight Lab registration found. Verify installers only on a clean disposable Windows runner/VM.")


def verify_artifact(artifact: Path, expected_engine: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="Q22 Installed Δ with spaces ") as temporary:
        directory = Path(temporary)
        suffix = artifact.suffix.lower()
        if suffix == ".deb" and sys.platform == "linux":
            destination = directory / "Debian installation"
            run(["dpkg-deb", "--extract", str(artifact), str(destination)])
            result = verify_layout(destination / "usr" / "bin" / "q22-flight-lab", expected_engine, directory)
        elif suffix == ".appimage" and sys.platform == "linux":
            run([str(artifact), "--appimage-extract"], cwd=directory)
            binaries = list((directory / "squashfs-root").rglob("q22-flight-lab"))
            binaries = [path for path in binaries if path.is_file() and path.parent.name == "bin"]
            if len(binaries) != 1:
                raise RuntimeError("Expected one application executable in the AppImage")
            app_run = directory / "squashfs-root" / "AppRun"
            if not app_run.is_file() or not os.access(app_run, os.X_OK):
                raise RuntimeError("AppImage must include its executable AppRun entrypoint")
            # AppRun supplies the packaged WebKit subprocess/library paths.
            # Launching usr/bin directly is valid for the non-GUI supervisor
            # diagnostic, but is not the supported AppImage GUI entrypoint.
            result = verify_layout(binaries[0], expected_engine, directory, gui_entrypoint=app_run)
            # Exercise the AppImage runtime wrapper as well as its extracted layout.
            env = {**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"}
            wrapped = run([str(artifact), "--verify-engine", "--data-dir", str(directory / "AppImage data Δ")], env=env)
            if json.loads(wrapped.stdout).get("native_supervisor_verified") is not True:
                raise RuntimeError("AppImage launcher failed its native diagnostic")
            result["appimage_launcher_verified"] = True
            if REQUIRE_GUI:
                result["appimage_launcher_gui"] = verify_gui(artifact, directory, expected_engine=None, env=env)
        elif suffix == ".app" and sys.platform == "darwin":
            destination = directory / "Q22 Flight Lab.app"
            shutil.copytree(artifact, destination, symlinks=True)
            result = verify_app(destination, expected_engine, directory)
        elif suffix == ".dmg" and sys.platform == "darwin":
            if REQUIRE_PUBLISHER:
                run(["codesign", "--verify", "--strict", str(artifact)])
                run(["xcrun", "stapler", "validate", str(artifact)])
                run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", str(artifact)])
            mounted = run(["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(artifact)])
            entities = plistlib.loads(mounted.stdout.encode("utf-8"))["system-entities"]
            volumes = [Path(item["mount-point"]) for item in entities if "mount-point" in item]
            try:
                apps = [app for volume in volumes for app in volume.glob("*.app")]
                if len(apps) != 1:
                    raise RuntimeError("Expected one application bundle in the DMG")
                destination = directory / "Q22 Flight Lab.app"
                shutil.copytree(apps[0], destination, symlinks=True)
                result = verify_app(destination, expected_engine, directory)
            finally:
                for volume in volumes:
                    run(["hdiutil", "detach", str(volume)])
        elif suffix == ".exe" and sys.platform == "win32":
            if REQUIRE_PUBLISHER:
                verify_authenticode(artifact)
            require_clean_windows_registration()
            destination = directory / "Application installation"
            # NSIS requires its /D argument last and unquoted, including spaces.
            # This is a CreateProcess command line, never a shell command.
            command = f'"{artifact}" /S /D={destination}'
            failure = None
            try:
                run(command, executable=str(artifact), timeout=600)
                if not (destination / "uninstall.exe").is_file():
                    raise RuntimeError("NSIS installation did not provide its required uninstaller")
                result = verify_layout(destination / "q22-flight-lab.exe", expected_engine, directory)
                result["nsis_install_verified"] = True
            except BaseException as error:
                failure = error
                raise
            finally:
                uninstaller = destination / "uninstall.exe"
                try:
                    if uninstaller.is_file():
                        # _?= prevents NSIS from spawning an untracked temporary
                        # copy. Run our explicit copy and wait for real completion.
                        driver = directory / "Q22 uninstall driver.exe"
                        shutil.copy2(uninstaller, driver)
                        run(f'"{driver}" /S _?={destination}', executable=str(driver), timeout=300)
                    require_clean_windows_registration()
                except Exception as cleanup_error:
                    if failure is None:
                        raise
                    failure.add_note(f"Partial-install cleanup also failed: {cleanup_error!r}")
        else:
            raise RuntimeError(f"Cannot verify {artifact.name} on {sys.platform}")
    return {"artifact": str(artifact), "artifact_sha256": None if artifact.is_dir() else sha256(artifact), **result}


def main() -> None:
    global REQUIRE_PUBLISHER, REQUIRE_GUI, GUI_REPETITIONS, GUI_SUMMARY_DIRECTORY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=PACKAGER.host_target())
    parser.add_argument("--artifact", action="append", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--formats", help="Required comma-separated bundle formats when using --bundle-root")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--require-publisher-signature", action="store_true")
    parser.add_argument("--require-gui", action="store_true",
                        help="Fail unless each installed production WebView renders and passes real IPC")
    parser.add_argument("--gui-repetitions", type=int, choices=range(1, 11), default=1,
                        help="Independent GUI launches per entrypoint; every launch must pass (default: 1)")
    args = parser.parse_args()
    REQUIRE_PUBLISHER = args.require_publisher_signature
    REQUIRE_GUI = args.require_gui
    if args.gui_repetitions != 1 and not REQUIRE_GUI:
        parser.error("--gui-repetitions requires --require-gui")
    GUI_REPETITIONS = args.gui_repetitions
    GUI_SUMMARY_DIRECTORY = args.report.resolve().parent
    GUI_SUMMARY_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if args.target != PACKAGER.host_target():
        raise SystemExit("Artifact verification must run on the target OS and CPU")
    artifacts = [path.resolve() for path in args.artifact or []]
    if args.bundle_root:
        if not args.formats:
            parser.error("--bundle-root requires --formats")
        patterns = {"deb": "deb/*.deb", "appimage": "appimage/*.AppImage", "nsis": "nsis/*.exe",
                    "app": "macos/*.app", "dmg": "dmg/*.dmg"}
        for format_name in args.formats.split(","):
            matches = list(args.bundle_root.glob(patterns[format_name]))
            if len(matches) != 1:
                raise RuntimeError(f"Expected exactly one {format_name} artifact; found {len(matches)}")
            artifacts.append(matches[0].resolve())
    if not artifacts:
        parser.error("Supply --artifact or --bundle-root and --formats")
    suffix = ".exe" if sys.platform == "win32" else ""
    expected = ROOT / "src-tauri" / "binaries" / f"q22-engine-{args.target}{suffix}"
    report = {"target": args.target, "artifacts": [verify_artifact(path, expected) for path in artifacts],
              "source": json.loads((PACKAGER.RESOURCES / "frozen-inventory.json").read_text(encoding="utf-8"))["source"],
              "publisher_signature": "verified" if REQUIRE_PUBLISHER and sys.platform in {"darwin", "win32"} else "not_tested",
              "native_gui_rendering": "verified" if REQUIRE_GUI else "not_tested", "hardware": "not_tested"}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {len(artifacts)} native bundle(s) on {args.target}; report: {args.report}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit_error_annotation(error)
        raise
