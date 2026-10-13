"""Synthetic recorder callbacks; these tests are not Q22 hardware evidence."""
from copy import deepcopy
import json
import sqlite3
import time
from uuid import uuid4

import pytest

from q22_engine.collection import CollectionService
from q22_engine.catalog import CATALOG_VERSION
from q22_engine.store import Store


@pytest.fixture
def service(tmp_path):
    return CollectionService(Store(tmp_path / 'catalog'))


def profile(**connection_changes):
    return {'aircraft_id': 'synthetic-aircraft', 'aircraft_profile_version': 'declared-v1',
            'connection_profile_version': 'synthetic-loopback-v1',
            'connection': {'local_address': '127.0.0.1', 'listen_port': 14551,
                           'expected_sender': '127.0.0.1', 'system_id': 1, 'component_id': 1,
                           'feed_origin': 'live_forwarded', 'synthetic': False, **connection_changes}}


def envelope(service, profile_value=None, **fields):
    value = profile_value or profile()
    preview = service.preview(value)
    return {'operation_id': str(uuid4()), 'target_profile_version': value['aircraft_profile_version'],
            'applicability_fingerprint': preview['applicability_fingerprint'], **fields}


def prepare(service, profile_value=None):
    value = profile_value or profile()
    params = {**value, **envelope(service, value)}
    result = service.prepare(params)
    service.mark_recorder_started(result['capture_session']['uuid'])
    return result['capture_session']['uuid'], params


def event(record_number, armed_flag=False, **changes):
    return {'kind': 'heartbeat', 'origin': 'live_forwarded', 'live': True, 'crc_valid': True,
            'sender': '127.0.0.1', 'source_id': '127.0.0.1:50100/1:1', 'system_id': 1,
            'component_id': 1, 'record_index': record_number, 'armed': armed_flag, 'autopilot': 3,
            'vehicle_type': 2, 'received_monotonic_ns': str(time.monotonic_ns()),
            'received_utc_us': '1700000000000000', 'raw_ref': {'chunk_id': 'raw-000001.udp', 'offset': record_number * 20}, **changes}


def arm(service, capture_id, index=0):
    service.ingest_event(capture_id, event(index))
    service.ingest_event(capture_id, event(index + 1, True))
    return service.status({'capture_session_id': capture_id})['flight_records'][0]


def detail(service, record):
    return service.detail({'flight_record_id': record['id']})


def test_prepare_fingerprint_and_idempotency_payload_bound(service):
    capture_id, params = prepare(service)
    repeated = service.prepare(params)
    assert repeated['operation_replayed'] and repeated['capture_session']['uuid'] == capture_id
    assert len(service.status()['capture_sessions']) == 1
    with pytest.raises(ValueError, match='different payload'):
        service.prepare(dict(params, extra_payload='different'))
    with pytest.raises(ValueError, match='fingerprint'):
        service.prepare(dict(params, applicability_fingerprint='stale'))
    with pytest.raises(ValueError, match='profile version'):
        service.prepare(dict(params, target_profile_version='old'))


def test_future_collection_schema_is_rejected_without_appending_or_changing_rows(tmp_path):
    store = Store(tmp_path / 'future-catalog')
    with store._connect() as db:
        db.execute('CREATE TABLE collection_schema (version INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO collection_schema VALUES (99)')
        db.execute('CREATE TABLE future_evidence (value TEXT)')
        db.execute("INSERT INTO future_evidence VALUES ('preserve-original')")
    with pytest.raises(ValueError, match='Unsupported collection schema'):
        CollectionService(store)
    with store._connect() as db:
        assert [row[0] for row in db.execute('SELECT version FROM collection_schema')] == [99]
        assert db.execute('SELECT value FROM future_evidence').fetchone()[0] == 'preserve-original'
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_heads'").fetchone() is None


def test_rapid_rearm_creates_exactly_one_record_each_while_prior_incomplete(service):
    capture_id, _ = prepare(service)
    first = arm(service, capture_id)
    service.ingest_event(capture_id, event(2))
    service.ingest_event(capture_id, event(3, True))
    records = service.status()['flight_records']
    assert len(records) == 2
    assert sorted(r['display_number'] for r in records) == [1, 2]
    old = detail(service, first)
    assert old['armed_interval']['disarm_evidence']['record_index'] == 2
    assert old['flight_record']['processing_state'] == 'AWAITING_EVIDENCE'
    assert all(r['classification'] == 'unknown' and r['collection_completeness']['state'] == 'partial' for r in records)
    assert len({r['armed_interval_id'] for r in records}) == 2


def test_receipt_clocks_preserved_duplicate_event_and_unknown_start(service):
    capture_id, _ = prepare(service)
    sample = event(1, True)
    service.ingest_event(capture_id, sample)
    assert service.ingest_event(capture_id, sample)['duplicate']
    record = service.status()['flight_records'][0]
    assert record['started_already_armed']
    assert record['arm_evidence']['received_utc_us'] == sample['received_utc_us']
    assert record['arm_evidence']['received_monotonic_ns'] == sample['received_monotonic_ns']
    assert record['parameter_evidence']['preflight'] is None
    with pytest.raises(ValueError, match='conflicting content'):
        service.ingest_event(capture_id, dict(sample, armed=False))


@pytest.mark.parametrize('change', [
    {'vehicle_type': 6}, {'autopilot': 8}, {'autopilot': None}, {'component_id': 100},
    {'system_id': 255}, {'sender': '127.0.0.2'}, {'origin': 'replay'}, {'origin': 'synthetic'},
    {'live': False}, {'crc_valid': False}, {'armed': 128},
    {'received_monotonic_ns': '1'},
])
def test_unqualified_heartbeat_cannot_open_interval(service, change):
    capture_id, _ = prepare(service)
    result = service.ingest_event(capture_id, event(1, True, **change))
    assert result['accepted'] is False
    assert service.status()['flight_records'] == []


def test_declared_synthetic_capture_cannot_create_real_intervals(service):
    capture_id, _ = prepare(service, profile(synthetic=True))
    assert not service.ingest_event(capture_id, event(1, True))['accepted']
    assert service.status()['flight_records'] == []


def test_expected_uid_pending_and_conflict_quarantine(service):
    capture_id, _ = prepare(service, profile(expected_uid='123'))
    service.ingest_event(capture_id, event(0, True))
    assert service.status()['flight_records'] == []
    service.ingest_event(capture_id, event(1, kind='identity', uid='123'))
    service.ingest_event(capture_id, event(2, True))
    record = service.status()['flight_records'][0]
    service.ingest_event(capture_id, event(3, kind='identity', uid='999'))
    service.ingest_event(capture_id, event(4))
    assert detail(service, record)['armed_interval']['disarm_evidence'] is None
    assert service.status()['capture_session']['watcher_state'] == 'LINK_UNCERTAIN'
    assert not service.safety_state(capture_id)['identity_verified']


def test_gap_clock_segment_and_stop_never_manufacture_disarm(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    service.ingest_event(capture_id, event(2, kind='link_gap'))
    assert detail(service, record)['armed_interval']['disarm_evidence'] is None
    service.ingest_event(capture_id, event(3, True, boot_epoch='new-clock-segment'))
    assert len(service.status()['flight_records']) == 2
    assert detail(service, record)['armed_interval']['boot_epochs'] == ['unknown']
    newer = next(r for r in service.status()['flight_records'] if r['id'] != record['id'])
    assert newer['started_already_armed']
    assert detail(service, newer)['armed_interval']['boot_epochs'] == ['new-clock-segment']
    service.ingest_event(capture_id, event(4, False, boot_epoch='new-clock-segment'))
    assert detail(service, newer)['armed_interval']['disarm_evidence']['record_index'] == 4
    assert detail(service, record)['armed_interval']['disarm_evidence'] is None
    stop = envelope(service, capture_session_id=capture_id)
    assert not service.validate_stop(stop)['operation_replayed']
    service.stop_capture(stop)
    assert service.stop_capture(stop)['operation_replayed']
    assert detail(service, record)['armed_interval']['disarm_evidence'] is None
    assert detail(service, record)['armed_interval']['end_status'] == 'unknown'


def test_different_endpoint_cannot_close_current_interval(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    service.ingest_event(capture_id, event(2, source_id='127.0.0.1:60100/1:1'))
    assert detail(service, record)['armed_interval']['disarm_evidence'] is None
    assert service.status()['capture_session']['watcher_state'] == 'LINK_UNCERTAIN'


def test_reopen_does_not_arbitrarily_recover_live_capture(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    reopened = CollectionService(service.store)
    assert reopened.status()['capture_session']['status'] == 'observing'
    assert reopened.recover_interrupted(capture_ids=[]) == []
    reopened.recover_interrupted(capture_ids=[capture_id])
    result = detail(reopened, record)
    assert result['capture_session']['status'] == 'interrupted'
    assert result['armed_interval']['disarm_evidence'] is None
    assert len(reopened.status()['flight_records']) == 1
    assert not reopened.ingest_event(capture_id, event(9))['accepted']


def test_capture_revisions_do_not_grow_for_unchanged_heartbeat(service):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    with service.store._connect() as db:
        before = db.execute("SELECT count(*) FROM collection_versions WHERE kind='capture'").fetchone()[0]
    for i in range(1, 20):
        service.ingest_event(capture_id, event(i))
    with service.store._connect() as db:
        after = db.execute("SELECT count(*) FROM collection_versions WHERE kind='capture'").fetchone()[0]
        events = db.execute('SELECT count(*) FROM collection_events').fetchone()[0]
    assert after == before and events == 20
    assert service.status()['capture_session']['journal_checkpoint']['record_index'] == 19


def test_manifest_and_operation_evidence_cannot_be_mutated(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    old = detail(service, record)['manifest']
    service.ingest_event(capture_id, event(2))
    historical = service.detail({'flight_record_id': record['id'], 'manifest_revision': old['revision']})
    assert historical['manifest'] == old
    with service.store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE collection_versions SET data='{}' WHERE kind='manifest'")
        assert db.execute('PRAGMA user_version').fetchone()[0] == CATALOG_VERSION


def test_legacy_metric_session_does_not_become_flight_record(service):
    service.store.add_document('session', {'aircraft_id': 'legacy', 'metrics': {}})
    assert service.status()['flight_records'] == []


def test_synthetic_import_cannot_be_linked_to_declared_live_flight(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    log = service.store.add_document('log', {'synthetic': True, 'streams': []})
    before = detail(service, record)['manifest']
    with pytest.raises(ValueError, match='Synthetic log evidence'):
        service.evaluate_association(envelope(service, flight_record_id=record['id'], log_id=log['id']))
    assert detail(service, record)['manifest'] == before


def artifact(service, tmp_path, **changes):
    path = tmp_path / 'synthetic.tlog'
    path.write_bytes(b'synthetic recorder bytes for catalog association test')
    return {'source': service.store.archive_file(path), 'role': 'app_mavlink_capture',
            'record_start': 0, 'record_end': 100, **changes}


def test_many_to_many_artifact_spans_and_archive_retry(service, tmp_path):
    capture_id, _ = prepare(service)
    first = arm(service, capture_id)
    service.ingest_event(capture_id, event(2))
    service.ingest_event(capture_id, event(3, True))
    service.ingest_event(capture_id, event(4))
    item = artifact(service, tmp_path)
    service.attach_capture_artifacts(capture_id, [item])
    service.attach_capture_artifacts(capture_id, [item])
    records = service.status()['flight_records']
    associations = [detail(service, record)['associations'] for record in records]
    assert all(len(items) == 1 for items in associations)
    assert len({items[0]['artifact_sha256'] for items in associations}) == 1
    assert sorted((items[0]['time_or_record_spans'][0]['start'], items[0]['time_or_record_spans'][0]['end']) for items in associations) == [(1, 2), (3, 4)]
    assert all(items[0]['state'] == 'provisional' for items in associations)
    assert detail(service, first)['manifest']['collection_completeness']['state'] == 'partial'


def test_interrupted_artifact_without_record_span_stays_capture_level(service, tmp_path):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    item = artifact(service, tmp_path, coverage={'interval_membership': 'unknown'})
    service.attach_capture_artifacts(capture_id, [item])
    assert detail(service, record)['associations'] == []
    assert service.status()['capture_session']['artifact_count'] == 1


def test_finalizing_job_link_cannot_overwrite_terminal_archive_state(service):
    capture_id, _ = prepare(service)
    service.mark_finalizing(capture_id)
    service.mark_archive_complete(capture_id)
    service.mark_finalizing(capture_id, job_id='finished-job')
    capture = service.status()['capture_session']
    assert capture['archive_state'] == 'complete' and capture['status'] == 'stopped'
    assert capture['archive_job_id'] == 'finished-job'


def test_parameter_windows_are_passive_non_atomic_and_scoped_to_arm_disarm(service):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    service.ingest_event(capture_id, event(1, kind='parameter', name='TEST_P', value=1.0, type=9, index=0, count=1))
    service.ingest_event(capture_id, event(2, True))
    record = service.status()['flight_records'][0]
    evidence = record['parameter_evidence']['preflight']
    assert evidence['completeness'] == 'complete_for_scope'
    assert evidence['atomic'] is False and evidence['full_readback_requested'] is False
    service.ingest_event(capture_id, event(3, True, kind='parameter', name='TEST_P', value=2.0, type=9, index=0, count=1))
    service.ingest_event(capture_id, event(4))
    service.ingest_event(capture_id, event(5, kind='parameter', name='TEST_P', value=3.0, type=9, index=0, count=1))
    service.stop_capture(envelope(service, capture_session_id=capture_id))
    evidence = detail(service, record)['manifest']['parameter_evidence']
    assert evidence['preflight']['typed_hash'] != evidence['postflight']['typed_hash']
    assert evidence['applicability'] == 'unknown'
    assert 'preflight_parameters' in detail(service, record)['manifest']['collection_completeness']['missing']


def test_retrieval_has_no_invented_filename_or_resolved_bin(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    request = next(r for r in detail(service, record)['retrieval_requests'] if r['role'] == 'onboard_bin')
    assert request['observed_path_or_null'] is None and request['resolved_artifact_id'] is None
    assert request['state'] == 'missing' and request['recovery_action'] == 'select_sd_log_folder_and_content_match'


def test_manual_association_provisional_review_preserves_old_manifest(service, tmp_path):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    original = artifact(service, tmp_path)['source']
    log = service.store.add_document('log', {'source': original, 'format': 'bin', 'record_count': 100,
        'streams': [{'source_key': 'dataflash', 'boot_epoch': 0, 'system_id': None, 'component_id': None, 'identity_observations': []}]})
    params = envelope(service, flight_record_id=record['id'], log_id=log['id'], source_key='dataflash', boot_epoch=0,
                      role='onboard_bin', time_or_record_spans=[{'kind': 'records', 'start': 10, 'end': 20}])
    result = service.evaluate_association(params)
    assert result['association']['state'] == 'provisional'
    prior = result['manifest']
    review = service.review_association(envelope(service, association_id=result['association']['id'], decision='reject', reason='Synthetic mismatch review'))
    assert review['association']['state'] == 'rejected'
    assert service.detail({'flight_record_id': record['id'], 'manifest_revision': prior['revision']})['manifest'] == prior
    assert next(r for r in detail(service, record)['retrieval_requests'] if r['role'] == 'onboard_bin')['state'] == 'missing'
    with pytest.raises(ValueError, match='cannot manufacture'):
        service.review_association(envelope(service, association_id=result['association']['id'], decision='verified', reason='unsupported'))


def test_worker_journal_event_id_separates_admin_and_same_frame_gap(service):
    capture_id, _ = prepare(service)
    admin = {'event_id': 1, 'kind': 'coverage', 'received_monotonic_ns': str(time.monotonic_ns()),
             'received_utc_us': '1700000000000000', 'coverage': {'state': 'recording'}}
    assert service.ingest_event(capture_id, admin)['accepted']
    sample = event(1, False, event_id=2)
    assert service.ingest_event(capture_id, sample)['accepted']
    gap = event(2, True, kind='link_gap', event_id=3, reason='sequence_discontinuity')
    assert service.ingest_event(capture_id, gap)['accepted']
    heartbeat = event(2, True, event_id=4)
    assert service.ingest_event(capture_id, heartbeat)['accepted']
    assert service.ingest_event(capture_id, heartbeat)['duplicate']
    assert len(service.status()['flight_records']) == 1
    with pytest.raises(ValueError, match='conflicting content'):
        service.ingest_event(capture_id, dict(heartbeat, armed=False))


def test_failed_worker_status_preserves_unknown_end_and_polling_has_no_revision_growth(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    before = service.status()['capture_session']['revision']
    service.update_recorder_status(capture_id, {'state': 'recording', 'bytes': 100})
    service.update_recorder_status(capture_id, {'state': 'recording', 'bytes': 200})
    assert service.status()['capture_session']['revision'] == before
    service.update_recorder_status(capture_id, {'state': 'failed', 'failure': 'synthetic disk failure'})
    assert service.status()['capture_session']['status'] == 'failed'
    assert detail(service, record)['armed_interval']['disarm_evidence'] is None
    assert service.safety_state(capture_id)['armed_state'] == 'unknown'


def test_aircraft_counter_continues_across_capture_sessions(service):
    capture_id, _ = prepare(service)
    assert arm(service, capture_id)['display_number'] == 1
    service.stop_capture(envelope(service, capture_session_id=capture_id))
    second_id, _ = prepare(service)
    assert arm(service, second_id)['display_number'] == 2


def test_finalizing_capture_can_recover_as_interrupted(service):
    capture_id, _ = prepare(service)
    service.mark_finalizing(capture_id)
    service.recover_interrupted(capture_ids=[capture_id])
    assert service.status()['capture_session']['status'] == 'interrupted'


def test_empty_recorder_chunk_archives_without_interval_association(service, tmp_path):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    item = artifact(service, tmp_path, record_start=1, record_end=0)
    service.attach_capture_artifacts(capture_id, [item])
    assert detail(service, record)['associations'] == []
    assert service.status()['capture_session']['artifact_count'] == 1


def test_link_gap_before_arm_does_not_claim_exact_transition(service):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    service.ingest_event(capture_id, event(1, kind='link_gap'))
    service.ingest_event(capture_id, event(2, True))
    record = service.status()['flight_records'][0]
    assert record['started_already_armed']
    assert detail(service, record)['armed_interval']['start_status'] == 'unknown'


def test_other_component_gap_does_not_change_selected_source_state(service):
    capture_id, _ = prepare(service)
    record = arm(service, capture_id)
    service.ingest_event(capture_id, event(2, kind='link_gap', component_id=100))
    assert service.status()['capture_session']['watcher_state'] == 'ARMED_RECORDING'
    assert detail(service, record)['armed_interval']['coverage_warnings'] == []


def test_interval_counter_record_and_event_commit_atomically(service, monkeypatch):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    actual = service._manifest
    sample = event(1, True)
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic transaction failure')
    monkeypatch.setattr(service, '_manifest', fail)
    with pytest.raises(RuntimeError):
        service.ingest_event(capture_id, sample)
    assert service.status()['flight_records'] == []
    with service.store._connect() as db:
        assert db.execute('SELECT count(*) FROM collection_counters').fetchone()[0] == 0
    monkeypatch.setattr(service, '_manifest', actual)
    service.ingest_event(capture_id, sample)
    assert service.status()['flight_records'][0]['display_number'] == 1


def test_history_pages_are_explicit_and_filtered_before_limiting(service):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    for i in range(1, 7):
        service.ingest_event(capture_id, event(i, bool(i % 2)))
    first = service.status({'capture_session_id': capture_id, 'limit': 1})
    second = service.status({'capture_session_id': capture_id, 'limit': 1, 'offset': 1})
    assert first['flight_record_total'] == 3 and first['flight_records_truncated']
    assert first['flight_records'][0]['id'] != second['flight_records'][0]['id']
    assert first['capture_total'] == 1 and not first['captures_truncated']


@pytest.mark.parametrize('fields', [
    {'index': 65535, 'count': 0}, {'index': 0, 'count': 65535}, {'name': 'unsupported-name'},
])
def test_wire_valid_unsupported_parameter_does_not_poison_later_heartbeat(service, fields):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    value = {'name': 'TEST_P', 'value': 1.0, 'type': 9, 'index': 0, 'count': 1, **fields}
    assert service.ingest_event(capture_id, event(1, kind='parameter', **value))['accepted']
    service.ingest_event(capture_id, event(2, True))
    record = service.status()['flight_records'][0]
    evidence = record['parameter_evidence']['preflight']
    assert evidence['completeness'] == 'partial' and evidence['flight_time_applicability'] == 'unknown'
    service.ingest_event(capture_id, event(3))
    assert detail(service, record)['armed_interval']['disarm_evidence'] is not None


def test_two_endpoints_are_ambiguous_before_any_arm(service):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    service.ingest_event(capture_id, event(1, source_id='127.0.0.1:60100/1:1'))
    service.ingest_event(capture_id, event(2, True, source_id='127.0.0.1:60100/1:1'))
    service.ingest_event(capture_id, event(3, True))
    assert service.status()['flight_records'] == []
    assert service.status()['capture_session']['source_ambiguity']
    assert service.safety_state(capture_id)['armed_state'] == 'unknown'


def test_preflight_enumeration_before_gap_is_explicitly_ineligible(service):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0))
    service.ingest_event(capture_id, event(1, kind='parameter', name='TEST_P', value=1.0, type=9, index=0, count=1))
    service.ingest_event(capture_id, event(2, kind='link_gap'))
    service.ingest_event(capture_id, event(3))
    service.ingest_event(capture_id, event(4, True))
    evidence = service.status()['flight_records'][0]['parameter_evidence']['preflight']
    assert evidence is None
    capture = service.status()['capture_session']
    historical_id = capture['historical_parameter_capture_ids'][0]
    with service.store._connect() as db:
        historical = service._get(db, 'parameter_capture', historical_id)
    assert historical['completeness'] == 'complete_for_scope'
    assert historical['freshness'] == 'gap_since_capture' and historical['flight_time_applicability'] == 'unknown'


def test_mixed_matching_and_foreign_uid_is_hard_contradiction(service, tmp_path):
    chosen = profile(expected_uid='123')
    capture_id, _ = prepare(service, chosen)
    service.ingest_event(capture_id, event(0, kind='identity', uid='123'))
    record = arm(service, capture_id, 1)
    original = artifact(service, tmp_path)['source']
    log = service.store.add_document('log', {'source': original, 'format': 'tlog', 'record_count': 100,
        'streams': [{'source_key': '1:1', 'boot_epoch': 0, 'system_id': 1, 'component_id': 1,
                     'identity_observations': [{'raw_fields': {'uid': 123}}, {'raw_fields': {'uid': 999}}]}]})
    result = service.evaluate_association(envelope(service, chosen, flight_record_id=record['id'], log_id=log['id'], source_key='1:1', boot_epoch=0,
        role='gcs_tlog', time_or_record_spans=[{'kind': 'records', 'start': 1, 'end': 10}]))
    assert result['association']['state'] == 'rejected'
    with pytest.raises(ValueError, match='hard identity'):
        service.review_association(envelope(service, chosen, association_id=result['association']['id'], decision='accept_provisional', reason='Cannot override contradiction'))


@pytest.mark.parametrize('terminal', ['stop', 'failed', 'recover', 'worker_failed'])
def test_terminal_capture_releases_passive_parameter_accumulators(service, terminal):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0, kind='parameter', name='TEST_P', value=1.0, type=9, index=0, count=1))
    assert service._parameters
    if terminal == 'stop':
        service.stop_capture(envelope(service, capture_session_id=capture_id))
    elif terminal == 'failed':
        service.mark_recorder_failed(capture_id, 'synthetic failure')
    elif terminal == 'recover':
        service.recover_interrupted(capture_ids=[capture_id])
    else:
        service.update_recorder_status(capture_id, {'state': 'failed'})
    assert not service._parameters


def test_clock_boundary_caps_old_artifact_span_without_inventing_disarm(service, tmp_path):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0, boot_epoch='0'))
    service.ingest_event(capture_id, event(1, True, boot_epoch='0'))
    old = service.status()['flight_records'][0]
    service.ingest_event(capture_id, event(2, kind='link_gap', boot_epoch='1', reason='boot_time_regression_unclassified'))
    service.ingest_event(capture_id, event(3, True, boot_epoch='1'))
    newer = next(r for r in service.status()['flight_records'] if r['id'] != old['id'])
    service.ingest_event(capture_id, event(4, boot_epoch='1'))
    service.attach_capture_artifacts(capture_id, [artifact(service, tmp_path)])
    old_detail = detail(service, old)
    assert old_detail['armed_interval']['disarm_evidence'] is None
    assert old_detail['armed_interval']['membership_end_record'] == 1
    assert old_detail['associations'][0]['time_or_record_spans'] == [{'kind': 'records', 'start': 1, 'end': 1}]
    assert detail(service, newer)['associations'][0]['time_or_record_spans'] == [{'kind': 'records', 'start': 3, 'end': 4}]


@pytest.mark.parametrize('conflict', ['uid', 'endpoint'])
def test_identity_ambiguity_caps_interval_membership_at_first_conflict(service, tmp_path, conflict):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0, kind='identity', uid='123'))
    service.ingest_event(capture_id, event(1))
    service.ingest_event(capture_id, event(2, True))
    old = service.status()['flight_records'][0]
    if conflict == 'uid':
        service.ingest_event(capture_id, event(3, kind='identity', uid='999'))
    else:
        service.ingest_event(capture_id, event(3, True, source_id='127.0.0.1:60100/1:1'))
    service.ingest_event(capture_id, event(4))
    service.attach_capture_artifacts(capture_id, [artifact(service, tmp_path)])
    evidence = detail(service, old)
    assert evidence['armed_interval']['disarm_evidence'] is None
    assert evidence['armed_interval']['membership_end_record'] == 2
    assert evidence['associations'][0]['time_or_record_spans'] == [{'kind': 'records', 'start': 2, 'end': 2}]


def test_later_clock_discontinuity_cannot_extend_prior_identity_cutoff(service, tmp_path):
    capture_id, _ = prepare(service)
    service.ingest_event(capture_id, event(0, kind='identity', uid='123', boot_epoch='0'))
    service.ingest_event(capture_id, event(1, boot_epoch='0'))
    service.ingest_event(capture_id, event(2, True, boot_epoch='0'))
    old = service.status()['flight_records'][0]
    service.ingest_event(capture_id, event(3, kind='identity', uid='999', boot_epoch='0'))
    service.ingest_event(capture_id, event(10, kind='link_gap', boot_epoch='1'))
    service.attach_capture_artifacts(capture_id, [artifact(service, tmp_path)])
    evidence = detail(service, old)
    assert evidence['armed_interval']['membership_end_record'] == 2
    assert evidence['armed_interval']['membership_end_basis'].startswith('Conflicting vehicle identity')
    assert evidence['associations'][0]['time_or_record_spans'][0]['end'] == 2
