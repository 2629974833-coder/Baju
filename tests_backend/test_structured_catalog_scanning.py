"""Browser integration uses fictional, entirely route-fulfilled documentary pages."""

import json

import pytest

from backend.generic_catalog import discover_catalog
from tests_backend.test_generic_series_scanning import (
    SITE,
    directory,
    fixture_pages,
    neutral_browser,
    run_scan,
)
from tests_backend.test_structured_catalog import PAGE, episode, series


def markup(document):
    return '<script type="application/ld+json">' + json.dumps(document) + "</script>"


async def discover_page(scanner):
    context = await scanner.browser.context_for(SITE["id"])
    page = await context.new_page()
    await page.goto(PAGE)
    return await discover_catalog(page, PAGE, None, lambda: False)


@pytest.mark.asyncio
async def test_structured_only_series_page_queues_declared_episodes():
    pages = fixture_pages(2)
    pages["/series/field-notes"] = markup(series())
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(scanner)
    assert result["pagesVisited"] == 3 and len(assets) == 2
    assert result["catalog"]["expectedCount"] == 2
    assert {path for _, path in requests if path.startswith("/watch/")} == {"/watch/1", "/watch/2"}


@pytest.mark.asyncio
async def test_matching_dom_and_jsonld_merge_links_without_clearing_dom_gate():
    pages = fixture_pages(3)
    pages["/series/field-notes"] = directory(2).replace(
        "data-episode-count='2'", "data-episode-count='3'"
    ).replace("Episode 2</a>", "Episode 2 Members only</a>") + markup(
        series(numberOfEpisodes=3, episode=[episode(1), episode(2), episode(3)])
    )
    async with neutral_browser(pages) as (scanner, _requests):
        result = await discover_page(scanner)
    assert [item["ordinal"] for item in result["links"]] == [1, 2, 3]
    assert result["links"][1]["access"] == "needs-user"
    assert result["expectedCount"] == 3
    assert result["discoverySources"] == ["dom", "json-ld"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"episode": [episode(1, url="/different/1"), episode(2)]},
        {"url": PAGE + "/other", "@id": PAGE + "/other#series"},
    ],
)
async def test_dom_and_structured_identity_or_episode_conflicts_are_not_merged(change):
    pages = fixture_pages(2)
    pages["/series/field-notes"] += markup(series(**change))
    async with neutral_browser(pages) as (scanner, _requests):
        result = await discover_page(scanner)
    assert result["links"] == []
    assert "catalog-conflict" in result["reasons"]


@pytest.mark.asyncio
async def test_malformed_unrelated_json_does_not_poison_independent_dom_catalog():
    pages = fixture_pages(2)
    pages["/series/field-notes"] += '<script type="application/ld+json">{broken JSON</script>'
    async with neutral_browser(pages) as (scanner, _requests):
        result = await discover_page(scanner)
    assert len(result["links"]) == 2 and result["expectedCount"] == 2
    assert "structured-invalid" not in result["reasons"]
    assert result["structuredWarnings"] == ["structured-invalid"]
    assert result["discoverySources"] == ["dom"]


@pytest.mark.asyncio
async def test_jsonld_parent_link_from_playback_page_expands_confirmed_catalog():
    pages = fixture_pages(2)
    pages["/series/field-notes"] = markup(series())
    pages["/watch/1"] += markup(
        episode(1, partOfSeries={"@type": "TVSeries", "url": PAGE, "name": "Field Notes"})
    )
    async with neutral_browser(pages) as (scanner, requests):
        result, assets = await run_scan(scanner, url="https://documentary.example/watch/1")
    assert ("GET", "/series/field-notes") in requests
    assert result["pagesVisited"] == 3 and len(assets) == 2


@pytest.mark.asyncio
async def test_unrelated_publisher_conflicts_do_not_poison_matching_dom_and_jsonld():
    pages = fixture_pages(2)
    pages["/series/field-notes"] += markup(
        {
            "@graph": [
                series(),
                {"@type": "Organization", "@id": "#publisher", "name": "A"},
                {"@type": "Organization", "@id": "#publisher", "name": "B"},
            ]
        }
    )
    async with neutral_browser(pages) as (scanner, _requests):
        result = await discover_page(scanner)
    assert len(result["links"]) == 2 and result["expectedCount"] == 2
    assert result["reasons"] == [] and result["discoverySources"] == ["dom", "json-ld"]
