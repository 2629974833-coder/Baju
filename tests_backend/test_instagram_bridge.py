import json
from urllib.parse import urlencode

import httpx
import pytest
from yt_dlp.extractor.instagram import _id_to_pk

from backend.short_media import SHORT_EXTRACTORS
from tests_backend import test_short_media
from tests_backend.test_short_media import IG, MEDIA

fixture = test_short_media.fixture


async def test_instagram_share_redirect_rebinds_read_query_to_canonical_post(fixture, monkeypatch):
    from backend.instagram_requests import READ_DOCUMENT_ID, READ_ENDPOINT, READ_QUERY_NAME

    def read(adapter, target):
        adapter._download_json(READ_ENDPOINT, target.post_id, data=urlencode({
            "lsd": "visitor", "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": READ_QUERY_NAME, "server_timestamps": "true",
            "doc_id": READ_DOCUMENT_ID, "variables": json.dumps({"media_id": str(_id_to_pk(target.post_id))}),
        }).encode())
        return [{"id": target.post_id, "url": MEDIA + "/movie.mp4", "ext": "mp4"}]

    monkeypatch.setattr(SHORT_EXTRACTORS["instagram"], "read_post", read)
    fixture.routes.update({
        "www.instagram.com/share/reel/abc/": httpx.Response(302, headers={"location": IG}),
        "www.instagram.com/reel/B7/": httpx.Response(200, text="post"),
        "www.instagram.com/api/graphql": httpx.Response(200, json={"ok": True}),
    })
    report, assets = await fixture.scan("https://www.instagram.com/share/reel/abc/")
    assert report["status"] == "completed" and len(assets) == 1
    assert any(request.method == "POST" for request in fixture.requests)


@pytest.mark.parametrize("audio_host,accepted", [(MEDIA, True), ("http://127.0.0.1", False)])
async def test_instagram_pair_is_preserved_and_both_sources_are_validated(fixture, monkeypatch, audio_host, accepted):
    monkeypatch.setattr(SHORT_EXTRACTORS["instagram"], "read_post", lambda *_: [{
        "id": "B7", "title": "Video with original audio", "duration": 3,
        "formats": [{"url": MEDIA + "/video.mp4", "ext": "mp4", "protocol": "https",
                     "vcodec": "avc1.64001e", "acodec": "mp4a.40.2", "format_id": "video+audio",
                     "_instagramAudio": {"url": audio_host + "/audio.m4a", "formatId": "audio", "language": "und"},
                     "_instagramDuration": 3, "_instagramAudioExpected": True}],
    }])
    _report, assets = await fixture.scan(IG)
    if not accepted:
        assert assets == []
        return
    assert len(assets) == 1
    item = assets[0]
    assert item["engine"] == "yt-dlp" and item["_protocol"] == "instagram"
    assert item["_instagramAudio"]["url"] == MEDIA + "/audio.m4a"
    assert item["_instagramDuration"] == 3 and item["_instagramAudioExpected"] is True


async def test_unsupported_known_instagram_tracks_do_not_fall_back_to_silent_embed(fixture, monkeypatch):
    import backend.short_media as social

    assert hasattr(social, "ShortMediaUnsupported")

    def unsupported(*_):
        raise social.ShortMediaUnsupported("unsupported media", expected=True)

    def no_fallback(*_):
        pytest.fail("Known unsupported media must not silently downgrade to an incomplete embed")

    monkeypatch.setattr(SHORT_EXTRACTORS["instagram"], "read_post", unsupported)
    monkeypatch.setattr(SHORT_EXTRACTORS["instagram"], "read_embed", no_fallback)
    _report, assets = await fixture.scan(IG)
    assert assets == []
