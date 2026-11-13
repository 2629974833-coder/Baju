import httpx
import pytest

from backend.classification import automatic_classification
from backend.lustflix_access import probe_lustflix_episode
from backend.site_adapters import stable_asset_identity

SLUG = "series-00000000-0000-0000-0000-000000000123"
SITE = {"id": "lustflix", "url": "https://lustflix.tv"}
BASE = SITE["url"]
SHOW = BASE + "/#/watch/" + SLUG + "/"
MEDIA = f"/api/media/{SLUG}/1/stream.m3u8"


class Session:
    async def cookies_for(self, site_id, url):
        assert site_id == "lustflix" and url.startswith(BASE + "/")
        return [{"name": "session", "value": "fixture"}]

    async def validate(self, url):
        assert url.startswith((BASE + "/", "https://cdn.example/"))
        return url


async def test_official_account_grants_scan_free_and_locked_once_without_writes(monkeypatch):
    seen, assets, cache = [], [], {}

    def serve(request):
        path = request.url.path
        seen.append((request.method, path))
        if request.url.host == "cdn.example":
            assert "cookie" not in request.headers
            return httpx.Response(
                200, text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=90000\nvideo.m3u8"
            )
        assert request.headers.get("cookie") == "session=fixture"
        if path == "/api/content":
            return httpx.Response(
                200,
                json={
                    "series": {"slug": SLUG},
                    "episodes": [
                        {"episode": 1, "available": True, "priceCoins": 0},
                        {"episode": 2, "available": True, "priceCoins": 5},
                    ],
                    "media": [{"episode": 1, "src": MEDIA}],
                },
            )
        assert path == MEDIA
        return httpx.Response(307, headers={"Location": "https://cdn.example/signed/master.m3u8"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(serve), **kw)
    )

    async def collect(asset):
        assets.append(asset)

    for num, status in [(1, "ready"), (2, "needs-user")]:
        url = SHOW + str(num)
        report = await probe_lustflix_episode(
            SITE, url, cache, Session(), Session(), collect, lambda: False
        )
        assert report["_episodeOutcomes"][url]["status"] == status
    assert seen == [("GET", "/api/content"), ("GET", MEDIA), ("GET", "/signed/master.m3u8")]
    assert len(assets) == 1 and assets[0]["sourceUrl"] == BASE + MEDIA
    assert (
        stable_asset_identity(assets[0])
        and automatic_classification(assets[0])["kind"] == "episode"
    )
    wrong = {**assets[0], "pageUrl": SHOW.replace(SLUG, "different-show") + "1"}
    assert (
        stable_asset_identity(wrong) is None
        and automatic_classification(wrong)["kind"] != "episode"
    )


@pytest.mark.parametrize(
    "src",
    [
        "/api/media/other/1/stream.m3u8",
        "//other.example/video.mp4",
        "/api/media/test/1/preview.m3u8",
    ],
)
async def test_unknown_or_unrelated_grant_falls_back_without_following_it(src):
    class NoRequests(Session):
        async def validate(self, url):
            raise AssertionError(url)

    payload = {
        "series": {"slug": SLUG},
        "episodes": [{"episode": 1, "priceCoins": 0, "available": True}],
        "media": [{"episode": 1, "src": src}],
    }
    assert (
        await probe_lustflix_episode(
            SITE, SHOW + "1", {SLUG: payload}, Session(), NoRequests(), None, lambda: False
        )
        is None
    )


async def test_purchased_episode_uses_the_account_grant_and_keeps_identity_on_renewal(
    monkeypatch, tmp_path
):
    from backend.scanner import Scanner
    from backend.site_adapters import refresh_identity_matches
    from backend.store import Store

    path = f"/api/playback-media/old.grant/{SLUG}/4/stream.m3u8"
    renewed = path.replace("old.grant", "new.grant")
    page = SHOW + "4"
    row = {"episode": 4, "available": True, "priceCoins": 5, "viewerPriceCoins": 4}

    def serve(request):
        assert request.method == "GET" and request.headers["cookie"] == "session=fixture"
        if request.url.path == "/api/content":
            return httpx.Response(
                200,
                json={
                    "series": {"slug": SLUG},
                    "episodes": [row],
                    "media": [{"episode": 4, "src": renewed}],
                },
            )
        assert request.url.path == renewed
        return httpx.Response(200, text="#EXTM3U\n#EXTINF:150,\nseg.ts\n#EXT-X-ENDLIST")

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(serve), **kw)
    )
    old = {
        "kind": "video",
        "engine": "yt-dlp",
        "pageUrl": page,
        "sourceUrl": BASE + path,
        "_readOnly": True,
    }
    new = {**old, "sourceUrl": BASE + renewed}
    assert stable_asset_identity(old) and refresh_identity_matches(old, new)
    assert not stable_asset_identity({**new, "pageUrl": page.replace(SLUG, "other-show")})
    assert (
        automatic_classification({**new, "pageUrl": page.replace(SLUG, "other-show")})["kind"]
        != "episode"
    )
    result = await Scanner(Session(), Session()).resolve_asset(SITE, old, lambda: False)
    assert result["status"] == "resolved" and result["candidate"]["sourceUrl"] == BASE + renewed
    store = Store(tmp_path / "workspace.sqlite3")
    store.mutate(
        lambda state: state["assets"].append(
            {**result["candidate"], "id": "checked", "siteId": "lustflix"}
        )
    )
    assert "sourceUrl" not in store.public_state()["assets"][0]
    assert store.read()["assets"][0]["sourceUrl"] == BASE + renewed
