"""Neutral, local route-fulfilled Chromium fixtures for generic series scanning."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from backend.scanner import Scanner
from backend.site_adapters import update_catalog

ORIGIN = "https://documentary.example"
SITE = {"id": "neutral-fixture", "name": "Field Notes", "url": ORIGIN + "/series/field-notes"}


@asynccontextmanager
async def neutral_browser(pages):
    requests = []
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)

        async def context():
            created = await chromium.new_context(service_workers="block")

            async def respond(route):
                request = route.request
                path = urlsplit(request.url).path
                requests.append((request.method, path))
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8" if path in pages else "video/mp4",
                    body=pages.get(path, b"fixture"),
                )

            await created.route("**/*", respond)
            return created

        user_context = await context()

        class Browser:
            async def open_session(self, _site):
                return {"open": True}

            async def context_for(self, _site):
                return user_context

            async def create_scan_context(self, _site):
                return await context()

        class Policy:
            async def validate(self, url):
                if urlsplit(url).hostname != "documentary.example":
                    raise ValueError("Fixture origin only")
                return url

        try:
            yield Scanner(Browser(), Policy()), requests
        finally:
            await chromium.close()


def directory(count=3, label="Episodes"):
    links = "".join(
        f"<a href='/watch/{number}' data-episode-number='{number}'>Episode {number}</a>"
        for number in range(1, count + 1)
    )
    return f"<h1>Field Notes</h1><section aria-label='{label}' data-series-url='/series/field-notes' data-episode-count='{count}'>{links}</section>"


def fixture_pages(count=3):
    return {
        "/series/field-notes": directory(count)
        + "<a href='/home'>Home</a><aside><a href='/watch/99'>Episode 99</a></aside>",
        **{
            f"/watch/{number}": f"<h1>Field Notes {number}</h1><main><video controls src='/media/{number}.mp4'></video></main>"
            for number in range(1, count + 1)
        },
    }


async def run_scan(scanner, **options):
    assets = []

    async def found(asset):
        assets.append({"id": f"asset-{len(assets) + 1}", **asset})

    async def progress(_value):
        pass

    result = await scanner.scan(
        SITE,
        {
            "url": SITE["url"],
            "mode": "series",
            "depth": 0,
            "limit": 100,
            "kinds": ["video"],
            **options,
        },
        found,
        progress,
        lambda: False,
    )
    return result, assets


@pytest.mark.asyncio
async def test_series_mode_discovers_actual_directory_then_scans_episodes_without_depth():
    async with neutral_browser(fixture_pages()) as (scanner, requests):
        result, assets = await run_scan(scanner)
    assert result["pagesVisited"] == 4
    assert {asset["pageUrl"] for asset in assets} == {
        ORIGIN + f"/watch/{number}" for number in range(1, 4)
    }
    assert all(asset["classification"]["kind"] == "episode" for asset in assets)
    assert not any(path in {"/home", "/watch/99"} for _, path in requests)
    catalog = update_catalog(result["catalog"], assets)
    assert catalog["adapterId"] == "generic" and catalog["scope"] == "series"
    assert catalog["catalogUrl"] == SITE["url"] and catalog["title"] == "Field Notes"
    assert catalog["expectedCount"] == 3 and catalog["status"] == "complete-observed"


@pytest.mark.asyncio
async def test_conflicting_episode_numbers_do_not_queue_ambiguous_directory():
    pages = fixture_pages()
    pages["/series/field-notes"] = """<section aria-label='剧集'>
      <a href='/watch/1'>第1集</a><a href='/other/1'>第1集</a></section>"""
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(scanner)
    assert result["pagesVisited"] == 1 and not assets
    assert "catalog-conflict" in result["catalog"]["reasons"]
    assert all(path not in {"/watch/1", "/other/1"} for _, path in requests)


@pytest.mark.asyncio
async def test_follow_catalog_false_scans_only_explicit_seed_with_saved_catalog():
    async with neutral_browser(fixture_pages()) as (scanner, requests):
        initial, _ = await run_scan(scanner)
        requests.clear()
        result, assets = await run_scan(
            scanner,
            url=ORIGIN + "/watch/2",
            seedUrls=[ORIGIN + "/watch/2"],
            followCatalog=False,
            catalog=initial["catalog"],
        )
    assert result["pagesVisited"] == 1 and len(assets) == 1
    assert assets[0]["pageUrl"] == ORIGIN + "/watch/2"
    assert assets[0]["_episodeEvidence"]["seriesKey"] == initial["catalog"]["seriesKey"]
    assert not any(path in {"/watch/1", "/watch/3"} for _, path in requests)


@pytest.mark.asyncio
async def test_playback_entry_can_follow_explicit_catalog_link_then_recheck_that_episode():
    pages = fixture_pages()
    pages["/watch/2"] += "<a rel='up' href='/series/field-notes'>Series directory</a>"
    async with neutral_browser(pages) as (scanner, _requests):
        result, assets = await run_scan(scanner, url=ORIGIN + "/watch/2")
    assert len(assets) == 3 and result["pagesVisited"] == 5
    assert all(asset["classification"]["kind"] == "episode" for asset in assets)
    assert result["catalog"]["catalogUrl"] == SITE["url"]


@pytest.mark.asyncio
async def test_multiple_distinct_labelled_directories_are_not_mixed_or_auto_selected():
    pages = fixture_pages()
    pages["/series/field-notes"] += (
        "<aside aria-label='Episodes'><a href='/other/1'>Episode 1</a></aside>"
    )
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(scanner)
    assert result["pagesVisited"] == 1 and assets == []
    assert "catalog-conflict" in result["catalog"]["reasons"]
    assert all(not path.startswith("/watch/") for _, path in requests)


@pytest.mark.asyncio
async def test_catalog_pagination_collects_real_links_and_respects_page_budget():
    pages = fixture_pages(5)
    pages[
        "/series/field-notes"
    ] = """<h1>Field Notes</h1><section aria-label='劇集' data-series-url='/series/field-notes' data-episode-count='5'>
      <div id='rows'></div><button id='more'>Load more episodes</button></section>
      <script>let batch=0;function render(){rows.innerHTML=[1,2].map(i=>batch*2+i).filter(i=>i<=5).map(i=>`<a data-ordinal='${i}' href='/watch/${i}'>第${i}集</a>`).join('');more.disabled=batch===2;}more.onclick=()=>{batch++;render();};render();</script>"""
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(scanner, limit=3)
    assert result["pagesVisited"] == 3 and len(assets) == 2
    assert [entry["ordinal"] for entry in result["catalog"]["entries"]] == [1, 2, 3, 4, 5]
    assert result["catalog"]["truncated"] and "page-limit" in result["catalog"]["reasons"]
    assert not any(path in {"/watch/3", "/watch/4", "/watch/5"} for _, path in requests)


@pytest.mark.asyncio
async def test_preview_audio_and_multiple_main_video_candidates_remain_conservative():
    pages = fixture_pages(1)
    pages[
        "/watch/1"
    ] = """<main><video src='/media/first.mp4'></video><video src='/media/second.mp4'></video>
      <video aria-label='Official trailer' src='/media/preview.mp4'></video><video src='/media/audio1.mp4'></video></main>"""
    async with neutral_browser(pages) as (scanner, _requests):
        result, assets = await run_scan(scanner)
    assert [asset["classification"]["kind"] for asset in assets] == [
        "episode",
        "episode",
        "preview",
        "audio",
    ]
    catalog = update_catalog(result["catalog"], assets)
    assert catalog["entries"][0]["mediaStatus"] == "ambiguous"
    assert catalog["entries"][0]["downloadAssetId"] is None


@pytest.mark.asyncio
async def test_page_access_evidence_is_fresh_per_scan_and_reports_only_actual_gates():
    pages = fixture_pages(2)
    pages["/watch/2"] = '<input type="password"><button>Sign in</button>'
    async with neutral_browser(pages) as (scanner, _requests):
        initial, assets = await run_scan(scanner)
        result, _ = await run_scan(
            scanner,
            url=ORIGIN + "/watch/1",
            seedUrls=[ORIGIN + "/watch/1"],
            followCatalog=False,
            catalog=initial["catalog"],
        )
    assert initial["_pageAccess"] == {
        SITE["url"]: "observed",
        ORIGIN + "/watch/1": "observed",
        ORIGIN + "/watch/2": "needs-user",
    }
    assert result["_pageAccess"] == {ORIGIN + "/watch/1": "observed"}
    assert len(assets) == 1


@pytest.mark.asyncio
async def test_gate_observes_visible_directory_without_clicking_its_pager():
    pages = fixture_pages()
    pages["/series/field-notes"] = '<input type="password">' + directory().replace(
        "</section>",
        "<button onclick=\"fetch('/clicked')\">Load more episodes</button></section>",
    )
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(scanner)
    assert result["status"] == "needs-user" and not assets
    assert result["_pageAccess"] == {SITE["url"]: "needs-user"}
    assert ("GET", "/clicked") not in requests


@pytest.mark.asyncio
async def test_series_cancellation_closes_scan_context_and_stops_before_episodes():
    async with neutral_browser(fixture_pages()) as (scanner, requests):
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 4

        async def ignore(_value):
            pass

        result = await scanner.scan(
            SITE,
            {"mode": "series", "url": SITE["url"], "limit": 100, "kinds": ["video"]},
            ignore,
            ignore,
            cancelled,
        )
        user_context = await scanner.browser.context_for(SITE["id"])
        assert user_context.browser.contexts == [user_context]
    assert result["status"] == "cancelled"
    assert all(not path.startswith("/watch/") for _, path in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media", "main", "expected"),
    [
        ("/media/1.mp4", True, "resolved"),
        ("/media/1.mp4", False, "needs-user"),
        ("/media/1.mp4?token=rotated", True, "needs-user"),
    ],
)
async def test_generic_refresh_reobserves_main_video_and_keeps_exact_source_identity(
    media, main, expected
):
    from backend.generic_catalog import metadata_for

    metadata = metadata_for(SITE["url"], "Field Notes")
    page_url = ORIGIN + "/watch/1"
    old = {
        "sourceUrl": ORIGIN + "/media/1.mp4",
        "pageUrl": page_url,
        "kind": "video",
        "engine": "http",
        "_episodeEvidence": {
            "version": 1,
            "adapterId": "generic",
            "source": "observed-main-video",
            "sourceUrl": ORIGIN + "/media/1.mp4",
            "pageUrl": page_url,
            "catalogUrl": SITE["url"],
            "seriesKey": metadata["seriesKey"],
            "episodeKey": metadata["seriesKey"] + ":1",
            "ordinal": 1,
        },
    }
    wrapper = "main" if main else "aside"
    async with neutral_browser(
        {"/watch/1": f"<{wrapper}><video src='{media}'></video></{wrapper}>"}
    ) as (scanner, _requests):
        result = await scanner.resolve_asset(SITE, old, lambda: False)
    assert result["status"] == expected
    if expected == "resolved":
        assert result["candidate"]["_episodeEvidence"]["sourceUrl"] == old["sourceUrl"]
        assert result["candidate"]["classification"]["kind"] == "episode"


@pytest.mark.asyncio
async def test_equivalent_catalog_roots_merge_count_conflicts_instead_of_claiming_complete():
    pages = fixture_pages(2)
    pages["/series/field-notes"] = directory(2) + directory(2).replace(
        "data-episode-count='2'", "data-episode-count='3'"
    )
    async with neutral_browser(pages) as (scanner, _requests):
        result, assets = await run_scan(scanner)
    catalog = update_catalog(result["catalog"], assets)
    assert catalog["expectedCount"] is None
    assert "conflicting-counts" in catalog["reasons"]
    assert catalog["status"] != "complete-observed"


@pytest.mark.asyncio
async def test_equivalent_catalog_roots_keep_restrictive_access_evidence():
    pages = fixture_pages(2)
    pages["/series/field-notes"] = directory(2) + directory(2).replace(
        "Episode 2</a>", "Episode 2 Members only</a>"
    )
    async with neutral_browser(pages) as (scanner, requests):
        result, _assets = await run_scan(scanner)
    assert ("GET", "/watch/2") not in requests
    assert result["catalog"]["entries"][1]["access"] == "needs-user"


@pytest.mark.asyncio
async def test_equivalent_catalog_roots_with_different_identity_are_not_auto_selected():
    pages = fixture_pages(2)
    pages["/series/field-notes"] = directory(2) + directory(2).replace(
        "data-series-url='/series/field-notes'", "data-series-url='/series/another'"
    )
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(scanner)
    assert result["pagesVisited"] == 1 and assets == []
    assert "catalog-conflict" in result["catalog"]["reasons"]
    assert not any(path.startswith("/watch/") for _, path in requests)


@pytest.mark.asyncio
async def test_automatic_pending_episode_rechecks_gate_discovered_after_queueing():
    pages = fixture_pages(2)
    pages["/watch/1"] += directory(2).replace("Episode 2</a>", "Episode 2 Members only</a>")
    async with neutral_browser(pages) as (scanner, requests):
        result, _assets = await run_scan(scanner)
        assert ("GET", "/watch/2") not in requests
        assert result["catalog"]["entries"][1]["access"] == "needs-user"
        requests.clear()
        recheck, _assets = await run_scan(
            scanner, url=ORIGIN + "/watch/2", followCatalog=False, catalog=result["catalog"]
        )
        assert ("GET", "/watch/2") in requests
        assert recheck["_pageAccess"] == {ORIGIN + "/watch/2": "observed"}


@pytest.mark.asyncio
async def test_rereading_directory_does_not_visit_dismissed_episode():
    async with neutral_browser(fixture_pages()) as (scanner, requests):
        initial, _ = await run_scan(scanner, limit=1)
        catalog = initial["catalog"]
        key = catalog["entries"][1]["episodeKey"]
        requests.clear()
        result, assets = await run_scan(scanner, catalog=catalog, excludedEpisodeKeys=[key])
    assert [e["ordinal"] for e in result["catalog"]["entries"]] == [1, 2, 3]
    assert result["pagesVisited"] == 3
    assert [a["pageUrl"] for a in assets] == [ORIGIN + "/watch/1", ORIGIN + "/watch/3"]
    assert all(path != "/watch/2" for _, path in requests)
