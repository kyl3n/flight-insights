#!/usr/bin/env python3
"""Archive the exact local source tree, including uncommitted source additions.

Creates a ZIP, a per-file source manifest and a SHA-256 receipt outside the
checkout. Excludes ignored build/runtime data and refuses sensitive or unfamiliar
paths. Checks for changes during capture, but does not lock the checkout: pause
source writers first. This does not build, test, sign, publish, or claim that an
installer matches this source snapshot.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRECTORIES = {".github", "docs", "engine", "fixtures", "native-repair",
                      "schemas", "scripts", "src", "src-tauri", "tests"}
SOURCE_FILES = {".gitattributes", ".gitignore", ".nvmrc", ".python-version", "README.md",
                "LICENSE", "LICENSE.md", "LICENSE.txt", "index.html", "package-lock.json",
                "package.json", "playwright.config.ts", "pyproject.toml", "tsconfig.json",
                "uv.lock", "vite.config.ts"}
PRIVATE_PARTS = {".git", ".aws", ".ssh", ".gnupg", ".azure", ".kube", "secrets",
                 "credentials", "node_modules", ".venv", "__pycache__", ".local", "target"}
PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"}
PRIVATE_NAMES = {".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
                 "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                 "credentials.json", "credentials.yaml", "credentials.yml",
                 "credentials.toml", "credentials.ini", "credentials.xml", "credentials.txt",
                 "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml", "secrets.ini"}
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
ARCHIVE_ROOT = "flight-insights"
MANIFEST_NAME = "SOURCE-MANIFEST.json"


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)


def checkout_state(root: Path) -> dict:
    """Record index entries, not volatile index cache/stat metadata."""
    index = git(root, "ls-files", "--stage", "-z")
    modes = {}
    for record in index.decode("utf-8").split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, _, stage = metadata.split()
        if stage != "0":
            raise ValueError(f"Resolve unmerged Git index entries before source delivery: {name}")
        if mode not in {"100644", "100755"}:
            raise ValueError(f"Source delivery requires regular files in the Git index: {name}")
        modes[name] = int(mode, 8)
    return {
        "commit": git(root, "rev-parse", "HEAD").decode().strip(),
        "status": git(root, "status", "--porcelain", "--untracked-files=all", "-z"),
        "index": index,
        "modes": modes,
        "filemode": git(root, "config", "--bool", "--default", "true", "--get", "core.filemode").strip() == b"true",
    }


def source_names(root: Path) -> list[str]:
    names = sorted(set(git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
                       .decode("utf-8").strip("\0").split("\0")) - {""})
    result = []
    for name in names:
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name
                or any(part.lower() in PRIVATE_PARTS or part.lower() in PRIVATE_NAMES
                       or part.lower().startswith(".env")
                       for part in path.parts)
                or path.suffix.lower() in PRIVATE_SUFFIXES):
            raise ValueError(f"Refusing private, generated or unsafe source path: {name}")
        if path.parts[0] not in SOURCE_DIRECTORIES and name not in SOURCE_FILES:
            raise ValueError(f"Review an unfamiliar top-level path before source delivery: {name}")
        full = root / path
        if full.is_symlink():
            raise ValueError(f"Source delivery does not follow symbolic links: {name}")
        if not full.exists():
            # A tracked deletion is part of the current working tree.
            continue
        if not full.is_file() or not full.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Source path is not a regular file inside the checkout: {name}")
        result.append(name)
    if not result:
        raise ValueError("No source files were found")
    return result


def file_identity(item: os.stat_result) -> tuple[int, ...]:
    return (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns)


def path_handle_identity(item: os.stat_result) -> tuple[int, ...]:
    # CPython 3.12 on Windows reports creation time as stat(path).st_ctime,
    # but change time as fstat(fd).st_ctime. Path stat also derives executable
    # bits from the filename; fstat does not. Compare those fields only within
    # the same API below, retaining every full identity check independently.
    return (item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode), item.st_size, item.st_mtime_ns)


def read_source(root: Path, name: str, mode_override: int | None = None) -> tuple[bytes, dict, tuple[int, ...]]:
    path = root / name
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
        raise ValueError(f"Source file is not regular or exceeds the 64 MiB limit: {name}")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if path_handle_identity(opened) != path_handle_identity(before):
            raise ValueError(f"Source path changed before capture: {name}")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(data) > MAX_FILE_BYTES or len(data) != opened.st_size
            or file_identity(opened) != file_identity(after)
            or file_identity(before) != file_identity(path.stat(follow_symlinks=False))):
        raise ValueError(f"Source file changed during capture: {name}")
    mode = mode_override if mode_override is not None else (0o100755 if before.st_mode & 0o111 else 0o100644)
    return data, {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                  "mode": format(mode, "06o")}, file_identity(before)


def verify_checkout(root: Path, names: list[str], state: dict) -> None:
    if names != source_names(root) or state != checkout_state(root):
        raise ValueError("Source checkout changed during capture; freeze edits and retry")


def encoded(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def add_file(archive: zipfile.ZipFile, name: str, data: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{name}", (1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def package(root: Path, output: Path) -> dict:
    root = root.resolve()
    output = output.resolve()
    if output.suffix.lower() != ".zip" or output.is_relative_to(root):
        raise ValueError("Choose a .zip output path outside the source checkout")
    destinations = [output, output.with_suffix(".manifest.json"), output.with_suffix(".zip.sha256")]
    if any(path.exists() for path in destinations):
        raise ValueError("Delivery output already exists; choose a new name to preserve prior artifacts")
    names = source_names(root)
    state = checkout_state(root)
    commit, status = state["commit"], state["status"]
    mode_overrides = state["modes"] if not state["filemode"] else {}
    untracked = set(names) - state["modes"].keys()
    output.parent.mkdir(parents=True, exist_ok=True)
    published: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="q22-source-", dir=output.parent) as directory:
        work = Path(directory)
        entries = []
        identities = {}
        total = 0
        with zipfile.ZipFile(work / "source.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                data, entry, identity = read_source(root, name, mode_overrides.get(name))
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("Source delivery exceeds the 256 MiB bound")
                entries.append(entry)
                identities[name] = identity
                add_file(archive, name, data, int(entry["mode"], 8))
            manifest = {
                "schema_version": 1,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_commit": commit,
                "source_index_sha256": hashlib.sha256(state["index"]).hexdigest(),
                "executable_mode_basis": ("Tracked files use Git index modes because core.filemode=false; untracked files use filesystem modes."
                                          if not state["filemode"] else "Filesystem executable modes."),
                "working_tree_dirty": bool(status),
                "source_tree_sha256": hashlib.sha256(encoded(entries)).hexdigest(),
                "source_tree_hash_basis": "SHA-256 of the canonical UTF-8 JSON files array, sorted by path, with two-space indentation, sorted keys and trailing newline",
                "file_count": len(entries),
                "total_source_bytes": total,
                "included_untracked_source": sorted(untracked.intersection(names)),
                "files": entries,
                "limits": ["Source archive only; no installer, test pass, signature or hardware qualification is established.",
                           "Ignored build outputs, local evidence, dependencies and Git metadata are excluded.",
                           "Consistency checks do not lock the checkout or provide an atomic filesystem snapshot; pause source writers before capture.",
                           "An installer requires a separate recorded build and artifact verification for this source snapshot."],
            }
            manifest_bytes = encoded(manifest)
            add_file(archive, MANIFEST_NAME, manifest_bytes, 0o100644)
        verify_checkout(root, names, state)
        for expected in entries:
            name = expected["path"]
            _, actual, identity = read_source(root, name, mode_overrides.get(name))
            if expected != actual or identities[name] != identity:
                raise ValueError(f"Source file changed after capture: {expected['path']}")
        with zipfile.ZipFile(work / "source.zip") as archive:
            if archive.testzip() is not None:
                raise ValueError("Source ZIP failed its integrity check")
            if archive.read(f"{ARCHIVE_ROOT}/{MANIFEST_NAME}") != manifest_bytes:
                raise ValueError("Source ZIP manifest does not match its receipt")
        (work / "manifest.json").write_bytes(manifest_bytes)
        with (work / "source.zip").open("rb") as stream:
            archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        (work / "source.sha256").write_text(f"{archive_hash}  {output.name}\n", encoding="utf-8")
        # Catch changes to earlier files while later files/ZIP receipts were checked.
        # Writers must still be paused: these checks cannot make publication atomic
        # with respect to a process that does not cooperate with source freezing.
        verify_checkout(root, names, state)
        for name, expected_identity in identities.items():
            if file_identity((root / name).stat(follow_symlinks=False)) != expected_identity:
                raise ValueError(f"Source file changed before publication: {name}")
        try:
            for source, destination in zip((work / "source.zip", work / "manifest.json", work / "source.sha256"), destinations):
                os.link(source, destination)
                published.append(destination)
        except OSError:
            for path in published:
                path.unlink()
            raise
    return {"archive": str(output), "manifest": str(destinations[1]), "sha256": archive_hash,
            "source_tree_sha256": manifest["source_tree_sha256"], "file_count": len(entries),
            "source_commit": commit, "working_tree_dirty": bool(status)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = package(ROOT, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Source delivery failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
