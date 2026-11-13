import asyncio
import io

import pytest
from PIL import Image

import backend.series_posters as posters
from backend.site_adapters import merge_catalog
from backend.thumbnails import ThumbnailService


def test_directory_poster_survives_merging_partial_episode_scans():
    base = {"adapterId": "generic", "seriesKey": "s", "entries": []}
    covered = {**base, "coverUrls": ["https://example.org/series.jpg?private=token"]}
    assert merge_catalog(merge_catalog(base, covered), base)["coverUrls"] == covered["coverUrls"]


async def test_existing_series_share_index_and_cache_real_public_jpegs(tmp_path, monkeypatch):
    calls = []

    async def index(url, policy):
        calls.append(url)
        await asyncio.sleep(0.01)
        return {
            "shows": [
                {
                    "url": f"https://lustflix.tv/#/s/{key}",
                    "covers": [f"https://lustflix.tv/{key}.png"],
                }
                for key in ("C13", "C131")
            ]
        }

    monkeypatch.setattr(posters, "read_publisher_index", index)
    thumbs = ThumbnailService(tmp_path, None, None)

    async def fetch(asset, *, public=False):
        assert public and asset["_thumbnailUrl"].endswith(asset["id"] + ".png")
        data = io.BytesIO()
        Image.new("RGB", (150, 250), "blue").save(data, "PNG")
        return data.getvalue()

    monkeypatch.setattr(thumbs, "_fetch", fetch)
    service = posters.SeriesPosters(thumbs)

    def row(key):
        return {
            "id": key,
            "siteId": "lustflix",
            "url": f"https://lustflix.tv/#/watch/{key}/1",
            "catalog": {"adapterId": "lustflix", "seriesKey": key},
        }

    paths = await asyncio.gather(*(service.get(row(key), []) for key in ("C13", "C131")))
    assert len(calls) == 1
    assert paths[0] != paths[1]
    for path in paths:
        with Image.open(path) as im:
            assert im.size == (150, 250) and im.format == "JPEG"

    async def no_network(*args, **kwargs):
        pytest.fail("cached poster needs no session or network")

    monkeypatch.setattr(posters, "read_publisher_index", no_network)
    monkeypatch.setattr(thumbs, "_fetch", no_network)
    assert await posters.SeriesPosters(thumbs).get(row("C13"), []) == paths[0]


def test_generic_show_keeps_its_own_structured_poster_and_hides_signed_url(tmp_path):
    import json

    from backend.scanner import Scanner
    from backend.store import Store
    from backend.structured_catalog import parse_structured_catalog

    root = "https://shows.example/dramas/one"
    poster = "https://media.example/series.jpg?signature=private"
    chapter = root + "/1"
    structured = {
        "@context": "https://schema.org",
        "@type": "TVSeries",
        "url": root,
        "image": {"@type": "ImageObject", "contentUrl": poster},
        "episode": [
            {
                "@type": "TVEpisode",
                "url": chapter,
                "episodeNumber": 1,
                "image": "https://media.example/still.jpg",
            }
        ],
    }
    observed = parse_structured_catalog([json.dumps(structured)], root)
    catalog = Scanner._merge_catalog(None, root, observed)
    assert catalog["coverUrls"] == [poster]
    store = Store(tmp_path / "store.sqlite3")
    store.mutate(
        lambda data: data["scans"].append(
            {"id": "sample", "siteId": "shows", "url": root, "mode": "series", "catalog": catalog}
        )
    )
    published = store.public_state()
    assert published["series"][0]["posterUrl"].startswith("/api/series/")
    assert "signature=private" not in json.dumps(published)
