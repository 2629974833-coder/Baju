"""A public share redirect identifies a post without opening its desktop player."""

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

import backend.extractor as extraction
from backend.short_media import short_target

SHARE = "https://v.douyin.com/public/"
CANONICAL = "https://www.douyin.com/video/123"
MOBILE = "https://m.douyin.com/share/video/123/"
MEDIA = "https://cdn.example.test/video.mp4"


@pytest.fixture
def public_post(monkeypatch):
    requests, validated, routes = [], [], {}
    real = httpx.AsyncClient

    class Policy:
        async def validate(self, url):
            validated.append(url)
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in {
                "v.douyin.com", "www.iesdouyin.com", "www.douyin.com",
                "m.douyin.com", "cdn.example.test",
            }:
                raise ValueError("out of scope")
            return url

    class Browser:
        async def cookies_for(self, *args):
            return []

    def dispatch(request):
        requests.append(request)
        return routes.get(str(request.url), httpx.Response(404))

    def client(**kwargs):
        return real(**kwargs, transport=httpx.MockTransport(dispatch))

    monkeypatch.setattr(extraction, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": client}))
    item = {"aweme_id": "123", "desc": "Public video", "video": {
        "play_addr": {"url_list": [MEDIA]}, "width": 320, "height": 180,
    }}
    payload = {"loaderData": {"video_(id)/page": {"videoInfoRes": {
        "status_code": 0, "item_list": [item],
    }}}}
    routes[MOBILE] = httpx.Response(200, text="<script>window._ROUTER_DATA = " + json.dumps(payload) + ";</script>")
    routes[CANONICAL] = httpx.Response(403)

    async def run():
        return await extraction.ExtractorService(Browser(), Policy()).extract(
            {"id": "anonymous", "url": "https://www.douyin.com"}, SHARE,
            post_target=short_target(SHARE), read_only=True,
        )

    return SimpleNamespace(routes=routes, requests=requests, validated=validated, run=run)


@pytest.mark.parametrize("destination", [
    CANONICAL,
    "https://www.iesdouyin.com/share/video/123/?from=copy",
])
async def test_share_identity_uses_public_mobile_page_without_desktop_login(public_post, destination):
    public_post.routes[SHARE] = httpx.Response(302, headers={"location": destination})
    result = await public_post.run()
    assert result["status"] == "resolved", result
    assert [asset["sourceUrl"] for asset in result["assets"]] == [MEDIA]
    assert result["assets"][0]["pageUrl"] == CANONICAL
    assert destination in public_post.validated
    assert [str(request.url) for request in public_post.requests] == [SHARE, MOBILE]
    assert all("cookie" not in request.headers for request in public_post.requests)


@pytest.mark.parametrize("destination, status", [
    ("https://www.douyin.com/passport/login/", "needs-user"),
    ("https://www.douyin.com/challenge/", "needs-user"),
    ("https://www.douyin.com/checkout/123", "unsupported"),
    ("https://www.tiktok.com/@someone/video/123", "unsupported"),
    ("https://127.0.0.1/video/123", "unsupported"),
    ("http://www.douyin.com/video/123", "unsupported"),
])
async def test_share_identity_preserves_redirect_boundaries(public_post, destination, status):
    public_post.routes[SHARE] = httpx.Response(302, headers={"location": destination})
    result = await public_post.run()
    assert result["status"] == status
    assert not result["assets"]
    assert [str(request.url) for request in public_post.requests] == [SHARE]
