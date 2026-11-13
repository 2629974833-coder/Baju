"""Regression for a single Lustflix watch URL whose player exposes only one link."""

import asyncio
import copy

import httpx
import pytest

from backend.scanner import Scanner
from backend.site_adapters import build_catalog, merge_catalog

ORIGIN = "https://lustflix.tv"
PAGE = ORIGIN + "/#/watch/C13/1"
SITE = {"id": "lf-test", "url": ORIGIN}


class Policy:
    async def validate(self, url):
        assert url.startswith((ORIGIN + "/", "https://www.lustflix.tv/"))
        return url


class NoPlayer:
    async def context_for(self, site_id):
        pytest.fail("Adding a directory must not open the account or the player")


def directory_server(
    monkeypatch,
    episodes,
    *,
    slug="her-prisoner",
    lookup="her-prisoner",
    extra=None,
    member_only=False,
    live_series=None,
    fault=None,
):
    from backend import lustflix_directory

    paths = []

    def serve(request):
        assert request.method == "GET" and "cookie" not in request.headers
        path = request.url.path
        paths.append(path)
        if fault:
            response = fault(path, paths.count(path))
            if response is not None:
                return response
        if path == "/":
            return httpx.Response(
                200, text='<script src="/assets/catalog.js?v=published"></script>'
            )
        if path == "/assets/catalog.js":
            assert request.url.params["v"] == "published"
            return httpx.Response(
                200,
                text='window.LF_CATALOG={version:"test",series:[{id:"C13",slug:"her-prisoner",title:{hans:"她的囚徒"},eps:[{n:1}]}],other:{price:.8}};',
            )
        if path == "/api/tv/catalog":
            return httpx.Response(
                200, json={"series": live_series if live_series is not None else [{"slug": "new-series", "title": {"zh-CN": "新剧"}}]}
            )
        assert path == "/api/content" and dict(request.url.params) == {
            "slug": lookup,
            "view": "episodes",
        }
        return httpx.Response(
            200,
            json={
                "series": {"slug": slug, "title": "Her Prisoner", "memberOnly": member_only},
                "episodes": episodes,
                "memberAccess": False,
                "ownedEpisodes": [],
                **(extra or {}),
            },
        )

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        lustflix_directory.httpx,
        "AsyncClient",
        lambda **kw: factory(transport=httpx.MockTransport(serve), **kw),
    )
    return paths


async def scan(prior=None, page=PAGE):
    events = []

    async def progress(result):
        events.append(copy.deepcopy(result))

    async def found(_asset):
        pytest.fail("Catalog lookup must not discover a media file")

    result = await Scanner(NoPlayer(), Policy()).scan(
        SITE,
        {"mode": "series", "limit": 1, "url": page, "catalog": prior},
        found,
        progress,
        lambda: False,
    )
    assert events[-1]["status"] == result["status"]
    return result


def rows(numbers):
    return [
        {
            "episode": n,
            "title": f"Episode {n}",
            "available": True,
            "viewerPriceCoins": 5 if n > 1 else 0,
        }
        for n in numbers
    ]


async def test_one_watch_url_reads_all_official_rows_without_loading_player(monkeypatch):
    requests = directory_server(monkeypatch, rows(range(1, 6)))
    result = await scan()
    cat = result["catalog"]
    assert result["status"] == "completed" and cat["title"] == "她的囚徒"
    assert cat["expectedCount"] == 5 and cat["discoverySources"] == ["site-directory"]
    assert [(e["pageUrl"], e["access"]) for e in cat["entries"]] == [
        (ORIGIN + f"/#/watch/C13/{n}", "unknown" if n == 1 else "needs-user") for n in range(1, 6)
    ]
    assert requests == ["/", "/assets/catalog.js", "/api/tv/catalog", "/api/content"]


@pytest.mark.parametrize(
    "numbers,slug",
    [
        ([1, 1], "her-prisoner"),
        ([1, 2], "another"),
        ([2, 3], "her-prisoner"),
        ([1, 0], "her-prisoner"),
    ],
)
async def test_invalid_reply_does_not_publish_or_replace_an_old_catalog(monkeypatch, numbers, slug):
    directory_server(monkeypatch, rows(numbers), slug=slug)
    prior = build_catalog(PAGE, [{"url": PAGE}])
    result = await scan(prior)
    assert result["status"] == "failed" and result["catalog"] == prior


async def test_sparse_rows_never_invent_missing_episode_and_do_not_downgrade_existing_access(
    monkeypatch,
):
    directory_server(monkeypatch, rows([1, 3, 5]))
    prior = build_catalog(PAGE, [{"url": PAGE[:-1] + "3", "access": "observed"}])
    result = await scan(prior)
    public = (await scan())["catalog"]
    # Also cover two historical scan snapshots merged independently by the library.
    for cat in [result["catalog"], merge_catalog(prior, public)]:
        assert [(e["ordinal"], e["access"]) for e in cat["entries"]] == [
            (1, "unknown"),
            (3, "observed"),
            (5, "needs-user"),
        ]
        assert cat["expectedCount"] is None and cat["observedGapOrdinals"] == [2, 4]


async def test_series_with_a_server_slug_uses_verified_published_series_for_mapping(monkeypatch):
    paths = directory_server(monkeypatch, rows([1, 2]), slug="new-series", lookup="new-series")
    result = await scan(page="https://www.lustflix.tv/#/watch/new-series/2")
    assert result["status"] == "completed" and result["catalog"]["title"] == "新剧"
    assert paths == ["/", "/assets/catalog.js", "/api/tv/catalog", "/api/content"]


async def test_member_series_free_first_episode_remains_available_for_scanning(monkeypatch):
    directory_server(monkeypatch, rows([1, 2]), member_only=True)
    result = await scan()
    assert [e["access"] for e in result["catalog"]["entries"]] == ["unknown", "needs-user"]


async def test_explicit_pagination_cannot_mark_one_page_as_the_full_series(monkeypatch):
    directory_server(monkeypatch, rows([1]), extra={"total": 5, "nextOffset": 1})
    result = await scan()
    assert result["status"] == "failed" and not result.get("catalog")


async def test_public_price_provenance_survives_repeated_catalog_merges(monkeypatch):
    directory_server(monkeypatch, rows([1, 2]))
    page2 = PAGE[:-1] + "2"
    unknown = build_catalog(PAGE, [{"url": page2}])
    public = (await scan(unknown))["catalog"]
    verified = build_catalog(PAGE, [{"url": page2, "access": "observed"}])
    merged = merge_catalog(verified, public)
    assert next(e for e in merged["entries"] if e["ordinal"] == 2)["access"] == "observed"
    gated = build_catalog(PAGE, [{"url": page2, "access": "needs-user"}])
    confirmed_gate = merge_catalog(gated, public)
    assert (
        next(e for e in merge_catalog(verified, confirmed_gate)["entries"] if e["ordinal"] == 2)[
            "access"
        ]
        == "needs-user"
    )


async def test_complete_public_directory_replaces_old_enumeration_warnings_but_not_access(
    monkeypatch,
):
    directory_server(monkeypatch, rows([1, 2, 3]))
    prior = build_catalog(
        PAGE,
        [{"url": PAGE, "access": "needs-user"}],
        truncated=True,
        reasons=["route-changed", "read-only-blocked", "page-limit"],
    )
    fresh = await scan(prior)
    cat = fresh["catalog"]
    assert cat["expectedCount"] == 3 and cat["truncated"] is False
    assert not {"route-changed", "truncated", "read-only-blocked", "page-limit"}.intersection(
        cat["reasons"]
    )
    assert cat["blockedCount"] == 3
    assert {entry["mediaStatus"] for entry in cat["entries"]} == {"needs-user"}
    assert "blocked-entries" in cat["reasons"]
    assert "unresolved-media" not in cat["reasons"]


@pytest.mark.parametrize("count_match", [True, False])
def test_confirmed_directory_coverage_survives_rebuilding_library(tmp_path, count_match):
    from backend.store import Store

    db = tmp_path / "state.sqlite3"
    store = Store(db)
    old = build_catalog(PAGE, [{"url": PAGE}], truncated=True, reasons=["read-only-blocked"])
    # A fresh directory read must also clear warnings when the library re-merges earlier scans.
    entries = [{"url": PAGE[:-1] + str(i)} for i in ([1, 2, 3] if count_match else [1, 3, 4])]
    persisted = build_catalog(
        PAGE, entries, expected_count=3, truncated=True, reasons=["route-changed"]
    )
    persisted["discoverySources"] = ["site-directory"]
    base = {"siteId": "lf", "mode": "series", "url": PAGE, "status": "completed"}
    store.mutate(
        lambda state: state.update(
            scans=[
                {
                    **base,
                    "id": "fresh",
                    "catalog": persisted,
                    "_officialDirectoryCount": 3,
                },
                {**base, "id": "older", "catalog": old},
            ]
        )
    )
    state = Store(db).public_state()
    cat = state["series"][0]["catalog"]
    assert cat["truncated"] is (not count_match)
    assert ("route-changed" in cat["reasons"]) is (not count_match)
    assert ("missing-entries" in cat["reasons"]) is (not count_match)
    assert "read-only-blocked" in state["scans"][1]["catalog"]["reasons"]


@pytest.mark.parametrize(
    "previous_total,previous_page,compatible",
    [(2, PAGE, True), (4, PAGE, False), (2, PAGE.replace("/#", "/old/#"), False)],
)
async def test_authoritative_increase_only_when_retained_page_identity_fits(
    monkeypatch, previous_total, previous_page, compatible
):
    directory_server(monkeypatch, rows([1, 2, 3]))
    prior = build_catalog(
        PAGE, [{"url": previous_page}], expected_count=previous_total, truncated=True
    )
    cat = (await scan(prior))["catalog"]
    assert cat["expectedCount"] == (3 if compatible else previous_total)
    assert ("conflicting-counts" in cat["reasons"]) is (not compatible)
    assert cat["truncated"] is (not compatible)


@pytest.mark.parametrize("fragment", ["/s/C13", "/s/C13/1"])
async def test_share_link_uses_official_directory_and_watch_episode_ids(monkeypatch, fragment):
    directory_server(monkeypatch, rows([1, 2]))
    current = (await scan(page=ORIGIN + "/#" + fragment))["catalog"]
    assert current["seriesKey"] == "C13" and current["scope"] == "series"
    assert [e["pageUrl"] for e in current["entries"]] == [PAGE, PAGE[:-1] + "2"]


async def test_proxy_handover_retries_current_request_and_preserves_progress(monkeypatch):
    def fault(path, n):
        if path == "/api/content" and n == 1:
            raise httpx.ProxyError("502 Bad Gateway")

    paths = directory_server(monkeypatch, rows([1, 2]), fault=fault)
    result = await scan()
    assert result["status"] == "completed" and paths.count("/api/content") == 2
    assert paths.count("/") == paths.count("/assets/catalog.js") == 1


@pytest.mark.parametrize(
    "status,expected", [(401, "needs-user"), (403, "needs-user"), (429, "failed"), (502, "failed")]
)
async def test_directory_auth_limit_and_gateway_errors_have_distinct_outcomes(
    monkeypatch, status, expected
):
    paths = directory_server(monkeypatch, rows([1]), fault=lambda _p, _n: httpx.Response(status))
    result = await scan()
    assert result["status"] == expected
    if status == 502:
        assert 1 < len(paths) <= 3 and "网络" in result["message"]
    else:
        assert len(paths) == 1
    if status == 429:
        assert "频繁" in result["message"]


async def test_directory_cancel_during_reconnect_stops_before_second_request(monkeypatch):
    paths = directory_server(monkeypatch, [], fault=lambda *_: httpx.Response(503))
    retrying = asyncio.Event()

    async def progress(event):
        if "重试" in event["message"]:
            retrying.set()

    async def no_asset(_):
        pytest.fail("A cancelled directory read cannot publish assets")

    task = asyncio.create_task(
        Scanner(NoPlayer(), Policy()).scan(
            SITE, {"mode": "series", "url": PAGE}, no_asset, progress, lambda: False
        )
    )
    await asyncio.wait_for(retrying.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert paths == ["/"]


def test_readd_hidden_watch_directory_from_new_share_alias(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tests_backend.test_app import app_at, until

    directory_server(monkeypatch, rows([1, 2]))
    app = app_at(tmp_path)
    app.state.tasks.scanner = Scanner(NoPlayer(), Policy())
    with TestClient(app) as client:

        def add(fragment):
            scan_id = client.post("/api/series", json={"url": ORIGIN + "/#" + fragment}).json()[
                "id"
            ]
            state = until(client, lambda s: s["scans"][0].get("finishedAt"))
            assert state["scans"][0]["id"] == scan_id
            assert state["scans"][0]["status"] == "completed"
            return state

        first = add("/watch/C13/1")["series"][0]
        assert client.post("/api/series/dismiss", json={"ids": [first["id"]]}).is_success
        restored = add("/s/C13")
        assert restored["scans"][0]["restoredSeriesId"] == first["id"]
        assert [s["id"] for s in add("/s/C13/1")["series"]] == [first["id"]]



async def test_public_picker_repairs_legacy_placeholder_rows_and_accepts_later_release(monkeypatch):
    from backend.series import series_library

    public = rows(range(1, 27))
    placeholders = [
        {"episode": n, "available": False, "duration": None, "publishedAt": None, "priceCoins": 10}
        for n in range(27, 61)
    ]
    live = {"slug": "her-prisoner", "episodes": [{"n": n} for n in range(1, 27)]}
    directory_server(monkeypatch, public + placeholders, live_series=[live])
    legacy = build_catalog(PAGE, [{"url": PAGE[:-1] + str(i), "access": "needs-user"} for i in range(1, 61)], expected_count=60)
    legacy["discoverySources"] = ["site-directory"]
    fresh = await scan(legacy)
    assert fresh["_officialDirectoryCount"] == 26
    assert fresh["catalog"]["ongoing"] and fresh["catalog"]["publishedCount"] == 26
    base = {"siteId":"lf", "mode":"series", "url":PAGE, "status":"completed"}
    scans = [{**base, "id":"fresh", **fresh}, {**base,"id":"old", "catalog":legacy}]
    catalog = series_library({"scans":scans})[0]["catalog"]
    assert catalog["expectedCount"] == 26 and [e["ordinal"] for e in catalog["entries"]] == list(range(1,27))
    assert "conflicting-counts" not in catalog["reasons"]
    # An older selector cached with a later single-page scan cannot re-add future slots.
    stale = merge_catalog(catalog, legacy)
    assert len(stale["entries"]) == 26 and "conflicting-counts" not in stale["reasons"]
    # E27 goes live on the official picker even before the detail endpoint catches up.
    live["episodes"].append({"n":27, "duration":85, "priceCoins":5})
    later = (await scan(catalog))["catalog"]
    assert later["expectedCount"] == later["publishedCount"] == len(later["entries"]) == 27
    assert later["entries"][-1]["access"] == "needs-user"


async def test_missing_live_episode_metadata_requires_positive_placeholder_evidence(monkeypatch):
    episodes = [
        {"episode":1, "available":True},
        {"episode":2, "available":False, "duration":67, "publishedAt":"2026-11-01", "priceCoins":5},
        {"episode":3, "available":False, "duration":None, "publishedAt":None, "priceCoins":5},
        {"episode":4, "available":False},
    ]
    directory_server(monkeypatch, episodes)
    cat = (await scan())["catalog"]
    assert [e["ordinal"] for e in cat["entries"]] == [1, 2, 4] and cat["expectedCount"] is None
    assert cat["publishedCount"] is None
    assert cat["entries"][1]["access"] == "needs-user"
    absent = await scan(page=PAGE[:-1]+"3")
    assert absent["status"] != "completed"
