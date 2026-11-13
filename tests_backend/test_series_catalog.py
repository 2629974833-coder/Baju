import copy

import pytest

from backend import site_adapters as adapters


def page(number=1, series="C03"):
    return f"https://lustflix.tv/#/watch/{series}/{number}"


def catalog(numbers, *, total=None, locked=(), series="C03", **kwargs):
    return adapters.build_catalog(
        page(series=series),
        [
            {"url": page(number, series), "access": "needs-user" if number in locked else "unknown"}
            for number in numbers
        ],
        expected_count=total,
        **kwargs,
    )


def asset(identifier="master", number=1, role="master", token="old", slug="series", **kwargs):
    return {
        "id": identifier,
        "kind": "video",
        "engine": "yt-dlp",
        "siteId": "lustflix",
        "pageUrl": page(number),
        "sourceUrl": f"https://lustflix.tv/api/media/{slug}/{number}/{role}.m3u8?token={token}",
        "status": "discovered",
        **kwargs,
    }


def test_directory_duplicate_lock_wins_in_both_orders():
    links = [{"url": page(), "access": "observed"}, {"url": page(), "access": "needs-user"}]
    for ordered in (links, links[::-1]):
        result = adapters.build_catalog(page(), ordered)
        assert result["entries"][0]["access"] == "needs-user"
        assert result["duplicateCount"] == 1


def test_merge_observed_pages_without_fabricating_unknown_urls():
    previous, current = catalog([1, 3]), catalog([3, 5])
    original = copy.deepcopy(previous)
    result = adapters.merge_catalog(previous, current)
    assert [entry["ordinal"] for entry in result["entries"]] == [1, 3, 5]
    assert result["expectedCount"] is None and result["status"] == "unknown"
    assert result["missingEpisodeKeys"] == [] and result["missingCount"] is None
    assert result["observedGapOrdinals"] == [2, 4]
    assert {entry["pageUrl"] for entry in result["entries"]} == {page(1), page(3), page(5)}
    assert previous == original


def test_merge_preserves_scope_when_another_series_is_seen():
    previous = catalog([1], total=1)
    result = adapters.merge_catalog(previous, catalog([1, 2], series="OTHER"))
    assert result == previous and result is not previous


def test_merge_does_not_mix_origins_with_the_same_series_identifier():
    previous = catalog([1])
    other = catalog([2])
    other["entries"][0]["pageUrl"] = "https://www.lustflix.tv/#/watch/C03/2"
    result = adapters.merge_catalog(previous, other)
    assert [entry["ordinal"] for entry in result["entries"]] == [1]


def test_old_lock_clears_only_after_exact_page_success():
    previous = catalog([1, 2], locked=[1])
    current = catalog([1, 2])
    current["entries"][0]["access"] = "observed"
    merged = adapters.merge_catalog(previous, current, observed_page=page(2), access="observed")
    assert merged["entries"][0]["access"] == "needs-user"
    unlocked = adapters.merge_catalog(merged, current, observed_page=page(1), access="observed")
    assert unlocked["entries"][0]["access"] == "observed"
    assert unlocked["blockedCount"] == 0


def test_new_lock_is_not_cleared_by_conflicting_same_page_success():
    result = adapters.merge_catalog(
        catalog([1]), catalog([1], locked=[1]), observed_page=page(), access="observed"
    )
    assert result["entries"][0]["access"] == "needs-user"


def test_merge_retains_count_conflict_and_scan_limits():
    previous = catalog([1], total=3, truncated=True, reasons=["catalog-limit"])
    result = adapters.merge_catalog(previous, catalog([2, 3, 4], total=4))
    assert result["expectedCount"] == 3
    assert "conflicting-counts" in result["reasons"]
    assert result["truncated"] and "catalog-limit" in result["reasons"]
    assert result["status"] == "partial"


def test_complete_rescan_recovers_from_historical_scan_limit_without_losing_warning():
    previous = catalog([1], total=3, truncated=True, reasons=["catalog-limit"])
    observed = adapters.merge_catalog(previous, catalog([1, 2, 3], total=3))
    result = adapters.update_catalog(
        observed, [asset(str(number), number=number, status="downloaded") for number in (1, 2, 3)]
    )
    assert result["status"] == "complete-observed" and result["downloadedCount"] == 3
    assert result["truncated"] and "catalog-limit" in result["reasons"]
    assert result["missingEpisodeKeys"] == []


@pytest.mark.parametrize(
    "warning",
    ["catalog-limit", "catalog-stalled", "route-changed", "read-only-blocked", "page-limit"],
)
def test_current_complete_evidence_outweighs_observation_warning(warning):
    result = adapters.update_catalog(catalog([1], total=1, reasons=[warning]), [asset()])
    assert result["status"] == "complete-observed"
    assert warning in result["reasons"]


@pytest.mark.parametrize(
    "problem", ["missing", "blocked", "ambiguous", "outside", "conflict", "unknown"]
)
def test_historical_warnings_never_hide_current_incomplete_or_conflicting_evidence(problem):
    numbers = [1] if problem == "missing" else [1, 3] if problem == "outside" else [1, 2]
    total = None if problem == "unknown" else 2
    observed = catalog(
        numbers,
        total=total,
        locked=[2] if problem == "blocked" else (),
        truncated=True,
        reasons=["catalog-limit", "read-only-blocked"],
    )
    candidates = [asset(str(number), number=number) for number in numbers]
    if problem == "ambiguous":
        candidates.append(asset("other", number=2, slug="other"))
    if problem == "conflict":
        observed = adapters.merge_catalog(observed, catalog([1, 2], total=3))
    result = adapters.update_catalog(observed, candidates)
    assert result["status"] == ("unknown" if problem == "unknown" else "partial")
    assert [entry["pageUrl"] for entry in result["entries"]] == [page(number) for number in numbers]


def test_missing_count_and_out_of_range_are_distinct():
    result = catalog([1, 3, 4], total=3)
    assert result["missingEpisodeKeys"] == ["C03:2"] and result["missingCount"] == 1
    assert result["outOfRangeOrdinals"] == [4]
    assert "out-of-range" in result["reasons"]
    assert result["status"] == "partial"


def test_unknown_total_reports_only_internal_numbering_gaps():
    result = catalog([5, 7])
    assert result["observedGapOrdinals"] == [6]
    assert result["expectedCount"] is None and result["missingEpisodeKeys"] == []


def test_oversized_observed_ordinal_cannot_expand_an_unbounded_gap_list():
    result = catalog([1, 10**30])
    assert [entry["ordinal"] for entry in result["entries"]] == [1]
    assert result["truncated"] and "ordinal-limit" in result["reasons"]


def test_selection_prefers_master_over_variants_and_excludes_audio_preview():
    entry = catalog([1])["entries"][0]
    candidates = [
        asset("variant", role="stream_0"),
        asset("master"),
        asset("audio", role="stream_audio0"),
        asset("preview", _mediaLabel="Official trailer"),
        asset("other-episode", number=2),
    ]
    result = adapters.select_episode_asset(entry, candidates)
    assert result == {"assetId": "master", "state": "ready"}


def test_rotated_duplicate_master_keeps_downloaded_original_identifier():
    candidates = [asset("new", token="new"), asset("legacy", status="downloaded")]
    result = adapters.update_catalog(catalog([1], total=1), candidates)
    entry = result["entries"][0]
    assert entry["downloadAssetId"] == "legacy" and entry["mediaStatus"] == "downloaded"
    assert entry["assetIds"] == ["new", "legacy"]
    assert result["resolvedCount"] == result["downloadedCount"] == 1
    assert result["ambiguousCount"] == 0 and result["status"] == "complete-observed"


@pytest.mark.parametrize("difference", ["slug", "query"])
def test_distinct_master_identities_are_ambiguous(difference):
    other = asset("other", slug="other" if difference == "slug" else "series")
    if difference == "query":
        other["sourceUrl"] += "&cut=alternate"
    result = adapters.update_catalog(catalog([1], total=1), [asset(), other])
    entry = result["entries"][0]
    assert entry["downloadAssetId"] is None and entry["mediaStatus"] == "ambiguous"
    assert result["ambiguousCount"] == 1 and result["resolvedCount"] == 0
    assert "ambiguous-media" in result["reasons"] and result["status"] == "partial"


@pytest.mark.parametrize("difference", ["slug", "query"])
def test_master_does_not_hide_a_variant_from_another_media_family(difference):
    other = asset(
        "other-variant", role="stream_0", slug="other" if difference == "slug" else "series"
    )
    if difference == "query":
        other["sourceUrl"] += "&cut=alternate"
    result = adapters.update_catalog(catalog([1], total=1), [asset(), other])
    entry = result["entries"][0]
    assert entry["downloadAssetId"] is None and entry["mediaStatus"] == "ambiguous"
    assert result["ambiguousCount"] == 1 and result["resolvedCount"] == 0
    assert result["status"] == "partial"


def test_two_variants_without_master_need_a_choice():
    entry = catalog([1])["entries"][0]
    assert adapters.select_episode_asset(entry, [asset("one", role="stream_0")]) == {
        "assetId": "one",
        "state": "ready",
    }
    assert adapters.select_episode_asset(
        entry, [asset("one", role="stream_0"), asset("two", role="stream_2")]
    ) == {"assetId": None, "state": "ambiguous"}


def test_historical_download_does_not_clear_current_access_requirement():
    result = adapters.update_catalog(catalog([1], locked=[1]), [asset(status="downloaded")])
    entry = result["entries"][0]
    assert entry["downloaded"] is True
    assert entry["mediaStatus"] == "needs-user" and entry["downloadAssetId"] is None
    assert entry["access"] == "needs-user"


def test_saved_variant_does_not_mark_an_unresolved_master_as_saved():
    result = adapters.update_catalog(
        catalog([1], total=1), [asset(), asset("video-only", role="stream_0", status="downloaded")]
    )
    assert result["entries"][0]["downloadAssetId"] == "master"
    assert result["downloadedCount"] == 0


def test_saved_one_of_ambiguous_masters_does_not_claim_canonical_file():
    result = adapters.update_catalog(
        catalog([1], total=1), [asset(status="downloaded"), asset("other", slug="other")]
    )
    assert result["downloadedCount"] == 0 and result["ambiguousCount"] == 1


def test_merge_acquires_explicit_total_without_replacing_observed_entries():
    result = adapters.merge_catalog(catalog([1]), catalog([3], total=4))
    assert result["expectedCount"] == 4
    assert result["missingEpisodeKeys"] == ["C03:2", "C03:4"]
    assert [entry["ordinal"] for entry in result["entries"]] == [1, 3]


def test_merging_the_same_progress_snapshot_is_idempotent():
    current = adapters.build_catalog(page(), [{"url": page(1)}, {"url": page(1)}])
    once = adapters.merge_catalog(catalog([2]), current)
    twice = adapters.merge_catalog(once, current)
    assert twice == once


def test_manual_episode_label_and_unknown_hosts_do_not_create_lustflix_identity():
    entry = catalog([1])["entries"][0]
    candidates = [
        asset(
            "fake",
            sourceUrl="https://cdn.example/episode-1.mp4",
            classification={"kind": "episode", "source": "manual"},
        )
    ]
    assert adapters.select_episode_asset(entry, candidates) == {
        "assetId": None,
        "state": "unresolved",
    }


def test_unknown_site_signed_urls_are_not_collapsed():
    entry = {"pageUrl": "https://example.com/episode/1", "access": "unknown"}
    candidates = [
        asset(
            str(index),
            pageUrl=entry["pageUrl"],
            sourceUrl=f"https://cdn.example/episode-1.mp4?token={index}",
        )
        for index in (1, 2)
    ]
    assert adapters.select_episode_asset(entry, candidates) == {
        "assetId": None,
        "state": "ambiguous",
    }


@pytest.mark.parametrize("outcome", ["unsupported-media", "failed"])
def test_price_hint_preserves_exact_playback_outcome_but_a_player_denial_takes_precedence(outcome):
    catalog = adapters.build_catalog("https://example.com/series/demo", [
        {"url": "https://example.com/episode/1", "ordinal": 1},
    ])
    entry = catalog["entries"][0]
    page = entry["pageUrl"]
    entry.update(access="needs-user", accessEvidence="public-directory")
    seen = {page: {"status": outcome, "message": "播放器检查结论"}}
    result = adapters.update_catalog(catalog, [], episode_outcomes=seen)
    assert result["entries"][0]["mediaStatus"] == outcome
    assert result["entries"][0]["downloadAssetId"] is None and result["blockedCount"] == 1
    entry["accessEvidence"] = "player"
    assert adapters.update_catalog(catalog, [], episode_outcomes=seen)["entries"][0]["mediaStatus"] == "needs-user"
