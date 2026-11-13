"""Public metadata omissions are not evidence that an account is required."""

import io
import json
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import ExtractorError

import backend.short_media as media

DY = "https://www.douyin.com/video/123"
TT = "https://www.tiktok.com/@garden/video/123"
MESSAGE = "暂未取得公开媒体，请稍后重试"


def adapter(platform):
    return media.SHORT_EXTRACTORS[platform](
        YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False)
    )


def assert_unavailable(call):
    with pytest.raises(ExtractorError) as error:
        call()
    assert not isinstance(error.value, media.ShortAccessRequired)
    assert type(error.value) is media.ShortMetadataUnavailable
    assert str(error.value) == MESSAGE


def tiktok_page(detail):
    state = {"__DEFAULT_SCOPE__": {"webapp.video-detail": detail}}
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(state) + "</script>"


@pytest.mark.parametrize(
    "detail", [{}, {"statusCode": 0}, {"statusCode": 99999}, {"statusCode": 10204}]
)
def test_tiktok_empty_metadata_unknown_status_and_ip_block_do_not_require_login(
    monkeypatch, detail
):
    parser = adapter("tiktok")
    monkeypatch.setattr(parser, "_download_webpage", lambda *_: tiktok_page(detail))
    assert_unavailable(lambda: parser.read_post(media.short_target(TT)))


def test_tiktok_ordinary_html_shell_is_not_a_login_challenge(monkeypatch):
    parser = adapter("tiktok")
    monkeypatch.setattr(
        parser,
        "_download_webpage",
        lambda *_: (
            '<title>Public work</title><script>const names=["captcha","login","verify"];</script>'
        ),
    )
    assert_unavailable(lambda: parser.read_post(media.short_target(TT)))


@pytest.mark.parametrize("status", [10216, 10222])
def test_tiktok_explicit_private_post_or_account_still_requires_access(monkeypatch, status):
    parser = adapter("tiktok")
    monkeypatch.setattr(parser, "_download_webpage", lambda *_: tiktok_page({"statusCode": status}))
    with pytest.raises(media.ShortAccessRequired):
        parser.read_post(media.short_target(TT))


def test_tiktok_public_work_remains_available_when_scripts_mention_login(monkeypatch):
    parser = adapter("tiktok")
    item = {
        "id": "123",
        "desc": "Garden",
        "video": {
            "playAddr": "https://cdn.example.test/clip.mp4",
            "width": 320,
            "height": 180,
            "duration": 1,
        },
    }
    monkeypatch.setattr(
        parser,
        "_download_webpage",
        lambda *_: (
            tiktok_page({"statusCode": 0, "itemInfo": {"itemStruct": item}})
            + '<script>const routes=["captcha","login"];</script>'
        ),
    )
    result = parser.read_post(media.short_target(TT))
    assert len(result) == 1 and result[0]["id"] == "123"


@pytest.mark.parametrize("payload", [{}, {"aweme_detail": None}, {"status_code": 99999}])
def test_douyin_empty_detail_does_not_require_login(monkeypatch, payload):
    parser = adapter("douyin")
    monkeypatch.setattr(parser, "_share_item", lambda *_: None)
    monkeypatch.setattr(parser, "_download_json", lambda *_, **__: payload)
    assert_unavailable(lambda: parser.read_post(media.short_target(DY)))


@pytest.mark.parametrize("status", [0, 99999])
def test_douyin_empty_share_payload_and_unknown_business_status_are_unavailable(
    monkeypatch, status
):
    parser = adapter("douyin")
    state = {
        "loaderData": {
            "video_(id)/page": {
                "itemId": "123",
                "videoInfoRes": {"status_code": status, "item_list": []},
            }
        }
    }
    monkeypatch.setattr(
        parser,
        "_download_webpage_handle",
        lambda *_, **__: (
            "<script>window._ROUTER_DATA = " + json.dumps(state) + ";</script>",
            SimpleNamespace(headers={}),
        ),
    )
    assert_unavailable(lambda: parser.read_post(media.short_target(DY)))


@pytest.mark.parametrize("status", [401, 402, 403])
def test_douyin_explicit_http_access_gate_still_requires_access(monkeypatch, status):
    parser = adapter("douyin")

    def denied(*_args, **_kwargs):
        response = Response(io.BytesIO(b"private"), DY, {}, status)
        raise ExtractorError("HTTP access gate", cause=HTTPError(response))

    monkeypatch.setattr(parser, "_download_webpage_handle", denied)
    with pytest.raises(media.ShortAccessRequired):
        parser.read_post(media.short_target(DY))


def test_douyin_transport_failure_followed_by_empty_detail_is_not_a_login_gate(monkeypatch):
    parser = adapter("douyin")

    def unavailable(*_args, **_kwargs):
        raise ExtractorError("temporary transport failure")

    monkeypatch.setattr(parser, "_download_webpage_handle", unavailable)
    monkeypatch.setattr(parser, "_download_json", lambda *_, **__: {"aweme_detail": None})
    assert_unavailable(lambda: parser.read_post(media.short_target(DY)))
