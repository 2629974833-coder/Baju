# Pytest discovers shared fixtures imported below by their argument names.
# ruff: noqa: F811
import importlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from yt_dlp import YoutubeDL

from backend.short_media import _ShortInstagramIE
from tests_backend.test_download_resume import origin  # noqa: F401
from tests_backend.test_youtube_media import movie_tracks, staged_transfer  # noqa: F401

VIDEO = "https://cdn.example.test/video.mp4"
AUDIO = "https://cdn.example.test/audio.m4a"


def manifest(extra="", video_url=VIDEO, audio_url=AUDIO):
    return f'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
      <Period><AdaptationSet mimeType="video/mp4" codecs="avc1.64001f">
        <Representation id="v1" width="1280" height="720" bandwidth="1500000">
          <BaseURL>{video_url}</BaseURL><SegmentBase indexRange="100-200"><Initialization range="0-99"/></SegmentBase>
          {extra}
        </Representation></AdaptationSet><AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2" lang="en">
        <Representation id="a1" bandwidth="128000"><BaseURL>{audio_url}</BaseURL></Representation>
      </AdaptationSet></Period></MPD>'''


def parse_media(xml=None, **extra):
    adapter = _ShortInstagramIE(YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False))
    media = {"pk": "123", "media_type": 2, "has_audio": True, "video_duration": 2,
             "video_versions": [{"id": "progressive", "width": 640, "height": 360, "url": VIDEO}], **extra}
    if xml is not None:
        media["video_dash_manifest"] = xml
    return adapter._extract_product_media(media)


def test_instagram_inline_dash_pairs_original_audio_without_network(monkeypatch):
    from yt_dlp.extractor.common import InfoExtractor

    def reject_request(*_args, **_kwargs):
        pytest.fail("inline MPD parser must not make network requests")
    monkeypatch.setattr(InfoExtractor, "_request_webpage", reject_request)
    info = parse_media(manifest())
    pairs = [f for f in info["formats"] if f.get("_instagramAudio")]
    assert pairs and any(f.get("height") == 720 for f in pairs)
    assert all(f["_instagramAudio"]["url"] == AUDIO for f in pairs)
    assert all(f["_instagramAudioExpected"] is True and f["_instagramDuration"] == 2 for f in pairs)
    assert all("+dash-a1" in f["format_id"] for f in pairs)


@pytest.mark.parametrize("xml", [
    manifest('<ContentProtection schemeIdUri="urn:uuid:test"/>'),
    manifest('<SegmentTemplate media="segment-$Number$.m4s" duration="1"/>'),
    manifest().replace('type="static"', 'type="dynamic"'),
    manifest().replace('</Period>', '</Period><Period/>'),
    '<!DOCTYPE MPD [<!ENTITY x "value">]>' + manifest(),
    manifest(video_url="http://127.0.0.1/private.mp4"),
])
def test_unsupported_inline_manifest_never_falls_back_to_silent_video(xml):
    import backend.short_media as module

    error = getattr(module, "ShortMediaUnsupported", None)
    assert error is not None, "need explicit unsupported signal instead of embed fallback"
    with pytest.raises(error):
        parse_media(xml)


def test_missing_inline_audio_is_not_treated_as_confirmed_silence():
    import backend.short_media as module

    error = getattr(module, "ShortMediaUnsupported", None)
    assert error is not None
    xml = manifest().replace('mimeType="audio/mp4" codecs="mp4a.40.2"', 'mimeType="text/vtt" codecs="wvtt"')
    with pytest.raises(error):
        parse_media(xml)


def test_explicit_silence_and_unknown_audio_remain_distinct():
    silent = parse_media(has_audio=False)
    unknown = parse_media(has_audio=None)
    assert silent["formats"][0].get("_instagramAudioExpected") is False
    assert unknown["formats"][0].get("_instagramAudioExpected") is None
    assert parse_media()["formats"][0].get("_instagramAudioExpected") is True


def test_official_embed_carries_audio_evidence_and_inline_tracks(monkeypatch):
    from backend.short_media import short_target

    adapter = _ShortInstagramIE(YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False))
    media = {"shortcode": "B7", "id": "123", "__typename": "GraphVideo", "is_video": True,
             "video_url": VIDEO, "has_audio": True, "video_duration": 2,
             "dash_info": {"video_dash_manifest": manifest()}}
    html = '<script>' + json.dumps({"contextJSON": json.dumps({"gql_data": {"shortcode_media": media}})}) + '</script>'
    monkeypatch.setattr(adapter, "_download_webpage", lambda *_a, **_kw: html)
    entries = adapter.read_embed(short_target("https://www.instagram.com/reel/B7/"))
    assert entries[0]["id"] == "123"
    assert entries[0].get("formats") and any(f.get("_instagramAudio") for f in entries[0]["formats"])


def test_logged_out_instagram_uses_one_fixed_official_read_query(monkeypatch):
    from backend.short_media import short_target

    adapter = _ShortInstagramIE(YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False))
    adapter.has_session = False
    calls = []
    def homepage(url, *_a, **_kw):
        calls.append(("GET", url))
        return '<script id="__eqmc">{"l":"temporary-fixture-lsd"}</script>'
    def query(url, *_a, **kwargs):
        calls.append(("POST", url))
        payload = parse_qs(kwargs["data"].decode())
        assert payload["doc_id"] == ["27130156389949648"]
        assert json.loads(payload["variables"][0]) == {"media_id": "123"}
        assert kwargs["headers"]["X-FB-LSD"] == "temporary-fixture-lsd"
        assert "impersonate" not in kwargs
        return {"data": {"xig_polaris_media": {"if_not_gated_logged_out": {
            "pk": "123", "media_type": 2, "has_audio": False, "video_duration": 2,
            "video_versions": [{"id": "native", "url": VIDEO, "width": 320, "height": 180}],
        }}}}
    monkeypatch.setattr(adapter, "_download_webpage", homepage)
    monkeypatch.setattr(adapter, "_download_json", query)
    monkeypatch.setattr(adapter, "_real_extract", lambda *_: pytest.fail("public product already resolved"))
    entries = adapter.read_post(short_target("https://www.instagram.com/reel/B7/"))
    assert entries[0]["id"] == "B7"
    assert entries[0]["formats"][0]["_instagramAudioExpected"] is False
    assert calls == [("GET", "https://www.instagram.com/"), ("POST", "https://www.instagram.com/api/graphql")]
    assert "temporary-fixture-lsd" not in json.dumps(entries)


def test_missing_visitor_token_uses_existing_native_flow_without_post(monkeypatch):
    from backend.short_media import short_target

    adapter = _ShortInstagramIE(YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False))
    adapter.has_session = False
    monkeypatch.setattr(adapter, "_download_webpage", lambda *_a, **_kw: '<title>Public page</title>')
    monkeypatch.setattr(adapter, "_download_json", lambda *_a, **_kw: pytest.fail("no token means no extra POST"))
    monkeypatch.setattr(adapter, "_real_extract", lambda *_: {"id": "B7", "url": VIDEO})
    assert adapter.read_post(short_target("https://www.instagram.com/reel/B7/"))[0]["url"] == VIDEO


def ig_transfer(tmp_path, origin, movie_tracks, callback):
    downloader, transfer, partial, cache, server = staged_transfer(tmp_path, origin, movie_tracks, callback)
    transfer.asset.update(
        id="ig-test", siteId="instagram", pageUrl="https://www.instagram.com/reel/B7/",
        _protocol="instagram", _resolver="yt-dlp:instagram",
        _instagramAudio={"url": transfer.asset["_youtubeFormats"][1]["url"], "formatId": "audio", "language": "en"},
        _instagramDuration=2, _instagramAudioExpected=True,
    )
    transfer.asset.pop("_youtubeFormats")
    return downloader, transfer, partial, cache, server


async def noop(_event):
    pass


async def test_instagram_real_split_tracks_mux_to_original_audio_mp4(tmp_path, origin, movie_tracks):
    module = importlib.import_module("backend.instagram_media")
    downloader, transfer, partial, cache, server = ig_transfer(tmp_path, origin, movie_tracks, noop)
    original = transfer.asset
    expected = await module.download_instagram(downloader, transfer, partial, cache)
    info = json.loads(await downloader._process([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(partial)]))
    assert expected == 2 and transfer.asset is original
    assert {s["codec_name"] for s in info["streams"]} == {"h264", "aac"}
    assert [r["path"] for r in server.requests] == ["/137", "/140"]


async def test_instagram_separate_audio_counts_toward_aggregate_download_budget(tmp_path, origin, movie_tracks):
    module = importlib.import_module("backend.instagram_media")
    downloader, transfer, partial, cache, _ = ig_transfer(tmp_path, origin, movie_tracks, noop)
    downloader.max_bytes = sum(map(len, movie_tracks)) - 1
    with pytest.raises(ValueError, match="大小限制"):
        await module.download_instagram(downloader, transfer, partial, cache)
    assert not partial.exists()


async def test_instagram_pause_preserves_tracks_and_resumes_with_checkpoint(tmp_path, origin, movie_tracks):
    import time

    from backend.downloader import DownloadPaused, _Transfer

    module = importlib.import_module("backend.instagram_media")
    transfer = None
    async def pause(event):
        if event["downloadedBytes"]:
            transfer.control.paused = True
    downloader, transfer, partial, cache, server = ig_transfer(tmp_path, origin, movie_tracks, pause)
    original = transfer.asset
    with pytest.raises(DownloadPaused):
        await module.download_instagram(downloader, transfer, partial, cache)
    assert transfer.asset is original and any(cache.rglob("*.json"))
    restarted = _Transfer(original, SimpleNamespace(paused=False, cancelled=False), noop, started=time.monotonic())
    await module.download_instagram(downloader, restarted, partial, cache)
    assert restarted.details["resumedBytes"] > 0 and partial.is_file()


@pytest.mark.parametrize("failure", ["missing-audio", "truncated-audio", "duration-mismatch"])
async def test_incomplete_instagram_never_replaces_saved_original(tmp_path, origin, movie_tracks, failure):
    import subprocess

    downloader, transfer, _, _, server = ig_transfer(tmp_path, origin, movie_tracks, noop)
    saved = tmp_path / "assets" / "ig-test" / "original.mp4"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"previous verified original")
    if failure == "missing-audio":
        server.routes["/140"] = (movie_tracks[0], "video/mp4", '"wrong-track"')
    elif failure == "truncated-audio":
        full, short = tmp_path / "full.m4a", tmp_path / "short.m4a"
        full.write_bytes(movie_tracks[1])
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(full), "-t", "0.1",
                        "-c", "copy", str(short)], check=True)
        server.routes["/140"] = (short.read_bytes(), "audio/mp4", '"short-track"')
    else:
        transfer.asset.update(_instagramDuration=19, duration="0:19")
    with pytest.raises(ValueError):
        await downloader.download(transfer.asset, transfer.control, noop)
    assert saved.read_bytes() == b"previous verified original"
    assert not (saved.parent / ".hls").exists() and not (saved.parent / "original.part").exists()


async def test_instagram_outer_download_cancel_cleans_tracks(tmp_path, origin, movie_tracks):
    from backend.downloader import DownloadCancelled

    downloader, transfer, _, _, _ = ig_transfer(tmp_path, origin, movie_tracks, noop)
    async def cancel(event):
        if event["downloadedBytes"]:
            transfer.control.cancelled = True
    with pytest.raises(DownloadCancelled):
        await downloader.download(transfer.asset, transfer.control, cancel)
    folder = tmp_path / "assets" / "ig-test"
    assert not (folder / ".hls").exists() and not (folder / "original.part").exists()
