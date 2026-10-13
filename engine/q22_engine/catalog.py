"""Transactional catalog upgrades and read-only integrity checks.

Pre-upgrade snapshots contain SQLite metadata only. Original blobs and derived
files stay in the same data directory; a snapshot is not a portable full backup.
No snapshot or saved SQL is ever automatically restored or executed.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time
from uuid import uuid4

CATALOG_VERSION = 2
BASE_SCHEMA = (
    "CREATE TABLE sources (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE documents (id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL)",
    "CREATE INDEX documents_kind ON documents(kind, created_at)",
    "CREATE TRIGGER sources_no_update BEFORE UPDATE ON sources BEGIN SELECT RAISE(ABORT, 'Sources are immutable'); END",
    "CREATE TRIGGER sources_no_delete BEFORE DELETE ON sources BEGIN SELECT RAISE(ABORT, 'Sources are immutable'); END",
    "CREATE TRIGGER documents_no_update BEFORE UPDATE ON documents BEGIN SELECT RAISE(ABORT, 'Documents are immutable'); END",
    "CREATE TRIGGER documents_no_delete BEFORE DELETE ON documents BEGIN SELECT RAISE(ABORT, 'Documents are immutable'); END",
)


def _digest(path, cancel_check=None):
    result = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
                         | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Integrity checking requires a regular file.')
        while chunk := incoming.read(1024 * 1024):
            if cancel_check and cancel_check():
                raise CancelledError()
            result.update(chunk)
            size += len(chunk)
        after = os.fstat(incoming.fileno())
        current = path.stat(follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError('File changed during integrity checking.')
    return result.hexdigest(), size


def _upgrade_v2(db, backup):
    db.execute("CREATE TABLE catalog_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, data TEXT NOT NULL)")
    record = {'from_version': 1 if backup else 0, 'to_version': 2,
              'method': 'transactional_additive_schema', 'backup': backup,
              'original_documents_rewritten': False}
    db.execute("INSERT INTO catalog_migrations VALUES (?,?,?)",
               (2, datetime.now(timezone.utc).isoformat(), json.dumps(record, sort_keys=True)))
    db.execute("PRAGMA user_version=2")


def migrate_catalog(db, root: Path):
    # A read transaction fixes the backup's view. Upgrading the same transaction
    # may fail if another writer races us; SQLite then rolls back every DDL step.
    # It is safer to fail opening than silently use a partial upgrade.
    db.execute('BEGIN')
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > CATALOG_VERSION:
        raise ValueError('Catalog is newer than this engine; use its matching version.')
    if version == CATALOG_VERSION:
        row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_migrations'").fetchone()
        if not row:
            raise ValueError('Catalog migration metadata is missing; preserve the catalog for recovery.')
        return
    if version not in (0, 1):
        raise ValueError('Unsupported catalog version; preserve the catalog for recovery.')
    check = db.execute('PRAGMA quick_check').fetchall()
    if check != [('ok',)]:
        raise ValueError('Catalog integrity check failed; preserve the catalog for recovery.')
    backup = None
    if version == 0:
        existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if existing:
            raise ValueError('Unversioned populated catalog is not a recognized upgrade source.')
        for statement in BASE_SCHEMA:
            db.execute(statement)
    else:
        # Check the old shape before publishing any snapshot or changing it.
        db.execute('SELECT id,created_at,data FROM sources LIMIT 0')
        db.execute('SELECT id,kind,created_at,data FROM documents LIMIT 0')
        directory = root / 'catalog-migrations'
        if directory.is_symlink():
            raise ValueError('Catalog migration directory must not be a symlink.')
        directory.mkdir(mode=0o700, exist_ok=True)
        database_size = (root / 'catalog.sqlite3').stat().st_size
        if shutil.disk_usage(directory).free < database_size + 2 * 1024 * 1024:
            raise OSError('Insufficient disk space for a pre-upgrade catalog snapshot.')
        path = directory / f'catalog-v1-before-v2-{uuid4()}.sqlite3'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            destination = sqlite3.connect(path)
            try:
                db.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Pre-upgrade snapshot integrity check failed.')
            finally:
                destination.close()
            with path.open('r+b') as stream:
                os.fsync(stream.fileno())
            if os.name != 'nt':
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            digest, size = _digest(path)
            os.chmod(path, 0o444)
            backup = {'filename': path.name, 'sha256': digest, 'byte_size': size,
                      'scope': 'catalog_sqlite_only', 'automatic_restore': False}
        except BaseException:
            if path.exists():
                path.chmod(0o600)
                path.unlink()
            raise
    _upgrade_v2(db, backup)


class CatalogService:
    def __init__(self, store):
        self.store = store

    def dispatch(self, method, params, cancel_check=None, progress=None):
        if params:
            raise ValueError('Catalog checks take no paths, SQL, or repair instructions.')
        if method == 'catalog.status':
            return self.status()
        if method == 'catalog.verify':
            return self.verify(cancel_check, progress)
        raise ValueError('Unsupported catalog method.')

    def status(self):
        with self.store._connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            migrations = [json.loads(row[0]) | {'applied_at': row[1]}
                          for row in db.execute('SELECT data,applied_at FROM catalog_migrations ORDER BY version')]
        return {'catalog_version': version, 'supported_version': CATALOG_VERSION,
                'migrations': migrations, 'automatic_restore': False,
                'backup_scope': 'Catalog snapshots exclude raw blobs, derived files, jobs, and live recorder files.'}

    def verify(self, cancel_check=None, progress=None):
        # Copy metadata first, then release the live database before hashing raw
        # files. A long integrity check must not hold a read lock against capture.
        with tempfile.TemporaryDirectory(prefix='q22-catalog-check-') as temporary:
            snapshot = sqlite3.connect(Path(temporary) / 'snapshot.sqlite3')
            try:
                deadline = time.monotonic() + 30

                def copied(_status, _remaining, _total):
                    if cancel_check and cancel_check():
                        raise CancelledError()
                    if time.monotonic() > deadline:
                        raise ValueError('Catalog snapshot exceeded its 30-second limit; retry when less busy.')

                with self.store._connect() as live:
                    live.backup(snapshot, pages=256, progress=copied)
                return self._verify_snapshot(snapshot, cancel_check, progress)
            finally:
                snapshot.close()

    def _verify_snapshot(self, db, cancel_check=None, progress=None):
        issues = []
        issue_count = 0
        source_count = document_count = verified_blobs = verified_bytes = 0
        seen = set()

        def issue(value):
            nonlocal issue_count
            issue_count += 1
            if len(issues) < 100:
                issues.append(value)

        with db:
            check = [row[0] for row in db.execute('PRAGMA quick_check')]
            if check != ['ok']:
                issue({'kind': 'sqlite_integrity', 'details': check[:10]})
            triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            for name in ('sources_no_update', 'sources_no_delete', 'documents_no_update', 'documents_no_delete'):
                if name not in triggers:
                    issue({'kind': 'missing_immutability_trigger', 'name': name})
            total = db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
            for identity, payload in db.execute('SELECT id,data FROM sources ORDER BY id'):
                if cancel_check and cancel_check():
                    raise CancelledError()
                source_count += 1
                try:
                    source = json.loads(payload)
                    if not isinstance(source, dict) or source.get('id') != identity:
                        raise ValueError('Stored source identity differs from its catalog key.')
                    if type(source.get('byte_size')) is not int or source['byte_size'] < 0:
                        raise ValueError('Original byte size is invalid.')
                    sha = source['sha256']
                    path = self.store.raw_path(sha)
                    if sha not in seen:
                        if path.is_symlink() or not path.is_file():
                            raise ValueError('Original blob is missing or not a regular owned blob.')
                        actual, size = _digest(path, cancel_check)
                        if actual != sha or size != source['byte_size']:
                            raise ValueError('Original blob hash or size differs from its source record.')
                        seen.add(sha)
                        verified_blobs += 1
                        verified_bytes += size
                    elif path.stat().st_size != source['byte_size']:
                        raise ValueError('Duplicate source has contradictory byte size.')
                except (ValueError, KeyError, TypeError, OSError) as error:
                    issue({'kind': 'source_integrity', 'source_id': identity, 'reason': str(error)[:300]})
                if progress:
                    progress('verifying_originals', source_count, total)
            for identity, payload in db.execute('SELECT id,data FROM documents ORDER BY id'):
                if cancel_check and cancel_check():
                    raise CancelledError()
                document_count += 1
                try:
                    value = json.loads(payload)
                    if not isinstance(value, dict) or value.get('id') != identity:
                        raise ValueError('Stored document identity differs from its catalog key.')
                except (ValueError, TypeError) as error:
                    issue({'kind': 'document_integrity', 'document_id': identity, 'reason': str(error)[:300]})
        return {'state': 'passed' if not issues else 'failed', 'source_count': source_count,
                'document_count': document_count, 'verified_unique_blobs': verified_blobs,
                'verified_bytes': verified_bytes, 'issues': issues, 'issue_count': issue_count,
                'scope': 'Catalog read snapshot and referenced original bytes; not derived-data, hardware, or full-directory backup validation.',
                'mutations_performed': False}
