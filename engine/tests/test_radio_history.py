"""Loopback and injected SkyPulse replies are synthetic, not vendor fixtures."""
from concurrent.futures import CancelledError
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from uuid import uuid4

import pytest

from q22_engine.radio_history import RadioHistoryService, _http_get
from q22_engine.store import Store


@contextmanager
def server(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            respond(self)
        def log_message(self, *_args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'id': 'SYNTHETIC HTTP FIXTURE', 'kind': 'skypulse', 'host': '127.0.0.1', 'port': httpd.server_port}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def reply(body=b'{"fixture":1}', **changes):
    return {'body': body, 'http_status': 200, 'complete_response': True, 'body_representation': 'http_entity',
            'content_type': 'application/json', 'content_encoding': 'identity', 'json_valid': True,
            'error': None, 'cancelled': False, **changes}


SOURCE = {'id': 'SYNTHETIC NO NETWORK', 'kind': 'skypulse', 'host': '127.0.0.1', 'port': 54321}


def capture_args(source=SOURCE, **changes):
    return {'source': source, 'duration_s': 1, 'interval_ms': 1000, 'timeout_ms': 100, **changes}


def test_real_loopback_preserves_exact_bodies_failure_categories_and_receipt_clocks(tmp_path):
    observed = []
    bodies = {'/scatter_data.json': b'{"unknown_firmware_field":[1,2]}', '/events.json': b'not JSON; fixture only'}
    def respond(handler):
        observed.append((handler.path, dict(handler.headers)))
        body = bodies[handler.path]
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Set-Cookie', 'do-not-store-this-header')
        handler.end_headers()
        handler.wfile.write(body)
    service = RadioHistoryService(Store(tmp_path))
    with server(respond) as source:
        result = service.dispatch('radio.capture', capture_args(source))['capture']
    record = service.dispatch('radio.detail', {'capture_id': result['capture_id']})['capture']
    assert record['state'] == 'partial'
    assert record['poll_count'] == 2
    assert record['failure_count'] == 1
    assert [poll['state'] for poll in record['polls']] == ['json_response', 'malformed_json']
    assert record['normalization'] == 'raw_only_schema_unvalidated'
    assert len(observed) == 2
    assert all('Authorization' not in headers for _, headers in observed)
    assert 'do-not-store-this-header' not in json.dumps(record)
    for poll in record['polls']:
        body = bodies[poll['endpoint']]
        assert service.store.raw_path(poll['body_source']['sha256']).read_bytes() == body
        assert poll['response_sha256'] == hashlib.sha256(body).hexdigest()
        assert poll['body_source']['acquisition_method'] == 'app_owned_readonly_radio_capture'
        assert poll['started_utc'].endswith('+00:00')
        assert int(poll['received_monotonic_ns']) >= int(poll['started_monotonic_ns'])
        assert poll['schema_validated'] is False
        assert poll['sample_times_validated'] is False


def test_http_error_and_redirect_are_recorded_without_following_location(tmp_path):
    visited = []
    def respond(handler):
        visited.append(handler.path)
        handler.send_response(302 if handler.path == '/scatter_data.json' else 403)
        handler.send_header('Location', '/not-a-documented-endpoint')
        handler.send_header('Content-Length', '2')
        handler.end_headers()
        handler.wfile.write(b'{}')
    service = RadioHistoryService(Store(tmp_path))
    with server(respond) as source:
        record_id = service.capture(capture_args(source))['capture']['capture_id']
    record = service.dispatch('radio.detail', {'capture_id': record_id})['capture']
    assert visited == ['/scatter_data.json', '/events.json']
    assert [p['http_status'] for p in record['polls']] == [302, 403]
    assert all(p['state'] == 'http_error' for p in record['polls'])
    assert record['gaps'] == []


def test_slow_or_truncated_body_is_partial_and_retained_without_json_success(tmp_path):
    def respond(handler):
        handler.send_response(200)
        handler.send_header('Content-Length', '100')
        handler.end_headers()
        handler.wfile.write(b'{"partial":')
        handler.wfile.flush()
        time.sleep(.25)
    service = RadioHistoryService(Store(tmp_path))
    with server(respond) as source:
        capture_id = service.capture(capture_args(source))['capture']['capture_id']
    record = service.dispatch('radio.detail', {'capture_id': capture_id})['capture']
    assert record['state'] == 'partial'
    assert all(p['state'] == 'transport_incomplete' for p in record['polls'])
    for poll in record['polls']:
        assert poll['json_valid'] is False
        assert poll['complete_response'] is False
        assert service.store.raw_path(poll['body_source']['sha256']).read_bytes() == b'{"partial":'


def test_cancel_archives_received_partial_bytes_and_persists_cancelled_manifest(tmp_path):
    calls = []
    def fetch(source, endpoint, timeout, cancelled):
        calls.append(endpoint)
        return reply(b'partial received bytes', complete_response=False, json_valid=False,
                     error='capture_cancelled', cancelled=True)
    service = RadioHistoryService(Store(tmp_path), fetcher=fetch)
    with pytest.raises(CancelledError):
        service.capture(capture_args())
    captures = service.dispatch('radio.list', {})['captures']
    assert len(captures) == 1
    assert captures[0]['state'] == 'cancelled'
    record = service.dispatch('radio.detail', {'capture_id': captures[0]['capture_id']})['capture']
    assert len(calls) == 1
    assert record['poll_count'] == 1
    assert service.store.raw_path(record['polls'][0]['body_source']['sha256']).read_bytes() == b'partial received bytes'
    assert record['finished_utc']


def test_total_body_budget_stops_capture_and_preserves_truncated_original(tmp_path, monkeypatch):
    monkeypatch.setattr('q22_engine.radio_history.TOTAL_BODY_LIMIT', 5)
    calls = []
    def fetch(*args):
        calls.append(args[1])
        return reply(b'{"fixture":1}')
    service = RadioHistoryService(Store(tmp_path), fetcher=fetch)
    result = service.capture(capture_args())['capture']
    record = service.dispatch('radio.detail', {'capture_id': result['capture_id']})['capture']
    assert len(calls) == 1
    assert result['state'] == 'budget_exhausted'
    assert result['finished_utc']
    assert record['body_bytes'] == 5
    assert record['polls'][0]['json_valid'] is False
    assert service.store.raw_path(record['polls'][0]['body_source']['sha256']).read_bytes() == b'{"fix'


def test_restart_reads_interrupted_evidence_without_replaying_requests(tmp_path):
    calls = []
    service = RadioHistoryService(Store(tmp_path), fetcher=lambda *_: calls.append('unexpected'))
    capture_id = str(uuid4())
    service._directory(capture_id).mkdir()
    record = {'version': 1, 'capture_id': capture_id, 'source': SOURCE, 'state': 'running',
              'owner': {'pid': -1, 'created': 0}, 'started_utc': '2026-10-13T00:00:00+00:00',
              'polls': [], 'gaps': [], 'pending_poll': {'endpoint': '/events.json'}, 'poll_count': 0}
    service._save(record)
    restarted = RadioHistoryService(service.store, fetcher=lambda *_: calls.append('unexpected'))
    assert restarted.dispatch('radio.list', {})['captures'][0]['state'] == 'interrupted'
    restored = restarted.dispatch('radio.detail', {'capture_id': capture_id})['capture']
    assert restored['state'] == 'interrupted'
    assert restored['pending_poll']['endpoint'] == '/events.json'
    assert calls == []
    # Reading derives recovery state; prior raw manifest evidence is not rewritten.
    assert json.loads((service._directory(capture_id) / 'manifest.json').read_text())['state'] == 'running'


def test_adapter_failure_retains_durable_request_intent_and_failed_state(tmp_path):
    def fetch(*_):
        raise OSError('fixture adapter unavailable')
    service = RadioHistoryService(Store(tmp_path), fetcher=fetch)
    with pytest.raises(OSError):
        service.capture(capture_args())
    record = service.dispatch('radio.list', {})['captures'][0]
    assert record['state'] == 'failed'
    detail = service.dispatch('radio.detail', {'capture_id': record['capture_id']})['capture']
    assert detail['pending_poll']['endpoint'] == '/scatter_data.json'
    assert detail['polls'] == []


@pytest.mark.parametrize('changes', [
    {'duration_s': 301}, {'duration_s': True}, {'interval_ms': 1}, {'timeout_ms': 5001},
    {'source': {**SOURCE, 'path': '/set_config'}}, {'source': {**SOURCE, 'password': 'secret-fixture'}},
    {'source': {**SOURCE, 'headers': {'Authorization': 'secret-fixture'}}},
    {'source': {**SOURCE, 'host': 'http://user:secret-fixture@example.com'}},
    {'source': {**SOURCE, 'kind': 'udp'}}, {'endpoint': '/set_config'},
])
def test_invalid_scope_never_creates_capture_or_logs_credential_value(tmp_path, changes):
    service = RadioHistoryService(Store(tmp_path), fetcher=lambda *_: pytest.fail('No requests expected'))
    with pytest.raises(ValueError) as error:
        service.capture(capture_args(**changes))
    assert 'secret-fixture' not in str(error.value)
    assert service.dispatch('radio.list', {})['captures'] == []


def test_manifest_identity_and_symlink_rejected_without_execution(tmp_path):
    service = RadioHistoryService(Store(tmp_path))
    with pytest.raises(ValueError):
        service.dispatch('radio.detail', {'capture_id': '../../outside'})
    capture_id = str(uuid4())
    directory = service._directory(capture_id)
    directory.mkdir()
    (directory / 'manifest.json').write_text(json.dumps({'version': 1, 'capture_id': str(uuid4())}))
    with pytest.raises(ValueError, match='identity'):
        service.dispatch('radio.detail', {'capture_id': capture_id})
    assert service.dispatch('radio.list', {})['errors']


def test_http_header_slow_drip_is_bounded_and_no_header_bytes_are_archived():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    def drip():
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            for byte in b'HTTP/1.1 200 OK\r\nSet-Cookie: secret-fixture\r\n':
                try:
                    connection.sendall(bytes([byte]))
                except OSError:
                    break
                time.sleep(.015)
    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        result = _http_get({**SOURCE, 'port': listener.getsockname()[1]}, '/events.json', .1, lambda: False)
        assert time.monotonic() - started < .6
        assert result['body'] == b''
        assert result['complete_response'] is False
        assert 'secret-fixture' not in json.dumps({k: v for k, v in result.items() if k != 'body'})
    finally:
        listener.close()
        thread.join(timeout=1)


def test_schedule_overrun_counts_only_skipped_cycles_inside_capture_window(tmp_path):
    calls = []
    def fetch(*args):
        calls.append(args[1])
        time.sleep(.55)
        return reply()
    service = RadioHistoryService(Store(tmp_path), fetcher=fetch)
    result = service.capture(capture_args(duration_s=2))['capture']
    assert len(calls) == 2
    assert result['missed_poll_cycles'] == 1
    assert result['state'] == 'partial'


def test_response_archival_must_match_bytes_that_were_observed(tmp_path, monkeypatch):
    service = RadioHistoryService(Store(tmp_path), fetcher=lambda *_: reply())
    original = service.store.archive_file
    def mismatched(path, **kwargs):
        result = original(path, **kwargs)
        result['sha256'] = '0' * 64
        return result
    monkeypatch.setattr(service.store, 'archive_file', mismatched)
    with pytest.raises(ValueError, match='changed before archival'):
        service.capture(capture_args())
    result = service.dispatch('radio.list', {})['captures'][0]
    assert result['state'] == 'failed'
    assert result['poll_count'] == 0
    assert result['pending_poll']['endpoint'] == '/scatter_data.json'


def test_http_connection_refusal_is_transport_failure_without_response_hash(tmp_path):
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
        service = RadioHistoryService(Store(tmp_path))
        result = service.capture(capture_args({**SOURCE, 'port': port}))['capture']
    detail = service.dispatch('radio.detail', {'capture_id': result['capture_id']})['capture']
    assert detail['failure_count'] == 2
    for poll in detail['polls']:
        assert poll['state'] == 'transport_incomplete'
        assert poll['response_sha256'] is None
        assert poll['http_status'] is None
        assert 'body_source' not in poll


def test_deep_json_is_preserved_as_unvalidated_without_failing_capture(tmp_path):
    body = b'[' * 12000 + b'0' + b']' * 12000
    def respond(handler):
        handler.send_response(200)
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    service = RadioHistoryService(Store(tmp_path))
    with server(respond) as source:
        result = service.capture(capture_args(source))['capture']
    assert result['state'] == 'partial'
    record = service.dispatch('radio.detail', {'capture_id': result['capture_id']})['capture']
    assert all(poll['state'] == 'json_unvalidated' for poll in record['polls'])
    assert all(poll['json_parse_error'] == 'parser_depth_limit' for poll in record['polls'])
    assert service.store.raw_path(record['polls'][0]['body_source']['sha256']).read_bytes() == body


def test_manifest_fsync_failure_closes_temporary_before_cleanup(tmp_path, monkeypatch):
    from q22_engine import radio_history
    from pathlib import Path
    service = RadioHistoryService(Store(tmp_path))
    capture_id = str(uuid4())
    directory = service._directory(capture_id)
    directory.mkdir()
    record = {'version': 1, 'capture_id': capture_id, 'state': 'running'}
    service._save(record)
    original_payload = (directory / 'manifest.json').read_bytes()
    original_temp = radio_history.tempfile.NamedTemporaryFile
    original_unlink = Path.unlink
    streams = []
    def temporary(*args, **kwargs):
        stream = original_temp(*args, **kwargs)
        streams.append(stream)
        return stream
    def unlink(path, *args, **kwargs):
        if path.name.startswith('.manifest-'):
            assert streams[-1].closed, 'Windows cannot unlink an open temporary'
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(radio_history.tempfile, 'NamedTemporaryFile', temporary)
    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(radio_history.os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('fixture disk full')))
    with pytest.raises(OSError, match='disk full'):
        service._save({**record, 'state': 'complete'})
    assert (directory / 'manifest.json').read_bytes() == original_payload
    assert list(directory.glob('.manifest-*')) == []


def test_radio_manifest_uses_bounded_windows_sharing_violation_retry(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from q22_engine import recorder
    service = RadioHistoryService(Store(tmp_path))
    capture_id = str(uuid4())
    directory = service._directory(capture_id)
    directory.mkdir()
    record = {'version': 1, 'capture_id': capture_id, 'state': 'running'}
    service._save(record)
    original_replace = os.replace
    attempts = []
    def replacing(source, target):
        attempts.append(str(target))
        if len(attempts) == 1:
            error = PermissionError('fixture sharing violation')
            error.winerror = 32
            raise error
        original_replace(source, target)
    monkeypatch.setattr(recorder, 'os', SimpleNamespace(name='nt', replace=replacing))
    service._save({**record, 'state': 'complete'})
    assert len(attempts) == 2
    assert json.loads((directory / 'manifest.json').read_text())['state'] == 'complete'
    assert list(directory.glob('.manifest-*')) == []
