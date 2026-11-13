"""JSON-LD parsing fixtures contain only fictional documentary metadata."""

import json

import pytest

from backend.structured_catalog import parse_structured_catalog

PAGE = "https://documentary.example/series/field-notes"


def episode(number, **extra):
    return {
        "@type": "TVEpisode",
        "episodeNumber": number,
        "url": f"https://documentary.example/watch/{number}",
        "name": f"Field note {number}",
        **extra,
    }


def series(**extra):
    return {
        "@context": "https://schema.org",
        "@type": "TVSeries",
        "@id": PAGE + "#series",
        "url": PAGE,
        "name": "Field Notes",
        "numberOfEpisodes": 2,
        "episode": [episode(1), episode(2)],
        **extra,
    }


def parse(*documents, page=PAGE, **limits):
    return parse_structured_catalog(
        [
            json.dumps(document) if not isinstance(document, str) else document
            for document in documents
        ],
        page,
        **limits,
    )


def test_tvseries_declared_episode_urls_and_count_are_used_without_url_guessing():
    result = parse(series())
    assert [(item["ordinal"], item["url"]) for item in result["links"]] == [
        (1, "https://documentary.example/watch/1"),
        (2, "https://documentary.example/watch/2"),
    ]
    assert result["expectedCount"] == 2
    assert result["seriesMetadata"]["catalogUrl"] == PAGE
    assert result["seriesMetadata"]["title"] == "Field Notes"
    assert result["discoverySources"] == ["json-ld"]
    assert all(item["access"] == "unknown" for item in result["links"])


def test_graph_references_are_resolved_locally_and_episode_parent_confirms_scope():
    document = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "TVSeries",
                "@id": "#series",
                "url": PAGE,
                "name": "Field Notes",
                "numberOfEpisodes": 2,
                "hasPart": [{"@id": "#one"}, {"@id": "#two"}],
            },
            episode("1", **{"@id": "#one", "partOfSeries": {"@id": "#series"}}),
            episode(2, **{"@id": "#two", "partOfSeries": {"@id": "#series"}}),
        ],
    }
    result = parse(document)
    assert [item["ordinal"] for item in result["links"]] == [1, 2]
    assert result["reasons"] == []


def test_season_scope_preserves_its_actual_catalog_url():
    season_url = PAGE + "/season-1"
    result = parse(
        {
            "@context": "https://schema.org",
            "@type": "TVSeason",
            "url": season_url,
            "name": "Season 1",
            "numberOfEpisodes": 2,
            "partOfSeries": {"@type": "TVSeries", "url": PAGE},
            "episode": [episode(1), episode(2)],
        },
        page=season_url,
    )
    assert result["seriesMetadata"]["catalogUrl"] == season_url
    assert [item["ordinal"] for item in result["links"]] == [1, 2]


@pytest.mark.parametrize("number", [True, 1.5, 0, -1, 1001, "Episode 1"])
def test_invalid_episode_numbers_are_never_inferred_from_position_or_title(number):
    result = parse(series(episode=[episode(number, position=1)]))
    assert result["links"] == []
    assert result["expectedCount"] is None or result["reasons"]


def test_ordinary_itemlist_and_breadcrumbs_do_not_establish_a_series():
    result = parse(
        {
            "@type": "ItemList",
            "numberOfItems": 2,
            "itemListElement": [{"@type": "ListItem", "position": 1, "url": "/watch/1"}],
        },
        {
            "@type": "BreadcrumbList",
            "itemListElement": [{"position": 1, "item": {"@id": "/watch/2", "name": "Episode 2"}}],
        },
    )
    assert result["links"] == [] and result["seriesMetadata"] is None


def test_tv_episode_itemlist_without_parent_binding_is_not_a_series():
    assert parse({"@type": "ItemList", "itemListElement": [episode(1), episode(2)]})["links"] == []


def test_confirmed_series_itemlist_requires_episode_number_not_list_position():
    result = parse(
        series(
            episode={
                "@type": "ItemList",
                "numberOfItems": 2,
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": 99,
                        "item": {"episodeNumber": 1, "url": "/watch/1"},
                    },
                    {
                        "@type": "ListItem",
                        "position": 2,
                        "item": {"episodeNumber": 2, "url": "/watch/2"},
                    },
                ],
            }
        )
    )
    assert [item["ordinal"] for item in result["links"]] == [1, 2]


def test_two_unrelated_series_without_page_association_are_not_auto_selected():
    second = series(
        url="https://documentary.example/series/other",
        **{"@id": "https://documentary.example/series/other#series"},
    )
    result = parse(series(), second, page="https://documentary.example/browse")
    assert result["links"] == [] and "catalog-conflict" in result["reasons"]


def test_series_does_not_flatten_two_seasons_with_repeated_episode_numbers():
    seasons = [
        {
            "@type": "TVSeason",
            "url": PAGE + f"/season-{number}",
            "episode": [episode(1, url=f"/season-{number}/watch/1")],
        }
        for number in (1, 2)
    ]
    result = parse(series(episode=None, hasPart=seasons))
    assert result["links"] == [] and "catalog-conflict" in result["reasons"]


def test_episodes_declaring_another_parent_cannot_enter_selected_series():
    result = parse(
        series(episode=[episode(1, partOfSeries={"@type": "TVSeries", "url": PAGE + "/other"})])
    )
    assert result["links"] == [] and "catalog-conflict" in result["reasons"]


def test_unrelated_floating_episode_is_ignored():
    result = parse(series(), episode(88, url="/recommendation/88"))
    assert [item["ordinal"] for item in result["links"]] == [1, 2]


def test_conflicting_duplicate_graph_id_is_not_last_write_wins():
    result = parse(
        series(),
        episode(1, **{"@id": "#one", "partOfSeries": {"@id": PAGE + "#series"}}),
        episode(2, **{"@id": "#one", "partOfSeries": {"@id": PAGE + "#series"}}),
    )
    assert result["links"] == [] and "catalog-conflict" in result["reasons"]


def test_counts_conflict_is_kept_without_inventing_missing_episode_urls():
    result = parse(
        series(
            episode={
                "@type": "ItemList",
                "numberOfItems": 3,
                "itemListElement": [episode(1), episode(2)],
            }
        )
    )
    assert len(result["links"]) == 2 and result["expectedCount"] is None
    assert "conflicting-counts" in result["reasons"]


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/watch/1",
        "https://documentary.example:444/watch/1",
        "https://user:pass@documentary.example/watch/1",
        "javascript:alert(1)",
    ],
)
def test_episode_urls_must_be_actual_same_origin_http_urls(url):
    result = parse(series(episode=[episode(1, url=url)]))
    assert result["links"] == [] and "structured-invalid" in result["reasons"]


def test_fragment_only_node_id_is_not_a_navigation_url():
    result = parse(series(episode=[{"@type": "TVEpisode", "@id": "#ep1", "episodeNumber": 1}]))
    assert result["links"] == []


def test_conflicting_url_and_navigable_node_id_are_rejected():
    result = parse(series(episode=[episode(1, **{"@id": "/watch/other"})]))
    assert result["links"] == [] and "catalog-conflict" in result["reasons"]


def test_paid_flag_does_not_claim_current_user_is_locked():
    result = parse(series(episode=[episode(1, isAccessibleForFree=False)]))
    assert result["links"][0]["access"] == "unknown"


def test_malformed_json_is_only_a_warning_and_does_not_establish_scope():
    result = parse("{broken JSON")
    assert result["links"] == [] and result["seriesMetadata"] is None
    assert result["reasons"] == ["structured-invalid"]


@pytest.mark.parametrize(
    "limit", ["max_scripts", "max_bytes", "max_nodes", "max_depth", "max_ref_hops"]
)
def test_budget_exhaustion_never_accepts_a_partial_catalog(limit):
    document = series(episode={"@type": "ItemList", "itemListElement": [episode(1), episode(2)]})
    value = {"max_scripts": 0, "max_bytes": 10, "max_nodes": 2, "max_depth": 2, "max_ref_hops": 0}[
        limit
    ]
    result = parse(document, **{limit: value})
    assert result["links"] == [] and result["expectedCount"] is None
    assert result["linksTruncated"] and "structured-limit" in result["reasons"]


def test_local_reference_cycle_is_bounded_and_no_prefix_catalog_is_accepted():
    result = parse(
        {
            "@graph": [
                series(episode=[episode(1), {"@id": "#loop"}]),
                {"@type": "ItemList", "@id": "#loop", "itemListElement": [{"@id": "#loop"}]},
            ]
        }
    )
    assert result["links"] == [] and result["linksTruncated"]


def test_schema_expanded_types_and_root_array_are_supported_without_fetching_context():
    document = series(
        **{
            "@context": "https://unreachable.invalid/context",
            "@type": "https://schema.org/TVSeries",
        }
    )
    result = parse([document])
    assert len(result["links"]) == 2


def test_playback_episode_with_explicit_series_reference_exposes_parent_catalog_link():
    result = parse(
        episode(1, partOfSeries={"@type": "TVSeries", "url": PAGE, "name": "Field Notes"}),
        page="https://documentary.example/watch/1",
    )
    assert result["catalogLink"] == PAGE
    assert [item["ordinal"] for item in result["links"]] == [1]


@pytest.mark.parametrize(
    "change",
    [
        {"episodeNumber": "1" * 5000},
        {"url": ""},
        {"url": "   "},
        {"@id": "http://[bad", "url": None},
        {"url": PAGE + "/\ud800"},
    ],
)
def test_malformed_identifiers_and_numbers_cannot_escape_parser_as_exceptions(change):
    result = parse(series(episode=[episode(1, **change)]))
    assert result["links"] == []
    assert result["reasons"]


def test_thirty_three_scripts_rejects_even_a_complete_looking_prefix():
    result = parse(series(), *[{"@type": "Thing", "name": str(index)} for index in range(32)])
    assert result["links"] == [] and result["expectedCount"] is None
    assert result["reasons"] == ["structured-limit"]


def test_seventeen_local_reference_hops_are_bounded():
    graph = [series(episode=[episode(1), {"@id": "#list0"}])]
    graph += [
        {
            "@type": "ItemList",
            "@id": f"#list{index}",
            "itemListElement": [{"@id": f"#list{index + 1}"}],
        }
        for index in range(17)
    ]
    graph.append({"@type": "ItemList", "@id": "#list17", "itemListElement": [episode(2)]})
    result = parse({"@graph": graph})
    assert result["links"] == [] and result["reasons"] == ["structured-limit"]


def test_explicit_parent_bound_itemlist_can_use_numbered_untyped_items():
    result = parse(
        {
            "@graph": [
                {"@type": "TVSeries", "@id": "#series", "url": PAGE, "numberOfEpisodes": 2},
                {
                    "@type": "ItemList",
                    "partOfSeries": {"@id": "#series"},
                    "itemListElement": [
                        {"@type": "ListItem", "item": {"episodeNumber": 1, "url": "/watch/1"}},
                        {"@type": "ListItem", "item": {"episodeNumber": 2, "url": "/watch/2"}},
                    ],
                },
            ]
        }
    )
    assert [item["ordinal"] for item in result["links"]] == [1, 2]


def test_navigable_same_origin_ids_are_actual_links_while_graph_ids_stay_references():
    result = parse(
        {
            "@type": "TVSeries",
            "@id": PAGE,
            "episode": [{"@type": "TVEpisode", "@id": "/watch/1", "episodeNumber": 1}],
        }
    )
    assert result["links"][0]["url"] == "https://documentary.example/watch/1"


def test_shared_reference_dag_does_not_expand_exponentially():
    import subprocess
    import sys

    nodes = [series(numberOfEpisodes=1, episode={"@id": "#list0"})]
    nodes.extend(
        {
            "@type": "ItemList",
            "@id": f"#list{index}",
            "itemListElement": [{"@id": f"#list{index + 1}"}] * 8,
        }
        for index in range(12)
    )
    nodes.append(episode(1, **{"@id": "#list12"}))
    program = "from backend.structured_catalog import parse_structured_catalog; import sys,json; print(json.dumps(parse_structured_catalog([sys.stdin.read()], 'https://documentary.example/series/field-notes')))"
    completed = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps({"@graph": nodes}),
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert [item["ordinal"] for item in result["links"]] == [1]
    assert result["expectedCount"] == 1


def test_global_visit_budget_discards_even_a_complete_looking_prefix():
    result = parse(series(numberOfEpisodes=1), max_visits=1)
    assert result["links"] == [] and result["expectedCount"] is None
    assert result["linksTruncated"] and result["reasons"] == ["structured-limit"]


def test_episode_title_surrogates_cannot_break_utf8_catalog_persistence():
    result = parse(series(episode=[episode(1, name="bad\ud800title")]))
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert encoded and result["links"][0]["title"] == "bad?title"


def test_unrelated_organization_id_conflicts_do_not_poison_confirmed_catalog():
    result = parse(
        series(),
        {"@type": "Organization", "@id": "#org", "name": "A"},
        {"@type": "Organization", "@id": "#org", "name": "B"},
    )
    assert [item["ordinal"] for item in result["links"]] == [1, 2]
    assert result["reasons"] == []


def test_conflict_in_selected_episode_dependency_is_still_rejected():
    result = parse(
        series(episode=[{"@id": "#one"}]),
        episode(1, **{"@id": "#one"}),
        episode(2, **{"@id": "#one"}),
    )
    assert result["links"] == [] and "catalog-conflict" in result["reasons"]


def test_unrelated_episode_graph_conflicts_do_not_override_explicit_owner_dependencies():
    result = parse(series(), episode(88, **{"@id": "#other"}), episode(99, **{"@id": "#other"}))
    assert [item["ordinal"] for item in result["links"]] == [1, 2]
    assert result["reasons"] == []


@pytest.mark.parametrize("role", ["owner", "list", "parent"])
def test_selected_owner_list_and_parent_graph_conflicts_remain_blocked(role):
    page = PAGE
    if role == "owner":
        documents = [series(name="A"), series(name="B")]
    elif role == "list":
        documents = [
            series(episode={"@id": "#list"}),
            {"@type": "ItemList", "@id": "#list", "itemListElement": [episode(1)]},
            {"@type": "ItemList", "@id": "#list", "itemListElement": [episode(2)]},
        ]
    else:
        page = PAGE + "/season-1"
        documents = [
            {
                "@type": "TVSeason",
                "url": page,
                "partOfSeries": {"@id": "#parent"},
                "episode": [episode(1)],
            },
            {"@type": "TVSeries", "@id": "#parent", "url": PAGE, "name": "A"},
            {"@type": "TVSeries", "@id": "#parent", "url": PAGE, "name": "B"},
        ]
    result = parse(*documents, page=page)
    assert result["links"] == [] and "catalog-conflict" in result["reasons"]
