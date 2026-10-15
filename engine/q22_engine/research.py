"""Explicit official-source research with an offline, immutable local library.

Only a fixed topic selects fixed public documentation paths. Search terms and
flight evidence stay local. Fetched prose remains quoted source material; it
cannot modify reviewed advice, firmware applicability, or aircraft settings.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import datetime, timezone
import hashlib
import http.client
import json
import re
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from . import guidance
from .performance_advice import _references


VERSION = "q22-official-research/2026-10-15.1"
MAX_SOURCE_BYTES = 256 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_QUERY_CHARS = 160
MAX_EXCERPTS = 3
MAX_EXCERPT_CHARS = 1200
REQUEST_TIMEOUT_S = 6.0
REFRESH_TIMEOUT_S = 30.0
REF_URL = "https://api.github.com/repos/ArduPilot/ardupilot_wiki/git/ref/heads/master"
REPOSITORY = "ArduPilot/ardupilot_wiki"
SHA = re.compile(r"[0-9a-f]{40}\Z")
RATE_LIMIT_FAILURE = "Official source request was rate-limited; retry later."
NETWORK_FAILURE = "Official source could not be reached; cached research remains available."
VALIDATION_FAILURE = "Official source response failed validation; cached research remains available."
PUBLIC_FAILURES = {RATE_LIMIT_FAILURE, NETWORK_FAILURE, VALIDATION_FAILURE}

# Each quoted initial passage was checked against the pinned full-source hash.
# It is deliberately small; updated full text is fetched only on explicit demand.
_INITIAL = {
    "wiki_vibration": ("Measuring vibration", "vibration", 28,
        "Vibration levels below 30m/s/s are normally acceptable."),
    "mechanical": ("Mechanical vibration and mounting", "mechanical", 201,
        "Motor to frame arm and frame arm to central hub mounts need to be secure and flex free"),
    "sampling": ("IMU sampling and aliasing", "sampling", 7,
        "The IMU BatchSampler can be used to record high-frequency data from the IMU sensors to the dataflash log on the autopilot."),
    "wiki_notch": ("Dynamic harmonic notch filtering", "harmonic_notch", 74,
        "ESC RPM telemetry is generally the best source of frequency data because it automatically adapts to varying payloads"),
    "esc_notch": ("ESC telemetry based harmonic notch", "esc_notch", 26,
        "The rate at which the harmonic notch frequency is updated has a big impact on noise in the PID loops."),
    "throttle_notch": ("Throttle based dynamic notch", "throttle_notch", 12,
        "In order to configure the throttle-based dynamic harmonic notch filter it is important to identify  the motor noise at the hover throttle level."),
    "fft_tracking": ("In-flight FFT tracking", "fft_tracking", 28,
        "Using In-Flight FFT can result in poorer performance than a properly set up"),
    "esc_telemetry": ("ESC telemetry and bidirectional DShot", "esc_telemetry", 41,
        "Supporting bi-directional DShot requires exclusive use of one or more DMA channels and thus not all autopilots support it."),
    "wiki_tuning": ("Copter tuning sequence", "heavy_lift", 7,
        "The tuning process consists of the following steps:"),
    "wiki_initial": ("Preparing the aircraft for tuning", "heavy_lift", 11,
        "It is very important to ensure that the thrust curve of your VTOL\nmotors is as linear as possible."),
    "thrust": ("Motor thrust scaling", "thrust", 7,
        "Copter includes *motor thrust scaling* which compensates for the\nnon-linear thrust output of most ESCs and motors."),
    "power": ("Current limiting and voltage scaling", "power", 7,
        "*current limiting* in order to protect the battery from\ndamage and *voltage scaling* to compensate for the voltage drop as the\nbattery is depleted."),
}


def _source_catalog():
    sources = {}
    for identifier, (title, reference, line, excerpt) in _INITIAL.items():
        candidates = _references(reference)
        source = next(item for item in candidates if item["source_id"] == identifier)
        sources[identifier] = {"source_id": identifier, "title": title, "path": source["path"],
            "revision": source["revision"], "sha256": source["sha256"], "retrieved_at": source["retrieved_at"],
            "url": f"https://github.com/{REPOSITORY}/blob/{source['revision']}/{source['path']}",
            "excerpt": {"text": excerpt, "line_start": line, "line_end": line + excerpt.count("\n"), "truncated": True, "basis": "source_excerpt"}}
    return sources


SOURCES = _source_catalog()
TOPICS = {
    "vibration": {"title": "Vibration, clipping and mechanical causes", "source_ids": ["wiki_vibration", "mechanical", "sampling"],
        "requirements": ["IMU identity, VIBE units, continuous clipping counters and flight phase", "Mechanical inspection; high-rate gyro evidence before considering notch values"],
        "terms": "vibration clipping mounting propeller"},
    "sampling": {"title": "Gyro sampling, logging and aliasing", "source_ids": ["sampling", "wiki_notch"],
        "requirements": ["Exact board/IMU and firmware features", "Actual sample rate, timebase, filter stage and anti-alias chain", "Processor and logging capacity"],
        "terms": "sampling aliasing pre post filter"},
    "notch_tracking": {"title": "Notch tracking: RPM, throttle and FFT", "source_ids": ["wiki_notch", "esc_notch", "throttle_notch", "fft_tracking"],
        "requirements": ["Exact build, active notch configuration and high-rate noise evidence", "Tracking-source rate, latency, reliability and matching noise peaks", "Payload/thrust calibration for throttle tracking; compiled FFT support and processor budget for FFT"],
        "terms": "RPM throttle FFT phase bandwidth"},
    "bidirectional_dshot": {"title": "ESC telemetry and bidirectional DShot", "source_ids": ["esc_telemetry", "esc_notch"],
        "requirements": ["Exact board/output mapping and DMA support", "ESC model and installed compatible firmware", "Motor pole count, electrical-to-mechanical RPM conversion and per-motor telemetry reliability", "Bidirectional DShot provides RPM; serial/CAN telemetry can also support RPM-tracked filtering"],
        "terms": "bidirectional DShot poles firmware telemetry"},
    "control_response": {"title": "Control response and tuning prerequisites", "source_ids": ["wiki_tuning", "wiki_initial", "wiki_notch"],
        "requirements": ["Exact firmware parameter names and units", "Aligned desired/actual rates, noise spectrum and verified output limits", "Comparable manoeuvres, payload, battery and wind"],
        "terms": "tuning thrust noise control"},
    "propulsion_power": {"title": "Thrust, battery and power calibration", "source_ids": ["thrust", "power", "wiki_initial"],
        "requirements": ["Calibrated voltage/current and actual battery identity/capability", "Measured motor/ESC/propeller thrust response before changing thrust curves", "Output mapping, load and limiting evidence"],
        "terms": "thrust current voltage calibration"},
}
LIMITATIONS = [
    "Research covers these selected official ArduPilot topics; it is not an exhaustive web search or AI-generated diagnosis.",
    "Retrieved source passages are unreviewed for this app and unvalidated for the aircraft. They do not replace the pinned, reviewed guidance or authorize applying example settings.",
    "An explicit update contacts only fixed GitHub API/raw paths for the official ArduPilot wiki. Flight data, parameter values, identifiers and search terms are not sent.",
    "Local searches and startup do not make network requests. Source text is displayed as text, never executed as code or instructions.",
]


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _cancel(check):
    if check and check():
        raise CancelledError("Research update cancelled; completed source notes remain available offline.")


def _validate_url(url):
    """Accept only canonical, generated HTTPS URLs; redirects have no allowance."""
    if not isinstance(url, str) or len(url) > 600 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ValueError("Research URL is outside the fixed source allowlist.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username or parsed.password
            or parsed.netloc not in {"api.github.com", "raw.githubusercontent.com"} or "%" in url or "\\" in url):
        raise ValueError("Research URL is outside the fixed source allowlist.")
    if url == REF_URL:
        return url
    if parsed.netloc == "api.github.com" and re.fullmatch(
            r"/repos/ArduPilot/ardupilot_wiki/git/commits/[0-9a-f]{40}", parsed.path):
        return url
    prefix = "/ArduPilot/ardupilot_wiki/"
    if parsed.netloc == "raw.githubusercontent.com" and parsed.path.startswith(prefix):
        revision, _, path = parsed.path[len(prefix):].partition("/")
        if SHA.fullmatch(revision) and path in {source["path"] for source in SOURCES.values()}:
            return url
    raise ValueError("Research URL is outside the fixed source allowlist.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Research source redirects are not accepted.")


def _https_get(url, *, timeout_s, max_bytes, cancel_check=None):
    """Verified TLS, inherited proxy/CA trust, bounded bodies and receipt time.

    A deadline timer interrupts connected header/body reads, including a slow
    trickle. Platform DNS resolution remains subject to the OS resolver.
    """
    _validate_url(url)
    _cancel(cancel_check)
    deadline = time.monotonic() + timeout_s
    connections = []

    def abort_connections():
        for connection in connections:
            # Retain the socket reference: urllib may clear connection.sock
            # while the response's makefile still holds the descriptor open.
            sock = getattr(connection, "research_socket", None) or connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    class DeadlineConnection(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            connections.append(self)

        def connect(self):
            super().connect()
            self.research_socket = self.sock
            if time.monotonic() >= deadline:
                abort_connections()
                raise TimeoutError("Research connection deadline exceeded.")

    class DeadlineHTTPSHandler(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(DeadlineConnection, req, context=self._context)

    opener = build_opener(_NoRedirect(), DeadlineHTTPSHandler(context=ssl.create_default_context()))
    request = Request(url, headers={"User-Agent": "Q22-Flight-Lab-Research/1",
        "Accept": "application/json" if url.startswith("https://api.github.com/") else "text/plain",
        "Accept-Encoding": "identity"}, method="GET")
    timer = threading.Timer(timeout_s, abort_connections)
    timer.daemon = True
    timer.start()
    try:
        with opener.open(request, timeout=timeout_s) as response:
            if response.status != 200 or response.geturl() != url:
                raise ValueError("Research source returned an unexpected response.")
            if sum(len(key) + len(value) for key, value in response.headers.items()) > MAX_METADATA_BYTES:
                raise ValueError("Research response headers exceeded the bound.")
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ValueError("Compressed research responses are not accepted.")
            lengths = response.headers.get_all("Content-Length", [])
            if len(lengths) > 1:
                raise ValueError("Research response has ambiguous Content-Length headers.")
            length = lengths[0] if lengths else None
            if length is not None and (not length.isdigit() or int(length) > max_bytes):
                raise ValueError("Research source exceeds the response-size limit.")
            content = bytearray()
            while True:
                _cancel(cancel_check)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Research response deadline exceeded.")
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(min(timeout_s, remaining))
                chunk = response.read1(min(65536, max_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ValueError("Research source exceeds the response-size limit.")
            _cancel(cancel_check)
            if time.monotonic() > deadline:
                raise TimeoutError("Research response deadline exceeded.")
            if length is not None and len(content) != int(length):
                raise ValueError("Research response was incomplete.")
            return bytes(content)
    finally:
        timer.cancel()


def _text(body):
    if not isinstance(body, bytes) or len(body) > MAX_SOURCE_BYTES:
        raise ValueError("Research source must be bounded UTF-8 text.")
    text = body.decode("utf-8")
    if not text.strip() or "\x00" in text or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        raise ValueError("Research source was not a usable documentation file.")
    return text


def _date(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Research source date is invalid.")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Research source date requires a timezone.")
    return stamp.astimezone(timezone.utc).isoformat()


def _freshness(retrieved_at):
    stamp = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)  # Bundled review dates can be date-only.
    age = (datetime.now(timezone.utc) - stamp).total_seconds() / 86400
    return {"status": "clock_before_retrieval" if age < 0 else "review_due" if age >= guidance.REVIEW_AFTER_DAYS else "within_review_window",
            "age_days": round(age, 2), "review_after_days": guidance.REVIEW_AFTER_DAYS,
            "interpretation": "Age is not aircraft validation or proof that no newer source exists."}


def _passages(text, terms):
    lines = text.splitlines()
    ranked = sorted(((sum(token in line.casefold() for token in terms), index)
                     for index, line in enumerate(lines) if line.strip()), key=lambda row: (-row[0], row[1]))
    excerpts, covered = [], set()
    for score, index in ranked:
        if not score or index in covered:
            continue
        start, end = max(0, index - 1), min(len(lines), index + 3)
        excerpt = "\n".join(lines[start:end])
        excerpts.append({"text": excerpt[:MAX_EXCERPT_CHARS], "line_start": start + 1, "line_end": end,
                         "truncated": len(excerpt) > MAX_EXCERPT_CHARS, "basis": "source_excerpt"})
        covered.update(range(start, end))
        if len(excerpts) == MAX_EXCERPTS:
            break
    return excerpts


class ResearchService:
    def __init__(self, store, *, fetcher=None):
        self.store = store
        self.fetcher = fetcher or _https_get

    def dispatch(self, method, params, cancel_check=None, progress=None):
        if not isinstance(params, dict):
            raise ValueError("Research params must be an object.")
        allowed = {"research.status": set(), "research.query": {"topic", "query"},
                   "research.refresh": {"topic", "allow_online"}}
        if method not in allowed or set(params) - allowed[method]:
            raise ValueError("Unsupported research method or fields.")
        if method == "research.status":
            return self.status()
        topic = params.get("topic")
        if not isinstance(topic, str) or topic not in TOPICS:
            raise ValueError("Select a supported research topic.")
        if method == "research.query":
            return self.query(topic, params.get("query", ""))
        if params.get("allow_online") is not True:
            raise ValueError("An explicit online source check requires allow_online=true.")
        return self.refresh(topic, cancel_check, progress)

    def _latest(self, kind, field, value):
        # kind and field are selected by implementation, never caller input.
        with self.store._connect() as db:
            row = db.execute(f"SELECT data FROM documents WHERE kind=? AND json_extract(data, '$.{field}')=? "
                             "ORDER BY created_at DESC, id DESC LIMIT 1", (kind, value)).fetchone()
        return json.loads(row[0]) if row else None

    def _last_check(self, topic):
        note = self._latest("research_check", "topic", topic)
        if note is None:
            return None
        return {key: note[key] for key in ("id", "state", "checked_at", "revision", "commit_at", "source_count", "errors")}

    def status(self):
        return {"version": VERSION, "mode": "offline_first", "online_refresh_available": True,
                "topics": [{"id": topic, "title": value["title"], "source_ids": list(value["source_ids"]),
                            "latest_check": self._last_check(topic)} for topic, value in TOPICS.items()],
                "bundled_review": {"registry_version": guidance.REGISTRY_VERSION, "revision": guidance.WIKI_REVISION,
                                   "retrieved_at": guidance.RETRIEVED_AT, "firmware_release": guidance.FIRMWARE_RELEASE,
                                   "freshness": _freshness(guidance.RETRIEVED_AT)},
                "research_limits": {"source_bytes": MAX_SOURCE_BYTES, "query_characters": MAX_QUERY_CHARS,
                                    "request_timeout_s": REQUEST_TIMEOUT_S, "refresh_timeout_s": REFRESH_TIMEOUT_S},
                "limitations": list(LIMITATIONS)}

    def _cached_source(self, source_id):
        note = self._latest("research_source", "source_id", source_id)
        if note is None:
            return None
        if not isinstance(note, dict):
            raise ValueError("Cached source is invalid.")
        source = SOURCES[source_id]
        body = note.get("text")
        if (not isinstance(body, str) or len(body) > MAX_SOURCE_BYTES or note.get("path") != source["path"]
                or not SHA.fullmatch(str(note.get("revision", "")))):
            raise ValueError("Cached source metadata is invalid.")
        encoded = body.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES or hashlib.sha256(encoded).hexdigest() != note.get("sha256"):
            raise ValueError("Cached source integrity check failed.")
        _text(encoded)
        _date(note.get("retrieved_at"))
        _date(note.get("commit_at"))
        return note

    def query(self, topic, query=""):
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS or any(ord(c) < 32 for c in query):
            raise ValueError("Research query must be plain text, at most 160 characters.")
        selection = TOPICS[topic]
        tokens = re.findall(r"[a-z0-9_]{2,40}", (query or selection["terms"]).casefold())[:8]
        results, warnings = [], []
        for source_id in selection["source_ids"]:
            source = SOURCES[source_id]
            try:
                note = self._cached_source(source_id)
            except (ValueError, TypeError, KeyError):
                note = None
                warnings.append(f"{source['title']}: cached source failed integrity validation; using the bundled excerpt.")
            if note:
                excerpts = _passages(note["text"], tokens)
                if query and not excerpts:
                    continue
                if not excerpts:
                    excerpts = [{"text": note["text"][:MAX_EXCERPT_CHARS], "line_start": 1,
                        "line_end": note["text"][:MAX_EXCERPT_CHARS].count("\n") + 1,
                        "truncated": len(note["text"]) > MAX_EXCERPT_CHARS, "basis": "source_excerpt"}]
                results.append({"source_id": source_id, "title": source["title"], "note_id": note["id"],
                    "url": f"https://github.com/{REPOSITORY}/blob/{note['revision']}/{source['path']}",
                    "revision": note["revision"], "sha256": note["sha256"], "retrieved_at": note["retrieved_at"],
                    "commit_at": note["commit_at"], "review_state": "unreviewed_source", "cache_state": "cached",
                    "changed_since_bundled": note["sha256"] != source["sha256"], "excerpts": excerpts,
                    "vehicle_applicability": "unverified", "freshness": _freshness(note["retrieved_at"])})
            else:
                excerpt = dict(source["excerpt"])
                if query and not any(token in excerpt["text"].casefold() for token in tokens):
                    continue
                results.append({key: source[key] for key in ("source_id", "title", "url", "revision", "sha256", "retrieved_at")}
                    | {"commit_at": None, "review_state": "bundled_reviewed_excerpt", "cache_state": "bundled",
                       "changed_since_bundled": None, "excerpts": [excerpt], "vehicle_applicability": "unverified",
                       "freshness": _freshness(source["retrieved_at"])})
        if not results:
            warnings.append("No locally available passage matched this search; no search terms were sent online.")
        latest_check = self._last_check(topic)
        if latest_check and latest_check["state"] in {"failed", "partial", "cancelled"}:
            warnings.append("The last source update " + {"failed": "failed", "partial": "completed only partially", "cancelled": "was cancelled"}[latest_check["state"]]
                            + ". Available cached sources and bundled excerpts remain in use.")
            for failure in latest_check.get("errors", [])[:len(selection["source_ids"]) + 1]:
                if not isinstance(failure, dict):
                    continue
                source = SOURCES.get(failure.get("source_id"), {})
                title = source.get("title", "Official source revision")
                message = failure.get("message")
                # Even a malformed/legacy local note cannot introduce raw proxy
                # exceptions or response bodies into these user-facing warnings.
                warnings.append(f"{title}: {message if isinstance(message, str) and message in PUBLIC_FAILURES else VALIDATION_FAILURE}")
        return {"version": VERSION, "topic": topic, "title": selection["title"], "query": query,
                "results": results, "capability_requirements": list(selection["requirements"]),
                "latest_check": latest_check, "warnings": warnings, "limitations": list(LIMITATIONS)}

    def refresh(self, topic, cancel_check=None, progress=None):
        checked_at, deadline = _utc(), time.monotonic() + REFRESH_TIMEOUT_S
        source_ids = TOPICS[topic]["source_ids"]
        check = {"topic": topic, "checked_at": checked_at, "revision": None, "commit_at": None,
                 "source_count": 0, "source_note_ids": [], "errors": [], "immutable": True}

        def fetch(url, limit):
            _cancel(cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Research update deadline exceeded.")
            body = self.fetcher(_validate_url(url), timeout_s=min(REQUEST_TIMEOUT_S, remaining),
                                max_bytes=limit, cancel_check=cancel_check)
            _cancel(cancel_check)
            if time.monotonic() > deadline:
                raise TimeoutError("Research update deadline exceeded.")
            if not isinstance(body, bytes) or len(body) > limit:
                raise ValueError("Research response exceeded its bound.")
            return body

        def error(source_id, exc):
            # Never propagate remote response bodies, redirect URLs, credentials
            # from proxy exceptions or untrusted exception strings into the UI.
            reason = (RATE_LIMIT_FAILURE if isinstance(exc, HTTPError) and exc.code == 429 else
                      NETWORK_FAILURE if isinstance(exc, (OSError, URLError, HTTPError, http.client.HTTPException)) else VALIDATION_FAILURE)
            check["errors"].append({"source_id": source_id, "message": reason})

        try:
            if progress:
                progress("checking_official_revision", 0, len(source_ids) + 2)
            try:
                ref = json.loads(fetch(REF_URL, MAX_METADATA_BYTES))
                if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
                    raise ValueError("Official revision response was invalid.")
                revision = ref.get("object", {}).get("sha")
                if ref.get("object", {}).get("type") != "commit" or not isinstance(revision, str) or not SHA.fullmatch(revision):
                    raise ValueError("Official revision response was invalid.")
                commit = json.loads(fetch(f"https://api.github.com/repos/{REPOSITORY}/git/commits/{revision}", MAX_METADATA_BYTES))
                if not isinstance(commit, dict) or commit.get("sha") != revision or not isinstance(commit.get("committer"), dict):
                    raise ValueError("Official commit identity did not match.")
                check["revision"], check["commit_at"] = revision, _date(commit.get("committer", {}).get("date"))
            except CancelledError:
                raise
            except (ValueError, TypeError, KeyError, OSError, URLError, http.client.HTTPException) as exc:
                _cancel(cancel_check)
                error("repository_revision", exc)
            if check["revision"]:
                for index, source_id in enumerate(source_ids):
                    _cancel(cancel_check)
                    source = SOURCES[source_id]
                    if progress:
                        progress("fetching_official_sources", index + 2, len(source_ids) + 2)
                    try:
                        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{check['revision']}/{source['path']}"
                        body = fetch(url, MAX_SOURCE_BYTES)
                        text = _text(body)
                        note = self.store.add_document("research_source", {
                            "source_id": source_id, "repository": REPOSITORY, "path": source["path"],
                            "revision": check["revision"], "commit_at": check["commit_at"], "retrieved_at": _utc(),
                            "sha256": hashlib.sha256(body).hexdigest(), "byte_size": len(body), "text": text,
                            "review_state": "unreviewed_source", "vehicle_applicability": "unverified", "immutable": True,
                        })
                        check["source_count"] += 1
                        check["source_note_ids"].append(note["id"])
                    except CancelledError:
                        raise
                    except (ValueError, TypeError, KeyError, OSError, URLError, http.client.HTTPException) as exc:
                        _cancel(cancel_check)
                        error(source_id, exc)
            _cancel(cancel_check)
            check["state"] = "updated" if not check["errors"] else "partial" if check["source_count"] else "failed"
            check = self.store.add_document("research_check", check)
            if progress:
                progress("research_sources_saved", len(source_ids) + 2, len(source_ids) + 2)
            return {**self.query(topic), "state": check["state"], "check": self._last_check(topic)}
        except CancelledError:
            check["state"] = "cancelled"
            self.store.add_document("research_check", check)
            raise
