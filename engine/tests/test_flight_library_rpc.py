"""Flight history work must yield to active flight collection at the RPC boundary."""
from concurrent.futures import CancelledError
import time

import pytest

from q22_engine.__main__ import Engine


def test_library_jobs_are_available_through_public_protocol(tmp_path):
    engine = Engine(tmp_path)
    try:
        capabilities = engine.call('handshake', {})['capabilities']
        assert {'library.list', 'library.detail', 'library.import', 'library.refresh', 'library.analyze'} <= set(capabilities)
        assert engine.call('library.list', {})['flights'] == []
        job = engine.call('library.refresh', {})['job']
        deadline = time.monotonic() + 5
        while job['state'] in {'queued', 'running'} and time.monotonic() < deadline:
            time.sleep(.01)
            job = engine.call('jobs.get', {'job_id': job['id']})['job']
        assert job['state'] == 'succeeded', job
        assert job['result']['total'] == 0
    finally:
        engine.close()


def test_automatic_analysis_defers_and_cancels_for_collection(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    try:
        engine.call('library.list', {})
        with monkeypatch.context() as changes:
            changes.setattr(engine.collection.recorder, 'status', lambda: {'active': True})
            for method in ('library.refresh', 'library.import', 'library.analyze'):
                with pytest.raises(ValueError, match='after collection stops'):
                    engine.call(method, {})
            # Opening already saved results never requires stopping collection.
            assert engine.call('library.list', {})['flights'] == []
            def interrupted(method, params, cancel_check=None, progress=None):
                if cancel_check():
                    raise CancelledError()
                pytest.fail('History analysis retained the worker after collection started')
            changes.setattr(engine.library, 'dispatch', interrupted)
            with pytest.raises(CancelledError):
                engine._library_job('library.refresh', {})
    finally:
        engine.close()
