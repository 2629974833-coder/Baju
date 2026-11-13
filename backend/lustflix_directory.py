"""Read Lustflix's public series directory without starting a player or account session."""

import asyncio
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from yt_dlp.utils import js_to_json

from backend.network import NetworkResolutionError, is_transient_proxy_error
from backend.site_adapters import build_catalog

_IDENTIFIER = r"[A-Za-z0-9_-]{1,100}"
_WATCH = re.compile(rf"/watch/({_IDENTIFIER})/([1-9][0-9]{{0,3}})/?")
_SHARE = re.compile(rf"/s/({_IDENTIFIER})(?:/([1-9][0-9]{{0,3}}))?/?")
_RETRY_DELAYS = (0.5, 1.5)
_MAX_RESPONSE = 2 * 1024**2


class DirectoryAccessRequired(ValueError):
    pass


def directory_route(url):
    page = urlsplit(url)
    if (
        page.scheme == "https"
        and page.netloc.lower() in {"lustflix.tv", "www.lustflix.tv"}
        and page.path in {"", "/"}
        and not page.query
    ):
        return _WATCH.fullmatch(page.fragment) or _SHARE.fullmatch(page.fragment)
    return None


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            src = dict(attrs).get("src")
            if src:
                self.sources.append(src)


def _script_url(origin, html):
    scripts = _Scripts()
    scripts.feed(html)
    urls = {urljoin(origin + "/", src) for src in scripts.sources}
    matches = [url for url in urls if url.split("?", 1)[0] == origin + "/assets/catalog.js"]
    if len(matches) != 1:
        raise ValueError("未能确认站点使用的剧集目录")
    return matches[0]


def _published_series(script):
    # Only decode the published series array. Other catalog fields may contain JS-specific
    # numbers such as .8. Never evaluate the downloaded JavaScript in Python or a browser.
    assignment = re.match(r"\s*window\.LF_CATALOG\s*=\s*", script)
    if not assignment:
        raise ValueError("站点目录格式已改变")
    converted = js_to_json(script[assignment.end() :])
    prefix = re.match(
        r'^\s*\{\s*(?:"(?:version|built)"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*)*"series"\s*:\s*',
        converted,
    )
    if not prefix:
        raise ValueError("站点剧集映射格式已改变")
    series, _ = json.JSONDecoder().raw_decode(converted, prefix.end())
    if not isinstance(series, list):
        raise ValueError("站点剧集映射无效")
    return series


def _series_reference(script, series_id):
    matches = [s for s in _published_series(script) if isinstance(s, dict) and s.get("id") == series_id]
    if not matches:
        return None
    slug = matches[0].get("slug")
    if len(matches) != 1 or not isinstance(slug, str) or not re.fullmatch(_IDENTIFIER, slug):
        raise ValueError("公开目录中未找到与链接相符的剧集编号")
    return matches[0]


def _episode_rows(rows, field):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("站点的选集列表格式无效")
    by_number = {}
    for row in rows:
        number = row.get(field) if isinstance(row, dict) else None
        if type(number) is not int or not 1 <= number <= 1000 or number in by_number:
            raise ValueError("站点返回的集号无效或重复")
        by_number[number] = row
    return by_number


def _placeholder(row):
    return (row.get("available") is False
            and "publishedAt" in row and row["publishedAt"] is None
            and "duration" in row and row["duration"] is None)


def _catalog(page_url, route, ref, payload):
    show = payload.get("series") if isinstance(payload, dict) else None
    if not isinstance(show, dict) or show.get("slug") != ref["slug"]:
        raise ValueError("站点返回的剧集与输入链接不一致")
    rows = payload.get("episodes")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError("站点没有返回有效的单集目录")
    if (
        payload.get("hasMore")
        or payload.get("nextOffset")
        or payload.get("nextCursor")
        or any(payload.get(k, len(rows)) != len(rows) for k in ("total", "totalCount"))
    ):
        raise ValueError("站点返回的目录仍有下一页，请稍后重新读取")
    details = _episode_rows(rows, "episode")
    picker = ref.get("episodes")
    if picker is not None:
        # The site's picker is driven by /api/tv/catalog, including published paid episodes.
        visible = _episode_rows(picker, "n")
        published = {n: {**item, **details.get(n, {})} for n, item in visible.items()}
    else:
        # Older catalogs omit the picker. An unavailable paid episode with publication
        # metadata is different from a future slot with explicit null metadata.
        published = {n: row for n, row in details.items() if not _placeholder(row)}
    if not published:
        raise ValueError("站点当前没有已发布的单集")
    numbers, links = set(published), []
    page = page_url.split("#", 1)[0].rstrip("/") + f"/#/watch/{route[1]}/"
    for n, row in published.items():
        price = row.get("viewerPriceCoins", row.get("priceCoins"))
        restricted = (
            row.get("available") is False
            or (
                show.get("memberOnly") is True
                and not (n == 1 and type(price) is int and price == 0)
            )
            or (type(price) is int and price > 0)
        )
        links.append(
            {
                "url": page + str(n),
                "access": "needs-user" if restricted else "unknown",
                "title": row.get("title") if isinstance(row.get("title"), str) else "",
            }
        )
    if route[2] is not None and int(route[2]) not in numbers:
        raise ValueError("输入的单集未列入站点当前目录")
    total = len(numbers) if numbers == set(range(1, len(numbers) + 1)) else None
    cat = build_catalog(page_url, links, expected_count=total)
    titles = ref.get("title") if isinstance(ref.get("title"), dict) else {}
    cat["title"] = next(
        (
            name.strip()[:200]
            for name in (
                titles.get("hans"),
                titles.get("zh-CN"),
                show.get("title"),
                titles.get("hant"),
                titles.get("en"),
                route[1],
            )
            if isinstance(name, str) and name.strip()
        )
    )
    excluded = details.keys() - numbers
    cat["_publicationRead"] = {
        "at": time.time_ns(),
        "excludedPages": [page + str(n) for n in sorted(excluded)],
    }
    confirmed = picker is not None or all(
        row.get("available") is True or row.get("publishedAt")
        or (type(row.get("duration")) in (int, float) and row["duration"] > 0)
        for row in published.values()
    )
    cat["publishedCount"] = len(published) if confirmed else None
    cat["ongoing"] = any(_placeholder(details[n]) for n in excluded)
    cat["discoverySources"] = ["site-directory"]
    for entry in cat["entries"]:
        if entry["access"] == "needs-user":
            entry["accessEvidence"] = "public-directory"
    cat["coverUrls"] = cover_urls(ref, show)
    return cat


def cover_urls(*records):
    covers = []
    for item in records:
        for key in ("poster", "hero", "image", "wide"):
            value = item.get(key)
            values = value.values() if isinstance(value, dict) else [value]
            for href in values:
                if isinstance(href, str) and href.strip() and len(href) < 8192:
                    url = urljoin("https://lustflix.tv/", href.strip())
                    parsed = urlsplit(url)
                    if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
                        covers.append(url)
    return list(dict.fromkeys(covers))[:12]


async def _get_once(client, policy, url):
    await policy.validate(url)
    client.cookies.clear()
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes(65536):
            if len(body) + len(chunk) > _MAX_RESPONSE:
                raise ValueError("站点目录超过读取上限")
            body.extend(chunk)
        return body.decode("utf-8-sig")


async def _public_get(client, policy, url, on_retry):
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return await _get_once(client, policy, url)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 402, 403}:
                raise DirectoryAccessRequired(
                    f"站点拒绝读取公开目录（HTTP {status}），请在站点窗口检查访问权限后重试"
                ) from exc
            if status == 429:
                raise ValueError("站点请求过于频繁（HTTP 429），请稍后重新扫描") from exc
            if status not in {502, 503, 504}:
                raise
        except httpx.ProxyError as exc:
            if not is_transient_proxy_error(exc):
                raise ValueError("出口代理拒绝连接，请检查代理设置后重试") from exc
        except (
            NetworkResolutionError,
            httpx.NetworkError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ):
            pass
        if attempt >= len(_RETRY_DELAYS):
            raise ValueError("多次重连后仍无法读取站点目录，请检查网络或出口代理后重试")
        if invalidate := getattr(policy, "invalidate_dns", None):
            invalidate(url)
        if on_retry:
            await on_retry(
                f"连接暂时中断，正在重新读取目录（重试 {attempt + 1}/{len(_RETRY_DELAYS)}）"
            )
        await asyncio.sleep(_RETRY_DELAYS[attempt])


async def read_directory(page_url, route, policy, *, on_retry=None):
    # The www host redirects to this same publisher's canonical directory.
    origin = "https://lustflix.tv"
    try:
        async with (
            asyncio.timeout(40),
            httpx.AsyncClient(
                timeout=8,
                limits=httpx.Limits(max_keepalive_connections=0),
                follow_redirects=False,
                trust_env=False,
                proxy=getattr(policy, "proxy_url", None),
            ) as client,
        ):

            async def get(url):
                return await _public_get(client, policy, url, on_retry)

            html = await get(origin + "/")
            script = await get(_script_url(origin, html))
            ref = _series_reference(script, route[1])
            slug = ref["slug"] if ref else route[1]
            library = json.loads(await get(origin + "/api/tv/catalog"))
            shows = library.get("series") if isinstance(library, dict) else None
            matches = ([item for item in shows if isinstance(item, dict) and item.get("slug") == slug]
                       if isinstance(shows, list) else [])
            if len(matches) > 1 or (not matches and ref is None):
                raise ValueError("公开目录中未找到与播放链接相符的剧集")
            if matches:
                ref = {**(ref or {}), **matches[0]}
            query = urlencode({"slug": ref["slug"], "view": "episodes"})
            payload = json.loads(await get(origin + "/api/content?" + query))
            return _catalog(page_url, route, ref, payload)
    except TimeoutError as exc:
        raise ValueError("读取目录超时，请检查网络或出口代理后重试") from exc
    except (
        httpx.HTTPError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise ValueError("无法读取 Lustflix 的公开剧集目录，请稍后重新读取") from exc
