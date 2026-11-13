"""Controlled queue exercises; transfer protocol behavior has separate HTTP tests."""

import asyncio
import hashlib
import io
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.downloader import Downloader as MediaDownloader
from backend.store import Store
from backend.tasks import Conflict, Control, TaskManager


class Browser:
    async def open_session(self, site):
        pass

    async def cookies_for(self, *_args):
        return []


def seed(store, count=1, concurrency=3):
    def add(state):
        state["settings"]["concurrency"] = concurrency
        state["assets"] = [
            {"id": f"asset_{index}", "siteId": "chunlou", "status": "discovered",
             "title": f"Controlled asset {index}"}
            for index in range(count)
        ]
    store.mutate(add)


async def until(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def result_for(asset):
    return {"path": f"assets/{asset['id']}/original.bin", "size": 1000,
            "sha256": hashlib.sha256(b"fixture").hexdigest(), "mime": "video/mp4"}


@pytest.mark.asyncio
async def test_cancelled_writer_keeps_asset_reserved_until_it_exits(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Downloader:
        async def download(self, asset, control, progress):
            entered.set()
            await release.wait()
            control.check()

        def discard_partial(self, asset_id):
            pass

    store = Store(tmp_path / "state.db")
    seed(store)
    manager = TaskManager(store, Browser(), None, Downloader())
    manager.enqueue(["asset_0"])
    await manager.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        job_id = store.read()["jobs"][0]["id"]
        manager.job_action(job_id, "cancel")
        with pytest.raises(Conflict):
            manager.job_action(job_id, "retry")
        assert manager.enqueue(["asset_0"]) == {"added": 0, "blocked": 0, "duplicates": 1}
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_scheduler_never_starts_two_writers_for_legacy_duplicate_jobs(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    class Downloader:
        async def download(self, asset, control, progress):
            writes.append(asset["id"])
            entered.set()
            await release.wait()
            control.check()
            return result_for(asset)

    store = Store(tmp_path / "state.db")
    seed(store, concurrency=2)
    store.mutate(lambda state: state["jobs"].extend([
        {"id": "job_a", "assetId": "asset_0", "status": "queued"},
        {"id": "job_b", "assetId": "asset_0", "status": "queued"},
    ]))
    manager = TaskManager(store, Browser(), None, Downloader())
    manager._scheduler = asyncio.create_task(manager._schedule())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        assert writes == ["asset_0"]
        assert len(manager.active) == 1
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_progress_is_throttled_but_events_and_pause_are_durable(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    snapshots = []

    class CountingStore(Store):
        writes = 0

        def mutate(self, callback):
            self.writes += 1
            return super().mutate(callback)

    class Downloader:
        async def download(self, asset, control, progress):
            for amount in range(1, 101):
                await progress({"downloadedBytes": amount, "totalBytes": 1000,
                                "progress": amount / 10, "phase": "downloading"})
            await progress({"phase": "retrying", "retryCount": 1, "retryInSeconds": 1})
            snapshots.append(store.read()["jobs"][0])
            await progress({"phase": "downloading", "retryInSeconds": 0})
            await progress({"downloadedBytes": 101, "progress": 10.1})
            entered.set()
            await release.wait()
            control.check()

    store = CountingStore(tmp_path / "state.db")
    seed(store)
    manager = TaskManager(store, Browser(), None, Downloader())
    manager.enqueue(["asset_0"])
    job_id = store.read()["jobs"][0]["id"]
    before = store.writes
    control = Control()
    task = asyncio.create_task(manager._download(job_id, control))
    manager.active[job_id] = (control, task)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert snapshots[0]["phase"] == "retrying"
        assert snapshots[0]["retryCount"] == 1
        assert snapshots[0]["downloadedBytes"] == 100
        manager.job_action(job_id, "pause")
        assert store.read()["jobs"][0]["downloadedBytes"] == 101
        assert store.writes - before <= 8
    finally:
        release.set()
        await task
    job = store.read()["jobs"][0]
    assert job["status"] == "paused"
    assert job["downloadedBytes"] == 101


@pytest.mark.asyncio
async def test_browser_startup_failure_does_not_stop_other_jobs(tmp_path):
    class FailingBrowser:
        calls = 0

        async def open_session(self, site):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("private browser startup error")

    class Downloader:
        async def download(self, asset, control, progress):
            return result_for(asset)

    store = Store(tmp_path / "state.db")
    seed(store, count=3, concurrency=1)
    manager = TaskManager(store, FailingBrowser(), None, Downloader())
    await manager.start()
    try:
        manager.enqueue(["asset_0", "asset_1", "asset_2"])
        await until(lambda: all(job["status"] in {"failed", "completed"}
                               for job in store.read()["jobs"]))
        assert [job["status"] for job in store.read()["jobs"]] == [
            "failed", "completed", "completed"
        ]
        assert "private" not in store.read()["jobs"][0]["error"]
        assert not manager.active
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("pause", "paused"), ("cancel", "cancelled")])
async def test_control_during_transfer_completion_is_not_overwritten(tmp_path, action, expected):
    entered, release = asyncio.Event(), asyncio.Event()

    class Downloader:
        async def download(self, asset, control, progress):
            entered.set()
            await release.wait()
            return result_for(asset)

        def discard_partial(self, asset_id):
            pass

    store = Store(tmp_path / "state.db")
    seed(store)
    manager = TaskManager(store, Browser(), None, Downloader())
    manager.enqueue(["asset_0"])
    job_id = store.read()["jobs"][0]["id"]
    control = Control()
    task = asyncio.create_task(manager._download(job_id, control))
    manager.active[job_id] = (control, task)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        manager.job_action(job_id, action)
    finally:
        release.set()
        await task
    assert store.read()["jobs"][0]["status"] == expected


@pytest.mark.asyncio
async def test_shutdown_flushes_pending_progress_and_restart_keeps_waiting_jobs(tmp_path):
    entered = asyncio.Event()

    class Downloader:
        async def download(self, asset, control, progress):
            await progress({"downloadedBytes": 100, "progress": 10})
            await progress({"downloadedBytes": 101, "progress": 10.1})
            entered.set()
            while True:
                control.check()
                await asyncio.sleep(0.01)

    store = Store(tmp_path / "state.db")
    seed(store, count=2, concurrency=1)
    manager = TaskManager(store, Browser(), None, Downloader())
    manager.enqueue(["asset_0", "asset_1"])
    await manager.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        await manager.close()
    restored = Store(tmp_path / "state.db")
    restored.recover_interrupted()
    jobs = restored.read()["jobs"]
    assert jobs[0]["status"] == "paused"
    assert jobs[0]["downloadedBytes"] == 101
    assert jobs[0]["progress"] == 10.1
    assert jobs[1]["status"] == "queued"


@pytest.mark.asyncio
async def test_mixed_72_job_batch_is_bounded_and_resumes_independently(tmp_path):
    class Downloader:
        active = 0
        peak = 0
        attempts = {}
        held = asyncio.Event()

        async def download(self, asset, control, progress):
            index = int(asset["id"].split("_")[1])
            self.active += 1
            self.peak = max(self.active, self.peak)
            self.attempts[index] = self.attempts.get(index, 0) + 1
            try:
                await progress({"phase": "downloading", "downloadedBytes": 400,
                                "totalBytes": 1000, "progress": 40})
                if index == 0 and self.attempts[index] == 1:
                    self.held.set()
                    while True:
                        control.check()
                        await asyncio.sleep(0.01)
                if index % 11 == 0:
                    await progress({"phase": "retrying", "retryCount": 1,
                                    "retryInSeconds": 0.01})
                    await asyncio.sleep(0.01)
                    control.check()
                    await progress({"phase": "downloading", "retryInSeconds": 0})
                await asyncio.sleep(0.005)
                control.check()
                if index % 13 == 12:
                    raise ValueError("controlled fatal media failure")
                await progress({"phase": "verifying", "downloadedBytes": 1000,
                                "progress": 100})
                return result_for(asset)
            finally:
                self.active -= 1

    store = Store(tmp_path / "state.db")
    seed(store, count=72, concurrency=6)
    downloader = Downloader()
    manager = TaskManager(store, Browser(), None, downloader)
    await manager.start()
    try:
        assert manager.enqueue([f"asset_{index}" for index in range(72)])["added"] == 72
        await asyncio.wait_for(downloader.held.wait(), 2)
        job_id = store.read()["jobs"][0]["id"]
        manager.job_action(job_id, "pause")
        await until(lambda: job_id not in manager.active)
        assert store.read()["jobs"][0]["downloadedBytes"] == 400
        resumed = manager.job_action(job_id, "resume")
        assert resumed["downloadedBytes"] == 400
        await until(lambda: all(job["status"] in {"completed", "failed"}
                               for job in store.read()["jobs"]), timeout=12)
        jobs = store.read()["jobs"]
        assert len(jobs) == 72
        assert sum(job["status"] == "failed" for job in jobs) == 5
        assert sum(job["status"] == "completed" for job in jobs) == 67
        assert 1 < downloader.peak <= 6
        assert downloader.attempts[0] == 2
        assert all(count == 1 for index, count in downloader.attempts.items() if index)
        assert not manager.active
    finally:
        await manager.close()


@pytest.fixture
def batch_origin():
    stream = io.BytesIO()
    Image.frombytes("RGB", (256, 256), random.Random(74).randbytes(256 * 256 * 3)).save(
        stream, "PNG"
    )
    body, requests = stream.getvalue(), []
    counts, lock = {}, threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            index = int(self.path.removeprefix("/image/"))
            with lock:
                counts[index] = counts.get(index, 0) + 1
                attempt = counts[index]
            if index % 13 == 12:
                status = 403
            elif index % 11 == 0 and attempt == 1:
                status = 503
            else:
                status = 206 if self.headers.get("Range") else 200
            offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
            requests.append({"index": index, "status": status, "offset": offset})
            self.send_response(status)
            if status in {403, 503}:
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = body[offset:]
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("ETag", '"batch-image-v1"')
            if status == 206:
                self.send_header("Content-Range", f"bytes {offset}-{len(body)-1}/{len(body)}")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", body=body,
                          requests=requests, counts=counts)
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.asyncio
async def test_72_real_http_jobs_retry_fail_and_resume_with_concurrency_four(tmp_path, batch_origin):
    """Real HTTP, image verification, checkpoints and SQLite run in one bounded batch."""
    paused = asyncio.Event()

    class Policy:
        proxy_url = None

        async def validate(self, url):
            if not url.startswith(batch_origin.url + "/"):
                raise ValueError("fixture origin only")
            return url

    class ObservedDownloader(MediaDownloader):
        active_count = 0
        peak = 0

        async def download(self, asset, control, progress):
            self.active_count += 1
            self.peak = max(self.peak, self.active_count)

            async def observe(values):
                await progress(values)
                if (asset["id"] == "asset_0" and values.get("downloadedBytes", 0) >= 65536
                        and not paused.is_set()):
                    job = next(job for job in store.read()["jobs"]
                               if job["assetId"] == "asset_0")
                    manager.job_action(job["id"], "pause")
                    paused.set()

            try:
                return await super().download(asset, control, observe)
            finally:
                self.active_count -= 1

    store = Store(tmp_path / "state.db")
    seed(store, count=72, concurrency=4)

    def sources(state):
        for index, asset in enumerate(state["assets"]):
            asset.update(kind="image", engine="http", sourceUrl=f"{batch_origin.url}/image/{index}")

    store.mutate(sources)
    browser = Browser()
    downloader = ObservedDownloader(tmp_path, browser, Policy())
    manager = TaskManager(store, browser, None, downloader)
    await manager.start()
    try:
        manager.enqueue([f"asset_{index}" for index in range(72)])
        await asyncio.wait_for(paused.wait(), 8)
        job_id = store.read()["jobs"][0]["id"]
        await until(lambda: job_id not in manager.active)
        saved = store.read()["jobs"][0]["downloadedBytes"]
        assert 0 < saved < len(batch_origin.body)
        assert manager.job_action(job_id, "resume")["downloadedBytes"] == saved
        await until(lambda: all(job["status"] in {"completed", "failed", "needs-user"}
                               for job in store.read()["jobs"]), timeout=25)
        state = store.read()
        assert sum(job["status"] == "completed" for job in state["jobs"]) == 67
        assert sum(job["status"] == "needs-user" for job in state["jobs"]) == 5
        assert not any(job["status"] == "failed" for job in state["jobs"])
        assert 1 < downloader.peak <= 4
        assert state["jobs"][0]["resumedBytes"] == saved
        assert any(request["index"] == 0 and request["status"] == 206
                   and request["offset"] == saved for request in batch_origin.requests)
        assert all(batch_origin.counts[index] == 1 for index in range(12, 72, 13))
        assert all(batch_origin.counts[index] == 2 for index in range(11, 72, 11))
        expected_sha = hashlib.sha256(batch_origin.body).hexdigest()
        for asset in state["assets"]:
            if asset["status"] == "downloaded":
                assert asset["sha256"] == expected_sha
                assert (tmp_path / asset["_path"]).read_bytes() == batch_origin.body
        assert not manager.active
    finally:
        await manager.close()
