import json
from urllib.parse import urlencode

import httpx
import pytest
from yt_dlp.extractor.instagram import _id_to_pk
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import UnsupportedRequest

from backend.short_media import short_target
from tests_backend import test_youtube_bridge

bridge_factory = test_youtube_bridge.bridge_factory
PAGE = "https://www.instagram.com/p/BQ0eAlwhDrw/"
QUERY = "https://www.instagram.com/api/graphql"


def payload(**extra):
    return urlencode({"lsd": "temporary-visitor-token", "fb_api_caller_class": "RelayModern",
                      "fb_api_req_friendly_name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
                      "server_timestamps": "true", "doc_id": "27130156389949648",
                      "variables": json.dumps({"media_id": str(_id_to_pk("BQ0eAlwhDrw"))}), **extra}).encode()


async def test_instagram_exact_read_query_works_without_browser_impersonation(bridge_factory):
    make, sent = bridge_factory
    bridge = make()
    bridge.url = PAGE
    bridge.post_target = short_target(PAGE)
    response = await bridge.request(Request(QUERY, method="POST", data=payload()), {
        "Content-Type": "application/x-www-form-urlencoded", "X-FB-LSD": "temporary-visitor-token",
        "X-FB-Friendly-Name": "PolarisLoggedOutDesktopWWWPostRootContentQuery", "X-Unrelated": "ignore",
    })
    assert response.status == 200 and len(sent) == 1
    assert sent[0].headers["x-fb-lsd"] == "temporary-visitor-token"
    assert "x-unrelated" not in sent[0].headers
    assert not bridge.read_only_blocked


@pytest.mark.parametrize("url,body", [
    (QUERY, payload(doc_id="different-operation")),
    (QUERY, payload(variables=json.dumps({"media_id": "123"}))),
    (QUERY, payload(variables=json.dumps({"media_id": str(_id_to_pk("BQ0eAlwhDrw")), "action": "purchase"}))),
    (QUERY, payload(action="like")),
    (QUERY, payload() + b"&doc_id=27130156389949648"),
    ("https://www.instagram.com/api/v1/web/likes/123/like/", payload()),
    ("https://i.instagram.com/api/graphql", payload()),
    ("http://www.instagram.com/api/graphql", payload()),
])
async def test_instagram_read_exception_rejects_other_posts_and_operations(bridge_factory, url, body):
    make, sent = bridge_factory
    bridge = make()
    bridge.url = PAGE
    bridge.post_target = short_target(PAGE)
    with pytest.raises(UnsupportedRequest):
        await bridge.request(Request(url, method="POST", data=body), {})
    assert sent == []


async def test_instagram_visitor_grants_are_ephemeral_and_never_import_an_account(bridge_factory):
    make, sent = bridge_factory
    bridge = make()
    bridge.url = PAGE
    bridge.post_target = short_target(PAGE)
    response = httpx.Response(200, request=httpx.Request("GET", "https://www.instagram.com/"), headers=[
        ("set-cookie", "csrftoken=visitor-csrf; Domain=.instagram.com; Path=/; Secure"),
        ("set-cookie", "mid=visitor-id; Domain=.instagram.com; Path=/; Secure"),
        ("set-cookie", "sessionid=must-not-import; Domain=.instagram.com; Path=/; Secure"),
    ])
    bridge._capture_post_cookie(response)
    await bridge.request(Request(QUERY, method="POST", data=payload()), {})
    assert sent[0].headers["x-csrftoken"] == "visitor-csrf"
    assert "mid=visitor-id" in sent[0].headers["cookie"]
    assert "sessionid" not in sent[0].headers["cookie"]
    assert await bridge._cookies("https://cdn.example.test/video.mp4") == ""
    assert await bridge._cookies("https://i.instagram.com/") == ""
    assert make().visitor_cookies == {}
