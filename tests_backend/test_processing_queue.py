import asyncio
import hashlib

import pytest
from PIL import Image

from backend.processing_tasks import ProcessingQueue
from backend.store import Store
from backend.tasks import Conflict


def workspace(tmp_path):
    store = Store(tmp_path / "workspace.sqlite3")
    path = tmp_path / "assets/sample/original.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (120, 80), "white").save(path)
    asset = {
        "id": "sample",
        "siteId": "chunlou",
        "title": "Neutral image",
        "kind": "image",
        "status": "downloaded",
        "_path": "assets/sample/original.png",
        "fileUrl": "/api/assets/sample/file",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "versions": [{"id": "original", "label": "原始文件", "path": "/api/assets/sample/file"}],
    }
    store.mutate(lambda state: state["assets"].append(asset))
    return store


class Engine:
    def __init__(self, root, *, block=False, fail=False):
        self.root, self.block, self.fail = root, block, fail
        self.started = asyncio.Event()
        self.calls = []

    async def process(self, asset, version_id, options, control, progress):
        self.calls.append(version_id)
        self.started.set()
        while self.block:
            control.check()
            await progress({"progress": 20, "phase": "processing"})
            await asyncio.sleep(0.01)
        if self.fail:
            raise ValueError("Neutral fixture failure")
        target = self.root / "assets" / asset["id"] / "versions" / version_id / "processed.png"
        target.parent.mkdir(parents=True)
        Image.new("RGB", (120, 80), "green").save(target)
        return {
            "path": str(target.relative_to(self.root)),
            "size": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "mime": "image/png",
            "width": 120,
            "height": 80,
            "options": options,
            "processor": "pillow",
            "sourceSha256": asset["sha256"],
        }


async def wait_status(store, status):
    for _ in range(200):
        jobs = store.read()["processingJobs"]
        if jobs and jobs[-1]["status"] == status:
            return jobs[-1]
        await asyncio.sleep(0.02)
    raise AssertionError(jobs)


@pytest.mark.asyncio
async def test_real_version_registration_preserves_original_and_deduplicates(tmp_path):
    store = workspace(tmp_path)
    original = (tmp_path / "assets/sample/original.png").read_bytes()
    engine = Engine(tmp_path)
    queue = ProcessingQueue(store, engine)
    assert queue.enqueue(["sample", "sample"], {"mode": "add", "text": "SAMPLE"})["added"] == 1
    assert queue.enqueue(["sample"], {"mode": "add", "text": "SAMPLE"})["duplicates"] == 1
    await queue.start()
    try:
        await wait_status(store, "completed")
        assert queue.enqueue(["sample"], {"mode": "add", "text": "SAMPLE"})["duplicates"] == 1
        asset = store.read()["assets"][0]
        assert asset["status"] == "downloaded" and len(asset["versions"]) == 2
        version = asset["versions"][1]
        assert version["status"] == "completed" and version["sourceSha256"] == asset["sha256"]
        assert (tmp_path / version["_path"]).is_file()
        assert version["fileUrl"].endswith(f"/{version['id']}/file")
        assert "_path" not in store.public_state()["assets"][0]["versions"][1]
        assert (tmp_path / "assets/sample/original.png").read_bytes() == original
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_pause_then_resume_reprocesses_and_cancel_never_registers_version(tmp_path):
    store = workspace(tmp_path)
    engine = Engine(tmp_path, block=True)
    queue = ProcessingQueue(store, engine)
    queue.enqueue(["sample"], {"mode": "add", "text": "SAMPLE"})
    await queue.start()
    try:
        await engine.started.wait()
        job = store.read()["processingJobs"][0]
        queue.job_action(job["id"], "pause")
        await wait_status(store, "paused")
        while queue.active:
            await asyncio.sleep(0.01)
        engine.block = False
        queue.job_action(job["id"], "resume")
        await wait_status(store, "completed")
        assert len(set(engine.calls)) == 2
        engine.block = True
        engine.started.clear()
        queue.enqueue(["sample"], {"mode": "add", "text": "NEXT"})
        await engine.started.wait()
        next_job = store.read()["processingJobs"][-1]
        queue.job_action(next_job["id"], "cancel")
        await wait_status(store, "cancelled")
        assert len(store.read()["assets"][0]["versions"]) == 2
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_interrupted_process_pauses_on_reopen_and_retry_cleans_own_orphan(tmp_path):
    store = workspace(tmp_path)
    queue = ProcessingQueue(store, Engine(tmp_path))
    queue.enqueue(["sample"], {"mode": "add", "text": "SAMPLE"})
    orphan_id = "version_interrupted"
    orphan = tmp_path / "assets/sample/versions" / orphan_id
    orphan.mkdir(parents=True)
    (orphan / "processed.png").write_bytes(b"incomplete")
    store.mutate(
        lambda state: state["processingJobs"][0].update(status="running", versionId=orphan_id)
    )
    recovered = ProcessingQueue(Store(store.path), Engine(tmp_path))
    await recovered.start()
    try:
        job = await wait_status(store, "paused")
        recovered.job_action(job["id"], "resume")
        await wait_status(store, "completed")
        assert not orphan.exists()
        assert len(store.read()["assets"][0]["versions"]) == 2
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_failed_task_can_retry_without_losing_original(tmp_path):
    store = workspace(tmp_path)
    engine = Engine(tmp_path, fail=True)
    queue = ProcessingQueue(store, engine)
    queue.enqueue(["sample"], {"mode": "add", "text": "SAMPLE"})
    await queue.start()
    try:
        job = await wait_status(store, "failed")
        assert len(store.read()["assets"][0]["versions"]) == 1
        while queue.active:
            await asyncio.sleep(0.01)
        engine.fail = False
        queue.job_action(job["id"], "retry")
        await wait_status(store, "completed")
        assert len(set(engine.calls)) == 2
    finally:
        await queue.close()


def test_only_downloaded_files_can_process_and_maintenance_blocks_admission(tmp_path):
    store = workspace(tmp_path)
    queue = ProcessingQueue(store, Engine(tmp_path))
    assert queue.enqueue(["missing"], {"mode": "add", "text": "SAMPLE"})["blocked"] == 1
    queue.maintenance = True
    with pytest.raises(Conflict):
        queue.enqueue(["sample"], {"mode": "add", "text": "SAMPLE"})
    assert store.read()["processingJobs"] == []


@pytest.mark.asyncio
async def test_storage_pressure_pauses_work_without_claiming_a_version(tmp_path):
    store = workspace(tmp_path)

    class Pressure(ValueError):
        code = "storage-low"

    class Guard:
        low = False

        def check(self, required_bytes=0):
            if self.low:
                raise Pressure("磁盘剩余空间不足")

    guard = Guard()
    engine = Engine(tmp_path)
    queue = ProcessingQueue(store, engine, storage_guard=guard)
    queue.enqueue(["sample"], {"mode": "add", "text": "SAMPLE"})
    guard.low = True
    await queue.start()
    try:
        job = await wait_status(store, "paused")
        assert job["failureCode"] == "storage-low" and engine.calls == []
        assert len(store.read()["assets"][0]["versions"]) == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_retry_cannot_duplicate_a_later_completed_identical_version(tmp_path):
    store = workspace(tmp_path)
    engine = Engine(tmp_path, fail=True)
    queue = ProcessingQueue(store, engine)
    options = {"mode": "add", "text": "SAMPLE"}
    queue.enqueue(["sample"], options)
    await queue.start()
    try:
        failed = await wait_status(store, "failed")
        while queue.active:
            await asyncio.sleep(0.01)
        engine.fail = False
        queue.enqueue(["sample"], options)
        await wait_status(store, "completed")
        with pytest.raises(Conflict):
            queue.job_action(failed["id"], "retry")
        assert len(store.read()["assets"][0]["versions"]) == 2
    finally:
        await queue.close()


def test_chunlou_glyph_queue_skips_unrelated_assets_in_mixed_request(tmp_path):
    store = workspace(tmp_path)
    def add_videos(state):
        reference = state["assets"][0]
        state["assets"].extend([{**reference, "id": "foreign", "siteId": "other", "kind": "video"},
                                {**reference, "id": "episode", "siteId": "chunlou", "kind": "video"}])
    store.mutate(add_videos)
    queue = ProcessingQueue(store, Engine(tmp_path))
    result = queue.enqueue(["sample", "foreign", "episode"], {"mode": "remove", "method": "chunlou-glyph"})
    assert result == {"added": 1, "blocked": 2, "duplicates": 0}
    assert store.read()["processingJobs"][0]["assetId"] == "episode"


async def test_delete_running_job_waits_for_cleanup_and_other_jobs_continue(tmp_path):
    store = workspace(tmp_path)
    engine = Engine(tmp_path, block=True)
    queue = ProcessingQueue(store, engine)
    for text in ("FIRST", "SECOND"):
        queue.enqueue(["sample"], {"text": text})
    await queue.start()
    try:
        await asyncio.wait_for(engine.started.wait(), timeout=2)
        first, second = store.read()["processingJobs"]
        attempt = tmp_path / "assets/sample/versions" / first["versionId"]
        attempt.mkdir(parents=True)
        (attempt / "unfinished.tmp").write_bytes(b"work")
        await asyncio.wait_for(queue.delete_job(first["id"]), timeout=2)
        assert first["id"] not in queue.active and not attempt.exists()
        assert [j["id"] for j in store.read()["processingJobs"]] == [second["id"]]
        engine.block = False
        await wait_status(store, "completed")
        assert len(store.read()["assets"][0]["versions"]) == 2
    finally:
        await queue.close()
