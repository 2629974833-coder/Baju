"""Neutral series indexes, available artwork and observed permission boundaries."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from backend.scanner import Scanner
from backend.site_catalog import _publisher_shows
from tests_backend.test_generic_series_scanning import ORIGIN, SITE


@asynccontextmanager
async def neutral_browser(pages, denied=None):
    requests = []
    async with async_playwright() as w:
        browser = await w.chromium.launch()

        class Contexts:
            async def context_for(self, site_id):
                raise ValueError("独立授权上下文未打开")

            async def create_scan_context(self, site):
                context = await browser.new_context(service_workers="block")

                async def serve(route):
                    address = urlsplit(route.request.url)
                    path = address.path + ("?" + address.query if address.query else "")
                    requests.append((route.request.method, path))
                    if denied and path in denied:
                        return await route.fulfill(body="Access required", status=403)
                    if path in pages:
                        return await route.fulfill(
                            body=pages[path], content_type="text/html; charset=utf-8"
                        )
                    mime = "image/png" if path.endswith(".png") else "video/mp4"
                    return await route.fulfill(body=b"fixture", content_type=mime)

                await context.route("**/*", serve)
                return context

        class Policy:
            async def validate(self, url):
                assert urlsplit(url).hostname == "documentary.example"
                return url

        try:
            yield Scanner(Contexts(), Policy()), requests
        finally:
            await browser.close()


def index_pages():
    def card(key, category):
        return f'<article class="show-card" data-category="{category}"><a href="/series/{key}"><img src="/art/{key}.png" width="200" height="150">{key}</a><a href="/cast/person">演员</a></article>'

    def detail(key):
        cover = f'<main><img width="200" height="150" src="/art/{key}-detail.png"></main>'
        entries = "".join(
            f'<a href="/{key}/ep/{n}" data-episode-number="{n}">Episode {n}{" · Members only" if n == 3 else ""}</a>'
            for n in (1, 2, 3)
        )
        return f'<h1>{key}</h1>{cover}<section data-series-url="/series/{key}" aria-label="Episodes" data-episode-count="3">{entries}</section>'

    pages = {
        "/": card("A", "剧情")
        + '<a rel="next" href="/p2">Next</a><a href="https://other.invalid/series/foreign">Ignore</a>',
        "/p2": card("B", "喜剧") + card("A", "剧情") + '<a rel="next" href="/">Next</a>',
    }
    for key in ("A", "B"):
        pages[f"/series/{key}"] = detail(key)
        for n in (1, 2):
            pages[f"/{key}/ep/{n}"] = (
                f'<main><video controls src="/media/{key}-{n}.mp4"></video><img width="320" height="200" src="/art/shared.png"></main><aside><img width="320" height="200" src="/art/advert.png"></aside>'
            )
    return pages


async def collect(scanner, **options):
    found = []

    async def asset(item):
        found.append(dict(item))

    async def progress(_):
        pass

    result = await scanner.scan(
        SITE,
        {"mode": "site", "url": ORIGIN + "/", "limit": 50, **options},
        asset,
        progress,
        lambda: False,
    )
    return result, found


@pytest.mark.asyncio
async def test_site_follows_index_pages_and_groups_shared_art_and_video():
    async with neutral_browser(index_pages()) as (scanner, requests):
        result, assets = await collect(scanner)
    assert result["status"] == "completed", result
    catalog = result["siteCatalog"]
    assert catalog["indexPages"] == 2
    assert [row["categories"] for row in catalog["shows"]] == [["剧情"], ["喜剧"]]
    assert result["pagesVisited"] == 8
    assert all(len(row["catalog"]["entries"]) == 3 for row in catalog["shows"])
    assert all(row["status"] == "partial" for row in catalog["shows"])
    assert "restricted" in catalog["reasons"]
    assert len([item for item in assets if item["kind"] == "video"]) == 4
    assert len([item for item in assets if item["sourceUrl"].endswith("shared.png")]) == 4
    assert all(item.get("_intakeGroupUrl", "").startswith(ORIGIN + "/series/") for item in assets)
    assert any(item["sourceUrl"].endswith("A-detail.png") for item in assets)
    assert not any(item["sourceUrl"].endswith("advert.png") for item in assets)
    assert not any("/ep/3" in path for _, path in requests)


@pytest.mark.asyncio
async def test_index_only_budget_retains_all_recognized_cards_and_limit():
    async with neutral_browser(index_pages()) as (scanner, requests):
        result, _ = await collect(scanner, limit=1)
    assert result["pagesVisited"] == 1 and result["limited"]
    assert [row["title"] for row in result["siteCatalog"]["shows"]] == ["A"]
    assert not any(path in {"/series/A", "/p2"} for _, path in requests)


@pytest.mark.asyncio
async def test_one_show_collects_directory_images_and_shared_episode_images_once_per_page():
    async with neutral_browser(index_pages()) as (scanner, requests):
        result, assets = await collect(
            scanner,
            mode="series",
            url=ORIGIN + "/series/A",
            intakeScope="series",
            _captureCatalogImages=True,
        )
    assert result["status"] == "completed", result
    assert len(result["catalog"]["entries"]) == 3
    assert not any(path == "/A/ep/3" for _, path in requests)
    assert any(a["sourceUrl"].endswith("A-detail.png") for a in assets)
    shared = [a for a in assets if a["sourceUrl"].endswith("shared.png")]
    assert {a["pageUrl"] for a in shared} == {ORIGIN + "/A/ep/1", ORIGIN + "/A/ep/2"}


@pytest.mark.asyncio
async def test_official_site_reads_catalog_before_players_and_skips_locked_entries(monkeypatch):
    origin = "https://lustflix.tv"
    rows = _publisher_shows(
        origin,
        [{"id": "C11", "slug": "sample", "title": "样例"}],
        [
            {
                "slug": "sample",
                "title": "样例",
                "category": "剧情",
                "hero": "/cover.jpg",
                "episodes": [{"n": 1, "priceCoins": 0}, {"n": 2, "priceCoins": 9}],
            }
        ],
    )

    async def index(*_):
        return {"shows": rows, "limited": False, "pages": []}

    monkeypatch.setattr("backend.site_catalog.read_publisher_index", index)
    from backend.site_catalog import scan_site

    visits = []

    class Policy:
        async def validate(self, url):
            return url

    class Scanner:
        policy = Policy()

        async def scan(self, site, options, on_asset, on_progress, cancelled):
            visits.extend(options["seedUrls"])
            return {
                "catalog": options["catalog"],
                "pagesVisited": len(options["seedUrls"]),
                "status": "completed",
            }

    result, found = [], []

    async def progress(value):
        result.append(value)

    async def asset(value):
        found.append(value)

    await scan_site(
        Scanner(), {"id": "lustflix"}, {"url": origin, "limit": 10}, asset, progress, lambda: False
    )
    assert visits == [origin + "/#/watch/C11/1"]
    (show,) = result[-1]["siteCatalog"]["shows"]
    assert show["title"] == "样例" and show["category"] == "剧情"
    assert {a["sourceUrl"] for a in found} == {origin + "/cover.jpg"}
    assert len(show["catalog"]["entries"]) == 2 and show["status"] == "partial"


@pytest.mark.asyncio
async def test_category_page_label_is_inherited_through_index_pagination():
    pages = {
        "/": '<nav><a href="/category/drama">剧情</a></nav>',
        "/category/drama": '<a rel="next" href="/category/drama?page=2">下一页</a><article data-series-card><a href="/series/A">剧 A</a></article>',
    }
    pages["/category/drama?page=2"] = (
        '<article data-series-card><a href="/series/B">剧 B</a></article>'
    )
    async with neutral_browser(pages) as (scanner, _):
        result, _ = await collect(scanner, limit=3)
    assert [(show["title"], show["categories"]) for show in result["siteCatalog"]["shows"]] == [
        ("剧 A", ["剧情"]),
        ("剧 B", ["剧情"]),
    ]
    assert result["siteCatalog"]["limited"]


@pytest.mark.asyncio
async def test_one_lustflix_show_uses_verified_catalog_then_one_open_player(monkeypatch):
    from tests_backend.test_lustflix_directory import PAGE, Policy, directory_server

    paths = directory_server(
        monkeypatch,
        [{"episode": 1, "viewerPriceCoins": 0}, {"episode": 2, "viewerPriceCoins": 15}],
        extra={"series": {"slug": "her-prisoner", "poster": "/show.jpeg", "memberOnly": False}},
    )
    visits, media = [], []

    class TracedScanner(Scanner):
        async def scan(self, site, options, on_asset, on_progress, cancelled):
            if seeds := options.get("seedUrls"):
                visits.extend(seeds)
                return {
                    "catalog": options["catalog"],
                    "pagesVisited": len(seeds),
                    "status": "completed",
                }
            return await super().scan(site, options, on_asset, on_progress, cancelled)

    async def found(asset):
        media.append(asset)

    async def progress(_):
        pass

    options = {
        "mode": "series",
        "intakeScope": "series",
        "_captureCatalogImages": True,
        "kinds": ["image", "video"],
        "limit": 4,
        "url": PAGE,
    }
    result = await TracedScanner(None, Policy()).scan(
        {"id": "lf"}, options, found, progress, lambda: False
    )
    assert result["status"] == "completed"
    assert visits == [PAGE] and result["pagesVisited"] == 2
    assert paths == ["/", "/assets/catalog.js", "/api/tv/catalog", "/api/content"]
    assert len(result["catalog"]["entries"]) == 2 and result["limited"]
    (cover,) = media
    assert cover["_intakePageUrl"] == "https://lustflix.tv/#/s/C13" and cover["sourceUrl"].endswith(
        "/show.jpeg"
    )


@pytest.mark.asyncio
async def test_index_access_gate_preserves_reason_without_scanning_the_login_form():
    async with neutral_browser({"/": "<input type='password' aria-label='Password'>"}) as (
        scanner,
        _,
    ):
        result, assets = await collect(scanner)
    assert result["status"] == "needs-user" and not assets
    assert result["siteCatalog"]["shows"] == []


@pytest.mark.asyncio
async def test_site_card_with_episode_link_uses_observed_up_link_then_resolves_that_episode():
    pages = index_pages()
    pages["/"] = '<article class="series-card"><a href="/A/ep/1">A</a></article>'
    pages["/A/ep/1"] += '<a rel="up" href="/series/A">Series directory</a>'
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await collect(scanner)
    (show,) = result["siteCatalog"]["shows"]
    assert len(show["catalog"]["entries"]) == 3
    assert any(path == "/series/A" for _, path in requests)
    assert sum(item["kind"] == "video" for item in assets) == 2
    assert not any(path == "/A/ep/3" for _, path in requests)


@pytest.mark.asyncio
async def test_same_image_as_series_cover_and_actual_episode_art_retains_both_memberships():
    from backend.site_adapters import build_catalog
    from backend.site_catalog import scan_site

    page = "https://lustflix.tv/#/watch/C13/1"
    cover = "https://lustflix.tv/poster.png"
    catalog = {**build_catalog(page, [{"url": page}], expected_count=1), "coverUrls": [cover]}

    class Policy:
        async def validate(self, url):
            return url

    class Source:
        policy = Policy()

        async def scan(self, site, opts, on_asset, on_progress, cancelled):
            if opts.get("seedUrls"):
                await on_asset({"sourceUrl": cover, "pageUrl": page, "kind": "image"})
            return {"catalog": catalog, "pagesVisited": 1, "status": "completed"}

    observed = []

    async def found(asset):
        observed.append((asset["sourceUrl"], asset.get("_intakePageUrl") or asset["pageUrl"]))

    async def progress(_result):
        pass

    await scan_site(
        Source(),
        {"id": "fixture"},
        {"url": page, "limit": 2, "kinds": ["image"]},
        found,
        progress,
        lambda: False,
        one_show=True,
    )
    assert observed == [(cover, "https://lustflix.tv/#/s/C13"), (cover, page)]


@pytest.mark.asyncio
async def test_manual_episode_can_recheck_access_when_player_denies_but_poster_is_public():
    page = ORIGIN + "/A/ep/3"
    pages = index_pages()
    pages["/A/ep/3"] = (
        '<main><img src="/art/public-poster.png" width="400" height="240"><script>fetch("/player-access")</script></main>'
    )
    denied = {"/player-access"}
    async with neutral_browser(pages, denied) as (scanner, requests):
        site_result, _ = await collect(scanner)
        group = next(row for row in site_result["siteCatalog"]["shows"] if row["title"] == "A")
        assert group["catalog"]["entries"][2]["access"] == "needs-user"
        before = len(requests)
        report, found = await collect(
            scanner, mode="single", intakeScope="single", url=page, limit=1, readOnly=True
        )
        assert ("GET", "/A/ep/3") in requests[before:]
        assert report["status"] == "needs-user", report
        assert {a["sourceUrl"] for a in found} == {ORIGIN + "/art/public-poster.png"}

        denied.clear()
        pages["/A/ep/3"] += '<main><video controls src="/media/A-3.mp4"></video></main>'
        report, found = await collect(
            scanner, mode="single", intakeScope="single", url=page, limit=1, readOnly=True
        )
    assert report["status"] == "completed"
    assert any(a["kind"] == "video" and a["sourceUrl"].endswith("A-3.mp4") for a in found)


@pytest.mark.asyncio
async def test_resume_site_consumes_index_frontier_then_unvisited_episodes():
    async with neutral_browser(index_pages()) as (scanner, requests):
        state = None
        sources = []
        for limit in (1, 1, 2, 1, 3):
            before = len(requests)
            prior = state
            state, found = await collect(scanner, limit=limit, _resumeIntake=prior)
            sources.extend((a["pageUrl"], a["sourceUrl"]) for a in found)
            assert state["runPagesVisited"] == limit
            assert not any(path == "/A/ep/3" or path == "/B/ep/3" for _, path in requests[before:])
            if limit != 3:
                assert state["canContinue"], state
    assert state["pagesVisited"] == 8 and not state["canContinue"]
    assert state["limited"] and "restricted" in state["siteCatalog"]["reasons"]
    documents = [p for _, p in requests if p in index_pages()]
    assert sorted(documents) == sorted(
        ["/", "/p2", "/series/A", "/series/B", "/A/ep/1", "/A/ep/2", "/B/ep/1", "/B/ep/2"]
    )
    assert len([source for source in sources if source[1].endswith("shared.png")]) == 4


@pytest.mark.asyncio
async def test_resume_whole_show_opens_observed_parent_without_rereading_input_for_directory():
    pages = index_pages()
    pages["/A/ep/1"] += '<a rel="up" href="/series/A">Series directory</a>'
    url = ORIGIN + "/A/ep/1"
    options = {"mode": "series", "intakeScope": "series", "_captureCatalogImages": True, "url": url}
    async with neutral_browser(pages) as (scanner, requests):
        first, _ = await collect(scanner, **options, limit=1)
        assert first["canContinue"] and first["pagesVisited"] == 1
        before = len(requests)
        second, _ = await collect(scanner, **options, limit=1, _resumeIntake=first)
        assert second["canContinue"] and second["pagesVisited"] == 2
        assert ("GET", "/series/A") in requests[before:] and ("GET", "/A/ep/1") not in requests[
            before:
        ]
        final, _ = await collect(scanner, **options, limit=2, _resumeIntake=second)
    assert final["pagesVisited"] == 4 and not final["canContinue"]
    assert len(final["catalog"]["entries"]) == 3


@pytest.mark.asyncio
async def test_timeout_persists_completed_child_pages_then_restarts_at_next_episode():
    from backend.site_adapters import build_catalog
    from backend.site_catalog import scan_site

    home = "https://lustflix.tv/#/s/C13"
    urls = [f"https://lustflix.tv/#/watch/C13/{n}" for n in (1, 2, 3)]
    catalog = build_catalog(home, [{"url": p} for p in urls], expected_count=3)

    class Policy:
        async def validate(self, url):
            return url

    class Source:
        policy = Policy()
        emitted_timeout = False
        visits = []

        async def scan(self, site, opts, on_asset, on_progress, cancelled):
            if opts.get("_directoryOnly"):
                return {"catalog": catalog, "pagesVisited": 1, "status": "completed"}
            seeds = opts["seedUrls"]
            self.visits.append(list(seeds))
            if not self.emitted_timeout:
                self.emitted_timeout = True
                await on_progress(
                    {"catalog": catalog, "pagesVisited": 1, "_pageAccess": {urls[0]: "observed"}}
                )
                return {
                    "catalog": catalog,
                    "pagesVisited": 2,
                    "status": "timed-out",
                    "_pageAccess": {urls[0]: "observed"},
                }
            return {"catalog": catalog, "pagesVisited": len(seeds), "status": "completed"}

    source = Source()

    async def ignore(_):
        pass

    saved = []

    async def progress(p):
        import copy

        saved.append(copy.deepcopy(p))

    opts = {"url": home, "limit": 6, "kinds": ["video"]}
    first = await scan_site(source, {}, opts, ignore, progress, lambda: False, one_show=True)
    assert first["status"] == "timed-out" and first["canContinue"]
    live = next(
        s
        for s in saved
        if s["status"] == "running"
        and urls[0] in s["_intakeCheckpoint"]["checkedPages"].get(home, [])
    )
    # Even an unexpected shutdown before the child returned retains the settled first page.
    assert live["canContinue"]
    final = await scan_site(
        source, {}, {**opts, "_resumeIntake": live}, ignore, progress, lambda: False, one_show=True
    )
    assert source.visits == [urls, urls[1:]] and not final["canContinue"]
    assert final["pagesVisited"] == 4
