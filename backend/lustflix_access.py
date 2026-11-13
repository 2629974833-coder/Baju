"""Resolve published Lustflix slug routes using the same read-only grants as the player."""

import asyncio
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from backend.classification import lustflix_stream_matches
from backend.lustflix_directory import directory_route

_ORIGIN = "https://lustflix.tv"
_SLUG = re.compile(r"series-[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}")


async def _read(client, url, site, browser, policy, *, manifest):
    maximum = 512 * 1024 if manifest else 2 * 1024 * 1024
    for _ in range(6):
        url = await policy.validate(url)
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError("不支持的重定向")
        client.cookies.clear()
        headers = {}
        if parts.netloc == "lustflix.tv":
            cookies = await browser.cookies_for(site["id"], url)
            if cookies:
                headers["Cookie"] = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
        async with client.stream("GET", url, headers=headers) as response:
            if manifest and response.is_redirect and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes(65536):
                if len(data) + len(chunk) > maximum:
                    raise ValueError("响应过大")
                data.extend(chunk)
            return data.decode("utf-8-sig")
    raise ValueError("重定向次数过多")


def _result(url, status, message, *, stop=False):
    access = {"ready": "observed", "needs-user": "needs-user"}.get(status)
    return {
        "status": "completed" if status == "ready" else status,
        "message": message,
        "pagesVisited": 1,
        "assetsFound": int(status == "ready"),
        "_pageAccess": {url: access} if access else {},
        "_recheckAttemptedUrls": [url],
        "_episodeOutcomes": {url: {"status": status, "message": message}},
        "_stopBatch": stop,
    }


async def probe_lustflix_episode(site, url, cache, browser, policy, on_asset, cancelled):
    route = directory_route(url)
    if (
        not route
        or not route[2]
        or not route[0].startswith("/watch/")
        or urlsplit(url).netloc != "lustflix.tv"
        or not _SLUG.fullmatch(route[1])
    ):
        return None
    slug, number = route[1], int(route[2])
    async with (
        asyncio.timeout(35),
        httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=10,
            proxy=getattr(policy, "proxy_url", None),
        ) as client,
    ):
        try:
            if slug not in cache:
                endpoint = _ORIGIN + "/api/content?" + urlencode({"slug": slug})
                cache[slug] = None  # Unknown formats fall back to the browser for this batch.
                data = json.loads(
                    await _read(client, endpoint, site, browser, policy, manifest=False)
                )
                if (
                    isinstance(data, dict)
                    and isinstance(data.get("series"), dict)
                    and data["series"].get("slug") == slug
                    and isinstance(data.get("media"), list)
                    and isinstance(data.get("episodes"), list)
                ):
                    cache[slug] = data
            data = cache[slug]
            if not data or cancelled():
                return None
            episodes = [
                e for e in data["episodes"] if isinstance(e, dict) and e.get("episode") == number
            ]
            media = [e for e in data["media"] if isinstance(e, dict) and e.get("episode") == number]
            if len(episodes) != 1:
                return None
            episode = episodes[0]
            price = episode.get("viewerPriceCoins", episode.get("priceCoins"))
            if (
                not media
                and episode.get("available") is True
                and ((type(price) is int and price > 0) or data["series"].get("memberOnly") is True)
            ):
                return _result(url, "needs-user", "当前站点会话没有取得本集的正片播放权限")
            if len(media) != 1 or not isinstance(media[0].get("src"), str):
                return None
            source = urljoin(_ORIGIN, media[0]["src"])
            if not lustflix_stream_matches(urlsplit(source), urlsplit(url)):
                return None
            playlist = (await _read(client, source, site, browser, policy, manifest=True)).strip()
            if not playlist.startswith("#EXTM3U") or (
                "#EXT-X-STREAM-INF:" not in playlist
                and not ("#EXTINF:" in playlist and "#EXT-X-ENDLIST" in playlist)
            ):
                return None
            if re.search(r'METHOD=SAMPLE-AES|KEYFORMAT="(?!identity")', playlist, re.I):
                return _result(url, "unsupported-drm", "此播放清单使用受保护的媒体格式")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429:
                return _result(url, "failed", "站点请求过于频繁（HTTP 429），请稍后重试", stop=True)
            return None
        except (ValueError, httpx.HTTPError):
            return None
    if cancelled():
        return None
    title = data["series"].get("title")
    await on_asset(
        {
            "sourceUrl": source,
            "pageUrl": url,
            "kind": "video",
            "engine": "yt-dlp",
            "mime": "application/vnd.apple.mpegurl",
            "category": "视频素材",
            "title": f"{title or slug} · 第 {number} 集",
            "duration": episode.get("duration") or 0,
            "_protocol": "m3u8_native",
            "_readOnly": True,
            "_lustflixGrant": urlsplit(source).path.startswith("/api/playback-media/"),
            "_headers": {"Referer": url},
        }
    )
    return _result(url, "ready", "已使用当前会话确认本集正片播放清单")
