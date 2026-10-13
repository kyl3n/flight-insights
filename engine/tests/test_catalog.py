"""Exercise actual old-catalog upgrades, transaction faults and raw integrity."""
from concurrent.futures import CancelledError
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from unittest.mock import patch

import pytest

from q22_engine import catalog
from q22_engine.catalog import CatalogService
from q22_engine.store import Store


def legacy(root):
    root.mkdir()
    raw = root / 'raw' / 'sha256'
    raw.mkdir(parents=True)
    content = b'Original synthetic flight bytes\x00\xff\r\n'
    sha = hashlib.sha256(content).hexdigest()
    (raw / sha).write_bytes(content)
    source = {'id': 'old-source', 'sha256': sha, 'byte_size': len(content),
              'original_name': 'unchanged.bin', 'schema_version': 1}
    document = {'id': 'old-report', 'source': source, 'schema_version': 1,
                'original_result': {'value': 12.5, 'unit': 'W'}}
    with closing(sqlite3.connect(root / 'catalog.sqlite3')) as db, db:
        for sql in catalog.BASE_SCHEMA:
            db.execute(sql)
        db.execute('INSERT INTO sources VALUES (?,?,?)', ('old-source', 'old-time', json.dumps(source)))
        db.execute('INSERT INTO documents VALUES (?,?,?,?)', ('old-report', 'insight_report', 'old-time', json.dumps(document)))
        # Include a separately versioned subsystem. The base upgrade cannot
        # rewrite or prune earlier manifest revisions or foreign subsystem rows.
        db.execute('CREATE TABLE preserved_revisions(revision INTEGER PRIMARY KEY,data TEXT)')
        db.executemany('INSERT INTO preserved_revisions VALUES (?,?)', [(1, '{"state":"partial"}'), (2, '{"state":"reviewed"}')])
        db.execute('PRAGMA user_version=1')
    return source, document


def test_upgrade_preserves_rows_blobs_and_valid_snapshot_then_reopens(tmp_path):
    root = tmp_path / 'old catalog ü'
    source, document = legacy(root)
    store = Store(root)
    assert store.get_document('insight_report', 'old-report') == document
    status = CatalogService(store).status()
    assert status['catalog_version'] == 2
    migration = status['migrations'][0]
    assert migration['from_version'] == 1 and not migration['original_documents_rewritten']
    backup = migration['backup']
    path = root / 'catalog-migrations' / backup['filename']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == backup['sha256']
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert json.loads(db.execute('SELECT data FROM documents').fetchone()[0]) == document
        assert db.execute('SELECT count(*) FROM preserved_revisions').fetchone()[0] == 2
    with store._connect() as db:
        assert db.execute('SELECT * FROM preserved_revisions ORDER BY revision').fetchall() == [(1, '{"state":"partial"}'), (2, '{"state":"reviewed"}')]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE documents SET data='{}'")
    assert store.raw_path(source['sha256']).read_bytes().startswith(b'Original synthetic')
    assert CatalogService(Store(root)).status() == status
    assert len(list((root / 'catalog-migrations').iterdir())) == 1
    assert CatalogService(store).verify()['state'] == 'passed'
    path.chmod(0o600)


def test_fault_after_upgrade_statements_rolls_back_entire_schema(tmp_path):
    root = tmp_path / 'old'
    _, document = legacy(root)
    real_upgrade = catalog._upgrade_v2

    def interrupted(db, backup):
        real_upgrade(db, backup)
        raise OSError('Injected interruption after DDL')

    with patch.object(catalog, '_upgrade_v2', side_effect=interrupted), pytest.raises(OSError, match='interruption'):
        Store(root)
    with closing(sqlite3.connect(root / 'catalog.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='catalog_migrations'").fetchall()
        assert json.loads(db.execute('SELECT data FROM documents').fetchone()[0]) == document
    assert CatalogService(Store(root)).verify()['state'] == 'passed'
    for path in (root / 'catalog-migrations').iterdir():
        path.chmod(0o600)


def test_future_and_unversioned_populated_catalogs_are_preserved(tmp_path):
    root = tmp_path / 'future'
    legacy(root)
    database = root / 'catalog.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('PRAGMA user_version=99')
    before = database.read_bytes()
    with pytest.raises(ValueError, match='newer'):
        Store(root)
    assert database.read_bytes() == before
    assert not (root / 'catalog-migrations').exists()
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('PRAGMA user_version=0')
    with pytest.raises(ValueError, match='Unversioned populated'):
        Store(root)


def test_failed_backup_leaves_legacy_version_and_documents(tmp_path):
    root = tmp_path / 'old'
    _, document = legacy(root)
    with patch.object(catalog.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()), pytest.raises(OSError, match='disk space'):
        Store(root)
    with closing(sqlite3.connect(root / 'catalog.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert json.loads(db.execute('SELECT data FROM documents').fetchone()[0]) == document


def test_integrity_check_detects_changed_bytes_without_repair(tmp_path):
    store = Store(tmp_path / 'catalog')
    original = tmp_path / 'original.bin'
    original.write_bytes(b'first original')
    source = store.archive_file(original)
    store.add_document('report', {'evidence': source['id']})
    good = CatalogService(store).verify()
    assert good['state'] == 'passed' and good['verified_unique_blobs'] == 1
    blob = store.raw_path(source['sha256'])
    blob.chmod(0o600)
    blob.write_bytes(b'changed bytes!')
    result = CatalogService(store).verify()
    assert result['state'] == 'failed' and result['issue_count'] == 1
    assert result['issues'][0]['kind'] == 'source_integrity'
    assert not result['mutations_performed']
    assert blob.read_bytes() == b'changed bytes!'


def test_integrity_snapshot_releases_live_database_before_hashing(tmp_path):
    store = Store(tmp_path / 'catalog')
    original = tmp_path / 'original.bin'
    original.write_bytes(b'original')
    source = store.archive_file(original)
    written = threading.Event()
    errors = []
    real_digest = catalog._digest

    def while_hashing(path, cancel_check):
        def writer():
            try:
                store.add_document('report', {'label': 'Concurrent capture append'})
                written.set()
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=writer)
        worker.start()
        try:
            assert written.wait(2), 'Integrity hashing held a live-catalog lock'
        finally:
            worker.join(3)
        return real_digest(path, cancel_check)

    with patch.object(catalog, '_digest', side_effect=while_hashing):
        result = CatalogService(store).verify()
    assert not errors
    assert result['state'] == 'passed'
    assert result['document_count'] == 0  # New append is outside the initial snapshot.
    assert len(store.list_documents('report')) == 1
    store.raw_path(source['sha256']).chmod(0o600)


def test_cancellation_and_invalid_rpc_inputs_do_not_change_originals(tmp_path):
    store = Store(tmp_path / 'catalog')
    before = store.database.read_bytes()
    with pytest.raises(CancelledError):
        CatalogService(store).verify(cancel_check=lambda: True)
    assert store.database.read_bytes() == before
    with pytest.raises(ValueError, match='no paths'):
        CatalogService(store).dispatch('catalog.verify', {'sql': 'DELETE FROM sources'})


def test_verification_reports_removed_guard_and_invalid_documents(tmp_path):
    store = Store(tmp_path / 'catalog')
    with store._connect() as db:
        db.execute('DROP TRIGGER documents_no_update')
        db.execute('INSERT INTO documents VALUES (?,?,?,?)', ('invalid', 'report', 'now', '[]'))
    result = CatalogService(store).verify()
    assert result['state'] == 'failed'
    assert {item['kind'] for item in result['issues']} == {'missing_immutability_trigger', 'document_integrity'}
