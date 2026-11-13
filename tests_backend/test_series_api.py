import asyncio
import copy
import hashlib
import importlib
import io
import json
import threading
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from tests_backend.test_app import app_at, until
from tests_backend.test_catalog_api import setup_catalog


def archive_module():
    try:
        return importlib.import_module("backend.archive")
    except ImportError:
        pytest.fail("Named series archive is not implemented")


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_archive_disconnect_closes_current_file(tmp_path, monkeypatch, spec):
    app = app_at(tmp_path)
    _, path = saved_series(app, tmp_path)
    opened = []
    original_open = type(path).open

    def tracked_open(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        if self == path:
            opened.append(handle)
        return handle

    monkeypatch.setattr(type(path), "open", tracked_open)
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/scans/{identifier}/archive"
    )
    response = await endpoint("s1")
    disconnected = asyncio.Event()
    chunks = 0

    async def send(message):
        nonlocal chunks
        if message["type"] == "http.response.body":
            chunks += 1
            if chunks == 4:
                assert opened and not opened[0].closed
                if spec == "2.4":
                    raise OSError("Fixture client disconnected")
                disconnected.set()
                await asyncio.Future()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    try:
        await response({"type": "http", "asgi": {"spec_version": spec}}, receive, send)
    except ClientDisconnect:
        assert spec == "2.4"
    try:
        assert chunks == 4 and opened and all(handle.closed for handle in opened)
    finally:
        for handle in opened:
            handle.close()


def test_new_scan_does_not_inherit_an_incompatible_origin_catalog(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        response = client.post(
            "/api/scans",
            json={
                "siteId": "lustflix",
                "url": "https://www.lustflix.tv/#/watch/C04/2",
                "depth": 0,
                "limit": 1,
            },
        )
        assert response.status_code == 200
        assert response.json().get("catalog", {}).get("entries", []) == []


def test_long_names_keep_episode_number_and_format_suffix():
    module = archive_module()
    name = module.download_filename(
        {"id": "a", "kind": "video", "_path": "assets/a/original.mp4", "height": 720},
        {"name": "站点" * 60},
        {"ordinal": 12},
        {"seriesKey": "A" * 200},
    )
    assert name.endswith("_E012_720p.mp4")
    assert len(name.encode()) < 250


@pytest.mark.asyncio
async def test_archive_cancellation_waits_for_inflight_read_before_closing():
    module = archive_module()

    class SlowSource(io.BytesIO):
        def __init__(self):
            super().__init__(b"synthetic-media" * 10000)
            self.entered = threading.Event()
            self.release = threading.Event()

        def read(self, size=-1):
            self.entered.set()
            self.release.wait(2)
            return super().read(size)

    source = SlowSource()

    class File:
        def open(self, mode):
            return source

    response = module.SeriesArchiveResponse(
        {
            "folder": "fixture",
            "manifest": {},
            "files": [{"name": "fixture/one.mp4", "path": File()}],
        }
    )

    async def send(_message):
        pass

    async def receive():
        await asyncio.Future()

    task = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    )
    try:
        assert await asyncio.to_thread(source.entered.wait, 2)
        task.cancel()
        asyncio.get_running_loop().call_later(0.1, source.release.set)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert source.closed
    finally:
        source.release.set()


def saved_series(app, root):
    setup_catalog(app)
    payload = b"original-media-bytes" * 10000
    path = root / "assets/a1/original.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    def save(state):
        asset = next(a for a in state["assets"] if a["id"] == "a1")
        asset.update(
            status="downloaded",
            title="Episode fixture",
            _path="assets/a1/original.mp4",
            height=720,
            mime="video/mp4",
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    app.state.store.mutate(save)
    return payload, path


def test_selected_scan_skips_locked_entries_and_returns_details(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        response = client.post("/api/scans/s1/episodes", json={"episodeKeys": ["C04:1", "C04:2"]})
        assert response.status_code == 200
        assert response.json()["skippedEpisodeKeys"] == ["C04:2"]
        assert response.json()["url"].endswith("/C04/1")
        assert response.json()["catalog"]["enumeratedCount"] == 2


def test_selected_scan_with_only_locked_entries_does_not_start(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        response = client.post("/api/scans/s1/episodes", json={"episodeKeys": ["C04:2"]})
        assert response.status_code == 409
        state = client.get("/api/state").json()
        assert len(state["scans"]) == 1
        assert state["seriesArchiveAvailable"] is True


def test_lustflix_download_and_format_inspection_never_open_authorization_pages(tmp_path):
    app = app_at(tmp_path)

    async def must_not_navigate(_site):
        raise AssertionError("Read-only media work must not navigate an authorization page")

    async def describe(_asset):
        return {"protocol": "hls", "selectable": True, "heights": [720], "audioTracks": []}

    app.state.browser.open_session = must_not_navigate
    app.state.tasks.downloader.describe_options = describe
    with TestClient(app) as client:
        setup_catalog(app)
        app.state.store.mutate(lambda state: state["assets"][0].update(title="Episode fixture"))
        assert client.get("/api/assets/a1/download-options").status_code == 200
        result = client.post("/api/scans/s1/downloads", json={"episodeKeys": ["C04:1"]})
        assert result.json()["added"] == 1
        state = until(client, lambda state: state["jobs"][0]["status"] in {"completed", "failed"})
        assert state["jobs"][0]["status"] == "completed"


def test_signed_duplicate_master_prefers_saved_original(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        saved_series(app, tmp_path)

        def duplicate(state):
            original = next(a for a in state["assets"] if a["id"] == "a1")
            duplicate = {
                **original,
                "id": "fresh",
                "status": "discovered",
                "sourceUrl": original["sourceUrl"] + "?token=rotated",
            }
            duplicate.pop("_path")
            state["assets"].append(duplicate)

        app.state.store.mutate(duplicate)
        result = client.post("/api/scans/s1/downloads", json={"episodeKeys": ["C04:1"]}).json()
        assert result["added"] == 0 and result["duplicates"] == 1
        assert result["unavailable"] == [] and not app.state.store.read()["jobs"]


def test_catalog_download_returns_distinct_unavailable_reasons(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        app.state.store.mutate(
            lambda state: state["assets"].append(
                {
                    **state["assets"][0],
                    "id": "other",
                    "sourceUrl": "https://lustflix.tv/api/media/different/1/master.m3u8",
                }
            )
        )
        result = client.post(
            "/api/scans/s1/downloads", json={"episodeKeys": ["C04:1", "C04:2"]}
        ).json()
        assert result["added"] == 0
        assert result["unavailableDetails"] == [
            {"episodeKey": "C04:1", "reason": "ambiguous"},
            {"episodeKey": "C04:2", "reason": "needs-user"},
        ]


def test_new_scan_inherits_only_same_site_same_series_history(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)

        def add_other(state):
            other = copy.deepcopy(state["scans"][0])
            other.update(id="other", siteId="chunlou")
            other["catalog"]["entries"][0]["title"] = "wrong-site"
            state["scans"].insert(0, other)

        app.state.store.mutate(add_other)
        response = client.post(
            "/api/scans",
            json={
                "siteId": "lustflix",
                "url": "https://lustflix.tv/#/watch/C04/1",
                "depth": 0,
                "limit": 1,
            },
        )
        assert response.status_code == 200
        catalog = response.json()["catalog"]
        assert catalog["enumeratedCount"] == 2 and catalog["entries"][0]["title"] != "wrong-site"
        until(client, lambda state: state["scans"][0]["status"] == "completed")
        other = client.post(
            "/api/scans",
            json={
                "siteId": "lustflix",
                "url": "https://lustflix.tv/#/watch/C03/5",
                "depth": 0,
                "limit": 1,
            },
        )
        assert other.status_code == 200
        assert all(
            entry["episodeKey"].startswith("C03:")
            for entry in other.json().get("catalog", {}).get("entries", [])
        )


def test_attachment_names_and_zip_export_preserve_original_bytes_and_missing_inventory(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        payload, original = saved_series(app, tmp_path)
        attachment = client.get("/api/assets/a1/file?download=true")
        assert attachment.content == payload
        assert "Lustflix_C04_E001_720p.mp4" in unquote(attachment.headers["content-disposition"])
        ranged = client.get("/api/assets/a1/file", headers={"Range": "bytes=0-15"})
        assert ranged.status_code == 206 and ranged.content == payload[:16]
        response = client.get("/api/scans/s1/archive")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            media = [name for name in archive.namelist() if name.endswith(".mp4")]
            assert media == ["C04/Lustflix_C04_E001_720p.mp4"]
            assert archive.read(media[0]) == payload
            manifest = json.loads(archive.read("C04/manifest.json"))
            assert archive.namelist() == ["C04/manifest.json", *media]
            assert manifest["entries"][0]["file"] == media[0]
            assert manifest["savedCount"] == 1
            assert manifest["entries"][1]["state"] == "needs-user"
            assert not manifest["allSaved"]
            assert all("sourceUrl" not in entry for entry in manifest["entries"])
        assert original.read_bytes() == payload
        assert app.state.store.read()["assets"][0]["_path"] == "assets/a1/original.mp4"


def test_archive_rejects_empty_and_reports_missing_local_file(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        assert client.get("/api/scans/s1/archive").status_code == 409
        assert client.get("/api/scans/unknown/archive").status_code == 404


def test_names_clean_untrusted_unicode_paths_and_reserve_extension():
    module = archive_module()
    asset = {
        "id": "asset_safe",
        "title": "../坏\r\n名字/CON\u202e",
        "kind": "video",
        "_path": "assets/asset_safe/original.mp4",
        "height": 1080,
        "pageUrl": "https://lustflix.tv/#/watch/C03/5",
    }
    name = module.download_filename(asset, {"name": "../Lu/stflix\x00"})
    assert name.endswith("_C03_E005_1080p.mp4")
    assert not any(char in name for char in ("/", "\\", "\r", "\n", "\x00", "\u202e"))
    assert not name.startswith(".")
    unknown = module.download_filename(
        {**asset, "pageUrl": "https://example.com/video"}, {"name": "CON"}
    )
    assert unknown.endswith(".mp4") and len(unknown.encode()) < 250


def test_archive_generator_streams_chunks_without_materializing_whole_video(tmp_path):
    module = archive_module()
    app = app_at(tmp_path)
    payload, _ = saved_series(app, tmp_path)
    state = app.state.store.read()
    site = next(site for site in state["sites"] if site["id"] == "lustflix")
    plan = module.prepare_series_archive(
        state["scans"][0]["catalog"], state["assets"], site, tmp_path
    )
    chunks = list(module.stream_series_archive(plan))
    assert len(chunks) > 2 and max(map(len, chunks)) < 100000
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as result:
        assert result.read("C04/Lustflix_C04_E001_720p.mp4") == payload


def test_single_episode_export_returns_named_original_range_and_only_local_visible_episode(tmp_path):
    app = app_at(tmp_path)
    payload, original = saved_series(app, tmp_path)
    with TestClient(app) as client:
        series_id = client.get("/api/state").json()["series"][0]["id"]
        series_path = f"/api/series/{series_id}"
        export_path = series_path + "/episode-file"
        client.put(series_path + "/title", json={"title": "单集验收"}).raise_for_status()
        app.state.store.mutate(lambda s: s["scans"][0]["catalog"]["entries"][0].update(access="needs-user"))
        key = {"episodeKey": "C04:1"}
        response = client.get(export_path, params=key)
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == "video/mp4"
        assert "Lustflix_单集验收_E001_720p.mp4" in unquote(response.headers["content-disposition"])
        ranged = client.get(export_path, params=key, headers={"Range": "bytes=3-15"})
        assert ranged.status_code == 206 and ranged.content == payload[3:16]
        assert client.get(export_path, params={"episodeKey": "C04:2"}).status_code == 409
        assert client.get(export_path, params={"episodeKey": "unrelated"}).status_code == 400
        assert client.get(export_path).status_code == 422
        assert original.read_bytes() == payload
        original.unlink()
        assert client.get(export_path, params=key).status_code == 404
        client.post("/api/series/dismiss", json={"ids": [series_id], "episodeKeys": ["C04:1"]}).raise_for_status()
        assert client.get(export_path, params=key).status_code == 400
