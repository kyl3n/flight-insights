"""Benchmark failure evidence survives temporary-input cleanup."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def test_failed_import_persists_error_and_exact_fixture_identity_before_cleanup(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("q22_import_benchmark", root / "scripts/benchmark-log-import.py")
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    data = tmp_path / "temporary inputs"
    data.mkdir()
    monkeypatch.setattr(benchmark.tempfile, "mkdtemp", lambda **kwargs: str(data))

    class FailedEngine:
        peak_rss = 12345
        errors = ["synthetic importer failure fixture\n"]

        def __init__(self, directory): pass
        def close(self): pass
        def call(self, method, params=None):
            if method == "logs.import":
                return {"job": {"id": "synthetic-failed-job", "state": "running"}}
            if method == "jobs.get":
                return {"job": {"id": "synthetic-failed-job", "state": "failed",
                    "progress": {"stage": "decoding", "completed": 1024},
                    "error": {"message": "Synthetic event-bound rejection"}}}
            return {}

    monkeypatch.setattr(benchmark, "Client", FailedEngine)
    output = tmp_path / "retained failure.json"
    monkeypatch.setattr(sys, "argv", ["benchmark-log-import.py", "--size-mib", "1",
        "--cancel-size-mib", "1", "--report", str(output)])
    with pytest.raises(RuntimeError, match="event-bound rejection"):
        benchmark.main()
    report = json.loads(output.read_text())
    assert report["state"] == "failed"
    assert report["full_import"]["job"]["error"]["message"] == "Synthetic event-bound rejection"
    assert len(report["full_import"]["fixture"]["sha256"]) == 64
    assert report["full_import"]["fixture"]["dataflash_record_counts"]["PAD"] > 0
    assert report["source_hashes_at_start"]
    assert report["source_hashes_at_finish"]
    assert report["temporary_data_cleanup"] == "completed"
    assert not data.exists()
