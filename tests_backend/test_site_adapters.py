import copy
import importlib

import pytest


def adapters():
    try:
        return importlib.import_module("backend.site_adapters")
    except ImportError:
        pytest.fail("site identity and catalog adapter is not implemented")


def episode(token="old", **values):
    return {
        "siteId": "lustflix",
        "kind": "video",
        "engine": "yt-dlp",
        "sourceUrl": f"https://lustflix.tv/api/media/series-slug/1/master.m3u8?token={token}",
        "pageUrl": "https://lustflix.tv/#/watch/C04/1",
        **values,
    }


def paid_episode(token="synthetic-token", role="master", **values):
    return episode(
        sourceUrl=f"https://lustflix.tv/api/playback-media/{token}/daughter-kingdom-night/1/{role}.m3u8",
        **values,
    )


def test_paid_path_signature_rotation_preserves_exact_media_identity():
    module = adapters()
    old, fresh = paid_episode(), paid_episode("synthetic-rotated.token_2")
    assert module.stable_asset_identity(old) is not None
    assert module.stable_asset_identity(old) == module.stable_asset_identity(fresh)
    assert module.refresh_identity_matches(old, fresh)
    assert module.refresh_identity_matches(old, paid_episode("synthetic-preview-audio-token"))
    for changed in [
        paid_episode(role="stream_0"),
        paid_episode(role="stream_2"),
        paid_episode(role="stream_audio0"),
        {**fresh, "pageUrl": old["pageUrl"].replace("C04", "C03")},
        {**fresh, "sourceUrl": fresh["sourceUrl"].replace("daughter-kingdom-night", "other-slug")},
        {**fresh, "sourceUrl": fresh["sourceUrl"] + "?cut=alternate"},
        {**fresh, "_mediaLabel": "Official trailer"},
    ]:
        assert not module.refresh_identity_matches(old, changed)


@pytest.mark.parametrize(
    "source",
    [
        "https://lustflix.tv/api/playback-media/synthetic-token/daughter-kingdom-night/2/master.m3u8",
        "https://other.example/api/playback-media/synthetic-token/daughter-kingdom-night/1/master.m3u8",
        "https://lustflix.tv.other.example/api/playback-media/synthetic-token/daughter-kingdom-night/1/master.m3u8",
        "https://lustflix.tv/api/playback-media/daughter-kingdom-night/1/synthetic-token/master.m3u8",
        "https://lustflix.tv/api/playback-media/synthetic-token/extra/daughter-kingdom-night/1/master.m3u8",
        "https://lustflix.tv/api/playback-media//daughter-kingdom-night/1/master.m3u8",
        "https://lustflix.tv/api/playback-media/synthetic%2Ftoken/daughter-kingdom-night/1/master.m3u8",
        "http://lustflix.tv/api/playback-media/synthetic-token/daughter-kingdom-night/1/master.m3u8",
        "https://lustflix.tv:444/api/playback-media/synthetic-token/daughter-kingdom-night/1/master.m3u8",
    ],
)
def test_paid_identity_rejects_wrong_episode_origin_or_path_shape(source):
    assert adapters().stable_asset_identity(episode(sourceUrl=source)) is None


def test_paid_master_is_canonical_and_keeps_downloaded_rotated_identifier():
    module = adapters()
    master = paid_episode(id="original", status="downloaded")
    catalog = module.build_catalog(
        master["pageUrl"], [{"url": master["pageUrl"]}], expected_count=1
    )
    assets = [
        paid_episode("synthetic-fresh", id="fresh"),
        master,
        paid_episode(role="stream_0", id="variant"),
        paid_episode(role="stream_audio0", id="audio"),
    ]
    updated = module.update_catalog(catalog, assets)
    assert updated["entries"][0]["downloadAssetId"] == "original"
    assert updated["status"] == "complete-observed" and updated["downloadedCount"] == 1


def test_known_identity_accepts_signature_rotation_only_for_exact_episode_and_role():
    module = adapters()
    old, fresh = episode(), episode("fresh")
    assert module.refresh_identity_matches(old, fresh)
    assert module.stable_asset_identity(old) == module.stable_asset_identity(fresh)
    for values in [
        {"pageUrl": "https://lustflix.tv/#/watch/C05/1"},
        {"pageUrl": "https://lustflix.tv/#/watch/C04/2"},
        {"sourceUrl": "https://lustflix.tv/api/media/other-slug/1/master.m3u8"},
        {"sourceUrl": "https://lustflix.tv/api/media/series-slug/2/master.m3u8"},
        {"sourceUrl": "https://lustflix.tv/api/media/series-slug/1/stream_audio0.m3u8"},
        {"sourceUrl": "https://lustflix.tv/api/media/series-slug/1/stream_0.m3u8"},
        {"sourceUrl": "https://lustflix.tv/member-previews/episode-1.mp4"},
        {"kind": "image"},
        {"engine": "http"},
        {"sourceUrl": "https://other.example/api/media/series-slug/1/master.m3u8"},
        {"sourceUrl": "https://lustflix.tv/api/media/series-slug/1/master.m3u8?cut=trailer"},
        {"_mediaLabel": "Official trailer"},
    ]:
        assert not module.refresh_identity_matches(old, episode("fresh", **values)), values


def test_generic_identity_keeps_query_and_exact_page_and_ignores_manual_labels():
    module = adapters()
    old = episode(sourceUrl="https://cdn.example/opaque.mp4?signature=old")
    assert module.stable_asset_identity(old) is None
    assert module.refresh_identity_matches(old, copy.deepcopy(old))
    assert not module.refresh_identity_matches(
        old, {**old, "sourceUrl": "https://cdn.example/opaque.mp4?signature=fresh"}
    )
    assert not module.refresh_identity_matches(old, {**old, "pageUrl": old["pageUrl"] + "x"})
    assert not module.refresh_identity_matches(
        old, {**episode(), "classification": {"kind": "episode", "source": "manual"}}
    )


def test_catalog_requires_explicit_total_and_matches_only_same_episode_master():
    module = adapters()
    page = "https://lustflix.tv/#/series/C04"
    links = [
        {"url": "https://lustflix.tv/#/watch/C04/1", "text": "Episode 1"},
        {"url": "https://lustflix.tv/#/watch/C04/3", "text": "Episode 3"},
        {"url": "https://lustflix.tv/#/watch/C04/3", "text": "Episode 3"},
        {"url": "https://lustflix.tv/#/watch/OTHER/1", "text": "Recommendation"},
        {"url": "https://lustflix.tv/#/login", "text": "Login"},
    ]
    catalog = module.build_catalog(page, links, expected_count=5)
    assert catalog["adapterId"] == "lustflix"
    assert catalog["expectedCount"] == 5
    assert catalog["enumeratedCount"] == 2
    assert catalog["duplicateCount"] == 1
    assert catalog["status"] == "partial"
    assert catalog["missingEpisodeKeys"] == ["C04:2", "C04:4", "C04:5"]
    assert module.build_catalog(page, links)["expectedCount"] is None
    assert module.build_catalog(page, links)["status"] == "unknown"
    assets = [
        episode(id="master", status="downloaded"),
        episode(id="trailer", sourceUrl="https://lustflix.tv/member-previews/episode-1.mp4"),
        episode(
            id="audio", sourceUrl="https://lustflix.tv/api/media/series-slug/1/stream_audio0.m3u8"
        ),
    ]
    updated = module.update_catalog(catalog, assets)
    assert updated["entries"][0]["assetIds"] == ["master"]
    assert updated["resolvedCount"] == updated["downloadedCount"] == 1
    assert catalog["resolvedCount"] == 0  # Pure helper.


def test_catalog_reports_blocked_and_truncated_without_false_completeness():
    module = adapters()
    page = "https://lustflix.tv/#/watch/C04/1"
    catalog = module.build_catalog(
        page,
        [
            {"pageUrl": page, "title": "Episode 1", "access": "needs-user"},
        ],
        expected_count=1,
        truncated=True,
        reasons=["page-limit"],
    )
    assert catalog["blockedCount"] == 1
    assert catalog["truncated"]
    assert "page-limit" in catalog["reasons"]
    assert catalog["status"] == "partial"
    generic = module.build_catalog("https://candy.ai/", [{"url": "/pricing", "text": "3 plans"}])
    assert generic["adapterId"] == "generic"
    assert generic["expectedCount"] is None and generic["entries"] == []


def test_lustflix_catalog_does_not_turn_ordinal_metadata_into_unobserved_watch_routes():
    module = adapters()
    catalog = module.build_catalog(
        "https://lustflix.tv/#/series/C04",
        [
            {"pageUrl": "https://lustflix.tv/#/payment", "ordinal": 1, "title": "Episode 1"},
        ],
        expected_count=1,
    )
    assert catalog["entries"] == []
