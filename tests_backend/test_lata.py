"""Lata's published hidden episode directory and exact current-player payload."""

import json

import httpx
import pytest

from backend import lata

ORIGIN = "https://lata.casa"
EP1 = ORIGIN + "/watch/garden-ep-1"
EP2 = ORIGIN + "/watch/garden-ep-2"
SHOW = ORIGIN + "/originals/garden"
SITE = {"id": "lata", "url": ORIGIN + "/series"}


def html(*, number=1, locked=False, total=2, links=None, data=None, series=None):
    player = {
        "episodeId": 40 + number,
        "filmId": 9,
        "filmTitle": "Garden",
        "episode": number,
        "isFree": number == 1,
        "locked": locked,
        "hlsUrl": None if locked else "https://media.example/movie/playlist.m3u8",
        "poster": "https://media.example/poster.jpg",
        "totalEpisodes": total,
    }
    player.update(data or {})
    show = {"@type": "TVSeries", "url": SHOW, "name": "Garden", "numberOfEpisodes": total}
    show.update(series or {})
    links = (
        links
        if links is not None
        else [
            (EP1, "EP 1", "active" if number == 1 else ""),
            (EP2, "EP 2", "locked-episode" if locked or number == 1 else "active"),
        ]
    )
    anchors = "".join(
        f'<a class="episode-item {cls}" href="{url}"><span>{label}</span></a>'
        for url, label, cls in links
    )
    return f'''<link rel="canonical" href="{EP1 if number == 1 else EP2}">
        <script type="application/ld+json">{json.dumps({"@graph": [show]})}</script>
        <div id="episode-overlay" hidden><div class="episode-grid">{anchors}</div></div>
        <a class="episode-item" href="/watch/unrelated">EP 99</a>
        <script id="current-episode-data" type="application/json">{json.dumps(player)}</script>'''


def test_route_is_official_https_and_only_recognized_pages():
    assert lata.route(SHOW) == ("series", "garden")
    assert lata.route(EP1) == ("episode", "garden-ep-1")
    assert lata.route(ORIGIN + "/series") == ("index", None)
    for bad in [
        "https://lata.casa.attacker.test/series",
        "https://user@lata.casa/series",
        "http://lata.casa/series",
        "https://lata.casa:444/series",
        ORIGIN + "/login",
        ORIGIN + "/watch/a%2fb",
    ]:
        assert lata.route(bad) is None


def test_hidden_directory_has_exact_numbered_links_poster_and_public_gate():
    result = lata.parse_page(html(), EP1)
    catalog = result["catalog"]
    assert catalog["adapterId"] == "generic" and catalog["seriesKey"] == "lata-9"
    assert catalog["catalogUrl"] == EP1 and catalog["title"] == "Garden"
    assert catalog["expectedCount"] == 2 and catalog["coverUrls"] == [
        "https://media.example/poster.jpg"
    ]
    assert [e["pageUrl"] for e in catalog["entries"]] == [EP1, EP2]
    assert catalog["entries"][1]["accessEvidence"] == "public-directory"
    assert result["episode"]["hlsUrl"].endswith("playlist.m3u8")


def test_locked_current_player_never_exposes_stream_and_account_access_replaces_price_hint():
    locked = lata.parse_page(html(number=2, locked=True), EP2)
    assert locked["episode"]["locked"] and locked["episode"]["hlsUrl"] is None
    allowed = lata.parse_page(html(number=2), EP2)
    assert allowed["episode"]["locked"] is False and allowed["episode"]["hlsUrl"]
    assert allowed["catalog"]["entries"][1]["access"] == "unknown"


@pytest.mark.parametrize(
    "changes",
    [
        {"data": {"episode": 2}},
        {"data": {"filmId": True}},
        {"data": {"totalEpisodes": 1}},
        {"data": {"locked": "false"}},
        {"data": {"hlsUrl": "javascript:alert(1)"}},
        {"series": {"numberOfEpisodes": 3}},
        {"series": {"url": "https://else.test/originals/garden"}},
        {"links": [(EP1, "EP 1", ""), (EP2, "EP 1", "")]},
        {"links": [(EP1, "EP 1", ""), ("https://else.test/watch/e2", "EP 2", "")]},
    ],
)
def test_inconsistent_page_or_catalog_rejected(changes):
    with pytest.raises(ValueError):
        lata.parse_page(html(**changes), EP1)


def test_unobserved_episode_is_not_synthesized_and_originals_must_match_series():
    result = lata.parse_page(html(total=3), EP1, requested_url=SHOW)
    assert len(result["catalog"]["entries"]) == 2 and result["catalog"]["expectedCount"] == 3
    with pytest.raises(ValueError):
        lata.parse_page(html(), EP1, requested_url=ORIGIN + "/originals/other")
    with pytest.raises(ValueError):
        lata.parse_page(html(), EP1, requested_url=EP2)


def test_index_reads_only_collection_list_and_does_not_invent_episode_links():
    item = {
        "@type": "TVSeries",
        "url": SHOW,
        "name": "Garden",
        "image": "https://media.example/poster.jpg",
        "genre": "Drama",
        "numberOfEpisodes": 2,
    }
    data = {
        "@graph": [
            {
                "@type": "CollectionPage",
                "url": ORIGIN + "/series",
                "mainEntity": {
                    "@type": "ItemList",
                    "itemListElement": [{"@type": "ListItem", "url": SHOW, "item": item}],
                },
            }
        ]
    }
    markup = f'<script type="application/ld+json">{json.dumps(data)}</script>'
    result = lata.parse_index(markup, ORIGIN + "/series")
    assert result == {
        "shows": [
            {
                "url": SHOW,
                "title": "Garden",
                "categories": ["Drama"],
                "covers": [item["image"]],
                "catalog": None,
            }
        ],
        "pages": [],
        "limited": False,
    }


class Policy:
    proxy_url = None

    async def validate(self, url):
        assert lata.route(url)
        return url


class Browser:
    async def cookies_for(self, site_id, url):
        assert site_id == "lata" and lata.route(url)
        return [{"name": "session", "value": "account-cookie"}]


@pytest.mark.asyncio
async def test_read_page_follows_only_official_originals_redirect_and_sends_site_cookie(
    monkeypatch,
):
    called = []

    def server(request):
        called.append(str(request.url))
        assert request.method == "GET" and request.headers["Cookie"] == "session=account-cookie"
        if str(request.url) == SHOW:
            return httpx.Response(302, headers={"Location": EP1, "Set-Cookie": "tracking=ignore"})
        assert str(request.url) == EP1
        return httpx.Response(200, text=html())

    original = httpx.AsyncClient
    monkeypatch.setattr(
        lata.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(server), **kw),
    )
    result = await lata.read_page(SHOW, Browser(), SITE, Policy())
    assert result["url"] == EP1 and called == [SHOW, EP1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,location",
    [(302, "https://else.test/watch/garden"), (403, None), (302, ORIGIN + "/login")],
)
async def test_rejected_or_login_page_requires_authorization_without_reading_unrelated_origin(
    monkeypatch, status, location
):
    called = []

    def server(request):
        called.append(str(request.url))
        return httpx.Response(status, headers={"Location": location} if location else {})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        lata.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(server), **kw),
    )
    with pytest.raises(ValueError):
        await lata.read_page(SHOW, Browser(), SITE, Policy())
    assert called == [SHOW]


def test_locked_page_can_publish_directory_without_search_engine_metadata():
    import re

    markup = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", html(number=2, locked=True)
    )
    result = lata.parse_page(markup, EP2)
    assert result["episode"]["locked"] and result["catalog"]["seriesKey"] == "lata-9"
    assert result["catalog"]["catalogUrl"] == EP1
    assert len(result["catalog"]["entries"]) == 2


def test_canonical_source_normalizes_official_www_and_default_https_port_only():
    url = "https://www.lata.casa:443/watch/garden-ep-1?campaign=one#player"
    assert lata.canonical_source_url(url) == EP1 + "?campaign=one#player"
    assert lata.route(url) == ("episode", "garden-ep-1")
    assert lata.route("https://lata.casa:443/series") == ("index", None)
    for other in [
        "https://www.lata.casa:444/series",
        "http://lata.casa/series",
        "https://user@lata.casa/series",
    ]:
        assert lata.canonical_source_url(other) == other and lata.route(other) is None
    assert lata.parse_page(html(), url)["url"] == EP1
    with pytest.raises(ValueError):
        lata.parse_page(html(data={"hlsUrl": "https://media.example:bad/playlist.m3u8"}), EP1)


def test_relative_poster_is_resolved_against_actual_site_page():
    page = lata.parse_page(html(data={"poster": "/uploads/poster.jpg"}), EP1)
    assert page["episode"]["poster"] == ORIGIN + "/uploads/poster.jpg"
    assert page["catalog"]["coverUrls"] == [ORIGIN + "/uploads/poster.jpg"]
    missing = lata.parse_page(html(data={"poster": "", "hlsUrl": ""}), EP1)
    assert missing["episode"]["poster"] is None and missing["episode"]["hlsUrl"] is None


@pytest.mark.asyncio
async def test_originals_redirect_can_lead_to_paid_first_episode_without_schema(monkeypatch):
    import re

    markup = re.sub(
        r'<script type="application/ld\+json">.*?</script>',
        "",
        html(locked=True, data={"isFree": False}),
    )

    def server(request):
        if str(request.url) == SHOW:
            return httpx.Response(302, headers={"Location": EP1})
        assert str(request.url) == EP1
        return httpx.Response(200, text=markup)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        lata.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(server), **kw),
    )
    result = await lata.read_page(SHOW, Browser(), SITE, Policy())
    assert result["url"] == EP1 and result["episode"]["locked"]
    assert result["episode"]["hlsUrl"] is None
    assert result["catalog"]["expectedCount"] == 2


@pytest.mark.asyncio
async def test_originals_redirect_still_must_match_available_series_metadata(monkeypatch):
    def server(request):
        if str(request.url) == SHOW:
            return httpx.Response(302, headers={"Location": EP1})
        assert str(request.url) == EP1
        return httpx.Response(200, text=html(series={"url": ORIGIN + "/originals/different-show"}))

    original = httpx.AsyncClient
    monkeypatch.setattr(
        lata.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(server), **kw),
    )
    with pytest.raises(ValueError, match="剧集与输入链接不符"):
        await lata.read_page(SHOW, Browser(), SITE, Policy())
