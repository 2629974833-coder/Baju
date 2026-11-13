import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import UnsupportedRequest

import backend.extractor as extraction

VIDEO_ID = "jNQXAC9IVRw"
PAGE = "https://www.youtube.com/watch?v=" + VIDEO_ID
PLAYER = "https://www.youtube.com/youtubei/v1/player"


@pytest.fixture
def bridge_factory(monkeypatch):
    sent = []
    real_client = httpx.AsyncClient

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"videoDetails": {"videoId": VIDEO_ID}})

    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(extraction, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": client}))

    class Browser:
        async def cookies_for(self, *_args):
            return []

    class Policy:
        async def validate(self, url):
            return url

    def make():
        bridge = extraction._Bridge(Browser(), Policy(), {"id": "youtube"}, PAGE,
                                    None, lambda: False, None, read_only=True)
        bridge.post_target = SimpleNamespace(platform="youtube", post_id=VIDEO_ID,
                                             url=PAGE, origin="https://www.youtube.com")
        return bridge

    return make, sent


async def test_only_exact_video_player_metadata_post_is_allowed_in_read_only_scan(bridge_factory):
    make, sent = bridge_factory
    data = json.dumps({"videoId": VIDEO_ID, "context": {"client": {"clientName": "WEB"}}}).encode()
    bridge = make()
    reply = await bridge.request(Request(PLAYER, data=data, method="POST"), {
        "Content-Type": "application/json", "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": "test-version", "X-Unrelated": "no",
    })
    assert reply.status == 200 and len(sent) == 1
    assert sent[0].headers["x-youtube-client-name"] == "1"
    assert "x-unrelated" not in sent[0].headers
    assert not bridge.read_only_blocked


@pytest.mark.parametrize("url,payload", [
    (PLAYER, {"videoId": "different-id"}),
    (PLAYER, {"videoId": VIDEO_ID, "context": {}, "action": "purchase"}),
    ("https://www.youtube.com/youtubei/v1/like/like", {"videoId": VIDEO_ID}),
    ("https://www.youtube.com.evil.test/youtubei/v1/player", {"videoId": VIDEO_ID}),
    ("http://www.youtube.com/youtubei/v1/player", {"videoId": VIDEO_ID}),
])
async def test_youtube_metadata_exception_never_allows_other_operations(bridge_factory, url, payload):
    make, sent = bridge_factory
    with pytest.raises(UnsupportedRequest):
        await make().request(Request(url, data=json.dumps(payload).encode(), method="POST"), {})
    assert not sent


async def test_cancelled_extraction_waits_for_worker_cleanup(bridge_factory, monkeypatch):
    make, _ = bridge_factory
    bridge = make()
    service = extraction.ExtractorService(bridge.browser, bridge.policy)
    started, finished = threading.Event(), threading.Event()

    def work(current, *_):
        started.set()
        try:
            while True:
                time.sleep(0.03)
                current.check()
        except extraction._Stopped:
            pass
        finally:
            finished.set()

    monkeypatch.setattr(service, "_extract", work)
    task = asyncio.create_task(service.extract({"id": "youtube"}, PAGE))
    async with asyncio.timeout(1):
        while not started.is_set():
            await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set(), "scan capacity must not be released while its worker is still running"
