"""Atomic, bounded repair receipts. This module never stores executable argv.

Records are recovery evidence, not authorization: callers must validate typed
descriptors, recompile fixed commands, inspect current ownership and ask for
confirmation before rollback. No record is replayed automatically.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


class RepairJournal:
    MAX_BYTES = 1024 * 1024

    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir) / "network-repairs"
        if self.directory.is_symlink():
            raise OSError("Repair journal directory must not be a symbolic link.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check(self.directory, directory=True)

    @staticmethod
    def _check(path: Path, *, directory: bool = False) -> None:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)):
            raise OSError("Repair journal paths must be ordinary files/directories.")
        if os.name != "nt" and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise OSError("Repair journal must be private and owned by the current OS user; cross-user elevation recovery requires manual review.")

    def _path(self, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise ValueError("Invalid repair receipt ID.")
        return self.directory / (plan_id + ".json")

    def read(self, path: Path) -> dict:
        self._check(self.directory, directory=True)
        self._check(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError("Repair receipt exceeds its size limit.")
        record = json.loads(raw)
        if not isinstance(record, dict) or set(record) != {"version", "plan_id", "descriptor", "baseline", "events"} or record["version"] != 1:
            raise ValueError("Unsupported repair receipt schema.")
        if self._path(record["plan_id"]).name != path.name:
            raise ValueError("Receipt identity does not match its filename.")
        if not isinstance(record["events"], list) or not record["events"] or len(record["events"]) > 100:
            raise ValueError("Invalid repair receipt history.")
        for event in record["events"]:
            if not isinstance(event, dict) or set(event) != {"time", "state", "post_state"} or event["state"] not in {"apply_intent", "applied", "uncertain", "rollback_intent", "rolled_back", "rollback_uncertain"}:
                raise ValueError("Invalid repair receipt lifecycle.")
        return record

    def records(self) -> tuple[list[dict], list[str]]:
        self._check(self.directory, directory=True)
        records, warnings = [], []
        paths = sorted(self.directory.glob("*.json"))
        if len(paths) > 512:
            raise OSError("Repair journal exceeds the record limit; preserve and review its history before proceeding.")
        for path in paths:
            try:
                records.append(self.read(path))
            except (OSError, ValueError) as exc:
                warnings.append(f"Receipt {path.name} could not be trusted: {exc}")
        return records, warnings

    @contextmanager
    def _writer(self):
        """OS lock releases on process death; no stale sentinel blocks recovery."""
        path = self.directory / ".writer-lock"
        if path.exists() or path.is_symlink():
            self._check(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def save(self, plan_id: str, descriptor: dict, baseline: dict, state: str, post_state: object = None) -> dict:
        """Write intent before mutation; preserve original descriptor/baseline."""
        with self._writer():
            return self._save_locked(plan_id, descriptor, baseline, state, post_state)

    def _save_locked(self, plan_id: str, descriptor: dict, baseline: dict, state: str, post_state: object) -> dict:
        self._check(self.directory, directory=True)
        path = self._path(plan_id)
        if path.exists() or path.is_symlink():
            record = self.read(path)
            if record["descriptor"] != descriptor or record["baseline"] != baseline:
                raise ValueError("Repair identity or original baseline changed; refusing to overwrite its evidence.")
        else:
            record = {"version": 1, "plan_id": plan_id, "descriptor": copy.deepcopy(descriptor), "baseline": copy.deepcopy(baseline), "events": []}
        if len(record["events"]) >= 100:
            raise OSError("Repair receipt history is full; no host change was authorized by this journal write.")
        record["events"].append({"time": time.time(), "state": state, "post_state": copy.deepcopy(post_state)})
        payload = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > self.MAX_BYTES:
            raise ValueError("Repair receipt exceeds its size limit.")
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=self.directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            # Directory fsync makes the rename durable on POSIX. Windows uses
            # flushed file content + atomic replacement (directory fsync absent).
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return record
