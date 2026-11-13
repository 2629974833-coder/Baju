import asyncio
import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.store import Store
from backend.tasks import Conflict, Control, TaskManager
from tests_backend.test_app import Browser, Downloader, Policy, Scanner, until
from tests_backend.test_catalog_api import setup_catalog
from tests_backend.test_task_recovery import Transfer, manager_at

OPTIONS = {"maxHeight": 720, "audioLanguage": "eng"}
NORMALIZED = {"maxHeight": 720, "audioLanguage": "en"}


class Describer(Downloader):
    async def describe_options(self, asset):
        return {
            "protocol": "hls",
            "selectable": True,
            "heights": [360, 720],
            "audioTracks": [{"language": "en", "name": "English", "default": True}],
            "message": "",
        }

    async def download(self, asset, control, on_progress):
        result = await super().download(asset, control, on_progress)
        result["selection"] = {
            "protocol": "hls",
            "height": 720,
            "audioLanguage": "en",
            "optionsApplied": True,
        }
        return result


def app_at(tmp_path):
    return create_app(
        Settings(data_dir=tmp_path),
        browser=Browser(),
        policy=Policy(),
        scanner=Scanner(),
        downloader=Describer(tmp_path),
    )


def test_api_inspects_only_public_options_and_persists_episode_choices(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        app.state.store.mutate(
            lambda state: [asset.update(title="Fixture episode") for asset in state["assets"]]
        )
        inspected = client.get("/api/assets/a1/download-options")
        assert inspected.status_code == 200
        assert inspected.json()["heights"] == [360, 720]
        assert "sourceUrl" not in inspected.text and "_headers" not in inspected.text
        r = client.post(
            "/api/scans/s1/downloads", json={"episodeKeys": ["C04:1"], "options": OPTIONS}
        )
        assert r.status_code == 200 and r.json()["added"] == 1
        state = until(client, lambda state: state["jobs"][0]["status"] == "completed")
        assert state["jobs"][0]["downloadOptions"] == NORMALIZED
        assert state["jobs"][0]["selection"]["height"] == 720
        assert (
            next(a for a in state["assets"] if a["id"] == "a1")["downloadSelection"][
                "audioLanguage"
            ]
            == "en"
        )
        assert all("_downloadOptions" not in asset for asset in state["assets"])


@pytest.mark.parametrize(
    "options",
    [
        {"maxHeight": True},
        {"maxHeight": "720"},
        {"maxHeight": 1},
        {"maxHeight": 9999},
        {"audioLanguage": 123},
        {"audioLanguage": "en\r\nsecret"},
        {"audioLanguage": "english"},
        {"extra": "anything"},
    ],
)
def test_download_options_reject_invalid_payloads(tmp_path, options):
    with TestClient(app_at(tmp_path)) as client:
        assert (
            client.post("/api/downloads", json={"ids": ["a"], "options": options}).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_queue_choices_survive_new_manager_and_refresh(tmp_path):
    manager, job_id = manager_at(tmp_path)
    manager.store.mutate(lambda state: state["jobs"][0].update(downloadOptions=NORMALIZED))
    restored = TaskManager(
        Store(tmp_path / "state.db"), manager.browser, manager.scanner, manager.downloader
    )
    await restored._download(job_id, Control())
    assert all(asset["_downloadOptions"] == NORMALIZED for asset in restored.downloader.seen)
    assert restored.store.read()["jobs"][0]["downloadOptions"] == NORMALIZED
    assert "_downloadOptions" not in restored.store.read()["assets"][0]


def test_enqueue_freezes_each_job_and_retry_can_replace_only_terminal_choices(tmp_path):
    manager, job_id = manager_at(tmp_path)
    manager.store.mutate(lambda state: state["jobs"].clear())
    options = copy.deepcopy(OPTIONS)
    manager.enqueue(["legacy_original"], options)
    options["maxHeight"] = 1080
    job = manager.store.read()["jobs"][0]
    assert job["downloadOptions"] == NORMALIZED
    with pytest.raises(Conflict):
        manager.job_action(job["id"], "pause", {"maxHeight": 360})
    manager.store.mutate(
        lambda state: state["jobs"][0].update(
            status="failed", selection={"height": 720}, downloadedBytes=20
        )
    )
    result = manager.job_action(job["id"], "retry", {"maxHeight": 360, "audioLanguage": "auto"})
    assert result["downloadOptions"] == {"maxHeight": 360, "audioLanguage": "auto"}
    assert not result.get("selection") and result["downloadedBytes"] == 0


@pytest.mark.asyncio
async def test_legacy_job_gets_automatic_defaults_and_original_file_stays_protected(tmp_path):
    manager, job_id = manager_at(tmp_path, downloader=Transfer(failures=0))
    manager.store.mutate(lambda state: state["jobs"][0].pop("downloadOptions", None))
    await manager._download(job_id, Control())
    assert manager.downloader.seen[0]["_downloadOptions"] == {
        "maxHeight": None,
        "audioLanguage": "auto",
    }
    assert manager.enqueue(["legacy_original"], {"maxHeight": 360})["duplicates"] == 1


@pytest.mark.asyncio
async def test_inspection_limits_concurrency_and_releases_slots_after_timeout(
    tmp_path, monkeypatch
):
    class SlowDescriber(Describer):
        active = 0
        peak = 0
        cancelled = 0
        slow = True

        async def describe_options(self, asset):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.slow:
                    await asyncio.Future()
                return await super().describe_options(asset)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            finally:
                self.active -= 1

    downloader = SlowDescriber(tmp_path)
    app = create_app(
        Settings(data_dir=tmp_path),
        browser=Browser(),
        policy=Policy(),
        scanner=Scanner(),
        downloader=downloader,
    )
    setup_catalog(app)
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda delay: original_timeout(min(delay, 0.05)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        responses = await asyncio.gather(
            *[client.get("/api/assets/a1/download-options") for _ in range(5)]
        )
        assert all(response.status_code == 504 for response in responses)
        assert downloader.peak == 2 and downloader.active == 0
        assert downloader.cancelled >= 2
        downloader.slow = False
        assert (await client.get("/api/assets/a1/download-options")).status_code == 200


@pytest.mark.asyncio
async def test_retry_api_changes_terminal_options_but_rejects_other_actions(tmp_path):
    app = app_at(tmp_path)
    setup_catalog(app)
    app.state.tasks.enqueue(["a1"])
    job = app.state.store.read()["jobs"][0]
    path = "/api/jobs/" + job["id"] + "/action"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (
            await client.post(path, json={"action": "pause", "options": OPTIONS})
        ).status_code == 409
        assert (
            await client.post(path, json={"action": "retry", "options": OPTIONS})
        ).status_code == 409
        app.state.store.mutate(lambda state: state["jobs"][0].update(status="failed"))
        assert (
            await client.post(path, json={"action": "retry", "options": {"maxHeight": "720"}})
        ).status_code == 422
        response = await client.post(path, json={"action": "retry", "options": OPTIONS})
        assert response.status_code == 200
        assert response.json()["downloadOptions"] == NORMALIZED
