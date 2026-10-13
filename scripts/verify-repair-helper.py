#!/usr/bin/env python3
"""Exercise only the unprivileged diagnostic executable; never install/elevate it."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent

def run(command, **kwargs):
    response = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
        check=True, timeout=10, **kwargs)
    if len(response.stdout.encode()) > 64 * 1024:
        raise RuntimeError("Diagnostic helper response exceeds its bound")
    return response.stdout

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    suffix = ".exe" if sys.platform == "win32" else ""
    binary = ROOT / "native-repair/target" / args.target / "release" / f"q22-network-helper{suffix}"
    capabilities = json.loads(run([str(binary), "--capabilities"]))
    if (capabilities.get("protocol_version") != 1 or capabilities.get("state") != "diagnosis_only"
            or any(capabilities.get(name) is not False for name in ("qualified", "signed_helper", "automatic_apply", "supervised_rollback"))):
        raise RuntimeError("Unqualified diagnostic helper incorrectly advertises mutation or deployment qualification")
    replies = run([str(binary), "--stdio"], input='{"method":"capabilities","params":{}}\n{"method":"recovery","params":{}}\n').splitlines()
    if len(replies) != 2 or json.loads(replies[0]).get("result") != capabilities or json.loads(replies[1]).get("error") != "diagnosis_only":
        raise RuntimeError("Diagnostic helper did not preserve its no-mutation contract")
    with binary.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"schema_version": 1, "target": args.target, "binary_sha256": checksum,
        "capabilities": capabilities, "bounded_stdio_verified": True,
        "unqualified_recovery_refused": True, "installed": False, "elevated": False,
        "native_network_writes_tested": False,
        "limitation": "Library transaction/failure tests use injected host state; this diagnostic executable does not qualify native privileged repair."}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print("Unprivileged diagnostic helper verified; native repair remains unqualified.")

if __name__ == "__main__":
    main()
