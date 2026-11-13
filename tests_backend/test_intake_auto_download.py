"""Single-work automatic downloads are committed with scan completion, once."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.operations import StoragePressure
from backend.store import Store
from backend.tasks import TaskManager
from tests_backend.test_app import Browser, app_at, until

PAGE = "https://example.com/watch/1"


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, [], {}])
def test_auto_download_requires_a_strict_boolean(tmp_path, value):
    with TestClient(app_at(tmp_path)) as client:
        response = client.post("/api/intake", json={"scope": "single", "url": PAGE, "autoDownload": value})
        assert response.status_code == 422
        assert client.get("/api/state").json()["scans"] == []


@pytest.mark.parametrize("scope", ["site", "series"])
def test_auto_download_rejects_broader_scopes_before_creating_a_scan(tmp_path, scope):
    with TestClient(app_at(tmp_path)) as client:
        before = client.get("/api/state").json()
        response = client.post("/api/intake", json={"scope": scope, "url": PAGE, "autoDownload": True})
        assert response.status_code == 422
        after = client.get("/api/state").json()
        assert after["scans"] == before["scans"] and after["sites"] == before["sites"]


@pytest.mark.parametrize("extra", [{}, {"autoDownload": False}])
def test_default_or_disabled_intake_still_requires_manual_download(tmp_path, extra):
    with TestClient(app_at(tmp_path)) as client:
        response = client.post("/api/intake", json={"scope": "single", "url": PAGE, **extra})
        assert response.status_code == 200
        scan = response.json()
        assert scan["downloadAfterScan"] is False
        state = until(client, lambda state: state["scans"][0]["status"] == "completed")
        assert state["jobs"] == []
        result = client.get(f"/api/intake/{scan['id']}/results").json()
        assert result["downloadAfterScan"] is False and result["downloadSummary"] is None


def test_single_intake_downloads_without_any_second_ui_action_and_survives_restart(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/intake", json={"scope": "single", "url": PAGE, "autoDownload": True})
        assert response.status_code == 200
        scan = response.json()
        assert scan["downloadAfterScan"] is True and "_singleIntakeDownload" not in scan
        state = until(client, lambda state: state["jobs"] and state["jobs"][0]["status"] == "completed")
        assert len(state["jobs"]) == 1 and state["assets"][0]["status"] == "downloaded"
        summary = {"added": 1, "blocked": 0, "duplicates": 0}
        for _ in range(3):
            result = client.get(f"/api/intake/{scan['id']}/results").json()
            assert result["downloadAfterScan"] is True and result["downloadSummary"] == summary
        stored = app.state.store.read()["scans"][0]
        assert stored["_singleIntakeDownload"] is True
    with TestClient(app_at(tmp_path)) as client:
        state = client.get("/api/state").json()
        assert len(state["jobs"]) == 1 and state["scans"][0]["downloadSummary"] == summary
        assert "_singleIntakeDownload" not in state["scans"][0]


def candidate(name="video", **extra):
    return {"sourceUrl": f"https://example.com/{name}.mp4", "pageUrl": PAGE,
            "kind": "video", "engine": "http", "title": name, **extra}


def manager_for(tmp_path, scanner, guard=None):
    store = Store(tmp_path / "workspace.sqlite3")
    manager = TaskManager(store, Browser(), scanner, None, storage_guard=guard)
    return manager, store.read()["sites"][0]


def start(manager, site, **extra):
    return manager.start_scan(site, {"mode": "single", "intakeScope": "single", "url": PAGE,
                                    "kinds": ["video"], "autoDownload": True, **extra})


async def finished(manager, scan):
    await manager.scan_tasks[scan["id"]]
    await asyncio.sleep(0)
    return manager.store.read()


async def test_completion_queues_only_observed_same_site_selected_kind_once(tmp_path):
    snapshots = []

    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            for value in [candidate(), candidate(), candidate("poster", kind="image"),
                          candidate("locked", locked=True), candidate("unsupported", unsupported=True)]:
                await asset(value)
            def inject_refs(state):
                state["scans"][0]["_intakeRefs"][""][PAGE].extend(["foreign", "saved", "reserved"])
            manager.store.mutate(inject_refs)
            for _ in range(2):
                await progress({"status": "completed"})
                snapshots.append(manager.store.read())

    manager, site = manager_for(tmp_path, Scanner())
    def seed(state):
        for identifier, owner, status in [("foreign", "other-site", "discovered"),
                                           ("unobserved", site["id"], "discovered"),
                                           ("saved", site["id"], "downloaded"),
                                           ("reserved", site["id"], "discovered")]:
            state["assets"].append({"id": identifier, "siteId": owner, "kind": "video",
                                    "status": status, "pageUrl": PAGE, "title": identifier})
        state["jobs"].append({"id": "already", "assetId": "reserved", "status": "paused"})
    manager.store.mutate(seed)
    state = await finished(manager, start(manager, site))
    summary = {"added": 1, "blocked": 2, "duplicates": 2}
    assert state["scans"][0]["downloadSummary"] == summary
    assert len(state["jobs"]) == 2
    selected = next(asset for asset in state["assets"] if asset["title"] == "video")
    assert state["jobs"][-1]["assetId"] == selected["id"]
    assert all(len(snapshot["jobs"]) == 2 and snapshot["scans"][0]["downloadSummary"] == summary
               for snapshot in snapshots)


@pytest.mark.parametrize("report", [
    {"status": "failed"}, {"status": "needs-user"}, {"status": "cancelled"},
    {"status": "timed-out"}, {"status": "unsupported-drm"},
    {"status": "completed", "limited": True},
    {"status": "completed", "catalog": {"entries": [], "truncated": True}},
    {"status": "completed", "extraction": {"status": "limited"}},
])
async def test_incomplete_or_unsuccessful_scan_never_queues_partial_observations(tmp_path, report):
    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            await asset(candidate())
            await progress(report)
    manager, site = manager_for(tmp_path, Scanner())
    state = await finished(manager, start(manager, site))
    assert state["jobs"] == []


@pytest.mark.parametrize("stop", ["cancelled", "closing", "late-success-after-needs-user"])
async def test_stopped_scan_cannot_enqueue_even_if_scanner_reports_late_completion(tmp_path, stop):
    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            await asset(candidate())
            if stop == "closing":
                manager._closing = True
            elif stop == "cancelled":
                next(iter(manager.scan_controls.values())).cancelled = True
            else:
                await progress({"status": "needs-user"})
            await progress({"status": "completed"})
    manager, site = manager_for(tmp_path, Scanner())
    state = await finished(manager, start(manager, site))
    assert state["jobs"] == []


async def test_cancelled_scan_waiting_for_completion_does_not_queue(tmp_path):
    observed = asyncio.Event()
    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            await asset(candidate())
            observed.set()
            await asyncio.Event().wait()
    manager, site = manager_for(tmp_path, Scanner())
    scan = start(manager, site)
    await asyncio.wait_for(observed.wait(), 2)
    manager.cancel_scan(scan["id"])
    state = await finished(manager, scan)
    assert state["jobs"] == [] and state["scans"][0]["status"] == "cancelled"


async def test_capacity_failure_is_durable_and_can_be_manually_retried(tmp_path):
    class Guard:
        full = False
        def check(self):
            if self.full:
                raise StoragePressure("测试容量不足")
    guard = Guard()
    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            await asset(candidate())
            guard.full = True
            await progress({"status": "completed"})
            guard.full = False
            await progress({"status": "completed"})
    manager, site = manager_for(tmp_path, Scanner(), guard)
    state = await finished(manager, start(manager, site))
    assert state["jobs"] == [] and state["scans"][0]["status"] == "completed"
    assert state["scans"][0]["downloadSummary"] == {
        "added": 0, "blocked": 0, "duplicates": 0, "error": "测试容量不足"}
    assert manager.enqueue([state["assets"][0]["id"]])["added"] == 1


async def test_completed_empty_scan_records_zero_without_enqueuing_old_media(tmp_path):
    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            await progress({"status": "completed"})
    manager, site = manager_for(tmp_path, Scanner())
    state = await finished(manager, start(manager, site))
    assert state["jobs"] == []
    assert state["scans"][0]["downloadSummary"] == {"added": 0, "blocked": 0, "duplicates": 0}


async def test_one_work_downloads_all_selected_video_and_image_attachments(tmp_path):
    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            for item in [candidate("first"), candidate("second"), candidate("photo", kind="image")]:
                await asset(item)
            await progress({"status": "completed"})
    manager, site = manager_for(tmp_path, Scanner())
    state = await finished(manager, start(manager, site, kinds=["video", "image"]))
    assert state["scans"][0]["downloadSummary"]["added"] == 3
    assert {job["assetId"] for job in state["jobs"]} == {asset["id"] for asset in state["assets"]}


async def test_unexpected_scan_failure_does_not_suggest_login_or_download(tmp_path):
    class Scanner:
        async def scan(self, site, options, asset, progress, cancelled):
            await asset(candidate())
            raise RuntimeError("untrusted upstream details")
    manager, site = manager_for(tmp_path, Scanner())
    state = await finished(manager, start(manager, site))
    assert state["jobs"] == [] and state["scans"][0]["status"] == "failed"
    assert state["scans"][0]["error"] == "站点连接或页面解析失败，请稍后重试"
