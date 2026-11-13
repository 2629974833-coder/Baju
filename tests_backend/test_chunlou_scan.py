"""Exercise the exact feed, including neighboring playlists that a feed preloads."""

import json
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from backend import chunlou_scan
from backend.chunlou import AUTH_PROJECT, DirectoryAccessRequired, _record, episode_route
from backend.scanner import Scanner
from backend.site_adapters import (
    refresh_identity_matches,
    select_episode_asset,
    stable_asset_identity,
)
from tests_backend.test_chunlou import (
    DETAIL,
    FIRST,
    ORIGIN,
    OTHER,
    PAGE,
    SECOND,
    SHOW_ROW,
    TARGET,
    playlist,
)

SITE = {"id": "chunlou", "name": "春楼", "url": ORIGIN}


@asynccontextmanager
async def fixture(
    monkeypatch, *, gate="", key="", metadata_denied=False, streamed=False, unlocks=None
):
    queries = []
    chosen = playlist(key=key)
    neighbor = playlist(OTHER)
    documents = {**chosen, **neighbor}
    account_api = f"https://{AUTH_PROJECT}-all.supabase.co"
    source_receipts = account_api + "/rest/v1/unlocks?select=episode_id&user_id=eq." + TARGET

    async def metadata(url, _policy):
        queries.append(url)
        if metadata_denied:
            raise DirectoryAccessRequired("春楼目录需要站点授权")
        return _record(SHOW_ROW, [FIRST, SECOND], episode_route(url))

    monkeypatch.setattr(chunlou_scan, "read_episode", metadata)
    monkeypatch.setattr(chunlou_scan, "read_drama", metadata)
    async with async_playwright() as w:
        chrome = await w.chromium.launch()
        requests = []

        class Policy:
            async def validate(self, url):
                assert urlsplit(url).hostname in {
                    "chunlou.tv",
                    "hls.chunlou.tv",
                    urlsplit(account_api).hostname,
                }
                return url

        class Browser:
            async def context_for(self, _id):
                raise ValueError("fixture empty session")

            async def create_scan_context(self, _site):
                ctx = await chrome.new_context(service_workers="block")

                async def serve(route):
                    req = route.request
                    requests.append((req.method, req.url))
                    if req.method not in {"GET", "HEAD"}:
                        pytest.fail("A write reached the site")
                    headers = {"access-control-allow-origin": "*"}
                    if req.url in documents:
                        body = documents[req.url]
                        if streamed:
                            # Fetch.fulfill supplies Chromium the decoded response body.
                            headers["content-encoding"] = "br"
                        else:
                            headers["content-length"] = str(len(body))
                        await route.fulfill(
                            status=200,
                            body=body,
                            headers={**headers, "content-type": "application/vnd.apple.mpegurl"},
                        )
                        return
                    if req.url == source_receipts:
                        await route.fulfill(
                            json=[{"episode_id": uid} for uid in unlocks], headers=headers
                        )
                        return
                    assert req.url in {PAGE, PAGE.replace(TARGET, OTHER), DETAIL}, req.url
                    preloads = list(documents)
                    if unlocks is not None:
                        preloads.append(source_receipts)
                    if streamed:
                        preloads.extend(
                            [
                                f"https://hls.chunlou.tv/key/{TARGET}/1",
                                f"https://hls.chunlou.tv/hls/{TARGET}/r1/v720/seg_0000.ts",
                            ]
                        )
                    page_gate = gate.get(req.url, "") if isinstance(gate, dict) else gate
                    html = f'<div id="app"><main>作品列表</main>{page_gate}</div><script>for (const u of {json.dumps(preloads)}) fetch(u)</script>'
                    await route.fulfill(
                        status=200, content_type="text/html; charset=utf-8", body=html
                    )

                await ctx.route("**/*", serve)
                return ctx

        try:
            yield Scanner(Browser(), Policy()), queries, requests, documents
        finally:
            await chrome.close()


async def scan(scanner, **options):
    assets = []
    updates = options.pop("updates", [])

    async def collect(item):
        assets.append(item)

    async def ignore(item):
        updates.append(item)

    result = await scanner.scan(
        SITE,
        {"mode": "single", "url": PAGE, "kinds": ["video", "image"], **options},
        collect,
        ignore,
        lambda: False,
    )
    return result, assets


@pytest.mark.parametrize(
    "gate,reason",
    [
        ('<div role="alertdialog" aria-label="Age Verification">18+</div>', "年龄确认"),
        ('<input type="password">', "Cloudflare"),
        ('<div role="dialog">同意隐私设置</div>', "处理弹窗"),
    ],
)
async def test_gate_blocks_prefetched_manifests_and_metadata(monkeypatch, gate, reason):
    async with fixture(monkeypatch, gate=gate) as (scanner, queries, _requests, _docs):
        result, assets = await scan(scanner)
        assert result["status"] == "needs-user" and reason in result["message"]
        assert not queries and not assets


@pytest.mark.parametrize("source", [PAGE, PAGE.replace("chunlou.tv/", "www.chunlou.tv:443/")])
async def test_clear_player_returns_only_its_video_and_signed_refresh(monkeypatch, source):
    async with fixture(monkeypatch) as (scanner, _, _requests, docs):
        result, assets = await scan(scanner, url=source)
        assert result["status"] == "completed"
        (video,) = [a for a in assets if a["kind"] == "video"]
        assert video["pageUrl"] == PAGE and video["_chunlouEpisode"] == TARGET
        assert stable_asset_identity(video)
        plain_variant = video["sourceUrl"].partition("?")[0]
        with_signature = {**video, "sourceUrl": plain_variant + "?e=1&n=2&s=3"}
        assert refresh_identity_matches(
            with_signature, {**video, "sourceUrl": plain_variant + "?e=4&n=5&s=6"}
        )
        new_url = video["sourceUrl"].replace("sig=fixture", "sig=rotated")
        master = next(url for url in docs if TARGET in url and "master" in url)
        docs[master] = docs[master].replace(video["sourceUrl"], new_url)
        docs[new_url] = docs.pop(video["sourceUrl"])
        refreshed = await scanner.resolve_asset(SITE, {**video, "id": "old"}, lambda: False)
        assert refreshed["status"] == "resolved" and refreshed["candidate"]["sourceUrl"] == new_url
        assert refresh_identity_matches(video, refreshed["candidate"])
        assert not refresh_identity_matches(video, {**video, "sourceUrl": new_url + "&language=de"})
        assert not refresh_identity_matches(
            video, {**video, "sourceUrl": new_url.replace(TARGET, OTHER)}
        )


async def test_streamed_player_observes_original_signatures_without_replaying_get(monkeypatch):
    key = f'#EXT-X-KEY:METHOD=AES-128,URI="chunlou://hls.chunlou.tv/key/{TARGET}/1"\n'
    async with fixture(monkeypatch, key=key, streamed=True) as (scanner, _, requests, docs):
        result, assets = await scan(scanner)
        assert result["status"] == "completed"
        video = next(a for a in assets if a["kind"] == "video")
        assert video["_chunlouDuration"] == pytest.approx(32.9)
        assert sorted(url for _, url in requests if url != PAGE) == sorted(
            url for url in docs if TARGET in url
        )


async def test_streamed_oversized_master_cannot_supply_a_video(monkeypatch):
    monkeypatch.setattr(chunlou_scan, "_MAX_PLAYLIST_BYTES", 1024)
    async with fixture(monkeypatch, streamed=True) as (scanner, _, _requests, docs):
        master = next(url for url in docs if TARGET in url and "master" in url)
        docs[master] = docs[master].replace("#EXTM3U", "#EXTM3U\n#" + "x" * 1024)
        result, assets = await scan(scanner)
        assert result["status"] == "failed" and "播放清单" in result["message"]
        assert not any(asset["kind"] == "video" for asset in assets)


async def test_protected_video_preserves_art_and_series_directory(monkeypatch):
    key = f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="chunlou://hls.chunlou.tv/key/{TARGET}/1"\n'
    async with fixture(monkeypatch, key=key) as (scanner, queries, requests, _docs):
        result, assets = await scan(scanner)
        assert result["status"] == "unsupported-media" and "专用密钥" in result["message"]
        assert [a["kind"] for a in assets] == ["image"]
        result, assets = await scan(scanner, mode="series", limit=2)
        assert result["status"] == "completed"
        assert "本次检查 1 集" in result["message"] and "暂不支持导出 1 集" in result["message"]
        assert "1 集的账号权限待核实" in result["message"]
        assert "第 1 集" not in result["message"]
        assert result["_episodeOutcomes"][PAGE]["status"] == "unsupported-media"
        assert list(result["_episodeOutcomes"]) == [PAGE]
        assert len(result["catalog"]["entries"]) == 2 and queries == [PAGE, PAGE]
        assert all(url != PAGE.replace(TARGET, OTHER) for _, url in requests)
        selected, _ = await scan(scanner, mode="series", seedUrls=[PAGE], limit=1)
        assert "暂不支持导出 1 集" in selected["message"]
        assert "权限待核实" not in selected["message"]  # Unrelated catalog prices are outside this selection.


async def test_directory_auth_rejection_is_not_an_empty_or_failed_scan(monkeypatch):
    async with fixture(monkeypatch, metadata_denied=True) as (scanner, _, _requests, _docs):
        result, assets = await scan(scanner)
        assert result["status"] == "needs-user" and "授权" in result["message"] and not assets


def test_legacy_feed_recommendation_is_not_selected_for_this_episode():
    row = {"episodeKey": "1", "pageUrl": PAGE, "ordinal": 1}
    video = {
        "id": "unrelated",
        "kind": "video",
        "engine": "yt-dlp",
        "pageUrl": PAGE,
        "sourceUrl": f"https://hls.chunlou.tv/hls/{OTHER}/r1/v720/index.m3u8",
        "_mediaLabel": "正片",
    }
    assert select_episode_asset(row, [video])["assetId"] is None


@pytest.mark.parametrize(
    "url", [PAGE + "&ep=" + OTHER, PAGE.replace(TARGET, "invalid"), ORIGIN + "/feed"]
)
async def test_ambiguous_feed_is_not_scanned_as_single(url):
    result, assets = await scan(Scanner(None, None), url=url)
    assert result["status"] == "failed" and "ep" in result["message"] and not assets


async def test_account_unlock_receipt_enables_priced_episode_without_inferring_other_permissions(
    monkeypatch,
):
    other_page = PAGE.replace(TARGET, OTHER)
    async with fixture(monkeypatch, unlocks=[OTHER]) as (scanner, queries, requests, _docs):
        result, assets = await scan(scanner, mode="series", url=DETAIL, limit=3)
        assert set(result["_episodeOutcomes"]) == {PAGE, other_page}
        assert result["_episodeOutcomes"][other_page]["status"] == "ready"
        assert [e["access"] for e in result["catalog"]["entries"]] == ["observed", "observed"]
        assert {a["pageUrl"] for a in assets if a["kind"] == "video"} == {PAGE, other_page}
        assert all(method in {"GET", "HEAD"} for method, url in requests)
        assert queries == [DETAIL]  # One complete, same-show listing supplies the entire batch.


async def test_recheck_marks_only_visited_seeds_and_keeps_missing_playback_distinct_from_denied(
    monkeypatch,
):
    other_page = PAGE.replace(TARGET, OTHER)
    async with fixture(monkeypatch) as (scanner, _, _, docs):
        for url in list(docs):
            if OTHER in url:
                del docs[url]
        result, _ = await scan(
            scanner,
            mode="series",
            url=other_page,
            seedUrls=[other_page, PAGE],
            recheckUrls=[other_page, PAGE],
            limit=1,
        )
        assert result["_recheckAttemptedUrls"] == [other_page]
        assert result["_episodeOutcomes"][other_page]["status"] == "failed"
        assert PAGE not in result["_episodeOutcomes"]
        assert not result["_pageAccess"]
        assert "未能解析 1 集" in result["message"] and "重扫" in result["message"]
        assert "1 集的账号权限待核实" in result["message"]


async def test_seed_batch_continues_after_first_gate_and_publishes_per_episode(monkeypatch):
    other_page = PAGE.replace(TARGET, OTHER)
    gates = {other_page: '<input type="password">'}
    updates = []
    async with fixture(monkeypatch, gate=gates) as (scanner, queries, requests, _docs):
        result, _ = await scan(
            scanner,
            mode="series",
            url=other_page,
            seedUrls=[other_page, PAGE],
            recheckUrls=[other_page, PAGE],
            limit=2,
            updates=updates,
        )
        assert result["status"] == "completed" and result["pagesVisited"] == 2
        assert result["_episodeOutcomes"][other_page]["status"] == "needs-user"
        assert result["_episodeOutcomes"][PAGE]["status"] == "ready"
        assert result["_recheckAttemptedUrls"] == [other_page, PAGE]
        assert queries == [PAGE]
        assert "站点要求处理权限或验证 1 集" in result["message"] and "复查" in result["message"]
        assert "可下载 1 集" in result["message"] and "待核实" not in result["message"]
        assert any(
            list(s["_episodeOutcomes"]) == [other_page] for s in updates if s["status"] == "running"
        )
        assert sum(url == other_page for _, url in requests) == 1


@pytest.mark.parametrize(
    "case, expected_status",
    [
        ("target", "needs-user"),
        ("neighbor", "completed"),
        ("loading", "completed"),
        ("unrelated-button", "completed"),
        ("offscreen", "completed"),
    ],
)
async def test_paywall_must_belong_to_the_addressed_visible_episode(
    monkeypatch, case, expected_status
):
    episode = OTHER if case == "neighbor" else TARGET
    style = "position:absolute;top:200vh;" if case == "offscreen" else ""
    title = "正在加载" if case == "loading" else "免费试看结束"
    action = "查看会员" if case == "unrelated-button" else "立即解锁 · 100"
    gate = (
        f'<div data-ep="{episode}" data-drama="field-notes" data-idx="0" style="{style}height:99vh;">'
        '<div class="absolute inset-0"><div><b class="font-display">' + title + "</b></div>"
        f'<button class="btn btn-primary">{action}</button></div></div>'
        "<button>Unlock Episode</button>"
    )
    async with fixture(monkeypatch, gate=gate) as (scanner, _, _, _):
        result, assets = await scan(scanner)
        assert result["status"] == expected_status
        if case == "target":
            assert (
                "解锁" in result["message"]
                and result["_episodeOutcomes"][PAGE]["status"] == "needs-user"
            )
            assert [a["kind"] for a in assets] == ["image"]

@pytest.mark.parametrize("purchased", [False, True])
async def test_custom_key_requires_evidence_of_entitlement_for_priced_target(monkeypatch, purchased):
    page = PAGE.replace(TARGET, OTHER)
    key = f'#EXT-X-KEY:METHOD=AES-128,URI="chunlou://hls.chunlou.tv/key/{OTHER}/3"\n'
    async with fixture(monkeypatch, unlocks=[OTHER] if purchased else []) as (scanner, _, _, docs):
        docs.update(playlist(OTHER, key=key))
        result, assets = await scan(scanner, url=page)
        videos = [a for a in assets if a["kind"] == "video"]
        if purchased:
            assert result["status"] == "completed" and len(videos) == 1
            assert videos[0]["_chunlouDuration"] == pytest.approx(32.9)
        else:
            assert result["status"] == "needs-user" and not videos
            assert result["_episodeOutcomes"][page]["status"] == "access-pending"
            assert page not in result["_pageAccess"]


@pytest.mark.parametrize("visible_target", [True, False])
async def test_missing_player_is_distinct_from_target_without_a_playlist(monkeypatch, visible_target):
    episode = TARGET if visible_target else OTHER
    gate = f'<div data-ep="{episode}" data-drama="field-notes" data-idx="0" style="height:95vh"></div>'
    async with fixture(monkeypatch, gate=gate) as (scanner, _, _, docs):
        docs.clear()
        result, assets = await scan(scanner)
        assert result["status"] == "failed" and not any(a["kind"] == "video" for a in assets)
        if visible_target:
            assert "尚未观察到可验证的完整播放清单" in result["message"]
        else:
            assert "目标集的播放器尚未出现" in result["message"]


@pytest.mark.parametrize("stop_on,visited", [(1, 0), (2, 1)])
async def test_expired_account_stops_the_batch_without_marking_unvisited_episode_locked(
    monkeypatch, stop_on, visited
):
    from backend.session_refresh import ChunlouRelinkRequired

    async with fixture(monkeypatch) as (scanner, _, _, _):
        create = scanner.browser.create_scan_context
        calls = 0

        async def expired(site):
            nonlocal calls
            calls += 1
            if calls == stop_on:
                raise ChunlouRelinkRequired("账号需重新关联")
            return await create(site)

        monkeypatch.setattr(scanner.browser, "create_scan_context", expired)
        other_page = PAGE.replace(TARGET, OTHER)
        result, _ = await scan(scanner, mode="series", limit=2, seedUrls=[PAGE, other_page],
                               recheckUrls=[PAGE, other_page], kinds=["video"])
        assert result["status"] == "needs-user" and result["pagesVisited"] == visited
        assert calls == stop_on
        assert result["_recheckAttemptedUrls"] == [PAGE][:visited]
        assert list(result["_episodeOutcomes"]) == [PAGE][:visited]
