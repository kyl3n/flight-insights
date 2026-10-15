"""Public engine workflows with event-driven background-job synchronization.

Network fetchers and slow work are synthetic; no aircraft or public network is
contacted. Real dispatch, durable journals, research cache and plans are used.
"""
from concurrent.futures import CancelledError, ThreadPoolExecutor
import threading
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from q22_engine.__main__ import Engine
from q22_engine.alignment import AlignmentService
from q22_engine.experiments import ExperimentService
from q22_engine.jobs import JobService, TERMINAL
from q22_engine.logs import LogService
from q22_engine.research import ResearchService
from test_experiments import flight, plan
from test_flight_configuration import readback, report_pair
from test_research import OfficialFetcher


def request(engine, method, params=None):
    return engine.handle({"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}})


def rpc(engine, method, params=None):
    reply = request(engine, method, params)
    assert "error" not in reply, reply
    return reply["result"]


class JobOutcomes:
    """Observe publication events, then read outcomes through the public RPC."""
    def __init__(self, engine, monkeypatch):
        self.engine, self.events, self.lock = engine, {}, threading.Lock()
        update = engine.jobs._update

        def observed(job_id, **changes):
            job = update(job_id, **changes)
            if job["state"] in TERMINAL:
                self.event(job_id).set()
            return job

        monkeypatch.setattr(engine.jobs, "_update", observed)

    def event(self, job_id):
        with self.lock:
            return self.events.setdefault(job_id, threading.Event())

    def wait(self, job):
        assert self.event(job["id"]).wait(5), f"No terminal publication for {job['method']}"
        return rpc(self.engine, "jobs.get", {"job_id": job["id"]})["job"]


@pytest.fixture
def engine(tmp_path):
    value = Engine(tmp_path / "engine")
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def outcomes(engine, monkeypatch):
    return JobOutcomes(engine, monkeypatch)


def test_new_methods_and_local_research_are_available_without_network(engine, monkeypatch):
    def unexpected_fetch(*_args, **_kwargs):
        pytest.fail("Local engine research contacted the network")

    monkeypatch.setattr("q22_engine.research._https_get", unexpected_fetch)
    methods = rpc(engine, "handshake")["capabilities"]
    assert {"parameters.promote_readback", "experiments.create", "experiments.list", "experiments.detail",
            "experiments.evaluate", "research.status", "research.query", "research.refresh"} <= set(methods)
    sources = {row["kind"]: row for row in rpc(engine, "sources.capabilities")["sources"]}
    assert all(sources[kind]["state"] == "available" for kind in ("flight_configuration", "flight_experiments", "research_library"))
    status = rpc(engine, "research.status")
    assert status["mode"] == "offline_first" and status["online_refresh_available"]
    result = rpc(engine, "research.query", {"topic": "vibration"})
    assert result["results"] and result["latest_check"] is None
    assert all(source["cache_state"] == "bundled" for source in result["results"])
    assert rpc(engine, "jobs.list")["jobs"] == []
    assert engine.store.list_documents("research_check") == []


def test_experiment_evaluation_runs_in_worker_and_engine_stays_responsive(engine, outcomes):
    entered, release = threading.Event(), threading.Event()
    actual = ExperimentService(engine.store)

    def delayed(method, params, *, cancel_check=None, progress=None):
        if method == "experiments.evaluate":
            progress("waiting_for_synthetic_analysis", 0, 1)
            entered.set()
            assert release.wait(5)
        return actual.dispatch(method, params, cancel_check=cancel_check, progress=progress)

    engine.experiments = SimpleNamespace(dispatch=delayed)
    experiment = rpc(engine, "experiments.create", plan())["experiment"]
    assert "job" not in experiment and rpc(engine, "jobs.list")["jobs"] == []
    try:
        job = rpc(engine, "experiments.evaluate", {"experiment_id": experiment["id"]})["job"]
        assert entered.wait(5)
        # A second RPC must complete while the analysis is held, not merely
        # after its release. Future waits avoid sleep/poll timing assumptions.
        with ThreadPoolExecutor(max_workers=1) as caller:
            assert caller.submit(rpc, engine, "engine.status").result(timeout=2)["state"] == "available"
            assert caller.submit(rpc, engine, "research.query", {"topic": "sampling"}).result(timeout=2)["results"]
            assert caller.submit(rpc, engine, "experiments.list").result(timeout=2)["experiments"][0]["id"] == experiment["id"]
        assert rpc(engine, "jobs.get", {"job_id": job["id"]})["job"]["state"] == "running"
        assert rpc(engine, "experiments.detail", {"experiment_id": experiment["id"]})["evaluations"] == []
    finally:
        release.set()
    finished = outcomes.wait(job)
    assert finished["state"] == "succeeded" and finished["method"] == "experiments.evaluate"
    evaluation = finished["result"]["evaluation"]
    assert evaluation["status"] == "insufficient_evidence"
    assert rpc(engine, "experiments.detail", {"experiment_id": experiment["id"]})["evaluations"][0]["id"] == evaluation["id"]
    assert engine.store.get_document("experiment_evaluation", evaluation["id"]) == evaluation


def test_experiment_failure_and_operator_cancellation_are_durable_rpc_outcomes(engine, outcomes):
    failed = rpc(engine, "experiments.evaluate", {"experiment_id": "missing-experiment"})["job"]
    failure = outcomes.wait(failed)
    assert failure["state"] == "failed" and "Unknown experiment ID" in failure["error"]["message"]
    entered, release = threading.Event(), threading.Event()

    def delayed(method, params, *, cancel_check, progress):
        entered.set()
        assert release.wait(5)
        if cancel_check():
            raise CancelledError("Synthetic delayed evaluation cancelled")
        pytest.fail("Cancelled evaluation continued")

    engine.experiments = SimpleNamespace(dispatch=delayed)
    try:
        cancelled = rpc(engine, "experiments.evaluate", {"experiment_id": "synthetic-held"})["job"]
        assert entered.wait(5)
        assert rpc(engine, "jobs.cancel", {"job_id": cancelled["id"]})["cancellation"] == "requested"
    finally:
        release.set()
    cancellation = outcomes.wait(cancelled)
    assert cancellation["state"] == "cancelled" and cancellation["cancel_requested"]
    assert "result" not in cancellation and engine.store.list_documents("experiment_evaluation") == []
    # A fresh journal reader must see the outcomes, not an in-memory cache.
    journal = JobService(engine.store.root)
    try:
        assert journal.dispatch("jobs.get", {"job_id": failed["id"]})["job"] == failure
        assert journal.dispatch("jobs.get", {"job_id": cancelled["id"]})["job"] == cancellation
    finally:
        journal.close()
    listed = {job["id"]: job for job in rpc(engine, "jobs.list")["jobs"]}
    assert listed[failed["id"]]["error"] == failure["error"]
    assert listed[cancelled["id"]]["state"] == "cancelled"


def test_recording_blocks_new_online_refresh_but_keeps_cached_research_available(engine, monkeypatch):
    fetched = []
    engine.research = ResearchService(engine.store, fetcher=lambda *a, **k: fetched.append(a))
    monkeypatch.setattr(engine.collection.recorder, "status", lambda: {"active": True})
    response = request(engine, "research.refresh", {"topic": "sampling", "allow_online": True})
    assert response["error"]["code"] == -32602 and "Stop recording" in response["error"]["message"]
    assert rpc(engine, "research.status")["mode"] == "offline_first"
    assert rpc(engine, "research.query", {"topic": "sampling"})["results"]
    assert fetched == [] and rpc(engine, "jobs.list")["jobs"] == []


def test_queued_research_cancels_without_fetch_when_recording_starts_before_worker(engine, outcomes, monkeypatch):
    entered, release, active = threading.Event(), threading.Event(), threading.Event()
    calls, fetched = [], []
    monkeypatch.setattr(engine.collection.recorder, "status", lambda: {"active": active.is_set()})

    def blocking_experiment(method, params, *, cancel_check, progress):
        calls.append(method)
        entered.set()
        assert release.wait(5)
        return {"evaluation": {"synthetic": True}}

    def following_readback(method, params, *, cancel_check, progress):
        calls.append(method)
        return {"acquisition": {"state": "complete", "synthetic": True}}

    engine.experiments = SimpleNamespace(dispatch=blocking_experiment)
    engine.acquisition = SimpleNamespace(dispatch=following_readback)
    engine.research = ResearchService(engine.store, fetcher=lambda *a, **k: fetched.append(a))
    try:
        blocked = rpc(engine, "experiments.evaluate", {"experiment_id": "synthetic-held"})["job"]
        assert entered.wait(5)
        update = rpc(engine, "research.refresh", {"topic": "sampling", "allow_online": True})["job"]
        successor = rpc(engine, "acquisition.parameters.capture", {})["job"]
        assert rpc(engine, "jobs.get", {"job_id": update["id"]})["job"]["state"] == "queued"
        active.set()
    finally:
        release.set()
    assert outcomes.wait(blocked)["state"] == "succeeded"
    cancelled = outcomes.wait(update)
    assert cancelled["state"] == "cancelled" and not cancelled["cancel_requested"]
    assert outcomes.wait(successor)["state"] == "succeeded"
    assert calls == ["experiments.evaluate", "acquisition.parameters.capture"] and fetched == []
    assert engine.store.list_documents("research_source") == []
    checks = engine.store.list_documents("research_check")
    assert len(checks) == 1 and checks[0]["state"] == "cancelled" and checks[0]["source_count"] == 0
    topic = next(row for row in rpc(engine, "research.status")["topics"] if row["id"] == "sampling")
    assert topic["latest_check"]["state"] == "cancelled"


@pytest.mark.parametrize("trigger", ["recording", "operator"])
def test_running_refresh_uses_recording_and_operator_cancellation_predicates(engine, outcomes, monkeypatch, trigger):
    entered, release, active = threading.Event(), threading.Event(), threading.Event()
    fetched = []
    monkeypatch.setattr(engine.collection.recorder, "status", lambda: {"active": active.is_set()})

    def cooperative_fetch(url, *, cancel_check, **limits):
        fetched.append(url)
        entered.set()
        assert release.wait(5)
        if cancel_check():
            raise CancelledError("Synthetic fetch yielded to cancellation")
        pytest.fail("Research cancellation predicate was not propagated")

    engine.research = ResearchService(engine.store, fetcher=cooperative_fetch)
    try:
        job = rpc(engine, "research.refresh", {"topic": "vibration", "allow_online": True})["job"]
        assert entered.wait(5)
        if trigger == "recording":
            active.set()
        else:
            assert rpc(engine, "jobs.cancel", {"job_id": job["id"]})["cancellation"] == "requested"
    finally:
        release.set()
    finished = outcomes.wait(job)
    assert finished["state"] == "cancelled" and finished["cancel_requested"] == (trigger == "operator")
    assert len(fetched) == 1 and engine.store.list_documents("research_source") == []
    assert engine.store.list_documents("research_check")[0]["state"] == "cancelled"


def test_research_refresh_success_and_fetch_failure_surface_domain_outcomes(engine, outcomes):
    fetcher = OfficialFetcher()
    engine.research = ResearchService(engine.store, fetcher=fetcher)
    success = rpc(engine, "research.refresh", {"topic": "bidirectional_dshot", "allow_online": True})["job"]
    finished = outcomes.wait(success)
    assert finished["state"] == "succeeded" and finished["result"]["state"] == "updated"
    assert len(fetcher.requests) == 4 and finished["result"]["check"]["source_count"] == 2

    def unavailable_fetch(*_args, **_kwargs):
        raise URLError("synthetic private proxy details must not reach RPC")

    engine.research.fetcher = unavailable_fetch
    failed = rpc(engine, "research.refresh", {"topic": "bidirectional_dshot", "allow_online": True})["job"]
    checked = outcomes.wait(failed)
    # The research operation completed and preserved a failed online-check
    # outcome; clients must inspect result.state rather than job.state alone.
    assert checked["state"] == "succeeded" and checked["result"]["state"] == "failed"
    assert checked["result"]["check"]["state"] == "failed" and checked["result"]["check"]["errors"]
    assert "private proxy" not in str(checked)
    local = rpc(engine, "research.query", {"topic": "bidirectional_dshot"})
    assert local["latest_check"]["state"] == "failed" and all(row["cache_state"] == "cached" for row in local["results"])


@pytest.mark.parametrize("trigger", ["recording", "operator"])
@pytest.mark.parametrize("phase", ["revision", "source"])
def test_fetch_error_does_not_mask_cancellation_while_request_was_blocked(engine, outcomes, monkeypatch, trigger, phase):
    entered, release, active = threading.Event(), threading.Event(), threading.Event()
    official, source_calls = OfficialFetcher(), []
    monkeypatch.setattr(engine.collection.recorder, "status", lambda: {"active": active.is_set()})

    def blocked_then_failed(url, **kwargs):
        if phase == "source":
            if url.startswith("https://raw."):
                source_calls.append(url)
            if len(source_calls) < 2:
                return official(url, **kwargs)
        entered.set()
        assert release.wait(5)
        raise URLError("Synthetic request timed out after cancellation")

    engine.research = ResearchService(engine.store, fetcher=blocked_then_failed)
    try:
        job = rpc(engine, "research.refresh", {"topic": "vibration", "allow_online": True})["job"]
        assert entered.wait(5)
        if trigger == "recording":
            active.set()
        else:
            rpc(engine, "jobs.cancel", {"job_id": job["id"]})
    finally:
        release.set()
    result = outcomes.wait(job)
    assert result["state"] == "cancelled"
    check = engine.store.list_documents("research_check")[0]
    assert check["state"] == "cancelled" and check["source_count"] == int(phase == "source")
    assert len(engine.store.list_documents("research_source")) == int(phase == "source")


def test_readback_promotion_is_wired_into_engine_parameter_catalog(engine, outcomes, tmp_path):
    acquisition = readback((engine.store,), tmp_path)
    job = rpc(engine, "parameters.promote_readback", {"acquisition_id": acquisition["id"]})["job"]
    completed = outcomes.wait(job)
    assert completed["state"] == "succeeded"
    snapshot = completed["result"]["snapshot"]
    assert snapshot["usable_for_full_comparison"] and snapshot["parameter_count"] == 2
    assert snapshot["effective_configuration"] == "unknown"
    assert rpc(engine, "parameters.list")["snapshots"][0]["id"] == snapshot["id"]
    repeated = rpc(engine, "parameters.promote_readback", {"acquisition_id": acquisition["id"]})["job"]
    assert outcomes.wait(repeated)["result"]["already_preserved"]
    invalid = rpc(engine, "parameters.promote_readback", {"acquisition_id": "missing"})["job"]
    failed = outcomes.wait(invalid)
    assert failed["state"] == "failed" and "Unknown acquisition" in failed["error"]["message"]


def test_report_comparison_is_a_durable_background_result(engine, outcomes):
    before, after = flight(engine.store, 1, 100), flight(engine.store, 2, 80)
    job = rpc(engine, "insights.compare", {"before_report_id": before["id"], "after_report_id": after["id"]})["job"]
    completed = outcomes.wait(job)
    assert completed["state"] == "succeeded"
    assert completed["result"]["before_report_id"] == before["id"]
    power = next(row for row in completed["result"]["metrics"] if row["name"] == "mean_power_w")
    assert power["delta"] == -20
    assert not completed["result"]["causal_attribution"]


@pytest.mark.parametrize("method", ["parameters.promote_readback", "insights.compare", "experiments.evaluate"])
def test_full_readback_hashing_is_responsive_and_cancellable(engine, outcomes, monkeypatch, tmp_path, method):
    import q22_engine.flight_configuration as configuration

    if method == "parameters.promote_readback":
        acquisition = readback((engine.store,), tmp_path)
        params = {"acquisition_id": acquisition["id"]}
    else:
        logs = LogService(engine.store)
        collection = engine.collection.collection
        reports, _ = report_pair((engine.store, logs, collection, AlignmentService(engine.store, logs, collection)), tmp_path)
        before, after = [engine.store.add_document("insight_report", report) for report in reports]
        if method == "insights.compare":
            params = {"before_report_id": before["id"], "after_report_id": after["id"]}
        else:
            experiment = rpc(engine, "experiments.create", plan([before], [after]))["experiment"]
            params = {"experiment_id": experiment["id"]}
    snapshots_before = engine.store.list_documents("snapshot")
    entered, release = threading.Event(), threading.Event()
    original_hash = configuration.hash_file

    def held_hash(path, cancel_check=None):
        assert callable(cancel_check), "Worker cancellation must reach original-file hashing"
        entered.set()
        assert release.wait(5)
        return original_hash(path, cancel_check)

    monkeypatch.setattr(configuration, "hash_file", held_hash)
    try:
        job = rpc(engine, method, params)["job"]
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as reader:
            status = reader.submit(rpc, engine, "engine.status").result(timeout=2)
            assert status["state"] == "available"
        rpc(engine, "jobs.cancel", {"job_id": job["id"]})
    finally:
        release.set()
    cancelled = outcomes.wait(job)
    assert cancelled["state"] == "cancelled" and "result" not in cancelled
    assert engine.store.list_documents("snapshot") == snapshots_before
