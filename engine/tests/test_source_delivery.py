"""Delivery preserves the actual working tree and rejects uncertain provenance."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("q22_package_source", Path(__file__).resolve().parents[2] / "scripts/package-source.py")
SOURCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SOURCE)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "src").mkdir()
    (root / "README.md").write_text("Original\n", encoding="utf-8")
    (root / "src/removed.ts").write_text("deleted\n", encoding="utf-8")
    (root / ".gitignore").write_text(".local/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Source delivery test", "-c",
                    "user.email=source-test@example.invalid", "commit", "-qm", "Test fixture"],
                   cwd=root, check=True)
    return root


def test_source_delivery_retains_dirty_and_new_source_and_exact_hashes(checkout, tmp_path):
    (checkout / "README.md").write_text("Current source\n", encoding="utf-8")
    (checkout / "src/removed.ts").unlink()
    (checkout / "src/new.ts").write_text("new uncommitted source\n", encoding="utf-8")
    (checkout / "src/new.ts").chmod(0o755)
    (checkout / ".local").mkdir()
    (checkout / ".local/private-evidence.txt").write_text("never distribute", encoding="utf-8")
    output = tmp_path / "delivery.zip"
    result = SOURCE.package(checkout, output)
    assert result["working_tree_dirty"] is True
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = json.loads(output.with_suffix(".manifest.json").read_text())
    assert manifest["included_untracked_source"] == ["src/new.ts"]
    assert manifest["source_tree_sha256"] == hashlib.sha256(SOURCE.encoded(manifest["files"])).hexdigest()
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"flight-insights/.gitignore", "flight-insights/README.md",
                                         "flight-insights/src/new.ts", "flight-insights/SOURCE-MANIFEST.json"}
        assert archive.read("flight-insights/SOURCE-MANIFEST.json") == output.with_suffix(".manifest.json").read_bytes()
        for entry in manifest["files"]:
            data = archive.read(f"flight-insights/{entry['path']}")
            assert len(data) == entry["bytes"]
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
            assert archive.getinfo(f"flight-insights/{entry['path']}").external_attr >> 16 == int(entry["mode"], 8)


@pytest.mark.parametrize("name", [
    ".env", "src/client.key", "secrets/credential.txt", "unexpected.txt",
    "src/credentials.json", "docs/local/.netrc", "engine/local/id_rsa",
    "src/config/CREDENTIALS.YAML", "scripts/local/id_ed25519", "src/.git-credentials",
])
def test_source_delivery_refuses_unreviewed_or_sensitive_paths(checkout, tmp_path, name):
    path = checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("do not archive", encoding="utf-8")
    output = tmp_path / "delivery.zip"
    with pytest.raises(ValueError):
        SOURCE.package(checkout, output)
    assert not output.exists()


def test_source_delivery_refuses_symlink_to_external_content(checkout, tmp_path):
    external = tmp_path / "outside.txt"
    external.write_text("outside source", encoding="utf-8")
    try:
        (checkout / "src/outside.ts").symlink_to(external)
    except OSError:
        pytest.skip("Creating a symbolic link is unavailable on this test host")
    with pytest.raises(ValueError, match="symbolic links"):
        SOURCE.package(checkout, tmp_path / "delivery.zip")


def test_source_delivery_rejects_concurrent_edit_without_publishing(checkout, tmp_path, monkeypatch):
    original = SOURCE.add_file

    def edit_after_capture(archive, name, data, mode):
        original(archive, name, data, mode)
        if name == "README.md":
            (checkout / "README.md").write_text("changed during packaging", encoding="utf-8")

    monkeypatch.setattr(SOURCE, "add_file", edit_after_capture)
    with pytest.raises(ValueError, match="changed"):
        SOURCE.package(checkout, tmp_path / "delivery.zip")
    assert not list(tmp_path.glob("delivery*"))


@pytest.mark.parametrize("initially_dirty", [False, True])
def test_source_delivery_rejects_edit_after_earlier_file_verification(checkout, tmp_path, monkeypatch, initially_dirty):
    if initially_dirty:
        (checkout / "README.md").write_text("Already dirty before capture\n", encoding="utf-8")
    original = SOURCE.read_source
    counts = {}

    def edit_after_earlier_verification(root, name, mode_override=None):
        result = original(root, name, mode_override)
        counts[name] = counts.get(name, 0) + 1
        if name == "src/removed.ts" and counts[name] == 2:
            # README has passed both reads; on a dirty checkout, status stays " M".
            (checkout / "README.md").write_text("Changed during final verification\n", encoding="utf-8")
        return result

    monkeypatch.setattr(SOURCE, "read_source", edit_after_earlier_verification)
    with pytest.raises(ValueError, match="changed"):
        SOURCE.package(checkout, tmp_path / "delivery.zip")
    assert not list(tmp_path.glob("delivery*"))


def test_source_delivery_rechecks_source_after_hashing_archive(checkout, tmp_path, monkeypatch):
    original = SOURCE.hashlib.file_digest

    def edit_during_archive_hash(*args, **kwargs):
        result = original(*args, **kwargs)
        (checkout / "src/new.ts").write_text("Arrived after source verification\n", encoding="utf-8")
        return result

    monkeypatch.setattr(SOURCE.hashlib, "file_digest", edit_during_archive_hash)
    with pytest.raises(ValueError, match="changed"):
        SOURCE.package(checkout, tmp_path / "delivery.zip")
    assert not list(tmp_path.glob("delivery*"))


def test_source_delivery_preserves_index_modes_when_filemode_is_disabled(checkout, tmp_path):
    # Windows-style checkout: Git records executable mode, the filesystem does not.
    (checkout / "src/removed.ts").chmod(0o644)
    subprocess.run(["git", "config", "core.filemode", "false"], cwd=checkout, check=True)
    subprocess.run(["git", "update-index", "--chmod=+x", "src/removed.ts"], cwd=checkout, check=True)
    subprocess.run(["git", "-c", "user.name=Source delivery test", "-c",
                    "user.email=source-test@example.invalid", "commit", "-qm", "Executable source"],
                   cwd=checkout, check=True)
    output = tmp_path / "delivery.zip"
    result = SOURCE.package(checkout, output)
    assert result["working_tree_dirty"] is False
    manifest = json.loads(output.with_suffix(".manifest.json").read_text())
    entry = next(item for item in manifest["files"] if item["path"] == "src/removed.ts")
    assert entry["mode"] == "100755"
    assert "Git index modes" in manifest["executable_mode_basis"]
    assert manifest["source_index_sha256"] == hashlib.sha256(
        subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=checkout)).hexdigest()
    with zipfile.ZipFile(output) as archive:
        assert archive.getinfo("flight-insights/src/removed.ts").external_attr >> 16 == 0o100755


@pytest.mark.skipif(os.name == "nt", reason="Windows filesystems do not expose POSIX executable bits")
def test_source_delivery_preserves_uncommitted_executable_mode_when_supported(checkout, tmp_path):
    subprocess.run(["git", "config", "core.filemode", "true"], cwd=checkout, check=True)
    (checkout / "src/removed.ts").chmod(0o755)
    output = tmp_path / "delivery.zip"
    result = SOURCE.package(checkout, output)
    assert result["working_tree_dirty"] is True
    with zipfile.ZipFile(output) as archive:
        assert archive.getinfo("flight-insights/src/removed.ts").external_attr >> 16 == 0o100755


def test_source_delivery_rejects_index_changes_with_unchanged_working_files_and_status(checkout, tmp_path, monkeypatch):
    subprocess.run(["git", "config", "core.filemode", "false"], cwd=checkout, check=True)
    (checkout / "README.md").write_text("Staged version\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
    (checkout / "README.md").write_text("Working version\n", encoding="utf-8")
    original = SOURCE.add_file
    staged_oid = subprocess.check_output(["git", "rev-parse", ":README.md"], cwd=checkout).decode().strip()
    initial_status = subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=checkout)

    def change_only_index(archive, name, data, mode):
        original(archive, name, data, mode)
        if name == "README.md":
            subprocess.run(["git", "update-index", "--cacheinfo", f"100755,{staged_oid},README.md"],
                           cwd=checkout, check=True)
            assert subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=checkout) == initial_status

    monkeypatch.setattr(SOURCE, "add_file", change_only_index)
    with pytest.raises(ValueError, match="changed"):
        SOURCE.package(checkout, tmp_path / "delivery.zip")
    assert not list(tmp_path.glob("delivery*"))


def test_source_delivery_refuses_unmerged_index(checkout, tmp_path):
    oid = subprocess.check_output(["git", "rev-parse", "HEAD:README.md"], cwd=checkout).decode().strip()
    # Remove stage 0, then create the three unmerged stages without changing files.
    records = f"0 {'0' * len(oid)}\tREADME.md\n"
    records += "".join(f"100644 {oid} {stage}\tREADME.md\n" for stage in (1, 2, 3))
    subprocess.run(["git", "update-index", "--index-info"], input=records.encode(), cwd=checkout, check=True)
    with pytest.raises(ValueError, match="unmerged"):
        SOURCE.package(checkout, tmp_path / "delivery.zip")
    assert not list(tmp_path.glob("delivery*"))


def test_source_delivery_preserves_existing_artifact_and_refuses_checkout_output(checkout, tmp_path):
    output = tmp_path / "delivery.zip"
    output.write_bytes(b"prior evidence")
    with pytest.raises(ValueError, match="already exists"):
        SOURCE.package(checkout, output)
    assert output.read_bytes() == b"prior evidence"
    with pytest.raises(ValueError, match="outside"):
        SOURCE.package(checkout, checkout / "delivery.zip")


def test_source_delivery_preserves_sidecar_created_during_publication(checkout, tmp_path, monkeypatch):
    original = SOURCE.os.link
    output = tmp_path / "delivery.zip"
    sidecar = output.with_suffix(".manifest.json")

    def collide_with_existing_sidecar(source, destination):
        if destination == sidecar:
            sidecar.write_bytes(b"prior delivery evidence")
        return original(source, destination)

    monkeypatch.setattr(SOURCE.os, "link", collide_with_existing_sidecar)
    with pytest.raises(FileExistsError):
        SOURCE.package(checkout, output)
    assert sidecar.read_bytes() == b"prior delivery evidence"
    assert not output.exists()
    assert not output.with_suffix(".zip.sha256").exists()
