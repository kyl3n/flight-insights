#!/usr/bin/env python3
"""Measure real-engine import responsiveness using explicitly synthetic BIN data.

This is a development measurement on the current machine, not qualification of
the handoff's reference-laptop target. It never contacts an aircraft or network.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import platform
import queue
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

import psutil


ROOT = Path(__file__).resolve().parents[1]


def write_fixture(path: Path, size_mib: int, *, padding: bool):
    spec = importlib.util.spec_from_file_location("synthetic_formats", ROOT / "fixtures/logs/generate_synthetic.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    preamble = bytearray()
    by_name = {name: (type_id, codes) for type_id, name, codes, _ in fixtures.SCHEMAS}
    for type_id, name, codes, columns in fixtures.SCHEMAS:
        length = 3 + struct.calcsize("<" + "".join(fixtures.WIRE[c] for c in codes))
        preamble += b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", type_id, length, name.encode(), codes.encode(), columns.encode())
    for name, values in fixtures.records():
        if name in ("BAT", "ATT") or (name == "PARM" and values[0] != 0):
            continue
        type_id, codes = by_name[name]
        preamble += b"\xa3\x95" + bytes([type_id]) + struct.pack("<" + "".join(fixtures.WIRE[c] for c in codes),
            *(value.encode() if isinstance(value, str) else value for value in values))
    with path.open("wb") as output:
        output.write(preamble)
        if padding:
            # The 2 GiB cancellation case exercises streaming archive/cancel;
            # this valid but repetitive message is not a performance flight.
            output.write(b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", 136, 203, b"PAD", b"QZZZ", b"TimeUS,Note1,Note2,Note3"))
            packet = b"\xa3\x95\x88" + struct.pack("<Q64s64s64s", 0, b"SYNTHETIC BENCHMARK ONLY", b"NO AIRCRAFT", b"ARCHIVE CANCELLATION CASE")
            block = packet * 4096
            while output.tell() < size_mib * 1024 * 1024:
                output.write(block)
        else:
            second = 1
            while output.tell() < size_mib * 1024 * 1024:
                block = bytearray()
                for _ in range(4096):
                    block += b"\xa3\x95\x85" + struct.pack("<QBffhBB", second * 1_000_000, 0, 24., 10., 2000, 80, 100)
                    second += 1
                output.write(block)


class Client:
    def __init__(self, data: Path):
        self.process = subprocess.Popen([sys.executable, "-m", "q22_engine", "--data-dir", str(data)],
                                        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        self.responses = queue.Queue()
        self.errors = []
        self.sequence = 0
        self.peak_rss = 0
        self.stop = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr = threading.Thread(target=self._stderr, daemon=True)
        self.stderr.start()
        self.sampler = threading.Thread(target=self._sample, daemon=True)
        self.sampler.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except ValueError:
                self.responses.put({"error": {"message": "Engine emitted non-protocol output"}})

    def _stderr(self):
        for line in self.process.stderr:
            self.errors.append(line)
            if len(self.errors) > 100:
                del self.errors[0]

    def _sample(self):
        observed = psutil.Process(self.process.pid)
        while not self.stop.wait(0.025):
            try:
                self.peak_rss = max(self.peak_rss, observed.memory_info().rss)
            except psutil.NoSuchProcess:
                return

    def call(self, method, params=None):
        self.sequence += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}}) + "\n")
        self.process.stdin.flush()
        reply = self.responses.get(timeout=30)
        if reply.get("id") != self.sequence or "error" in reply:
            raise RuntimeError(str(reply))
        return reply["result"]

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.stop.set()
        self.reader.join(timeout=1)
        self.stderr.join(timeout=1)
        self.sampler.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=20)
    parser.add_argument("--cancel-size-mib", type=int, default=2048)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.size_mib <= 256 or not 1 <= args.cancel_size_mib <= 4096:
        parser.error("Full import size must be 1..256 MiB and cancellation size 1..4096 MiB")
    root = Path(tempfile.mkdtemp(prefix="q22-synthetic-benchmark-"))
    client = None
    try:
        regular, large = root / "synthetic-benchmark.bin", root / "synthetic-cancellation.bin"
        write_fixture(regular, args.size_mib, padding=False)
        write_fixture(large, args.cancel_size_mib, padding=True)
        client = Client(root / "data")
        client.call("handshake")
        started = time.monotonic()
        job = client.call("logs.import", {"path": str(regular), "label": "SYNTHETIC throughput benchmark", "synthetic": True})["job"]
        request_s = time.monotonic() - started
        latencies = []
        while True:
            before = time.monotonic()
            client.call("engine.status")
            latencies.append(time.monotonic() - before)
            job = client.call("jobs.get", {"job_id": job["id"]})["job"]
            if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            if time.monotonic() - started > 1800:
                raise RuntimeError("Full import exceeded the 30 minute benchmark limit")
            time.sleep(0.25)
        duration = time.monotonic() - started
        if job["state"] != "succeeded":
            raise RuntimeError(str(job))
        completed_peak = client.peak_rss
        cancel_job = client.call("logs.import", {"path": str(large), "label": "SYNTHETIC archive cancellation", "synthetic": True})["job"]
        time.sleep(0.05)
        cancel_stage = client.call("jobs.get", {"job_id": cancel_job["id"]})["job"].get("progress", {}).get("stage")
        before_cancel = time.monotonic()
        client.call("jobs.cancel", {"job_id": cancel_job["id"]})
        cancel_rpc_s = time.monotonic() - before_cancel
        while True:
            cancel_job = client.call("jobs.get", {"job_id": cancel_job["id"]})["job"]
            if cancel_job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            if time.monotonic() - before_cancel > 30:
                raise RuntimeError("Cancellation exceeded 30 seconds")
            time.sleep(0.01)
        if cancel_job["state"] != "cancelled":
            raise RuntimeError(str(cancel_job))
        report = {"synthetic": True, "platform": platform.platform(), "python": platform.python_version(),
                  "qualification": "Managed-cloud development measurement; not reference-laptop or real-aircraft validation",
                  "full_import": {"source_bytes": regular.stat().st_size, "elapsed_s": duration,
                                  "initial_rpc_s": request_s, "status_rpc_max_s": max(latencies),
                                  "status_rpc_count": len(latencies), "peak_engine_rss_bytes": completed_peak,
                                  "state": job["state"]},
                  "cancellation": {"source_bytes": large.stat().st_size, "acknowledgement_s": cancel_rpc_s,
                                   "terminal_state_s": time.monotonic() - before_cancel, "state": cancel_job["state"],
                                   "last_reported_stage": cancel_stage,
                                   "scope": "Cancellation during import; source size does not establish a completed decode at that size"}}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        if client is not None:
            client.close()
        def writable_remove(function, path, error):
            Path(path).chmod(stat.S_IWRITE | stat.S_IREAD)
            function(path)
        shutil.rmtree(root, onexc=writable_remove)


if __name__ == "__main__":
    main()
