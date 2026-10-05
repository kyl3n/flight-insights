#!/usr/bin/env python3
"""Freeze the local Python engine for this OS and verify its actual stdio contract.

Run using the project's locked virtualenv: uv run python scripts/package-engine.py.
Each operating system builds its own sidecar; cross-compilation is deliberately rejected.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import queue
import time

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "src-tauri" / "resources" / "engine"


class SmokeRPC:
    """Bounded synchronous client; jobs run in the engine while this client polls."""
    def __init__(self, binary: Path, directory: Path, environment: dict):
        self.stderr_path = directory / "smoke-stderr.txt"
        self.stderr = self.stderr_path.open("w+", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(binary.resolve()), "--data-dir", str(directory)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True, encoding="utf-8",
            cwd=directory, env=environment, start_new_session=os.name != "nt")
        self.lines = queue.Queue()
        self.sequence = 0

        def read():
            try:
                for line in self.process.stdout:
                    self.lines.put(line)
            finally:
                self.lines.put(None)
        threading.Thread(target=read, daemon=True).start()

    def call(self, method: str, params: dict) -> dict:
        self.sequence += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.sequence,
                                           "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        try:
            line = self.lines.get(timeout=45)
        except queue.Empty as error:
            raise RuntimeError(f"Frozen engine timed out on {method}") from error
        if line is None or len(line.encode("utf-8")) > 4 * 1024 * 1024:
            raise RuntimeError(f"Frozen engine closed or exceeded its frame limit on {method}")
        reply = json.loads(line)
        if reply.get("jsonrpc") != "2.0" or reply.get("id") != self.sequence or "error" in reply:
            diagnostic = self.stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"Frozen engine failed {method}: {reply}\n{diagnostic}")
        return reply["result"]

    def job(self, method: str, params: dict) -> dict:
        job = self.call(method, params)["job"]
        deadline = time.monotonic() + 60
        while job["state"] in {"queued", "running"}:
            if time.monotonic() >= deadline:
                self.call("jobs.cancel", {"job_id": job["id"]})
                raise RuntimeError(f"Synthetic {method} job exceeded its smoke-test deadline")
            time.sleep(0.05)
            job = self.call("jobs.get", {"job_id": job["id"]})["job"]
        if job["state"] != "succeeded":
            raise RuntimeError(f"Frozen {method} job failed: {job}")
        return job["result"]

    def close(self) -> None:
        try:
            self.process.stdin.close()
            try:
                code = self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   capture_output=True, check=False, timeout=15)
                else:
                    import signal
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=15)
                raise RuntimeError("Frozen engine did not shut down after EOF")
            if code != 0:
                self.stderr.seek(0)
                raise RuntimeError(f"Frozen engine exited {code}: {self.stderr.read(4000)}")
            if self.lines.get(timeout=5) is not None:
                raise RuntimeError("Frozen engine emitted unsolicited stdout outside a reply")
        finally:
            self.stderr.close()


def collect_notices() -> Path:
    """Retain original dependency texts; inventory remains an explicit review input."""
    notices = RESOURCES / "notices"
    shutil.rmtree(notices, ignore_errors=True)
    notices.mkdir(parents=True)
    for name in ("pyarrow", "pymavlink", "psutil", "pyinstaller"):
        distribution = importlib.metadata.distribution(name)
        texts = [file for file in distribution.files or [] if ".dist-info/" in str(file)
                 and Path(file).name.lower().startswith(("license", "copying", "notice"))]
        if not texts:
            raise RuntimeError(f"Installed {name} distribution is missing its license texts")
        destination = notices / f"{name}-{distribution.version}"
        destination.mkdir()
        for file in texts:
            shutil.copy2(distribution.locate_file(file), destination / Path(file).name)
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        # Official Windows Python installations keep this beside python.exe.
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Cannot locate the bundled CPython runtime's license")
    shutil.copy2(python_license, notices / "CPython-LICENSE.txt")
    return notices


def frozen_inventory(binary: Path) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(binary))
    entries, modules = [], []
    forbidden = ("pymavlink.dfreader", "pymavlink.mavutil", "pymavlink.generator", "lxml", "fastcrc")
    for name, entry in sorted(archive.toc.items()):
        normalized = name.lower().replace("/", ".").replace("\\", ".")
        if any(normalized.startswith(prefix) for prefix in forbidden) or "iconv" in normalized:
            raise RuntimeError(f"Unexpected excluded dependency in frozen runtime: {name}")
        data = archive.extract(name)
        entries.append({"name": name, "kind": entry[-1], "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
        if entry[-1] == "z":
            modules.extend(sorted(archive.open_embedded_archive(name).toc))
    rejected = [name for name in modules if any(name.lower().startswith(prefix) for prefix in forbidden)]
    if rejected:
        raise RuntimeError(f"Unexpected excluded Python modules: {rejected}")
    required = {"pymavlink.dialects.v20.ardupilotmega", "q22_engine.logs", "q22_engine.jobs"}
    if not required.issubset(modules):
        raise RuntimeError(f"Frozen runtime missing required modules: {sorted(required - set(modules))}")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
    return {"target": host_target(), "python": platform.python_version(),
            "source": {"commit": commit.stdout.strip() if commit.returncode == 0 else None,
                       "dirty": bool(status.stdout), "state": "committed" if commit.returncode == 0 else "unborn"},
            "distributions": {name: importlib.metadata.version(name)
                              for name in ("pyarrow", "pymavlink", "psutil", "pyinstaller")},
            "modules": modules, "entries": entries,
            "excluded": list(forbidden) + ["iconv"],
            "license_review": "Review native entries and notices for each target before distribution; this inventory is not a legal signoff."}


def collect_linux_native_notices() -> None:
    """Preserve distro copyright texts for system libraries copied by PyInstaller."""
    if sys.platform != "linux":
        return
    import ast
    analysis = ROOT / ".engine-build" / "work" / "q22-engine" / "Analysis-00.toc"
    data = ast.literal_eval(analysis.read_text(encoding="utf-8"))
    paths = set()

    def walk(item):
        if isinstance(item, (list, tuple)):
            if len(item) == 3 and item[2] == "BINARY" and isinstance(item[1], str):
                paths.add(item[1])
            else:
                for child in item:
                    walk(child)
    walk(data)
    packages = set()
    for path in sorted(paths):
        for candidate in {path, str(Path(path).resolve())}:
            result = subprocess.run(["dpkg-query", "--search", candidate], capture_output=True,
                                    text=True, encoding="utf-8", check=False)
            if result.returncode == 0:
                packages.update(line.split(": ", 1)[0].split(":", 1)[0] for line in result.stdout.splitlines())
    destination = RESOURCES / "notices" / "linux-system"
    destination.mkdir(exist_ok=True)
    for package in sorted(packages):
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if copyright_file.is_file():
            shutil.copy2(copyright_file, destination / f"{package}.txt")
    common = Path("/usr/share/common-licenses")
    if common.is_dir():
        shutil.copytree(common, destination / "common-licenses", dirs_exist_ok=True)


def host_target() -> str:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "x86_64-pc-windows-msvc"
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "aarch64-apple-darwin"
    if system == "Darwin" and machine == "x86_64":
        return "x86_64-apple-darwin"
    if system == "Linux" and machine == "x86_64":
        return "x86_64-unknown-linux-gnu"
    raise SystemExit(f"Unsupported packaging host: {system} {machine}")


def normalize_linux_runtime(binary: Path) -> None:
    """Prepare the exact RPATH linuxdeploy applies, before recording frozen bytes."""
    if sys.platform != "linux":
        return
    patchelf = shutil.which("patchelf")
    if not patchelf:
        raise RuntimeError("Linux packaging requires patchelf; install the documented native prerequisites or activate scripts/cloud-native-env.sh.")
    # Tauri's pinned linuxdeploy rewrites existing usr/bin ELF files to this
    # RPATH. Preparing it here makes that later operation byte-idempotent and
    # retains the verifier's whole-executable equality check, including bootloader.
    command = [patchelf, "--set-rpath", "$ORIGIN/../lib", str(binary)]
    subprocess.run(command, check=True)
    first = hashlib.sha256(binary.read_bytes()).digest()
    subprocess.run(command, check=True)
    if hashlib.sha256(binary.read_bytes()).digest() != first:
        raise RuntimeError("patchelf does not preserve an already-normalized engine; refuse an unverifiable AppImage build")


def smoke(binary: Path) -> None:
    """Exercise the frozen runtime, persistent process, ID association and EOF exit."""
    with tempfile.TemporaryDirectory(prefix="Q22 packaging Δ ") as directory:
        fixture = Path(directory) / "synthetic params données.param"
        fixture.write_text("# Synthetic packaging fixture; never aircraft evidence.\nATC_RAT_RLL_P,0.1\n", encoding="utf-8")
        label = "Synthetic · données · Перевірка"
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "handshake", "params": {"protocol_version": 1}},
            {"jsonrpc": "2.0", "id": 2, "method": "engine.status", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "sources.capabilities", "params": {}},
            {"jsonrpc": "2.0", "id": 4, "method": "parameters.import", "params": {
                "path": str(fixture), "label": label, "aircraft_id": "synthetic-packaging",
                "board": "synthetic", "firmware": "synthetic", "source_kind": "file_export",
                "observed_at": "2026-10-05T00:00:00Z", "synthetic": True,
            }},
            {"jsonrpc": "2.0", "id": 5, "method": "parameters.list", "params": {}},
            {"jsonrpc": "2.0", "id": 6, "method": "network.inspect", "params": {"sources": []}},
        ]
        # Exclude Python's import path from the child. It must run from its bundled runtime.
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
        rpc = SmokeRPC(binary, Path(directory), environment)
        try:
            replies = [rpc.call(request["method"], request["params"]) for request in requests]
            smoke_logs(rpc, Path(directory))
        finally:
            rpc.close()
        handshake = replies[0]
        if handshake.get("protocol_version") != 1 or handshake.get("product") != "Q22 Flight Lab":
            raise RuntimeError("Frozen engine handshake does not match desktop protocol version 1")
        if replies[1].get("state") != "available":
            raise RuntimeError("Frozen engine did not become available")
        snapshot = replies[3]["snapshot"]
        if snapshot.get("label") != label or snapshot.get("synthetic") is not True:
            raise RuntimeError("Frozen runtime failed UTF-8 parameter import")
        if replies[4]["snapshots"][0]["id"] != snapshot["id"]:
            raise RuntimeError("Frozen runtime did not persist the imported snapshot")
        network = replies[5]
        if (network.get("platform") != platform.system() or not network.get("interfaces")
                or network.get("status") not in {"inspected", "partial"}):
            raise RuntimeError("Frozen runtime failed native interface inspection")
        print("Frozen engine smoke passed: Unicode imports, BIN/LOG/TLOG jobs, Parquet reads, scoped metrics, native interfaces, clean EOF exit.")


def smoke_logs(rpc: SmokeRPC, directory: Path) -> None:
    """Small synthetic real-format fixtures exercise the shipped native dependencies."""
    for format_name in ("bin", "log", "tlog"):
        fixture = directory / f"Synthetic flight données.{format_name}"
        shutil.copy2(ROOT / "fixtures" / "logs" / f"synthetic-flight.{format_name}", fixture)
        log = rpc.job("logs.import", {"path": str(fixture), "label": "Synthetic · données",
                                      "synthetic": True})["log"]
        if (log["synthetic"] is not True or log["record_count"] < 1
                or log["status"] not in {"decoded", "partial"}):
            raise RuntimeError(f"Frozen {format_name} import produced no decoded synthetic evidence")
        if not any(item["id"] == log["id"] for item in rpc.call("logs.list", {})["logs"]):
            raise RuntimeError("Frozen log import was not persisted")
        stream = next(item for item in log["streams"] if item["parameter_observation_count"])
        detail = rpc.call("logs.detail", {"log_id": log["id"], "source_key": stream["source_key"],
                                         "boot_epoch": stream["boot_epoch"], "timeline_limit": 10})
        if not detail["parameter_observations"]:
            raise RuntimeError("Frozen Parquet dataset reader lost parameter observations")
        if format_name in {"bin", "log"}:
            params = {"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
                      "battery_id": "0", "aircraft_id": "synthetic-aircraft", "board": "synthetic-board",
                      "firmware": "synthetic-exact-build", "label": "Synthetic interval", "phase": "synthetic fixture",
                      "interval_start_s": 1, "interval_end_s": 2.5, "max_gap_s": 1.5, "conditions": {}}
            session = rpc.job("logs.session", params)["session"]
            for name, expected in (("mean_power_w", 240), ("energy_wh", .1), ("roll_tracking_rmse_deg", 2)):
                found = session["metrics"][name]["value"]
                if found is None or abs(found - expected) > 1e-6:
                    raise RuntimeError(f"Frozen {format_name} interval has incorrect {name}: {found}")


def macos_minimum(path: Path) -> tuple[int, ...]:
    """Inspect Mach-O load commands without rewriting signed binaries."""
    result = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True,
                            encoding="utf-8", check=True)
    versions = []
    command = None
    for line in result.stdout.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == "cmd":
            command = words[1]
        if len(words) >= 2 and ((command == "LC_BUILD_VERSION" and words[0] == "minos")
                                or (command == "LC_VERSION_MIN_MACOSX" and words[0] == "version")):
            if not re.fullmatch(r"\d+(?:\.\d+){0,2}", words[1]):
                raise RuntimeError(f"Unrecognized macOS deployment version in {path.name}")
            versions.append(tuple(int(value) for value in words[1].split(".")))
    if not versions:
        raise RuntimeError(f"No macOS deployment load command in {path.name}")
    return max(versions)


def audit_macos_runtime(binary: Path, minimum: str) -> dict:
    """Verify the frozen bootloader and every embedded Mach-O against the declared floor."""
    from PyInstaller.archive.readers import CArchiveReader

    declared = tuple(int(value) for value in minimum.split("."))
    declared = declared + (0,) * (3 - len(declared))
    checked = []

    def check(path: Path, name: str) -> None:
        found = macos_minimum(path)
        if found + (0,) * (3 - len(found)) > declared:
            raise RuntimeError(f"{name} needs macOS {'.'.join(map(str, found))}, above declared {minimum}")
        checked.append({"name": name, "minimum": ".".join(map(str, found))})

    check(binary, "q22-engine bootloader")
    archive = CArchiveReader(str(binary))
    macho = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
             b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
    with tempfile.TemporaryDirectory(prefix="q22-macho-audit-") as directory:
        for index, name in enumerate(sorted(archive.toc)):
            data = archive.extract(name)
            if data[:4] not in macho:
                continue
            path = Path(directory) / f"runtime-{index}"
            path.write_bytes(data)
            check(path, name)
    if len(checked) < 2:
        raise RuntimeError("Frozen macOS runtime audit did not discover embedded native libraries")
    return {"declared_minimum": minimum, "checked": checked}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=host_target())
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    if args.target != host_target():
        raise SystemExit("Build on the target operating system and architecture; PyInstaller cannot cross-compile this sidecar.")
    suffix = ".exe" if os.name == "nt" else ""
    destination = ROOT / "src-tauri" / "binaries" / f"q22-engine-{args.target}{suffix}"
    if not args.smoke_only:
        build = ROOT / ".engine-build"
        build.mkdir(parents=True, exist_ok=True)
        entry = build / "entrypoint.py"
        entry.write_text("from q22_engine.__main__ import main\n\nif __name__ == '__main__':\n    main()\n", encoding="utf-8")
        command = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--console", "--name", "q22-engine", "--noupx",
            "--paths", str(ROOT / "engine"),
            "--distpath", str(build / "dist"),
            "--workpath", str(build / "work"),
            "--specpath", str(build),
            "--additional-hooks-dir", str(ROOT / "src-tauri" / "packaging-hooks"),
            "--add-data", f"{collect_notices()}{os.pathsep}q22-notices",
        ]
        for module in ("pymavlink.DFReader", "pymavlink.mavutil", "pymavlink.generator", "lxml", "fastcrc"):
            command.extend(["--exclude-module", module])
        if platform.system() == "Darwin":
            command.extend(["--target-arch", "arm64" if args.target.startswith("aarch64") else "x86_64"])
            if os.environ.get("APPLE_SIGNING_IDENTITY"):
                command.extend(["--codesign-identity", os.environ["APPLE_SIGNING_IDENTITY"]])
        subprocess.run([*command, str(entry)], cwd=ROOT, check=True,
                       env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(build / "cache")})
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build / "dist" / f"q22-engine{suffix}", destination)
        normalize_linux_runtime(destination)
        collect_linux_native_notices()
    inventory = frozen_inventory(destination)
    RESOURCES.mkdir(parents=True, exist_ok=True)
    if args.smoke_only:
        previous = json.loads((RESOURCES / "frozen-inventory.json").read_text(encoding="utf-8"))
        for field in ("target", "python", "distributions", "modules", "entries"):
            if previous[field] != inventory[field]:
                raise RuntimeError(f"Existing frozen inventory disagrees with {field}; rebuild using locked dependencies")
        inventory = previous  # Preserve provenance of the actual build, including its source commit.
    inventory_text = json.dumps(inventory, indent=2) + "\n"
    (RESOURCES / "frozen-inventory.json").write_text(inventory_text, encoding="utf-8")
    destination.with_name(destination.name + ".inventory.json").write_text(inventory_text, encoding="utf-8")
    if platform.system() == "Darwin":
        config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        audit = audit_macos_runtime(destination, config["bundle"]["macOS"]["minimumSystemVersion"])
        destination.with_name(destination.name + ".macos-runtime.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    # Relocation catches launch assumptions about the checkout and executable name.
    with tempfile.TemporaryDirectory(prefix="Q22 binary path Δ ") as directory:
        relocated = Path(directory) / f"q22 engine{suffix}"
        shutil.copy2(destination, relocated)
        smoke(relocated)
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    checksum = destination.with_name(destination.name + ".sha256")
    checksum.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    print(f"Verified {destination.relative_to(ROOT)}")
    print(f"SHA-256 {digest}")


if __name__ == "__main__":
    main()
