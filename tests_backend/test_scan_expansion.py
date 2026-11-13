import pytest

from backend.scanner import Scanner
from backend.site_adapters import build_catalog


class Policy:
    async def validate(self, url):
        return url


class ResolutionScanner(Scanner):
    def __init__(self, candidates, status="completed"):
        super().__init__(None, Policy())
        self.candidates, self.status, self.calls = candidates, status, []

    async def scan(self, site, options, on_asset, on_progress, cancelled):
        self.calls.append(options)
        for candidate in self.candidates:
            await on_asset(candidate)
        return await on_progress({"status": self.status})


def original():
    return {
        "sourceUrl": "https://lustflix.tv/api/media/series-a/1/master.m3u8?token=old",
        "pageUrl": "https://lustflix.tv/#/watch/C04/1",
        "kind": "video",
        "engine": "yt-dlp",
    }


@pytest.mark.asyncio
async def test_resolution_uses_exact_page_and_unique_strong_identity():
    old = original()
    new = {**old, "sourceUrl": old["sourceUrl"].replace("old", "new")}
    wrong = {**new, "sourceUrl": new["sourceUrl"].replace("series-a", "series-b")}
    scanner = ResolutionScanner([wrong, new])
    result = await scanner.resolve_asset({"id": "lustflix"}, old, lambda: False)
    assert result["candidate"] == new
    assert scanner.calls[0]["url"] == old["pageUrl"]
    assert scanner.calls[0]["depth"] == 0 and scanner.calls[0]["limit"] == 1
    ambiguous = await ResolutionScanner(
        [new, {**new, "sourceUrl": new["sourceUrl"] + "2"}]
    ).resolve_asset({}, old, lambda: False)
    assert ambiguous["reasonCode"] == "ambiguous-media"
    mismatch = await ResolutionScanner([wrong]).resolve_asset({}, old, lambda: False)
    assert mismatch["reasonCode"] == "identity-mismatch"


@pytest.mark.asyncio
async def test_gates_and_generic_signed_urls_never_silently_replace_original():
    old = {**original(), "sourceUrl": "https://cdn.example/video?token=old"}
    new = {**old, "sourceUrl": "https://cdn.example/video?token=new"}
    assert (await ResolutionScanner([new]).resolve_asset({}, old, lambda: False))[
        "status"
    ] == "needs-user"
    assert (await ResolutionScanner([old], "needs-user").resolve_asset({}, old, lambda: False))[
        "reasonCode"
    ] == "access-required"
    assert (
        await ResolutionScanner([old], "unsupported-drm").resolve_asset({}, old, lambda: False)
    )["status"] == "unsupported"


def test_catalog_merge_preserves_visited_gates_and_unknown_totals():
    page = original()["pageUrl"]
    first = build_catalog(page, [{"url": page, "access": "needs-user"}])
    merged = Scanner._merge_catalog(
        first, page, {"links": [{"url": page}, {"url": page[:-1] + "2"}]}
    )
    assert merged["entries"][0]["access"] == "needs-user"
    assert merged["enumeratedCount"] == 2 and merged["expectedCount"] is None
    assert merged["status"] != "complete-observed"


@pytest.mark.asyncio
async def test_incomplete_scan_cannot_establish_unique_refresh_match():
    class LimitedScanner(ResolutionScanner):
        async def scan(self, site, options, on_asset, on_progress, cancelled):
            await on_asset(original())
            await on_progress({"status": "completed", "limited": True})

    assert (await LimitedScanner([]).resolve_asset({}, original(), lambda: False))[
        "reasonCode"
    ] == "refresh-failed"


def test_successful_exact_page_visit_clears_only_its_previous_gate():
    page = original()["pageUrl"]
    prior = build_catalog(
        page,
        [{"url": page, "access": "needs-user"}, {"url": page[:-1] + "2", "access": "needs-user"}],
    )
    merged = Scanner._merge_catalog(prior, page, {"links": []}, access="observed")
    assert merged["entries"][0]["access"] == "observed"
    assert merged["entries"][1]["access"] == "needs-user"


@pytest.mark.asyncio
async def test_extractor_gate_cannot_refresh_dom_candidate():
    class GatedExtractorScanner(ResolutionScanner):
        async def scan(self, site, options, on_asset, on_progress, cancelled):
            await on_asset(original())
            await on_progress({"status": "completed", "extraction": {"status": "needs-user"}})

    assert (await GatedExtractorScanner([]).resolve_asset({}, original(), lambda: False))[
        "reasonCode"
    ] == "access-required"


@pytest.mark.asyncio
async def test_visible_lustflix_selector_enumerates_only_observed_rows(tmp_path):
    from playwright.async_api import async_playwright

    from backend.scanner import _DOM_MEDIA

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("""<section><h2>Vampire Boyfriend</h2><div>
              <div id="playerEpisode1"><h4>Episode 1</h4><p>Playing</p></div>
              <div id="playerEpisode2"><h4>Episode 2</h4><p>Members only</p></div>
              <div id="playerEpisode5"><h4>Episode 5</h4><p>Members only</p></div></div>
              <div style="display:none">Episode 999</div></section>
              <aside>50 episodes</aside>""")
            script = (
                _DOM_MEDIA.replace("location.hostname", "'lustflix.tv'")
                .replace("location.hash", "'#/watch/C04/1'")
                .replace("location.origin", "'https://lustflix.tv'")
            )
            contents = await page.evaluate(script)
            catalog = Scanner._merge_catalog(None, original()["pageUrl"], contents)
            assert [entry["ordinal"] for entry in catalog["entries"]] == [1, 2, 5]
            assert catalog["entries"][0]["access"] == "unknown"
            assert catalog["entries"][1]["access"] == "needs-user"
            assert catalog["expectedCount"] is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_selected_seed_pages_are_all_visited_without_crawling(tmp_path):
    from backend.browser import BrowserService
    from backend.network import NetworkPolicy
    from tests_backend.fixture_server import media_fixture

    with media_fixture() as fixture:
        policy = NetworkPolicy({"127.0.0.1"})
        browser = BrowserService(tmp_path, policy)
        site = {"id": "fixture", "name": "Fixture", "url": fixture.url}
        await browser.start()
        try:
            reports = []

            async def collect(asset):
                pass

            async def progress(value):
                reports.append(value)

            await Scanner(browser, policy).scan(
                site,
                {
                    "url": fixture.url + "/one",
                    "seedUrls": [fixture.url + "/one", fixture.url + "/two"],
                    "limit": 2,
                    "depth": 0,
                    "kinds": ["image"],
                },
                collect,
                progress,
                lambda: False,
            )
            assert reports[-1]["pagesVisited"] == 2
            assert {path for path, _ in fixture.requests} >= {"/one", "/two"}
        finally:
            await browser.close()
