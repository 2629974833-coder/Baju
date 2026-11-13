import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from backend.automation import AutomationService
from backend.store import Store
from backend.tasks import Conflict, TaskManager, find

URL = "https://example.test/gallery"


def image(name):
    return {"kind": "image", "sourceUrl": URL + "/" + name, "pageUrl": URL,
            "title": name, "engine": "http"}


class Page:
    def __init__(self):
        self.media = []
        self.outcome = {"status": "completed"}
        self.calls = []

    async def scan(self, site, options, on_asset, progress, cancelled):
        self.calls.append(options)
        for entry in self.media:
            await on_asset(dict(entry))
        await progress(self.outcome)


def setup(tmp_path, *, enabled=True, auto=True):
    store = Store(tmp_path / "workspace.sqlite3")
    scanner = Page()
    tasks = TaskManager(store, None, scanner, None)
    rule = {"id": "r1", "name": "画廊更新", "enabled": enabled, "autoDownload": auto,
            "siteId": "s1", "url": URL, "kinds": ["image", "video"], "intervalMinutes": 15,
            "status": "idle", "nextRunAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    def seed(state):
        state["rules"].append(rule)
        state["sites"].append({"id": "s1", "url": URL, "name": "gallery"})
    store.mutate(seed)
    return AutomationService(store, tasks), tasks, scanner


async def finish(service, tasks):
    await asyncio.gather(*tasks.scan_tasks.values())
    await asyncio.sleep(0)
    service.tick()
    return service.store.read()


@pytest.mark.asyncio
async def test_first_pass_only_enqueues_assets_new_to_workspace_and_repeat_adds_none(tmp_path):
    automation, tasks, page = setup(tmp_path)
    site = find(tasks.store.read()["sites"], "s1")
    page.media = [image("existing.jpg")]
    tasks.start_scan(site, {"url": URL})
    await finish(automation, tasks)
    page.media += [image("new.jpg"), {**image("preview.mp4"), "kind": "video"}]
    automation.run_now("r1")
    with pytest.raises(Conflict):
        automation.run_now("r1")
    state = await finish(automation, tasks)
    assert len(state["jobs"]) == 1 and state["jobs"][0]["ruleId"] == "r1"
    assert find(state["assets"], state["jobs"][0]["assetId"])["title"] == "new.jpg"
    assert state["rules"][0]["lastNewCount"] == 2 and state["rules"][0]["lastQueuedCount"] == 1
    assert page.calls[-1]["limit"] == 1 and page.calls[-1]["depth"] == 0
    automation.run_now("r1")
    state = await finish(automation, tasks)
    assert len(state["jobs"]) == 1 and state["rules"][0]["lastNewCount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("report", [{"status": "needs-user"}, {"status": "completed", "extraction": {"status": "needs-user"}}])
async def test_incomplete_scan_pauses_and_can_recover_only_reobserved_results(tmp_path, report):
    service, tasks, page = setup(tmp_path)
    page.media, page.outcome = [image("ok.jpg"), image("gone.jpg")], report
    service.run_now("r1")
    state = await finish(service, tasks)
    rule = state["rules"][0]
    assert rule["status"] == "needs-user" and not rule["enabled"] and not rule["nextRunAt"]
    assert state["jobs"] == []
    with pytest.raises(Conflict):
        service.run_now("r1")
    service.store.mutate(lambda state: state["rules"][0].update(enabled=True, status="idle"))
    page.media, page.outcome = [image("ok.jpg")], {"status": "completed"}
    service.run_now("r1")
    state = await finish(service, tasks)
    assert len(state["jobs"]) == 1
    assert find(state["assets"], state["jobs"][0]["assetId"])["title"] == "ok.jpg"


@pytest.mark.asyncio
async def test_reopen_after_success_before_settlement_is_idempotent(tmp_path):
    service, tasks, page = setup(tmp_path)
    page.media = [image("new.jpg")]
    service.run_now("r1")
    await asyncio.gather(*tasks.scan_tasks.values())
    await asyncio.sleep(0)
    reopened, reopened_tasks = Store(tasks.store.path), TaskManager(Store(tasks.store.path), None, page, None)
    AutomationService(reopened, reopened_tasks).tick()
    AutomationService(reopened, reopened_tasks).tick()
    state = reopened.read()
    assert len(state["jobs"]) == 1 and state["rules"][0]["lastQueuedCount"] == 1


@pytest.mark.asyncio
async def test_scheduled_scan_defers_while_manual_scan_or_maintenance_owns_workspace(tmp_path):
    service, tasks, page = setup(tmp_path, auto=False)
    service.store.mutate(lambda state: state["rules"][0].update(nextRunAt="2000-01-01T00:00:00+00:00"))
    page.media = [image("a.jpg")]
    tasks.maintenance = True
    service.tick()
    assert not tasks.scan_tasks
    tasks.maintenance = False
    service.tick()
    state = await finish(service, tasks)
    assert len(page.calls) == 1 and state["rules"][0]["status"] == "completed"
    assert not state["jobs"] and state["rules"][0]["lastNewCount"] == 1


@pytest.mark.asyncio
async def test_hidden_assets_and_unknown_audio_preview_are_skipped_but_new_episode_is_queued(tmp_path):
    service, tasks, page = setup(tmp_path)
    page.media = [image("hidden.jpg"), *[{**image(name), "kind": "video"}
                 for name in ("clip.mp4", "stream_audio0.m3u8", "preview.mp4", "episode-1.mp4")]]
    service.run_now("r1")
    await asyncio.gather(*tasks.scan_tasks.values())
    service.store.mutate(lambda state: state["assets"][0].update(dismissedFromDiscovery=True))
    state = await finish(service, tasks)
    assert [find(state["assets"], job["assetId"])["title"] for job in state["jobs"]] == ["episode-1.mp4"]


@pytest.mark.asyncio
async def test_manual_scan_and_low_storage_do_not_consume_an_unfinished_checkpoint(tmp_path):
    from backend.operations import StoragePressure

    class Disk:
        def check(self):
            raise StoragePressure("需要释放空间")

    service, tasks, page = setup(tmp_path)
    page.media = [image("new.jpg")]
    service.run_now("r1")
    await asyncio.gather(*tasks.scan_tasks.values())
    tasks.storage_guard = Disk()
    state = await finish(service, tasks)
    assert state["rules"][0]["status"] == "failed" and not state["jobs"]
    tasks.storage_guard = None
    service.run_now("r1")
    state = await finish(service, tasks)
    assert state["rules"][0]["lastQueuedCount"] == 1

    service.store.mutate(lambda state: state["rules"][0].update(nextRunAt="2000-01-01T00:00:00+00:00"))
    site = find(state["sites"], "s1")
    manual = tasks.start_scan(site, {"url": URL})
    service.tick()
    assert list(tasks.scan_tasks) == [manual["id"]]
    await finish(service, tasks)
    state = await finish(service, tasks)
    assert len(state["jobs"]) == 1 and state["rules"][0]["lastNewCount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retarget", [True, False])
async def test_download_gate_pauses_only_own_current_rule_and_refresh_uses_read_only_page(tmp_path, retarget):
    from backend.downloader import DownloadAccessRequired
    from backend.tasks import Control

    class Denied:
        async def download(self, asset, control, progress):
            raise DownloadAccessRequired(403)

    class Reauthorize(Page):
        async def resolve_asset(self, site, asset, cancelled):
            assert asset["_readOnly"] is True
            return {"status": "needs-user", "reasonCode": "verification-required"}

    service, tasks, page = setup(tmp_path)
    page.media = [image("new.jpg")]
    service.run_now("r1")
    state = await finish(service, tasks)
    if retarget:
        service.store.mutate(lambda state: state["rules"][0].update(url=URL + "/other"))
    tasks.downloader, tasks.scanner = Denied(), Reauthorize()
    await tasks._download(state["jobs"][0]["id"], Control())
    state = service.store.read()
    assert state["jobs"][0]["failureCode"] == "verification-required"
    assert state["rules"][0]["enabled"] is retarget
    assert state["rules"][0]["status"] == ("completed" if retarget else "needs-user")


@pytest.mark.asyncio
async def test_scheduler_picks_oldest_due_check_once_and_reaches_next_rule(tmp_path):
    service, tasks, page = setup(tmp_path, auto=False)
    page.media = [image("one.jpg")]
    def add_due(state):
        rule = state["rules"][0]
        rule.update(nextRunAt="2001-01-01T00:00:00+00:00")
        state["rules"].append({**rule, "id": "r2", "nextRunAt": "2000-01-01T00:00:00+00:00"})
    service.store.mutate(add_due)
    await service.start()
    try:
        for _ in range(100):
            rules = service.store.read()["rules"]
            if all(rule["status"] == "completed" for rule in rules):
                break
            await asyncio.sleep(0.02)
        assert all(rule["status"] == "completed" for rule in rules)
        assert rules[0]["lastNewCount"] == 0 and rules[1]["lastNewCount"] == 1
        assert len(page.calls) == 2
    finally:
        await service.close()
    assert not service.is_running


@pytest.mark.asyncio
async def test_history_bounds_only_dispensable_automated_page_checks(tmp_path):
    service, tasks, page = setup(tmp_path)
    def seed_history(state):
        for index in range(70):
            state["scans"].insert(0, {"id": f"old-{index}", "siteId": "s1", "url": URL,
                "ruleId": "r1", "status": "completed",
                "catalog": {"entries": [], "expectedCount": None, "seriesKey": "unconfirmed-route"}})
        state["scans"].extend([
            {"id": "manual", "siteId": "s1", "url": URL, "status": "completed"},
            {"id": "catalog", "ruleId": "r1", "catalog": {"entries": [{"pageUrl": URL}]}},
            {"id": "another", "ruleId": "r2", "status": "failed"},
        ])
    service.store.mutate(seed_history)
    service.run_now("r1")
    state = await finish(service, tasks)
    ids = {scan["id"] for scan in state["scans"]}
    assert len(ids) == 33 and {"manual", "catalog", "another", "old-69", "old-41"} <= ids
    assert "old-40" not in ids and state["rules"][0]["lastScanId"] in ids


@pytest.mark.asyncio
async def test_automatic_download_preserves_explicit_candidate_lock(tmp_path):
    service, tasks, page = setup(tmp_path)
    page.media = [{**image("locked.jpg"), "locked": True}]
    service.run_now("r1")
    state = await finish(service, tasks)
    assert state["assets"][0]["locked"] and not state["jobs"]


@pytest.mark.asyncio
async def test_failed_run_does_not_mask_rule_access_error_with_completed_scan_message(tmp_path):
    service, tasks, page = setup(tmp_path)
    page.outcome = {"status": "completed", "message": "扫描完成", "extraction": {"status": "needs-user"}}
    service.run_now("r1")
    state = await finish(service, tasks)
    assert "授权" in state["rules"][0]["error"]


@pytest.mark.asyncio
async def test_wall_clock_jump_is_detected_at_heartbeat_without_polling_an_idle_workspace(tmp_path, monkeypatch):
    import backend.automation as module

    class WallClock(datetime):
        delta = timedelta()

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + cls.delta

    service, tasks, page = setup(tmp_path, auto=False)
    monkeypatch.setattr(module, "datetime", WallClock)
    service.heartbeat_interval = 0.005
    await service.start()
    try:
        await asyncio.sleep(0.02)
        assert not page.calls
        WallClock.delta = timedelta(days=2)
        for _ in range(40):
            await asyncio.sleep(0.01)
            if service.store.read()["rules"][0]["status"] == "completed":
                break
        assert len(page.calls) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_plain_http_and_empty_spa_access_errors_pause_unattended_checks(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from io import BytesIO
    from threading import Thread

    from PIL import Image

    from backend.browser import BrowserService
    from backend.network import NetworkPolicy
    from backend.scanner import Scanner

    picture = BytesIO()
    Image.new("RGB", (640, 480), "teal").save(picture, format="PNG")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/splash.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(picture.getvalue())
                return
            shell = self.path in {"/shell", "/shell-with-logo"}
            status = 200 if shell else (401 if self.path in {"/session", "/basic"} else 403)
            if self.path == "/gone":
                status = 404
            body = b"<script>fetch('/session')</script>" if shell else b"forbidden"
            if self.path == "/shell-with-logo":
                body += b'<img width="640" height="480" src="/splash.png">'
            self.send_response(status)
            if self.path == "/basic":
                self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    service, tasks, _ = setup(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    policy = NetworkPolicy({"127.0.0.1"})
    browser = BrowserService(tmp_path, policy)
    tasks.browser, tasks.scanner = browser, Scanner(browser, policy)
    try:
        for path in ("/plain", "/basic", "/shell", "/shell-with-logo", "/gone"):
            def reset(state):
                find(state["sites"], "s1")["url"] = origin
                state["rules"][0].update(url=origin + path, status="idle", enabled=True)
            tasks.store.mutate(reset)
            service.run_now("r1")
            state = await finish(service, tasks)
            assert state["rules"][0]["status"] == ("failed" if path == "/gone" else "needs-user"), path
            assert state["rules"][0]["enabled"] is (path == "/gone") and not state["jobs"]
            if path == "/shell-with-logo":
                assert any(asset["sourceUrl"].endswith("splash.png") for asset in state["assets"])
    finally:
        await browser.close()
        server.shutdown()
        server.server_close()
        thread.join()
