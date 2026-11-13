import copy

import pytest

from backend.classification import automatic_classification
from backend.site_adapters import build_catalog, merge_catalog, select_episode_asset, update_catalog

CATALOG_URL = "https://series.example/series/documentary"
METADATA = {"seriesKey": "generic_demo", "catalogUrl": CATALOG_URL, "title": "Documentary"}


def page(ordinal):
    return f"https://series.example/watch/{ordinal}"


def catalog(ordinals, *, total=None, metadata=None, **kwargs):
    return build_catalog(
        CATALOG_URL,
        [{"url": page(ordinal), "ordinal": ordinal} for ordinal in ordinals],
        expected_count=total,
        series_metadata=metadata or METADATA,
        **kwargs,
    )


def media(ordinal=1, **changes):
    return {
        "id": f"video_{ordinal}",
        "kind": "video",
        "engine": "http",
        "status": "discovered",
        "pageUrl": page(ordinal),
        "sourceUrl": f"https://cdn.example/opaque-{ordinal}.mp4",
        "_episodeEvidence": {
            "version": 1,
            "adapterId": "generic",
            "seriesKey": "generic_demo",
            "episodeKey": f"generic_demo:{ordinal}",
            "ordinal": ordinal,
            "pageUrl": page(ordinal),
            "sourceUrl": changes.get("sourceUrl", f"https://cdn.example/opaque-{ordinal}.mp4"),
            "catalogUrl": CATALOG_URL,
            "source": "observed-main-video",
        },
        **changes,
    }


def test_generic_metadata_scopes_real_observed_urls_and_missing_ordinals():
    result = catalog([1, 3], total=3)
    assert result["scope"] == "series" and result["seriesKey"] == "generic_demo"
    assert result["catalogUrl"] == CATALOG_URL and result["title"] == "Documentary"
    assert [entry["episodeKey"] for entry in result["entries"]] == [
        "generic_demo:1",
        "generic_demo:3",
    ]
    assert [entry["pageUrl"] for entry in result["entries"]] == [page(1), page(3)]
    assert result["missingEpisodeKeys"] == ["generic_demo:2"] and result["missingCount"] == 1


def test_generic_unknown_total_never_invents_episode_urls():
    result = catalog([2, 4])
    assert result["status"] == "unknown" and result["expectedCount"] is None
    assert result["missingCount"] is None and result["missingEpisodeKeys"] == []
    assert result["observedGapOrdinals"] == [3]
    assert [entry["pageUrl"] for entry in result["entries"]] == [page(2), page(4)]


def test_generic_without_metadata_keeps_legacy_url_keys():
    result = build_catalog(CATALOG_URL, [{"url": page(1), "ordinal": 1}])
    assert result["scope"] == "page" and result["seriesKey"] is None
    assert result["entries"][0]["episodeKey"] == page(1)


def test_generic_links_require_same_origin_not_only_same_hostname():
    links = [
        {"url": page(1), "ordinal": 1},
        {"url": "https://series.example:443/watch/2", "ordinal": 2},
        {"url": "http://series.example/watch/3", "ordinal": 3},
        {"url": "https://series.example:444/watch/4", "ordinal": 4},
        {"url": "https://other.example/watch/5", "ordinal": 5},
    ]
    result = build_catalog(CATALOG_URL, links, series_metadata=METADATA)
    assert [entry["ordinal"] for entry in result["entries"]] == [1, 2]


@pytest.mark.parametrize(
    "metadata",
    [
        {**METADATA, "seriesKey": "../invalid"},
        {**METADATA, "seriesKey": "x" * 101},
        {**METADATA, "catalogUrl": "https://other.example/series"},
        {**METADATA, "catalogUrl": "http://series.example/series"},
        {**METADATA, "catalogUrl": "https://user:secret@series.example/series"},
    ],
)
def test_invalid_generic_metadata_never_establishes_series_scope(metadata):
    result = catalog([1], metadata=metadata)
    assert result["seriesKey"] is None and result["scope"] == "page"


def test_generic_merge_preserves_catalog_identity_and_updates_title():
    previous = catalog([1], metadata={**METADATA, "title": ""})
    current = catalog([3], total=3)
    original = copy.deepcopy(previous)
    result = merge_catalog(previous, current)
    assert result["title"] == "Documentary" and result["catalogUrl"] == CATALOG_URL
    assert result["missingEpisodeKeys"] == ["generic_demo:2"]
    assert previous == original


@pytest.mark.parametrize("difference", ["series", "origin", "catalog"])
def test_generic_merge_cannot_drift_into_other_catalog_even_when_empty(difference):
    previous, current = catalog([]), catalog([1])
    if difference == "series":
        current["seriesKey"] = "other"
    elif difference == "origin":
        current["catalogUrl"] = "https://other.example/series/documentary"
        current["entries"][0]["pageUrl"] = "https://other.example/watch/1"
    else:
        current["catalogUrl"] = "https://series.example/series/another"
    assert merge_catalog(previous, current) == previous


def test_generic_duplicate_ordinal_with_distinct_page_is_not_silently_complete():
    result = build_catalog(
        CATALOG_URL,
        [{"url": page(1), "ordinal": 1}, {"url": "https://series.example/alternate", "ordinal": 1}],
        expected_count=1,
        series_metadata=METADATA,
    )
    result = update_catalog(result, [media()])
    assert result["status"] == "partial" and "catalog-conflict" in result["reasons"]


def test_observed_main_video_evidence_promotes_generic_media_to_episode():
    asset = media()
    result = automatic_classification(asset)
    assert result["kind"] == "episode" and result["confidence"] == "medium"
    assert automatic_classification({**asset, "_episodeEvidence": None})["kind"] == "unknown"
    ready = update_catalog(catalog([1], total=1), [asset])
    assert ready["status"] == "complete-observed"
    assert ready["entries"][0]["downloadAssetId"] == asset["id"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("version", 2),
        ("adapterId", "unknown"),
        ("seriesKey", "../bad"),
        ("episodeKey", "generic_demo:2"),
        ("ordinal", True),
        ("ordinal", 0),
        ("ordinal", 1001),
        ("pageUrl", page(2)),
        ("sourceUrl", "https://cdn.example/new-opaque.mp4"),
        ("catalogUrl", "https://other.example/series"),
        ("catalogUrl", "https://series.example:444/series"),
        ("source", "guessed"),
    ],
)
def test_invalid_episode_evidence_does_not_promote_opaque_media(key, value):
    asset = media()
    asset["_episodeEvidence"][key] = value
    assert automatic_classification(asset)["kind"] == "unknown"


@pytest.mark.parametrize(
    "source", ["file:///tmp/movie.mp4", "https://user:secret@cdn.example/movie.mp4"]
)
def test_generic_evidence_requires_valid_http_media_source(source):
    assert automatic_classification(media(sourceUrl=source))["kind"] == "unknown"


@pytest.mark.parametrize(
    "changes,kind",
    [
        ({"sourceUrl": "https://cdn.example/trailer.mp4"}, "preview"),
        ({"_mediaLabel": "Official preview"}, "preview"),
        ({"sourceUrl": "https://cdn.example/audio.m4a"}, "audio"),
        ({"mime": "audio/mp4"}, "audio"),
    ],
)
def test_generic_evidence_never_overrides_preview_or_audio(changes, kind):
    assert automatic_classification(media(**changes))["kind"] == kind


def test_generic_multiple_media_sources_remain_ambiguous_and_keep_queries():
    entry = catalog([1])["entries"][0]
    first, second = (
        media(sourceUrl="https://cdn.example/opaque.mp4?token=a"),
        media(id="other", sourceUrl="https://cdn.example/opaque.mp4?token=b"),
    )
    assert select_episode_asset(entry, [first, second]) == {"assetId": None, "state": "ambiguous"}


def test_generic_catalog_cannot_reuse_evidence_for_another_series_on_same_page():
    entry = catalog([1], metadata={**METADATA, "seriesKey": "different"})["entries"][0]
    assert select_episode_asset(entry, [media()]) == {"assetId": None, "state": "unresolved"}
