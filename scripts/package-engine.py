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
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import queue
import time
from uuid import uuid4

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
        return self.wait_job(job, method)

    def wait_job(self, job: dict, label: str) -> dict:
        deadline = time.monotonic() + 60
        while job["state"] in {"queued", "running"}:
            if time.monotonic() >= deadline:
                self.call("jobs.cancel", {"job_id": job["id"]})
                raise RuntimeError(f"Synthetic {label} job exceeded its smoke-test deadline")
            time.sleep(0.05)
            job = self.call("jobs.get", {"job_id": job["id"]})["job"]
        if job["state"] != "succeeded":
            raise RuntimeError(f"Frozen {label} job failed: {job}")
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
    required = {"pymavlink.dialects.v20.ardupilotmega", "q22_engine.logs", "q22_engine.jobs",
                "q22_engine.recorder", "q22_engine.collection", "q22_engine.collection_controller",
                "q22_engine.parameter_capture", "q22_engine.acquisition", "q22_engine.reconciliation",
                "q22_engine.insights", "q22_engine.insight_service", "q22_engine.guidance",
                "q22_engine.rtp_diagnostics", "q22_engine.radio_history"}
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
            acquisitions = smoke_acquisition(rpc, Path(directory))
            report = smoke_insights(rpc, Path(directory))
            capture = smoke_recorder(rpc, Path(directory), binary)
        finally:
            rpc.close()
        smoke_recorder_restart(binary, Path(directory), environment, capture, acquisitions, report)
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
        print("Frozen engine smoke passed: Unicode and BIN/LOG/TLOG imports, Parquet metrics, native interfaces, synthetic bidirectional readback/list/download with immutable originals, vibration and power report/review export, receive-only recorder subprocess, verified archives, persistence/recovery, clean EOF worker exit. Synthetic evidence only; no hardware qualification.")


def wait_smoke(check, ready, label: str, timeout: float = 15):
    """Poll real bounded operations; elapsed time alone never establishes success."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if ready(result):
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Frozen recorder smoke timed out waiting for {label}: {result}")
        time.sleep(.05)


def recorder_operation(capture: dict) -> dict:
    return {"operation_id": str(uuid4()),
            "target_profile_version": capture["profile"]["aircraft_profile_version"],
            "applicability_fingerprint": capture["applicability_fingerprint"]}


def start_smoke_capture(rpc: SmokeRPC, profile: dict, binary: Path) -> tuple[dict, dict]:
    import psutil

    preview = rpc.call("session.preview", profile)
    capture = rpc.call("session.prepare_and_watch", {
        **preview["profile"], "operation_id": str(uuid4()),
        "target_profile_version": preview["profile"]["aircraft_profile_version"],
        "applicability_fingerprint": preview["applicability_fingerprint"],
    })["capture_session"]
    status = wait_smoke(lambda: rpc.call("session.status", {"capture_session_id": capture["uuid"]}),
                        lambda value: value.get("recorder", {}).get("state") == "recording",
                        "separate recorder process readiness")
    worker = status["recorder"]
    process = psutil.Process(worker["worker_pid"])
    if (worker["worker_pid"] == worker["parent_pid"] or worker["synthetic"] is not True
            or "--recorder-worker" not in process.cmdline()
            or Path(process.exe()).resolve() != binary.resolve()):
        raise RuntimeError("Recorder did not re-enter the actual frozen executable as a synthetic worker")
    if not worker.get("owns_udp") or not worker.get("active"):
        raise RuntimeError("Frozen recorder did not own its dedicated loopback socket")
    if status["readiness"]["can_claim_complete"] is not False:
        raise RuntimeError("Starting a synthetic observer falsely established complete evidence")
    return capture, worker


def check_worker_exit(worker: dict, port: int) -> None:
    import psutil

    def alive():
        try:
            process = psutil.Process(worker["worker_pid"])
            return (abs(process.create_time() - worker["worker_created"]) < .01
                    and process.is_running() and process.status() != psutil.STATUS_ZOMBIE)
        except psutil.NoSuchProcess:
            return False

    wait_smoke(alive, lambda value: not value, "worker shutdown")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as endpoint:
        if os.name == "nt":
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        endpoint.bind(("127.0.0.1", port))


def smoke_recorder(rpc: SmokeRPC, directory: Path, binary: Path) -> dict:
    """Exercise the bundled worker on loopback; synthetic packets never become flights."""
    from pymavlink.dialects.v20 import ardupilotmega as mavlink

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    profile = {"aircraft_id": "synthetic-packaging-observer",
               "aircraft_profile_version": "synthetic-v1", "connection_profile_version": "loopback-v1",
               "connection": {"local_address": "127.0.0.1", "listen_port": port,
                              "expected_sender": "127.0.0.1", "system_id": 1, "component_id": 1,
                              "feed_origin": "live_forwarded", "synthetic": True},
               "required_sources": ["app_mavlink_capture", "onboard_bin", "preflight_parameters", "postflight_parameters"]}
    capture, worker = start_smoke_capture(rpc, profile, binary)
    source = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    messages = [mavlink.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3),
                mavlink.MAVLink_param_value_message(b"ATC_RAT_RLL_P", .1, 9, 2, 0),
                mavlink.MAVLink_heartbeat_message(2, 3, mavlink.MAV_MODE_FLAG_SAFETY_ARMED, 0, 3, 3),
                mavlink.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3)]
    frames = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.bind(("127.0.0.1", 0))
        sender.settimeout(.25)
        for index, message in enumerate(messages):
            if index == 2:
                # A gap produces both a journal event and a heartbeat for one frame;
                # event identity must not collide with telemetry record identity.
                source.seq = (source.seq + 2) % 256
            frame = message.pack(source)
            source.seq = (source.seq + 1) % 256
            frames.append(frame)
            sender.sendto(frame, ("127.0.0.1", port))
        status = wait_smoke(lambda: rpc.call("session.status", {"capture_session_id": capture["uuid"]}),
                            lambda value: (value.get("recorder", {}).get("valid_frames", 0) >= len(frames)
                                           and not value["recorder"].get("callback_error")
                                           and value["recorder"].get("event_backlog_bytes") == 0
                                           and value["capture_session"].get("rejected_event_count", 0) >= len(frames)),
                            "CRC-validated frames and drained synthetic-event bridge")
        if rpc.call("handshake", {"protocol_version": 1})["protocol_version"] != 1:
            raise RuntimeError("Engine protocol stopped responding during passive recording")
        try:
            packet, _ = sender.recvfrom(65535)
        except socket.timeout:
            pass
        else:
            raise RuntimeError(f"Receive-only recorder unexpectedly sent {len(packet)} bytes")
    if (status["flight_records"] or status["capture_session"]["armed_interval_ids"]
            or status["readiness"]["can_claim_complete"] is not False):
        raise RuntimeError("Synthetic heartbeats became real flight intervals or complete evidence")
    stopped = rpc.call("session.stop", {"capture_session_id": capture["uuid"], **recorder_operation(capture)})
    if stopped.get("archive_job"):
        rpc.wait_job(stopped["archive_job"], "capture archival")
    check_worker_exit(worker, port)
    status = wait_smoke(lambda: rpc.call("session.status", {"capture_session_id": capture["uuid"]}),
                        lambda value: bool(value["capture_session"].get("artifact_sources")),
                        "immutable capture archive")
    capture = status["capture_session"]
    if (capture["status"] != "stopped" or capture.get("archive_state") != "complete"
            or status["flight_records"] or status["readiness"]["can_claim_complete"]):
        raise RuntimeError("Stopping a synthetic capture falsely established a completed flight")
    recording = directory / "captures" / capture["uuid"]
    artifacts = json.loads((recording / "artifacts.json").read_text(encoding="utf-8"))["artifacts"]
    if {item["role"] for item in artifacts} != {"raw_udp_capture", "app_mavlink_capture", "recorder_journal"}:
        raise RuntimeError("Frozen recorder omitted raw UDP, TLOG, or journal evidence")
    for artifact in artifacts:
        path = Path(artifact["path"])
        payload = path.read_bytes()
        if (len(payload) != artifact["bytes"] or hashlib.sha256(payload).hexdigest() != artifact["sha256"]
                or artifact["complete"] is not True):
            raise RuntimeError("Frozen recorder artifact bytes do not match their finalized integrity receipt")
        if artifact["role"] == "raw_udp_capture" and any(frame not in payload for frame in frames):
            raise RuntimeError("Raw UDP capture lost the transmitted synthetic frames")
        if artifact["role"] == "recorder_journal":
            events = [json.loads(line) for line in payload.splitlines()]
            if (not events or any(event.get("origin") != "synthetic" or event.get("live") is not False for event in events)
                    or not any(event.get("kind") == "heartbeat" and event.get("armed") is True for event in events)
                    or not any(event.get("reason") == "sequence_discontinuity" for event in events)):
                raise RuntimeError("Recorder journal lost synthetic provenance or the captured armed heartbeat")
        if artifact["role"] == "app_mavlink_capture":
            log = rpc.job("logs.import", {"path": str(path), "label": "Synthetic packaged live capture", "synthetic": True})["log"]
            if log["synthetic"] is not True or log["record_count"] != len(frames):
                raise RuntimeError("Frozen decoder could not read every frame from its own synthetic TLOG")
    roles = {item["role"] for item in capture["artifact_sources"]}
    if not {"raw_udp_capture", "app_mavlink_capture", "recorder_journal"}.issubset(roles):
        raise RuntimeError("Completed capture evidence was not attached to its durable session")
    expected = {(item["role"], item["sha256"], item["bytes"]) for item in artifacts}
    attached = {(item["role"], item["source"]["sha256"], item["source"]["byte_size"])
                for item in capture["artifact_sources"]}
    if attached != expected:
        raise RuntimeError("Archived capture source roles and bytes differ from the recorder's finalized receipts")
    for item in capture["artifact_sources"]:
        original = item["source"]
        path = directory / "raw" / "sha256" / original["sha256"]
        payload = path.read_bytes()
        if (hashlib.sha256(payload).hexdigest() != original["sha256"]
                or len(payload) != original["byte_size"]
                or original["acquisition_method"] != "app_owned_passive_udp_capture"):
            raise RuntimeError("Managed recorder original was not preserved with matching bytes and provenance")
    return capture


def smoke_recorder_restart(binary: Path, directory: Path, environment: dict, capture: dict,
                           acquisitions: list[dict], report: dict) -> None:
    """Restart retains evidence; EOF terminates a worker instead of silently resuming it."""
    rpc = SmokeRPC(binary, directory, environment)
    worker = None
    try:
        for acquisition in acquisitions:
            restored_acquisition = rpc.call("acquisition.detail", {"acquisition_id": acquisition["id"]})["acquisition"]
            if restored_acquisition != acquisition:
                raise RuntimeError("Frozen restart lost the immutable acquisition result")
        if rpc.call("insights.detail", {"report_id": report["id"]})["report"] != report:
            raise RuntimeError("Frozen restart lost the persisted diagnostic report")
        status = rpc.call("session.status", {"capture_session_id": capture["uuid"]})
        restored = status["capture_session"]
        if (restored["status"] != "stopped" or not restored["artifact_sources"]
                or restored["artifact_sources"] != capture["artifact_sources"]
                or restored["profile"]["connection"]["synthetic"] is not True
                or status.get("recorder", {}).get("active") or status["flight_records"]
                or status["readiness"]["can_claim_complete"]):
            raise RuntimeError("Frozen engine restart lost synthetic evidence or automatically resumed recording")
        # Leave a second explicitly started observer running: EOF must stop its subprocess.
        restarted, worker = start_smoke_capture(rpc, capture["profile"], binary)
    finally:
        rpc.close()
    if worker is not None:
        check_worker_exit(worker, capture["profile"]["connection"]["listen_port"])
        final = json.loads((directory / "captures" / restarted["uuid"] / "status.json").read_text(encoding="utf-8"))
        if final["state"] != "stopped" or final["active"] or final["synthetic"] is not True:
            raise RuntimeError("Engine EOF did not finalize its active synthetic recorder")
        recovery = SmokeRPC(binary, directory, environment)
        try:
            status = recovery.call("session.status", {"capture_session_id": restarted["uuid"]})
            interrupted = status["capture_session"]
            if (interrupted["status"] != "interrupted" or status.get("recorder", {}).get("active")
                    or status["flight_records"] or status["readiness"]["can_claim_complete"]):
                raise RuntimeError("Restart inferred a complete flight or resumed an EOF-interrupted observer")
            stopped = recovery.call("session.stop", {"capture_session_id": interrupted["uuid"],
                                                     **recorder_operation(interrupted)})
            if not stopped.get("archive_job"):
                raise RuntimeError("Explicit interrupted-capture recovery did not queue preserved source archival")
            recovery.wait_job(stopped["archive_job"], "interrupted capture recovery")
            restored = recovery.call("session.status", {"capture_session_id": interrupted["uuid"]})["capture_session"]
            if restored.get("archive_state") != "complete" or not restored["artifact_sources"]:
                raise RuntimeError("Explicit recovery lost the EOF-finalized recorder evidence")
        finally:
            recovery.close()


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


class AcquisitionPeer:
    """Actual loopback MAVLink peer with explicitly synthetic, finite evidence."""

    def __init__(self, log_bytes: bytes):
        from pymavlink.dialects.v20 import ardupilotmega as mavlink
        self.mavlink, self.log_bytes = mavlink, log_bytes
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(.01)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.target = reservation.getsockname()
        self.connection = {"local_address": self.target[0], "local_port": self.target[1],
                           "remote_address": "127.0.0.1", "remote_port": self.socket.getsockname()[1],
                           "system_id": 1, "component_id": 1, "expected_uid": "123",
                           "feed_origin": "direct_bidirectional"}
        self.encoder = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
        self.decoder = mavlink.MAVLink(None)
        self.requests, self.error = [], None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def send(self, message):
        payload = message.pack(self.encoder)
        self.encoder.seq = (self.encoder.seq + 1) % 256
        self.socket.sendto(payload, self.target)

    def parameter(self, index):
        name, value, kind = [(b"TEST_A", 1.5, 9), (b"TEST_B", 7.0, 6)][index]
        self.send(self.mavlink.MAVLink_param_value_message(name, value, kind, 2, index))

    def run(self):
        mav = self.mavlink
        started, last_status = time.monotonic(), 0
        try:
            while not self.stopped.is_set():
                now = time.monotonic()
                if now - last_status >= .04:
                    self.send(mav.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3))
                    self.send(mav.MAVLink_system_time_message(1700000000000000, 1000 + int((now - started) * 1000)))
                    last_status = now
                try:
                    payload, endpoint = self.socket.recvfrom(65535)
                except (socket.timeout, ConnectionResetError):
                    continue
                if endpoint != self.target:
                    raise RuntimeError("Unexpected endpoint contacted the synthetic acquisition peer")
                for message in self.decoder.parse_buffer(payload) or []:
                    self.requests.append(message)
                    name = message.get_type()
                    if name == "COMMAND_LONG":
                        if (message.command != 512 or message.param1 != 148 or message.confirmation != 0
                                or any(getattr(message, f"param{i}") != 0 for i in range(2, 8))):
                            raise RuntimeError("Frozen engine emitted an unapproved command")
                        self.send(mav.MAVLink_autopilot_version_message(131072, 1, 0, 0, 1,
                            [0] * 8, [0] * 8, [0] * 8, 1, 1, 123))
                    elif name == "PARAM_REQUEST_LIST":
                        self.parameter(0)  # Exercise a real missing-index retry.
                    elif name == "PARAM_REQUEST_READ":
                        if message.param_index != 1:
                            raise RuntimeError("Unexpected synthetic parameter retry index")
                        self.parameter(1)
                    elif name == "LOG_REQUEST_LIST":
                        if message.start <= 7 <= message.end:
                            self.send(mav.MAVLink_log_entry_message(7, 1, 7, 1700000000, len(self.log_bytes)))
                    elif name == "LOG_REQUEST_DATA":
                        if message.id != 7:
                            raise RuntimeError("Frozen engine guessed an unlisted log ID")
                        if message.ofs >= len(self.log_bytes):
                            self.send(mav.MAVLink_log_data_message(7, message.ofs, 0, [0] * 90))
                        else:
                            end = min(len(self.log_bytes), message.ofs + message.count)
                            for offset in range(message.ofs, end, 90):
                                part = self.log_bytes[offset:min(offset + 90, end)]
                                self.send(mav.MAVLink_log_data_message(7, offset, len(part), list(part) + [0] * (90 - len(part))))
                    elif name != "LOG_REQUEST_END":
                        raise RuntimeError(f"Unexpected outbound MAVLink message: {name}")
        except BaseException as error:
            self.error = error

    def close(self):
        self.stopped.set()
        self.thread.join(2)
        self.socket.close()
        if self.thread.is_alive() or self.error:
            raise RuntimeError(f"Synthetic acquisition peer failed: {self.error}")


def smoke_acquisition(rpc: SmokeRPC, directory: Path) -> list[dict]:
    """Exercise the frozen sender only against an explicitly synthetic loopback peer."""
    payload = (ROOT / "fixtures/logs/synthetic-flight.bin").read_bytes()
    peer = AcquisitionPeer(payload)
    try:
        limits = {"timeout_s": 15, "bootstrap_timeout_s": 3, "heartbeat_timeout_s": 1,
                  "boot_timeout_s": 1, "identity_timeout_s": 3, "retry_wait_s": .2,
                  "request_interval_s": .02, "settle_s": .1, "retries": 2}
        preview = rpc.call("acquisition.preview", {"connection": peer.connection, "limits": limits})
        base = {"connection": peer.connection, "limits": limits, "aircraft_id": "synthetic-packaging-readback",
                "synthetic": True, "applicability_fingerprint": preview["applicability_fingerprint"]}
        arguments = base | {"operation_id": str(uuid4()), "role": "preflight"}
        result = rpc.job("acquisition.parameters.capture", arguments)
        parameters = result["acquisition"]
        capture = parameters.get("parameter_capture", {})
        if (parameters["state"] != "complete" or parameters["synthetic"] is not True
                or capture.get("completeness") != "complete_for_scope" or capture.get("decoded_count") != 2
                or capture.get("atomic") is not False or capture.get("flight_time_applicability") != "unknown"
                or capture.get("missing_index_retry_examples") != [1]):
            raise RuntimeError(f"Frozen parameter readback did not preserve scope/completeness: {parameters}")
        count = len(peer.requests)
        repeated = rpc.job("acquisition.parameters.capture", arguments)
        if not repeated["operation_replayed"] or repeated["acquisition"]["id"] != parameters["id"] or len(peer.requests) != count:
            raise RuntimeError("Retrying an operation ID resent acquisition requests")
        listing = rpc.job("acquisition.logs.list", base | {"operation_id": str(uuid4()),
                          "confirm_log_transfer_side_effects": True})["acquisition"]
        if (listing["state"] != "complete" or not listing.get("listing_complete")
                or [entry["log_id"] for entry in listing["entries"]] != [7]
                or listing["entries"][0]["remote_filename"] is not None):
            raise RuntimeError(f"Frozen log inventory did not retain actual remote IDs: {listing}")
        downloaded = rpc.job("acquisition.logs.download", base | {"operation_id": str(uuid4()),
            "confirm_log_transfer_side_effects": True, "listing_id": listing["id"], "log_id": 7})["acquisition"]
        transfer = downloaded.get("download", {})
        evidence = downloaded["transport_evidence"]
        expected_hash = hashlib.sha256(payload).hexdigest()
        if (downloaded["state"] != "complete" or transfer.get("transfer_complete") is not True
                or transfer.get("sha256") != expected_hash or transfer.get("received_bytes") != len(payload)
                or transfer.get("logger_closed") != "unknown" or downloaded["association_state"] != "provisional"
                or evidence["logging_resumption_verified"] is not False or not evidence["log_request_end_sent"]
                or not evidence["recovery_warning"]):
            raise RuntimeError(f"Frozen transfer validation/provisional evidence failed: {downloaded}")
        acquisitions = [parameters, listing, downloaded]
        for acquisition in acquisitions:
            if rpc.call("acquisition.detail", {"acquisition_id": acquisition["id"]})["acquisition"] != acquisition:
                raise RuntimeError("Frozen acquisition detail differs from its terminal result")
            for source in acquisition["source_refs"]:
                path = directory / "raw/sha256" / source["sha256"]
                raw = path.read_bytes()
                if (hashlib.sha256(raw).hexdigest() != source["sha256"] or len(raw) != source["byte_size"]
                        or source["acquisition_method"] != "app_owned_readonly_mavlink_acquisition"):
                    raise RuntimeError("Frozen acquisition lost immutable original bytes/provenance")
        page = rpc.call("acquisition.list", {"limit": 2})
        remaining = rpc.call("acquisition.list", {"limit": 2, "offset": page["next_offset"]})
        if {item["id"] for item in page["acquisitions"] + remaining["acquisitions"]} != {item["id"] for item in acquisitions}:
            raise RuntimeError("Frozen acquisition catalog pagination lost a result")
        source = next(item for item in downloaded["source_refs"] if item["role"] == "onboard_log_candidate")
        log = rpc.job("logs.import", {"path": str(directory / "raw/sha256" / source["sha256"]),
            "label": "Synthetic transferred BIN candidate", "synthetic": True})["log"]
        if log["source"]["sha256"] != expected_hash or not log["record_count"] or log["synthetic"] is not True:
            raise RuntimeError("Frozen decoder could not content-index the downloaded synthetic BIN")
        return acquisitions
    finally:
        peer.close()


def smoke_insights(rpc: SmokeRPC, directory: Path) -> dict:
    fixture = directory / "Synthetic vibration données.log"
    shutil.copy2(ROOT / "fixtures/logs/synthetic-insights.log", fixture)
    log = rpc.job("logs.import", {"path": str(fixture), "label": "Synthetic vibration and power", "synthetic": True})["log"]
    report = rpc.job("insights.report", {"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
        "interval_start_s": 1, "interval_end_s": 4, "max_gap_s": 1.5, "battery_id": "0",
        "operating_profile": {"aircraft_mass_kg": 10, "payload_mass_kg": 4, "rotor_count": 4,
            "propeller_diameter_m": .5, "rotor_layout": "non_overlapping", "battery_cell_count": 6,
            "declared_firmware": "Copter-4.7.1", "flight_phase": "hover", "actuator_protocol": "pwm",
            "motor_output_channels": [1, 2], "output_low_us": 1000, "output_high_us": 2000},
        "thresholds": {"roll_error_deg": 1}})["report"]
    for name, expected in (("mean_power_w", 1160 / 3), ("energy_wh", 1160 / 3600),
                           ("roll_tracking_rmse_deg", math.sqrt(53 / 9))):
        found = report["metrics"][name]["value"]
        if found is None or abs(found - expected) > 1e-6:
            raise RuntimeError(f"Frozen insight integration oracle failed for {name}: {found}")
    clipping = report["vibration"]["clipping"]
    if (clipping["observed_deltas"] != {"0:Clip": 5} or clipping["reset_count"] != 1
            or not any(row["details"]["peak_mps2"] == 70 and row["evidence"]["time_s"] == 3
                       for row in report["vibration"]["excursions"])
            or report["operating_profile"]["derived_context"]["payload_mass_fraction"] != .4
            or report["parameter_evidence"]["attribution_allowed"] is not False
            or report["spectrum"]["status"] != "unavailable"
            or report["guidance"]["firmware_compatibility"]["exact_build_validated"] is not False
            or not report["scope"]["synthetic"]):
        raise RuntimeError("Frozen insight report lost scoped observations or uncertainty gates")
    if not any(row["id"] == report["id"] for row in rpc.call("insights.list", {})["reports"]):
        raise RuntimeError("Frozen insight report was not persisted")
    preview = rpc.call("insights.review_packet", {"report_id": report["id"]})
    packet = preview["packet"]
    if (str(directory) in json.dumps(packet) or not packet["scope"]["synthetic"]
            or not packet["findings"] or not packet["guidance"]["entries"][0]["references"][0]["sha256"]
            or any(item["proposed_values"] is not None for item in packet["recommendations"])):
        raise RuntimeError("Review packet lost evidence/citations or its conservative projection")
    exported = rpc.call("insights.export_review", {"report_id": report["id"], "folder": str(directory)})
    path = Path(exported["path"])
    raw = path.read_bytes()
    if (path.parent != directory or json.loads(raw) != packet or len(raw) != preview["byte_size"]
            or len(raw) != exported["byte_size"] or hashlib.sha256(raw).hexdigest() != exported["sha256"]):
        raise RuntimeError("Local review export differs from its exact preview or integrity receipt")
    return report


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
