"""Bounded media discovery from the user's existing, authorized browser session."""

from __future__ import annotations

import asyncio
import copy
import re
from collections import deque
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from playwright.async_api import Error
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from backend.catalog_discovery import (
    EPISODE_ROOTS_JS,
    EPISODE_ROWS_JS,
    collect_catalog,
    open_episode_selector,
    same_page,
)
from backend.chunlou import drama_route as chunlou_drama
from backend.chunlou import episode_route as chunlou_episode
from backend.chunlou import manifest_url_matches
from backend.chunlou_scan import PlaylistObserver, scan_chunlou
from backend.chunlou_scan import probe_page as probe_chunlou_page
from backend.classification import (
    _generic_episode_evidence,
    classify_asset,
    lustflix_stream_matches,
)
from backend.generic_catalog import discover_catalog, same_origin_url
from backend.lata import route as lata_route
from backend.lata_scan import scan_lata
from backend.lustflix_access import probe_lustflix_episode
from backend.lustflix_directory import DirectoryAccessRequired, directory_route, read_directory
from backend.short_media import platform_for_url, short_target
from backend.site_adapters import (
    build_catalog,
    directory_covers_prior,
    merge_catalog,
    refresh_identity_matches,
    stable_asset_identity,
)
from backend.site_catalog import read_index_page, scan_site

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp"}
_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".ogv"}
_MANIFEST_EXTENSIONS = {".m3u8", ".mpd"}
_FRAGMENT_EXTENSIONS = {".ts", ".m4s", ".cmfv", ".cmfa", ".aac", ".vtt"}
_DECORATION = re.compile(
    r"(?:^|[/_.-])(logo|favicon|icon|avatar|emoji|sprite|tracking|pixel)(?:[/_.-]|$)", re.I
)
_ACCOUNT_LINK = re.compile(
    r"(?:^|[/_.-])(login|logout|signin|signout|register|signup|checkout|billing|subscribe|payment|oauth)(?:[/_.-]|$)",
    re.I,
)

_READ_ONLY_MEDIA = """(() => {
  window.open = () => null;
  const pauseMedia = () => {
    for (const media of document.querySelectorAll('video,audio')) {
      if (media.autoplay) media.autoplay = false;
      media.pause();
    }
  };
  Object.defineProperty(HTMLMediaElement.prototype, 'play', {
    configurable: true,
    value: function() {
      this.pause();
      return Promise.reject(new DOMException('Playback is disabled in a scan page', 'NotAllowedError'));
    }
  });
  document.addEventListener('play', event => { event.target.pause(); }, true);
  new MutationObserver(pauseMedia).observe(document, {
    subtree:true, childList:true, attributes:true, attributeFilter:['autoplay']
  });
})();"""

_DOM_MEDIA = r"""() => {
  const assets = [];
  const visible = element => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  const add = (url, kind, element, extra = {}) => {
    if (!url) return;
    const rect = element.getBoundingClientRect();
    assets.push({url, kind, width: element.naturalWidth || element.videoWidth || element.width || rect.width || 0,
      height: element.naturalHeight || element.videoHeight || element.height || rect.height || 0,
      pageRegion: element.closest('nav,aside,footer,header,[role="navigation"],[role="complementary"],[data-recommendations],[data-ad]') ? 'auxiliary' : 'content',
      displayWidth: rect.width, displayHeight: rect.height,
      title: element.alt || element.title || '', ...extra});
  };
  const srcset = (value, element) => {
    for (const entry of (value || '').split(',')) add(entry.trim().split(/\s+/)[0], 'image', element);
  };
  for (const element of document.querySelectorAll('img')) {
    for (const attribute of ['currentSrc', 'src', 'data-src', 'data-original', 'data-lazy-src']) {
      add(element[attribute] || element.getAttribute(attribute), 'image', element);
    }
    srcset(element.srcset, element);
    srcset(element.getAttribute('data-srcset'), element);
  }
  for (const element of document.querySelectorAll('picture source')) srcset(element.srcset, element);
  for (const element of document.querySelectorAll('video')) {
    const extra = {thumbnail: element.poster || '', duration: Number.isFinite(element.duration) ? element.duration : null,
      mediaLabel: (element.getAttribute('aria-label') || element.title || '').slice(0, 200),
      mainVideo: visible(element) && !!(element.closest('main,[role="main"]') || element.hasAttribute('data-main-video') || /^main video$/i.test(element.getAttribute('aria-label') || ''))};
    add(element.currentSrc || (element.getAttribute('src')?.trim() ? element.src : null), 'video', element, extra);
    for (const source of element.querySelectorAll('source')) {
      if (source.getAttribute('src')?.trim()) add(source.src, 'video', element, {...extra, mime: source.type});
    }
    add(element.poster, 'image', element);
  }
  for (const element of Array.from(document.querySelectorAll('*')).slice(0, 5000)) {
    const background = getComputedStyle(element).backgroundImage;
    for (const match of background.matchAll(/url\(["']?([^"')]+)["']?\)/g)) add(match[1], 'image', element);
  }
  const body = (document.body?.innerText || '').slice(0, 18000).toLowerCase();
  const modal = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="age-gate"], [class*="ageGate"], [id*="challenge"], [class*="captcha"]'))
    .filter(visible).map(element => element.innerText.toLowerCase()).join(' ');
  let blocked = '';
  const loginHeading = [...document.querySelectorAll('h1, [role="dialog"] h2')].some(element =>
    visible(element) && /^(?:sign in|log in) to (?:continue|watch)[.!]?$/i.test(element.textContent.trim()));
  if (loginHeading || Array.from(document.querySelectorAll('input[type="password"]')).some(visible)) blocked = '需要登录，请在站点浏览器窗口中完成操作后重新扫描';
  if (/verify you are human|checking your browser|just a moment|attention required.*cloudflare|security verification/.test(body + ' ' + document.title.toLowerCase())) blocked = '检测到人机验证，请在站点浏览器窗口中手动完成后重新扫描';
  if (/are you (?:over )?18|18 years (?:of age|or older)|age verification|年龄验证|年满18|已满18/.test(body)) blocked = '检测到年龄确认，请在站点浏览器窗口中自行处理后重新扫描';
  if (/consent|accept all cookies|agree to|同意|cookie/.test(modal)) blocked = '检测到同意或隐私对话框，请在站点浏览器窗口中自行处理后重新扫描';
  if (/subscribe to unlock|purchase to unlock|payment required|付费解锁/.test(modal)) blocked = '此页面要求额外访问权限，请在站点浏览器窗口中检查';
  if (/^(www\.)?lustflix\.tv$/i.test(location.hostname) && /members only/.test(body) && /join to watch/.test(body)) blocked = '此剧集要求会员访问权限，请在站点浏览器窗口中检查';
  const links = Array.from(document.querySelectorAll('a[href]')).filter(visible)
    .map(element => ({url: element.href, text: (element.textContent || '').trim().slice(0, 100)}));
  const watch = location.hash.match(/^#\/watch\/([A-Za-z0-9_-]+)\/\d+$/);
  let episodeRows = 0;
  if (/^(www\.)?lustflix\.tv$/i.test(location.hostname) && watch) {
    // Require a scoped list, an observed same-series link, or the known row ID.
    // Unscoped Episode N recommendation text is not evidence for a watch URL.
    const episodes = new Map();
    for (const {number,label,url} of (__FRAMEKEEP_EPISODE_ROWS__)(watch)) {
      const access = /members only|requires? (?:coins|credits)|unlock for|paid episode/i.test(label) ? 'needs-user' : 'unknown';
      if (!episodes.has(number) || access === 'needs-user') {
        episodes.set(number, {url, text: `Episode ${number}`, access});
      }
    }
    links.push(...episodes.values());
    episodeRows = episodes.size;
  }
  // Only an explicitly labelled episode list establishes its total. Counts in
  // recommendations or the page body cannot establish coverage for this series.
  const lists = (__FRAMEKEEP_EPISODE_ROOTS__)(watch);
  const counts = lists.map(element => Number(element.getAttribute('data-episode-count')))
    .filter(count => Number.isInteger(count) && count > 0 && count <= 1000);
  const expectedCount = new Set(counts).size === 1 ? counts[0] : null;
  const episodeReady = episodeRows > 0 || [...document.querySelectorAll(
    'button[title="Episodes"],button[aria-label="Episodes"],button[title="Episode list"],button[aria-label="Episode list"]'
  )].some(visible);
  return {assets, blocked, encrypted: !!window.__framekeepEncrypted, expectedCount, episodeReady,
    linksTruncated: links.length > 3000,
    links: links.slice(0, 3000)};
}""".replace("__FRAMEKEEP_EPISODE_ROWS__", EPISODE_ROWS_JS).replace(
    "__FRAMEKEEP_EPISODE_ROOTS__", EPISODE_ROOTS_JS
)


def _bounded_integer(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _absolute_url(value: str, base: str, *, media=False) -> str | None:
    try:
        url = urljoin(base, value.strip())
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        return urldefrag(url)[0] if media else url
    except (ValueError, AttributeError):
        return None


def _extension(url):
    path = urlsplit(url).path.lower()
    return "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""


def _media_type(url, mime="", hint=None):
    extension = _extension(url)
    mime = mime.split(";", 1)[0].strip().lower()
    if (
        extension in _FRAGMENT_EXTENSIONS
        or extension in {".svg", ".ico"}
        or mime == "image/svg+xml"
    ):
        return None
    if extension in _MANIFEST_EXTENSIONS or mime in {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/dash+xml",
        "audio/mpegurl",
    }:
        return "video", "yt-dlp"
    if mime.startswith("video/") or extension in _VIDEO_EXTENSIONS:
        return "video", "http"
    if mime.startswith("image/") or extension in _IMAGE_EXTENSIONS:
        return "image", "http"
    if hint in {"image", "video"}:
        return hint, "http"
    return None


def _decorative(candidate):
    if _DECORATION.search(urlsplit(candidate["url"]).path):
        return True
    for width, height in [
        (candidate.get("width", 0), candidate.get("height", 0)),
        (candidate.get("displayWidth", 0), candidate.get("displayHeight", 0)),
    ]:
        if (0 < width < 80) or (0 < height < 60):
            return True
    return False


class PageLoadFailure(Exception):
    pass


def transient_navigation_error(error):
    # Chromium signals proxy switches separately from application/authentication failures.
    return isinstance(error, PlaywrightTimeoutError) or any(code in str(error) for code in (
        "ERR_NETWORK_CHANGED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED",
        "ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_PROXY_CONNECTION_FAILED", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
    ))


class Scanner:
    def __init__(self, browser, policy, extractor=None):
        self.browser = browser
        self.policy = policy
        self.extractor = extractor

    async def scan(self, site, options, on_asset, on_progress, cancelled):
        series_mode = options.get("mode") == "series"
        capture_art = series_mode and options.get("_captureCatalogImages", False)
        whole_show = capture_art and options.get("intakeScope") == "series" and not options.get("seedUrls")
        start = options.get("url") or site["url"]
        if series_mode and "_seriesCheckSession" in options and not cancelled():
            result = await probe_lustflix_episode(
                site, start, options["_seriesCheckSession"], self.browser, self.policy, on_asset, cancelled
            )
            if result is not None:
                await on_progress(result)
                return result
        if not options.get("_chunlouProbe") and (chunlou_episode(start) or chunlou_drama(start)):
            if options.get("mode") == "site":
                result = {"status": "failed", "pagesVisited": 0, "assetsFound": 0,
                          "message": "这是春楼作品链接，请选择「单集或短视频」或「整部剧」重新拾取"}
                await on_progress(result)
                return result
            return await scan_chunlou(self, site, {**options, "url": start}, on_asset, on_progress, cancelled)
        if options.get("mode") in {"single", "series"}:
            parsed = urlsplit(start)
            if (parsed.scheme == "https" and parsed.hostname in {"chunlou.tv", "www.chunlou.tv"}
                    and parsed.path.rstrip("/") == "/feed" and ("ep" in parse_qs(parsed.query, keep_blank_values=True)
                                                                or options.get("mode") == "single")):
                result = {"status": "failed", "pagesVisited": 0, "assetsFound": 0,
                          "message": "请输入含单个有效 ep 作品编号的春楼链接"}
                await on_progress(result)
                return result
        if options.get("mode") == "site" or whole_show:
            return await scan_site(self, site, options, on_asset, on_progress, cancelled, one_show=whole_show)
        if lata_route(start):
            return await scan_lata(self, site, options, on_asset, on_progress, cancelled)
        follow_catalog = options.get("followCatalog", True)
        excluded_keys = set(options.get("excludedEpisodeKeys", []))
        limit = _bounded_integer(
            options.get("limit"), 100 if series_mode else 10, 1, 1000 if series_mode else 100
        )
        max_assets = _bounded_integer(options.get("maxAssets"), 250, 1, 1000)
        depth = _bounded_integer(options.get("depth"), 1, 0, 10)
        kinds = set(options.get("kinds") or ["image", "video"]) & {"image", "video"}
        start_url = await self.policy.validate(options.get("url") or site["url"])
        if options.get("mode") == "single" and platform_for_url(start_url):
            return await self._scan_post(site, start_url, kinds, on_asset, on_progress, cancelled)
        series_start = start_url if build_catalog(start_url, [])["seriesKey"] else None
        hostname = urlsplit(start_url).hostname
        seeds = options.get("seedUrls") or [start_url]
        seeds = list(dict.fromkeys(seeds))[:limit]
        for seed in seeds:
            await self.policy.validate(seed)
            if urlsplit(seed).hostname != hostname:
                raise ValueError("选集页面必须属于当前站点")
        pending = deque((seed, 0) for seed in seeds)
        queued = set(seeds)
        catalog = copy.deepcopy(options.get("catalog"))
        recheck_urls = (
            set(options.get("recheckUrls") or []) & set(seeds)
            & {entry["pageUrl"] for entry in (catalog or {}).get("entries", [])}
            if series_mode and not follow_catalog else set()
        )
        extraction = None
        directory_link = None
        single_video = options.get("intakeScope") == "single" and "video" in kinds
        observed_video = False
        seen_assets = set()
        seen_observations = set()
        attempted_pages = []
        visited = 0
        page = None
        scan_context = None
        gated = False
        load_failures = []
        drm = False
        lustflix = hostname in {"lustflix.tv", "www.lustflix.tv"}
        read_only = options.get("readOnly") is True
        chunlou_probe = options.get("_chunlouProbe") is True
        chunlou_id = chunlou_episode(start_url) if chunlou_probe else None
        protected_scan = lustflix or series_mode or read_only
        scanned_episodes = set()
        page_access = {}
        recheck_attempts = []
        official_directory_count = None
        blocked_requests = 0
        requested_page = None
        route_changed = False

        def guarded_contents(contents):
            if blocked_requests:
                return {**contents, "reasons": [*contents.get("reasons", []), "read-only-blocked"]}
            return contents

        async def report(message, status="running"):
            result = {
                "pagesVisited": visited,
                "pagesQueued": len(pending),
                "assetsFound": len(seen_assets),
                "limited": len(seen_assets) >= max_assets,
                "maxAssets": max_assets,
                "message": message,
                "status": status,
                "_pageAccess": dict(page_access),
                "_recheckAttemptedUrls": list(recheck_attempts),
            }
            if official_directory_count is not None:
                result["_officialDirectoryCount"] = official_directory_count
            if catalog is not None:
                result["catalog"] = catalog
            if capture_art:
                result["_attemptedPages"] = list(attempted_pages)
            if directory_link:
                result["_directoryLink"] = directory_link
            if extraction:
                result["extraction"] = extraction
            await on_progress(result)
            return result

        async def stop_changed_page(url):
            nonlocal catalog
            catalog = self._merge_catalog(
                catalog, url, {"reasons": ["route-changed"]}, access="needs-user"
            )
            return await report("页面跳转到了其他剧集或访问页面，已停止本次扫描", "needs-user")

        if cancelled():
            return await report("扫描已取消", "cancelled")
        route = directory_route(start_url) if series_mode and follow_catalog and not options.get("seedUrls") else None
        if route:
            await report("正在读取 Lustflix 的公开选集目录")
            try:
                current = await read_directory(start_url, route, self.policy, on_retry=report)
            except DirectoryAccessRequired as error:
                return await report(str(error), "needs-user")
            except ValueError as error:
                return await report(str(error), "failed")
            if cancelled():
                return await report("扫描已取消", "cancelled")
            if directory_covers_prior(catalog, current, current["expectedCount"]):
                official_directory_count = current["expectedCount"]
            catalog = merge_catalog(catalog, current, directory_count=official_directory_count)
            visited = 1
            return await report(f"已从站点目录列出 {len(current['entries'])} 集，可按需解析单集", "completed")
        try:
            async with asyncio.timeout(min(300, limit * 25 + 30)):
                if protected_scan:
                    try:
                        context = await self.browser.context_for(site["id"])
                    except ValueError:
                        context = None
                    # Cookies/local storage/IndexedDB are copied in memory. The user's
                    # authorization context stays interactive and never receives scan popups.
                    scan_context = await self.browser.create_scan_context(site)

                    async def read_only_request(route):
                        nonlocal blocked_requests, route_changed
                        if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                            blocked_requests += 1
                            await route.abort("blockedbyclient")
                            return
                        try:
                            if chunlou_probe:
                                resource = urlsplit(route.request.url)
                                if (
                                    resource.hostname == "hls.chunlou.tv"
                                    and not resource.path.startswith("/cover/")
                                    and (
                                        route.request.method != "GET"
                                        or not manifest_url_matches(route.request.url, chunlou_id)
                                    )
                                ):
                                    await route.abort("blockedbyclient")
                                    return
                            frame = route.request.frame
                            if route.request.is_navigation_request() and frame.parent_frame is None:
                                if frame.page != page:
                                    blocked_requests += 1
                                    await route.abort("blockedbyclient")
                                    return
                                if requested_page and not same_page(
                                    urldefrag(route.request.url)[0], urldefrag(requested_page)[0]
                                ):
                                    route_changed = True
                                    await route.abort("blockedbyclient")
                                    return
                            await self.policy.validate(route.request.url)
                        except (Error, ValueError, OSError):
                            await route.abort("blockedbyclient")
                            return
                        await route.fallback()

                    async def read_only_socket(route):
                        nonlocal blocked_requests
                        blocked_requests += 1
                        await route.close()

                    def navigated(frame):
                        nonlocal route_changed
                        if (
                            frame == page.main_frame
                            and requested_page
                            and not page.url.startswith("chrome-error://")
                            and not same_page(page.url, requested_page)
                        ):
                            route_changed = True

                    await scan_context.route("**/*", read_only_request)
                    await scan_context.route_web_socket("**/*", read_only_socket)
                    await scan_context.add_init_script(_READ_ONLY_MEDIA)
                    page = await scan_context.new_page()
                    page.on("framenavigated", navigated)
                else:
                    await self.browser.open_session(site)
                    context = await self.browser.context_for(site["id"])
                    page = await context.new_page()
                # A user may already have expanded a script-only episode selector.
                # Read visible catalog evidence without interacting with that page.
                if lustflix and not read_only and context is not None:
                    requested_origin = urlsplit(start_url)
                    requested_series = build_catalog(start_url, [])["seriesKey"]
                    for opened in context.pages[:10]:
                        if (
                            opened == page
                            or opened.is_closed()
                            or urlsplit(opened.url).hostname != hostname
                        ):
                            continue
                        opened_origin = urlsplit(opened.url)
                        if (
                            (opened_origin.scheme, opened_origin.netloc)
                            != (requested_origin.scheme, requested_origin.netloc)
                            or not requested_series
                            or build_catalog(opened.url, [])["seriesKey"] != requested_series
                        ):
                            continue
                        try:
                            visible = await opened.evaluate(_DOM_MEDIA)
                            catalog = self._merge_catalog(catalog, opened.url, visible)
                        except Error:
                            pass
                await page.add_init_script(
                    "window.__framekeepEncrypted = false; document.addEventListener('encrypted', () => { window.__framekeepEncrypted = true; }, true);"
                )
                user_agent = await page.evaluate("navigator.userAgent")
                observed = []
                chunlou_observer = (
                    await PlaylistObserver.attach(page, start_url, chunlou_id) if chunlou_probe else None
                )
                unavailable = set()
                xhr_access_denied = False

                def response_seen(response):
                    nonlocal xhr_access_denied
                    if (read_only and response.status in {401, 402, 403}
                            and response.request.resource_type in {"fetch", "xhr"}
                            and same_origin_url(response.url, start_url)):
                        xhr_access_denied = True
                    if response.status >= 400:
                        request = response.request
                        while request is not None and len(unavailable) < 5000:
                            unavailable.add(urldefrag(request.url)[0])
                            request = request.redirected_from
                        return
                    if len(observed) >= 3500 or not 200 <= response.status < 300:
                        return
                    mime = response.headers.get("content-type", "")
                    media_type = _media_type(response.url, mime)
                    if media_type:
                        observed.append({"url": response.url, "kind": media_type[0], "mime": mime})

                page.on("response", response_seen)
                while pending and visited < limit and len(seen_assets) < max_assets:
                    if cancelled():
                        return await report("扫描已取消", "cancelled")
                    url, level = pending.popleft()
                    if url not in recheck_urls and (options.get("seedUrls") or level > 0) and any(
                        entry.get("pageUrl") == url and entry.get("access") == "needs-user"
                        for entry in (catalog or {}).get("entries", [])
                    ):
                        gated = True
                        await report("已跳过需要站点权限的剧集；解锁后可单独检查该页面")
                        continue
                    observed.clear()
                    unavailable.clear()
                    fresh_episode = False
                    requested_page, route_changed, blocked_requests = url, False, 0
                    if url in recheck_urls:
                        recheck_attempts.append(url)
                    visited += 1
                    if capture_art:
                        attempted_pages.append(url)
                    await report(f"正在检查第 {visited} 个页面")
                    try:
                        attempts = 2 if protected_scan else 1
                        for attempt in range(attempts):
                            try:
                                if attempt and same_page(page.url, url):
                                    # Repeating goto on an SPA hash can reuse the previous error document.
                                    response = await page.reload(wait_until="domcontentloaded", timeout=20_000)
                                else:
                                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                            except Error as error:
                                if route_changed or not transient_navigation_error(error):
                                    raise
                                response = None
                            else:
                                if response is None or response.status not in {502, 503, 504}:
                                    break
                            if attempt + 1 == attempts:
                                raise PageLoadFailure("网络或出口代理连接失败，请稍后重试")
                            if invalidate := getattr(self.policy, "invalidate_dns", None):
                                invalidate(url)
                            observed.clear()
                            unavailable.clear()
                            await report("页面连接中断，正在重新连接")
                            await asyncio.sleep(0.5)
                            if cancelled():
                                return await report("扫描已取消", "cancelled")

                        if protected_scan and (route_changed or not same_page(page.url, url)):
                            return await stop_changed_page(url)
                        # Do not follow a site's redirect into unrelated origins when crawling.
                        if urlsplit(page.url).hostname != hostname:
                            await report("页面跳转到其他站点，已停止遍历此分支")
                            continue
                        if response is not None and response.status == 429:
                            return {**await report("站点请求过于频繁（HTTP 429），请稍后重新扫描", "failed"),
                                    "_stopBatch": True}
                        if response is not None and response.status in {401, 403, 402}:
                            if same_page(page.url, url):
                                page_access[url] = "needs-user"
                            gated = True
                            catalog = self._merge_catalog(catalog, url, {}, access="needs-user")
                            await report(
                                "页面要求登录、访问权限或人工验证，请在浏览器窗口中处理",
                                "needs-user",
                            )
                            continue
                        if read_only and response is not None and response.status >= 400:
                            return await report(f"目标页面返回 HTTP {response.status}，请检查网址或稍后重试", "failed")
                        if chunlou_probe:
                            return await probe_chunlou_page(
                                page, url, chunlou_observer, user_agent, cancelled
                            )
                        await page.wait_for_timeout(350)
                        contents = guarded_contents(await page.evaluate(_DOM_MEDIA))
                        if options.get("_siteIndex") and not contents["blocked"]:
                            data = await read_index_page(page, url, lambda: cancelled() or route_changed)
                            if cancelled():
                                return await report("扫描已取消", "cancelled")
                            if route_changed or not same_page(page.url, url):
                                return await stop_changed_page(url)
                            if xhr_access_denied and not data["shows"]:
                                return await report("站点目录接口需要授权", "needs-user")
                            if xhr_access_denied:
                                data["limited"] = True
                            return {**(await report("已读取站点剧集索引", "completed")), "index": data}
                        generic_discovery = None
                        if series_mode and not lustflix:
                            generic_discovery = await discover_catalog(
                                page,
                                url,
                                catalog,
                                lambda: cancelled() or route_changed,
                                max_steps=1 if contents["blocked"] else 20,
                            )
                            if options.get("_directoryOnly"):
                                directory_link = same_origin_url(generic_discovery.get("catalogLink"), url)
                            catalog = self._merge_catalog(
                                catalog, url, guarded_contents(generic_discovery)
                            )
                            if cancelled():
                                return await report("扫描已取消", "cancelled")
                            if route_changed or not same_page(page.url, url):
                                return await stop_changed_page(url)
                        if (
                            lustflix
                            and "video" in kinds
                            and build_catalog(url, [{"url": url}])["entries"]
                        ):
                            contents = guarded_contents(
                                await self._wait_for_watch_ready(
                                    page,
                                    url,
                                    contents,
                                    observed,
                                    lambda: cancelled() or route_changed,
                                )
                            )
                            if cancelled():
                                return await report("扫描已取消", "cancelled")
                            if route_changed or not same_page(page.url, url):
                                return await stop_changed_page(url)
                            catalog = self._merge_catalog(catalog, url, contents)
                        if contents["blocked"]:
                            if same_page(page.url, url):
                                page_access[url] = "needs-user"
                            gated = True
                            catalog = self._merge_catalog(
                                catalog, url, contents, access="needs-user"
                            )
                            await report(contents["blocked"], "needs-user")
                            continue
                        if lustflix and not read_only:
                            await open_episode_selector(page, url)
                            discovered = await collect_catalog(
                                page, url, lambda: cancelled() or route_changed
                            )
                            catalog = self._merge_catalog(
                                catalog, url, guarded_contents(discovered)
                            )
                            if cancelled():
                                return await report("扫描已取消", "cancelled")
                            if route_changed or not same_page(page.url, url):
                                return await stop_changed_page(url)
                        if not contents["assets"] and not observed:
                            # SPA shells can finish navigation before their first media is mounted.
                            try:
                                await page.wait_for_function(
                                    "() => !!document.querySelector('img, video, picture, [style*=\"background-image\"]')",
                                    timeout=3000,
                                )
                            except PlaywrightTimeoutError:
                                pass
                        for _ in range(3):
                            if cancelled():
                                return await report("扫描已取消", "cancelled")
                            await page.mouse.wheel(0, 650)
                            await page.wait_for_timeout(250)
                        contents = guarded_contents(await page.evaluate(_DOM_MEDIA))
                        if protected_scan and (route_changed or not same_page(page.url, url)):
                            return await stop_changed_page(url)
                        if contents["blocked"]:
                            if same_page(page.url, url):
                                page_access[url] = "needs-user"
                            gated = True
                            catalog = self._merge_catalog(
                                catalog, url, contents, access="needs-user"
                            )
                            await report(contents["blocked"], "needs-user")
                            continue
                        title = await page.title()
                        page_url = page.url
                        catalog = self._merge_catalog(
                            catalog, page_url, contents,
                            access="unknown" if url in recheck_urls else "observed",
                        )
                        if contents["encrypted"]:
                            drm = True
                            await report(
                                "检测到 DRM 加密媒体，不支持下载，也不会尝试绕过", "unsupported-drm"
                            )
                        previous_media = []
                        if (
                            context is not None
                            and not read_only
                            and not options.get("refresh")
                            and url not in recheck_urls
                            and hasattr(self.browser, "observed_media_for")
                        ):
                            previous_media = await self.browser.observed_media_for(
                                site["id"], page_url
                            )
                        candidates = self._candidates(
                            contents["assets"], [*previous_media, *observed], page_url
                        )
                        if self.extractor and "video" in kinds and not contents["encrypted"]:
                            extracted = await self.extractor.extract(
                                site,
                                page_url,
                                headers={"Referer": page_url, "User-Agent": user_agent},
                                cancelled=cancelled,
                                html=await page.content(),
                                **({"read_only": True} if read_only else {}),
                            )
                            extraction = {
                                key: extracted.get(key)
                                for key in ("status", "message", "extractor")
                            }
                            extra = [
                                {**item, "url": item["sourceUrl"]}
                                for item in extracted.get("assets", [])
                            ]
                            candidates = self._candidates(candidates, extra, page_url)
                            catalog = self._merge_catalog(
                                catalog,
                                page_url,
                                {
                                    "links": extracted.get("entries", []),
                                    "linksTruncated": extracted.get("status") == "limited",
                                },
                            )
                        if protected_scan and (route_changed or not same_page(page.url, url)):
                            return await stop_changed_page(url)
                        series_entry = next(
                            (
                                entry
                                for entry in (catalog or {}).get("entries", [])
                                if entry["pageUrl"] == page_url
                            ),
                            None,
                        )
                        if series_mode and not series_entry:
                            observed_show = options.get("_directoryOnly") or (catalog or {}).get("entries")
                            candidates = [item for item in candidates if capture_art and observed_show
                                          and item.get("kind") == "image" and item.get("pageRegion") == "content"]
                        if capture_art:
                            candidates = [item for item in candidates if item.get("pageRegion") != "auxiliary"
                                          and (item.get("kind") != "image" or item.get("pageRegion") == "content")]
                        for candidate in candidates:
                            if cancelled():
                                return await report("扫描已取消", "cancelled")
                            if protected_scan and (route_changed or not same_page(page.url, url)):
                                return await stop_changed_page(url)
                            if len(seen_assets) >= max_assets:
                                break
                            source_url = candidate["url"]
                            media_type = _media_type(
                                source_url, candidate.get("mime", ""), candidate.get("kind")
                            )
                            if (
                                not media_type
                                or ((source_url, page_url) in seen_observations if capture_art else source_url in seen_assets)
                                or source_url in unavailable
                                or media_type[0] not in kinds
                            ):
                                continue
                            kind, engine = media_type
                            if kind == "image" and _decorative(candidate):
                                continue
                            if kind == "video" and contents["encrypted"]:
                                continue
                            try:
                                source_url = await self.policy.validate(source_url)
                            except ValueError:
                                continue
                            candidate_page = candidate.get("pageUrl") or page_url
                            if (capture_art or options.get("mode") == "single") and not same_page(candidate_page, page_url):
                                continue
                            try:
                                candidate_page = await self.policy.validate(candidate_page)
                            except ValueError:
                                continue
                            seen_assets.add(source_url)
                            if capture_art:
                                seen_observations.add((source_url, page_url))
                            asset = self._asset_record(
                                candidate,
                                source_url,
                                candidate_page,
                                title or site["name"],
                                kind,
                                engine,
                                user_agent,
                            )
                            if (
                                series_mode
                                and not lustflix
                                and series_entry
                                and candidate.get("mainVideo")
                            ):
                                asset["_episodeEvidence"] = {
                                    "version": 1,
                                    "adapterId": "generic",
                                    "source": "observed-main-video",
                                    "seriesKey": catalog["seriesKey"],
                                    "episodeKey": series_entry["episodeKey"],
                                    "ordinal": series_entry["ordinal"],
                                    "pageUrl": page_url,
                                    "sourceUrl": source_url,
                                    "catalogUrl": catalog["catalogUrl"],
                                }
                                asset["classification"] = classify_asset(asset)
                            if single_video and kind == "video" and asset.get("classification", {}).get("kind") not in {"preview", "audio"}:
                                observed_video = True
                            await on_asset(asset)
                            if (url in recheck_urls and candidate_page == url and kind == "video"
                                    and asset.get("classification", {}).get("kind") == "episode"):
                                verified_main = (
                                    bool(stable_asset_identity(asset)) if lustflix
                                    else _generic_episode_evidence(
                                        asset, urlsplit(source_url), urlsplit(candidate_page)
                                    )
                                )
                                fresh_episode = fresh_episode or verified_main
                        if series_mode:
                            if series_entry:
                                scanned_episodes.add(page_url)
                            if follow_catalog and not options.get("_directoryOnly") and "catalog-conflict" not in (
                                generic_discovery or {}
                            ).get("reasons", []):
                                entries = (catalog or {}).get("entries", [])
                                excluded_pages = {
                                    entry["pageUrl"] for entry in entries
                                    if entry["episodeKey"] in excluded_keys
                                }
                                targets = [
                                    entry["pageUrl"]
                                    for entry in entries
                                    if entry.get("access") != "needs-user"
                                ]
                                catalog_link = (generic_discovery or {}).get("catalogLink")
                                if catalog_link and (
                                    not (catalog or {}).get("seriesKey")
                                    or catalog_link == catalog.get("catalogUrl")
                                ):
                                    targets.append(catalog_link)
                                for target in targets:
                                    if (
                                        target in excluded_pages
                                        or target in scanned_episodes
                                        or target == page_url
                                        or any(item[0] == target for item in pending)
                                    ):
                                        continue
                                    if target in queued and target not in {
                                        entry["pageUrl"] for entry in entries
                                    }:
                                        continue
                                    if not same_origin_url(target, start_url):
                                        continue
                                    try:
                                        target = await self.policy.validate(target)
                                    except ValueError:
                                        continue
                                    if visited + len(pending) >= limit:
                                        catalog["truncated"] = True
                                        catalog["reasons"] = list(
                                            dict.fromkeys(
                                                [*catalog.get("reasons", []), "page-limit"]
                                            )
                                        )
                                        break
                                    queued.add(target)
                                    pending.append((target, level + 1))
                        elif level < depth:
                            blocked_pages = {
                                entry["pageUrl"]
                                for entry in (catalog or {}).get("entries", [])
                                if entry.get("access") == "needs-user"
                            }
                            for link in contents["links"]:
                                if len(queued) >= limit:
                                    break
                                next_url = self._crawl_link(
                                    link, page_url, hostname, series_start=series_start
                                )
                                if (
                                    next_url is None
                                    or next_url in queued
                                    or next_url in blocked_pages
                                ):
                                    continue
                                try:
                                    next_url = await self.policy.validate(next_url)
                                except ValueError:
                                    continue
                                queued.add(next_url)
                                pending.append((next_url, level + 1))
                        if not route_changed and same_page(page.url, url):
                            if url not in recheck_urls or fresh_episode:
                                if url in recheck_urls:
                                    catalog = self._merge_catalog(catalog, url, {}, access="observed")
                                page_access[url] = "observed"
                        await report(f"已检查 {visited} 个页面，发现 {len(seen_assets)} 个素材")
                    except PageLoadFailure as error:
                        load_failures.append(str(error))
                        await report(str(error))
                    except (PlaywrightTimeoutError, Error):
                        if protected_scan and route_changed:
                            return await stop_changed_page(url)
                        load_failures.append("页面加载超时或不可访问")
                        await report("页面加载超时或不可访问，已跳过此页面")
                missing_single_video = single_video and not observed_video
                if xhr_access_denied and (options.get("ruleId") or not seen_assets or missing_single_video):
                    page_access[start_url] = "needs-user"
                    return await report("目标页面的同站数据接口要求授权，请在站点窗口手动检查后重试", "needs-user")
                if missing_single_video and (extraction or {}).get("status") == "needs-user":
                    page_access[start_url] = "needs-user"
                    return await report(extraction.get("message") or "播放器要求授权，请处理后重新读取", "needs-user")
                if load_failures and not seen_assets and not gated:
                    return await report(load_failures[-1], "failed")
                if gated and not seen_assets:
                    return await report(
                        "需要在站点浏览器窗口中完成登录或人工验证后重新扫描", "needs-user"
                    )
                if drm and (not seen_assets or missing_single_video):
                    return await report(
                        "检测到 DRM 加密媒体，此类内容不支持下载", "unsupported-drm"
                    )
                if len(seen_assets) >= max_assets:
                    if catalog:
                        catalog["truncated"] = True
                    return await report(
                        f"已达到本次素材数量上限 {max_assets} 条，页面素材可能尚未全部收录；"
                        "本次扫描不代表全站结果，可缩小范围或按类型扫描",
                        "completed",
                    )
                if pending:
                    if catalog:
                        catalog["truncated"] = True
                if load_failures:
                    return await report(f"已发现 {len(seen_assets)} 个素材，{len(load_failures)} 页加载失败。{load_failures[-1]}", "completed")
                return await report(f"扫描完成，发现 {len(seen_assets)} 个可访问素材", "completed")
        except TimeoutError:
            if catalog:
                catalog["truncated"] = True
            return await report("已达到扫描时间上限，保留当前已发现素材", "timed-out")
        finally:
            if scan_context is not None:
                await scan_context.close()
            elif page is not None and not page.is_closed():
                await page.close()

    async def _scan_post(self, site, url, kinds, on_asset, on_progress, cancelled):
        target = short_target(url)
        count = 0
        if target and self.extractor:
            extracted = await self.extractor.extract(site, target.url, read_only=True,
                                                    post_target=target, max_entries=50, cancelled=cancelled)
            state = extracted["status"]
            if state in {"resolved", "limited"}:
                for item in extracted["assets"]:
                    if cancelled():
                        break
                    if item.get("kind") in kinds:
                        await on_asset(item)
                        count += 1
                status = "completed"
                message = f"已识别这条作品，发现 {count} 项所选类型的素材"
                if state == "limited":
                    message += "，已达到本次提取上限"
            else:
                status = state if state in {"needs-user", "unsupported-drm"} else "failed"
                message = extracted["message"]
        else:
            status, message = "failed", "请输入该平台的单条作品或视频分享链接"
        if cancelled():
            status, message = "cancelled", "作品解析已取消"
        result = {"status": status, "message": message, "pagesVisited": int(bool(target)),
                  "assetsFound": count, "limited": bool(target and self.extractor and extracted["status"] == "limited")}
        await on_progress(result)
        return result

    async def resolve_asset(self, site, asset, cancelled):
        """Refresh the original page once; never infer identity from a title or duration."""
        candidates, reports = [], []

        async def collect(value):
            candidates.append(value)

        async def progress(value):
            reports.append(value)

        scan_options = {
            "url": asset["pageUrl"],
            "depth": 0,
            "limit": 1,
            "maxAssets": 100,
            "kinds": [asset["kind"]],
            "refresh": True,
            "readOnly": bool(asset.get("_readOnly")),
        }
        if asset.get("shortMedia"):
            scan_options.update(mode="single", readOnly=True)
        try:
            source, page = urlsplit(asset.get("sourceUrl") or ""), urlsplit(asset["pageUrl"])
            valid_evidence = _generic_episode_evidence(asset, source, page)
            if lustflix_stream_matches(source, page):
                scan_options.update(mode="series", readOnly=True, seedUrls=[asset["pageUrl"]],
                                    followCatalog=False, _seriesCheckSession={})
        except (TypeError, ValueError):
            valid_evidence = False
        if valid_evidence:
            evidence = asset["_episodeEvidence"]
            scan_options.update(
                mode="series",
                followCatalog=False,
                catalog=build_catalog(
                    asset["pageUrl"],
                    [{"url": asset["pageUrl"], "ordinal": evidence["ordinal"]}],
                    series_metadata=evidence,
                ),
            )
        await self.scan(
            site,
            scan_options,
            collect,
            progress,
            cancelled,
        )
        if cancelled():
            raise asyncio.CancelledError()
        statuses = [report.get("status") for report in reports]
        if "needs-user" in statuses or any(
            report.get("extraction", {}).get("status") == "needs-user" for report in reports
        ):
            return {"status": "needs-user", "reasonCode": "access-required"}
        if "unsupported-drm" in statuses or "unsupported-media" in statuses:
            return {"status": "unsupported", "reasonCode": "unsupported-media"}
        if any(
            report.get("limited")
            or report.get("status") in {"timed-out", "failed"}
            or report.get("extraction", {}).get("status") == "limited"
            for report in reports
        ):
            return {"status": "needs-user", "reasonCode": "refresh-failed"}
        matches = {
            item["sourceUrl"]: item for item in candidates if refresh_identity_matches(asset, item)
        }
        if len(matches) == 1:
            return {"status": "resolved", "candidate": next(iter(matches.values()))}
        reason = (
            "ambiguous-media"
            if matches
            else "identity-mismatch"
            if candidates
            else "media-unavailable"
        )
        return {"status": "needs-user", "reasonCode": reason}

    @staticmethod
    def _merge_catalog(previous, page_url, contents, access="unknown"):
        metadata = contents.get("seriesMetadata")
        if (
            not metadata
            and (previous or {}).get("adapterId") == "generic"
            and previous.get("seriesKey")
        ):
            metadata = {key: previous.get(key) for key in ("seriesKey", "catalogUrl", "title")}
        current_entry = next(
            (
                entry
                for entry in (previous or {}).get("entries", [])
                if entry["pageUrl"] == page_url
            ),
            {},
        )
        links = [
            *contents.get("links", []),
            {"url": page_url, "ordinal": current_entry.get("ordinal"), "access": access},
        ]
        current = build_catalog(
            page_url,
            links,
            expected_count=contents.get("expectedCount"),
            truncated=contents.get("linksTruncated", False),
            reasons=contents.get("reasons"),
            series_metadata=metadata,
        )
        current["discoverySources"] = [source for source in contents.get("discoverySources", [])
                                      if source in {"dom", "json-ld"}]
        if current.get("entries") and "catalog-conflict" not in current.get("reasons", []):
            covers = [url for value in contents.get("coverUrls", [])[:4]
                      if isinstance(value, str) and len(value) <= 8192
                      and (url := _absolute_url(value, page_url, media=True))]
            if covers:
                current["coverUrls"] = covers
        return merge_catalog(previous, current, observed_page=page_url, access=access)

    @staticmethod
    def _asset_record(candidate, source_url, page_url, title, kind, engine, user_agent):
        asset = {
            "sourceUrl": source_url,
            "pageUrl": page_url,
            "title": candidate.get("title") or title,
            "kind": kind,
            "width": int(candidate.get("width", 0)),
            "height": int(candidate.get("height", 0)),
            "mime": candidate.get("mime", ""),
            "engine": engine,
            "locked": False,
            "category": "图片素材" if kind == "image" else "视频素材",
            "_headers": {"Referer": page_url, "User-Agent": user_agent},
        }
        thumbnail = candidate.get("thumbnail") or (source_url if kind == "image" else "")
        if thumbnail:
            thumbnail = _absolute_url(thumbnail, page_url, media=True)
            if thumbnail:
                asset["thumbnail"] = thumbnail
        if candidate.get("duration") is not None:
            asset["duration"] = candidate["duration"]
        for key in ("_protocol", "_resolver", "_formatId"):
            if key in candidate:
                asset[key] = candidate[key]
        if candidate.get("_headers"):
            asset["_headers"] = dict(candidate["_headers"])
        if kind == "video":
            asset["_mediaLabel"] = str(candidate.get("mediaLabel", ""))[:200]
            asset["classification"] = classify_asset(asset)
        return asset

    @staticmethod
    def _candidates(dom_assets, observed, page_url):
        merged = {}
        # DOM dimensions must win over network-only records so tiny UI images can be removed.
        for candidate in [*observed, *dom_assets]:
            url = _absolute_url(candidate.get("url", ""), page_url, media=True)
            if url is None:
                continue
            previous = merged.setdefault(url, {"url": url})
            for key, value in candidate.items():
                if key != "url" and value not in (None, "", 0):
                    previous[key] = value
        return list(merged.values())

    @staticmethod
    async def _wait_for_watch_ready(page, url, contents, observed, cancelled, *, max_seconds=8):
        """Wait for watch-page evidence, never for an image placeholder or empty player."""
        try:
            async with asyncio.timeout(max_seconds):
                while True:
                    if cancelled() or not same_page(page.url, url) or contents.get("blocked"):
                        return contents
                    media_ready = any(
                        _absolute_url(candidate.get("url", ""), url, media=True)
                        and (
                            kind := _media_type(
                                candidate.get("url", ""),
                                candidate.get("mime", ""),
                                candidate.get("kind"),
                            )
                        )
                        and kind[0] == "video"
                        for candidate in [*contents.get("assets", []), *observed]
                    )
                    if media_ready and contents.get("episodeReady"):
                        return contents
                    await page.wait_for_timeout(200)
                    contents = await page.evaluate(_DOM_MEDIA)
        except TimeoutError:
            return {
                **contents,
                "linksTruncated": True,
                "reasons": [*contents.get("reasons", []), "catalog-limit"],
            }

    @staticmethod
    def _crawl_link(link, page_url, hostname, *, series_start=None):
        if link.get("access") == "needs-user":
            return None
        url = _absolute_url(link["url"], page_url)
        if url is None:
            return None
        parsed = urlsplit(url)
        if parsed.hostname != hostname or _ACCOUNT_LINK.search(parsed.path):
            return None
        if series_start:
            start = urlsplit(series_start)

            def origin(target):
                return (
                    target.scheme,
                    target.hostname,
                    target.port or (443 if target.scheme == "https" else 80),
                )

            source_catalog = build_catalog(series_start, [])
            target_catalog = build_catalog(url, [{"url": url}])
            if (
                not target_catalog["entries"]
                or origin(parsed) != origin(start)
                or any(
                    target_catalog[key] != source_catalog[key] for key in ("adapterId", "seriesKey")
                )
            ):
                return None
        if re.search(
            r"\b(log ?in|log ?out|sign ?in|sign ?out|subscribe|buy now)\b|退出登录|立即购买|立即订阅",
            link.get("text", ""),
            re.I,
        ):
            return None
        if _media_type(url) or _extension(url) in {".pdf", ".zip", ".exe", ".dmg"}:
            return None
        current = urlsplit(page_url)
        # Preserve query-driven detail pages and SPA route fragments; skip in-page anchors.
        if parsed.path == current.path and parsed.query == current.query and parsed.fragment:
            if not parsed.fragment.startswith(("/", "!/", "?")):
                return None
        return url
