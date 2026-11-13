"""Real Chromium against route-fulfilled pages; no target-site/network access."""

import asyncio
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from backend.catalog_discovery import collect_catalog
from backend.scanner import Scanner

ORIGIN = "https://lustflix.tv"
PAGE = ORIGIN + "/#/watch/C03/5"
SITE = {"id": "lustflix", "name": "Fixture series", "url": ORIGIN}


@asynccontextmanager
async def fixture_browser(html, *, navigation_status=()):
    requests, sockets = [], []
    statuses = iter(navigation_status)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")

        async def respond(route):
            requests.append((route.request.method, urlsplit(route.request.url).path))
            if urlsplit(route.request.url).path == "/":
                response = next(statuses, 200)
                if isinstance(response, str):
                    await route.abort(response)
                else:
                    await route.fulfill(status=response, content_type="text/html", body=html)
            else:
                await route.fulfill(status=200, content_type="text/plain", body="fixture")

        async def websocket(route):
            sockets.append(route.url)
            await route.close()

        await context.route("**/*", respond)
        await context.route_web_socket("**/*", websocket)
        new_context = browser.new_context

        async def fixture_context(**kwargs):
            created = await new_context(**kwargs)
            await created.route("**/*", respond)
            await created.route_web_socket("**/*", websocket)
            return created

        # A scan has its own context, but every request still terminates at this fixture.
        browser.new_context = fixture_context

        class Browser:
            async def open_session(self, _site):
                return {"open": True}

            async def context_for(self, _site_id):
                return context

            async def create_scan_context(self, _site):
                return await browser.new_context(
                    storage_state=await context.storage_state(indexed_db=True),
                    service_workers="block",
                    accept_downloads=False,
                )

        class Policy:
            async def validate(self, url):
                assert urlsplit(url).hostname == "lustflix.tv"
                return url

        try:
            yield Scanner(Browser(), Policy()), context, requests, sockets
        finally:
            await browser.close()


async def scan_fixture(scanner, *, depth=0, limit=1, cancelled=lambda: False):
    assets, events = [], []

    async def found(asset):
        assets.append(asset)

    async def progress(event):
        events.append(event)

    result = await scanner.scan(
        SITE,
        {"url": PAGE, "depth": depth, "limit": limit, "kinds": ["video"]},
        found,
        progress,
        cancelled,
    )
    return result, assets, events


@pytest.mark.asyncio
async def test_virtual_episode_list_accumulates_rows_in_its_own_scroll_container():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
    <section aria-label='Episodes' data-episode-count='30'>
      <div id='list' style='overflow-y:auto;height:140px;width:300px'>
        <div id='space' style='height:1800px;position:relative'></div>
      </div>
    </section>
    <script>
      const list = document.querySelector('#list'), space = document.querySelector('#space');
      function render() {
        const first = Math.min(25, Math.floor(list.scrollTop/60));
        space.innerHTML = Array.from({length:5},(_,i) =>
          `<div style='position:absolute;top:${(first+i)*60}px;height:60px'>Episode ${first+i+1}</div>`).join('');
      }
      list.addEventListener('scroll',render); render();
    </script>"""
    async with fixture_browser(html) as (scanner, _context, _requests, _sockets):
        result, _, _ = await scan_fixture(scanner)
    assert [entry["ordinal"] for entry in result["catalog"]["entries"]] == list(range(1, 31))
    assert result["catalog"]["expectedCount"] == 30
    assert result["catalog"]["truncated"] is False


@pytest.mark.asyncio
async def test_scanner_blocks_write_requests_and_websockets_without_changing_user_pages():
    html = """<title>Fixture series</title><video src='/episode.mp4' autoplay muted></video>
      <script>fetch('/unlock', {method:'POST'}).catch(()=>{});
      new WebSocket('wss://lustflix.tv/socket');</script>"""
    async with fixture_browser(html) as (scanner, context, requests, sockets):
        user_page = await context.new_page()
        await user_page.goto(ORIGIN)
        await user_page.wait_for_timeout(100)
        assert ("POST", "/unlock") in requests and sockets
        requests.clear()
        sockets.clear()
        result, assets, _ = await scan_fixture(scanner)
        assert ("POST", "/unlock") not in requests
        assert sockets == []
        assert "read-only-blocked" in result["catalog"]["reasons"]
        assert assets
        assert not user_page.is_closed()


@pytest.mark.asyncio
async def test_same_origin_episode_change_stops_collection_instead_of_relabelling_media():
    html = """<title>Fixture series</title><video src='/episode-five.mp4'></video>
      <button title='Episodes' onclick="location.hash='/watch/C03/6'">Episodes</button>"""
    async with fixture_browser(html) as (scanner, _context, _requests, _sockets):
        result, assets, _ = await scan_fixture(scanner)
    assert assets == []
    assert result["status"] == "needs-user"
    assert "route-changed" in result["catalog"]["reasons"]


def test_known_permission_gate_is_never_queued_for_crawling():
    assert (
        Scanner._crawl_link(
            {"url": ORIGIN + "/#/watch/C03/6", "text": "Episode 6", "access": "needs-user"},
            PAGE,
            "lustflix.tv",
        )
        is None
    )


@pytest.mark.asyncio
async def test_delayed_episode_selector_is_opened_before_catalog_collection():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
    <div id='controls'></div><div id='list' aria-label='Episodes' style='display:none'>
      <h2>8 Episodes</h2><div id='rows'></div></div>
    <script>
      document.querySelector('#rows').innerHTML = Array.from({length:8},(_,i)=>`<div>Episode ${i+1}</div>`).join('');
      setTimeout(()=>{document.querySelector('#controls').innerHTML =
      `<button title='Episodes' onclick="document.querySelector('#list').style.display='block'">Episodes</button>`;},600);
    </script>"""
    async with fixture_browser(html) as (scanner, _context, _requests, _sockets):
        result, _, _ = await scan_fixture(scanner)
    assert [entry["ordinal"] for entry in result["catalog"]["entries"]] == list(range(1, 9))
    assert result["catalog"]["expectedCount"] == 8


@pytest.mark.asyncio
async def test_route_change_during_empty_extraction_is_still_reported():
    html = "<title>Fixture series</title><p>No visible media</p>"
    async with fixture_browser(html) as (scanner, context, _requests, _sockets):

        class ChangingExtractor:
            async def extract(self, *args, **kwargs):
                await (
                    next(item for item in context.browser.contexts if item != context)
                    .pages[-1]
                    .evaluate("location.hash='/watch/C03/6'")
                )
                return {"status": "completed", "assets": [], "entries": []}

        scanner.extractor = ChangingExtractor()
        result, assets, _ = await scan_fixture(scanner)
    assert assets == []
    assert result["status"] == "needs-user"
    assert "route-changed" in result["catalog"]["reasons"]


@pytest.mark.asyncio
async def test_locked_anchor_is_not_crawled_even_when_raw_link_has_no_access_attribute():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <section aria-label='Episodes'><a href='/#/watch/C03/6'>
      <h4>Episode 6</h4><p>Members only</p></a></section>"""
    async with fixture_browser(html) as (scanner, _context, requests, _sockets):
        result, _, _ = await scan_fixture(scanner, depth=1, limit=2)
    assert result["pagesVisited"] == 1
    assert requests.count(("GET", "/")) == 1
    locked = next(entry for entry in result["catalog"]["entries"] if entry["ordinal"] == 6)
    assert locked["access"] == "needs-user"


@pytest.mark.asyncio
async def test_unknown_pager_destination_cannot_navigate_the_scan_to_another_origin():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <section aria-label='Episodes'><div>Episode 5</div>
      <button aria-label='Next page' onclick="location.href='https://outside.invalid/unlock'">Next</button></section>"""
    async with fixture_browser(html) as (scanner, _context, requests, _sockets):
        result, assets, _ = await scan_fixture(scanner)
    assert ("GET", "/unlock") not in requests
    assert result["status"] == "needs-user"
    assert assets == []


@pytest.mark.asyncio
async def test_catalog_time_budget_includes_slow_dom_reads():
    class SlowPage:
        url = PAGE

        async def evaluate(self, *_args):
            await asyncio.sleep(0.3)
            return {"links": [], "counts": [], "scrolls": [], "pagers": []}

    started = time.monotonic()
    result = await collect_catalog(SlowPage(), PAGE, lambda: False, max_seconds=0.02)
    assert time.monotonic() - started < 0.15
    assert result["linksTruncated"] and "catalog-limit" in result["reasons"]


@pytest.mark.asyncio
async def test_read_only_pagination_accumulates_rows_without_clicking_episodes():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <section aria-label='Episodes' data-episode-count='9'>
      <div id='rows'></div><button id='more'>Load more episodes</button></section>
      <script>let page=0; function render(){
        rows.innerHTML=Array.from({length:3},(_,i)=>`<div onclick="fetch('/episode-clicked',{method:'POST'})">Episode ${page*3+i+1}</div>`).join('');
        if(page===2) more.disabled=true;
      } more.onclick=()=>{page++;render();};render();</script>"""
    async with fixture_browser(html) as (scanner, _context, requests, _sockets):
        result, _, _ = await scan_fixture(scanner)
    assert [entry["ordinal"] for entry in result["catalog"]["entries"]] == list(range(1, 10))
    assert not any(path == "/episode-clicked" for _, path in requests)
    assert not result["catalog"]["truncated"]


@pytest.mark.asyncio
async def test_stalled_pager_stops_without_claiming_complete_enumeration():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <section aria-label='Episodes'><div>Episode 5</div>
      <button>Load more episodes</button></section>"""
    async with fixture_browser(html) as (scanner, _context, _requests, _sockets):
        result, _, _ = await scan_fixture(scanner)
    assert result["catalog"]["truncated"]
    assert "catalog-stalled" in result["catalog"]["reasons"]
    assert result["catalog"]["expectedCount"] is None


@pytest.mark.asyncio
async def test_catalog_step_limit_keeps_observed_links_and_reports_partial_scope():
    html = """<section aria-label='Episodes'><div id='row'>Episode 1</div>
      <button onclick="row.innerText='Episode '+(++count)">Load more episodes</button></section>
      <script>let count=1;</script>"""
    async with fixture_browser(html) as (_scanner, context, _requests, _sockets):
        page = await context.new_page()
        await page.goto(PAGE)
        result = await collect_catalog(page, PAGE, lambda: False, max_steps=3)
    assert len(result["links"]) == 3
    assert result["expectedCount"] is None
    assert result["linksTruncated"] and "catalog-limit" in result["reasons"]


@pytest.mark.asyncio
async def test_cancelling_catalog_traversal_closes_only_its_scan_page():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <section aria-label='Episodes'><div id='row'>Episode 1</div>
      <button onclick="row.innerText='Episode '+(++count)">Load more episodes</button></section>
      <script>let count=1;</script>"""
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 8

    async with fixture_browser(html) as (scanner, context, _requests, _sockets):
        user_page = await context.new_page()
        await user_page.goto(ORIGIN)
        result, assets, _ = await scan_fixture(scanner, cancelled=cancelled)
        assert context.pages == [user_page]
    assert result["status"] == "cancelled"
    assert assets == []
    assert result["catalog"]["entries"]


@pytest.mark.asyncio
async def test_autoplay_is_disabled_only_in_the_scan_page():
    html = """<title>Fixture series</title><video src='/episode.mp4' autoplay muted></video>
      <button title='Episodes' onclick="document.querySelector('video').play().catch(()=>{})">Episodes</button>"""
    states = []
    async with fixture_browser(html) as (scanner, context, _requests, _sockets):
        user_page = await context.new_page()
        await user_page.goto(ORIGIN)

        async def found(_asset):
            states.append(
                await next(item for item in context.browser.contexts if item != context)
                .pages[-1]
                .locator("video")
                .evaluate("media => ({paused:media.paused,autoplay:media.autoplay})")
            )

        async def progress(_value):
            pass

        await scanner.scan(
            SITE,
            {"url": PAGE, "limit": 1, "depth": 0, "kinds": ["video"]},
            found,
            progress,
            lambda: False,
        )
        assert await user_page.locator("video").evaluate("media=>media.autoplay") is True
    assert states == [{"paused": True, "autoplay": False}]


@pytest.mark.asyncio
async def test_unscoped_episode_text_and_other_series_links_never_create_watch_urls():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <aside><div>Episode 17</div><a href='/#/watch/OTHER/7'>Episode 7</a></aside>
      <a href='/#/watch/C03/6'>Episode 6</a>
      <div id='playerEpisode9'>Episode 9</div>
      <div id='playerEpisode999' style='display:none'>Episode 999</div>"""
    async with fixture_browser(html) as (scanner, _context, _requests, _sockets):
        result, _, _ = await scan_fixture(scanner)
    assert [entry["ordinal"] for entry in result["catalog"]["entries"]] == [5, 6, 9]
    assert result["catalog"]["expectedCount"] is None


@pytest.mark.asyncio
async def test_open_user_page_of_other_series_cannot_pollute_requested_series_total():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <section id='episodes' aria-label='Episodes'><div>Episode 1</div><div>Episode 5</div></section>
      <script>episodes.dataset.episodeCount=location.hash.includes('OTHER')?'70':'6';</script>"""
    async with fixture_browser(html) as (scanner, context, _requests, _sockets):
        user_page = await context.new_page()
        await user_page.goto(ORIGIN + "/#/watch/OTHER/1")
        result, _, _ = await scan_fixture(scanner)
    assert result["catalog"]["seriesKey"] == "C03"
    assert result["catalog"]["expectedCount"] == 6
    assert "conflicting-counts" not in result["catalog"]["reasons"]


@pytest.mark.asyncio
async def test_membership_gate_keeps_visible_catalog_without_opening_any_controls():
    html = """<title>Fixture series</title><h1>Members only</h1><p>Join to watch</p>
      <button title='Episodes' onclick="fetch('/clicked',{method:'POST'})">Episodes</button>
      <section aria-label='Episodes' data-episode-count='6'>
      <div>Episode 1</div><div>Episode 5<p>Members only</p></div><div>Episode 6</div></section>"""
    async with fixture_browser(html) as (scanner, _context, requests, _sockets):
        result, assets, _ = await scan_fixture(scanner)
    assert result["status"] == "needs-user"
    assert [entry["ordinal"] for entry in result["catalog"]["entries"]] == [1, 5, 6]
    assert result["catalog"]["expectedCount"] == 6
    assert assets == [] and not any(path == "/clicked" for _, path in requests)


@pytest.mark.asyncio
async def test_form_popup_first_post_is_blocked_and_only_scan_context_is_closed():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <form action='/popup-unlock' method='post' target='_blank'></form>
      <script>if (location.hash) document.forms[0].submit();</script>"""
    async with fixture_browser(html) as (scanner, context, requests, _sockets):
        user_page = await context.new_page()
        await user_page.goto(ORIGIN)
        initial_contexts = list(context.browser.contexts)
        result, _assets, _ = await scan_fixture(scanner)
        assert ("POST", "/popup-unlock") not in requests
        assert context.pages == [user_page]
        assert context.browser.contexts == initial_contexts
        assert "read-only-blocked" in result["catalog"]["reasons"]
        await user_page.evaluate("fetch('/manual-action', {method:'POST'})")
        assert ("POST", "/manual-action") in requests


@pytest.mark.asyncio
async def test_scan_context_copies_authorized_storage_without_mutating_user_storage():
    html = "<title>Fixture series</title><video src='/episode.mp4'></video>"
    captured = []
    async with fixture_browser(html) as (scanner, context, _requests, _sockets):
        user_page = await context.new_page()
        await user_page.goto(ORIGIN)
        await user_page.evaluate("""async () => {
          localStorage.setItem('fixture-token', 'authorized');
          document.cookie = 'fixture-session=authorized; path=/; SameSite=Lax';
          await new Promise((resolve,reject) => {
            const request = indexedDB.open('fixture-auth', 1);
            request.onupgradeneeded = () => request.result.createObjectStore('tokens');
            request.onerror = () => reject(request.error);
            request.onsuccess = () => {
              const db = request.result, transaction = db.transaction('tokens','readwrite');
              transaction.objectStore('tokens').put('authorized', 'access');
              transaction.oncomplete = () => {db.close();resolve();};
            };
          });
        }""")

        class InspectStorage:
            async def extract(self, *args, **kwargs):
                scan_page = next(
                    item for item in context.browser.contexts if item != context
                ).pages[-1]
                captured.append(
                    await scan_page.evaluate("""async () => {
                  const token = localStorage.getItem('fixture-token'), cookie = document.cookie;
                  localStorage.setItem('fixture-token','scan-only');
                  document.cookie = 'fixture-session=scan-only; path=/; SameSite=Lax';
                  const indexed = await new Promise((resolve,reject) => {
                    const request = indexedDB.open('fixture-auth', 1);
                    request.onerror = () => reject(request.error);
                    request.onsuccess = () => {
                      const db = request.result, get = db.transaction('tokens').objectStore('tokens').get('access');
                      get.onsuccess = () => {db.close();resolve(get.result);};
                    };
                  });
                  return {token,cookie,indexed};
                }""")
                )
                return {"status": "completed", "assets": [], "entries": []}

        scanner.extractor = InspectStorage()
        _result, assets, _ = await scan_fixture(scanner)
        assert assets and captured == [
            {"token": "authorized", "cookie": "fixture-session=authorized", "indexed": "authorized"}
        ]
        assert await user_page.evaluate("localStorage.getItem('fixture-token')") == "authorized"
        assert await user_page.evaluate("document.cookie") == "fixture-session=authorized"
        assert context.browser.contexts == [context]


@pytest.mark.asyncio
async def test_other_series_selector_does_not_supply_current_series_total_or_pager():
    html = """<title>Fixture series</title><video src='/episode.mp4'></video>
      <section aria-label='Episodes' data-episode-count='6'><div>Episode 5</div></section>
      <aside aria-label='Episodes' data-episode-count='70'>
        <a href='/#/watch/OTHER/1'>Episode 1</a>
        <button onclick="fetch('/other-pager-clicked')">Load more episodes</button>
      </aside>"""
    async with fixture_browser(html) as (scanner, _context, requests, _sockets):
        result, _, _ = await scan_fixture(scanner)
    assert result["catalog"]["expectedCount"] == 6
    assert "conflicting-counts" not in result["catalog"]["reasons"]
    assert not any(path == "/other-pager-clicked" for _, path in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [False, True], ids=["anonymous", "saved-after-restart"])
async def test_scan_without_open_user_window_never_navigates_an_authorization_context(
    tmp_path, saved
):
    import json

    from backend.browser import BrowserService

    class Policy:
        async def validate(self, url):
            assert urlsplit(url).hostname == "lustflix.tv"
            return url

    service = BrowserService(tmp_path, Policy())
    requests, assets, events = [], [], []
    await service.start()
    if saved:
        state = {
            "cookies": [],
            "origins": [
                {"origin": ORIGIN, "localStorage": [{"name": "fixture-token", "value": "restored"}]}
            ],
        }
        service._private_write(
            service._state_path(SITE["id"]), service._cipher.encrypt(json.dumps(state).encode())
        )
        await service.close()
    original_factory = service.create_scan_context

    async def no_user_window(*args, **kwargs):
        pytest.fail("Scanning must not open or navigate an unguarded authorization page")

    async def fixture_context(site):
        created = await original_factory(site)
        assert created.pages == []

        async def respond(route):
            requests.append((route.request.method, urlsplit(route.request.url).path))
            body = """<video src='/episode.mp4'></video><script>
              document.title=localStorage.getItem('fixture-token') || 'anonymous';
              fetch('/unguarded-write', {method:'POST'}).catch(()=>{});
            </script>"""
            await route.fulfill(status=200, content_type="text/html", body=body)

        await created.route("**/*", respond)
        return created

    service.open_session = no_user_window
    service.create_scan_context = fixture_context

    async def collect(asset):
        assets.append(asset)

    async def progress(event):
        events.append(event)

    try:
        result = await Scanner(service, Policy()).scan(
            SITE,
            {"url": PAGE, "limit": 1, "depth": 0, "kinds": ["video"]},
            collect,
            progress,
            lambda: False,
        )
        assert assets[0]["title"] == ("restored" if saved else "anonymous")
        assert result["status"] == "completed"
        assert ("POST", "/unguarded-write") not in requests
        assert not service._sessions and not service._browser.contexts
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_saved_cookies_without_user_window_keep_url_scope_and_expiry(tmp_path):
    import json
    import time

    from backend.browser import BrowserService

    class Policy:
        async def validate(self, url):
            return url

    service = BrowserService(tmp_path, Policy())
    await service.start()
    cookies = [
        {"name": "host", "value": "fixture", "domain": "lustflix.tv", "path": "/", "expires": -1},
        {
            "name": "domain",
            "value": "fixture",
            "domain": ".lustflix.tv",
            "path": "/",
            "expires": time.time() + 3600,
        },
        {
            "name": "subdomain",
            "value": "fixture",
            "domain": "cdn.lustflix.tv",
            "path": "/",
            "expires": -1,
        },
        {
            "name": "secure",
            "value": "fixture",
            "domain": ".lustflix.tv",
            "path": "/",
            "expires": -1,
            "secure": True,
        },
        {
            "name": "path",
            "value": "fixture",
            "domain": ".lustflix.tv",
            "path": "/media",
            "expires": -1,
        },
        {
            "name": "foreign",
            "value": "fixture",
            "domain": "other.invalid",
            "path": "/",
            "expires": -1,
        },
        {
            "name": "expired",
            "value": "fixture",
            "domain": ".lustflix.tv",
            "path": "/",
            "expires": time.time() - 1,
        },
        {
            "name": "deleted",
            "value": "fixture",
            "domain": ".lustflix.tv",
            "path": "/",
            "expires": 0,
        },
    ]
    service._private_write(
        service._state_path(SITE["id"]),
        service._cipher.encrypt(json.dumps({"cookies": cookies, "origins": []}).encode()),
    )
    await service.close()
    try:
        secure = await service.cookies_for(SITE["id"], "https://lustflix.tv/media/episode.mp4")
        assert {cookie["name"] for cookie in secure} == {"host", "domain", "secure", "path"}
        insecure = await service.cookies_for(SITE["id"], "http://cdn.lustflix.tv/mediabox")
        assert {cookie["name"] for cookie in insecure} == {"domain", "subdomain"}
        suffix = await service.cookies_for(SITE["id"], "https://lustflix.tv.evil.invalid/media")
        assert suffix == []
        assert await service.cookies_for("anonymous", ORIGIN) == []
        assert not service._sessions and not service._browser.contexts
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_series_scan_bfs_stays_on_observed_same_series_episode_links():
    html = """<video src='/episode.mp4'></video>
      <a href='/'>Home</a><a href='/#/watch/OTHER/1'>Other series</a>
      <a href='/#/detail/C03'>Series details</a>
      <a href='http://lustflix.tv/#/watch/C03/7'>Different scheme</a>
      <a href='https://lustflix.tv:444/#/watch/C03/8'>Different port</a>
      <a href='/#/watch/C03/6'>Episode 6</a>"""
    async with fixture_browser(html) as (scanner, _context, requests, _sockets):
        result, _, _ = await scan_fixture(scanner, depth=1, limit=20)
    assert result["pagesVisited"] == 2
    assert requests.count(("GET", "/")) == 1  # Same-document episode navigation.
    assert result["catalog"]["seriesKey"] == "C03"


@pytest.mark.asyncio
async def test_slow_watch_shell_with_image_waits_for_real_media_and_episode_controls():
    html = """<title>Slow SPA fixture</title><img src='/placeholder.jpg'>
      <div id='mount'></div><script>setTimeout(()=>{
        mount.innerHTML=`<video src='/episode-full.mp4'></video>
          <button title='Episodes' onclick="document.querySelector('#episodes').style.display='block'">Episodes</button>
          <section id='episodes' aria-label='Episodes' data-episode-count='6' style='display:none'>
            <div>Episode 5</div><div>Episode 6</div></section>`;
      },3000);</script>"""
    async with fixture_browser(html) as (scanner, _context, _requests, _sockets):
        result, assets, _ = await scan_fixture(scanner)
    assert [asset["sourceUrl"] for asset in assets] == [ORIGIN + "/episode-full.mp4"]
    assert result["catalog"]["expectedCount"] == 6
    assert [entry["ordinal"] for entry in result["catalog"]["entries"]] == [5, 6]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["", "blob:https://lustflix.tv/fixture"])
async def test_empty_or_blob_player_readiness_is_bounded_and_reports_partial_catalog(source):
    html = f"<img src='/placeholder.jpg'><video src='{source}'></video><button title='Episodes'>Episodes</button>"
    from backend.scanner import _DOM_MEDIA

    async with fixture_browser(html) as (_scanner, context, _requests, _sockets):
        page = await context.new_page()
        await page.goto(PAGE)
        contents = await page.evaluate(_DOM_MEDIA)
        started = time.monotonic()
        result = await Scanner._wait_for_watch_ready(
            page, PAGE, contents, [], lambda: False, max_seconds=0.03
        )
        assert time.monotonic() - started < 0.3
        assert result["linksTruncated"] and "catalog-limit" in result["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["cancelled", "route-changed", "gate"])
async def test_watch_readiness_stops_without_further_dom_access_on_interruptions(state):
    class Page:
        url = PAGE if state != "route-changed" else ORIGIN + "/#/watch/C03/6"

        async def wait_for_timeout(self, _ms):
            pytest.fail("Readiness should stop immediately")

    contents = {
        "assets": [],
        "episodeReady": False,
        "blocked": "Sign in" if state == "gate" else "",
    }
    result = await Scanner._wait_for_watch_ready(
        Page(), PAGE, contents, [], lambda: state == "cancelled"
    )
    assert result == contents


def test_generic_crawl_without_identified_series_keeps_existing_link_behavior():
    assert (
        Scanner._crawl_link(
            {"url": "/gallery", "text": "Gallery"}, "https://example.test/start", "example.test"
        )
        == "https://example.test/gallery"
    )
    assert (
        Scanner._crawl_link({"url": "/#/watch/OTHER/1", "text": "Other"}, ORIGIN, "lustflix.tv")
        == ORIGIN + "/#/watch/OTHER/1"
    )


def test_series_crawl_ignores_malformed_port_in_observed_link():
    assert (
        Scanner._crawl_link(
            {"url": "https://lustflix.tv:invalid/#/watch/C03/6", "text": "Episode 6"},
            PAGE,
            "lustflix.tv",
            series_start=PAGE,
        )
        is None
    )


@pytest.mark.asyncio
async def test_blocked_background_request_is_not_an_authentication_challenge():
    html = """<title>Public catalog</title><h1>Public catalog</h1>
      <script>fetch('/telemetry', {method:'POST'}).catch(()=>{});</script>"""
    async with fixture_browser(html) as (scanner, _context, requests, _sockets):
        result, _, _ = await scan_fixture(scanner)
    assert result["status"] != "needs-user"
    assert ("POST", "/telemetry") not in requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,outcome,hits",
    [
        ([502, 200], "completed", 2),
        (["connectionreset", 200], "completed", 2),
        ([429], "failed", 1),
        ([403], "needs-user", 1),
    ],
)
async def test_browser_navigation_recovers_only_retryable_gateway_errors(statuses, outcome, hits):
    html = "<main><video src='/episode.mp4'></video></main>"
    async with fixture_browser(html, navigation_status=statuses) as (
        scanner,
        _context,
        requests,
        _sockets,
    ):
        result, assets, _ = await scan_fixture(scanner)
    assert result["status"] == outcome
    assert requests.count(("GET", "/")) == hits
    assert bool(assets) == (outcome == "completed")
