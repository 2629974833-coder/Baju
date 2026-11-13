"""YouTube's single-video adapter never treats a silent track or trailer as a movie."""

import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError


def module():
    assert importlib.util.find_spec("backend.youtube_media") is not None, (
        "YouTube adapter is missing"
    )
    return importlib.import_module("backend.youtube_media")


VIDEO_ID = "BaW_jenozKc"
PAGE = "https://www.youtube.com/watch?v=" + VIDEO_ID


def video(format_id="137", height=1080, **extra):
    return {
        "format_id": format_id,
        "url": f"https://rr1---sn-example.googlevideo.com/videoplayback?id={VIDEO_ID}&itag={format_id}",
        "ext": "mp4",
        "protocol": "https",
        "vcodec": "avc1.640028",
        "acodec": "none",
        "height": height,
        "width": height * 16 // 9,
        "tbr": 2000,
        **extra,
    }


def audio(format_id="140", language="en", **extra):
    return {
        **video(format_id),
        "ext": "m4a",
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "height": None,
        "width": None,
        "language": language,
        "tbr": 128,
        **extra,
    }


def asset(formats, **extra):
    m = module()
    return m.youtube_asset(
        {"id": VIDEO_ID, "title": "Test movie", "duration": 3600, "formats": formats, **extra},
        m.youtube_target(PAGE),
    )


@pytest.mark.parametrize(
    "url",
    [
        PAGE,
        "https://youtu.be/" + VIDEO_ID + "?t=30",
        "https://m.youtube.com/shorts/" + VIDEO_ID,
        "https://www.youtube-nocookie.com/embed/" + VIDEO_ID,
    ],
)
def test_single_video_urls_are_canonical_and_ignore_playlist_and_start_offsets(url):
    target = module().youtube_target(url)
    assert target.post_id == VIDEO_ID and target.url == PAGE
    assert target.platform == "youtube" and target.origin == "https://www.youtube.com"
    assert module().youtube_target(PAGE + "&list=PLother&t=30").url == PAGE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLother",
        "https://www.youtube.com/@creator",
        "https://www.youtube.com.evil.test/watch?v=" + VIDEO_ID,
        "https://user@www.youtube.com/watch?v=" + VIDEO_ID,
        "https://www.youtube.com:444/watch?v=" + VIDEO_ID,
        PAGE + "&v=otherid0000",
        "file:///watch?v=" + VIDEO_ID,
    ],
)
def test_non_video_or_ambiguous_urls_are_rejected(url):
    assert module().youtube_target(url) is None


def test_metadata_post_allowlist_is_bound_to_this_video_and_read_only_player_endpoint():
    m = module()
    target = m.youtube_target(PAGE)
    body = json.dumps({"videoId": VIDEO_ID, "context": {"client": {}}}).encode()
    assert m.youtube_allowed_request(
        target, "https://www.youtube.com/youtubei/v1/player", "POST", body
    )
    assert m.youtube_allowed_request(
        target, "https://www.youtube.com/s/player/test/base.js", "GET", None
    )
    assert m.youtube_allowed_request(
        target, "https://manifest.googlevideo.com/api/manifest/hls_playlist/test", "GET", None
    )
    for url in (
        "https://www.youtube.com/youtubei/v1/like/like",
        "https://evil.test/youtubei/v1/player",
        "http://www.youtube.com/youtubei/v1/player",
        "https://www.youtube.com:444/youtubei/v1/player",
    ):
        assert not m.youtube_allowed_request(target, url, "POST", body)
    assert not m.youtube_allowed_request(
        target,
        "https://www.youtube.com/youtubei/v1/player",
        "POST",
        json.dumps({"videoId": "otherid0000"}).encode(),
    )
    assert not m.youtube_allowed_request(
        target, "https://www.youtube.com/youtubei/v1/player", "POST", b"[]"
    )
    assert not m.youtube_allowed_request(target, PAGE, "DELETE", None)


def test_complete_video_selects_two_tracks_and_never_claims_watermark_removal():
    item = asset([video(), audio()])
    assert item["_protocol"] == "youtube" and item["engine"] == "yt-dlp"
    assert item["_formatId"] == "137+140"
    assert item["shortMedia"]["watermark"] == "unknown"
    assert [f["formatId"] for f in module().select_youtube_formats(item)] == ["137", "140"]
    assert asset([video()]) is None


def test_selection_honors_quality_and_audio_language_and_prefers_complete_hls_at_same_quality():
    hls = video(
        "96",
        protocol="m3u8_native",
        acodec="mp4a.40.2",
        language="en",
        url="https://manifest.googlevideo.com/api/manifest/hls_playlist/96",
    )
    item = asset([video(), video("136", 720), audio(), audio("140-zh", "zh"), hls])
    assert module().select_youtube_formats(item)[0]["protocol"] == "hls"
    chosen = module().select_youtube_formats(item, {"maxHeight": 720, "audioLanguage": "zh"})
    assert [f["formatId"] for f in chosen] == ["136", "140-zh"]
    with pytest.raises(ValueError, match="语言"):
        module().select_youtube_formats(item, {"audioLanguage": "fr"})


def test_separate_hls_audio_is_matched_only_with_its_own_master_manifest():
    master = "https://manifest.googlevideo.com/api/manifest/hls_playlist/master"
    vision = video("96", protocol="m3u8_native", manifest_url=master)
    sound = audio("aac-en", protocol="m3u8_native", manifest_url=master)
    item = asset([vision, sound])
    assert item is not None
    tracks = module().select_youtube_formats(item)
    assert tracks[0]["protocol"] == "hls" and tracks[0]["url"] == master
    assert tracks[1]["language"] == "en"
    assert asset([vision, {**sound, "manifest_url": master + "-other"}]) is None


def test_drm_live_trailer_unsupported_codecs_and_foreign_media_are_not_accepted():
    m = module()
    with pytest.raises(m.YouTubeDRMUnsupported):
        asset([video(), audio()], has_drm=True)
    with pytest.raises(m.YouTubeUnsupported):
        asset([video(), audio()], is_live=True)
    with pytest.raises(m.YouTubeUnsupported):
        asset([video(), audio()], id="otherid0000")
    with pytest.raises(m.YouTubeUnsupported):
        asset([video(), audio()], duration=None)
    assert asset([video(has_drm=True), audio()]) is None
    assert asset([video(vcodec="vp09.00.41.08"), audio()]) is None
    assert asset([video(url="http://127.0.0.1/secret"), audio()]) is None
    assert asset([video(fragments=[{"url": "https://evil.test"}]), audio()]) is None


def test_adapter_uses_initialized_official_extractor_and_rejects_trailer_redirect(monkeypatch):
    m = module()
    with YoutubeDL({"quiet": True, "cachedir": False}, auto_init=False) as ydl:
        adapter = m.YouTubeIE(ydl)
        calls = []
        monkeypatch.setattr(
            adapter, "extract", lambda url: calls.append(url) or {"id": VIDEO_ID, "formats": []}
        )
        assert adapter.read_post(m.youtube_target(PAGE))[0]["id"] == VIDEO_ID
        assert calls == [PAGE]
        monkeypatch.setattr(adapter, "extract", lambda url: {"_type": "url", "url": "otherid0000"})
        with pytest.raises(m.YouTubeAccessRequired):
            adapter.read_post(m.youtube_target(PAGE))


def test_official_bot_or_account_gate_becomes_actionable_without_echoing_upstream(monkeypatch):
    m = module()
    with YoutubeDL({"quiet": True}, auto_init=False) as ydl:
        adapter = m.YouTubeIE(ydl)

        def gated(_url):
            raise ExtractorError(
                "Sign in to confirm you're not a bot. sensitive-token", expected=True
            )

        monkeypatch.setattr(adapter, "extract", gated)
        with pytest.raises(m.YouTubeAccessRequired) as result:
            adapter.read_post(m.youtube_target(PAGE))
        assert "sensitive-token" not in str(result.value)


def test_runtime_params_disable_remote_components_and_cookiejar_is_per_extraction():
    m = module()
    params = m.youtube_ydl_params()
    assert set(params["js_runtimes"]) == {"deno"} and params["remote_components"] == []
    assert params["noplaylist"] is True
    with (
        YoutubeDL({"quiet": True}, auto_init=False) as one,
        YoutubeDL({"quiet": True}, auto_init=False) as two,
    ):
        m.seed_youtube_cookies(
            one,
            [
                {
                    "name": "SAPISID",
                    "value": "private",
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "wrong",
                    "value": "private",
                    "domain": ".evil.test",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "expired",
                    "value": "private",
                    "domain": ".youtube.com",
                    "path": "/",
                    "expires": 1,
                },
            ],
        )
        assert [c.name for c in one.cookiejar] == ["SAPISID"]
        assert list(two.cookiejar) == []


def test_adaptive_track_downloader_entry_exists():
    assert hasattr(module(), "download_youtube"), "YouTube adaptive-track transfer is missing"


def test_deno_is_located_next_to_python_without_depending_on_shell_path(tmp_path, monkeypatch):
    binary = tmp_path / "deno"
    binary.write_text("fixture runtime")
    binary.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert module().youtube_ydl_params()["js_runtimes"]["deno"] == {"path": str(binary)}


@pytest.fixture
def origin():
    from tests_backend.test_download_resume import origin as existing_origin

    yield from existing_origin.__wrapped__()


@pytest.fixture
def movie_tracks(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("local FFmpeg is required for real media verification")
    video_path, audio_path = tmp_path / "silent.mp4", tmp_path / "sound.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=1280x720:r=25",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "2",
            "-vn",
            "-c:a",
            "aac",
            str(audio_path),
        ],
        check=True,
    )
    return video_path.read_bytes(), audio_path.read_bytes()


def staged_transfer(tmp_path, origin, movie_tracks, callback):
    from backend.downloader import Downloader, _Transfer
    from tests_backend.test_download_resume import Browser

    url, server = origin.url, origin
    server.routes["/137"] = (movie_tracks[0], "video/mp4", '"video-one"')
    server.routes["/140"] = (movie_tracks[1], "audio/mp4", '"audio-one"')

    class FixturePolicy:
        proxy_url = None

        async def validate(self, target):
            if target.startswith(url):
                return target
            parsed = urlsplit(target)
            if parsed.hostname == "rr1---sn-example.googlevideo.com":
                return url + "/" + parse_qs(parsed.query)["itag"][0]
            raise ValueError("unexpected fixture destination")

    item = asset([video(height=720), audio()], duration=2)
    item.update(
        id="yt-test",
        siteId="youtube",
        _downloadOptions={"maxHeight": None, "audioLanguage": "auto"},
    )
    control = SimpleNamespace(paused=False, cancelled=False)
    transfer = _Transfer(item, control, callback, started=time.monotonic())
    downloader = Downloader(tmp_path, Browser(), FixturePolicy())
    folder = tmp_path / "staged"
    folder.mkdir(exist_ok=True)
    return downloader, transfer, folder / "original.part", folder / "cache", server


async def test_separate_http_tracks_are_downloaded_via_guarded_fetch_and_muxed_to_complete_mp4(
    tmp_path, origin, movie_tracks
):
    m = module()
    assert hasattr(m, "download_youtube"), "YouTube adaptive-track transfer is missing"

    async def callback(_event):
        pass

    downloader, transfer, partial, cache, server = staged_transfer(
        tmp_path, origin, movie_tracks, callback
    )
    duration = await m.download_youtube(downloader, transfer, partial, cache)
    assert duration == 2
    info = json.loads(
        await downloader._process(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(partial)]
        )
    )
    assert {s["codec_name"] for s in info["streams"]} == {"h264", "aac"}
    assert abs(float(info["format"]["duration"]) - 2) < 0.1
    assert [r["path"] for r in server.requests] == ["/137", "/140"]
    assert transfer.asset["_protocol"] == "youtube"
    assert transfer.details["selection"]["audioLanguage"] == "en"


async def test_pause_retains_private_track_checkpoint_and_resume_verifies_before_reuse(
    tmp_path, origin, movie_tracks
):
    m = module()
    assert hasattr(m, "download_youtube"), "YouTube adaptive-track transfer is missing"
    from backend.downloader import DownloadPaused, _Transfer

    paused = False
    transfer = None

    async def callback(event):
        nonlocal paused
        if event["downloadedBytes"] and not paused:
            paused = True
            transfer.control.paused = True

    downloader, transfer, partial, cache, server = staged_transfer(
        tmp_path, origin, movie_tracks, callback
    )
    original = transfer.asset
    with pytest.raises(DownloadPaused):
        await m.download_youtube(downloader, transfer, partial, cache)
    assert transfer.asset is original
    assert any(cache.rglob("*.json")) and not partial.exists()

    async def resumed_callback(_event):
        pass

    restarted = _Transfer(
        original,
        SimpleNamespace(paused=False, cancelled=False),
        resumed_callback,
        started=time.monotonic(),
    )
    await m.download_youtube(downloader, restarted, partial, cache)
    assert partial.stat().st_size > 0
    assert any("Range" in r["headers"] or "If-None-Match" in r["headers"] for r in server.requests)
    assert restarted.details["resumedBytes"] > 0


async def test_combined_track_download_budget_includes_audio(tmp_path, origin, movie_tracks):
    m = module()
    assert hasattr(m, "download_youtube"), "YouTube adaptive-track transfer is missing"

    async def callback(_event):
        pass

    downloader, transfer, partial, cache, _ = staged_transfer(
        tmp_path, origin, movie_tracks, callback
    )
    downloader.max_bytes = sum(map(len, movie_tracks)) - 1
    with pytest.raises(ValueError, match="大小限制"):
        await m.download_youtube(downloader, transfer, partial, cache)
    assert not partial.exists()


async def test_outer_downloader_publishes_mp4_and_removes_private_staging(
    tmp_path, origin, movie_tracks
):
    events = []

    async def callback(event):
        events.append(event)

    downloader, transfer, _, _, _ = staged_transfer(tmp_path, origin, movie_tracks, callback)
    result = await downloader.download(transfer.asset, transfer.control, callback)
    saved = tmp_path / result["path"]
    assert saved.is_file() and saved.suffix == ".mp4"
    assert result["mime"] == "video/mp4" and result["height"] == 720
    assert abs(result["duration"] - 2) < 0.1
    assert result["sha256"] and result["size"] == saved.stat().st_size
    assert events[-1]["phase"] == "completed" and events[-1]["progress"] == 100
    assert not (saved.parent / ".hls").exists()
    assert not (saved.parent / "original.part").exists()


async def test_catalog_duration_label_does_not_disable_full_movie_verification(
    tmp_path, origin, movie_tracks
):
    from backend.tasks import duration_label

    async def callback(_event):
        pass

    downloader, transfer, _, _, _ = staged_transfer(tmp_path, origin, movie_tracks, callback)
    transfer.asset.update(asset([video(height=720), audio()], duration=19))
    # TaskManager stores the display label in duration after receiving scanner metadata.
    transfer.asset["duration"] = duration_label(transfer.asset["duration"])
    with pytest.raises(ValueError, match="时长不符"):
        await downloader.download(transfer.asset, transfer.control, callback)
    assert not (tmp_path / "assets" / "yt-test" / "original.mp4").exists()


@pytest.mark.parametrize("failure", ["missing-audio", "duration-mismatch"])
async def test_invalid_or_partial_movie_does_not_replace_existing_original(
    tmp_path, origin, movie_tracks, failure
):
    async def callback(_event):
        pass

    downloader, transfer, _, _, server = staged_transfer(tmp_path, origin, movie_tracks, callback)
    saved = tmp_path / "assets" / "yt-test" / "original.mp4"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"previous verified original")
    if failure == "missing-audio":
        server.routes["/140"] = (movie_tracks[0], "video/mp4", '"silent"')
    else:
        transfer.asset["_youtubeDuration"] = 19
    with pytest.raises(ValueError):
        await downloader.download(transfer.asset, transfer.control, callback)
    assert saved.read_bytes() == b"previous verified original"
    assert not (saved.parent / ".hls").exists()
    assert not (saved.parent / "original.part").exists()


async def test_complete_hls_uses_existing_staging_and_preserves_audio(
    tmp_path, origin, movie_tracks
):
    async def callback(_event):
        pass

    downloader, transfer, partial, cache, server = staged_transfer(
        tmp_path, origin, movie_tracks, callback
    )
    manifest = tmp_path / "playlist.m3u8"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(tmp_path / "silent.mp4"),
            "-i",
            str(tmp_path / "sound.m4a"),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(tmp_path / "segment%03d.ts"),
            str(manifest),
        ],
        check=True,
    )
    server.routes["/96"] = (manifest.read_bytes(), "application/vnd.apple.mpegurl", '"manifest"')
    for path in tmp_path.glob("segment*.ts"):
        server.routes["/" + path.name] = (path.read_bytes(), "video/mp2t", f'"{path.name}"')
    transfer.asset = {
        **transfer.asset,
        **asset(
            [video("96", 720, protocol="m3u8_native", acodec="mp4a.40.2", language="en")],
            duration=2,
        ),
    }
    original = transfer.asset
    duration = await module().download_youtube(downloader, transfer, partial, cache)
    assert abs(duration - 2) < 0.1 and partial.stat().st_size > 0
    assert transfer.asset is original and transfer.asset["_protocol"] == "youtube"
    assert any(r["path"].endswith(".ts") for r in server.requests)
