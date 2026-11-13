import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend.operations import StorageGuard, WorkspaceLock
from backend.operations_api import OperationsService
from tests_backend.test_processing_queue import workspace


@pytest.mark.asyncio
async def test_repeated_request_cancellation_cannot_release_live_backup_gate(tmp_path, monkeypatch):
    store = workspace(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def copy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"verified": True}

    monkeypatch.setattr("backend.operations_api.create_backup", copy)
    tasks = SimpleNamespace(active={}, scan_tasks={}, maintenance=False)
    processing = SimpleNamespace(active={}, maintenance=False)
    with WorkspaceLock(tmp_path) as lock:
        service = OperationsService(
            SimpleNamespace(data_dir=tmp_path),
            store,
            lock,
            tasks,
            processing,
            object(),
            None,
            StorageGuard(tmp_path),
        )
        request = asyncio.create_task(service.backup())
        assert await asyncio.to_thread(entered.wait, 2)
        try:
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            await asyncio.sleep(0.02)
            assert service.maintenance and tasks.maintenance and processing.maintenance
            assert service._backup_task is not None
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
            await service.wait_for_backup()
        assert not service.maintenance
