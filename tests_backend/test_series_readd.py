"""A user can add an archived series again without reviving unrelated library entries."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests_backend.test_app import app_at, until
from tests_backend.test_series_library import catalog, scan, seed


def observe(app, cat, *, status="completed"):
    async def read(site, options, asset_found, report, cancelled):
        await report({"status": status, "catalog": cat})

    app.state.tasks.scanner.scan = read


def begin(client, url, **params):
    response = client.post("/api/series", json={"url": url, "siteId": "example", **params})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def finished(client, identifier):
    return until(
        client, lambda s: any(r["id"] == identifier and r.get("finishedAt") for r in s["scans"])
    )


@pytest.mark.parametrize("link", ["catalog", "another-episode", "new-episode", "hidden-episode"])
def test_readding_restores_same_series_and_preserves_previous_choices(tmp_path, link):
    app = app_at(tmp_path)
    seed(app)
    app.state.store.mutate(lambda s: s["scans"].insert(0, scan("else", catalog(key="ocean"))))
    fresh = catalog((1, 2, 3, 4))
    captured = []

    async def read(site, options, asset_found, report, cancelled):
        captured.append(options)
        await report({"catalog": fresh, "status": "completed"})

    app.state.tasks.scanner.scan = read
    with TestClient(app) as client:
        unrelated, row = client.get("/api/state").json()["series"]
        sid = row["id"]
        saved = client.post(f"/api/series/{sid}/downloads", json={}).json()
        assert saved["added"] == 1
        until(client, lambda s: s["jobs"][0]["status"] == "completed")
        assert client.put(f"/api/series/{sid}/title", json={"title": "我的花园"}).is_success
        hidden_episode = fresh["entries"][1]
        client.post(
            "/api/series/dismiss",
            json={"ids": [sid], "episodeKeys": [hidden_episode["episodeKey"]]},
        )
        client.post("/api/series/dismiss", json={"ids": [sid, unrelated["id"]]})
        before = app.state.store.read()
        link_url = {
            "catalog": row["url"],
            "another-episode": fresh["entries"][0]["pageUrl"],
            "new-episode": fresh["entries"][3]["pageUrl"],
            "hidden-episode": hidden_episode["pageUrl"],
        }[link]
        result = finished(client, begin(client, link_url))
        assert result["scans"][0]["restoredSeriesId"] == sid
        assert result["dismissedSeriesCount"] == 1
        assert [r["id"] for r in result["series"]] == [sid]
        row = result["series"][0]
        assert row["title"] == "我的花园"
        assert [e["ordinal"] for e in row["catalog"]["entries"]] == [1, 3, 4]
        assert row["catalog"]["blockedCount"] == 1 and row["catalog"]["downloadedCount"] == 1
        assert captured[0]["url"] != hidden_episode["pageUrl"]
        assert hidden_episode["episodeKey"] in captured[0]["excludedEpisodeKeys"]
        assert app.state.store.read()["jobs"] == before["jobs"]
        assert app.state.store.read()["assets"] == before["assets"]
        assert client.get("/api/assets/ep1/file").content == b"verified-file-fixture"
    with TestClient(app_at(tmp_path)) as client:
        assert [r["id"] for r in client.get("/api/state").json()["series"]] == [sid]


@pytest.mark.parametrize(
    "status,observed",
    [
        ("failed", "garden"),
        ("needs-user", "garden"),
        ("cancelled", "garden"),
        ("completed", None),
        ("completed", "ocean"),
    ],
)
def test_cached_missing_failed_or_different_directory_cannot_restore(tmp_path, status, observed):
    app = app_at(tmp_path)
    seed(app)
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        client.post("/api/series/dismiss", json={"ids": [row["id"]]})
        observe(app, catalog(key=observed) if observed else {}, status=status)
        state = finished(client, begin(client, row["url"]))
        assert row["id"] not in [r["id"] for r in state["series"]]
        assert state["dismissedSeriesCount"] == 1
        assert "restoredSeriesId" not in state["scans"][0]
        if status == "completed":
            assert state["scans"][0]["status"] == "failed"
            assert "原剧目录" in state["scans"][0]["message"]


def test_removal_during_an_earlier_scan_wins_and_busy_submission_does_not_restore(tmp_path):
    app = app_at(tmp_path)
    seed(app)

    async def wait(site, options, asset_found, report, cancelled):
        await asyncio.sleep(0.35)
        await report({"status": "completed", "catalog": catalog()})

    app.state.tasks.scanner.scan = wait
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        older = begin(client, row["url"])
        client.post("/api/series/dismiss", json={"ids": [row["id"]]})
        assert (
            client.post("/api/series", json={"url": row["url"], "siteId": "example"}).status_code
            == 409
        )
        assert finished(client, older)["series"] == []


def test_generic_discovery_scan_of_hidden_series_does_not_restore(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    observe(app, catalog())
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        client.post("/api/series/dismiss", json={"ids": [row["id"]]})
        scan_id = client.post("/api/scans", json={"url": row["url"], "siteId": "example"}).json()[
            "id"
        ]
        assert finished(client, scan_id)["series"] == []
