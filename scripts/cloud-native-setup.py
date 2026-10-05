#!/usr/bin/env python3
"""Install trusted native build tools in /workspace/shared without sudo.

This is for the managed Debian 13 x86-64 machine, not end-user installation.
Requires Python 3.11+, curl, apt, dpkg-deb, gcc and Debian archive keyrings.
Run when no native builds are active; source cloud-native-env.sh afterwards.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tomllib


ROOT = Path(os.environ.get("Q22_TOOLCHAIN_DIR", "/workspace/shared/q22-toolchain"))
SYSROOT = Path(os.environ.get("Q22_SYSROOT_DIR", "/workspace/shared/q22-sysroot"))
REPO = Path(__file__).resolve().parents[1]
RUST_VERSION = tomllib.loads((REPO / "src-tauri/rust-toolchain.toml").read_text())["toolchain"]["channel"]
TARGET = "x86_64-unknown-linux-gnu"
PACKAGES = ["libwebkit2gtk-4.1-dev", "libayatana-appindicator3-dev", "librsvg2-dev", "patchelf", "libssl-dev", "xvfb", "xauth"]


def run(*args: str, **kwargs: object) -> None:
    subprocess.run(args, check=True, **kwargs)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url: str, path: Path) -> None:
    temp = path.with_suffix(path.suffix + ".partial")
    run("curl", "-fsSL", "--proto", "=https", "--tlsv1.2", url, "-o", str(temp))
    temp.replace(path)


def install_rust() -> None:
    downloads = ROOT / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    manifest = downloads / f"channel-rust-{RUST_VERSION}.toml"
    checksum = manifest.with_suffix(".toml.sha256")
    url = f"https://static.rust-lang.org/dist/{manifest.name}"
    download(url, manifest)
    download(url + ".sha256", checksum)
    if digest(manifest) != checksum.read_text().split()[0]:
        raise RuntimeError("Rust manifest SHA-256 verification failed")
    data = tomllib.loads(manifest.read_text())
    names = ["rustc", "cargo", "rust-std", "rustfmt-preview", "clippy-preview"]

    def artifact(name: str) -> tuple[Path, str]:
        entry = data["pkg"][name]["target"][TARGET]
        path = downloads / entry["xz_url"].rsplit("/", 1)[1]
        if not path.is_file() or digest(path) != entry["xz_hash"]:
            download(entry["xz_url"], path)
        if digest(path) != entry["xz_hash"]:
            raise RuntimeError(f"Rust component SHA-256 verification failed: {name}")
        print(f"Verified {path.name}", flush=True)
        return path, entry["xz_hash"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        artifacts = list(pool.map(artifact, names))
    receipt = {path.name: sha for path, sha in artifacts}
    marker = ROOT / "rust-components.json"
    if marker.is_file() and json.loads(marker.read_text()) == receipt:
        if all((ROOT / "rust/bin" / tool).is_file() for tool in ("rustc", "cargo", "rustfmt", "cargo-clippy")):
            return
    extracted = ROOT / "extracted"
    extracted.mkdir(exist_ok=True)
    for path, _ in artifacts:
        with tarfile.open(path) as archive:
            folder = archive.getmembers()[0].name.split("/")[0]
            archive.extractall(extracted, filter="data")
        run("bash", str(extracted / folder / "install.sh"), f"--prefix={ROOT / 'rust'}", "--disable-ldconfig")
        shutil.rmtree(extracted / folder)
    marker.write_text(json.dumps(receipt, indent=2) + "\n")


def install_native() -> None:
    apt = ROOT / "apt"
    for folder in ("lists/partial", "archives/partial", "empty"):
        (apt / folder).mkdir(parents=True, exist_ok=True)
    (apt / "sources.list").write_text(
        "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie main\n"
        "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie-updates main\n"
        "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://security.debian.org/debian-security trixie-security main\n"
    )
    shutil.copyfile("/var/lib/dpkg/status", apt / "status")
    config = {
        "Dir::Etc::main": "-", "Dir::Etc::parts": str(apt / "empty"),
        "Dir::Etc::sourcelist": str(apt / "sources.list"),
        "Dir::Etc::sourceparts": str(apt / "empty"), "Dir::State": str(apt),
        "Dir::State::status": str(apt / "status"), "Dir::State::lists": str(apt / "lists"),
        "Dir::Cache": str(apt), "Dir::Cache::archives": str(apt / "archives"),
        "Dir::Log": str(apt), "Acquire::Languages": "none", "APT::Install-Recommends": "false",
    }
    (apt / "apt.conf").write_text("".join(f'{key} "{value}";\n' for key, value in config.items()))
    process_env = {**os.environ, "APT_CONFIG": str(apt / "apt.conf")}
    run("/usr/bin/apt-get", "update", env=process_env)
    # APT verifies Release signatures and package hashes; no installation or maintainer scripts run.
    run("/usr/bin/apt-get", "--download-only", "--no-install-recommends", "-y", "install", *PACKAGES, env=process_env)
    archives = sorted((apt / "archives").glob("*.deb"))
    receipt = {path.name: digest(path) for path in archives}
    marker = ROOT / "native-packages.json"
    if not marker.is_file() or json.loads(marker.read_text()) != receipt or not SYSROOT.is_dir():
        SYSROOT.mkdir(parents=True, exist_ok=True)
        # Clear only installer-created host links before dpkg replaces local development files.
        links_file = ROOT / "host-library-links.json"
        if links_file.is_file():
            for item in json.loads(links_file.read_text()):
                link = SYSROOT / item
                if link.is_symlink():
                    link.unlink()
        for archive in archives:
            run("dpkg-deb", "-x", str(archive), str(SYSROOT))
        links = []
        # APT omitted runtime packages already present on the host. Resolve their development stubs.
        for link in (SYSROOT / "usr/lib/x86_64-linux-gnu").glob("*.so*"):
            if link.is_symlink() and not link.exists():
                target = Path(os.path.normpath(link.parent / os.readlink(link)))
                if target.is_relative_to(SYSROOT):
                    relative = target.relative_to(SYSROOT)
                    host = Path("/") / relative
                    if host.exists():
                        target.symlink_to(host)
                        links.append(str(relative))
        links_file.write_text(json.dumps(links, indent=2) + "\n")
        marker.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Native dependency cache: {len(archives)} verified packages", flush=True)


if __name__ == "__main__":
    system = platform.freedesktop_os_release()
    if platform.machine() != "x86_64" or system.get("ID") != "debian" or system.get("VERSION_ID") != "13":
        raise SystemExit("This helper requires the managed Debian 13 x86-64 machine; use Tauri's platform prerequisites elsewhere.")
    ROOT.mkdir(parents=True, exist_ok=True)
    install_rust()
    install_native()
    print("Source scripts/cloud-native-env.sh before running Cargo or Tauri.")
