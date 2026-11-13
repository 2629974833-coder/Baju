"""Real Chromium tests against a controlled, local HTTP fixture only."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import importlib
import io
import threading
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from PIL import Image


class FixturePolicy:
    def __init__(self, origin):
        self.origin = origin
        self.checked = []

    async def validate(self, url):
        self.checked.append(url)
        if not url.startswith(self.origin + "/"):
            raise ValueError("Fixture permits its own origin only")
        return url


@pytest.fixture
def fixture_site():
    image = io.BytesIO()
    Image.new("RGB", (640, 480), "navy").save(image, "PNG")
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            hits.append(self.path)
            path = urlsplit(self.path).path
            authorized = "fixture_auth=private-fixture-value" in self.headers.get("Cookie", "")
            status, mime, cookie = 200, "text/html", None
            if path == "/login":
                cookie = "fixture_auth=private-fixture-value; HttpOnly; SameSite=Lax; Path=/"
                body = b"<title>Signed in fixture</title><script>localStorage.setItem('fixture','remembered')</script><a href='/gallery'>Gallery</a>"
            elif path == "/gallery" and not authorized:
                body = b"<title>Login required</title><form><input type=password><button>Sign in</button></form>"
            elif path == "/gallery":
                body = b"""<title>Private Gallery</title>
                  <img src='/private.png' width=640 height=480>
                  <img src='/private.png' width=640 height=480>
                  <img src='/thumb-small.png' width=16 height=16>
                  <img src='/logo.png' width=640 height=480>
                  <img srcset='/srcset.png 1x, /srcset-large.png 2x' width=640 height=480>
                  <div style="background-image: url('/background.png');width:640px;height:480px"></div>
                  <video width=640 height=480 poster='/poster.png'><source src='/movie.mp4' type='video/mp4'></video>
                  <script>fetch('/stream.m3u8');fetch('/segment.ts');</script>
                  <a href='/?d=album#photos'>Album</a>
                  <a href='/outside'>Other page</a>
                  <a href='https://outside.invalid/'>External</a>"""
            elif path == "/" and "d=album" in self.path:
                body = b"<title>Album</title><img src='/album.png' width=640 height=480><a href='/?ep=next'>Next</a>"
            elif path == "/" and "ep=next" in self.path:
                body = b"<img src='/next.png' width=640 height=480>"
            elif path == "/outside":
                body = b"<img src='/outside.png' width=640 height=480>"
            elif path == "/lazy":
                body = b"""<title>Lazy fixture</title><div style='height:3000px'></div>
                <script>setTimeout(() => { const img = document.createElement('img');img.src='/lazy.png'; img.width=640; img.height=480;document.body.append(img);}, 1400)</script>"""
            elif path == "/manual":
                body = b"""<title>Manual playback fixture</title><button id='play' style='width:200px;height:60px' onclick="fetch('/manual-stream.m3u8')">Play fixture</button>"""
            elif path == "/drm":
                body = b"""<title>Encrypted fixture</title><video width=640 height=480><source src='/movie.mp4' type='video/mp4'></video>
                <script>document.querySelector('video').dispatchEvent(new Event('encrypted', {bubbles:true}));</script>"""
            elif path == "/unavailable":
                body = b"<title>Unavailable media</title><img src='/denied.png' width=640 height=480><img src='/missing.png' width=640 height=480>"
            elif path == "/denied.png":
                status, body = 403, b"permission required"
            elif path == "/missing.png":
                status, body = 404, b"not found"
            elif path.endswith(".png"):
                if path == "/private.png" and not authorized:
                    status, body = 401, b"login required"
                else:
                    mime, body = "image/png", image.getvalue()
            elif path.endswith(".m3u8"):
                mime, body = (
                    "application/vnd.apple.mpegurl",
                    b"#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST",
                )
            elif path.endswith((".mp4", ".ts")):
                mime, body = "video/mp4", b"controlled fixture bytes"
            else:
                body = b"<title>Fixture</title><a href='/login'>Sign in</a>"
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    yield (
        {"id": "fixture", "name": "Fixture", "domain": "127.0.0.1", "url": origin + "/"},
        FixturePolicy(origin),
        hits,
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def browser_class():
    assert (Path(__file__).parents[1] / "backend/browser.py").exists(), (
        "BrowserService has not been implemented"
    )
    return importlib.import_module("backend.browser").BrowserService


def scanner_class():
    assert (Path(__file__).parents[1] / "backend/scanner.py").exists(), (
        "Scanner has not been implemented"
    )
    return importlib.import_module("backend.scanner").Scanner


@pytest.mark.asyncio
async def test_isolated_encrypted_sessions_reload_and_disconnect(tmp_path, fixture_site):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    await service.start()
    try:
        info = await service.open_session(site)
        assert info["open"] and not info["verified"] and info["width"] == 1280
        await service.interact(site["id"], {"type": "navigate", "url": site["url"] + "login"})
        assert (await service.cookies_for(site["id"], site["url"]))[0][
            "value"
        ] == "private-fixture-value"
        saved = await service.save_session(site["id"])
        assert saved["saved"] and not saved["verified"]
        assert b"private-fixture-value" not in b"".join(
            p.read_bytes() for p in (tmp_path / "sessions").iterdir()
        )
        assert (tmp_path / "sessions").stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / "sessions").iterdir())
        other = {**site, "id": "other"}
        await service.open_session(other)
        assert not await service.cookies_for("other", site["url"])
        assert (await service.screenshot("fixture")).startswith(b"\xff\xd8")
    finally:
        await service.close()
    service = browser_class()(tmp_path, policy)
    await service.start()
    try:
        await service.open_session(site)
        assert (await service.cookies_for("fixture", site["url"]))[0][
            "value"
        ] == "private-fixture-value"
        page = (await service.context_for("fixture")).pages[0]
        assert await page.evaluate("localStorage.getItem('fixture')") == "remembered"
        await service.disconnect("fixture")
        assert not (await service.session_info("fixture"))["saved"]
        await service.open_session(site)
        assert not await service.cookies_for("fixture", site["url"])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_session_policy_and_popup_tabs(tmp_path, fixture_site):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    await service.start()
    try:
        await service.open_session(site)
        page = (await service.context_for("fixture")).pages[0]
        await page.evaluate("window.open('/gallery', '_blank')")
        await page.wait_for_timeout(150)
        info = await service.session_info("fixture")
        assert len(info["tabs"]) == 2
        selected = await service.interact(
            "fixture", {"type": "tab", "tabId": info["tabs"][1]["id"]}
        )
        assert selected["url"].endswith("/gallery")
        with pytest.raises(ValueError):
            await service.interact(
                "fixture", {"type": "navigate", "url": "http://outside.invalid/"}
            )
        with pytest.raises(ValueError):
            await service.interact("fixture", {"type": "unknown"})
        assert any(url.endswith("/gallery") for url in policy.checked)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scan_authenticated_media_types_dedup_and_depth(tmp_path, fixture_site):
    site, policy, hits = fixture_site
    service = browser_class()(tmp_path, policy)
    await service.start()
    try:
        await service.open_session(site)
        await service.interact("fixture", {"type": "navigate", "url": site["url"] + "login"})
        assets, progress = [], []

        async def asset(value):
            assets.append(value)

        async def report(value):
            progress.append(value)

        await scanner_class()(service, policy).scan(
            site,
            {"url": site["url"] + "gallery", "depth": 1, "limit": 2, "kinds": ["image", "video"]},
            asset,
            report,
            lambda: False,
        )
        urls = [item["sourceUrl"] for item in assets]
        assert len(urls) == len(set(urls))
        assert site["url"] + "private.png" in urls
        assert site["url"] + "album.png" in urls
        assert site["url"] + "next.png" not in urls
        assert site["url"] + "outside.png" not in urls
        assert not any(url.endswith(("segment.ts", "logo.png", "thumb-small.png")) for url in urls)
        assert (
            next(item for item in assets if item["sourceUrl"].endswith("stream.m3u8"))["engine"]
            == "yt-dlp"
        )
        assert (
            next(item for item in assets if item["sourceUrl"].endswith("movie.mp4"))["kind"]
            == "video"
        )
        assert any(item["sourceUrl"].endswith("background.png") for item in assets)
        assert any(item["sourceUrl"].endswith("srcset-large.png") for item in assets)
        assert any(item["pageUrl"].endswith("?d=album#photos") for item in assets)
        assert all(
            not item["locked"] and "Cookie" not in item.get("_headers", {}) for item in assets
        )
        assert progress[-1]["pagesVisited"] == 2
        assert progress[-1]["limited"] is False
        assert progress[-1]["maxAssets"] == 250
        assert len((await service.context_for("fixture")).pages) == 1
        assert (await service.session_info("fixture"))["url"].endswith("/login")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scan_reports_login_without_clicking_and_honors_cancellation(tmp_path, fixture_site):
    site, policy, hits = fixture_site
    service = browser_class()(tmp_path, policy)
    await service.start()
    try:
        progress, assets = [], []

        async def report(value):
            progress.append(value)

        async def asset(value):
            assets.append(value)

        scanner = scanner_class()(service, policy)
        await scanner.scan(
            site,
            {"url": site["url"] + "gallery", "depth": 2, "limit": 10, "kinds": ["image"]},
            asset,
            report,
            lambda: False,
        )
        assert not assets
        assert any(item.get("status") == "needs-user" for item in progress)
        assert "/login" not in hits
        await scanner.scan(
            site,
            {"url": site["url"] + "gallery", "depth": 2, "limit": 10, "kinds": ["image"]},
            asset,
            report,
            lambda: True,
        )
        assert progress[-1]["status"] == "cancelled"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scan_excludes_unavailable_media_and_drm(tmp_path, fixture_site):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    await service.start()
    try:
        progress, assets = [], []

        async def report(value):
            progress.append(value)

        async def asset(value):
            assets.append(value)

        scanner = scanner_class()(service, policy)
        await scanner.scan(
            site, {"url": site["url"] + "unavailable", "limit": 1}, asset, report, lambda: False
        )
        assert not assets, "HTTP-denied and missing media must not appear as downloadable"
        await scanner.scan(
            site, {"url": site["url"] + "drm", "limit": 1}, asset, report, lambda: False
        )
        assert not assets
        assert any(item.get("status") == "unsupported-drm" for item in progress)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scan_asset_limit_and_kind_filter(tmp_path, fixture_site):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    await service.start()
    try:
        await service.open_session(site)
        await service.interact("fixture", {"type": "navigate", "url": site["url"] + "login"})
        assets = []

        async def report(value):
            pass

        async def asset(value):
            assets.append(value)

        result = await scanner_class()(service, policy).scan(
            site,
            {
                "url": site["url"] + "gallery",
                "depth": 5,
                "limit": 100,
                "maxAssets": 1,
                "kinds": ["video"],
            },
            asset,
            report,
            lambda: False,
        )
        assert len(assets) == 1 and assets[0]["kind"] == "video"
        assert result["pagesVisited"] == 1
        assert result["status"] == "completed"
        assert result["limited"] is True and result["maxAssets"] == 1
        assert "已达到本次素材数量上限" in result["message"]
        assert "不代表全站结果" in result["message"]
        assert "缩小范围或按类型扫描" in result["message"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scan_waits_for_late_spa_media(tmp_path, fixture_site):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    assets = []

    async def report(value):
        pass

    async def asset(value):
        assets.append(value)

    try:
        await scanner_class()(service, policy).scan(
            site, {"url": site["url"] + "lazy", "limit": 1}, asset, report, lambda: False
        )
        assert any(item["sourceUrl"].endswith("lazy.png") for item in assets)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scan_reuses_manually_observed_manifest_for_exact_page_only(tmp_path, fixture_site):
    site, policy, hits = fixture_site
    service = browser_class()(tmp_path, policy)
    assets = []

    async def report(value):
        pass

    async def asset(value):
        assets.append(value)

    try:
        await service.open_session(site)
        manual_url = site["url"] + "manual?episode=one#player"
        await service.interact(site["id"], {"type": "navigate", "url": manual_url})
        page = (await service.context_for(site["id"])).pages[0]
        async with page.expect_response("**/manual-stream.m3u8"):
            await service.interact(site["id"], {"type": "click", "x": 60, "y": 30})
        scanner = scanner_class()(service, policy)
        await scanner.scan(
            site,
            {"url": site["url"] + "manual?episode=two#player", "limit": 1},
            asset,
            report,
            lambda: False,
        )
        assert not assets, "Observed media from another detail page must not leak into this scan"
        await scanner.scan(site, {"url": manual_url, "limit": 1}, asset, report, lambda: False)
        assert len(assets) == 1 and assets[0]["sourceUrl"].endswith("manual-stream.m3u8")
        assert assets[0]["engine"] == "yt-dlp" and assets[0]["pageUrl"] == manual_url
        assert hits.count("/manual-stream.m3u8") == 1, "Scanning must not replay or click the page"
        observations = await service.observed_media_for(site["id"], manual_url)
        assert len(observations) == 1 and set(observations[0]) == {"url", "mime", "pageUrl"}
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True])
async def test_save_waits_for_pending_login_and_timeout_keeps_previous_state(
    tmp_path, fixture_site, monkeypatch, expires
):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    await service.open_session(site)
    await service.save_session(site["id"])
    state_file = next((tmp_path / "sessions").glob("*.enc"))
    previous = state_file.read_bytes()
    context = await service.context_for(site["id"])
    page = context.pages[0]

    async def delayed_login(route):
        entered.set()
        await release.wait()
        try:
            await route.fulfill(
                status=303,
                headers={
                    "Location": "/",
                    "Set-Cookie": "fixture_pending=completed; Path=/; HttpOnly",
                },
                body="",
            )
        finally:
            finished.set()

    await context.route("**/slow-login", delayed_login)
    await page.set_content(
        "<form method='post' action='/slow-login'><button>Submit fixture</button></form>"
    )
    await page.locator("button").focus()
    await service.interact(site["id"], {"type": "key", "key": "Enter"})
    await entered.wait()
    try:
        if expires:
            monkeypatch.setattr("backend.browser._SESSION_SAVE_TIMEOUT", 0.05, raising=False)
            with pytest.raises(ValueError, match="未保存"):
                await service.save_session(site["id"])
            assert state_file.read_bytes() == previous
        else:
            saving = asyncio.create_task(service.save_session(site["id"]))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(saving), 0.05)
            release.set()
            result = await saving
            assert result["saved"] and result["verified"] is False
            await service.close()
            await service.open_session(site)
            assert any(
                cookie["name"] == "fixture_pending"
                for cookie in await service.cookies_for(site["id"], site["url"])
            )
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        await service.close()


@pytest.mark.asyncio
async def test_closed_scan_page_does_not_block_saving_after_request_gc(
    tmp_path, fixture_site, monkeypatch
):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    entered, release = asyncio.Event(), asyncio.Event()
    request_refs = []
    await service.open_session(site)
    context = await service.context_for(site["id"])

    def observe(request):
        if request.url.endswith("/held-scan-request"):
            request_refs.append(weakref.ref(request))

    async def hold(route):
        entered.set()
        await release.wait()
        with contextlib.suppress(Exception):
            await route.fulfill(status=200, body="fixture")

    context.on("request", observe)
    await context.route("**/held-scan-request", hold)
    temporary_page = await context.new_page()
    try:
        await temporary_page.goto(site["url"])
        await temporary_page.evaluate("() => { fetch('/held-scan-request').catch(() => {}); }")
        await entered.wait()
        gc.collect()
        assert request_refs[0]() is not None, "Playwright keeps the in-flight wrapper alive"
        closed_frame = temporary_page.main_frame
        await temporary_page.close()
        session = service._sessions[site["id"]]
        assert all(
            closed_frame not in mapping
            for mapping in (
                session.document_markers,
                session.document_versions,
                session.navigation_boundaries,
                session.document_tasks,
            )
        )
        monkeypatch.setattr("backend.browser._SESSION_SAVE_TIMEOUT", 1.5)
        result = await service.save_session(site["id"])
        assert result["saved"] is True and result["verified"] is False
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,navigation,saves",
    [("GET", "document", True), ("GET", "hash", False), ("POST", "document", False)],
)
async def test_document_transition_only_retires_old_read_requests(
    tmp_path, fixture_site, monkeypatch, method, navigation, saves
):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    entered, release = asyncio.Event(), asyncio.Event()
    await service.open_session(site)
    context = await service.context_for(site["id"])
    page = context.pages[0]

    async def hold(route):
        entered.set()
        await release.wait()
        with contextlib.suppress(Exception):
            await route.fulfill(status=200, body="controlled fixture")

    await context.route("**/held-document-request", hold)
    try:
        await page.evaluate(
            "method => { fetch('/held-document-request', {method}).catch(() => {}); }", method
        )
        await entered.wait()
        destination = site["url"] + ("poster.png" if navigation == "document" else "#same-document")
        await service.interact(site["id"], {"type": "navigate", "url": destination})
        monkeypatch.setattr("backend.browser._SESSION_SAVE_TIMEOUT", 1.5)
        if saves:
            result = await service.save_session(site["id"])
            assert result["saved"] and result["verified"] is False
        else:
            with pytest.raises(ValueError, match="网络等待"):
                await service.save_session(site["id"])
            assert not list((tmp_path / "sessions").glob("*.enc"))
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_save_timeout_identifies_storage_capture_phase(tmp_path, fixture_site, monkeypatch):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    entered, release = asyncio.Event(), asyncio.Event()
    await service.open_session(site)
    context = await service.context_for(site["id"])

    async def blocked_storage(**kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(context, "storage_state", blocked_storage)
    monkeypatch.setattr("backend.browser._SESSION_SAVE_TIMEOUT", 0.8)
    try:
        with pytest.raises(ValueError, match="存储导出"):
            await service.save_session(site["id"])
        assert entered.is_set()
        assert not list((tmp_path / "sessions").glob("*.enc"))
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_navigation_preserves_new_document_read_request(tmp_path, fixture_site):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    old_entered, new_entered = asyncio.Event(), asyncio.Event()
    old_release, new_release = asyncio.Event(), asyncio.Event()
    await service.open_session(site)
    context = await service.context_for(site["id"])
    page = context.pages[0]

    async def hold_old(route):
        old_entered.set()
        await old_release.wait()
        with contextlib.suppress(Exception):
            await route.fulfill(status=200, body="old fixture")

    async def hold_new(route):
        new_entered.set()
        await new_release.wait()
        with contextlib.suppress(Exception):
            await route.fulfill(status=200, body="new fixture")

    async def new_document(route):
        await route.fulfill(
            status=200,
            content_type="text/html",
            body="<script>fetch('/new-document-read').catch(() => {})</script>",
        )

    await context.route("**/old-document-read", hold_old)
    await context.route("**/new-document-read", hold_new)
    await context.route("**/new-document", new_document)
    try:
        await page.evaluate("() => { fetch('/old-document-read').catch(() => {}); }")
        await old_entered.wait()
        await service.interact(
            site["id"], {"type": "navigate", "url": site["url"] + "new-document"}
        )
        await new_entered.wait()
        saving = asyncio.create_task(service.save_session(site["id"]))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(saving), 0.05)
        new_release.set()
        result = await asyncio.wait_for(saving, 3)
        assert result["saved"] and result["verified"] is False
        assert not old_release.is_set()
    finally:
        old_release.set()
        new_release.set()
        await service.close()


@pytest.mark.asyncio
async def test_same_document_sync_does_not_consume_pending_navigation_boundary(
    tmp_path, fixture_site
):
    site, policy, _ = fixture_site
    service = browser_class()(tmp_path, policy)
    navigation_entered, navigation_release = asyncio.Event(), asyncio.Event()
    await service.open_session(site)
    context = await service.context_for(site["id"])
    page = context.pages[0]
    session = service._sessions[site["id"]]

    async def delayed_navigation(route):
        navigation_entered.set()
        await navigation_release.wait()
        with contextlib.suppress(Exception):
            await route.fulfill(
                status=200, content_type="text/html", body="<title>New document</title>"
            )

    await context.route("**/delayed-document", delayed_navigation)
    navigating = asyncio.create_task(page.goto(site["url"] + "delayed-document"))
    try:
        await navigation_entered.wait()
        boundary = session.navigation_boundaries[page.main_frame]
        # A same-document callback while a different document request is still pending.
        session.frame_navigated(page.main_frame)
        await session.ensure_document(page.main_frame)
        retained = session.navigation_boundaries.get(page.main_frame) == boundary
        assert retained
        navigation_release.set()
        await navigating
        await session.ensure_document(page.main_frame)
        assert page.main_frame not in session.navigation_boundaries
    finally:
        navigation_release.set()
        with contextlib.suppress(Exception):
            await navigating
        await service.close()
