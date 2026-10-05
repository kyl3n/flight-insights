"""Persistent background jobs, real threads, and cooperative cancellation."""
from concurrent.futures import CancelledError
from contextlib import closing
import json
import sqlite3
import threading
import time

import pytest

from q22_engine.jobs import JobService, MAX_PENDING, TERMINAL


def finished(service, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = service.dispatch("jobs.get", {"job_id": job_id})["job"]
        if job["state"] in TERMINAL:
            return job
        time.sleep(0.01)
    raise AssertionError("Background job did not complete")


def test_results_progress_and_errors_survive_restart(tmp_path):
    service = JobService(tmp_path)

    def handler(method, params, *, cancel_check, progress):
        progress("decode", 3, 4)
        return {"log": {"label": params["label"]}}

    job = service.start("logs.import", {"label": "Flight α"}, handler)["job"]
    result = finished(service, job["id"])
    assert result["state"] == "succeeded"
    assert result["progress"]["fraction"] == 0.75
    service.close()
    reopened = JobService(tmp_path)
    assert reopened.dispatch("jobs.get", {"job_id": job["id"]})["job"]["result"] == {"log": {"label": "Flight α"}}
    assert "result" not in reopened.dispatch("jobs.list", {})["jobs"][0]
    reopened.close()


def test_running_and_queued_cancellation_and_backpressure(tmp_path):
    service = JobService(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def handler(method, params, *, cancel_check, progress):
        calls.append(params["index"])
        entered.set()
        assert release.wait(3)
        if cancel_check():
            raise CancelledError()
        return {}

    jobs = [service.start("logs.import", {"index": i}, handler)["job"] for i in range(MAX_PENDING)]
    assert entered.wait(3)
    with pytest.raises(ValueError, match="queue is full"):
        service.start("logs.import", {}, handler)
    for job in jobs:
        assert service.dispatch("jobs.cancel", {"job_id": job["id"]})["job"]["cancel_requested"]
    release.set()
    assert all(finished(service, job["id"])["state"] == "cancelled" for job in jobs)
    assert calls == [0]
    service.close()


def test_restart_marks_unfinished_jobs_interrupted_without_replay(tmp_path):
    service = JobService(tmp_path)
    service.close()
    with closing(sqlite3.connect(tmp_path / "jobs.sqlite3")) as db, db:
        for state in ("queued", "running"):
            db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (state, "2026-10-05", json.dumps({"id": state, "state": state})))
    reopened = JobService(tmp_path)
    assert {job["state"] for job in reopened.dispatch("jobs.list", {})["jobs"]} == {"interrupted"}
    reopened.close()


def test_completed_publication_wins_over_late_cancel(tmp_path):
    service = JobService(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def handler(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"log": {"id": "published"}}

    job = service.start("logs.import", {}, handler)["job"]
    assert entered.wait(3)
    service.dispatch("jobs.cancel", {"job_id": job["id"]})
    release.set()
    result = finished(service, job["id"])
    assert result["state"] == "succeeded" and result["cancel_requested"]
    service.close()


def test_failed_job_does_not_stop_next_import(tmp_path):
    service = JobService(tmp_path)

    def handler(method, params, **kwargs):
        if params.get("bad"):
            raise ValueError("Unsupported schema")
        return {"session": {"id": "ok"}}

    bad = service.start("logs.import", {"bad": True}, handler)["job"]
    good = service.start("logs.session", {}, handler)["job"]
    assert finished(service, bad["id"])["error"]["message"] == "Unsupported schema"
    assert finished(service, good["id"])["state"] == "succeeded"
    assert service.dispatch("jobs.list", {"limit": 1})["next_offset"] == 1
    service.close()
