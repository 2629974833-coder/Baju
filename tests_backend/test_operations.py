import importlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest


def operations():
    assert importlib.util.find_spec("backend.operations"), "runtime operations module is missing"
    return importlib.import_module("backend.operations")


def test_workspace_lock_excludes_second_owner_and_releases_after_process_death(tmp_path):
    module = operations()
    with module.WorkspaceLock(tmp_path):
        with pytest.raises(module.WorkspaceBusy):
            module.WorkspaceLock(tmp_path).acquire()
    program = (
        "from backend.operations import WorkspaceLock; import sys,time; "
        "lock=WorkspaceLock(sys.argv[1]);lock.acquire();print('locked',flush=True);time.sleep(10)"
    )
    child = subprocess.Popen([sys.executable, "-c", program, str(tmp_path)], stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"locked"
        with pytest.raises(module.WorkspaceBusy):
            module.WorkspaceLock(tmp_path).acquire()
    finally:
        child.kill()
        child.wait(timeout=5)
    with module.WorkspaceLock(tmp_path) as lock:
        assert lock.held
    assert not lock.held


def test_lock_file_cannot_follow_symlink(tmp_path):
    module = operations()
    target = tmp_path / "other"
    target.write_text("keep")
    (tmp_path / ".workspace.lock").symlink_to(target)
    with pytest.raises(ValueError):
        module.WorkspaceLock(tmp_path).acquire()
    assert target.read_text() == "keep"


def test_storage_guard_respects_reserve_and_rechecks_actual_free_space(tmp_path):
    module = operations()
    disk = SimpleNamespace(total=10000, used=7000, free=3000)
    guard = module.StorageGuard(tmp_path, min_free_bytes=1000, disk_usage=lambda _: disk)
    guard.check(2000)
    with pytest.raises(module.StoragePressure, match="空间"):
        guard.check(2001)
    disk.free = 999
    with pytest.raises(module.StoragePressure):
        guard.check()
    assert guard.snapshot()["healthy"] is False
    assert guard.snapshot()["availableBytes"] == 0
    with pytest.raises(ValueError):
        guard.check(-1)


@pytest.mark.asyncio
async def test_runtime_readiness_tracks_real_worker_beats_and_excludes_secrets(tmp_path):
    module = operations()
    now = [100.0]
    deps = {key: {"available": True} for key in ("ffmpeg", "ffprobe", "chromium", "pillow")}
    monitor = module.RuntimeMonitor(
        tmp_path,
        module.StorageGuard(tmp_path, min_free_bytes=0),
        dependencies=deps,
        clock=lambda: now[0],
    )
    await monitor.start()
    state = {
        "jobs": [
            {
                "id": "job1",
                "status": "failed",
                "type": "download",
                "error": "https://example.test/?token=secret",
                "_headers": {"Cookie": "secret"},
                "failureCode": "storage-low",
                "createdAt": "2026-11-04T00:00:00Z",
            }
        ],
        "processingJobs": [{"id": "process1", "status": "queued"}],
        "scans": [],
    }
    monitor.beat("download")
    monitor.beat("processing")
    monitor.beat("automation")
    snapshot = monitor.snapshot(state, workers={"download": True, "processing": True, "automation": True})
    assert snapshot["ready"] and snapshot["status"] == "ready"
    assert not monitor.snapshot(state, workers={"download": True, "processing": True})["ready"]
    assert snapshot["queues"]["processing"]["queued"] == 1
    assert snapshot["queues"]["download"]["failed"] == 1
    assert snapshot["recentFailures"][0]["code"] == "storage-low"
    assert "secret" not in json.dumps(snapshot) and str(tmp_path) not in json.dumps(snapshot)
    now[0] += 16
    assert monitor.snapshot(state, workers={"download": True, "processing": True, "automation": True})["ready"] is False
    monitor.beat("download")
    monitor.beat("processing")
    monitor.beat("automation")
    assert not monitor.snapshot(state, workers={"download": False, "processing": True, "automation": True})["ready"]
    await monitor.stop()
    assert not monitor.snapshot(state, workers={"download": True, "processing": True, "automation": True})["ready"]


@pytest.mark.asyncio
async def test_missing_database_and_dependency_are_not_ready(tmp_path):
    module = operations()
    monitor = module.RuntimeMonitor(
        tmp_path,
        module.StorageGuard(tmp_path, min_free_bytes=0),
        dependencies={"ffmpeg": {"available": False}},
    )
    await monitor.start()
    monitor.beat("download")
    monitor.beat("processing")
    monitor.beat("automation")
    snapshot = monitor.snapshot(None, workers={"download": True, "processing": True, "automation": True})
    assert not snapshot["ready"] and not snapshot["database"]["available"]
    await monitor.stop()


@pytest.mark.asyncio
async def test_corrupt_queue_state_is_reported_without_crashing_diagnostics(tmp_path):
    module = operations()
    monitor = module.RuntimeMonitor(tmp_path, module.StorageGuard(tmp_path, 0), dependencies={})
    await monitor.start()
    snapshot = monitor.snapshot({"jobs": None, "scans": []}, workers={})
    assert not snapshot["database"]["available"] and not snapshot["ready"]
