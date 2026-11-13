import asyncio
import copy

import pytest

from backend.downloader import DownloadAccessRequired
from backend.site_adapters import build_catalog
from backend.store import Store
from backend.tasks import Control, TaskManager
from tests_backend.test_site_adapters import episode


class Browser:
    async def open_session(self, site):
        pass


class Transfer:
    def __init__(self, failures=1):
        self.failures = failures
        self.seen = []
        self.discarded = []

    async def download(self, asset, control, progress):
        self.seen.append(copy.deepcopy(asset))
        if len(self.seen) <= self.failures:
            raise DownloadAccessRequired(403)
        return {
            "path": f"assets/{asset['id']}/original.mp4",
            "size": 100,
            "sha256": "f" * 64,
            "mime": "video/mp4",
            "duration": 31.2,
        }

    def discard_partial(self, identifier):
        self.discarded.append(identifier)


class Resolver:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "resolved", "candidate": episode("fresh")}

    async def resolve_asset(self, site, asset, cancelled):
        self.calls.append(copy.deepcopy(asset))
        return self.result


def manager_at(tmp_path, *, scanner=None, downloader=None):
    store = Store(tmp_path / "state.db")
    store.mutate(
        lambda state: state["assets"].append(
            episode(
                id="legacy_original",
                status="discovered",
                title="Episode 1",
                tags=["保留"],
                classification={"kind": "episode", "source": "manual"},
            )
        )
    )
    manager = TaskManager(store, Browser(), scanner or Resolver(), downloader or Transfer())
    manager.enqueue(["legacy_original"])
    return manager, store.read()["jobs"][0]["id"]


@pytest.mark.asyncio
async def test_access_error_refreshes_once_and_preserves_asset_identity_and_manual_fields(tmp_path):
    manager, job_id = manager_at(tmp_path)
    await manager._download(job_id, Control())
    state = manager.store.read()
    assert state["jobs"][0]["status"] == "completed"
    assert state["jobs"][0]["refreshCount"] == 1
    assert len(manager.scanner.calls) == 1 and len(manager.downloader.seen) == 2
    assert manager.downloader.seen[1]["sourceUrl"].endswith("token=fresh")
    assert {item["id"] for item in manager.downloader.seen} == {"legacy_original"}
    assert state["assets"][0]["tags"] == ["保留"]
    assert state["assets"][0]["classification"]["source"] == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        episode(sourceUrl="https://lustflix.tv/member-previews/episode-1.mp4"),
        episode(pageUrl="https://lustflix.tv/#/watch/C04/2"),
        episode(sourceUrl="https://lustflix.tv/api/media/other/1/master.m3u8"),
        episode(sourceUrl="https://lustflix.tv/api/media/series-slug/1/stream_audio0.m3u8"),
    ],
)
async def test_wrong_refresh_identity_never_downloads_replacement(tmp_path, candidate):
    manager, job_id = manager_at(
        tmp_path, scanner=Resolver({"status": "resolved", "candidate": candidate})
    )
    await manager._download(job_id, Control())
    job = manager.store.read()["jobs"][0]
    assert job["status"] == "needs-user" and job["failureCode"] == "identity-mismatch"
    assert len(manager.downloader.seen) == 1
    assert manager.store.read()["assets"][0]["sourceUrl"].endswith("token=old")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["session-required", "access-required", "ambiguous-media"])
async def test_unresolved_refresh_is_sanitized_and_needs_user_reserves_asset(tmp_path, reason):
    manager, job_id = manager_at(
        tmp_path,
        scanner=Resolver(
            {
                "status": "needs-user",
                "reasonCode": reason,
                "message": "https://secret?token=credentials",
            }
        ),
    )
    await manager._download(job_id, Control())
    job = manager.store.read()["jobs"][0]
    assert job["status"] == "needs-user" and job["failureCode"] == reason
    assert "secret" not in str(job)
    assert manager.enqueue(["legacy_original"])["duplicates"] == 1
    assert manager.job_action(job_id, "retry")["status"] == "queued"
    assert manager.store.read()["jobs"][0]["failureCode"] == ""


@pytest.mark.asyncio
async def test_second_access_error_does_not_resolve_or_download_forever(tmp_path):
    manager, job_id = manager_at(tmp_path, downloader=Transfer(failures=9))
    await manager._download(job_id, Control())
    job = manager.store.read()["jobs"][0]
    assert job["status"] == "needs-user" and job["failureCode"] == "repeated-access-error"
    assert len(manager.scanner.calls) == 1 and len(manager.downloader.seen) == 2
    assert manager.job_action(job_id, "cancel")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_resume_does_not_reset_refresh_budget_but_explicit_retry_does(tmp_path):
    manager, job_id = manager_at(tmp_path, downloader=Transfer(failures=9))
    manager.store.mutate(lambda state: state["jobs"][0].update(status="paused", refreshCount=1))
    manager.job_action(job_id, "resume")
    await manager._download(job_id, Control())
    assert manager.store.read()["jobs"][0]["status"] == "needs-user"
    assert manager.scanner.calls == []
    manager.job_action(job_id, "retry")
    await manager._download(job_id, Control())
    assert len(manager.scanner.calls) == 1


@pytest.mark.asyncio
async def test_scan_persists_supplied_catalog_before_background_work(tmp_path):
    manager, _ = manager_at(tmp_path)
    page = episode()["pageUrl"]
    catalog = build_catalog(page, [{"pageUrl": page, "title": "Episode 1"}], expected_count=1)
    catalog["coverUrls"] = ["https://media.example/cover?signature=local-test"]
    catalog["_publicationRead"] = {"at": 10, "excludedPages": []}
    scan = manager.start_scan(manager.store.read()["sites"][0], {"url": page, "catalog": catalog})
    manager.cancel_scan(scan["id"])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    saved = manager.store.read()["scans"][0]
    assert saved["catalog"]["resolvedCount"] == 1
    assert saved["catalog"]["entries"][0]["assetIds"] == ["legacy_original"]
    assert saved["catalog"]["coverUrls"] == catalog["coverUrls"]
    assert "coverUrls" not in scan["catalog"] and "_publicationRead" not in scan["catalog"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("pause", "paused"), ("cancel", "cancelled")])
async def test_pause_cancel_interrupt_pending_refresh_without_waiting_for_resolver(
    tmp_path, action, expected
):
    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingResolver:
        async def resolve_asset(self, site, asset, cancelled):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    manager, job_id = manager_at(tmp_path, scanner=WaitingResolver())
    control = Control()
    task = asyncio.create_task(manager._download(job_id, control))
    manager.active[job_id] = (control, task)
    await asyncio.wait_for(entered.wait(), 1)
    manager.job_action(job_id, action)
    await asyncio.wait_for(task, 1)
    assert closed.is_set()
    assert manager.store.read()["jobs"][0]["status"] == expected
    assert len(manager.downloader.seen) == 1


@pytest.mark.asyncio
async def test_rescan_rotated_known_url_keeps_legacy_id_and_downloaded_original_and_catalog(
    tmp_path,
):
    manager, _ = manager_at(tmp_path)
    manager.store.mutate(
        lambda state: state["assets"][0].update(
            status="downloaded", _path="assets/legacy_original/original.mp4"
        )
    )
    page = episode()["pageUrl"]

    class Scanner:
        async def scan(self, site, options, on_asset, on_progress, cancelled):
            await on_asset(episode("fresh"))
            await on_progress(
                {
                    "status": "completed",
                    "catalog": build_catalog(
                        page, [{"pageUrl": page, "title": "Episode 1"}], expected_count=1
                    ),
                }
            )

    manager.scanner = Scanner()
    manager.store.mutate(
        lambda state: state["scans"].append(
            {"id": "scan_test", "siteId": "lustflix", "status": "running"}
        )
    )
    await manager._scan("scan_test", manager.store.read()["sites"][0], {"url": page}, Control())
    state = manager.store.read()
    assert len(state["assets"]) == 1
    assert state["assets"][0]["id"] == "legacy_original"
    assert state["assets"][0]["_path"] == "assets/legacy_original/original.mp4"
    assert state["assets"][0]["tags"] == ["保留"]
    assert state["scans"][0]["catalog"]["downloadedCount"] == 1
    assert manager.store.public_state()["scans"][0]["catalog"]["entries"][0]["assetIds"] == [
        "legacy_original"
    ]


@pytest.mark.asyncio
async def test_real_http_video_401_recovers_with_current_session_and_verified_original(tmp_path):
    """Actual HTTP, decoder validation, SQLite and file output cross the recovery boundary."""
    import hashlib

    from backend.downloader import Downloader
    from tests_backend.fixture_server import media_fixture

    class SessionBrowser(Browser):
        authorized = False

        async def cookies_for(self, site_id, url):
            if not self.authorized:
                return []
            return [
                {
                    "name": "fixture_session",
                    "value": "authorized-test-session",
                    "domain": "127.0.0.1",
                    "path": "/",
                }
            ]

    with media_fixture() as fixture:
        browser = SessionBrowser()

        class Policy:
            async def validate(self, url):
                if not url.startswith(fixture.url + "/"):
                    raise ValueError("fixture origin only")
                return url

        class FreshSessionResolver:
            calls = 0

            async def resolve_asset(self, site, asset, cancelled):
                self.calls += 1
                browser.authorized = True
                return {"status": "resolved", "candidate": dict(asset)}

        store = Store(tmp_path / "state.db")
        store.mutate(
            lambda state: state["assets"].append(
                {
                    "id": "fixture_movie",
                    "siteId": "lustflix",
                    "kind": "video",
                    "engine": "http",
                    "sourceUrl": fixture.url + "/movie.mp4",
                    "pageUrl": fixture.url + "/gallery",
                    "title": "Controlled video",
                    "status": "discovered",
                    "tags": ["keep"],
                }
            )
        )
        resolver = FreshSessionResolver()
        manager = TaskManager(store, browser, resolver, Downloader(tmp_path, browser, Policy()))
        manager.enqueue(["fixture_movie"])
        await manager._download(store.read()["jobs"][0]["id"], Control())
        state = store.read()
        assert state["jobs"][0]["status"] == "completed"
        assert resolver.calls == 1
        assert fixture.requests == [("/movie.mp4", False), ("/movie.mp4", True)]
        saved = state["assets"][0]
        assert saved["sha256"] == hashlib.sha256(fixture.video).hexdigest()
        assert (tmp_path / saved["_path"]).read_bytes() == fixture.video
        assert saved["tags"] == ["keep"]
