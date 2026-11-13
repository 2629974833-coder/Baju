"""Shared process limits must bound work across independent workspace queues."""

import asyncio

import pytest

from backend.store import Store
from backend.tasks import Control, TaskManager


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


def manager_for(path, limits, entered, release):
    class Browser:
        async def open_session(self, site):
            entered.append((path.name, "browser"))

    class Downloader:
        async def download(self, asset, control, progress):
            entered.append((path.name, "download"))
            await release.wait()
            control.check()
            return {"path": "assets/original.mp4", "size": 1, "sha256": "fixture", "mime": "video/mp4"}

        def discard_partial(self, asset_id):
            pass

    class Scanner:
        async def scan(self, *args):
            entered.append((path.name, "scan"))
            await release.wait()

    store = Store(path)
    store.mutate(lambda state: state["assets"].append(
        {"id": "asset_1", "siteId": "chunlou", "status": "discovered", "title": "Fixture"}
    ))
    return TaskManager(store, Browser(), Scanner(), Downloader(), resource_limits=limits)


def start_download(manager):
    manager.enqueue(["asset_1"])
    job_id = manager.store.read()["jobs"][0]["id"]
    control = Control()
    task = asyncio.create_task(manager._download(job_id, control))
    manager.active[job_id] = control, task
    return job_id, task


@pytest.mark.asyncio
async def test_shared_download_limit_defers_browser_and_transfer_until_slot(tmp_path):
    from backend.resource_limits import ResourceLimits

    limits = ResourceLimits(downloads=1, scans=1)
    entered, release = [], asyncio.Event()
    first = manager_for(tmp_path / "first.db", limits, entered, release)
    second = manager_for(tmp_path / "second.db", limits, entered, release)
    _, first_task = start_download(first)
    await until(lambda: ("first.db", "download") in entered)
    _, second_task = start_download(second)
    try:
        await asyncio.sleep(0.05)
        assert entered == [("first.db", "browser"), ("first.db", "download")]
        assert second.store.read()["jobs"][0]["status"] == "queued"
        release.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), 2)
        assert second.store.read()["jobs"][0]["status"] == "completed"
    finally:
        release.set()
        await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("cancel", "cancelled"), ("pause", "paused")])
async def test_control_of_waiting_download_releases_reservation(tmp_path, action, expected):
    from backend.resource_limits import ResourceLimits

    limits = ResourceLimits(downloads=1, scans=1)
    entered, release = [], asyncio.Event()
    first = manager_for(tmp_path / "first.db", limits, entered, release)
    second = manager_for(tmp_path / "second.db", limits, entered, release)
    third = manager_for(tmp_path / "third.db", limits, entered, release)
    _, first_task = start_download(first)
    await until(lambda: ("first.db", "download") in entered)
    job_id, waiting = start_download(second)
    await asyncio.sleep(0.02)
    second.job_action(job_id, action)
    try:
        await asyncio.wait_for(waiting, 1)
        assert second.store.read()["jobs"][0]["status"] == expected
        assert not second.active
        assert not any(name == "second.db" for name, _ in entered)
        _, third_task = start_download(third)
        release.set()
        await asyncio.wait_for(asyncio.gather(first_task, third_task), 2)
        assert third.store.read()["jobs"][0]["status"] == "completed"
    finally:
        release.set()
        await asyncio.gather(first.close(), second.close(), third.close())


@pytest.mark.asyncio
async def test_shared_scan_limit_preserves_queued_state_and_survives_waiter_cancel(tmp_path):
    from backend.resource_limits import ResourceLimits

    limits = ResourceLimits(downloads=1, scans=1)
    entered, release = [], asyncio.Event()
    managers = [manager_for(tmp_path / f"{name}.db", limits, entered, release)
                for name in ("first", "second", "third")]
    def scan(manager):
        site = manager.store.read()["sites"][0]
        return manager.start_scan(site, {"url": site["url"]})

    scan(managers[0])
    await until(lambda: ("first.db", "scan") in entered)
    second_scan = scan(managers[1])
    await asyncio.sleep(0.05)
    try:
        waiting = managers[1].store.read()["scans"][0]
        assert waiting["status"] == "queued"
        assert "等待" in waiting["message"]
        assert entered == [("first.db", "scan")]
        managers[1].cancel_scan(second_scan["id"])
        await until(lambda: not managers[1].scan_tasks)
        scan(managers[2])
        release.set()
        await until(lambda: not any(manager.scan_tasks for manager in managers))
        assert managers[1].store.read()["scans"][0]["status"] == "cancelled"
        assert managers[2].store.read()["scans"][0]["status"] == "completed"
        assert entered == [("first.db", "scan"), ("third.db", "scan")]
    finally:
        release.set()
        await asyncio.gather(*(manager.close() for manager in managers))


@pytest.mark.asyncio
async def test_cancellation_at_slot_handoff_does_not_leak_capacity():
    from backend.resource_limits import ResourceLimits

    limits = ResourceLimits(downloads=1, scans=1)
    async def wait_for_slot():
        async with limits.slot("download"):
            pytest.fail("cancelled waiter must not start")

    async with limits.slot("download"):
        waiter = asyncio.create_task(wait_for_slot())
        await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    async with asyncio.timeout(1):
        async with limits.slot("download"):
            pass


@pytest.mark.asyncio
async def test_waiting_download_reads_latest_refreshed_asset_after_admission(tmp_path):
    from backend.resource_limits import ResourceLimits

    limits = ResourceLimits(downloads=1, scans=1)
    release = asyncio.Event()
    release.set()
    manager = manager_for(tmp_path / "state.db", limits, [], release)
    captured = []
    original = manager.downloader.download

    async def capture(asset, control, progress):
        captured.append(asset.get("sourceUrl"))
        return await original(asset, control, progress)

    manager.downloader.download = capture
    async with limits.slot("download"):
        _, waiting = start_download(manager)
        await asyncio.sleep(0.02)
        manager.store.mutate(lambda state: state["assets"][0].update(sourceUrl="https://example.com/refreshed.mp4"))
    await asyncio.wait_for(waiting, 2)
    assert captured == ["https://example.com/refreshed.mp4"]


@pytest.mark.asyncio
async def test_repeated_cancellation_during_cleanup_returns_slot():
    from backend.resource_limits import ResourceLimits

    limits = ResourceLimits(downloads=1, scans=1)
    entered = asyncio.Event()
    async def wait_for_slot():
        async with limits.slot("download"):
            entered.set()
            await asyncio.Event().wait()

    waiter = asyncio.create_task(wait_for_slot())
    await entered.wait()
    waiter.cancel()
    asyncio.get_running_loop().call_soon(waiter.cancel)
    with pytest.raises(asyncio.CancelledError):
        await waiter
    async with asyncio.timeout(0.2):
        async with limits.slot("download"):
            pass
