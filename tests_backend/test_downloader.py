import hashlib
import io
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image


class Policy:
    def __init__(self, blocked=()):
        self.seen = []
        self.blocked = blocked

    async def validate(self, url):
        self.seen.append(url)
        if not url.startswith(("http://", "https://")) or any(x in url for x in self.blocked):
            raise ValueError("URL is not allowed")
        return url


class Browser:
    def __init__(self):
        self.calls = []

    async def cookies_for(self, site_id, url):
        self.calls.append((site_id, url))
        return [{"name": "session", "value": "local-test", "domain": "127.0.0.1", "path": "/"}]


@pytest.fixture
def server():
    routes = {}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            status, headers, body = routes.get(self.path, (404, {}, b"missing"))
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

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{httpd.server_port}", routes=routes, requests=requests
    )
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.fixture
def png():
    stream = io.BytesIO()
    Image.new("RGB", (17, 11), (40, 120, 200)).save(stream, format="PNG")
    return stream.getvalue()


def asset(server, path="/image", **extra):
    return {
        "id": "asset_123",
        "siteId": "site_123",
        "sourceUrl": server.url + path,
        "pageUrl": server.url + "/page",
        "kind": "image",
        "title": "../evil.html",
        "engine": "http",
        **extra,
    }


def control():
    return SimpleNamespace(cancelled=False, paused=False)


async def progress(_event):
    pass


@pytest.mark.asyncio
async def test_http_image_is_verified_and_saved_atomically(tmp_path, server, png):
    from backend.downloader import Downloader

    server.routes["/image"] = (
        200,
        {"Content-Type": "image/png", "Content-Length": str(len(png))},
        png,
    )
    browser, policy, events = Browser(), Policy(), []

    async def collect(event):
        events.append(event)

    result = await Downloader(tmp_path, browser, policy).download(asset(server), control(), collect)
    assert result["path"] == "assets/asset_123/original.png"
    assert (tmp_path / result["path"]).read_bytes() == png
    assert result["sha256"] == hashlib.sha256(png).hexdigest()
    assert result["size"] == len(png)
    assert (result["width"], result["height"], result["duration"], result["mime"]) == (
        17,
        11,
        0,
        "image/png",
    )
    assert events[-1]["progress"] == 100
    assert not list(tmp_path.rglob("*.part"))
    assert browser.calls == [("site_123", server.url + "/image")]
    assert server.requests[0][1]["Cookie"] == "session=local-test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,mime,body",
    [
        (401, "text/html", b"login"),
        (200, "text/html", b"<html>login</html>"),
        (200, "image/png", b"<html>login</html>"),
    ],
)
async def test_http_rejects_auth_html_and_spoofed_media(tmp_path, server, status, mime, body):
    from backend.downloader import Downloader

    server.routes["/image"] = (status, {"Content-Type": mime}, body)
    with pytest.raises(ValueError):
        await Downloader(tmp_path, Browser(), Policy()).download(asset(server), control(), progress)
    assert not list(tmp_path.rglob("original.*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["cancelled", "paused"])
async def test_control_interrupts_stream_and_pause_retains_partial(tmp_path, server, state):
    from backend.downloader import DownloadCancelled, Downloader, DownloadPaused

    server.routes["/image"] = (200, {"Content-Type": "image/png"}, b"x" * 400_000)
    operation = control()

    async def interrupt(event):
        if event["downloadedBytes"]:
            setattr(operation, state, True)

    with pytest.raises(DownloadCancelled if state == "cancelled" else DownloadPaused):
        await Downloader(tmp_path, Browser(), Policy()).download(
            asset(server), operation, interrupt
        )
    partials = list(tmp_path.rglob("*.part"))
    assert bool(partials) == (state == "paused")
    if partials:
        assert 0 < partials[0].stat().st_size < 400_000


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, "10000"])
async def test_http_maximum_size_is_enforced(tmp_path, server, declared_length):
    from backend.downloader import Downloader

    headers = {"Content-Type": "image/png"}
    if declared_length:
        headers["Content-Length"] = declared_length
    server.routes["/image"] = (200, headers, b"x" * 5000)
    with pytest.raises(ValueError, match="大小|size|large"):
        await Downloader(tmp_path, Browser(), Policy(), max_bytes=1000).download(
            asset(server), control(), progress
        )
    assert not list(tmp_path.rglob("original.*"))


@pytest.mark.asyncio
async def test_http_rejects_truncated_response(tmp_path, server, png):
    from backend.downloader import Downloader

    server.routes["/image"] = (
        200,
        {"Content-Type": "image/png", "Content-Length": str(len(png) + 50)},
        png,
    )
    with pytest.raises(ValueError, match="完整|incomplete|transfer"):
        await Downloader(tmp_path, Browser(), Policy()).download(asset(server), control(), progress)
    assert not list(tmp_path.rglob("original.*"))


@pytest.mark.asyncio
async def test_redirects_are_revalidated_and_cross_host_credentials_are_removed(
    tmp_path, server, png
):
    from backend.downloader import Downloader

    target = server.url.replace("127.0.0.1", "localhost") + "/final"
    server.routes["/image"] = (302, {"Location": target}, b"")
    server.routes["/final"] = (200, {"Content-Type": "image/png"}, png)
    policy = Policy()
    await Downloader(tmp_path, Browser(), policy).download(
        asset(server, _headers={"Authorization": "Bearer private", "Cookie": "bad=raw"}),
        control(),
        progress,
    )
    assert target in policy.seen
    assert server.requests[0][1]["Authorization"] == "Bearer private"
    assert "Authorization" not in server.requests[1][1]
    assert "Cookie" not in server.requests[1][1]


@pytest.mark.asyncio
async def test_redirect_cannot_reach_disallowed_destination(tmp_path, server):
    from backend.downloader import Downloader

    server.routes["/image"] = (302, {"Location": server.url + "/blocked"}, b"")
    with pytest.raises(ValueError, match="allowed"):
        await Downloader(tmp_path, Browser(), Policy(blocked=("/blocked",))).download(
            asset(server), control(), progress
        )
    assert len(server.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["../other", "/tmp/escape", "a/b", "..", "x\\y"])
async def test_asset_id_cannot_escape_storage(tmp_path, server, identifier):
    from backend.downloader import Downloader

    with pytest.raises(ValueError):
        await Downloader(tmp_path, Browser(), Policy()).download(
            asset(server, id=identifier), control(), progress
        )
    assert not server.requests


def ffmpeg():
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


@pytest.mark.asyncio
async def test_hls_is_downloaded_with_policy_and_muxed_to_real_video(tmp_path, server):
    from backend.downloader import Downloader

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    subprocess.run(
        [
            ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            "-hls_time",
            "0.5",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            str(fixture / "index.m3u8"),
        ],
        check=True,
    )
    for path in fixture.iterdir():
        mime = "application/vnd.apple.mpegurl" if path.suffix == ".m3u8" else "video/mp2t"
        server.routes["/" + path.name] = (200, {"Content-Type": mime}, path.read_bytes())
    policy = Policy()
    result = await Downloader(tmp_path / "data", Browser(), policy).download(
        asset(server, "/index.m3u8", kind="video", engine="yt-dlp"), control(), progress
    )
    assert result["mime"] == "video/mp4"
    assert result["width"] == 64 and result["height"] == 48
    assert 0.8 <= result["duration"] <= 2
    assert result["size"] > 1000
    assert (tmp_path / "data" / result["path"]).is_file()
    assert any(url.endswith(".ts") for url in policy.seen)
    probe = subprocess.run(
        [
            ffmpeg().replace("ffmpeg", "ffprobe"),
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(tmp_path / "data" / result["path"]),
        ],
        capture_output=True,
        check=True,
    )
    assert {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]} == {
        "video",
        "audio",
    }


@pytest.mark.asyncio
async def test_hls_nested_destination_is_checked_before_request(tmp_path, server):
    from backend.downloader import Downloader

    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nblocked.ts\n#EXT-X-ENDLIST\n"
    server.routes["/index.m3u8"] = (
        200,
        {"Content-Type": "application/vnd.apple.mpegurl"},
        playlist,
    )
    with pytest.raises(ValueError, match="allowed"):
        await Downloader(tmp_path, Browser(), Policy(blocked=("blocked.ts",))).download(
            asset(server, "/index.m3u8", kind="video", engine="yt-dlp"), control(), progress
        )
    assert [path for path, _headers in server.requests] == ["/index.m3u8"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "playlist",
    [
        b"<html>website extractor needed</html>",
        b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n',
    ],
)
async def test_hls_rejects_unsupported_extractors_and_encrypted_streams(tmp_path, server, playlist):
    from backend.downloader import Downloader

    server.routes["/index.m3u8"] = (
        200,
        {"Content-Type": "application/vnd.apple.mpegurl"},
        playlist,
    )
    with pytest.raises(ValueError):
        await Downloader(tmp_path, Browser(), Policy()).download(
            asset(server, "/index.m3u8", kind="video", engine="yt-dlp"), control(), progress
        )
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_aes128_hls_with_signed_relative_urls_is_authorized_and_playable(tmp_path, server):
    from backend.downloader import Downloader

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "key.bin").write_bytes(bytes(range(16)))
    (fixture / "key-info").write_text(f"secret.key?key=signature\n{fixture / 'key.bin'}\n")
    subprocess.run(
        [
            ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=10:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-hls_time",
            "0.5",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            "-hls_key_info_file",
            str(fixture / "key-info"),
            str(fixture / "index.m3u8"),
        ],
        check=True,
    )
    playlist = (fixture / "index.m3u8").read_text()
    for segment in fixture.glob("*.ts"):
        playlist = playlist.replace(segment.name, segment.name + "?segment=signature")
        server.routes["/" + segment.name + "?segment=signature"] = (
            200,
            {"Content-Type": "video/mp2t"},
            segment.read_bytes(),
        )
    server.routes["/secret.key?key=signature"] = (
        200,
        {"Content-Type": "application/octet-stream"},
        bytes(range(16)),
    )
    server.routes["/index.m3u8?playlist=signature"] = (
        200,
        {"Content-Type": "application/vnd.apple.mpegurl"},
        playlist.encode(),
    )
    policy = Policy()
    result = await Downloader(tmp_path / "data", Browser(), policy).download(
        asset(server, "/index.m3u8?playlist=signature", kind="video", engine="yt-dlp"),
        control(),
        progress,
    )
    assert result["mime"] == "video/mp4"
    assert result["width"] == 64
    assert any(url.endswith("secret.key?key=signature") for url in policy.seen)
    assert any(url.endswith(".ts?segment=signature") for url in policy.seen)


@pytest.mark.asyncio
async def test_hls_separate_audio_is_preserved_in_output(tmp_path, server):
    from backend.downloader import Downloader

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    subprocess.run(
        [
            ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=10:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-hls_time",
            "0.5",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            str(fixture / "video.m3u8"),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            "-hls_time",
            "0.5",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            str(fixture / "audio.m3u8"),
        ],
        check=True,
    )
    for path in fixture.iterdir():
        server.routes["/" + path.name] = (
            200,
            {"Content-Type": "application/octet-stream"},
            path.read_bytes(),
        )
    server.routes["/master.m3u8"] = (
        200,
        {"Content-Type": "application/vnd.apple.mpegurl"},
        b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Main",DEFAULT=YES,URI="audio.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="audio"\nvideo.m3u8\n',
    )
    result = await Downloader(tmp_path / "data", Browser(), Policy()).download(
        asset(server, "/master.m3u8", kind="video", engine="yt-dlp"), control(), progress
    )
    probe = subprocess.run(
        [
            ffmpeg().replace("ffmpeg", "ffprobe"),
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(tmp_path / "data" / result["path"]),
        ],
        capture_output=True,
        check=True,
    )
    assert {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]} == {
        "video",
        "audio",
    }


@pytest.mark.asyncio
async def test_hls_live_and_non_http_nested_urls_are_rejected(tmp_path, server):
    from backend.downloader import Downloader

    for body in [
        b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nlive.ts\n",
        b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n",
    ]:
        server.routes["/index.m3u8"] = (
            200,
            {"Content-Type": "application/vnd.apple.mpegurl"},
            body,
        )
        with pytest.raises(ValueError):
            await Downloader(tmp_path, Browser(), Policy()).download(
                asset(server, "/index.m3u8", kind="video", engine="yt-dlp"), control(), progress
            )
    assert all(path == "/index.m3u8" for path, _headers in server.requests)
