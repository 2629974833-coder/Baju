from types import SimpleNamespace

import httpx

import backend.extractor as extraction


async def test_playback_candidate_probe_requires_real_mp4_bytes_and_checks_redirects(monkeypatch):
    requests, checked = [], []
    real_client = httpx.AsyncClient
    media = b"\x00\x00\x00\x18ftypisom" + b"\0" * 20

    def respond(request):
        requests.append(request)
        if request.url.host == "www.iesdouyin.com":
            return httpx.Response(302, headers={"location": "https://media.example.test/clean.mp4"})
        return httpx.Response(206, headers={"Content-Type": "video/mp4"}, content=media)

    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(extraction, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": client}))

    class Browser:
        async def cookies_for(self, *_):
            return []

    class Policy:
        async def validate(self, url):
            checked.append(url)
            if "127.0.0.1" in url:
                raise ValueError("private address")
            return url

    bridge = extraction._Bridge(Browser(), Policy(), {"id": "dy"}, "https://www.douyin.com/video/123",
                                None, lambda: False, None, read_only=True)
    probe = getattr(bridge, "probe_playback", None)
    assert callable(probe), "candidate playback must be verified before replacing marked media"
    assert await probe("https://www.iesdouyin.com/aweme/v1/play/?video_id=example", {})
    assert len(requests) == len(checked) == 2
    assert all(r.headers["range"] == "bytes=0-4095" for r in requests)
    assert not await probe("https://127.0.0.1/internal", {})
    assert len(requests) == 2

    media = b"<html>login required</html>"
    assert not await probe("https://media.example.test/clean.mp4", {})
