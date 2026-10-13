"""Persistent background jobs, real threads, and cooperative cancellation."""
from concurrent.futures import CancelledError
from contextlib import closing
import json
import os
import sqlite3
import threading
import time

import pytest
import psutil

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


def test_second_service_preserves_live_foreign_worker_and_cannot_cancel_it(tmp_path):
    first = JobService(tmp_path)
    second = None
    entered, release = threading.Event(), threading.Event()

    def handler(*_args, cancel_check, **_kwargs):
        entered.set()
        assert release.wait(4)
        assert not cancel_check()
        return {"completed_by_original_owner": True}

    try:
        job = first.start("synthetic.owner-review", {}, handler)["job"]
        assert entered.wait(2)
        running = first.dispatch("jobs.get", {"job_id": job["id"]})["job"]
        assert running["state"] == "running" and running["owner"]["pid"] == os.getpid()
        second = JobService(tmp_path)
        assert second.dispatch("jobs.get", {"job_id": job["id"]})["job"] == running
        denied = second.dispatch("jobs.cancel", {"job_id": job["id"]})
        assert denied["cancellation"] == "not_owned_by_this_engine"
        assert denied["job"] == running and not denied["job"]["cancel_requested"]
        second.close()
        second = None
        assert first.dispatch("jobs.get", {"job_id": job["id"]})["job"] == running
        release.set()
        done = finished(first, job["id"])
        assert done["state"] == "succeeded" and done["result"]["completed_by_original_owner"]
        assert first.dispatch("jobs.cancel", {"job_id": job["id"]})["cancellation"] == "already_terminal"
    finally:
        release.set()
        if second:
            second.close()
        first.close()


@pytest.mark.parametrize("liveness", ["missing", "pid_reused", "zombie", "access_denied"])
def test_recovery_requires_known_dead_owner_and_checks_process_creation_time(tmp_path, monkeypatch, liveness):
    import q22_engine.jobs as jobs_module
    initial = JobService(tmp_path)
    initial.close()
    owner_pid = 987654321
    payload = {"id": "foreign", "state": "running", "owner": {"pid": owner_pid, "created_at": 100.0}}
    with closing(sqlite3.connect(tmp_path / "jobs.sqlite3")) as db, db:
        db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (payload["id"], "2026-10-05", json.dumps(payload)))
    actual_process = jobs_module.psutil.Process

    class PriorOwner:
        def create_time(self):
            if liveness == "access_denied":
                raise psutil.AccessDenied(owner_pid)
            return 101.0 if liveness == "pid_reused" else 100.0

        def status(self):
            return psutil.STATUS_ZOMBIE if liveness == "zombie" else psutil.STATUS_RUNNING

    def process(pid=None):
        if pid != owner_pid:
            return actual_process(pid)
        if liveness == "missing":
            raise psutil.NoSuchProcess(owner_pid)
        return PriorOwner()

    monkeypatch.setattr(jobs_module.psutil, "Process", process)
    reopened = JobService(tmp_path)
    try:
        recovered = reopened.dispatch("jobs.get", {"job_id": payload["id"]})["job"]
        if liveness == "access_denied":
            assert recovered == payload
            assert reopened.dispatch("jobs.list", {})["recovery_warnings"][0]["job_id"] == payload["id"]
        else:
            assert recovered["state"] == "interrupted" and recovered["owner"] == payload["owner"]
            assert recovered["finished_at"]
        assert not reopened._events
    finally:
        reopened.close()


@pytest.mark.parametrize("owner", [None, {}, {"pid": True, "created_at": 100.0},
                                    {"pid": 123, "created_at": "invalid"}])
def test_invalid_owner_metadata_preserves_job_without_assuming_a_crash(tmp_path, owner):
    service = JobService(tmp_path)
    service.close()
    payload = {"id": "uncertain", "state": "queued", "owner": owner}
    with closing(sqlite3.connect(tmp_path / "jobs.sqlite3")) as db, db:
        db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (payload["id"], "2026-10-05", json.dumps(payload)))
    reopened = JobService(tmp_path)
    try:
        assert reopened.dispatch("jobs.get", {"job_id": payload["id"]})["job"] == payload
        result = reopened.dispatch("jobs.cancel", {"job_id": payload["id"]})
        assert result["cancellation"] == "not_owned_by_this_engine" and result["job"] == payload
        assert reopened.recovery_warnings and not reopened._events
    finally:
        reopened.close()
