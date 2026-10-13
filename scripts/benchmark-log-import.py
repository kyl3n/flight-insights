#!/usr/bin/env python3
"""Measure real-engine import responsiveness using explicitly synthetic BIN data.

This is a development measurement on the current machine, not qualification of
the handoff's reference-laptop target. It never contacts an aircraft or network.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
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


def source_identity():
    paths = sorted((ROOT / "engine/q22_engine").glob("*.py")) + [
        Path(__file__).resolve(), ROOT / "fixtures/logs/generate_synthetic.py", ROOT / "uv.lock"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def write_fixture(path: Path, size_mib: int, *, padding: bool, record_mix="bat-pad"):
    spec = importlib.util.spec_from_file_location("synthetic_formats", ROOT / "fixtures/logs/generate_synthetic.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    preamble = bytearray()
    counts = Counter()
    by_name = {name: (type_id, codes) for type_id, name, codes, _ in fixtures.SCHEMAS}
    for type_id, name, codes, columns in fixtures.SCHEMAS:
        length = 3 + struct.calcsize("<" + "".join(fixtures.WIRE[c] for c in codes))
        preamble += b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", type_id, length, name.encode(), codes.encode(), columns.encode())
        counts["FMT"] += 1
    for name, values in fixtures.records():
        if name in ("BAT", "ATT") or (name == "PARM" and values[0] != 0):
            continue
        type_id, codes = by_name[name]
        preamble += b"\xa3\x95" + bytes([type_id]) + struct.pack("<" + "".join(fixtures.WIRE[c] for c in codes),
            *(value.encode() if isinstance(value, str) else value for value in values))
        counts[name] += 1
    if padding or record_mix == "bat-pad":
        preamble += b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", 136, 203, b"PAD", b"QZZZ", b"TimeUS,Note1,Note2,Note3")
        counts["FMT"] += 1
    digest = hashlib.sha256()
    with path.open("wb") as output:
        def write(data):
            output.write(data)
            digest.update(data)
        write(preamble)
        if padding:
            # The 2 GiB cancellation case exercises streaming archive/cancel;
            # this valid but repetitive message is not a performance flight.
            packet = b"\xa3\x95\x88" + struct.pack("<Q64s64s64s", 0, b"SYNTHETIC BENCHMARK ONLY", b"NO AIRCRAFT", b"ARCHIVE CANCELLATION CASE")
            while output.tell() < size_mib * 1024 * 1024:
                records = min(4096, (size_mib * 1024 * 1024 - output.tell() + len(packet) - 1) // len(packet))
                write(packet * records)
                counts["PAD"] += records
        else:
            second = 1
            pair_bytes = 227 if record_mix == "bat-pad" else 24
            while output.tell() < size_mib * 1024 * 1024:
                block = bytearray()
                records = min(4096, (size_mib * 1024 * 1024 - output.tell() + pair_bytes - 1) // pair_bytes)
                for _ in range(records):
                    block += b"\xa3\x95\x85" + struct.pack("<QBffhBB", second * 1_000_000, 0, 24., 10., 2000, 80, 100)
                    if record_mix == "bat-pad":
                        # A complete valid unknown-message record: the decoder
                        # must normalize and persist this payload, not skip it.
                        block += b"\xa3\x95\x88" + struct.pack("<Q64s64s64s", second * 1_000_000,
                            b"SYNTHETIC BENCHMARK ONLY", b"NO AIRCRAFT", b"WIDE RECORD DECODE AND PERSISTENCE")
                    second += 1
                write(block)
                counts["BAT"] += records
                if record_mix == "bat-pad": counts["PAD"] += records
    return {"requested_source_bytes": size_mib * 1024 * 1024, "source_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(), "record_mix": "pad-only" if padding else record_mix,
            "dataflash_record_counts": dict(counts), "expected_normalized_events": sum(counts.values()),
            "scope": ("Archive cancellation fixture; not a completed decode qualification" if padding else
                "Synthetic complete BIN decode and normalized persistence; BAT24/PAD203 alternating records, not broad real-log or DSP qualification"
                if record_mix == "bat-pad" else "Dense synthetic BAT24 records; byte size may exceed the unchanged 20 million normalized-event bound")}


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


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def job_evidence(job):
    return {key: job[key] for key in ("id", "method", "state", "created_at", "started_at",
        "finished_at", "progress", "error", "cancel_requested") if key in job}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=20)
    parser.add_argument("--record-mix", choices=("bat-pad", "dense-bat"), default="bat-pad",
        help="BAT24/PAD203 alternating complete records or dense BAT24-only; the event limit is unchanged")
    parser.add_argument("--cancel-size-mib", type=int, default=2048)
    parser.add_argument("--full-timeout-s", type=int, default=10800)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.size_mib <= 2048 or not 1 <= args.cancel_size_mib <= 4096 or not 60 <= args.full_timeout_s <= 14400:
        parser.error("Full import size must be 1..2048 MiB, cancellation size 1..4096 MiB, and timeout 60..14400 seconds")
    if args.report.exists():
        parser.error("Report exists; choose a new path to preserve previous evidence")
    report = {"schema_version": 2, "synthetic": True, "state": "preparing",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python": platform.python_version(),
        "source_hashes_at_start": source_identity(),
        "qualification": "Managed-cloud development measurement; not reference-laptop, real-aircraft, arbitrary 2 GiB log or DSP validation",
        "normalized_event_limit_unchanged": 20_000_000,
        "requested_record_mix": args.record_mix,
        "full_import": {}, "cancellation": {"state": "not_started"}}
    write_report(args.report, report)
    root = Path(tempfile.mkdtemp(prefix="q22-synthetic-benchmark-"))
    client = None
    try:
        regular, large = root / "synthetic-benchmark.bin", root / "synthetic-cancellation.bin"
        report["full_import"]["fixture"] = write_fixture(regular, args.size_mib, padding=False, record_mix=args.record_mix)
        report["cancellation"]["fixture"] = write_fixture(large, args.cancel_size_mib, padding=True)
        report["state"] = "running"
        write_report(args.report, report)
        client = Client(root / "data")
        client.call("handshake")
        started = time.monotonic()
        job = client.call("logs.import", {"path": str(regular), "label": "SYNTHETIC throughput benchmark", "synthetic": True})["job"]
        full = report["full_import"]
        full["initial_rpc_s"] = time.monotonic() - started
        full["source_bytes"] = regular.stat().st_size
        latencies = []
        checkpoint = time.monotonic()
        while True:
            before = time.monotonic()
            client.call("engine.status")
            latencies.append(time.monotonic() - before)
            job = client.call("jobs.get", {"job_id": job["id"]})["job"]
            full.update(elapsed_s=time.monotonic() - started, status_rpc_max_s=max(latencies),
                status_rpc_count=len(latencies), peak_engine_rss_bytes=client.peak_rss,
                state=job["state"], job=job_evidence(job))
            if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            if time.monotonic() - checkpoint >= 15:
                write_report(args.report, report)
                checkpoint = time.monotonic()
            if time.monotonic() - started > args.full_timeout_s:
                raise RuntimeError("Full import exceeded its explicit benchmark time limit")
            time.sleep(0.25)
        write_report(args.report, report)
        if job["state"] != "succeeded":
            raise RuntimeError(f"Full import terminated as {job['state']}: {job.get('error')}")
        log = job["result"]["log"]
        full["normalized_record_count"] = log["record_count"]
        full["event_counts"] = log["event_counts"]
        full["archived_source_sha256"] = log["source"]["sha256"]
        full["decoded_log_status"] = log["status"]
        full["warnings"] = log["warnings"]
        # All completed shards are inspected before cancellation can add other
        # outputs. Metadata row totals verify persisted normalized record counts.
        import pyarrow.parquet as parquet
        shards = sorted((root / "data/derived/logs").glob("*/part-*.parquet"))
        full["persisted_shard_count"] = len(shards)
        full["persisted_parquet_bytes"] = sum(path.stat().st_size for path in shards)
        full["persisted_parquet_rows"] = sum(parquet.read_metadata(path).num_rows for path in shards)
        if (log["record_count"] != full["fixture"]["expected_normalized_events"]
                or full["persisted_parquet_rows"] != log["record_count"]
                or full["archived_source_sha256"] != full["fixture"]["sha256"]):
            raise RuntimeError("Completed import record count, persisted rows or archived source hash disagrees with the generated fixture")
        full["complete_decode_and_normalized_persistence_verified"] = True
        write_report(args.report, report)
        cancel_job = client.call("logs.import", {"path": str(large), "label": "SYNTHETIC archive cancellation", "synthetic": True})["job"]
        time.sleep(0.05)
        cancel_stage = client.call("jobs.get", {"job_id": cancel_job["id"]})["job"].get("progress", {}).get("stage")
        before_cancel = time.monotonic()
        client.call("jobs.cancel", {"job_id": cancel_job["id"]})
        cancel_rpc_s = time.monotonic() - before_cancel
        while True:
            cancel_job = client.call("jobs.get", {"job_id": cancel_job["id"]})["job"]
            report["cancellation"].update(source_bytes=large.stat().st_size, acknowledgement_s=cancel_rpc_s,
                terminal_state_s=time.monotonic() - before_cancel, state=cancel_job["state"],
                last_reported_stage=cancel_stage, job=job_evidence(cancel_job),
                scope="Cancellation during import; source size does not establish a completed decode at that size")
            if cancel_job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            if time.monotonic() - before_cancel > 30:
                raise RuntimeError("Cancellation exceeded 30 seconds")
            time.sleep(0.01)
        if cancel_job["state"] != "cancelled":
            raise RuntimeError(f"Cancellation terminated as {cancel_job['state']}")
        report["state"] = "succeeded"
    except BaseException as error:
        report["state"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)[:4000]}
        raise
    finally:
        report["source_hashes_at_finish"] = source_identity()
        report["source_changed_during_measurement"] = report["source_hashes_at_start"] != report["source_hashes_at_finish"]
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        if client is not None:
            report["engine_stderr_tail"] = "".join(client.errors)[-16000:]
        # Preserve outcome, job error, source/fixture fingerprints and telemetry
        # before shutting down or deleting any temporary benchmark data.
        write_report(args.report, report)
        try:
            if client is not None:
                client.close()
            def writable_remove(function, path, error):
                Path(path).chmod(stat.S_IWRITE | stat.S_IREAD)
                function(path)
            shutil.rmtree(root, onexc=writable_remove)
            report["temporary_data_cleanup"] = "completed"
        except Exception as cleanup_error:
            report["temporary_data_cleanup"] = {"state": "failed", "message": str(cleanup_error)[:1000]}
            raise
        finally:
            write_report(args.report, report)
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
