"""Selection must change real media, stay bounded, and never mix paused tracks."""

import hashlib
import json
import subprocess

import pytest

from backend.downloader import Downloader, DownloadPaused
from tests_backend import test_download_resume as resume_fixtures
from tests_backend.test_download_resume import Browser, Policy, asset, control, ignore

origin = resume_fixtures.origin


def test_normalize_selection_is_strict_and_normalizes_language():
    from backend.selection import normalize_selection

    assert normalize_selection() == {"maxHeight": None, "audioLanguage": "auto"}
    assert normalize_selection({"maxHeight": 720, "audioLanguage": "ENG-us"}) == {
        "maxHeight": 720,
        "audioLanguage": "en-us",
    }
    for value in [
        False,
        [],
        "",
        {"maxHeight": True},
        {"maxHeight": "720"},
        {"maxHeight": 143},
        {"maxHeight": 4321},
        {"audioLanguage": 1},
        {"audioLanguage": "en\r\nHeader"},
        {"audioLanguage": "en x"},
        {"audioLanguage": "english"},
        {"extra": 1},
    ]:
        with pytest.raises(ValueError):
            normalize_selection(value)


def tiny_mpd():
    return b"""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S"><Period>
      <AdaptationSet mimeType="video/mp4" codecs="avc1.64000a"><SegmentTemplate duration="1" initialization="init-$RepresentationID$.mp4" media="$RepresentationID$-$Number$.m4s"/>
      <Representation id="low" width="256" height="144" bandwidth="1000"/><Representation id="high" width="512" height="288" bandwidth="2000"/></AdaptationSet>
      <AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2" lang="en"><SegmentTemplate duration="1" initialization="init-en.mp4" media="en-$Number$.m4s"/><Representation id="en" bandwidth="1000"/></AdaptationSet>
      <AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2" lang="zh"><SegmentTemplate duration="1" initialization="init-zh.mp4" media="zh-$Number$.m4s"/><Representation id="zh" bandwidth="2000"/></AdaptationSet>
    </Period></MPD>"""


def test_dash_selection_honors_height_language_and_rejects_missing():
    from backend.dash import parse_mpd

    tracks = parse_mpd(
        tiny_mpd(),
        "https://example.com/index.mpd",
        selection={"maxHeight": 144, "audioLanguage": "en-US"},
    )
    assert [track["format_id"] for track in tracks] == ["low", "en"]
    with pytest.raises(ValueError, match="音轨|语言"):
        parse_mpd(tiny_mpd(), "https://example.com/index.mpd", selection={"audioLanguage": "fr"})
    high_only = tiny_mpd().replace(b'height="144"', b'height="720"')
    with pytest.raises(ValueError, match="画质|分辨率"):
        parse_mpd(high_only, "https://example.com/index.mpd", selection={"maxHeight": 144})


@pytest.mark.asyncio
async def test_describe_only_fetches_root_manifest_and_never_exposes_sources(tmp_path, origin):
    origin.routes["/index.mpd"] = (tiny_mpd(), "application/dash+xml", '"mpd"')
    item = asset(origin, sourceUrl=origin.url + "/index.mpd", kind="video", engine="yt-dlp")
    info = await Downloader(tmp_path, Browser(), Policy()).describe_options(item)
    assert info["protocol"] == "dash" and info["selectable"]
    assert info["heights"] == [144, 288]
    assert {track["language"] for track in info["audioTracks"]} == {"en", "zh"}
    assert len(origin.requests) == 1
    assert "http" not in json.dumps(info).replace('"http"', "")
    assert not list(tmp_path.rglob("*.mpd"))


def command(args):
    return subprocess.run(
        [Downloader._binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def multi_sources(tmp_path, origin):
    source = tmp_path / "sources"
    source.mkdir()
    dash = source / "dash"
    dash.mkdir()
    command(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=256x144:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=512x288:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-map",
            "0:v",
            "-map",
            "1:v",
            "-map",
            "2:a",
            "-map",
            "3:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=zho",
            "-adaptation_sets",
            "id=0,streams=0,1 id=1,streams=2 id=2,streams=3",
            "-seg_duration",
            "1",
            "-f",
            "dash",
            str(dash / "index.mpd"),
        ]
    )
    hls = source / "hls"
    hls.mkdir()
    for name, size in [("low", "256x144"), ("high", "512x288")]:
        command(
            [
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={size}:rate=10:duration=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "10",
                "-an",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                str(hls / f"{name}_%d.ts"),
                str(hls / f"{name}.m3u8"),
            ]
        )
    for name, frequency in [("en", 440), ("zh", 880)]:
        command(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration=2",
                "-c:a",
                "aac",
                "-vn",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                str(hls / f"{name}_%d.ts"),
                str(hls / f"{name}.m3u8"),
            ]
        )
    (hls / "master.m3u8").write_text("""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="Chinese",LANGUAGE="zh",DEFAULT=NO,URI="zh.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=256x144,AUDIO="main"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=512x288,AUDIO="main"
high.m3u8
""")
    for path in source.rglob("*"):
        if path.is_file():
            mime = (
                "application/dash+xml"
                if path.suffix == ".mpd"
                else "application/vnd.apple.mpegurl"
                if path.suffix == ".m3u8"
                else "application/octet-stream"
            )
            body = path.read_bytes()
            origin.routes["/" + path.relative_to(source).as_posix()] = (
                body,
                mime,
                '"' + hashlib.sha256(body).hexdigest() + '"',
            )
    return source


def inspect_output(path, *, height, language):
    output = subprocess.run(
        [Downloader._binary("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
    )
    streams = json.loads(output.stdout)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert video["height"] == height
    assert audio.get("tags", {}).get("language") == language
    command(["-xerror", "-i", str(path), "-f", "null", "-"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,manifest", [("hls", "hls/master.m3u8"), ("dash", "dash/index.mpd")]
)
async def test_real_low_quality_and_chinese_track_are_selected(
    tmp_path, origin, multi_sources, protocol, manifest
):
    item = asset(
        origin,
        sourceUrl=origin.url + "/" + manifest,
        kind="video",
        engine="yt-dlp",
        _downloadOptions={"maxHeight": 144, "audioLanguage": "zh"},
    )
    root = tmp_path / "data"
    result = await Downloader(root, Browser(), Policy()).download(item, control(), ignore)
    assert result["selection"]["height"] == 144
    assert result["selection"]["audioLanguage"] == "zh"
    assert result["selection"]["optionsApplied"] is True
    inspect_output(root / result["path"], height=144, language="zho")
    paths = [request["path"] for request in origin.requests]
    if protocol == "hls":
        assert not any("/high" in path or "/en" in path for path in paths)
    else:
        assert not any("stream1" in path or "stream2" in path for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,manifest", [("hls", "hls/master.m3u8"), ("dash", "dash/index.mpd")]
)
async def test_changed_selection_on_restart_never_reuses_other_track(
    tmp_path, origin, multi_sources, protocol, manifest
):
    item = asset(
        origin,
        sourceUrl=origin.url + "/" + manifest,
        kind="video",
        engine="yt-dlp",
        _downloadOptions={"maxHeight": 288, "audioLanguage": "en"},
    )
    root = tmp_path / "data"
    first = control()

    async def pause(progress):
        if progress.get("completedSegments", 0) >= 2:
            first.paused = True

    with pytest.raises(DownloadPaused):
        await Downloader(root, Browser(), Policy()).download(item, first, pause)
    start = len(origin.requests)
    item["_downloadOptions"] = {"maxHeight": 144, "audioLanguage": "zh"}
    result = await Downloader(root, Browser(), Policy()).download(item, control(), ignore)
    inspect_output(root / result["path"], height=144, language="zho")
    assert result["selection"]["height"] == 144
    assert result.get("resumedBytes", 0) == 0
    assert not any(request["status"] == 304 for request in origin.requests[start:])


def test_language_aliases_never_cross_explicit_regions():
    from backend.selection import choose_audio

    tracks = [{"lang": "eng-GB", "default": True}, {"lang": "en-US", "default": False}]

    def pick(request, available=tracks):
        return choose_audio(
            available, request, language=lambda row: row["lang"], rank=lambda row: row["default"]
        )

    assert pick("en")["lang"] == "eng-GB"
    assert pick("en-us")["lang"] == "en-US"
    with pytest.raises(ValueError, match="语言|音轨"):
        pick("en-au")
    assert pick("en-au", [{"lang": "en", "default": True}])["lang"] == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh", "auto"])
async def test_inband_audio_cannot_claim_language_selection(
    tmp_path, origin, multi_sources, language
):
    body, mime, etag = origin.routes["/hls/master.m3u8"]
    origin.routes["/hls/master.m3u8"] = (
        body.replace(b',URI="en.m3u8"', b"").replace(b',URI="zh.m3u8"', b""),
        mime,
        etag,
    )
    item = asset(
        origin,
        sourceUrl=origin.url + "/hls/master.m3u8",
        kind="video",
        engine="yt-dlp",
        _downloadOptions={"maxHeight": 144, "audioLanguage": language},
    )
    downloader = Downloader(tmp_path / "data", Browser(), Policy())
    if language != "auto":
        with pytest.raises(ValueError, match="内嵌|音轨"):
            await downloader.download(item, control(), ignore)
        assert len(origin.requests) == 1
    else:
        result = await downloader.download(item, control(), ignore)
        assert result["selection"]["audioLanguage"] == "und"


@pytest.mark.asyncio
async def test_hls_height_cap_prefers_highest_height_not_bandwidth(tmp_path, origin, multi_sources):
    body, mime, etag = origin.routes["/hls/master.m3u8"]
    origin.routes["/hls/master.m3u8"] = (
        body.replace(b"BANDWIDTH=100000", b"BANDWIDTH=900000"),
        mime,
        etag,
    )
    item = asset(
        origin,
        sourceUrl=origin.url + "/hls/master.m3u8",
        kind="video",
        engine="yt-dlp",
        _downloadOptions={"maxHeight": 288, "audioLanguage": "zh"},
    )
    root = tmp_path / "data"
    result = await Downloader(root, Browser(), Policy()).download(item, control(), ignore)
    inspect_output(root / result["path"], height=288, language="zho")


@pytest.mark.asyncio
async def test_describe_hls_never_exposes_uppercase_urls_or_credentials(
    tmp_path, origin, multi_sources
):
    body, mime, etag = origin.routes["/hls/master.m3u8"]
    body = body.replace(
        b'NAME="English"', b'NAME="English HTTPS://media.example/path?token=inspection-secret"'
    )
    body = body.replace(b'NAME="Chinese"', b'NAME="Authorization: Bearer inspection-secret"')
    origin.routes["/hls/master.m3u8"] = (body, mime, etag)
    item = asset(origin, sourceUrl=origin.url + "/hls/master.m3u8", kind="video", engine="yt-dlp")
    info = await Downloader(tmp_path / "data", Browser(), Policy()).describe_options(item)
    assert info["heights"] == [144, 288]
    assert {track["language"] for track in info["audioTracks"]} == {"en", "zh"}
    assert len(origin.requests) == 1
    assert "inspection-secret" not in json.dumps(info)
    assert "media.example" not in json.dumps(info)
    assert not list((tmp_path / "data").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,options,error",
    [
        (
            lambda body: body.replace(b"RESOLUTION=256x144", b"UNKNOWN=1").replace(
                b"RESOLUTION=512x288", b"UNKNOWN=2"
            ),
            {"maxHeight": 288},
            "画质|分辨率",
        ),
        (lambda body: body, {"audioLanguage": "fr"}, "音轨|语言"),
        (
            lambda body: body.replace(
                b'GROUP-ID="main",NAME="Chinese"', b'GROUP-ID="other",NAME="Chinese"'
            ),
            {"audioLanguage": "zh"},
            "音轨|语言",
        ),
    ],
)
async def test_hls_unsatisfied_preferences_fail_before_media(
    tmp_path, origin, multi_sources, change, options, error
):
    body, mime, etag = origin.routes["/hls/master.m3u8"]
    origin.routes["/hls/master.m3u8"] = (change(body), mime, etag)
    item = asset(
        origin,
        sourceUrl=origin.url + "/hls/master.m3u8",
        kind="video",
        engine="yt-dlp",
        _downloadOptions=options,
    )
    with pytest.raises(ValueError, match=error):
        await Downloader(tmp_path / "data", Browser(), Policy()).download(item, control(), ignore)
    assert len(origin.requests) == 1


@pytest.mark.asyncio
async def test_leaf_hls_requires_auto_preferences(tmp_path, origin, multi_sources):
    item = asset(
        origin,
        sourceUrl=origin.url + "/hls/low.m3u8",
        kind="video",
        engine="yt-dlp",
        _downloadOptions={"maxHeight": 144},
    )
    downloader = Downloader(tmp_path / "data", Browser(), Policy())
    info = await downloader.describe_options(item)
    assert not info["selectable"]
    with pytest.raises(ValueError, match="子清单"):
        await downloader.download(item, control(), ignore)
    assert [row["path"] for row in origin.requests] == ["/hls/low.m3u8"] * 2


@pytest.mark.asyncio
async def test_describe_cancel_cleans_manifest_temporary_directory(tmp_path, origin):
    import asyncio

    origin.stalled = True
    item = asset(origin, sourceUrl=origin.url + "/index.mpd", kind="video", engine="yt-dlp")
    downloader = Downloader(tmp_path / "data", Browser(), Policy())
    task = asyncio.create_task(downloader.describe_options(item))
    assert await asyncio.to_thread(origin.stall_started.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((tmp_path / "data").iterdir())


@pytest.mark.asyncio
async def test_http_options_are_explicitly_not_applied(tmp_path, origin):
    item = asset(origin, _downloadOptions={"maxHeight": 144, "audioLanguage": "zh"})
    downloader = Downloader(tmp_path / "data", Browser(), Policy())
    info = await downloader.describe_options(item)
    assert not info["selectable"] and not origin.requests
    result = await downloader.download(item, control(), ignore)
    assert result["selection"] == {
        "protocol": "http",
        "height": None,
        "audioLanguage": "und",
        "optionsApplied": False,
        "message": "固定源文件，不执行画质转换或音轨替换",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,manifest", [("hls", "hls/master.m3u8"), ("dash", "dash/index.mpd")]
)
async def test_same_selection_reuses_verified_tracks_after_restart(
    tmp_path, origin, multi_sources, protocol, manifest
):
    item = asset(
        origin,
        sourceUrl=origin.url + "/" + manifest,
        kind="video",
        engine="yt-dlp",
        _downloadOptions={"maxHeight": 144, "audioLanguage": "zh"},
    )
    root = tmp_path / "data"
    first = control()

    async def pause(progress):
        if progress.get("completedSegments", 0) >= 2:
            first.paused = True

    with pytest.raises(DownloadPaused):
        await Downloader(root, Browser(), Policy()).download(item, first, pause)
    start = len(origin.requests)
    events = []

    async def collect(event):
        events.append(event)

    result = await Downloader(root, Browser(), Policy()).download(item, control(), collect)
    assert any(event.get("resumedBytes", 0) > 0 for event in events)
    assert any(request["status"] == 304 for request in origin.requests[start:])
    inspect_output(root / result["path"], height=144, language="zho")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,manifest", [("hls", "hls/master.m3u8"), ("dash", "dash/index.mpd")]
)
async def test_inaccurate_height_cannot_replace_verified_original(
    tmp_path, origin, multi_sources, protocol, manifest
):
    root = tmp_path / "data"
    item = asset(
        origin,
        sourceUrl=origin.url + "/" + manifest,
        kind="video",
        engine="yt-dlp",
        _downloadOptions={"maxHeight": 144, "audioLanguage": "zh"},
    )
    downloader = Downloader(root, Browser(), Policy())
    result = await downloader.download(item, control(), ignore)
    output = root / result["path"]
    before = output.read_bytes()
    body, mime, etag = origin.routes["/" + manifest]
    if protocol == "hls":
        body = body.replace(b"RESOLUTION=512x288", b"RESOLUTION=512x144")
    else:
        body = body.replace(b'height="144"', b'height="720"').replace(
            b'height="288"', b'height="144"'
        )
    origin.routes["/" + manifest] = (body, mime, etag)
    with pytest.raises(ValueError, match="实际视频分辨率超过"):
        await downloader.download(item, control(), ignore)
    assert output.read_bytes() == before
    assert not list(output.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_inspect_enforces_manifest_size_limit_and_cleans_data(tmp_path, origin):
    from backend.dash import MAX_MANIFEST_BYTES

    body = b"x" * (MAX_MANIFEST_BYTES + 1)
    origin.routes["/index.mpd"] = (body, "application/dash+xml", '"large"')
    item = asset(origin, sourceUrl=origin.url + "/index.mpd", kind="video", engine="yt-dlp")
    root = tmp_path / "data"
    with pytest.raises(ValueError, match="大小|限制"):
        await Downloader(root, Browser(), Policy()).describe_options(item)
    assert len(origin.requests) == 1
    assert not list(root.iterdir())


def test_private_selection_rejects_overlong_language_like_api():
    from backend.selection import normalize_selection

    with pytest.raises(ValueError, match="语言"):
        normalize_selection({"audioLanguage": "en-abcdefgh-abcdefgh-abcdefgh-abcdefgh"})


@pytest.mark.asyncio
async def test_hls_inspection_hides_unselectable_inband_audio(tmp_path, origin, multi_sources):
    body, mime, etag = origin.routes["/hls/master.m3u8"]
    origin.routes["/hls/master.m3u8"] = (
        body.replace(b',URI="en.m3u8"', b"").replace(b',URI="zh.m3u8"', b""),
        mime,
        etag,
    )
    item = asset(origin, sourceUrl=origin.url + "/hls/master.m3u8", kind="video", engine="yt-dlp")
    info = await Downloader(tmp_path / "data", Browser(), Policy()).describe_options(item)
    assert info["heights"] == [144, 288]
    assert info["audioTracks"] == []
    assert "内嵌" in info["message"]
    assert len(origin.requests) == 1
