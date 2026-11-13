from copy import deepcopy

from fastapi.testclient import TestClient

from backend.scanner import Scanner
from backend.site_adapters import build_catalog
from tests_backend.test_app import Policy, app_at, until

ORIGIN = "https://lata.casa"
SHOW = ORIGIN + "/originals/garden"
PAGES = [ORIGIN + f"/watch/garden-ep-{n}" for n in range(1, 4)]


def setup_lata(tmp_path, monkeypatch):
    from backend import lata_scan

    app = app_at(tmp_path)
    app.state.tasks.scanner = Scanner(app.state.browser, Policy())
    owned = set()
    seen = []

    async def read_page(url, browser, site, policy):
        seen.append(url)
        number = 1 if url == SHOW else PAGES.index(url) + 1
        catalog = build_catalog(
            PAGES[number - 1],
            [
                {
                    "url": page,
                    "ordinal": n,
                    "access": "needs-user" if n > 1 and n not in owned else "unknown",
                }
                for n, page in enumerate(PAGES, 1)
            ],
            expected_count=3,
            series_metadata={"seriesKey": "lata-7", "catalogUrl": SHOW, "title": "花园"},
        )
        catalog.update(
            coverUrls=["https://images.example.com/garden.png"], discoverySources=["site-directory"]
        )
        for e in catalog["entries"]:
            if e["access"] == "needs-user":
                e["accessEvidence"] = "public-directory"
        locked = number > 1 and number not in owned
        return {
            "url": PAGES[number - 1],
            "catalog": catalog,
            "episode": {
                "filmId": 7,
                "episodeId": 90 + number,
                "episode": number,
                "filmTitle": "花园",
                "isFree": number == 1,
                "locked": locked,
                "hlsUrl": None
                if locked
                else f"https://videos.example.com/id-{number}/playlist.m3u8",
                "poster": "https://images.example.com/garden.png",
                "totalEpisodes": 3,
            },
        }

    async def read_index(url, browser, site, policy):
        return {
            "shows": [{"url": SHOW, "title": "花园", "covers": [], "categories": []}],
            "pages": [],
            "limited": False,
        }

    monkeypatch.setattr(lata_scan, "read_page", read_page)
    monkeypatch.setattr(lata_scan, "read_index", read_index)
    return app, owned, seen


def wait_scan(client, response):
    identifier = response.raise_for_status().json()["id"]
    state = until(
        client,
        lambda s: any(
            r["id"] == identifier and r["status"] not in {"queued", "running"} for r in s["scans"]
        ),
    )
    return next(row for row in state["scans"] if row["id"] == identifier), state


def test_episode_input_lists_whole_show_then_checks_free_and_saved_access(tmp_path, monkeypatch):
    app, owned, seen = setup_lata(tmp_path, monkeypatch)
    with TestClient(app) as client:
        scan, state = wait_scan(client, client.post("/api/series", json={"url": PAGES[0]}))
        assert scan["status"] == "completed" and scan["catalog"]["expectedCount"] == 3
        assert len(state["series"]) == 1 and len(state["series"][0]["catalog"]["entries"]) == 3
        assert not state["assets"]
        assert app.state.store.read()["scans"][0]["catalog"]["coverUrls"]
        row = state["series"][0]
        path = f"/api/series/{row['id']}/scan"
        body = {
            "episodeKeys": [e["episodeKey"] for e in row["catalog"]["entries"]],
            "scanAndCheck": True,
        }
        check, state = wait_scan(client, client.post(path, json=body))
        assert check["checkedCount"] == 3
        assert [e["mediaStatus"] for e in state["series"][0]["catalog"]["entries"]] == [
            "ready",
            "needs-user",
            "needs-user",
        ]
        (video,) = state["assets"]
        assert video["classification"]["kind"] == "episode"
        assert app.state.store.read()["assets"][0]["_readOnly"] is True
        owned.add(2)
        check, state = wait_scan(client, client.post(path, json=body))
        assert len(state["assets"]) == 2 and len(state["series"]) == 1
        assert [e["mediaStatus"] for e in state["series"][0]["catalog"]["entries"]] == [
            "ready",
            "ready",
            "needs-user",
        ]
        asset = next(a for a in app.state.store.read()["assets"] if a["pageUrl"] == PAGES[1])
        import asyncio

        result = asyncio.run(
            app.state.tasks.scanner.resolve_asset(
                state["sites"][-1], deepcopy(asset), lambda: False
            )
        )
        assert result["status"] == "resolved"


def test_site_intake_recognizes_catalog_and_only_observed_unlocked_media(tmp_path, monkeypatch):
    app, _, seen = setup_lata(tmp_path, monkeypatch)
    with TestClient(app) as client:
        scan, state = wait_scan(
            client,
            client.post(
                "/api/intake",
                json={"url": ORIGIN + "/series", "scope": "site", "limit": 20, "kinds": ["video"]},
            ),
        )
        assert scan["status"] == "completed"
        results = client.get("/api/intake/" + scan["id"] + "/results").raise_for_status().json()
        (group,) = results["groups"]
        assert group["title"] == "花园" and len(group["episodes"]) == 3
        assert len(state["assets"]) == 1
        assert all(url in {SHOW, *PAGES} for url in seen)
