"""Read Lata's published catalog and current episode payload, without running its player."""

import asyncio
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from backend.chunlou import canonical_source_url as canonical_chunlou_url
from backend.lustflix_directory import DirectoryAccessRequired
from backend.site_adapters import build_catalog

_ORIGIN = "https://lata.casa"
_PATH = re.compile(r"/(originals|watch)/([A-Za-z0-9_-]{1,180})/?")
_MAX_RESPONSE = 2 * 1024 * 1024
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def canonical_source_url(url):
    if not isinstance(url, str):
        return url
    try:
        page = urlsplit(url)
        if (
            page.scheme == "https"
            and page.hostname in {"lata.casa", "www.lata.casa"}
            and page.port in {None, 443}
            and not page.username
            and not page.password
        ):
            return urlunsplit(("https", "lata.casa", page.path, page.query, page.fragment))
    except ValueError:
        pass
    return canonical_chunlou_url(url)


def route(url):
    url = canonical_source_url(url)
    if not isinstance(url, str) or len(url) > 8192:
        return None
    try:
        page = urlsplit(url)
    except ValueError:
        return None
    if page.scheme != "https" or page.netloc != "lata.casa":
        return None
    if page.path.rstrip("/") in {"", "/series"}:
        return "index", None
    match = _PATH.fullmatch(page.path)
    if match:
        return ("series" if match[1] == "originals" else "episode"), match[2]
    return None


def _page_url(url):
    value = urlsplit(canonical_source_url(url))
    return urlunsplit((value.scheme, value.netloc, value.path.rstrip("/"), "", ""))


def _media_url(value, page_url):
    if not value:
        return None
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Lata 返回的素材网址无效")
    try:
        value = urljoin(page_url, value)
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Lata 返回的素材网址无效") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port == 0
    ):
        raise ValueError("Lata 返回的素材网址无效")
    return value


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.scripts, self.anchors = [], [], []
        self.canonical = []

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        current = {"tag": tag, "attrs": attrs, "text": []}
        if tag == "link" and "canonical" in (attrs.get("rel") or "").split():
            self.canonical.append(attrs.get("href"))
        if (
            tag == "a"
            and "episode-item" in (attrs.get("class") or "").split()
            and any(
                "episode-grid" in (node["attrs"].get("class") or "").split() for node in self.stack
            )
        ):
            self.anchors.append(current)
        if tag == "script":
            self.scripts.append(current)
        if tag not in _VOID:
            self.stack.append(current)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, text):
        for node in self.stack:
            if node["tag"] in {"script", "a"}:
                node["text"].append(text)


def _parse(html):
    if not isinstance(html, str) or len(html.encode("utf-8")) > _MAX_RESPONSE:
        raise ValueError("Lata 页面超过读取上限")
    page = _Page()
    page.feed(html)
    if len(page.scripts) > 64 or len(page.anchors) > 1000:
        raise ValueError("Lata 目录超过读取上限")
    return page


def _decode(script):
    try:
        value = json.loads("".join(script["text"]))
    except (ValueError, RecursionError):
        raise ValueError("Lata 页面数据格式已变化，请重试") from None
    if not isinstance(value, dict):
        raise ValueError("Lata 页面数据不完整")
    return value


def _schema(page):
    result = []
    for script in page.scripts:
        if script["attrs"].get("type") != "application/ld+json":
            continue
        data = _decode(script)
        rows = data.get("@graph", [data])
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("Lata 结构化目录无效")
        result.extend(row for row in rows if isinstance(row, dict))
    return result


def parse_page(html, final_url, requested_url=None, *, trusted_original_redirect=False):
    if not route(final_url) or route(final_url)[0] != "episode":
        raise ValueError("Lata 没有返回指定单集页面")
    final_url = _page_url(final_url)
    page = _parse(html)
    scripts = [
        script
        for script in page.scripts
        if script["attrs"].get("id") == "current-episode-data"
        and script["attrs"].get("type") == "application/json"
    ]
    if len(scripts) != 1:
        raise DirectoryAccessRequired("Lata 未返回单集信息，请在来源中检查登录或站点验证")
    data = _decode(scripts[0])
    for field in ("episodeId", "filmId", "episode", "totalEpisodes"):
        if type(data.get(field)) is not int or data[field] < 1:
            raise ValueError("Lata 单集或剧集编号无效")
    if not 1 <= data["episode"] <= data["totalEpisodes"] <= 1000:
        raise ValueError("Lata 返回的集数与目录不符")
    if any(type(data.get(field)) is not bool for field in ("locked", "isFree")):
        raise ValueError("Lata 未返回明确的单集权限状态")
    title = data.get("filmTitle")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("Lata 未返回有效剧名")
    series = [item for item in _schema(page) if item.get("@type") == "TVSeries"]
    if len(series) > 1 or (not series and not data["locked"]):
        raise ValueError("Lata 页面未确认唯一剧集目录")
    show_url = None
    if series:
        show = series[0]
        show_url = urljoin(final_url, show.get("url") or "")
        if (
            not route(show_url)
            or route(show_url)[0] != "series"
            or show.get("name") != title
            or show.get("numberOfEpisodes") != data["totalEpisodes"]
        ):
            raise ValueError("Lata 剧集信息与播放器不符")
        show_url = _page_url(show_url)
    if (
        len(page.canonical) != 1
        or not page.canonical[0]
        or _page_url(urljoin(final_url, page.canonical[0])) != final_url
    ):
        raise ValueError("Lata 返回的播放页面与输入链接不符")
    requested = requested_url or final_url
    requested_route = route(requested)
    if not requested_route:
        raise ValueError("Lata 返回的剧集与输入链接不符")
    expected_source = show_url if requested_route[0] == "series" else final_url
    if requested_route[0] == "series" and not series and trusted_original_redirect:
        expected_source = _page_url(requested)
    if _page_url(requested) != expected_source:
        raise ValueError("Lata 返回的剧集与输入链接不符")
    links, numbers, pages = [], set(), set()
    for anchor in page.anchors:
        href = anchor["attrs"].get("href")
        if not href:
            raise ValueError("Lata 的选集链接无效")
        url = _page_url(urljoin(final_url, href))
        match = re.fullmatch(r"EP\s+([1-9][0-9]{0,3})", "".join(anchor["text"]).strip(), re.I)
        if not match or not route(url) or route(url)[0] != "episode":
            raise ValueError("Lata 的选集链接或集号无效")
        number = int(match[1])
        if number > data["totalEpisodes"] or number in numbers or url in pages:
            raise ValueError("Lata 的选集目录重复或与总数不符")
        numbers.add(number)
        pages.add(url)
        classes = (anchor["attrs"].get("class") or "").split()
        gated = data["locked"] if url == final_url else "locked-episode" in classes
        links.append(
            {
                "url": url,
                "ordinal": number,
                "title": f"第 {number} 集",
                "access": "needs-user" if gated else "unknown",
            }
        )
    current = [link for link in links if link["url"] == final_url]
    if len(current) != 1 or current[0]["ordinal"] != data["episode"]:
        raise ValueError("Lata 当前集未列入本剧目录")
    links.sort(key=lambda link: link["ordinal"])
    if links[0]["ordinal"] != 1:
        raise ValueError("Lata 返回的目录未包含第一集，请重新读取完整目录")
    poster = _media_url(data.get("poster"), final_url)
    stream = _media_url(data.get("hlsUrl"), final_url)
    if stream and not urlsplit(stream).path.endswith(".m3u8"):
        raise ValueError("Lata 当前播放格式尚未适配")
    catalog = build_catalog(
        final_url,
        links,
        expected_count=data["totalEpisodes"],
        series_metadata={
            "seriesKey": f"lata-{data['filmId']}",
            "catalogUrl": links[0]["url"],
            "title": title,
        },
    )
    catalog.update(discoverySources=["site-directory"], coverUrls=[poster] if poster else [])
    for entry in catalog["entries"]:
        if entry["access"] == "needs-user":
            entry["accessEvidence"] = (
                "player" if entry["pageUrl"] == final_url else "public-directory"
            )
    episode = {
        field: data[field]
        for field in (
            "episodeId",
            "filmId",
            "filmTitle",
            "episode",
            "isFree",
            "locked",
            "totalEpisodes",
        )
    }
    episode.update(poster=poster, hlsUrl=None if data["locked"] else stream)
    return {"url": final_url, "catalog": catalog, "episode": episode}


def parse_index(html, page_url):
    if not route(page_url) or route(page_url)[0] != "index":
        raise ValueError("请输入 Lata 首页或剧集索引链接")
    collections = [
        item
        for item in _schema(_parse(html))
        if item.get("@type") == "CollectionPage" and route(item.get("url")) == ("index", None)
    ]
    if len(collections) != 1:
        raise ValueError("Lata 未返回完整剧集索引")
    listing = collections[0].get("mainEntity")
    if not isinstance(listing, dict) or listing.get("@type") != "ItemList":
        raise ValueError("Lata 剧集索引格式已变化")
    rows = listing.get("itemListElement")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError("Lata 剧集索引为空或超过读取上限")
    shows, seen = [], set()
    for row in rows:
        item = row.get("item") if isinstance(row, dict) else None
        if not isinstance(item, dict) or item.get("@type") != "TVSeries":
            raise ValueError("Lata 索引内存在无法识别的剧集")
        url, title = item.get("url"), item.get("name")
        if (
            not route(url)
            or route(url)[0] != "series"
            or row.get("url") != url
            or not isinstance(title, str)
            or not title.strip()
        ):
            raise ValueError("Lata 索引中的剧名或链接无效")
        url = _page_url(url)
        if url in seen:
            raise ValueError("Lata 索引中出现重复剧集")
        seen.add(url)
        image = _media_url(item.get("image"), page_url)
        genre = item.get("genre")
        categories = [genre] if isinstance(genre, str) else []
        shows.append(
            {
                "url": url,
                "title": title[:200],
                "categories": categories,
                "covers": [image] if image else [],
                "catalog": None,
            }
        )
    return {"shows": shows, "pages": [], "limited": False}


async def _read(url, browser, site, policy):
    if not route(url) or not route(site.get("url")):
        raise ValueError("此网址不属于已接入的 Lata 来源")
    url = _page_url(url) or _ORIGIN + "/"
    async with (
        asyncio.timeout(40),
        httpx.AsyncClient(
            timeout=12,
            follow_redirects=False,
            trust_env=False,
            proxy=getattr(policy, "proxy_url", None),
        ) as client,
    ):
        for _ in range(4):
            await policy.validate(url)
            cookies = await browser.cookies_for(site["id"], url)
            headers = {"Accept": "text/html"}
            if cookies:
                headers["Cookie"] = "; ".join(
                    f"{cookie['name']}={cookie['value']}" for cookie in cookies
                )
            client.cookies.clear()
            async with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    next_url = urljoin(url, response.headers.get("location", ""))
                    if not route(next_url):
                        raise DirectoryAccessRequired(
                            "Lata 要求登录或跳转到其他页面，请在来源中完成授权"
                        )
                    if route(url)[0] == "episode" or (
                        route(url)[0] == "series" and route(next_url)[0] != "episode"
                    ):
                        raise ValueError("Lata 播放页重定向与指定剧集不符")
                    url = _page_url(next_url)
                    continue
                if response.status_code in {401, 402, 403}:
                    raise DirectoryAccessRequired("Lata 拒绝读取页面，请在来源中完成登录或站点验证")
                response.raise_for_status()
                body = bytearray()
                async for block in response.aiter_bytes(65536):
                    if len(body) + len(block) > _MAX_RESPONSE:
                        raise ValueError("Lata 页面超过读取上限")
                    body.extend(block)
                return body.decode("utf-8-sig"), url
    raise ValueError("Lata 页面重定向过多，请重试")


async def read_page(url, browser, site, policy):
    if not route(url) or route(url)[0] == "index":
        raise ValueError("请输入 Lata 的剧集或单集链接")
    html, final = await _read(url, browser, site, policy)
    # Only a paid page without TVSeries metadata may rely on the observed official
    # redirect. Available metadata must still name the requested series.
    redirected = route(url)[0] == "series" and _page_url(url) != final
    return parse_page(html, final, requested_url=url, trusted_original_redirect=redirected)


async def read_index(url, browser, site, policy):
    if not route(url) or route(url)[0] != "index":
        raise ValueError("请输入 Lata 首页或剧集索引链接")
    html, final = await _read(url, browser, site, policy)
    return parse_index(html, final)
