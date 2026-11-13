"""Static DASH uses the installed parser and the existing constrained transport."""

import hashlib
import json
import shutil
import subprocess

import pytest
from yt_dlp.extractor.common import InfoExtractor

from tests_backend import test_download_resume as resume_fixtures
from tests_backend import test_downloader as download_fixtures
from tests_backend.test_download_resume import (
    Browser,
    Policy,
    asset,
    control,
    ignore,
)

origin = resume_fixtures.origin
redirect_origin = download_fixtures.server


def manifest(
    segment_xml=None, *, attributes='type="static" mediaPresentationDuration="PT3S"', extra=""
):
    segment_xml = segment_xml or (
        '<SegmentTemplate initialization="init-$RepresentationID$.mp4" '
        'media="chunk-$RepresentationID$-$Number%05d$.m4s" timescale="10">'
        '<SegmentTimeline><S t="0" d="10" r="2"/></SegmentTimeline></SegmentTemplate>'
    )
    return (
        f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" {attributes}>{extra}'
        '<Period><AdaptationSet mimeType="video/mp4" codecs="avc1.64000a">'
        f'{segment_xml}<Representation id="v1" bandwidth="1000" width="64" height="48"/>'
        '<Representation id="v2" bandwidth="2000" width="128" height="96"/>'
        '</AdaptationSet><AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2">'
        f'{segment_xml}<Representation id="a1" bandwidth="500"/>'
        "</AdaptationSet></Period></MPD>"
    ).encode()


def test_real_parser_expands_templates_and_selects_one_video_and_audio(monkeypatch):
    from backend.dash import parse_mpd

    original, calls = InfoExtractor._parse_mpd_formats_and_subtitles, []

    def observed(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InfoExtractor, "_parse_mpd_formats_and_subtitles", observed)
    tracks = parse_mpd(manifest(), "https://media.example.test/sub/index.mpd")
    assert calls == [True]
    assert [track["format_id"] for track in tracks] == ["v2", "a1"]
    assert all(len(track["fragments"]) == 4 for track in tracks)
    assert tracks[0]["fragments"][0]["url"] == "https://media.example.test/sub/init-v2.mp4"
    assert tracks[0]["fragments"][-1]["url"].endswith("chunk-v2-00003.m4s")


@pytest.mark.parametrize(
    "payload,reason",
    [
        (manifest(attributes='type="dynamic"'), "动态|直播|static"),
        (
            manifest(extra='<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>'),
            "DRM",
        ),
        (manifest().replace(b"</MPD>", b"<Period/></MPD>"), "Period"),
        (manifest('<SegmentBase indexRange="0-100"/>'), "范围|SegmentBase"),
        (
            manifest(
                '<SegmentList><Initialization sourceURL="init" range="0-10"/><SegmentURL media="seg"/></SegmentList>'
            ),
            "范围",
        ),
        (manifest().replace(b'r="2"', b'r="999999999999"'), "限制|数量"),
        (manifest().replace(b'r="2"', b'r="-1"'), "重复|范围|限制"),
        (manifest().replace(b"$Number%05d$", b"$Number%0999999999d$"), "模板|限制"),
        (
            manifest(
                '<SegmentTemplate initialization="init" media="s-$Number$.m4s" timescale="1000000000" duration="1"/>'
            ),
            "限制|数量",
        ),
        (b'<!DOCTYPE MPD [<!ENTITY x "test">]>' + manifest(), "XML"),
        (manifest().replace(b'timescale="10"', b'timescale="0"'), "无效|范围"),
    ],
)
def test_unsupported_or_excessive_mpd_is_rejected_before_sdk_expansion(
    monkeypatch, payload, reason
):
    from backend.dash import parse_mpd

    def forbidden(*_args, **_kwargs):
        pytest.fail("unsafe MPD reached the expanding SDK parser")

    monkeypatch.setattr(InfoExtractor, "_parse_mpd_formats_and_subtitles", forbidden)
    with pytest.raises(ValueError, match=reason):
        parse_mpd(payload, "https://media.example.test/index.mpd")


def test_segment_list_uses_mature_parser():
    from backend.dash import parse_mpd

    listing = '<SegmentList duration="10" timescale="10"><Initialization sourceURL="init.mp4"/><SegmentURL media="one.m4s"/><SegmentURL media="two.m4s"/></SegmentList>'
    tracks = parse_mpd(manifest(listing), "https://media.example.test/path/index.mpd")
    assert [part["url"] for part in tracks[0]["fragments"]] == [
        "https://media.example.test/path/init.mp4",
        "https://media.example.test/path/one.m4s",
        "https://media.example.test/path/two.m4s",
    ]


def dash_fixture(tmp_path, origin):
    fixture = tmp_path / "dash-source"
    fixture.mkdir()
    subprocess.run(
        [
            shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=10:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-c:a",
            "aac",
            "-seg_duration",
            "1",
            "-use_template",
            "1",
            "-use_timeline",
            "1",
            "-adaptation_sets",
            "id=0,streams=v id=1,streams=a",
            "-f",
            "dash",
            str(fixture / "index.mpd"),
        ],
        check=True,
    )
    for path in fixture.iterdir():
        body = path.read_bytes()
        mime = "application/dash+xml" if path.suffix == ".mpd" else "application/octet-stream"
        origin.routes["/" + path.name] = (body, mime, '"' + hashlib.sha256(body).hexdigest() + '"')
    return asset(origin, sourceUrl=origin.url + "/index.mpd", kind="video", engine="yt-dlp")


@pytest.mark.asyncio
async def test_real_dash_download_contains_audio_video_and_fully_decodes(tmp_path, origin):
    from backend.downloader import Downloader

    item = dash_fixture(tmp_path, origin)
    data = tmp_path / "data"
    result = await Downloader(data, Browser(), Policy()).download(item, control(), ignore)
    assert result["duration"] >= 2.9
    assert (result["width"], result["height"]) == (64, 48)
    target = data / result["path"]
    probe = subprocess.run(
        [Downloader._binary("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(target)],
        check=True,
        capture_output=True,
    )
    assert {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]} == {
        "video",
        "audio",
    }
    subprocess.run(
        [
            Downloader._binary("ffmpeg"),
            "-v",
            "error",
            "-xerror",
            "-i",
            str(target),
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    assert not (target.parent / ".hls").exists()


def test_representation_id_cannot_inject_unbounded_template_width(monkeypatch):
    from backend.dash import parse_mpd

    def forbidden(*_args, **_kwargs):
        pytest.fail("representation injection reached the expanding parser")

    monkeypatch.setattr(InfoExtractor, "_parse_mpd_formats_and_subtitles", forbidden)
    payload = manifest().replace(b'id="v1"', b'id="$Number%0999999999d$"')
    with pytest.raises(ValueError, match="表示|模板"):
        parse_mpd(payload, "https://media.example.test/index.mpd")


async def pause_dash(data, item):
    from backend.downloader import Downloader, DownloadPaused

    operation = control()

    async def pause(event):
        if event.get("completedSegments", 0) >= 2:
            operation.paused = True

    with pytest.raises(DownloadPaused):
        await Downloader(data, Browser(), Policy()).download(item, operation, pause)


@pytest.mark.asyncio
async def test_dash_pause_resume_reuses_strongly_validated_init_and_segments(tmp_path, origin):
    from backend.downloader import Downloader

    item = dash_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_dash(data, item)
    cached = [request for request in origin.requests if not request["path"].endswith(".mpd")]
    assert len(cached) == 2
    folder = data / "assets/resume_asset"
    for checkpoint in (folder / ".hls").rglob("*.json"):
        assert checkpoint.stat().st_mode & 0o077 == 0
        assert origin.url not in checkpoint.read_text()
    before = len(origin.requests)
    events = []

    async def collect(event):
        events.append(event)

    result = await Downloader(data, Browser(), Policy()).download(item, control(), collect)
    resumed = origin.requests[before:]
    for original in cached:
        request = next(request for request in resumed if request["path"] == original["path"])
        assert request["status"] == 304 and request["bytes"] == 0
        assert request["headers"].get("If-None-Match")
    assert any(event.get("resumedBytes", 0) > 0 for event in events)
    assert result["duration"] >= 2.9


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manifest", "source", "cache", "validator", "weak-validator"])
async def test_dash_changed_identity_or_invalid_cache_never_reuses_stale_bytes(
    tmp_path, origin, change
):
    from backend.downloader import Downloader

    item = dash_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_dash(data, item)
    first = next(request for request in origin.requests if request["path"].endswith(".m4s"))
    before = len(origin.requests)
    if change == "manifest":
        body, mime, _etag = origin.routes["/index.mpd"]
        origin.routes["/index.mpd"] = (body + b"<!-- changed manifest -->", mime, '"mpd-v2"')
    elif change == "source":
        origin.routes["/fresh.mpd"] = origin.routes["/index.mpd"]
        item["sourceUrl"] = origin.url + "/fresh.mpd"
    elif change == "cache":
        for path in (data / "assets/resume_asset/.hls/objects").glob("*.bin"):
            path.write_bytes(b"corrupt")
    else:
        body, mime, _etag = origin.routes[first["path"]]
        origin.routes[first["path"]] = (
            body,
            mime,
            'W/"weak"' if change == "weak-validator" else '"v2"',
        )
    result = await Downloader(data, Browser(), Policy()).download(item, control(), ignore)
    resumed = next(
        request for request in origin.requests[before:] if request["path"] == first["path"]
    )
    assert resumed["status"] == 200 and resumed["bytes"] > 0
    if change in {"manifest", "source", "cache"}:
        assert "If-None-Match" not in resumed["headers"]
    assert result["duration"] >= 2.9


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "invalid", "auth"])
async def test_dash_cleanup_preserves_original_and_only_auth_keeps_cache(tmp_path, origin, mode):
    from backend.downloader import DownloadAccessRequired, DownloadCancelled, Downloader

    item = dash_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_dash(data, item)
    folder = data / "assets/resume_asset"
    original = folder / "original.mp4"
    original.write_bytes(b"previous verified original")
    operation = control()
    if mode == "cancel":
        operation.cancelled = True
        expected = DownloadCancelled
    elif mode == "auth":
        origin.statuses = [403]
        expected = DownloadAccessRequired
    else:
        origin.routes["/index.mpd"] = (b"not a manifest", "application/dash+xml", '"invalid"')
        expected = ValueError
    with pytest.raises(expected):
        await Downloader(data, Browser(), Policy()).download(item, operation, ignore)
    assert original.read_bytes() == b"previous verified original"
    assert (folder / ".hls").exists() == (mode == "auth")
    assert not (folder / "original.part").exists()


@pytest.mark.asyncio
async def test_dash_validates_private_segments_before_fetch(tmp_path, origin):
    from backend.downloader import Downloader

    item = dash_fixture(tmp_path, origin)
    body, mime, etag = origin.routes["/index.mpd"]
    body = body.replace(
        b'initialization="init-', b'initialization="http://169.254.169.254/private/init-'
    )
    origin.routes["/index.mpd"] = body, mime, etag
    with pytest.raises(ValueError, match="fixture origin only"):
        await Downloader(tmp_path / "data", Browser(), Policy()).download(item, control(), ignore)
    assert len(origin.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["mime", "_protocol"])
async def test_dash_route_accepts_extensionless_extractor_manifest(tmp_path, origin, hint):
    from backend.downloader import Downloader

    item = dash_fixture(tmp_path, origin)
    origin.routes["/manifest"] = origin.routes["/index.mpd"]
    item.update(sourceUrl=origin.url + "/manifest")
    item[hint] = "application/dash+xml" if hint == "mime" else "dash"
    result = await Downloader(tmp_path / "data", Browser(), Policy()).download(
        item, control(), ignore
    )
    assert result["duration"] >= 2.9


def test_expanded_representation_templates_are_bounded_before_sdk(monkeypatch):
    from backend.dash import parse_mpd

    def forbidden(*_args, **_kwargs):
        pytest.fail("oversized expanded URL reached the SDK parser")

    monkeypatch.setattr(InfoExtractor, "_parse_mpd_formats_and_subtitles", forbidden)
    payload = manifest().replace(b'id="v1"', b'id="' + b"a" * 3000 + b'"')
    payload = payload.replace(b"init-$RepresentationID$.mp4", b"$RepresentationID$" * 4)
    with pytest.raises(ValueError, match="模板|限制"):
        parse_mpd(payload, "https://media.example.test/index.mpd")


def test_equal_metadata_representations_are_not_concatenated_together():
    from backend.dash import parse_mpd

    payload = manifest().replace(
        b'bandwidth="2000" width="128" height="96"', b'bandwidth="1000" width="64" height="48"'
    )
    tracks = parse_mpd(payload, "https://media.example.test/index.mpd")
    assert len(tracks[0]["fragments"]) == 4
    selected = tracks[0]["format_id"]
    assert all(selected in fragment["url"] for fragment in tracks[0]["fragments"])


@pytest.mark.asyncio
async def test_dash_segment_redirect_is_checked_by_existing_policy(tmp_path, redirect_origin):
    from backend.downloader import Downloader

    redirect_origin.routes["/index.mpd"] = (
        200,
        {"Content-Type": "application/dash+xml"},
        manifest(),
    )
    redirect_origin.routes["/init-v2.mp4"] = (
        302,
        {"Location": "http://169.254.169.254/private"},
        b"",
    )
    item = asset(
        redirect_origin, sourceUrl=redirect_origin.url + "/index.mpd", kind="video", engine="yt-dlp"
    )
    with pytest.raises(ValueError, match="fixture origin only"):
        await Downloader(tmp_path, Browser(), Policy()).download(item, control(), ignore)
    assert [path for path, _headers in redirect_origin.requests] == ["/index.mpd", "/init-v2.mp4"]


@pytest.mark.parametrize(
    "segments,last_path",
    [
        (
            '<SegmentTemplate initialization="init.mp4" media="chunk-$Number$.m4s" '
            'timescale="10" duration="10" startNumber="7"/>',
            "chunk-9.m4s",
        ),
        (
            '<SegmentTemplate initialization="init.mp4" media="chunk-$Time$.m4s" timescale="10">'
            '<SegmentTimeline><S t="20" d="10" r="2"/></SegmentTimeline></SegmentTemplate>',
            "chunk-40.m4s",
        ),
    ],
)
def test_duration_and_time_template_forms_use_real_parser(segments, last_path):
    from backend.dash import parse_mpd

    tracks = parse_mpd(manifest(segments), "https://media.example.test/index.mpd")
    assert all(len(track["fragments"]) == 4 for track in tracks)
    assert tracks[0]["fragments"][-1]["url"].endswith(last_path)


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/local.mp4",
        "data:video/mp4,secret",
        "https://user:secret@media.example.test/init.mp4",
    ],
)
def test_parser_rejects_non_http_or_credential_bearing_fragment_urls(url):
    from backend.dash import parse_mpd

    payload = manifest().replace(b"init-$RepresentationID$.mp4", url.encode())
    with pytest.raises(ValueError, match="URL") as error:
        parse_mpd(payload, "https://media.example.test/index.mpd")
    assert "secret" not in str(error.value)
