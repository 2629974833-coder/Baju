import asyncio

import pytest

from backend.downloader import Downloader
from backend.store import Store
from backend.tasks import Conflict, Control, TaskManager


@pytest.mark.asyncio
async def test_immediate_scan_cancel_releases_slot(tmp_path):
    store = Store(tmp_path / "state.db")
    manager = TaskManager(store, None, None, None)
    site = store.read()["sites"][0]
    scan = manager.start_scan(site, {"url": site["url"]})
    manager.cancel_scan(scan["id"])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not manager.scan_tasks
    assert store.read()["scans"][0]["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("cancel", "cancelled"), ("pause", "paused")])
async def test_user_control_wins_over_browser_failure(tmp_path, action, expected):
    entered, release = asyncio.Event(), asyncio.Event()

    class Browser:
        async def open_session(self, site):
            entered.set()
            await release.wait()
            raise RuntimeError("connection failed")

    store = Store(tmp_path / "state.db")
    store.mutate(
        lambda state: state["assets"].append(
            {"id": "asset_test", "siteId": "chunlou", "status": "discovered"}
        )
    )
    manager = TaskManager(store, Browser(), None, None)
    manager.enqueue(["asset_test"])
    job_id = store.read()["jobs"][0]["id"]
    control = Control()
    task = asyncio.create_task(manager._download(job_id, control))
    manager.active[job_id] = (control, task)
    await entered.wait()
    manager.job_action(job_id, action)
    release.set()
    await task
    assert store.read()["jobs"][0]["status"] == expected


def test_restart_preserves_queue_and_pauses_only_interrupted_work(tmp_path):
    store = Store(tmp_path / "state.db")
    store.mutate(
        lambda state: state.update(
            jobs=[
                {"id": "job_test", "status": "running", "downloadedBytes": 2048, "progress": 50},
                {"id": "job_queued", "status": "queued"},
                {"id": "job_completed", "status": "completed", "progress": 100},
            ],
            scans=[{"id": "scan_test", "status": "queued"}],
        )
    )
    store.recover_interrupted()
    assert store.read()["jobs"][0]["status"] == "paused"
    assert store.read()["jobs"][0]["downloadedBytes"] == 2048
    assert store.read()["jobs"][0]["progress"] == 50
    assert store.read()["jobs"][1]["status"] == "queued"
    assert store.read()["jobs"][2]["status"] == "completed"
    assert store.read()["scans"][0]["status"] == "failed"


def test_cancel_paused_task_removes_partial_but_keeps_original(tmp_path):
    store = Store(tmp_path / "state.db")
    store.mutate(
        lambda state: state["jobs"].append(
            {"id": "job_test", "assetId": "asset_test", "status": "paused"}
        )
    )
    folder = tmp_path / "assets" / "asset_test"
    folder.mkdir(parents=True)
    (folder / "original.part").write_bytes(b"partial")
    (folder / "original.png").write_bytes(b"original")
    manager = TaskManager(store, None, None, Downloader(tmp_path, None, None))
    manager.job_action("job_test", "cancel")
    assert not (folder / "original.part").exists()
    assert (folder / "original.png").read_bytes() == b"original"


@pytest.mark.parametrize("status,action", [("paused", "resume"), ("failed", "retry")])
def test_resume_and_retry_retain_checkpoint_progress(tmp_path, status, action):
    store = Store(tmp_path / "state.db")
    store.mutate(
        lambda state: state["jobs"].append(
            {"id": "job_test", "assetId": "asset_test", "status": status,
             "downloadedBytes": 2048, "totalBytes": 4096, "progress": 50,
             "completedSegments": 2, "totalSegments": 4, "error": "interrupted"}
        )
    )
    result = TaskManager(store, None, None, None).job_action("job_test", action)
    assert result["status"] == "queued"
    assert result["error"] == ""
    assert result["downloadedBytes"] == 2048
    assert result["progress"] == 50
    assert result["completedSegments"] == 2


def test_retry_cannot_compete_with_another_job_for_same_asset(tmp_path):
    store = Store(tmp_path / "state.db")
    store.mutate(
        lambda state: state["jobs"].extend([
            {"id": "job_old", "assetId": "asset_test", "status": "failed"},
            {"id": "job_new", "assetId": "asset_test", "status": "queued"},
        ])
    )
    with pytest.raises(Conflict):
        TaskManager(store, None, None, None).job_action("job_old", "retry")
    assert store.read()["jobs"][0]["status"] == "failed"


def test_retry_old_failure_cannot_replace_a_downloaded_original(tmp_path):
    store = Store(tmp_path / "state.db")
    store.mutate(lambda state: state.update(
        assets=[{"id": "asset_test", "status": "downloaded", "_path": "assets/original.mp4"}],
        jobs=[{"id": "job_old", "assetId": "asset_test", "status": "failed"}],
    ))
    with pytest.raises(Conflict):
        TaskManager(store, None, None, None).job_action("job_old", "retry")
    assert store.read()["assets"][0]["_path"] == "assets/original.mp4"


def test_cancelling_historical_job_does_not_discard_current_asset_checkpoint(tmp_path):
    class Downloader:
        def discard_partial(self, asset_id):
            pytest.fail("another live job owns this asset checkpoint")

    store = Store(tmp_path / "state.db")
    store.mutate(lambda state: state["jobs"].extend([
        {"id": "job_old", "assetId": "asset_test", "status": "failed"},
        {"id": "job_new", "assetId": "asset_test", "status": "paused"},
    ]))
    manager = TaskManager(store, None, None, Downloader())
    manager.job_action("job_old", "cancel")
    assert store.read()["jobs"][0]["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("cancel", "cancelled"), ("pause", "paused")])
async def test_control_before_scheduled_coroutine_starts(tmp_path, action, expected):
    class Browser:
        async def open_session(self, site):
            pytest.fail("a controlled queued job must not open a browser")

    class Downloader:
        def discard_partial(self, asset_id):
            pass

    store = Store(tmp_path / "state.db")
    store.mutate(lambda state: state["assets"].append(
        {"id": "asset_test", "siteId": "chunlou", "status": "discovered"}
    ))
    manager = TaskManager(store, Browser(), None, Downloader())
    manager.enqueue(["asset_test"])
    job_id = store.read()["jobs"][0]["id"]
    control = Control()
    task = asyncio.create_task(manager._download(job_id, control))
    manager.active[job_id] = (control, task)
    manager.job_action(job_id, action)
    await task
    assert store.read()["jobs"][0]["status"] == expected
    assert not manager.active
