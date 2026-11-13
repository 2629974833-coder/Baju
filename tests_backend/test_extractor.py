import asyncio
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import UnsupportedRequest


class Browser:
    def __init__(self):
        self.calls = []

    async def cookies_for(self, site_id, url):
        self.calls.append((site_id, url))
        return [
            {"name": "session", "value": "browser-only", "domain": "127.0.0.1", "path": "/"},
            {"name": "wrong", "value": "no", "domain": "example.org", "path": "/"},
            {"name": "restricted", "value": "no", "domain": "127.0.0.1", "path": "/account"},
        ]


@pytest.fixture
def origin():
    routes, requests = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(
                (self.path, {name.lower(): value for name, value in self.headers.items()})
            )
            status, headers, body = routes.get(self.path, (404, {}, b"not found"))
            if callable(body):
                body = body()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{server.server_port}", routes=routes, requests=requests
    )
    server.shutdown()
    server.server_close()
    thread.join()


def service():
    from backend.extractor import ExtractorService
    from backend.network import NetworkPolicy

    return ExtractorService(
        Browser(), NetworkPolicy(allow_private_hosts={"127.0.0.1", "localhost"})
    )


def page(origin, content, path="/page"):
    origin.routes[path] = (200, {"Content-Type": "text/html; charset=utf-8"}, content.encode())
    return origin.url + path


@pytest.fixture
def captured_outbound(monkeypatch):
    import backend.extractor as extractor

    requests = []

    def respond(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"{}")

    original = httpx.AsyncClient

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(respond))

    class Policy:
        async def validate(self, url):
            return url

    monkeypatch.setattr(
        extractor, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": client})
    )
    return requests, Policy()


@pytest.mark.parametrize("source", ["https://lustflix.tv/", "https://www.lustflix.tv/"])
@pytest.mark.parametrize("target", ["https://lustflix.tv/unlock", "https://other.invalid/unlock"])
async def test_lustflix_bridge_blocks_post_before_any_outbound_io(
    captured_outbound, source, target
):
    from backend.extractor import _Bridge

    requests, policy = captured_outbound
    bridge = _Bridge(Browser(), policy, {"id": "fixture"}, source, None, lambda: False, None)
    with pytest.raises(UnsupportedRequest, match="只读"):
        await bridge.request(Request(target, data=b"{}"), {})
    assert requests == [] and bridge.needs_user and bridge.read_only_blocked


async def test_lustflix_site_origin_retains_guard_for_external_page(captured_outbound):
    from backend.extractor import _Bridge

    requests, policy = captured_outbound
    bridge = _Bridge(
        Browser(),
        policy,
        {"id": "fixture", "url": "https://lustflix.tv"},
        "https://other.invalid/embed",
        None,
        lambda: False,
        None,
    )
    with pytest.raises(UnsupportedRequest, match="只读"):
        await bridge.request(Request("https://other.invalid/unlock", data=b"{}"), {})
    assert requests == [] and bridge.needs_user


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_lustflix_bridge_keeps_read_requests(captured_outbound, method):
    from backend.extractor import _Bridge

    requests, policy = captured_outbound
    bridge = _Bridge(
        Browser(), policy, {"id": "fixture"}, "https://lustflix.tv/", None, lambda: False, None
    )
    response = await bridge.request(Request("https://cdn.invalid/master.m3u8", method=method), {})
    assert response.status == 200
    assert requests == [(method, "https://cdn.invalid/master.m3u8")]
    assert not bridge.needs_user


async def test_generic_bridge_keeps_existing_bounded_json_post(captured_outbound):
    from backend.extractor import _Bridge

    requests, policy = captured_outbound
    bridge = _Bridge(
        Browser(), policy, {"id": "fixture"}, "https://generic.invalid/", None, lambda: False, None
    )
    response = await bridge.request(Request("https://generic.invalid/data", data=b"{}"), {})
    assert response.status == 200 and requests == [("POST", "https://generic.invalid/data")]


async def test_read_only_extraction_keeps_prior_get_candidate_and_explains_gate(
    captured_outbound, monkeypatch
):
    import backend.extractor as extractor

    requests, policy = captured_outbound

    def extract_with_followup(ie, url):
        ie._download_webpage(url, "fixture")

        def entries():
            yield {
                "id": "fixture",
                "title": "Episode 1",
                "url": "https://cdn.invalid/episode-1.mp4",
            }
            ie._download_json("https://other.invalid/unlock", "fixture", data=b"{}")

        return {"_type": "playlist", "entries": entries()}

    monkeypatch.setattr(extractor._GenericIE, "_real_extract", extract_with_followup)
    result = await extractor.ExtractorService(Browser(), policy).extract(
        {"id": "fixture"}, "https://lustflix.tv/#/watch/C03/1"
    )
    assert result["status"] == "needs-user" and result["reasonCode"] == "read-only-blocked"
    assert "只读" in result["message"]
    assert [asset["sourceUrl"] for asset in result["assets"]] == [
        "https://cdn.invalid/episode-1.mp4"
    ]
    assert requests and all(method == "GET" for method, _url in requests)


async def test_installed_generic_extracts_html5_formats_through_real_http(origin):
    url = page(
        origin,
        '<title>Episode one</title><video><source src="/720.mp4" '
        'type="video/mp4" label="720p"><source src="/1080.mp4" '
        'type="video/mp4" label="1080p"></video>',
    )
    result = await service().extract({"id": "site_one"}, url)
    assert result["status"] == "resolved"
    assert result["extractor"] == "yt-dlp:generic"
    assert len(result["assets"]) == 1
    asset = result["assets"][0]
    assert asset["sourceUrl"] == origin.url + "/1080.mp4"
    assert asset["engine"] == "http" and asset["kind"] == "video"
    assert asset["pageUrl"] == url
    assert origin.requests[0][1]["cookie"] == "session=browser-only"
    assert "Cookie" not in asset["_headers"]


async def test_json_ld_metadata_and_rendered_html_reuse(origin):
    content = (
        '<title>Shell</title><script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "VideoObject",
                "name": "Episode two",
                "contentUrl": origin.url + "/episode.mp4?signature=private",
                "duration": "PT42S",
                "thumbnailUrl": origin.url + "/poster.jpg",
            }
        )
        + "</script>"
    )
    result = await service().extract({"id": "site_one"}, origin.url + "/page", html=content)
    assert result["status"] == "resolved"
    assert result["assets"][0]["title"] == "Episode two"
    assert result["assets"][0]["duration"] == 42
    assert result["assets"][0]["thumbnail"] == origin.url + "/poster.jpg"
    assert origin.requests == []


async def test_hls_and_dash_candidates_keep_manifest_identity(origin):
    url = page(
        origin,
        '<title>Streams</title><video src="/master.m3u8"></video>'
        '<video src="/manifest.mpd"></video>',
    )
    origin.routes["/master.m3u8"] = (
        200,
        {"Content-Type": "application/vnd.apple.mpegurl"},
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=640x360\nlow.m3u8\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\nhigh.m3u8\n",
    )
    result = await service().extract({"id": "site_one"}, url)
    assert result["status"] == "resolved"
    assert {a["sourceUrl"] for a in result["assets"]} == {
        origin.url + "/master.m3u8",
        origin.url + "/manifest.mpd",
    }
    assert all(a["engine"] == "yt-dlp" for a in result["assets"])
    assert (
        next(a for a in result["assets"] if a["sourceUrl"].endswith("mpd"))["_protocol"] == "dash"
    )


async def test_redirects_revalidate_destinations_and_strip_cross_origin_authorization(origin):
    destination = origin.url.replace("127.0.0.1", "localhost") + "/target"
    origin.routes["/page"] = (302, {"Location": destination, "Set-Cookie": "leak=bad"}, b"")
    page(origin, '<video src="/movie.mp4"></video>', "/target")
    result = await service().extract(
        {"id": "site_one"},
        origin.url + "/page",
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "injected=bad",
            "User-Agent": "Fixture/1.0",
        },
    )
    assert result["status"] == "resolved"
    assert origin.requests[0][1]["authorization"] == "Bearer secret"
    assert all(
        "authorization" not in headers and "cookie" not in headers
        for path, headers in origin.requests
        if path == "/target"
    )
    assert all("authorization" not in {k.lower() for k in a["_headers"]} for a in result["assets"])


async def test_private_redirect_is_rejected_before_request_and_errors_are_sanitized(origin):
    origin.routes["/page"] = (302, {"Location": "http://169.254.169.254/token?secret=hidden"}, b"")
    origin.routes["/page?secret=hidden"] = origin.routes["/page"]
    result = await service().extract({"id": "site_one"}, origin.url + "/page?secret=hidden")
    assert result["status"] == "unsupported"
    assert "hidden" not in result["message"] and "169.254" not in result["message"]
    assert all(path.startswith("/page") for path, _ in origin.requests)


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_access_gate_is_explicit_without_raw_error(origin, status):
    origin.routes["/page"] = (status, {}, b"private")
    result = await service().extract({"id": "site_one"}, origin.url + "/page")
    assert result["status"] == "needs-user"
    assert result["assets"] == []


async def test_playlist_limit_and_unsupported_provider_are_explicit(origin):
    videos = "".join(f'<video src="/{n}.mp4"></video>' for n in range(6))
    result = await service().extract({"id": "site_one"}, page(origin, videos), max_entries=2)
    assert result["status"] == "limited"
    assert len(result["assets"]) == 2
    assert len(result["entries"]) <= 2
    url = page(
        origin,
        '<script>var player = { file: "https://www.youtube.com/watch?v=abcdefghijk" };</script>',
    )
    result = await service().extract({"id": "site_one"}, url)
    assert result["status"] == "unsupported"
    assert result["assets"] == []
    assert all("youtube" not in path for path, _ in origin.requests)


async def test_response_budget_is_enforced(origin):
    origin.routes["/page"] = (200, {"Content-Type": "text/html"}, b"x" * (4 * 1024**2 + 1))
    result = await service().extract({"id": "site_one"}, origin.url + "/page")
    assert result["status"] == "limited"
    assert result["assets"] == []


async def test_cancel_stops_worker_before_followup_io(origin):
    cancelled = threading.Event()

    def first_body():
        cancelled.set()
        return b'<video src="/playlist.m3u8"></video>'

    origin.routes["/page"] = (200, {"Content-Type": "text/html"}, first_body)
    result = await service().extract(
        {"id": "site_one"}, origin.url + "/page", cancelled=cancelled.is_set
    )
    await asyncio.sleep(0.1)
    assert result["assets"] == []
    assert [path for path, _ in origin.requests] == ["/page"]


async def test_pre_cancelled_extraction_has_no_io(origin):
    result = await service().extract(
        {"id": "site_one"}, origin.url + "/page", cancelled=lambda: True
    )
    assert result["assets"] == []
    assert origin.requests == []


async def test_rendered_spa_fragment_is_not_refetched(origin):
    url = origin.url + "/page#/episode/2"
    result = await service().extract(
        {"id": "site_one"}, url, html='<video src="/movie.mp4"></video>'
    )
    assert result["status"] == "resolved"
    assert result["assets"][0]["pageUrl"] == url
    assert origin.requests == []


async def test_direct_media_response_is_only_sniffed(origin):
    origin.routes["/movie.mp4"] = (200, {"Content-Type": "video/mp4"}, b"x" * (5 * 1024**2))
    result = await service().extract({"id": "site_one"}, origin.url + "/movie.mp4")
    assert result["status"] == "resolved"
    assert result["assets"][0]["sourceUrl"] == origin.url + "/movie.mp4"


async def test_rss_entries_resolve_to_their_own_page_identity(origin):
    origin.routes["/feed"] = (
        200,
        {"Content-Type": "application/rss+xml"},
        (
            "<rss><channel><title>Series</title><item><title>Episode 1</title><link>"
            + origin.url
            + "/episode-1</link></item><item><title>Episode 2</title><link>"
            + origin.url
            + "/episode-2</link></item></channel></rss>"
        ).encode(),
    )
    page(origin, '<video src="/one.mp4"></video>', "/episode-1")
    page(origin, '<video src="/two.mp4"></video>', "/episode-2")
    result = await service().extract({"id": "site_one"}, origin.url + "/feed")
    assert result["status"] == "resolved"
    assert {a["pageUrl"] for a in result["assets"]} == {
        origin.url + "/episode-1",
        origin.url + "/episode-2",
    }
    assert {entry["pageUrl"] for entry in result["entries"]} == {
        origin.url + "/episode-1",
        origin.url + "/episode-2",
    }


async def test_sole_request_handler_and_disabled_external_runtime(origin, monkeypatch):
    import subprocess

    from yt_dlp.networking import RequestDirector

    handlers = []
    original_send = RequestDirector.send

    def checked_send(director, request):
        handlers.append(set(director.handlers))
        return original_send(director, request)

    def no_process(*_args, **_kwargs):
        raise AssertionError("metadata extraction launched an external process")

    monkeypatch.setattr(RequestDirector, "send", checked_send)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    result = await service().extract(
        {"id": "site_one"}, page(origin, '<video src="/movie.mp4"></video>')
    )
    assert result["status"] == "resolved"
    assert handlers and all(value == {"Framekeep"} for value in handlers)


async def test_dynamic_and_drm_mpd_do_not_expand_segments(origin):
    for marker in ('type="dynamic"', 'type="static"'):
        protection = (
            '<ContentProtection schemeIdUri="urn:uuid:fixture"/>' if "static" in marker else ""
        )
        origin.routes["/manifest.mpd"] = (
            200,
            {"Content-Type": "application/dash+xml"},
            (
                f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" {marker}><Period>'
                f'<AdaptationSet>{protection}<Representation id="v" mimeType="video/mp4">'
                '<SegmentTemplate media="seg-$Time$.m4s"><SegmentTimeline>'
                '<S t="0" d="1" r="999999999"/></SegmentTimeline></SegmentTemplate>'
                "</Representation></AdaptationSet></Period></MPD>"
            ).encode(),
        )
        result = await service().extract({"id": "site_one"}, origin.url + "/manifest.mpd")
        assert result["status"] == "unsupported"
        assert result["assets"] == []
    assert [path for path, _ in origin.requests] == ["/manifest.mpd", "/manifest.mpd"]


async def test_request_budget_counts_every_redirect(origin, monkeypatch):
    import backend.extractor as extractor

    monkeypatch.setattr(extractor, "_REQUEST_LIMIT", 3)
    for number in range(5):
        origin.routes[f"/step-{number}"] = (302, {"Location": f"/step-{number + 1}"}, b"")
    result = await service().extract({"id": "site_one"}, origin.url + "/step-0")
    assert result["status"] == "limited"
    assert [path for path, _ in origin.requests] == ["/step-0", "/step-1", "/step-2"]


async def test_user_plugin_is_not_loaded(origin, monkeypatch, tmp_path):
    from yt_dlp.globals import all_plugins_loaded, plugin_dirs

    package = tmp_path / "yt_dlp_plugins" / "extractor"
    package.mkdir(parents=True)
    marker = tmp_path / "plugin-executed"
    (package / "unexpected.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(all_plugins_loaded, "value", False)
    monkeypatch.setattr(plugin_dirs, "value", ["default"])
    result = await service().extract(
        {"id": "site_one"}, page(origin, '<video src="/movie.mp4"></video>')
    )
    assert result["status"] == "resolved"
    assert not marker.exists()


async def test_calling_task_cancellation_stops_inflight_request(origin):
    release = threading.Event()

    def blocked_body():
        release.wait(2)
        return b'<video src="/playlist.m3u8"></video>'

    origin.routes["/page"] = (200, {"Content-Type": "text/html"}, blocked_body)
    task = asyncio.create_task(service().extract({"id": "site_one"}, origin.url + "/page"))
    try:
        async with asyncio.timeout(2):
            while not origin.requests:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
    finally:
        release.set()
    await asyncio.sleep(0.1)
    assert [path for path, _ in origin.requests] == ["/page"]


async def test_compressed_response_cannot_expand_outside_byte_budget(origin):
    origin.routes["/page"] = (
        200,
        {"Content-Type": "text/html", "Content-Encoding": "gzip"},
        gzip.compress(b'<video src="/movie.mp4"></video>'),
    )
    result = await service().extract({"id": "site_one"}, origin.url + "/page")
    assert result["status"] == "unsupported"
    assert result["assets"] == []
