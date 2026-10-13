#!/usr/bin/env python3
"""Inventory locked frontend production dependencies and the target Rust build graph.

Preserves available upstream notices with hashes. A build-graph package is not a
claim that every byte of that crate is linked into the final executable. Native
bundled libraries/Python remain covered by the separate frozen inventory.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "src-tauri/resources/engine/application-dependencies"

def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def command(args):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", check=True, timeout=120).stdout

def collect(name, version, directory, declared_license, scope, license_file=None):
    key = re.sub(r"[^A-Za-z0-9_.+-]", "_", f"{scope}-{name}-{version}")
    candidates = []
    for path in directory.iterdir():
        if path.name.lower().startswith(("license", "licence", "copying", "notice", "copyright")):
            candidates.extend(path.rglob("*") if path.is_dir() else [path])
    if license_file:
        path = (directory / license_file).resolve()
        if path.is_file() and path.is_relative_to(directory.resolve()):
            candidates.append(path)
    records = []
    for original in sorted(set(path.resolve() for path in candidates if path.is_file())):
        if not original.is_relative_to(directory.resolve()) or original.stat().st_size > 16 * 1024**2:
            raise RuntimeError(f"Unbounded or external dependency notice: {name}")
        relative = original.relative_to(directory.resolve())
        destination = OUTPUT / "notices" / key / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        records.append({"path": destination.relative_to(OUTPUT).as_posix(),
                        "sha256": digest(destination), "bytes": destination.stat().st_size})
    provenance = None
    if not records:
        fallback_root = ROOT / "scripts/dependency-notices"
        catalog = read(fallback_root / "manifest.json")
        provenance = catalog.get(f"{name}@{version}")
        if provenance:
            vcs = read(directory / ".cargo_vcs_info.json")
            if vcs.get("git", {}).get("sha1") != provenance["crate_vcs_commit"]:
                raise RuntimeError(f"Preserved upstream notices do not match the installed crate revision: {name}")
            for item in provenance["files"]:
                source = fallback_root / item["path"]
                if digest(source) != item["sha256"]:
                    raise RuntimeError(f"Preserved upstream notice checksum changed: {name}")
                destination = OUTPUT / "notices" / key / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                records.append({"path": destination.relative_to(OUTPUT).as_posix(),
                                "sha256": digest(destination), "bytes": destination.stat().st_size})
    if len(records) > 256:
        raise RuntimeError(f"Unbounded notice count: {name}")
    return {"name": name, "version": version, "scope": scope,
            "declared_license": declared_license, "notices": records,
            "preserved_upstream_notice_provenance": provenance,
            "notice_status": "texts_preserved" if records else "missing_upstream_text_requires_review"}

def frontend():
    modules = (ROOT / "node_modules").resolve()
    pending = [(ROOT, name) for name in read(ROOT / "package.json")["dependencies"]]
    seen, packages = set(), []
    while pending:
        consumer, name = pending.pop()
        directory = None
        for parent in (consumer, *consumer.parents):
            if not parent.is_relative_to(ROOT): break
            candidate = parent / "node_modules" / name
            if (candidate / "package.json").is_file():
                directory = candidate.resolve(); break
        if directory is None or not directory.is_relative_to(modules):
            raise RuntimeError(f"Locked production package is unavailable: {name}")
        if directory in seen: continue
        seen.add(directory)
        metadata = read(directory / "package.json")
        record = collect(metadata["name"], metadata["version"], directory,
            metadata.get("license"), "frontend_production_graph")
        record["package_manifest_sha256"] = digest(directory / "package.json")
        packages.append(record)
        pending.extend((directory, dependency) for dependency in metadata.get("dependencies", {}))
    return packages

def rust(target, manifest_path="src-tauri/Cargo.toml", scope="rust_target_build_graph"):
    metadata = json.loads(command(["cargo", "metadata", "--manifest-path", manifest_path,
        "--locked", "--offline", "--format-version", "1", "--filter-platform", target]))
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    packages = {package["id"]: package for package in metadata["packages"]}
    primary = metadata["resolve"]["root"]
    pending, seen = [primary], set()
    while pending:
        identifier = pending.pop()
        if identifier in seen: continue
        seen.add(identifier)
        pending.extend(dep["pkg"] for dep in nodes[identifier]["deps"]
            if any(kind["kind"] != "dev" for kind in dep["dep_kinds"]))
    records = []
    for identifier in sorted(seen - {primary}):
        package = packages[identifier]
        manifest = Path(package["manifest_path"])
        record = collect(package["name"], package["version"], manifest.parent,
            package.get("license"), scope, package.get("license_file"))
        record.update(package_manifest_sha256=digest(manifest),
            cargo_target_kinds=sorted({kind for target in package["targets"] for kind in target["kind"]}),
            enabled_features=nodes[identifier]["features"])
        if any(license_name in (package.get("license") or "") for license_name in ("MPL-", "LGPL-", "GPL-", "EPL-", "CDDL-")):
            # Preserve the exact registry source archive, checked against the
            # committed Cargo lock checksum, for review/rebuilding this graph.
            locked = tomllib.loads((ROOT / Path(manifest_path).with_name("Cargo.lock")).read_text())
            matches = [entry for entry in locked["package"] if entry["name"] == package["name"] and entry["version"] == package["version"]]
            source = manifest.parents[3] / "cache" / manifest.parents[1].name / f"{package['name']}-{package['version']}.crate"
            if len(matches) != 1 or digest(source) != matches[0].get("checksum"):
                raise RuntimeError(f"Exact licensed source archive cannot be verified: {package['name']}")
            destination = OUTPUT / "sources" / source.name
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, destination)
            record["corresponding_registry_source"] = {"path": destination.relative_to(OUTPUT).as_posix(),
                "sha256": digest(destination), "unchanged_upstream_archive": True}
        records.append(record)
    return records, packages[primary].get("license")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="Native target; defaults to rustc's host target")
    parser.add_argument("--fetch-locked", action="store_true",
        help="Fetch checksum-locked Cargo dependencies before offline inventory; never installs system packages")
    args = parser.parse_args()
    if args.target is None:
        args.target = next((line.removeprefix("host: ") for line in command(["rustc", "-vV"]).splitlines()
                            if line.startswith("host: ")), None)
    if args.target not in {"x86_64-pc-windows-msvc", "x86_64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-apple-darwin"}:
        parser.error("Unsupported native target")
    if args.fetch_locked:
        for manifest in ("src-tauri/Cargo.toml", "native-repair/Cargo.toml"):
            command(["cargo", "fetch", "--locked", "--manifest-path", manifest, "--target", args.target])
    if OUTPUT.is_symlink(): raise RuntimeError("Generated dependency output must not be a symlink")
    shutil.rmtree(OUTPUT, ignore_errors=True)
    OUTPUT.mkdir(parents=True)
    frontend_records = frontend()
    rust_records, project_license = rust(args.target)
    helper_records, _ = rust(args.target, "native-repair/Cargo.toml", "rust_diagnostic_helper_build_graph")
    packages = sorted(frontend_records + rust_records + helper_records, key=lambda item: (item["scope"], item["name"], item["version"]))
    unresolved = [{"name": item["name"], "version": item["version"], "scope": item["scope"]}
                  for item in packages if not item["notices"]]
    report = {"schema_version": 1, "target": args.target, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": command(["git", "rev-parse", "HEAD"]).strip(),
        "source_dirty": bool(command(["git", "status", "--porcelain"]).strip()),
        "lockfile_sha256": {name: digest(ROOT / name) for name in ("package-lock.json", "src-tauri/Cargo.lock", "native-repair/Cargo.lock", "uv.lock")},
        "packages": packages, "package_count": len(packages), "unresolved_notice_texts": unresolved,
        "application_license_declared": project_license,
        "limitations": ["Dependency declarations and installed notice texts are evidence for review, not legal sign-off.",
            "Rust graph includes build scripts/proc macros; not every listed crate is necessarily shipped as executable code.",
            "Frontend graph lists production dependencies; bundler tree shaking may omit portions.",
            "Frozen Python/native entries and their notices are covered by frozen-inventory.json.",
            "Host WebView/system dependencies are not redistributed by this inventory; AppImage and offline WebView2 payload terms need separate final-artifact review."]}
    (OUTPUT / "manifest.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"target": args.target, "packages": len(packages), "unresolved_notice_texts": unresolved,
        "application_license_declared": project_license, "manifest": str(OUTPUT / "manifest.json")}, indent=2))

if __name__ == "__main__":
    main()
