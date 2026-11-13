"""A truncated short-video source must not replace an existing complete file."""

import shutil
import subprocess
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL

from backend.downloader import Downloader
from backend.extractor import ExtractorService
from backend.short_media import short_target
from tests_backend import test_downloader
from tests_backend.test_downloader import Browser, Policy, asset, control, progress

server = test_downloader.server


def test_social_source_duration_survives_display_formatting():
    page = "https://www.tiktok.com/@example/video/123"
    bridge = SimpleNamespace(post_target=short_target(page), headers={}, url=page, limited=False)
    with YoutubeDL({"quiet": True}) as ydl:
        item = ExtractorService._asset(ydl, {
            "duration": 45.25,
            "formats": [{"url": "https://media.example.test/video.mp4", "ext": "mp4",
                         "vcodec": "h264", "acodec": "aac", "format_id": "play"}],
        }, bridge, page)
    assert item["_shortDuration"] == 45.25


@pytest.mark.asyncio
@pytest.mark.parametrize("declared,success", [(2.0, True), (20.0, False)])
@pytest.mark.parametrize("duration_field", ["_shortDuration", "_instagramDuration"])
async def test_direct_social_download_checks_declared_full_duration(tmp_path, server, declared, success, duration_field):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg required for real media verification")
    source = tmp_path / "fixture.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=64x64:r=10:d=2", "-f", "lavfi", "-i",
                    "sine=frequency=400:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(source)], check=True, capture_output=True)
    body = source.read_bytes()
    server.routes["/movie"] = (200, {"Content-Type": "video/mp4", "Content-Length": str(len(body))}, body)
    original = tmp_path / "assets/asset_123/original.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"previous original remains intact")
    item = asset(server, "/movie", kind="video", duration="0:20",
                 shortMedia={"platform": "tiktok"}, **{duration_field: declared})
    operation = Downloader(tmp_path, Browser(), Policy()).download(item, control(), progress)
    if success:
        result = await operation
        assert abs(result["duration"] - 2) < .2
        assert original.read_bytes() == body
    else:
        with pytest.raises(ValueError, match="时长"):
            await operation
        assert original.read_bytes() == b"previous original remains intact"
