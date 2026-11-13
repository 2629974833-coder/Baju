import asyncio
from types import SimpleNamespace

import pytest

from backend.downloader import Downloader
from backend.operations import StoragePressure
from backend.tasks import TaskManager
from tests_backend.test_app import Browser, Scanner
from tests_backend.test_app import Downloader as FixtureDownloader
from tests_backend.test_processing_queue import workspace


class Guard:
    low = False

    def check(self, required_bytes=0):
        if self.low:
            raise StoragePressure("磁盘剩余空间不足")


@pytest.mark.asyncio
async def test_fast_transfer_rechecks_headroom_before_publishing(tmp_path):
    guard = Guard()
    downloader = Downloader(tmp_path, object(), object(), storage_guard=guard)

    async def fast_transfer(transfer, partial, cache):
        partial.write_bytes(b"fixture-verified-output")
        guard.low = True
        return {"size": partial.stat().st_size}, ".png"

    downloader._download = fast_transfer

    async def progress(_value):
        pass

    with pytest.raises(StoragePressure):
        await downloader.download(
            {"id": "sample", "kind": "image"},
            SimpleNamespace(paused=False, cancelled=False),
            progress,
        )
    assert not (tmp_path / "assets/sample/original.png").exists()
    assert (tmp_path / "assets/sample/original.part").exists()


@pytest.mark.asyncio
async def test_queued_download_checks_storage_before_opening_a_browser(tmp_path):
    store = workspace(tmp_path)
    store.mutate(lambda state: state["assets"][0].update(status="discovered"))
    guard = Guard()

    class ObservedBrowser(Browser):
        opened = False

        async def open_session(self, site):
            self.opened = True

    browser = ObservedBrowser()
    tasks = TaskManager(store, browser, Scanner(), FixtureDownloader(tmp_path), storage_guard=guard)
    tasks.enqueue(["sample"])
    guard.low = True
    await tasks.start()
    try:
        for _ in range(100):
            job = store.read()["jobs"][0]
            if job["status"] not in {"running", "queued"}:
                break
            await asyncio.sleep(0.02)
        assert job["status"] == "paused" and job["failureCode"] == "storage-low"
        assert not browser.opened
    finally:
        await tasks.close()
