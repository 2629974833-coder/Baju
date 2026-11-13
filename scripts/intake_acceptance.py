"""Verify site, show and single-link intake with local HTML, real media and streamed exports."""

import asyncio
import hashlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from scripts.automation_acceptance import png, serve  # noqa: E402
from scripts.download_acceptance import inspect_and_decode, media_command  # noqa: E402


def fixture(movie):
    source = FastAPI()
    seen = []
    names = {"garden": "花园四季", "coast": "海岸笔记"}
    blobs = {"poster.png": png("#527150"), "still.png": png("#be964e"), "cover.png": png("#8e6541")}

    @source.get("/", response_class=HTMLResponse)
    async def home():
        return (
            "<section data-series-list>"
            + "".join(
                f'<article data-category="{genre}"><a data-category="{genre}" href="/series/{key}">'
                f'<img src="/media/cover.png">{names[key]}</a></article>'
                for key, genre in (("garden", "自然"), ("coast", "旅行"))
            )
            + "</section>"
        )

    @source.get("/series/{show}", response_class=HTMLResponse)
    async def series(show: str):
        links = "".join(
            f'<a data-episode-number="{ep}" href="/watch/{show}/{ep}">第 {ep} 集</a>'
            for ep in (1, 2)
        )
        return (
            f'<h1>{names[show]}</h1><main><img src="/media/poster.png" alt="剧照"></main>'
            f'<section aria-label="Episodes" data-series-title="{names[show]}" '
            f'data-series-url="/series/{show}" data-episode-count="2">{links}</section>'
        )

    @source.get("/watch/{show}/{ep}", response_class=HTMLResponse)
    async def episode(show: str, ep: int):
        seen.append((show, ep))
        return (
            f'<h1>{names[show]} 第 {ep} 集</h1><a rel="up" href="/series/{show}">剧集目录</a>'
            f'<main><img src="/media/still.png" width="240" alt="共用剧照">'
            f'<video controls src="/media/{show}-episode-{ep}.mp4"></video></main>'
        )

    @source.get("/media/{name}")
    async def file(name: str):
        if name in blobs:
            return Response(blobs[name], media_type="image/png")
        if name.endswith(".mp4"):
            return Response(movie, media_type="video/mp4")
        return Response(status_code=404)

    return source, seen


async def acquire(client, url, scope, **options):
    created = (
        (await client.post("/api/intake", json={"url": url, "scope": scope, **options}))
        .raise_for_status()
        .json()
    )
    return await finish(client, created["id"])


async def finish(client, scan_id):
    path = "/api/intake/" + scan_id
    async with asyncio.timeout(180):
        while True:
            result = (await client.get(path + "/results")).raise_for_status().json()
            if result["status"] not in {"queued", "running"}:
                assert result["status"] == "completed", (result["status"], result["message"])
                return path, result
            await asyncio.sleep(0.2)


async def verify(root):
    media = root / "episode.mp4"
    await asyncio.to_thread(
        media_command,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(media),
        ],
    )
    movie = media.read_bytes()
    site, seen = fixture(movie)
    settings = Settings(data_dir=root / "data", allow_private_hosts={"127.0.0.1"}, admin_token="")
    async with serve(site) as source, serve(create_app(settings)) as panel:
        async with httpx.AsyncClient(base_url=panel, trust_env=False, timeout=30) as client:
            first_path, index = await acquire(client, source + "/", "site", limit=1)
            assert len(index["groups"]) == 2 and index["canContinue"] and not seen, index
            continued = (
                (await client.post(first_path + "/continue", json={"limit": 12}))
                .raise_for_status()
                .json()
            )
            site_path, result = await finish(client, continued["id"])
            repeated = (
                (await client.post(first_path + "/continue", json={})).raise_for_status().json()
            )
            assert repeated["id"] == continued["id"] and result["previousScanId"] == index["scanId"]
            first = (await client.get(first_path + "/results")).json()
            assert not first["canContinue"] and first["nextScanId"] == continued["id"]
            assert not result["canContinue"] and (result["pagesVisited"], result["runPagesVisited"]) == (7, 6)
            shows = result["groups"]
            assert {s["category"] for s in shows} == {"自然", "旅行"}, shows
            assert len(shows) == 2 and all(len(s["episodes"]) == 2 for s in shows)
            assert all(s["assetIds"] for s in shows)
            episodes = [e for s in shows for e in s["episodes"]]
            assert all(len(e["assetIds"]) >= 2 for e in episodes), episodes
            shared = set.intersection(*(set(e["assetIds"]) for e in episodes))
            assert shared
            state = (await client.get("/api/state")).raise_for_status().json()
            selected = sorted(
                {i for s in shows for i in s["assetIds"]}
                | {i for e in episodes for i in e["assetIds"]}
            )
            queued = (
                (await client.post("/api/downloads", json={"ids": selected}))
                .raise_for_status()
                .json()
            )
            assert queued["added"] == len(selected)
            async with asyncio.timeout(90):
                while True:
                    state = (await client.get("/api/state")).json()
                    if all(j["status"] not in {"queued", "running"} for j in state["jobs"]):
                        break
                    await asyncio.sleep(0.2)
            assert all(j["status"] == "completed" for j in state["jobs"]), [
                j.get("error") for j in state["jobs"]
            ]
            assets = {a["id"]: a for a in state["assets"]}
            full = (
                await client.post(
                    site_path + "/archive-file", data={"payload": json.dumps({"ids": selected})}
                )
            ).raise_for_status()
            with zipfile.ZipFile(io.BytesIO(full.content)) as bundle:
                manifest = json.loads(
                    bundle.read(next(n for n in bundle.namelist() if n.endswith("manifest.json")))
                )
                for item in manifest["items"]:
                    assert item["sha256"] == hashlib.sha256(bundle.read(item["file"])).hexdigest()
                assert len({i["groupKey"] for i in manifest["items"]}) == 2
            target = shows[0]["episodes"][0]
            subset = (
                await client.post(
                    site_path + "/archive",
                    json={
                        "ids": target["assetIds"],
                        "groupKey": shows[0]["key"],
                        "episodeKey": target["key"],
                    },
                )
            ).raise_for_status()
            with zipfile.ZipFile(io.BytesIO(subset.content)) as bundle:
                entries = [n for n in bundle.namelist() if not n.endswith("manifest.json")]
                assert len(entries) == len(target["assetIds"]) and all(
                    "/E001" in n for n in entries
                )
            _, series_result = await acquire(client, target["url"], "series")
            (series,) = series_result["groups"]
            assert len(series["episodes"]) == 2 and all(
                e["assetIds"] for e in series["episodes"]
            ), series
            assert series["assetIds"], "剧详情页图片缺失"
            seen.clear()
            _, single_result = await acquire(client, target["url"], "single")
            (single,) = single_result["groups"]
            assert seen == [(target["url"].split("/")[-2], 1)], seen
            assert set(single["assetIds"]) == set(target["assetIds"]) and not single["episodes"]
            (video,) = [assets[i] for i in single["assetIds"] if assets[i]["kind"] == "video"]
            exported = (await client.get(video["fileUrl"] + "?download=true")).raise_for_status()
            assert (
                "attachment" in exported.headers["content-disposition"]
                and exported.content == movie
            )
            saved = root / "exported.mp4"
            saved.write_bytes(exported.content)
            await asyncio.to_thread(inspect_and_decode, saved)
    return {
        "shows": len(shows),
        "episodes": len(episodes),
        "downloaded": len(selected),
        "continuationFromIndex": True,
        "wholeSiteArchiveSHA": "all members match originals",
        "singleEpisodeArchiveFiles": len(entries),
        "singlePagesRead": len(seen),
        "exportedVideo": "hash and full decode verified",
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="framekeep-intake-") as temp:
        print(json.dumps(asyncio.run(verify(Path(temp))), ensure_ascii=False, indent=2))
