import copy

import pytest
from fastapi.testclient import TestClient

from backend.site_adapters import build_catalog, merge_catalog
from tests_backend.test_app import app_at, until
from tests_backend.test_generic_series_scanning import (
    ORIGIN,
    fixture_pages,
    neutral_browser,
    run_scan,
)
from tests_backend.test_series_library import seed


def test_recheck_is_explicit_and_only_enqueues_observed_permission_entries(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    options = []
    original = app.state.tasks.scanner.scan

    async def capture(site, values, *args):
        options.append(copy.deepcopy(values))
        return await original(site, values, *args)

    app.state.tasks.scanner.scan = capture
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        endpoint = f"/api/series/{row['id']}/scan"
        gated = row["catalog"]["entries"][2]
        response = client.post(endpoint, json={"recheckAccess": True})
        assert response.status_code == 200
        until(client, lambda state: state["scans"][0]["status"] == "completed")
        assert options[0]["seedUrls"] == [gated["pageUrl"]]
        assert options[0]["recheckUrls"] == [gated["pageUrl"]]
        assert options[0]["followCatalog"] is False
        assert response.json()["recheckedEpisodeKeys"] == [gated["episodeKey"]]
        assert response.json()["skippedEpisodeKeys"] == []
        assert (
            client.post(
                endpoint, json={"recheckAccess": True, "episodeKeys": ["invented"]}
            ).status_code
            == 400
        )
        assert client.post(endpoint, json={"recheckAccess": "yes"}).status_code == 422


def test_recheck_selection_and_page_budget_remain_bounded(tmp_path):
    app = app_at(tmp_path)
    seed(app)

    def gates(state):
        for entry in state["scans"][0]["catalog"]["entries"]:
            entry["access"] = "needs-user"

    app.state.store.mutate(gates)
    calls = []

    def start(_site, options):
        calls.append(options)
        return {"id": "fixture"}

    app.state.tasks.start_scan = start
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        entries = row["catalog"]["entries"]
        response = client.post(
            f"/api/series/{row['id']}/scan",
            json={
                "recheckAccess": True,
                "episodeKeys": [entry["episodeKey"] for entry in entries[1:]],
                "limit": 1,
            },
        )
        assert response.status_code == 200
        assert calls[0]["recheckUrls"] == [entries[1]["pageUrl"]]
        assert calls[0]["seedUrls"] == [entries[1]["pageUrl"]]
        assert response.json()["remainingEpisodeKeys"] == [entries[2]["episodeKey"]]


def test_recheck_next_batch_prioritizes_entries_not_yet_attempted(tmp_path):
    app = app_at(tmp_path)
    seed(app)

    def history(state):
        prior = state["scans"][0]
        for entry in prior["catalog"]["entries"]:
            entry["access"] = "needs-user"
        prior["_recheckAttemptedUrls"] = [prior["catalog"]["entries"][0]["pageUrl"]]

    app.state.store.mutate(history)
    calls = []
    app.state.tasks.start_scan = lambda _site, options: calls.append(options) or {"id": "fixture"}
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        response = client.post(
            f"/api/series/{row['id']}/scan", json={"recheckAccess": True, "limit": 1}
        )
        assert response.status_code == 200
        assert calls[0]["seedUrls"] == [row["catalog"]["entries"][1]["pageUrl"]]


def gated_catalog():
    return build_catalog(
        ORIGIN + "/series/field-notes",
        [{"url": ORIGIN + f"/watch/{n}", "ordinal": n, "access": "needs-user"} for n in (1, 2, 3)],
        expected_count=3,
        series_metadata={
            "seriesKey": "field-notes",
            "catalogUrl": ORIGIN + "/series/field-notes",
            "title": "Field Notes",
        },
    )


@pytest.mark.asyncio
async def test_recheck_visits_only_explicit_seed_and_updates_fresh_media_permission():
    pages = fixture_pages()
    pages["/watch/2"] += "<script>fetch('/charge',{method:'POST'}).catch(()=>{})</script>"
    catalog = gated_catalog()
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(
            scanner,
            url=ORIGIN + "/watch/2",
            seedUrls=[ORIGIN + "/watch/2"],
            recheckUrls=[ORIGIN + "/watch/2", ORIGIN + "/watch/3"],
            followCatalog=False,
            catalog=catalog,
        )
    assert result["pagesVisited"] == 1 and len(assets) == 1
    assert result["_pageAccess"] == {ORIGIN + "/watch/2": "observed"}
    assert result["_recheckAttemptedUrls"] == [ORIGIN + "/watch/2"]
    entries = result["catalog"]["entries"]
    assert [entry["access"] for entry in entries] == ["needs-user", "observed", "needs-user"]
    assert ("GET", "/watch/1") not in requests and ("GET", "/watch/3") not in requests
    assert ("POST", "/charge") not in requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", ["<h1>Sign in to continue</h1>", "<main><p>Content unavailable</p></main>"]
)
async def test_recheck_does_not_clear_gate_without_new_accessible_main_media(body):
    pages = fixture_pages()
    pages["/watch/2"] = body
    async with neutral_browser(pages) as (scanner, _requests):
        result, assets = await run_scan(
            scanner,
            url=ORIGIN + "/watch/2",
            seedUrls=[ORIGIN + "/watch/2"],
            recheckUrls=[ORIGIN + "/watch/2"],
            followCatalog=False,
            catalog=gated_catalog(),
        )
    assert result["pagesVisited"] == 1 and assets == []
    assert result["catalog"]["entries"][1]["access"] == "needs-user"
    assert result["_pageAccess"].get(ORIGIN + "/watch/2") != "observed"


def test_catalog_discovery_sources_survive_scanner_and_history_merge():
    from backend.scanner import Scanner

    previous = gated_catalog()
    previous["discoverySources"] = ["dom"]
    current = Scanner._merge_catalog(
        previous,
        previous["catalogUrl"],
        {
            "links": [],
            "discoverySources": ["json-ld", "not-a-supported-source"],
        },
    )
    assert current["discoverySources"] == ["dom", "json-ld"]
    assert merge_catalog(previous, current)["discoverySources"] == ["dom", "json-ld"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        '<aside><video controls src="/media/episode-2.mp4"></video></aside>',
        '<main hidden><video controls src="/media/episode-2.mp4"></video></main>',
        '<script>fetch("/media/episode-2.mp4")</script>',
    ],
)
async def test_recheck_needs_fresh_main_evidence_not_a_filename_hint(body):
    pages = fixture_pages()
    pages["/watch/2"] = body
    async with neutral_browser(pages) as (scanner, _requests):
        result, _assets = await run_scan(
            scanner,
            url=ORIGIN + "/watch/2",
            seedUrls=[ORIGIN + "/watch/2"],
            recheckUrls=[ORIGIN + "/watch/2"],
            followCatalog=False,
            catalog=gated_catalog(),
        )
    assert result["catalog"]["entries"][1]["access"] == "needs-user"
    assert result["_pageAccess"].get(ORIGIN + "/watch/2") != "observed"


def test_one_click_checks_every_selected_episode_in_order_despite_page_budget_and_failure(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    calls = []

    async def probe(site, options, on_asset, on_progress, cancelled):
        url = options["url"]
        assert options["readOnly"] and not options["followCatalog"]
        assert options["recheckUrls"] == options["seedUrls"] == [url]
        calls.append(url)
        if len(calls) == 1:
            await on_progress({"status": "running", "pagesVisited": 1})
            raise ValueError("此页无响应")
        if len(calls) == 2:
            return {"status": "needs-user", "message": "需要权限", "pagesVisited": 1,
                    "_pageAccess": {url: "needs-user"}}
        await on_asset({"sourceUrl": "https://example.com/media/episode-3.mp4", "pageUrl": url,
                        "kind": "video", "title": "Episode 3", "engine": "http"})
        return {"status": "completed", "pagesVisited": 1, "_pageAccess": {url: "observed"}}

    app.state.tasks.scanner.scan = probe
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        endpoint = f"/api/series/{row['id']}/scan"
        keys = [entry["episodeKey"] for entry in row["catalog"]["entries"]]
        bad = client.post(endpoint, json={"episodeKeys": ["invented"], "scanAndCheck": True})
        assert bad.status_code == 400 and not calls
        response = client.post(endpoint, json={"episodeKeys": [*reversed(keys), keys[0]],
                                                "limit": 1, "scanAndCheck": True})
        assert response.status_code == 200, response.text
        assert response.json()["checkTotal"] == 3
        state = until(client, lambda s: s["scans"][0]["status"] == "completed")
        scan = state["scans"][0]
        assert calls == [e["pageUrl"] for e in row["catalog"]["entries"]]
        assert scan["checkedCount"] == scan["checkTotal"] == scan["pagesVisited"] == 3
        assert not scan["pagesQueued"] and len(state["scans"]) == 2
        outcomes = state["series"][0]["catalog"]["entries"]
        assert [e["scanStatus"] for e in outcomes] == ["failed", "needs-user", "ready"]
        assert outcomes[1]["access"] == "needs-user" and outcomes[2]["access"] == "observed"


def test_combined_check_stops_on_site_throttle_and_reports_unvisited_count(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    calls = []

    async def probe(site, options, on_asset, on_progress, cancelled):
        calls.append(options["url"])
        return {"status": "failed", "pagesVisited": 1, "message": "站点限流", "_stopBatch": True}

    app.state.tasks.scanner.scan = probe
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        endpoint = f"/api/series/{row['id']}/scan"
        bad = client.post(endpoint, json={"scanAndCheck": True})
        assert bad.status_code in {400, 422}
        response = client.post(endpoint, json={"scanAndCheck": True, "episodeKeys":
            [e["episodeKey"] for e in row["catalog"]["entries"]]})
        assert response.status_code == 200
        state = until(client, lambda s: s["scans"][0]["status"] == "failed")
        scan = state["scans"][0]
        assert len(calls) == scan["checkedCount"] == 1
        assert scan["pagesQueued"] == 2 and "2 集" in scan["message"]


@pytest.mark.asyncio
async def test_combined_browser_keeps_later_episode_after_gate_and_blocks_page_writes():
    from backend.series_check import scan_and_check
    from tests_backend.test_generic_series_scanning import SITE

    catalog = gated_catalog()
    pages = fixture_pages()
    pages["/watch/1"] = '<h1>Sign in to continue</h1><script>fetch("/pay",{method:"POST"})</script>'
    pages["/watch/2"] = '<main>No current player</main><img alt="Poster">'
    assets, reports = [], []

    async def save(value):
        assets.append(value)

    async def progress(value):
        reports.append({k: v for k, v in value.items() if k != "catalog"})

    urls = [row["pageUrl"] for row in catalog["entries"]]
    async with neutral_browser(pages) as (scanner, requests):
        await scan_and_check(scanner, SITE, {"catalog": catalog, "seedUrls": urls}, save,
                             progress, lambda: False)
    final = reports[-1]
    assert final["checkedCount"] == 3 and final["status"] == "completed"
    assert [final["_episodeOutcomes"][u]["status"] for u in urls] == ["needs-user", "unresolved", "ready"]
    assert final["_pageAccess"] == {urls[0]: "needs-user", urls[2]: "observed"}
    assert [a["pageUrl"] for a in assets] == [urls[2]]
    assert ("POST", "/pay") not in requests
