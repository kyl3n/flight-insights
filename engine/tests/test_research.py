"""Research provenance, local-only queries and explicit bounded source retrieval."""
from concurrent.futures import CancelledError
from email.message import Message
import hashlib
import json
import ssl
from urllib.error import HTTPError, URLError

import pytest

from q22_engine import guidance, research
from q22_engine.research import ResearchService
from q22_engine.store import Store


REVISION = "a" * 40
COMMIT_AT = "2026-10-15T10:00:00Z"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")


class OfficialFetcher:
    def __init__(self):
        self.requests = []
        self.fail_source = None
        self.content = b"Official synthetic source\n=========================\nMotor RPM telemetry tracks a notch.\nCompatible ESC firmware and pole count matter.\n\nInspect bandwidth and phase delay.\n"

    def __call__(self, url, **limits):
        self.requests.append((url, limits))
        if url == research.REF_URL:
            return json.dumps({"object": {"type": "commit", "sha": REVISION}}).encode()
        if "/git/commits/" in url:
            return json.dumps({"sha": REVISION, "committer": {"date": COMMIT_AT}}).encode()
        if self.fail_source and self.fail_source in url:
            raise URLError("injected https://user:secret@example.com/proxy?private=leak")
        return self.content


def test_startup_status_and_initial_queries_are_offline_with_pinned_excerpts(store):
    calls = []
    service = ResearchService(store, fetcher=lambda *args, **kwargs: calls.append(args))
    status = service.dispatch("research.status", {})
    assert len(status["topics"]) == 6
    assert status["mode"] == "offline_first"
    for topic in status["topics"]:
        result = service.dispatch("research.query", {"topic": topic["id"]})
        assert result["results"] and result["capability_requirements"]
        assert result["latest_check"] is None
        for source in result["results"]:
            assert source["revision"] == guidance.WIKI_REVISION
            assert source["review_state"] == "bundled_reviewed_excerpt"
            assert source["cache_state"] == "bundled"
            assert len(source["sha256"]) == 64
            assert source["vehicle_applicability"] == "unverified"
            assert source["excerpts"][0]["line_start"] > 0
    assert calls == []
    assert store.list_documents("research_source") == []


def test_explicit_update_pins_commit_hashes_sources_and_reopens_offline(store):
    fetcher = OfficialFetcher()
    service = ResearchService(store, fetcher=fetcher)
    registry_before = guidance.registry()["sources"]
    progress = []
    result = service.dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True},
                              progress=lambda *args: progress.append(args))
    assert result["state"] == "updated"
    assert result["check"]["revision"] == REVISION
    assert result["check"]["source_count"] == 2
    assert progress[-1] == ("research_sources_saved", 4, 4)
    assert len(fetcher.requests) == 4
    for url, limits in fetcher.requests:
        research._validate_url(url)
        assert 0 < limits["timeout_s"] <= research.REQUEST_TIMEOUT_S
        assert limits["max_bytes"] <= research.MAX_SOURCE_BYTES
    raw_requests = [url for url, _ in fetcher.requests if url.startswith("https://raw.")]
    assert all(f"/{REVISION}/" in url for url in raw_requests)
    cached = store.list_documents("research_source")
    assert len(cached) == 2
    assert all(note["sha256"] == hashlib.sha256(fetcher.content).hexdigest() and note["immutable"] for note in cached)
    assert guidance.registry()["sources"] == registry_before
    offline = ResearchService(Store(store.root), fetcher=lambda *a, **k: pytest.fail("Offline query attempted a network request"))
    selected = offline.dispatch("research.query", {"topic": "bidirectional_dshot", "query": "pole"})
    assert len(selected["results"]) == 2
    assert all(source["review_state"] == "unreviewed_source" for source in selected["results"])
    assert all(source["changed_since_bundled"] is True for source in selected["results"])
    assert all("pole" in source["excerpts"][0]["text"] for source in selected["results"])
    assert all(source["revision"] in source["url"] for source in selected["results"])


def test_search_text_is_local_and_no_flight_context_is_accepted_in_online_requests(store):
    fetcher = OfficialFetcher()
    service = ResearchService(store, fetcher=fetcher)
    service.dispatch("research.refresh", {"topic": "notch_tracking", "allow_online": True})
    urls = list(fetcher.requests)
    result = service.dispatch("research.query", {"topic": "notch_tracking", "query": "PRIVATE_AIRCRAFT_ID"})
    assert result["results"] == [] and result["warnings"]
    assert fetcher.requests == urls
    with pytest.raises(ValueError, match="fields"):
        service.dispatch("research.refresh", {"topic": "notch_tracking", "allow_online": True, "flight_id": "private"})
    assert fetcher.requests == urls
    assert all("PRIVATE" not in url for url, _ in urls)


@pytest.mark.parametrize("allow", [None, False, 1, "true"])
def test_online_check_requires_explicit_boolean_and_never_runs_implicitly(store, allow):
    service = ResearchService(store, fetcher=lambda *a, **k: pytest.fail("Unexpected network request"))
    with pytest.raises(ValueError, match="allow_online=true"):
        service.dispatch("research.refresh", {"topic": "vibration", "allow_online": allow})


@pytest.mark.parametrize("url", [
    "http://api.github.com/repos/ArduPilot/ardupilot_wiki/git/ref/heads/master",
    "https://api.github.com.evil.test/repos/ArduPilot/ardupilot_wiki/git/ref/heads/master",
    "https://user:secret@api.github.com/repos/ArduPilot/ardupilot_wiki/git/ref/heads/master",
    "https://api.github.com:443/repos/ArduPilot/ardupilot_wiki/git/ref/heads/master",
    research.REF_URL + "?flight=private", research.REF_URL + "#fragment", research.REF_URL + "/../main",
    "https://127.0.0.1/source", "file:///etc/passwd", "https://api.github.com/other/path",
    f"https://raw.githubusercontent.com/ArduPilot/ardupilot_wiki/{REVISION}/common/source/docs/../../secret.rst",
    f"https://raw.githubusercontent.com/ArduPilot/ardupilot_wiki/{REVISION}/common/source/docs/common-esc-telemetry.rst%00",
    "https://raw.githubusercontent.com/ArduPilot/ardupilot_wiki/master/common/source/docs/common-esc-telemetry.rst",
    f"https://raw.githubusercontent.com/AnotherOrg/ardupilot_wiki/{REVISION}/common/source/docs/common-esc-telemetry.rst",
    research.REF_URL + "\n",
])
def test_url_allowlist_rejects_userinfo_redirect_targets_queries_mutable_refs_and_nonofficial_sources(url):
    with pytest.raises(ValueError, match="allowlist"):
        research._validate_url(url)


def test_partial_update_keeps_bundled_fallback_and_does_not_expose_proxy_exception_secrets(store):
    fetcher = OfficialFetcher()
    fetcher.fail_source = "common-esc-telem-based-notch.rst"
    result = ResearchService(store, fetcher=fetcher).dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True})
    assert result["state"] == "partial"
    assert result["check"]["source_count"] == 1
    assert {item["cache_state"] for item in result["results"]} == {"cached", "bundled"}
    assert "secret" not in json.dumps(result) and "leak" not in json.dumps(result)
    assert any("completed only partially" in item for item in result["warnings"])
    assert any("could not be reached" in item for item in result["warnings"])


@pytest.mark.parametrize("body", [b"[]", b"{\"object\": []}", b"{\"object\": {\"type\":\"tree\",\"sha\":\"bad\"}}", b"not json", b"x" * (research.MAX_METADATA_BYTES + 1)],
                         ids=["array", "invalid_object", "invalid_revision", "invalid_json", "oversized_metadata"])
def test_malformed_or_oversized_revision_response_cannot_publish_source_notes(store, body):
    result = ResearchService(store, fetcher=lambda *a, **k: body).dispatch("research.refresh", {"topic": "vibration", "allow_online": True})
    assert result["state"] == "failed"
    assert result["check"]["source_count"] == 0
    assert store.list_documents("research_source") == []
    assert any("failed validation" in item for item in result["warnings"])


@pytest.mark.parametrize("content", [b"<html>upstream error</html>", b"x\x00y", b"\xff\xff", b"", b"x" * (research.MAX_SOURCE_BYTES + 1)],
                         ids=["html_error", "nul_byte", "invalid_utf8", "empty", "oversized_source"])
def test_invalid_source_text_does_not_replace_pinned_guidance_or_publish_notes(store, content):
    fetcher = OfficialFetcher()
    fetcher.content = content
    result = ResearchService(store, fetcher=fetcher).dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True})
    assert result["state"] == "failed"
    assert all(item["cache_state"] == "bundled" for item in result["results"])
    assert store.list_documents("research_source") == []


def test_repeated_updates_preserve_immutable_history_and_latest_source_query(store):
    fetcher = OfficialFetcher()
    service = ResearchService(store, fetcher=fetcher)
    service.dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True})
    original = store.list_documents("research_source")
    fetcher.content = b"Changed official test source\nNew motor RPM telemetry evidence\n"
    service.dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True})
    assert len(store.list_documents("research_source")) == 4
    assert len(store.list_documents("research_check")) == 2
    for source in original:
        assert store.get_document("research_source", source["id"]) == source
    result = service.dispatch("research.query", {"topic": "bidirectional_dshot", "query": "New"})
    assert len(result["results"]) == 2


def test_cancellation_retains_completed_source_and_marks_check_without_publishing_unfinished_source(store):
    fetcher = OfficialFetcher()
    service = ResearchService(store, fetcher=fetcher)
    # Ref + commit + first source complete; cancel before the second source.
    def cancel():
        return len(store.list_documents("research_source")) == 1
    with pytest.raises(CancelledError):
        service.dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True}, cancel_check=cancel)
    assert len(store.list_documents("research_source")) == 1
    assert len(fetcher.requests) == 3
    assert service.status()["topics"][3]["latest_check"]["state"] == "cancelled"
    assert any("was cancelled" in item for item in service.query("bidirectional_dshot")["warnings"])


@pytest.mark.parametrize("failure_stage", ["revision", "source"])
def test_network_failure_after_cancellation_cannot_be_published_as_completed_check(store, failure_stage):
    fetcher = OfficialFetcher()
    cancelled = False
    def interrupted(url, **limits):
        nonlocal cancelled
        if (failure_stage == "revision" and url == research.REF_URL) or (failure_stage == "source" and "common-esc-telem-based-notch.rst" in url):
            cancelled = True
            raise URLError("Network failed after the cancellation request")
        return fetcher(url, **limits)
    service = ResearchService(store, fetcher=interrupted)
    with pytest.raises(CancelledError):
        service.dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True}, cancel_check=lambda: cancelled)
    assert service.query("bidirectional_dshot")["latest_check"]["state"] == "cancelled"
    assert len(store.list_documents("research_source")) == (1 if failure_stage == "source" else 0)


def test_tampered_cached_source_is_not_used_as_evidence(store):
    fetcher = OfficialFetcher()
    service = ResearchService(store, fetcher=fetcher)
    service.dispatch("research.refresh", {"topic": "bidirectional_dshot", "allow_online": True})
    note = store.list_documents("research_source")[0]
    note["text"] += "tampered"
    # The store blocks UPDATE; simulate an invalid imported/new note instead.
    store.add_document("research_source", note)
    result = service.query("bidirectional_dshot")
    assert result["warnings"]
    altered = next(item for item in result["results"] if item["source_id"] == note["source_id"])
    assert altered["cache_state"] == "bundled"
    assert "tampered" not in json.dumps(result)


class Response:
    def __init__(self, body=b"valid text", *, url=research.REF_URL, length=True, status=200, encoding=None):
        self.body, self.url, self.status, self.offset = body, url, status, 0
        self.headers = Message()
        if length:
            self.headers["Content-Length"] = str(len(body))
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read1(self, size):
        value = self.body[self.offset:self.offset + size]
        self.offset += len(value)
        return value


def transport(monkeypatch, response):
    captured = {}
    class Opener:
        def open(self, request, timeout):
            captured.update(request=request, timeout=timeout)
            return response
    def build(*handlers):
        captured["handlers"] = handlers
        return Opener()
    monkeypatch.setattr(research, "build_opener", build)
    return captured


def test_transport_uses_verified_tls_no_auth_no_cookies_and_identity_encoding(monkeypatch):
    captured = transport(monkeypatch, Response())
    assert research._https_get(research.REF_URL, timeout_s=1, max_bytes=100) == b"valid text"
    headers = dict(captured["request"].header_items())
    assert "Authorization" not in headers and "Cookie" not in headers
    assert headers["Accept-encoding"] == "identity"
    tls = next(handler for handler in captured["handlers"] if isinstance(handler, research.HTTPSHandler))._context
    assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
    assert any(isinstance(handler, research._NoRedirect) for handler in captured["handlers"])
    with pytest.raises(ValueError, match="redirects"):
        research._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test")


def test_https_handler_uses_current_python_context_and_deadline_interrupts_connected_socket(monkeypatch):
    timers = []
    class Timer:
        def __init__(self, delay, callback):
            self.callback, self.cancelled = callback, False
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            self.cancelled = True
    class Socket:
        shutdown_called = closed = False
        def shutdown(self, mode):
            self.shutdown_called = True
        def close(self):
            self.closed = True
    sock = Socket()
    monkeypatch.setattr(research.threading, "Timer", Timer)
    monkeypatch.setattr(research.time, "monotonic", lambda: 0)
    monkeypatch.setattr(research.http.client.HTTPSConnection, "connect", lambda self: setattr(self, "sock", sock))
    captured = transport(monkeypatch, Response())
    research._https_get(research.REF_URL, timeout_s=1, max_bytes=100)
    handler = next(item for item in captured["handlers"] if isinstance(item, research.HTTPSHandler))
    def do_open(connection_class, request, **kwargs):
        assert set(kwargs) == {"context"}  # Python 3.12 removed the old check_hostname argument.
        connection = connection_class("api.github.com", timeout=1, **kwargs)
        connection.connect()
        connection.sock = None  # urllib clears this while the response owns the file handle.
        timers[0].callback()
        return "handled"
    monkeypatch.setattr(handler, "do_open", do_open)
    assert handler.https_open(captured["request"]) == "handled"
    assert sock.shutdown_called and sock.closed
    assert timers[0].cancelled


@pytest.mark.parametrize("response", [
    Response(b"x" * 101), Response(b"x" * 101, length=False), Response(url="https://evil.test"),
    Response(status=302), Response(encoding="gzip"),
])
def test_transport_rejects_oversize_redirected_non_success_and_compressed_responses(monkeypatch, response):
    transport(monkeypatch, response)
    with pytest.raises(ValueError):
        research._https_get(research.REF_URL, timeout_s=1, max_bytes=100)


def test_transport_rejects_truncated_or_ambiguous_content_lengths(monkeypatch):
    response = Response()
    response.headers.replace_header("Content-Length", "20")
    transport(monkeypatch, response)
    with pytest.raises(ValueError, match="incomplete"):
        research._https_get(research.REF_URL, timeout_s=1, max_bytes=100)
    response.headers["Content-Length"] = "10"
    with pytest.raises(ValueError, match="ambiguous"):
        research._https_get(research.REF_URL, timeout_s=1, max_bytes=100)


def test_transport_receipt_deadline_and_cancellation_are_checked_between_reads(monkeypatch):
    response = Response(length=False)
    transport(monkeypatch, response)
    clock = iter([0, 0.1, 2])
    monkeypatch.setattr(research.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError):
        research._https_get(research.REF_URL, timeout_s=1, max_bytes=100)
    monkeypatch.setattr(research.time, "monotonic", lambda: 0)
    response.offset = 0
    with pytest.raises(CancelledError):
        research._https_get(research.REF_URL, timeout_s=1, max_bytes=100, cancel_check=lambda: response.offset > 0)


def test_results_and_passages_are_bounded_even_with_large_matching_source(store):
    fetcher = OfficialFetcher()
    fetcher.content = ("RPM " * 1000 + "\n") .encode() * 60
    service = ResearchService(store, fetcher=fetcher)
    result = service.dispatch("research.refresh", {"topic": "notch_tracking", "allow_online": True})
    assert len(result["results"]) <= 4
    for source in result["results"]:
        assert len(source["excerpts"]) <= research.MAX_EXCERPTS
        assert all(len(item["text"]) <= research.MAX_EXCERPT_CHARS for item in source["excerpts"])
    assert len(json.dumps(result)) < 100_000
