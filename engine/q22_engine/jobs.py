"""Single-worker, cancellable jobs with durable outcomes and bounded RPC views."""
from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4


TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
MAX_PENDING = 4
MAX_RESULT_BYTES = 3 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobService:
    """One engine owns this journal; a restart never replays imports or repairs."""

    def __init__(self, data_dir: Path):
        self.database = Path(data_dir) / "jobs.sqlite3"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="q22-import")
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL)")
            pending = db.execute("SELECT id, data FROM jobs WHERE json_extract(data, '$.state') IN ('queued','running')").fetchall()
            for job_id, payload in pending:
                job = json.loads(payload)
                job.update(state="interrupted", finished_at=now(), error={"message": "The engine stopped before completion. Review imported evidence and retry explicitly."})
                db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job_id))

    def _connect(self):
        return sqlite3.connect(self.database, timeout=20)

    def _read(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not job_id or len(job_id) > 100:
            raise ValueError("A valid job_id is required.")
        with closing(self._connect()) as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown job ID.")
        return json.loads(row[0])

    def _update(self, job_id: str, **changes) -> dict:
        with self._lock:
            job = self._read(job_id)
            job.update(changes)
            with closing(self._connect()) as db, db:
                db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job, allow_nan=False), job_id))
            return job

    def start(self, method: str, params: dict, handler) -> dict:
        """Handler accepts (method, params, cancel_check=..., progress=...)."""
        # Validate/copy before publishing a job: invalid input must not leave a
        # queued record with no worker.
        arguments = json.loads(json.dumps(params, allow_nan=False))
        with self._lock:
            if self._closed:
                raise ValueError("Engine is shutting down.")
            if len(self._events) >= MAX_PENDING:
                raise ValueError("Import queue is full. Wait for a job or cancel a queued import.")
            job = {"id": str(uuid4()), "method": method, "state": "queued", "created_at": now(),
                   "cancel_requested": False,
                   "progress": {"stage": "queued", "completed": 0, "total": None, "fraction": None}}
            with closing(self._connect()) as db, db:
                db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (job["id"], job["created_at"], json.dumps(job)))
            event = threading.Event()
            self._events[job["id"]] = event
            self._executor.submit(self._run, job["id"], method, arguments, handler, event)
            return {"job": job}

    def _run(self, job_id: str, method: str, params: dict, handler, event: threading.Event):
        last_progress = 0.0

        def progress(stage: str, completed: int, total: int | None = None):
            nonlocal last_progress
            current = time.monotonic()
            if current - last_progress < 0.2 and (total is None or completed < total):
                return
            last_progress = current
            completed = max(0, int(completed))
            total = max(0, int(total)) if total is not None else None
            self._update(job_id, progress={"stage": str(stage)[:200], "completed": completed,
                                         "total": total, "fraction": min(1.0, completed / total) if total else None})

        try:
            if event.is_set():
                raise CancelledError()
            self._update(job_id, state="running", started_at=now())
            result = handler(method, params, cancel_check=event.is_set, progress=progress)
            if len(json.dumps(result, allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
                raise ValueError("Job result exceeded the response limit. Review the evidence list for any completed import.")
            # Cancellation may arrive after immutable evidence was published. A
            # completed handler remains successful, never falsely 'cancelled'.
            self._update(job_id, state="succeeded", finished_at=now(), result=result)
        except CancelledError:
            self._update(job_id, state="cancelled", finished_at=now())
        except (ValueError, KeyError, TypeError, FileNotFoundError) as exc:
            self._update(job_id, state="failed", finished_at=now(), error={"message": str(exc)[:2000]})
        except PermissionError:
            self._update(job_id, state="failed", finished_at=now(), error={"message": "Access denied. Review file permissions and retry."})
        except Exception:
            logging.exception("Background job failed: %s", method)
            self._update(job_id, state="failed", finished_at=now(), error={"message": "Import failed. Inspect the engine diagnostic log."})
        finally:
            with self._lock:
                self._events.pop(job_id, None)

    def dispatch(self, method: str, params: dict) -> dict:
        if method == "jobs.get":
            return {"job": self._read(params.get("job_id"))}
        if method == "jobs.list":
            limit, offset = params.get("limit", 50), params.get("offset", 0)
            if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
                raise ValueError("List limit must be 1..100 and offset nonnegative.")
            with closing(self._connect()) as db:
                rows = db.execute("SELECT json_remove(data, '$.result') FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            # Results are fetched individually; a list of large results would
            # otherwise exceed the transport boundary.
            jobs = [json.loads(row[0]) for row in rows[:limit]]
            for job in jobs:
                job.pop("result", None)
            return {"jobs": jobs, "next_offset": offset + limit if len(rows) > limit else None}
        if method == "jobs.cancel":
            with self._lock:
                job = self._read(params.get("job_id"))
                if job["state"] not in TERMINAL:
                    event = self._events.get(job["id"])
                    if event is not None:
                        event.set()
                        job = self._update(job["id"], cancel_requested=True)
                return {"job": job}
        raise ValueError("Unsupported job method.")

    def close(self):
        with self._lock:
            self._closed = True
            for event in self._events.values():
                event.set()
        self._executor.shutdown(wait=True)
