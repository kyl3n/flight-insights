import hashlib
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from q22_engine.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "catalog")

    def tearDown(self):
        # Windows marks immutable blobs read-only; allow test-directory cleanup.
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.tmp.cleanup()

    def test_identical_bytes_deduplicate_blobs_but_not_source_identity(self):
        source = self.root / "first.bin"
        content = b"original bytes\x00" * 200000
        source.write_bytes(content)
        first = self.store.archive_file(source)
        another = self.root / "second.bin"
        another.write_bytes(content)
        second = self.store.archive_file(another)
        self.assertEqual(first["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(list(self.store.raw.iterdir())), 1)
        self.assertEqual(self.store.raw_path(first["sha256"]).read_bytes(), content)
        self.assertEqual(first["byte_size"], len(content))

    def test_same_filename_different_bytes_are_retained(self):
        source = self.root / "flight.bin"
        source.write_bytes(b"one")
        first = self.store.archive_file(source)
        source.write_bytes(b"two")
        second = self.store.archive_file(source)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(self.store.raw_path(first["sha256"]).read_bytes(), b"one")

    def test_document_and_source_rows_are_immutable_and_persist(self):
        source = self.root / "flight.param"
        source.write_text("A,1\n")
        manifest = self.store.archive_file(source)
        document = self.store.add_document("snapshot", {"source": manifest, "parameters": {"A": {"value": "1"}}})
        reopened = Store(self.store.root)
        self.assertEqual(reopened.get_document("snapshot", document["id"]), document)
        self.assertNotIn("parameters", reopened.list_documents("snapshot")[0])
        with closing(sqlite3.connect(self.store.database)) as db:
            for table in ("sources", "documents"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"DELETE FROM {table}")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"UPDATE {table} SET data='{{}}'")

    def test_corrupt_blob_is_not_silently_replaced(self):
        source = self.root / "sample.bin"
        source.write_bytes(b"good")
        manifest = self.store.archive_file(source)
        blob = self.store.raw_path(manifest["sha256"])
        blob.chmod(0o600)
        blob.write_bytes(b"evil")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.archive_file(source)
        self.assertEqual(blob.read_bytes(), b"evil")
        self.assertFalse(list(self.store.raw.glob(".import-*")))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO test")
    def test_fifo_rejected_before_open(self):
        source = self.root / "pipe"
        os.mkfifo(source)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.store.archive_file(source)

    def test_unknown_schema_not_migrated_backwards(self):
        with closing(sqlite3.connect(self.store.database)) as db, db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ValueError, "newer"):
            Store(self.store.root)

    def test_invalid_hash_cannot_escape_raw_directory(self):
        with self.assertRaises(ValueError):
            self.store.raw_path("../catalog.sqlite3")

    def test_binary_original_is_preserved_without_windows_text_translation(self):
        content = b"first\r\nsecond\n\x1a\x00\xff\x80\r\nlast"
        source = self.root / "binary-original.bin"
        source.write_bytes(content)
        manifest = self.store.archive_file(source)
        self.assertEqual(manifest["byte_size"], len(content))
        self.assertEqual(manifest["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.store.raw_path(manifest["sha256"]).read_bytes(), content)
        self.assertEqual(source.read_bytes(), content)

    def test_operations_release_connections_including_error_paths(self):
        opened = []
        original_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch("q22_engine.store.sqlite3.connect", side_effect=tracked_connect):
            store = Store(self.root / "resource-check")
            source = self.root / "resource-check.param"
            source.write_bytes(b"A,1\n")
            store.archive_file(source)
            document = store.add_document("snapshot", {"label": "committed"})
            self.assertEqual(store.get_document("snapshot", document["id"]), document)
            self.assertEqual(store.list_documents("snapshot"), [document])
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                with store._connect() as connection:
                    connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?)",
                                       ("cancelled", "snapshot", "unused", "{}"))
                    raise RuntimeError("cancelled")
            with self.assertRaisesRegex(ValueError, "Unknown"):
                store.get_document("snapshot", "cancelled")
            with store._connect() as connection:
                connection.execute("PRAGMA user_version=99")
            with self.assertRaisesRegex(ValueError, "newer"):
                Store(store.root)

        self.assertTrue(opened)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
