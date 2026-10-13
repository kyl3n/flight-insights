"""Synthetic loopback MAVLink peers only; no Q22 hardware qualification claim."""
from concurrent.futures import CancelledError
import hashlib
import json
from pathlib import Path
import socket
import struct
import threading
import time
from uuid import uuid4

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mav

from q22_engine.acquisition import AcquisitionService, ALLOWED_MESSAGES, DEFAULT_LIMITS, _Link, _limits
from q22_engine.store import Store


class Peer:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(.005)
        local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        local.bind(('127.0.0.1', 0))
        self.target = local.getsockname()
        local.close()
        self.encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        self.decoder = mav.MAVLink(None)
        self.requests = []
        self.error = None
        self.stop_event = threading.Event()
        self.uid, self.armed, self.capabilities = 123, False, 131072
        self.boot_ms, self.boot_increment = 1000, 20
        self.heartbeats, self.clock = True, True
        self.parameters = [(b'TEST_A', 1.5, 9), (b'TEST_B', 7.0, 6)]
        self.omit_index = None
        self.readback_hook = None
        self.on_request = None
        self.logs = {7: b'\xa3\x95' + bytes(range(250)) * 2}
        self.log_hook = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @property
    def connection(self):
        return {'local_address': '127.0.0.1', 'local_port': self.target[1],
                'remote_address': '127.0.0.1', 'remote_port': self.socket.getsockname()[1],
                'system_id': 1, 'component_id': 1, 'expected_uid': '123', 'feed_origin': 'direct_bidirectional'}

    def send(self, message):
        data = message.pack(self.encoder)
        self.encoder.seq = (self.encoder.seq + 1) % 256
        self.socket.sendto(data, self.target)

    def status(self):
        if self.heartbeats:
            self.send(mav.MAVLink_heartbeat_message(2, 3, 128 if self.armed else 0, 0, 3, 3))
        if self.clock:
            self.send(mav.MAVLink_system_time_message(1700000000000000, self.boot_ms))
            self.boot_ms += self.boot_increment

    def value(self, index):
        name, value, kind = self.parameters[index]
        self.send(mav.MAVLink_param_value_message(name, value, kind, len(self.parameters), index))

    def run(self):
        try:
            self._run()
        except BaseException as error:
            self.error = error

    def receive(self):
        return self.socket.recvfrom(65535)

    def _run(self):
        last_status = 0
        while not self.stop_event.is_set():
            if time.monotonic() - last_status >= .02:
                self.status()
                last_status = time.monotonic()
            try:
                payload, _ = self.receive()
            except (socket.timeout, ConnectionResetError):
                # Windows reports ICMP port-unreachable for status packets sent
                # before the acquisition socket binds. The next receive remains
                # usable; this synthetic peer must keep serving later requests.
                continue
            for message in self.decoder.parse_buffer(payload) or []:
                self.requests.append(message)
                name = message.get_type()
                if self.on_request and self.on_request(message):
                    continue
                if name == 'COMMAND_LONG':
                    self.send(mav.MAVLink_autopilot_version_message(self.capabilities, 1, 0, 0, 1,
                        [0] * 8, [0] * 8, [0] * 8, 1, 1, self.uid))
                elif name == 'PARAM_REQUEST_LIST':
                    if self.readback_hook:
                        self.readback_hook(message)
                    else:
                        for i in range(len(self.parameters)):
                            if i != self.omit_index:
                                self.value(i)
                elif name == 'PARAM_REQUEST_READ':
                    self.value(message.param_index)
                elif name == 'LOG_REQUEST_LIST':
                    if not self.logs:
                        self.send(mav.MAVLink_log_entry_message(0, 0, 0, 0, 0))
                    for log_id, data in self.logs.items():
                        if message.start <= log_id <= message.end:
                            self.send(mav.MAVLink_log_entry_message(log_id, len(self.logs), max(self.logs), 1700000000, len(data)))
                elif name == 'LOG_REQUEST_DATA':
                    if self.log_hook and self.log_hook(message):
                        continue
                    data = self.logs[message.id]
                    if message.ofs >= len(data):
                        self.send(mav.MAVLink_log_data_message(message.id, message.ofs, 0, [0] * 90))
                    else:
                        for position in range(message.ofs, min(len(data), message.ofs + message.count), 90):
                            part = data[position:min(position + 90, message.ofs + message.count)]
                            self.send(mav.MAVLink_log_data_message(message.id, position, len(part), list(part) + [0] * (90 - len(part))))

    def close(self):
        self.stop_event.set()
        self.thread.join(2)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError('Synthetic acquisition peer did not stop.')
        if self.error is not None:
            raise RuntimeError(f'Synthetic acquisition peer failed: {self.error}') from self.error


@pytest.fixture
def peer():
    value = Peer()
    yield value
    value.close()


@pytest.fixture
def service(tmp_path):
    return AcquisitionService(Store(tmp_path / 'catalog'))


def params(service, peer, **extra):
    value = {'connection': peer.connection, 'aircraft_id': 'synthetic-test-aircraft', 'synthetic': True,
             'limits': {'timeout_s': 3.0, 'bootstrap_timeout_s': .6, 'heartbeat_timeout_s': .15,
                        'boot_timeout_s': .2, 'identity_timeout_s': .6, 'retry_wait_s': .07,
                        'request_interval_s': .01, 'settle_s': .04, 'retries': 2}, **extra}
    value.update(operation_id=str(uuid4()), applicability_fingerprint=service.preview(value)['applicability_fingerprint'])
    return value


def durable_limits():
    """Production freshness gates for fixtures that also persist real jobs/catalogs.

    Transport fault tests keep params()'s explicit subsecond budgets. A routine
    disk/scheduler pause must not accidentally become their fault injection in
    unrelated durable workflow tests. Total work and retries remain bounded.
    """
    return {**DEFAULT_LIMITS, 'timeout_s': 20.0, 'retry_wait_s': .25,
            'request_interval_s': .02, 'settle_s': .08, 'retries': 2}


def delay_first_heartbeat_persistence(monkeypatch, delay_s=.25):
    """Pause after each link's first heartbeat write, preserving receipt clocks.

    This models evidence persistence/descheduling on a loaded runner. It does
    not fabricate fresh timestamps, inject telemetry into gates, or retry an
    acquisition. The peer continues sending ordinary loopback MAVLink frames.
    """
    original = _Link._record
    delays = []

    def record(link, direction, payload, endpoint, mono_ns, utc_us):
        reference = original(link, direction, payload, endpoint, mono_ns, utc_us)
        if direction == 0 and link.heartbeat is None:
            messages = mav.MAVLink(None).parse_buffer(payload) or []
            if any(message.get_type() == 'HEARTBEAT' for message in messages):
                started = time.monotonic()
                time.sleep(delay_s)
                delays.append({'receipt_monotonic_ns': mono_ns,
                               'elapsed_s': time.monotonic() - started})
        return reference

    monkeypatch.setattr(_Link, '_record', record)
    return delays


def run(service, peer, method='acquisition.parameters.capture', **extra):
    return service.dispatch(method, params(service, peer, **extra))['acquisition']


def listing(service, peer):
    result = run(service, peer, 'acquisition.logs.list', confirm_log_transfer_side_effects=True)
    assert result['state'] == 'complete', result
    return result


@pytest.mark.parametrize('durable', [False, True], ids=['short_gate_stops', 'production_gate_completes'])
def test_persistence_pause_preserves_receipt_age_and_fixture_budget_contract(service, peer, monkeypatch, durable):
    delays = delay_first_heartbeat_persistence(monkeypatch)
    arguments = params(service, peer, **({'limits': durable_limits()} if durable else {}))
    result = service.dispatch('acquisition.parameters.capture', arguments)['acquisition']
    assert len(delays) == 1 and delays[0]['elapsed_s'] >= .25
    evidence = result['transport_evidence']
    if durable:
        assert result['state'] == 'complete', result
        assert result['parameter_capture']['decoded_count'] == 2
        for key in ('heartbeat_timeout_s', 'boot_timeout_s', 'bootstrap_timeout_s', 'identity_timeout_s'):
            assert arguments['limits'][key] == DEFAULT_LIMITS[key]
        heartbeat = evidence['initial_gate_observations']['heartbeat']
        assert int(heartbeat['received_monotonic_ns']) >= delays[0]['receipt_monotonic_ns']
    else:
        assert result['state'] == 'partial', result
        assert result['interruption_reason'] == 'Fresh disarmed autopilot heartbeat is unavailable.'
        heartbeat = evidence['final_gate_observations']['heartbeat']
        assert int(heartbeat['received_monotonic_ns']) == delays[0]['receipt_monotonic_ns']
        assert not any(message.get_type().startswith('PARAM_REQUEST') for message in peer.requests)


def test_synthetic_peer_survives_windows_port_unreachable_reset(service, monkeypatch):
    original = Peer.receive
    injected = False

    def reset_once(peer):
        nonlocal injected
        if not injected:
            injected = True
            raise ConnectionResetError(10054, 'Synthetic Windows UDP ICMP reset')
        return original(peer)

    monkeypatch.setattr(Peer, 'receive', reset_once)
    peer = Peer()
    try:
        result = run(service, peer, role='preflight')
        assert injected and result['state'] == 'complete', result
        assert result['parameter_capture']['decoded_count'] == 2
    finally:
        peer.close()


def test_synthetic_peer_surfaces_unexpected_socket_failure(monkeypatch):
    failure = OSError('Synthetic unexpected receive failure')

    def fail(_peer):
        raise failure

    monkeypatch.setattr(Peer, 'receive', fail)
    peer = Peer()
    peer.thread.join(2)
    with pytest.raises(RuntimeError, match='unexpected receive failure') as raised:
        peer.close()
    assert raised.value.__cause__ is failure


def test_parameter_readback_missing_index_retry_and_raw_provenance(service, peer):
    peer.omit_index = 1
    result = run(service, peer, role='preflight')
    assert result['state'] == 'complete', result
    capture = result['parameter_capture']
    assert capture['completeness'] == 'complete_for_scope' and capture['decoded_count'] == 2
    assert not capture['atomic'] and capture['flight_time_applicability'] == 'unknown'
    assert capture['full_readback_requested'] and capture['missing_index_retry_examples'] == [1]
    assert {message.get_msgId() for message in peer.requests} <= ALLOWED_MESSAGES
    assert any(message.get_type() == 'PARAM_REQUEST_READ' and message.param_index == 1 for message in peer.requests)
    assert all(source['acquisition_method'] == 'app_owned_readonly_mavlink_acquisition' for source in result['source_refs'])
    for source in result['source_refs']:
        assert hashlib.sha256(service.store.raw_path(source['sha256']).read_bytes()).hexdigest() == source['sha256']


def test_operation_retry_is_bound_and_never_resends(service, peer):
    arguments = params(service, peer)
    first = service.dispatch('acquisition.parameters.capture', arguments)
    count = len(peer.requests)
    repeated = service.dispatch('acquisition.parameters.capture', arguments)
    assert repeated['operation_replayed'] and first['acquisition']['id'] == repeated['acquisition']['id']
    assert len(peer.requests) == count
    with pytest.raises(ValueError, match='another acquisition payload'):
        service.dispatch('acquisition.parameters.capture', arguments | {'role': 'postflight'})


@pytest.mark.parametrize('condition', ['armed', 'wrong_uid', 'missing_boot'])
def test_bootstrap_never_sends_parameter_or_log_requests_without_eligibility(service, peer, condition):
    if condition == 'armed':
        peer.armed = True
    elif condition == 'wrong_uid':
        peer.uid = 456
    else:
        peer.clock = False
    result = run(service, peer)
    assert result['state'] != 'complete'
    assert not any(message.get_type() in {'PARAM_REQUEST_LIST', 'PARAM_REQUEST_READ', 'LOG_REQUEST_LIST', 'LOG_REQUEST_DATA'} for message in peer.requests)
    assert result['retained_files']


@pytest.mark.parametrize('failure', ['arm', 'boot', 'stale'])
def test_mid_readback_live_gate_revokes_and_preserves_partial_evidence(service, peer, failure):
    def during_request(_):
        peer.value(0)
        if failure == 'arm':
            peer.armed = True
            peer.status()
        elif failure == 'boot':
            peer.boot_ms = 1
            peer.status()
        else:
            peer.heartbeats = False
            peer.on_request = lambda message: message.get_type() == 'PARAM_REQUEST_READ'
    peer.readback_hook = during_request
    result = run(service, peer)
    assert result['state'] in {'partial', 'interrupted'}
    assert result['parameter_capture']['completeness'] != 'complete_for_scope'
    assert result['interruption_reason']
    assert result['source_refs']


def test_parameter_conflicting_retransmission_cannot_be_complete(service, peer):
    def conflict(_):
        peer.value(0)
        peer.value(1)
        peer.send(mav.MAVLink_param_value_message(b'TEST_A', 9.0, 9, 2, 0))
    peer.readback_hook = conflict
    result = run(service, peer)
    assert result['state'] == 'partial'
    assert result['parameter_capture']['consistency'] == 'conflict'


def test_log_listing_actual_ids_download_eof_sha_and_cleanup(service, peer):
    index = listing(service, peer)
    assert index['entries'][0]['log_id'] == 7 and index['entries'][0]['remote_filename'] is None
    result = run(service, peer, 'acquisition.logs.download', listing_id=index['id'], log_id=7,
                 confirm_log_transfer_side_effects=True)
    assert result['state'] == 'complete', result
    download = result['download']
    assert download['transfer_complete'] and download['received_bytes'] == len(peer.logs[7])
    assert download['sha256'] == hashlib.sha256(peer.logs[7]).hexdigest()
    assert download['logger_closed'] == 'unknown' and download['association_state'] == 'provisional'
    assert result['transport_evidence']['log_request_end_sent']
    assert not result['transport_evidence']['logging_resumption_verified']
    source = next(source for source in result['source_refs'] if source['role'] == 'onboard_log_candidate')
    assert service.store.raw_path(source['sha256']).read_bytes() == peer.logs[7]


def test_empty_inventory_is_not_a_fake_log_zero(service, peer):
    peer.logs = {}
    result = listing(service, peer)
    assert result['entries'] == [] and result['listing_complete']


def test_log_transfer_requires_explicit_side_effect_acknowledgment(service, peer):
    with pytest.raises(ValueError, match='confirmation'):
        run(service, peer, 'acquisition.logs.list')
    assert peer.requests == []


@pytest.mark.parametrize('failure', ['early_eof', 'overlap', 'outside', 'wrong_id', 'no_eof'])
def test_download_rejects_inconsistent_offset_bytes_and_truncation(service, peer, failure):
    index = listing(service, peer)
    def corrupt(message):
        if failure == 'no_eof':
            return message.ofs >= len(peer.logs[7])
        if failure == 'early_eof':
            peer.send(mav.MAVLink_log_data_message(7, 0, 0, [0] * 90))
        elif failure == 'overlap':
            peer.send(mav.MAVLink_log_data_message(7, 0, 10, [1] * 90))
            peer.send(mav.MAVLink_log_data_message(7, 0, 10, [2] * 90))
        elif failure == 'outside':
            peer.send(mav.MAVLink_log_data_message(7, len(peer.logs[7]) + 1, 1, [1] * 90))
        else:
            peer.send(mav.MAVLink_log_data_message(8, 0, 1, [1] * 90))
        return True
    peer.log_hook = corrupt
    result = run(service, peer, 'acquisition.logs.download', listing_id=index['id'], log_id=7,
                 confirm_log_transfer_side_effects=True)
    assert result['state'] != 'complete' and not result['download']['transfer_complete']
    assert result['retained_files'] and result['interruption_reason']


def test_download_revalidates_observed_size_before_data_request(service, peer):
    index = listing(service, peer)
    peer.logs[7] += b'grew'
    before = len([m for m in peer.requests if m.get_type() == 'LOG_REQUEST_DATA'])
    result = run(service, peer, 'acquisition.logs.download', listing_id=index['id'], log_id=7,
                 confirm_log_transfer_side_effects=True)
    assert result['state'] == 'partial'
    assert len([m for m in peer.requests if m.get_type() == 'LOG_REQUEST_DATA']) == before


def test_cancellation_retains_local_partial_files_and_terminal_catalog(service, peer):
    peer.readback_hook = lambda _: peer.value(0)
    cancellation = threading.Event()
    def progress(stage, completed, total):
        if stage == 'receiving_parameters' and completed:
            cancellation.set()
    with pytest.raises(CancelledError):
        service.dispatch('acquisition.parameters.capture', params(service, peer), cancellation.is_set, progress)
    result = service.dispatch('acquisition.list', {})['acquisitions'][0]
    full = service.dispatch('acquisition.detail', {'acquisition_id': result['id']})['acquisition']
    assert full['state'] == 'interrupted'
    assert {Path(item['path']).name for item in full['retained_files']} == {'exchange.mavudp', 'parameters.snapshot.json'}
    assert all(Path(item['path']).exists() for item in full['retained_files'])


def test_external_gate_blocks_before_any_wire_request(service, peer):
    service.external_gate = lambda kind, connection: {'allowed': False, 'reason': 'synthetic external armed gate'}
    result = run(service, peer)
    assert result['state'] != 'complete' and 'external armed' in result['interruption_reason']
    assert peer.requests == []


def test_setup_error_releases_process_lock(service, peer, monkeypatch):
    original = Path.mkdir
    def fail(path, *args, **kwargs):
        if path.parent == service.directory:
            raise OSError('synthetic mkdir failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', fail)
    with pytest.raises(OSError, match='mkdir failure'):
        run(service, peer)
    monkeypatch.setattr(Path, 'mkdir', original)
    assert not service._lock.locked()
    assert run(service, peer)['state'] == 'complete'


def test_bytewise_value_uses_original_wire_bytes_and_cast_precision_is_withheld():
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    raw = struct.pack('<I', 0x7FC01234)
    message = mav.MAVLink_param_value_message(b'TEST_BITS', struct.unpack('<f', raw)[0], 5, 1, 0)
    frame = message.pack(encoder)
    decoded = mav.MAVLink(None).parse_char(frame)
    value, encoding, error = AcquisitionService._parameter_value(decoded, 16)
    payload_value = struct.unpack('<I', bytes(decoded.get_msgbuf())[10:14])[0]
    assert value == str(payload_value) and encoding == 'mavlink_bytewise_decoded' and error is None
    huge = mav.MAVLink_param_value_message(b'TEST_HUGE', float(2**24), 6, 1, 0)
    _, encoding, error = AcquisitionService._parameter_value(huge, 131072)
    assert encoding is None and 'precision' in error


def test_count_change_after_initial_full_response_marks_snapshot_inconsistent(service, peer):
    peer.parameters = [(b'TEST_A', 1.0, 9)]
    def change(_):
        peer.value(0)
        peer.send(mav.MAVLink_param_value_message(b'TEST_A', 1.0, 9, 2, 0))
    peer.readback_hook = change
    result = run(service, peer)
    assert result['state'] == 'partial'
    assert result['parameter_capture']['completeness'] == 'partial'
    assert result['parameter_capture']['consistency'] == 'conflict'
    source = next(source for source in result['source_refs'] if source['role'] == 'parameter_readback_snapshot')
    snapshot = json.loads(service.store.raw_path(source['sha256']).read_text())
    assert snapshot['completeness'] == 'partial' and snapshot['consistency'] == 'conflict'
    assert snapshot['flight_time_applicability'] == 'unknown'


def test_armed_transition_forbids_log_cleanup_request(service, peer):
    def interrupt(message):
        if message.get_type() == 'LOG_REQUEST_LIST':
            peer.armed = True
            peer.status()
            return True
        return False
    peer.on_request = interrupt
    result = run(service, peer, 'acquisition.logs.list', confirm_log_transfer_side_effects=True)
    assert result['state'] == 'interrupted'
    assert not result['transport_evidence']['log_request_end_sent']
    assert result['transport_evidence']['recovery_warning']
    assert not any(message.get_type() == 'LOG_REQUEST_END' for message in peer.requests)


def test_completed_window_identical_late_retransmission_is_deduplicated(service, peer):
    index = listing(service, peer)
    def duplicate_at_eof(message):
        if message.ofs == len(peer.logs[7]):
            peer.send(mav.MAVLink_log_data_message(7, 0, 90, list(peer.logs[7][:90])))
        return False
    peer.log_hook = duplicate_at_eof
    result = run(service, peer, 'acquisition.logs.download', listing_id=index['id'], log_id=7,
                 confirm_log_transfer_side_effects=True)
    assert result['state'] == 'complete'
    assert result['download']['received_bytes'] == len(peer.logs[7])


@pytest.mark.parametrize('conflict', [False, True])
def test_final_inventory_recheck_validates_late_data(service, peer, conflict):
    index = listing(service, peer)
    transferred = False

    def after_transfer(message):
        nonlocal transferred
        if message.get_type() == 'LOG_REQUEST_DATA':
            transferred = True
        elif message.get_type() == 'LOG_REQUEST_LIST' and transferred:
            byte = 0 if conflict else peer.logs[7][0]
            peer.send(mav.MAVLink_log_data_message(7, 0, 1, [byte] + [0] * 89))
        return False

    peer.on_request = after_transfer
    result = run(service, peer, 'acquisition.logs.download', listing_id=index['id'], log_id=7,
                 confirm_log_transfer_side_effects=True)
    assert result['download']['transfer_complete'] is (not conflict)
    assert result['state'] == ('partial' if conflict else 'complete')
    if conflict:
        assert 'Conflicting LOG_DATA after EOF' in result['interruption_reason']


def test_cleanup_contradiction_keeps_coverage_artifact_consistent(service, peer, monkeypatch):
    index = listing(service, peer)
    original = _Link.cleanup

    def conflicting_cleanup(link):
        if link.late_data_validator:
            from q22_engine.acquisition import AcquisitionStopped
            try:
                link.late_data_validator(mav.MAVLink_log_data_message(7, 0, 1, [0] * 90))
            except AcquisitionStopped:
                pass
        original(link)

    monkeypatch.setattr(_Link, 'cleanup', conflicting_cleanup)
    result = run(service, peer, 'acquisition.logs.download', listing_id=index['id'], log_id=7,
                 confirm_log_transfer_side_effects=True)
    assert result['state'] == 'partial' and not result['download']['transfer_complete']
    source = next(row for row in result['source_refs'] if row['role'] == 'download_byte_coverage')
    coverage = json.loads(service.store.raw_path(source['sha256']).read_text())
    assert not coverage['transfer_complete']


def test_unknown_encoding_and_c_cast_integer_precision_never_claim_complete(service, peer):
    peer.parameters = [(b'BIG_INTEGER', float(2**24), 6)]
    result = run(service, peer)
    assert result['state'] == 'partial'
    assert result['parameter_capture']['completeness'] == 'partial'
    assert any('precision' in item for item in result['parameter_capture']['semantic_errors'])


def test_dead_owner_attempt_recovery_is_visible_without_network_replay(service, peer):
    operation_id = str(uuid4())
    directory = service.directory / str(uuid4())
    directory.mkdir()
    (directory / 'download.partial.bin').write_bytes(b'synthetic partial bytes')
    document = {'operation_id': operation_id, 'kind': 'log_download', 'aircraft_id': 'synthetic',
                'connection': peer.connection, 'synthetic': True, 'state': 'in_progress',
                'owner_process': {'pid': 2147483647, 'created_at': 0}, 'source_refs': [], 'limitations': []}
    (directory / 'attempt.json').write_text(json.dumps(document))
    with service.store._connect() as db:
        db.execute('INSERT INTO acquisition_operations VALUES (?,?,NULL)', (operation_id, 'synthetic-bound-payload'))
    reopened = AcquisitionService(service.store)
    result = reopened.dispatch('acquisition.list', {})['acquisitions'][0]
    assert result['state'] == 'interrupted' and result['recovered_after_process_loss']
    assert result['retained_files'][0]['durability'] == 'crash_recovered_unverified'
    assert not result['retained_files'][0]['sealed']
    assert peer.requests == []
    assert AcquisitionService(service.store).dispatch('acquisition.list', {})['total'] == 1


def test_live_owner_attempt_is_not_mislabelled_interrupted(service, peer):
    import os
    import psutil
    operation_id = str(uuid4())
    directory = service.directory / str(uuid4())
    directory.mkdir()
    document = {'operation_id': operation_id, 'owner_process': {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}}
    (directory / 'attempt.json').write_text(json.dumps(document))
    with service.store._connect() as db:
        db.execute('INSERT INTO acquisition_operations VALUES (?,?,NULL)', (operation_id, 'synthetic-payload'))
    assert AcquisitionService(service.store).dispatch('acquisition.list', {})['total'] == 0


def test_publication_error_releases_process_lock_and_files_survive(service, peer, monkeypatch):
    original = service.store.add_document
    def fail(kind, data):
        if kind == 'acquisition':
            raise OSError('synthetic catalog failure')
        return original(kind, data)
    monkeypatch.setattr(service.store, 'add_document', fail)
    with pytest.raises(OSError, match='catalog failure'):
        run(service, peer)
    assert not service._lock.locked()
    assert list(service.directory.glob('*/exchange.mavudp'))
    monkeypatch.setattr(service.store, 'add_document', original)
    assert run(service, peer)['state'] == 'complete'


def test_bootstrap_version_command_is_exactly_allowlisted(service, peer):
    result = run(service, peer)
    assert result['state'] == 'complete'
    for message in peer.requests:
        assert message.target_system == 1 and message.target_component == 1
        if message.get_type() == 'COMMAND_LONG':
            assert message.command == 512 and message.param1 == 148 and message.confirmation == 0
            assert all(getattr(message, f'param{i}') == 0 for i in range(2, 8))
        assert message.get_type() not in {'HEARTBEAT', 'PARAM_SET', 'LOG_ERASE', 'SET_MODE', 'COMMAND_INT'}


def test_metadata_replacement_failure_preserves_prior_complete_json(tmp_path, monkeypatch):
    import q22_engine.acquisition as module
    path = tmp_path / 'attempt.json'
    module._write_metadata(path, {'state': 'in_progress'})
    def fail(*args):
        raise OSError('synthetic atomic replace failure')
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(OSError):
        module._write_metadata(path, {'state': 'complete'})
    assert json.loads(path.read_text()) == {'state': 'in_progress'}
    assert list(tmp_path.iterdir()) == [path]


def test_corrupt_recovery_metadata_is_reported_without_identity_claim(service):
    directory = service.directory / str(uuid4())
    directory.mkdir()
    (directory / 'attempt.json').write_text('{broken')
    (directory / 'download.partial.bin').write_bytes(b'partial')
    result = AcquisitionService(service.store).dispatch('acquisition.list', {})
    assert result['acquisitions'] == []
    assert directory.name in result['recovery_warnings'][0]
    assert (directory / 'download.partial.bin').read_bytes() == b'partial'
