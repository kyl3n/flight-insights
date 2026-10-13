#!/usr/bin/env python3
"""Read-only GitHub release configuration inspection. Never reads secret values.

Permission failures are unknown prerequisites, not absent credentials. This does
not create environments/tags/runners, dispatch workflows, or sign/publish code.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

REQUIREMENTS = {
    "windows": {"variables": ["Q22_WINDOWS_CERT_THUMBPRINT", "Q22_WINDOWS_TIMESTAMP_URL"],
        "credential_store": "Authorized Authenticode private key in the runner OS certificate store; no key material belongs in this report.",
        "runner_labels": ["self-hosted", "Windows", "X64", "q22-release"]},
    "linux": {"variables": ["Q22_GPG_FINGERPRINT"],
        "credential_store": "Selected publisher key available through the runner GPG agent/keyring.",
        "runner_labels": ["self-hosted", "Linux", "X64", "q22-release"]},
    "macos_optional": {"variables": ["APPLE_SIGNING_IDENTITY", "APPLE_TEAM_ID", "Q22_APPLE_NOTARY_PROFILE"],
        "credential_store": "Developer ID identity/private key and notarytool keychain profile on native architecture runners.",
        "runner_labels": ["self-hosted", "macOS", "q22-release"]},
}

def api(repository, endpoint, projection):
    process = subprocess.run(["gh", "api", f"repos/{repository}/{endpoint}", "--jq", projection],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    if process.returncode:
        code = re.search(r"HTTP (\d{3})", process.stderr)
        return {"state": "unavailable", "http_status": int(code[1]) if code else None,
                "reason": "GitHub metadata inspection was denied or unavailable; no credentials were requested."}
    return {"state": "observed", "data": json.loads(process.stdout)}

def inspect(repository):
    evidence = {
        "environments": api(repository, "environments", "{total_count,environments:[.environments[]|{name,protection_rules,deployment_branch_policy}]}"),
        "rulesets": api(repository, "rulesets?includes_parents=true", "[.[]|{id,name,target,enforcement,source_type}]"),
        "secret_names": api(repository, "actions/secrets", "{total_count,names:[.secrets[].name]}"),
        "variable_names": api(repository, "actions/variables", "{total_count,names:[.variables[].name]}"),
        "runners": api(repository, "actions/runners", "{total_count,runners:[.runners[]|{name,os,status,busy,labels:[.labels[].name]}]}"),
    }
    blockers = []
    environment = evidence["environments"]
    if environment["state"] != "observed":
        blockers.append("Protected release-signing environment readiness is unknown.")
    else:
        selected = [item for item in environment["data"]["environments"] if item["name"] == "release-signing"]
        if not selected:
            blockers.append("The release-signing environment does not exist.")
        elif not any(rule.get("type") == "required_reviewers" and rule.get("reviewers")
                     for rule in selected[0]["protection_rules"]):
            blockers.append("The release-signing environment has no required reviewer protection.")
    rulesets = evidence["rulesets"]
    if rulesets["state"] != "observed":
        blockers.append("Protected release-tag rules cannot be inspected.")
    elif not any(item["target"] == "tag" and item["enforcement"] == "active" for item in rulesets["data"]):
        blockers.append("No active release-tag ruleset is configured; a v-prefixed name alone is not protection.")
    else:
        evidence["tag_ruleset_details"] = [api(repository, f"rulesets/{item['id']}",
            "{id,name,target,enforcement,conditions,rules,bypass_actors}") for item in rulesets["data"]
            if item["target"] == "tag" and item["enforcement"] == "active"]
        blockers.append("Review tag rule patterns, mutation/deletion restrictions and bypass actors for the exact candidate tag.")
    if evidence["variable_names"]["state"] != "observed":
        blockers.append("Signing variable-name presence is unknown because metadata access is unavailable.")
    else:
        names = set(evidence["variable_names"]["data"]["names"])
        # Environment variables can override repository variables. Do not call
        # them missing without inspecting that scope as well.
        scoped = api(repository, "environments/release-signing/variables", "{total_count,names:[.variables[].name]}")
        evidence["environment_variable_names"] = scoped
        if scoped["state"] == "observed":
            names.update(scoped["data"]["names"])
            missing = sorted({name for target in ("windows", "linux") for name in REQUIREMENTS[target]["variables"]} - names)
            if missing: blockers.append("Required signing configuration names absent: " + ", ".join(missing))
        else:
            blockers.append("Release environment variable-name presence remains unknown.")
    if evidence["runners"]["state"] != "observed":
        blockers.append("Provisioned signing runner availability is unknown because metadata access is unavailable.")
    else:
        runners = evidence["runners"]["data"]["runners"]
        for target in ("windows", "linux"):
            if not any(set(REQUIREMENTS[target]["runner_labels"]) <= set(item["labels"])
                       and item["status"] == "online" for item in runners):
                blockers.append(f"No online {target} runner with the required signing labels was observed.")
    blockers.append("Private-key availability and signing/notarization ability require the protected runner preflight; API name inspection cannot establish them.")
    return {"schema_version": 1, "repository": repository,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(), "mode": "read_only_metadata",
        "required_targets": ["windows", "linux"], "optional_targets": ["macos"],
        "signing_ready": False, "blockers": blockers, "requirements": REQUIREMENTS,
        "evidence": evidence, "secrets_values_read": False, "remote_mutations": False}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="kyl3n/flight-insights")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Supply an owner/repository identifier")
    report = inspect(args.repository)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.report), "signing_ready": report["signing_ready"], "blockers": report["blockers"]}, indent=2))

if __name__ == "__main__":
    main()
