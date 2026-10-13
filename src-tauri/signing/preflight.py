#!/usr/bin/env python3
"""Check a protected signing runner without importing keys or producing signatures."""
from __future__ import annotations
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

def command(args, **kwargs):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30, **kwargs)
    if result.returncode:
        # Some credential tooling echoes identity metadata. Never expose its
        # captured output in a shared report or workflow annotation.
        raise RuntimeError(f"Read-only signing prerequisite failed: {Path(args[0]).name}")
    return result.stdout

def required(name, pattern=None):
    value = os.environ.get(name, "")
    if not value or (pattern and re.fullmatch(pattern, value) is None):
        raise RuntimeError(f"Missing or invalid signing configuration: {name}")
    return value

def inspect():
    if (os.environ.get("GITHUB_REF_TYPE") != "tag" or os.environ.get("GITHUB_REF_PROTECTED") != "true"
            or not re.fullmatch(r"refs/tags/v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", required("GITHUB_REF"))):
        raise RuntimeError("Signing requires an administrator-protected reviewed release tag.")
    commit = command(["git", "rev-parse", "HEAD"]).strip()
    if commit != required("GITHUB_SHA", r"[a-f0-9]{40}") or command(["git", "status", "--porcelain"]).strip():
        raise RuntimeError("Signing requires a clean checkout of the exact reviewed workflow commit.")
    tools = {"node": "v24.19.0", "uv": "uv 0.12.19"}
    for tool, expected in tools.items():
        if command([tool, "--version"]).strip() != expected:
            raise RuntimeError(f"Provision the pinned {tool} version before signing.")
    if sys.version_info[:3] != (3, 12, 14) or not command(["rustc", "--version"]).startswith("rustc 1.99.0 "):
        raise RuntimeError("Provision pinned Python 3.12.14 and Rust 1.99.0 before signing.")
    checked = []
    if sys.platform == "win32":
        required("Q22_WINDOWS_CERT_THUMBPRINT", r"[A-Fa-f0-9]{40}")
        required("Q22_WINDOWS_TIMESTAMP_URL", r"https://[^\s]+")
        if not shutil.which("signtool"):
            raise RuntimeError("Provision the Windows SDK signtool on the protected runner.")
        script = ("$c=Get-Item -LiteralPath ('Cert:\\CurrentUser\\My\\'+$env:Q22_WINDOWS_CERT_THUMBPRINT) -ErrorAction Stop;"
                  "if(-not $c.HasPrivateKey -or $c.NotAfter -le (Get-Date) -or $c.NotBefore -gt (Get-Date)){throw 'Certificate unavailable'};"
                  "if(-not ($c.EnhancedKeyUsageList.ObjectId.Value -contains '1.3.6.1.5.5.7.3.3')){throw 'Code signing EKU missing'}")
        command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        checked = ["certificate_present", "private_key_binding", "validity_period", "code_signing_eku", "timestamp_url_configuration"]
    elif sys.platform == "darwin":
        identity = required("APPLE_SIGNING_IDENTITY")
        team = required("APPLE_TEAM_ID", r"[A-Z0-9]{10}")
        required("Q22_APPLE_NOTARY_PROFILE")
        identities = command(["security", "find-identity", "-v", "-p", "codesigning"])
        if f'"{identity}"' not in identities or f"({team})" not in identity:
            raise RuntimeError("Developer ID identity/team is not available in the runner keychain.")
        command(["xcrun", "--find", "notarytool"])
        checked = ["codesigning_identity_available", "team_matches", "notarytool_available", "notary_profile_name_configured"]
    elif sys.platform == "linux":
        fingerprint = required("Q22_GPG_FINGERPRINT", r"[A-Fa-f0-9]{40,64}")
        keys = command(["gpg", "--batch", "--with-colons", "--list-secret-keys", fingerprint])
        fingerprints = [line.split(":")[9].upper() for line in keys.splitlines() if line.startswith("fpr:")]
        if fingerprint.upper() not in fingerprints or not any(line.startswith("sec:") for line in keys.splitlines()):
            raise RuntimeError("Configured publisher key is not available in the runner GPG keyring.")
        checked = ["configured_gpg_secret_key_present"]
    else:
        raise RuntimeError("Unsupported signing runner OS.")
    return {"schema_version": 1, "source_commit": commit, "platform": sys.platform,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(), "runner_preflight": "passed",
        "read_only_checks": checked, "secrets_exported": False, "signed_artifacts_created": False,
        "limitations": ["Presence does not prove a hardware key can sign or a timestamp/notary service will accept the candidate.",
            "Final publisher verification, signatures and applicable notarization remain mandatory after building."]}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = inspect()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print("Protected runner signing prerequisites inspected; final signature verification remains required.")

if __name__ == "__main__":
    main()
