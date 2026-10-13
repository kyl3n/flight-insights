"""Immutable local catalog and content-addressed originals; no external services."""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import CancelledError
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

from .catalog import migrate_catalog


CHUNK_BYTES = 1024 * 1024
MAX_RAW_BYTES = 8 * 1024 * 1024 * 1024


class Store:
    """One short-lived SQLite connection per operation, one catalog authority."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.raw = self.root / "raw" / "sha256"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "catalog.sqlite3"
        with self._connect() as db:
            migrate_catalog(db, self.root)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=20)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            # SQLite's own context manager controls the transaction but does
            # not close its handle. Close explicitly so Windows can release
            # database locks immediately and every operation owns its handle.
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def archive_file(self, path: Path | str, *, cancel_check=None, progress=None,
                     acquisition_method="manual_file_import") -> dict:
        if acquisition_method not in {"manual_file_import", "app_owned_passive_udp_capture",
                                      "app_owned_readonly_mavlink_acquisition",
                                      "app_owned_readonly_radio_capture"}:
            raise ValueError("Unsupported evidence acquisition method.")
        source = Path(path).expanduser().resolve(strict=True)
        if not stat.S_ISREG(source.stat().st_mode):
            raise ValueError("Import requires a regular completed file.")
        digest = hashlib.sha256()
        size = 0
        temporary = None
        try:
            # Nonblocking open prevents a regular-file-to-FIFO race hanging import.
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                                 | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as incoming:
                before = os.fstat(incoming.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Import requires a regular completed file.")
                if before.st_size > MAX_RAW_BYTES:
                    raise ValueError("This milestone supports original files up to 8 GiB.")
                with tempfile.NamedTemporaryFile(dir=self.raw, prefix=".import-", delete=False) as outgoing:
                    temporary = Path(outgoing.name)
                    while chunk := incoming.read(CHUNK_BYTES):
                        if cancel_check and cancel_check():
                            raise CancelledError("Import cancelled while archiving original.")
                        size += len(chunk)
                        if size > MAX_RAW_BYTES:
                            raise ValueError("Original exceeds the 8 GiB import limit.")
                        digest.update(chunk)
                        outgoing.write(chunk)
                        if progress:
                            progress("archiving", size, before.st_size)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                after = os.fstat(incoming.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Source changed during import. Stop recording and retry.")
            sha256 = digest.hexdigest()
            destination = self.raw / sha256
            def verify_existing():
                existing_hash = hashlib.sha256()
                with destination.open("rb") as existing:
                    while chunk := existing.read(CHUNK_BYTES):
                        if cancel_check and cancel_check():
                            raise CancelledError("Import cancelled while checking original integrity.")
                        existing_hash.update(chunk)
                if existing_hash.hexdigest() != sha256:
                    raise ValueError("Archived original failed integrity verification; refusing overwrite.")

            if cancel_check and cancel_check():
                raise CancelledError("Import cancelled before publishing original.")
            if destination.exists():
                # Never trust a same-name blob after disk corruption or local tampering.
                verify_existing()
            else:
                try:
                    if os.name == "nt":
                        # Windows rename refuses replacement; publish writable temp then
                        # protect destination. No read-only hardlink remains to unlink.
                        os.rename(temporary, destination)
                        temporary = None
                        os.chmod(destination, 0o444)
                    else:
                        os.chmod(temporary, 0o444)
                        # Atomic no-replace publication; POSIX permits unlinking readonly.
                        os.link(temporary, destination)
                except FileExistsError:
                    verify_existing()
                if os.name != "nt":
                    descriptor = os.open(self.raw, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            manifest = {
                "id": str(uuid4()), "sha256": sha256, "byte_size": size,
                "original_name": source.name, "original_path": str(source),
                "acquisition_method": acquisition_method, "created_at": self._now(),
                "status": "archived", "schema_version": 1,
            }
            with self._connect() as db:
                db.execute("INSERT INTO sources VALUES (?, ?, ?)",
                           (manifest["id"], manifest["created_at"], json.dumps(manifest, allow_nan=False)))
            return manifest
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def raw_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("Invalid SHA-256 reference.")
        return self.raw / sha256

    def add_document(self, kind: str, data: dict) -> dict:
        document = dict(data)
        document["id"] = str(uuid4())
        document["created_at"] = self._now()
        document["schema_version"] = 1
        payload = json.dumps(document, allow_nan=False, sort_keys=True)
        with self._connect() as db:
            db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)",
                       (document["id"], kind, document["created_at"], payload))
        return document

    def get_document(self, kind: str, document_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT data FROM documents WHERE id=? AND kind=?",
                             (document_id, kind)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {kind} ID.")
        return json.loads(row[0])

    def list_documents(self, kind: str, limit: int = 100, offset: int = 0) -> list[dict]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("List limit must be 1..100 and offset nonnegative.")
        with self._connect() as db:
            projection = "json_remove(data, '$.parameters')" if kind == "snapshot" else "data"
            rows = db.execute(f"SELECT {projection} FROM documents WHERE kind=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                              (kind, limit, offset)).fetchall()
        return [json.loads(row[0]) for row in rows]
