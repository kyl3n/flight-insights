#!/usr/bin/env python3
"""Sign on provisioned release runners; never import keys or publish artifacts."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("q22_packager", ROOT / "scripts/package-engine.py")
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, text=True, encoding="utf-8",
                          timeout=kwargs.pop("timeout", 3600), **kwargs)


def required(name, pattern=None):
    value = os.environ.get(name, "")
    if not value or (pattern and re.fullmatch(pattern, value) is None):
        raise RuntimeError(f"Release runner is missing a valid {name}")
    return value


def notarize(path, profile, report):
    result = run(["xcrun", "notarytool", "submit", str(path), "--keychain-profile", profile,
                  "--wait", "--output-format", "json"], capture_output=True)
    document = json.loads(result.stdout)
    report.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if document.get("status") != "Accepted":
        raise RuntimeError("Apple notarization did not accept this artifact; inspect the submission report")


def main():
    # The protected GitHub environment also checks these refs before assigning a
    # runner. Do not accept an arbitrary input tag or check out unreviewed code.
    if (os.environ.get("GITHUB_REF_TYPE") != "tag" or os.environ.get("GITHUB_REF_PROTECTED") != "true"
            or not re.fullmatch(r"refs/tags/v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", required("GITHUB_REF"))):
        raise RuntimeError("Signing requires a protected vMAJOR.MINOR.PATCH release tag")
    target = PACKAGER.host_target()
    bundle = ROOT / "src-tauri" / "target" / target / "release" / "bundle"
    if bundle.exists():
        # A dedicated ephemeral checkout prevents accidentally signing old output.
        raise RuntimeError("Signing requires a clean checkout without previous bundle output")
    suffix = ".exe" if sys.platform == "win32" else ""
    engine = ROOT / "src-tauri" / "binaries" / f"q22-engine-{target}{suffix}"
    override = {}
    if sys.platform == "win32":
        thumbprint = required("Q22_WINDOWS_CERT_THUMBPRINT", r"[A-Fa-f0-9]{40}")
        timestamp = required("Q22_WINDOWS_TIMESTAMP_URL", r"https://[^\s]+")
        run(["signtool", "sign", "/sha1", thumbprint, "/fd", "SHA256", "/tr", timestamp, "/td", "SHA256", str(engine)])
        run(["signtool", "verify", "/pa", "/all", str(engine)])
        run([sys.executable, str(ROOT / "scripts/package-engine.py"), "--target", target, "--smoke-only"])
        override = {"bundle": {"windows": {"certificateThumbprint": thumbprint, "digestAlgorithm": "sha256",
                                           "timestampUrl": timestamp, "tsp": True}}}
        formats = "nsis"
    elif sys.platform == "darwin":
        identity = required("APPLE_SIGNING_IDENTITY")
        required("APPLE_TEAM_ID", r"[A-Z0-9]{10}")
        profile = required("Q22_APPLE_NOTARY_PROFILE")
        # package-engine.py already signed every embedded Python native library
        # with this identity; the same-team requirement is not relaxed.
        override = {"bundle": {"macOS": {"signingIdentity": identity}}}
        formats = "app"
    else:
        release = platform.freedesktop_os_release()
        if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "22.04":
            raise RuntimeError("Linux release signing requires the documented Ubuntu 22.04 build baseline")
        fingerprint = required("Q22_GPG_FINGERPRINT", r"[A-Fa-f0-9]{40,64}")
        run(["gpg", "--batch", "--list-secret-keys", fingerprint], capture_output=True)
        formats = "deb,appimage"
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Provisioned Node is missing")
    # Invoke JavaScript directly: npm.cmd would reparse JSON quoting via cmd.exe.
    run([node, str(ROOT / "node_modules/@tauri-apps/cli/tauri.js"), "build", "--target", target, "--bundles", formats,
         "--config", json.dumps(override), "--ci", "--", "--locked"],
        env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"})
    if sys.platform == "darwin":
        apps = list((bundle / "macos").glob("*.app"))
        if len(apps) != 1:
            raise RuntimeError("Expected exactly one application to notarize")
        app = apps[0]
        with tempfile.TemporaryDirectory(prefix="Q22 notarization ") as temporary:
            directory = Path(temporary)
            archive = directory / "application.zip"
            run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)])
            notarize(archive, profile, bundle / "app-notarization.json")
            run(["xcrun", "stapler", "staple", str(app)])
            staging = directory / "volume"
            staging.mkdir()
            run(["ditto", str(app), str(staging / app.name)])
            (staging / "Applications").symlink_to("/Applications")
            dmg = bundle / "dmg" / f"Q22 Flight Lab_{target}.dmg"
            dmg.parent.mkdir()
            run(["hdiutil", "create", "-volname", "Q22 Flight Lab", "-srcfolder", str(staging),
                 "-format", "UDZO", "-ov", str(dmg)])
            run(["codesign", "--sign", identity, "--timestamp", str(dmg)])
            notarize(dmg, profile, bundle / "dmg-notarization.json")
            run(["xcrun", "stapler", "staple", str(dmg)])
        formats = "app,dmg"
    command = [sys.executable, str(ROOT / "scripts/verify-native-bundle.py"), "--target", target,
               "--bundle-root", str(bundle), "--formats", formats,
               "--report", str(ROOT / f"native-verification-signed-{target}.json")]
    if sys.platform in {"win32", "darwin"}:
        command.append("--require-publisher-signature")
    run(command)
    if sys.platform == "darwin":
        # Raw upload-artifact directories lose symlinks and executable mode.
        shutil.make_archive(str(app), "gztar", root_dir=app.parent, base_dir=app.name)
    if sys.platform == "linux":
        for pattern in ("deb/*.deb", "appimage/*.AppImage"):
            artifact, = bundle.glob(pattern)
            run(["gpg", "--batch", "--yes", "--local-user", fingerprint, "--armor", "--detach-sign", str(artifact)])
            verified = run(["gpg", "--batch", "--status-fd", "1", "--verify", str(artifact) + ".asc", str(artifact)], capture_output=True)
            valid = [line.split() for line in verified.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
            if not any(fingerprint.upper() in {line[2].upper(), line[-1].upper()} for line in valid):
                raise RuntimeError("Detached artifact signature has an unexpected signer")
        exported = run(["gpg", "--armor", "--export", fingerprint], capture_output=True)
        (bundle / "publisher-public-key.asc").write_text(exported.stdout, encoding="utf-8")
    checksums = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.suffix in {".deb", ".AppImage", ".exe", ".dmg", ".asc", ".gz"}:
            import hashlib
            checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(bundle).as_posix()}")
    (bundle / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
