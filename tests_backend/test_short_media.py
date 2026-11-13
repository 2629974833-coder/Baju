import hashlib
import json
import shutil
import subprocess
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

import backend.extractor as extraction
from backend.browser import BrowserService
from backend.downloader import Downloader
from backend.scanner import Scanner
from backend.short_media import short_target
from backend.site_adapters import refresh_identity_matches, stable_asset_identity

TT = "https://www.tiktok.com/@garden/video/123"
DY = "https://www.douyin.com/video/123"
IG = "https://www.instagram.com/reel/B7/"
MEDIA = "https://cdn.example.test"


class Policy:
    async def validate(self, url):
        if urlsplit(url).hostname not in {
            "www.tiktok.com", "vm.tiktok.com", "www.douyin.com", "m.douyin.com", "www.instagram.com", "cdn.example.test", "media.tiktok.com", "aweme.snssdk.com"
        }:
            raise ValueError("out of scope")
        return url


class Browser:
    async def cookies_for(self, site_id, url):
        if urlsplit(url).hostname == "www.instagram.com":
            return [{"name": "sessionid", "value": "scoped-test-only", "domain": ".instagram.com", "path": "/"}]
        return []


@pytest.fixture
def fixture(monkeypatch):
    routes, requests = {}, []
    real = httpx.AsyncClient

    def dispatch(request):
        requests.append(request)
        reply = routes.get(request.url.host + request.url.path, httpx.Response(404))
        return reply(request) if callable(reply) else reply

    def session(**kwargs):
        return real(**kwargs, transport=httpx.MockTransport(dispatch))

    monkeypatch.setattr(extraction, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": session}))
    scanner = Scanner(Browser(), Policy(), extraction.ExtractorService(Browser(), Policy()))

    async def run(url, kinds=None, *, with_browser=None):
        assets, events = [], []

        async def collect(item):
            assets.append(item)

        async def report(event):
            events.append(event)

        engine = scanner if with_browser is None else Scanner(
            with_browser, Policy(), extraction.ExtractorService(with_browser, Policy()))
        result = await engine.scan({"id": "post", "url": url}, {
            "url": url, "mode": "single", "kinds": kinds or ["image", "video"],
        }, collect, report, lambda: False)
        assert result == events[-1]
        return result, assets

    return SimpleNamespace(routes=routes, requests=requests, scan=run, client=session)


def page(url, item_id="123"):
    item = {"id": item_id, "desc": "Garden light", "video": {"playAddr": url, "downloadAddr": MEDIA + "/marked.mp4",
            "width": 320, "height": 180, "duration": 1}}
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}, "statusCode": 0}}}
    return httpx.Response(200, text='<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(data)
                          + '</script><video src="https://cdn.example.test/unrelated.mp4"></video>')


@pytest.mark.parametrize("link", [
    "https://www.tiktok.com/@garden", "https://www.instagram.com/reels/audio/123/",
    "https://www.douyin.com.evil.test/video/123", "https://user@www.douyin.com/video/123",
    "https://www.tiktok.com/@garden/video/123/extra", "https://www.douyin.com:8080/video/123",
])
def test_rejects_profiles_and_lookalike_urls(link):
    assert short_target(link) is None


async def test_single_tiktok_share_uses_only_target_playback_and_stays_on_platform(fixture):
    fixture.routes.update({
        "vm.tiktok.com/abc/": httpx.Response(302, headers={"location": TT + "?lang=en"}),
        "www.tiktok.com/@garden/video/123": page(MEDIA + "/primary.mp4?sig=1"),
    })
    report, assets = await fixture.scan("https://vm.tiktok.com/abc/")
    assert report["status"] == "completed" and len(assets) == 1
    item = assets[0]
    assert item["pageUrl"] == TT
    assert item["sourceUrl"] == MEDIA + "/primary.mp4?sig=1"
    assert item["shortMedia"] == {"platform": "tiktok", "postId": "123", "itemId": "123", "watermark": "playback"}
    assert stable_asset_identity(item)
    assert refresh_identity_matches(item, {**item, "sourceUrl": MEDIA + "/primary.mp4?sig=2"})
    assert not refresh_identity_matches(item, {**item, "_formatId": "download"})
    assert not refresh_identity_matches(item, {**item, "shortMedia": {**item["shortMedia"], "watermark": "embedded"}})
    assert all(request.method == "GET" for request in fixture.requests)
    fixture.routes["vm.tiktok.com/abc/"] = httpx.Response(302, headers={"location": IG})
    report, assets = await fixture.scan("https://vm.tiktok.com/abc/")
    assert report["status"] == "failed" and not assets
    assert not any(r.url.host == "www.instagram.com" for r in fixture.requests)


async def test_explicit_douyin_clean_source_wins_and_only_matches_specific_post(fixture):
    address = {"url_list": [MEDIA + "/clean.mp4"]}
    item = {"aweme_id": "123", "desc": "Garden", "video": {
        "play_addr": {"url_list": [MEDIA + "/play.mp4"]}, "download_addr": address,
        "has_watermark": False, "width": 320, "height": 180}}
    key = "www.douyin.com/aweme/v1/web/aweme/detail/"
    fixture.routes[key] = httpx.Response(200, json={"aweme_detail": item})
    report, assets = await fixture.scan(DY)
    assert report["status"] == "completed" and len(assets) == 1
    assert assets[0]["shortMedia"]["watermark"] == "source-clean"
    assert assets[0]["sourceUrl"] == MEDIA + "/clean.mp4"
    assert fixture.requests[-1].url.params["aweme_id"] == "123"
    fixture.routes[key] = httpx.Response(200, json={"aweme_detail": {**item, "aweme_id": "124"}})
    report, assets = await fixture.scan(DY)
    assert not assets and report["status"] == "failed"


async def test_missing_public_metadata_does_not_require_the_authorized_window(fixture):
    fixture.routes["www.tiktok.com/@garden/video/123"] = httpx.Response(200, text="<title>Just a moment</title>")
    report, assets = await fixture.scan(TT)
    assert report["status"] == "failed" and not assets
    assert report["message"] == "暂未取得公开媒体，请稍后重试"
    assert len(fixture.requests) == 1
    fixture.routes["www.douyin.com/aweme/v1/web/aweme/detail/"] = httpx.Response(200, json={"aweme_detail": None})
    report, assets = await fixture.scan(DY)
    assert report["status"] == "failed" and not assets
    assert report["message"] == "暂未取得公开媒体，请稍后重试"


@pytest.mark.parametrize("status", [401, 402, 403])
@pytest.mark.parametrize("url, endpoint", [
    (TT, "www.tiktok.com/@garden/video/123"),
    (DY, "m.douyin.com/share/video/123/"),
    (DY, "www.douyin.com/aweme/v1/web/aweme/detail/"),
])
async def test_explicit_http_access_gate_still_requires_authorization(fixture, status, url, endpoint):
    fixture.routes[endpoint] = httpx.Response(status, text="Access denied")
    report, assets = await fixture.scan(url)
    assert report["status"] == "needs-user" and not assets


async def test_instagram_carousel_uses_original_photos_and_checked_captioned_video(fixture):
    # Native Instagram shortcode B7 encodes product primary key 123.
    key = "www.instagram.com/api/v1/media/123/info/"
    fixture.routes[key] = httpx.Response(200, json={"items": [{"pk": "123", "carousel_media": [
        {"pk": "81", "media_type": 1, "image_versions2": {"candidates": [
            {"width": 80, "url": MEDIA + "/thumb.png"}, {"width": 800, "url": MEDIA + "/photo.png"}]}},
        {"pk": "82", "media_type": 2, "video_versions": [
            {"id": "101", "width": 320, "height": 180, "url": MEDIA + "/reel.mp4"}]}
    ]}]})
    report, assets = await fixture.scan(IG)
    assert report["status"] == "completed"
    assert [(a["kind"], a["sourceUrl"]) for a in assets] == [
        ("image", MEDIA + "/photo.png"), ("video", MEDIA + "/reel.mp4")]
    assert all(a["shortMedia"]["postId"] == "B7" and a["shortMedia"]["watermark"] == "unknown" for a in assets)
    assert all(stable_asset_identity(a) for a in assets)
    assert fixture.requests[0].headers["cookie"] == "sessionid=scoped-test-only"


async def test_downloaded_post_file_matches_original_and_decodes(fixture, tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    source = tmp_path / "fixture.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=400:duration=1", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(source)], check=True)
    raw = source.read_bytes()
    fixture.routes["www.tiktok.com/@garden/video/123"] = page(MEDIA + "/source.mp4")
    fixture.routes["cdn.example.test/source.mp4"] = httpx.Response(200, stream=httpx.ByteStream(raw), headers={"Content-Type": "video/mp4", "Content-Length": str(len(raw))})
    report, assets = await fixture.scan(TT)
    assert report["assetsFound"] == 1
    import backend.downloader as download
    monkeypatch.setattr(download, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": fixture.client}))
    async def progress(values):
        pass
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        {**assets[0], "siteId": "post", "id": "sample"}, SimpleNamespace(paused=False, cancelled=False), progress)
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(tmp_path / result["path"]),
                    "-map", "0", "-f", "null", "-"], check=True, timeout=10)


async def test_instagram_published_embed_preserves_only_confirmed_post_children(fixture):
    # Reproduce a publisher whose main page has only OG previews and whose
    # sanctioned embed explicitly enumerates all public carousel media.
    items = {"shortcode": "B7", "__typename": "GraphSidecar", "edge_sidecar_to_children": {"edges": [
        {"node": {"id": "51", "__typename": "GraphImage", "is_video": False, "display_url": MEDIA + "/slide.jpg"}},
        {"node": {"id": "52", "__typename": "GraphVideo", "is_video": True,
                  "video_url": MEDIA + "/clip.mp4", "display_url": MEDIA + "/cover.jpg"}},
    ]}}
    def embed(media):
        block = json.dumps({"gql_data": {"shortcode_media": media}})
        return httpx.Response(200, text="<script>" + json.dumps({"contextJSON": block}) + "</script>")
    fixture.routes.update({
        "www.instagram.com/api/v1/media/123/info/": httpx.Response(404),
        "www.instagram.com/p/B7/embed/": embed(items),
        "www.instagram.com/reel/B7/": httpx.Response(200,text="<title>Public reel</title>"),
    })
    report, assets = await fixture.scan(IG)
    assert report["status"] == "completed", report
    assert [(a["shortMedia"]["itemId"],a["sourceUrl"]) for a in assets] == [
        ("51", MEDIA + "/slide.jpg"), ("52", MEDIA + "/clip.mp4")]
    fixture.routes["www.instagram.com/p/B7/embed/"] = embed({**items, "shortcode": "B9"})
    report, assets = await fixture.scan(IG)
    assert not assets and report["status"] == "failed"


async def test_tiktok_playback_cookie_is_ephemeral_and_bound_to_exact_media(fixture, tmp_path):
    address = "https://media.tiktok.com/videos/clip.mp4?sig=one"
    document = page(address)
    document.headers["set-cookie"] = "tt_chain_token=grant-test; Domain=.tiktok.com; Path=/videos; Secure; Max-Age=300"
    fixture.routes["www.tiktok.com/@garden/video/123"] = document
    service = BrowserService(tmp_path, Policy())
    try:
        report, assets = await fixture.scan(TT, with_browser=service)
        assert report["status"] == "completed" and len(assets) == 1
        assert "grant-test" not in json.dumps(assets)
        assert [c["value"] for c in await service.cookies_for("post", address)] == ["grant-test"]
        assert not await service.cookies_for("different-site", address)
        assert not await service.cookies_for("post", address.replace("sig=one", "sig=two"))
        assert not await service.cookies_for("post", address.replace("https:", "http:"))
        assert not await service.cookies_for("post", address.replace("media.tiktok.com", "cdn.example.test"))
        await service.disconnect("post")
        assert not await service.cookies_for("post", address)
    finally:
        await service.close()


async def test_shared_instagram_redirect_to_account_operation_is_not_followed(fixture):
    fixture.routes["www.instagram.com/share/reel/token"] = httpx.Response(
        302, headers={"location": "/accounts/logout/"})
    report, assets = await fixture.scan("https://www.instagram.com/share/reel/token")
    assert not assets and report["status"] == "failed"
    assert [r.url.path for r in fixture.requests] == ["/share/reel/token"]

async def test_mobile_douyin_share_only_publishes_addressed_work(fixture):
    play = {"aweme_id": "123", "desc": "Field recording", "video": {"width": 320, "height": 180,
            "play_addr": {"url_list": [MEDIA + "/own.mp4"]}}}
    other = {**play, "aweme_id": "124", "video": {"play_addr": {"url_list": [MEDIA + "/recommend.mp4"]}}}
    payload = {"loaderData": {"video_(id)/page": {"itemId": "123",
               "videoInfoRes": {"status_code": 0, "item_list": [other, play]}}}}
    play["video"]["bit_rate"] = None
    def share(request):
        if request.headers.get("cookie") != "ttwid=anonymous-fixture":
            shell = {"loaderData": {"video_(id)/page": {"itemId": "123"}}}
            return httpx.Response(200, text="<script>window._ROUTER_DATA = " + json.dumps(shell) + ";</script>",
                                  headers={"Set-Cookie": "ttwid=anonymous-fixture; Domain=.douyin.com; Path=/share; Secure"})
        return httpx.Response(200, text="<script>window._ROUTER_DATA = " + json.dumps(payload) + ";</script>")
    fixture.routes["m.douyin.com/share/video/123/"] = share
    report, assets = await fixture.scan("https://m.douyin.com/share/video/123/")
    assert report["status"] == "completed"
    assert [(i["sourceUrl"], i["shortMedia"]["watermark"]) for i in assets] == [(MEDIA + "/own.mp4", "playback")]
    assert [r.url.host for r in fixture.requests] == ["m.douyin.com", "m.douyin.com"]


def test_douyin_share_adds_official_playback_candidate_without_relabeling_watermarked_source(monkeypatch):
    from yt_dlp import YoutubeDL

    from backend.short_media import _ShortDouyinIE, watermark_evidence

    original = "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=video123&ratio=720p&line=0"
    item = {"aweme_id": "123", "video": {"play_addr": {"url_list": [original]}, "bit_rate": []}}
    adapter = _ShortDouyinIE(YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False))
    monkeypatch.setattr(adapter, "_share_item", lambda *_: item)
    formats = adapter.read_post(short_target(DY))[0]["formats"]
    candidates = [f for f in formats if f.get("_cleanCandidate")]
    assert len(candidates) == 1
    assert candidates[0]["url"] == original.replace("/playwm/", "/play/")
    assert candidates[0]["format_id"] == "play_addr-play"
    assert watermark_evidence("douyin", candidates[0]) == "playback"
    assert candidates[0].get("_sourceClean") is not True
    assert [(f["url"], watermark_evidence("douyin", f)) for f in formats if not f.get("_cleanCandidate")] == [(original, "embedded")]


@pytest.mark.parametrize("url", [
    "https://aweme.snssdk.com.evil.test/aweme/v1/playwm/?video_id=a",
    "https://user@aweme.snssdk.com/aweme/v1/playwm/?video_id=a",
    "http://aweme.snssdk.com/aweme/v1/playwm/?video_id=a",
    "https://aweme.snssdk.com:8443/aweme/v1/playwm/?video_id=a",
    "https://aweme.snssdk.com/other/playwm/?video_id=a",
    "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=a&video_id=b",
    "https://aweme.snssdk.com/aweme/v1/playwm/?line=0",
])
def test_douyin_does_not_invent_playback_routes_on_other_hosts_or_invalid_endpoints(monkeypatch, url):
    from yt_dlp import YoutubeDL

    from backend.short_media import _ShortDouyinIE

    adapter = _ShortDouyinIE(YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False))
    monkeypatch.setattr(adapter, "_share_item", lambda *_: {
        "aweme_id": "123", "video": {"play_addr": {"url_list": [url]}, "bit_rate": []}})
    assert not any(f.get("_cleanCandidate") for f in adapter.read_post(short_target(DY))[0]["formats"])


def test_explicit_watermarked_route_cannot_be_overridden_by_false_clean_metadata():
    from backend.short_media import watermark_evidence

    assert watermark_evidence("douyin", {
        "url": "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=video123",
        "_sourceClean": True,
    }) == "embedded"


async def test_instagram_embed_keeps_unique_attachments_and_largest_published_photo(fixture):
    photo = {"id": "51", "__typename": "GraphImage", "is_video": False,
             "display_url": MEDIA + "/preview.jpg", "display_resources": [
                 {"src": MEDIA + "/small.jpg", "config_width": 150, "config_height": 150},
                 {"src": MEDIA + "/original.jpg", "config_width": 1080, "config_height": 1080}]}
    media = {"shortcode": "B7", "__typename": "GraphSidecar", "edge_sidecar_to_children": {"edges": [
        {"node": photo},
        {"node": {"id": "52", "__typename": "GraphVideo", "is_video": True, "video_url": MEDIA + "/first.mp4"}},
        {"node": photo},
        {"node": {"id": "53", "__typename": "GraphVideo", "is_video": True, "video_url": MEDIA + "/second.mp4"}},
    ]}}
    fixture.routes.update({
        "www.instagram.com/api/v1/media/123/info/": httpx.Response(404),
        "www.instagram.com/p/B7/embed/": httpx.Response(200, text="<script>" + json.dumps({
            "contextJSON": json.dumps({"gql_data": {"shortcode_media": media}})}) + "</script>"),
    })
    report, assets = await fixture.scan(IG)
    assert report["status"] == "completed"
    assert [(a["shortMedia"]["itemId"], a["sourceUrl"]) for a in assets] == [
        ("51", MEDIA + "/original.jpg"), ("52", MEDIA + "/first.mp4"), ("53", MEDIA + "/second.mp4")]
    assert len({stable_asset_identity(a) for a in assets}) == 3


async def test_instagram_silent_video_is_complete_media_not_an_unmerged_track(fixture):
    fixture.routes["www.instagram.com/api/v1/media/123/info/"] = httpx.Response(200, json={"items": [{
        "pk": "123", "has_audio": False, "media_type": 2,
        "video_versions": [{"id": "101", "width": 320, "height": 180, "url": MEDIA + "/silent.mp4"}],
    }]})
    report, assets = await fixture.scan(IG)
    assert report["status"] == "completed" and len(assets) == 1
    assert assets[0]["sourceUrl"] == MEDIA + "/silent.mp4"


@pytest.mark.parametrize("url", [
    "https://youtu.be/BaW_jenozKc", "https://www.youtube.com/watch?v=BaW_jenozKc",
    "https://www.youtube.com/shorts/BaW_jenozKc",
])
def test_youtube_single_video_dispatches_to_dedicated_adapter(url):
    from backend.short_media import platform_for_url

    target = short_target(url)
    assert target is not None
    assert (platform_for_url(url), target.platform, target.post_id) == ("youtube", "youtube", "BaW_jenozKc")
    assert target.url == "https://www.youtube.com/watch?v=BaW_jenozKc"
    assert platform_for_url("https://www.youtube.com/@channel") is None


async def test_tiktok_prefers_compatible_h264_within_same_watermark_evidence(fixture):
    item = {"id": "123", "desc": "Garden", "video": {
        "width": 576, "height": 1024, "duration": 1,
        "bitrateInfo": [
            {"PlayAddr": {"UrlKey": "v123_bytevc1_1080p_3000000", "UrlList": [MEDIA + "/hevc.mp4"]}},
            {"PlayAddr": {"UrlKey": "v123_h264_540p_1800000", "UrlList": [MEDIA + "/h264.mp4"]}},
        ],
        "downloadAddr": MEDIA + "/marked.mp4",
    }}
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}, "statusCode": 0}}}
    fixture.routes["www.tiktok.com/@garden/video/123"] = httpx.Response(
        200, text='<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(data) + '</script>')
    report, assets = await fixture.scan(TT)
    assert report["status"] == "completed" and len(assets) == 1
    assert assets[0]["sourceUrl"] == MEDIA + "/h264.mp4"
    assert assets[0]["shortMedia"]["watermark"] == "playback"


@pytest.mark.parametrize("candidate_available", [True, False])
async def test_douyin_candidate_must_pass_guarded_probe_or_keep_original(fixture, candidate_available):
    original = "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=video123&ratio=720p&line=0"
    item = {"aweme_id": "123", "video": {"play_addr": {"url_list": [original]}, "bit_rate": []}}
    fixture.routes["www.douyin.com/aweme/v1/web/aweme/detail/"] = httpx.Response(200, json={"aweme_detail": item})
    fixture.routes["aweme.snssdk.com/aweme/v1/play/"] = httpx.Response(
        302, headers={"location": MEDIA + "/candidate.mp4"})
    fixture.routes["cdn.example.test/candidate.mp4"] = httpx.Response(
        206 if candidate_available else 403,
        content=b"\x00\x00\x00\x18ftypisom" + b"\0" * 20 if candidate_available else b"<html>Not available</html>",
        headers={"Content-Type": "video/mp4" if candidate_available else "text/html"},
    )
    report, assets = await fixture.scan(DY)
    assert report["status"] == "completed" and len(assets) == 1
    assert assets[0]["sourceUrl"] == (original.replace("/playwm/", "/play/") if candidate_available else original)
    assert assets[0]["shortMedia"]["watermark"] == ("playback" if candidate_available else "embedded")
    probes = [r for r in fixture.requests if r.url.host in {"aweme.snssdk.com", "cdn.example.test"}]
    assert len(probes) == 2 and all(r.headers["range"] == "bytes=0-4095" for r in probes)
    assert all("cookie" not in r.headers for r in probes)
