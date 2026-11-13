"""Bounded yt-dlp metadata extraction through Framekeep's authorized HTTP transport.

Generic/HTML5 and explicitly selected single-post adapters share one transport. yt-dlp does not
download media, load user configuration/plugins, or launch postprocessors. Only YouTube may use
its bundled, bounded EJS parser in a permission-restricted Deno subprocess.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import io
import json
import math
import os
import re
import threading
import time
from itertools import islice
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.extractor.generic import GenericIE
from yt_dlp.extractor.genericembeds import HTML5MediaEmbedIE
from yt_dlp.globals import plugin_dirs
from yt_dlp.networking import Request, RequestDirector, RequestHandler, Response
from yt_dlp.networking.exceptions import HTTPError, RequestError, UnsupportedRequest
from yt_dlp.utils import DownloadError, ExtractorError, unsmuggle_url

from backend.instagram_requests import READ_HEADERS, instagram_read_request
from backend.short_media import (
    SHORT_EXTRACTORS,
    ShortAccessRequired,
    ShortMediaUnsupported,
    ShortMetadataUnavailable,
    platform_for_url,
    short_target,
    watermark_evidence,
)
from backend.youtube_media import (
    YouTubeAccessRequired,
    YouTubeDRMUnsupported,
    YouTubeIE,
    YouTubeUnsupported,
    seed_youtube_cookies,
    youtube_allowed_request,
    youtube_asset,
    youtube_ydl_params,
)

# YoutubeDL's Python constructor also loads plugins. Disable discovery before any
# instance is created, including child yt-dlp processes used for local HLS muxing.
os.environ["YTDLP_NO_PLUGINS"] = "1"
plugin_dirs.value = []

_RESPONSE_LIMIT = 4 * 1024**2
_TOTAL_LIMIT = 16 * 1024**2
_REQUEST_LIMIT = 32
_TIME_LIMIT = 30
_HEADERS = {"accept", "user-agent", "referer", "origin", "authorization", "content-type"}
_VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "m4v", "mkv", "ogv", "avi"}
_READ_ONLY_HOSTS = {"lustflix.tv", "www.lustflix.tv"}
_YOUTUBE_HEADERS = {
    "x-youtube-client-name", "x-youtube-client-version", "x-goog-visitor-id",
    "x-goog-authuser", "x-goog-pageid", "x-origin", "x-youtube-bootstrap-logged-in",
}


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _url(value, base):
    if not isinstance(value, str):
        return None
    try:
        value = urldefrag(unsmuggle_url(urljoin(base, value))[0])[0]
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or len(value) > 16384
            or any(ord(character) < 32 for character in value)
        ):
            return None
        return value
    except ValueError:
        return None


def _scoped_headers(headers, target, credential_origin):
    result = {}
    for name, value in (headers or {}).items():
        name = name.lower()
        if (
            name in _HEADERS
            and isinstance(value, str)
            and len(value) <= 16384
            and not any(ord(character) < 32 or ord(character) == 127 for character in value)
            and (name != "authorization" or _origin(target) == credential_origin)
        ):
            result[name] = value
    return result


class _QuietLogger:
    def debug(self, *_args, **_kwargs):
        pass

    info = warning = error = stdout = debug


class _Stopped(RequestError):
    pass


class _BudgetExceeded(RequestError):
    pass


class _Bridge:
    def __init__(self, browser, policy, site, url, headers, cancelled, html, *, read_only=False):
        self.browser, self.policy, self.site, self.url = browser, policy, site, url
        # Keep the original extraction scope across embeds and cross-origin requests.
        self.post_target = None
        self.media_cookie = None
        self.share_cookie = None
        self.visitor_cookies = {}
        self.single_page = read_only
        self.read_only = read_only or any(
            urlsplit(source).hostname in _READ_ONLY_HOSTS
            for value in (url, site.get("url"))
            if (source := _url(value, url))
        )
        self.read_only_blocked = False
        self.headers = {"referer": url, "user-agent": "Framekeep/1.0", **(headers or {})}
        self.cancelled = cancelled
        self.html = html.encode("utf-8") if isinstance(html, str) else html
        self.loop = asyncio.get_running_loop()
        self.deadline = time.monotonic() + _TIME_LIMIT
        self.stopped = threading.Event()
        self.pending = set()
        self.lock = threading.Lock()
        self.requests = self.total = 0
        self.limited = self.needs_user = False

    def check(self):
        if self.stopped.is_set() or self.cancelled():
            raise _Stopped("提取已取消")
        if time.monotonic() >= self.deadline:
            self.limited = True
            raise _BudgetExceeded("提取已达到时间上限")

    def stop(self):
        self.stopped.set()
        with self.lock:
            for future in self.pending:
                future.cancel()

    def call(self, coroutine):
        try:
            self.check()
        except RequestError:
            coroutine.close()
            raise
        with self.lock:
            if self.stopped.is_set():
                coroutine.close()
                raise _Stopped("提取已取消")
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            self.pending.add(future)
        try:
            return future.result(timeout=max(0.01, self.deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            self.limited = True
            future.cancel()
            raise _BudgetExceeded("提取已达到时间上限") from None
        except concurrent.futures.CancelledError:
            raise _Stopped("提取已取消") from None
        finally:
            with self.lock:
                self.pending.discard(future)

    def account(self, amount, response_size):
        self.total += amount
        if response_size > _RESPONSE_LIMIT or self.total > _TOTAL_LIMIT:
            self.limited = True
            raise _BudgetExceeded("提取已达到响应大小上限")

    async def _cookies(self, target):
        parsed = urlsplit(target)
        host, path = parsed.hostname.lower(), parsed.path or "/"
        scoped = []
        cookies = await self.browser.cookies_for(self.site["id"], target)
        saved_names = {cookie.get("name") for cookie in cookies}
        cookies = [*cookies, *(cookie for name, cookie in self.visitor_cookies.items() if name not in saved_names)]
        if self.share_cookie:
            cookies = [self.share_cookie, *cookies]
        used_share_cookie = False
        for cookie in cookies:
            domain = str(cookie.get("domain", "")).lower()
            cookie_path = cookie.get("path") or "/"
            expires = cookie.get("expires", -1)
            if not (
                host == domain.lstrip(".") or (domain.startswith(".") and host.endswith(domain))
            ):
                continue
            if not (path == cookie_path or path.startswith(cookie_path.rstrip("/") + "/")):
                continue
            if cookie.get("secure") and parsed.scheme != "https":
                continue
            if expires and expires > 0 and expires <= time.time():
                continue
            name, value = str(cookie.get("name", "")), str(cookie.get("value", ""))
            if used_share_cookie and name == "ttwid":
                continue
            if name and not any(character in name + value for character in "\r\n;"):
                scoped.append(f"{name}={value}")
                used_share_cookie |= cookie is self.share_cookie
        return "; ".join(scoped)

    def _capture_post_cookie(self, response):
        if not self.post_target or response.status_code != 200:
            return
        if self.post_target.platform == "instagram" and _origin(str(response.url)) == ("https", "www.instagram.com", 443):
            for cookie in response.cookies.jar:
                if (cookie.name in {"csrftoken", "mid", "ig_did", "ig_nrcb"}
                        and cookie.domain.lstrip(".") in {"instagram.com", "www.instagram.com"}
                        and cookie.value and len(cookie.value) <= 4096
                        and not any(ord(c) < 32 or ord(c) == 127 or c == ";" for c in cookie.value)):
                    self.visitor_cookies[cookie.name] = {
                        "name": cookie.name, "value": cookie.value, "domain": "www.instagram.com",
                        "path": "/", "secure": True, "expires": min(cookie.expires or time.time() + 1800, time.time() + 1800),
                    }
            return
        # Only these two non-account grants are needed. The visitor cookie is
        # scoped to this extraction. The playback cookie is later bound to one file.
        expected = {"tiktok": "tt_chain_token", "douyin": "ttwid"}.get(self.post_target.platform)
        for cookie in response.cookies.jar:
            domain = cookie.domain.lstrip(".")
            if (cookie.name != expected or platform_for_url(f"https://{domain}") != self.post_target.platform
                    or not cookie.value or len(cookie.value) > 4096 or any(c in cookie.value for c in "\r\n;")):
                continue
            expires = time.time() + 1800
            if cookie.expires is not None:
                expires = min(expires, cookie.expires)
            grant = {"name": cookie.name, "value": cookie.value, "path": cookie.path,
                     "domain": ("." if cookie.domain_specified else "") + domain,
                     "secure": True, "expires": expires}
            if cookie.name == "ttwid":
                self.share_cookie = grant
            else:
                self.media_cookie = grant

    async def probe_playback(self, url, source_headers):
        """Check a bounded MP4 prefix before preferring an alternate playback URL."""
        self.check()
        target = _url(url, self.url)
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                proxy=getattr(self.policy, "proxy_url", None),
                timeout=httpx.Timeout(8, connect=4),
            ) as client:
                for _ in range(5):
                    self.check()
                    if not target or urlsplit(target).scheme != "https":
                        return False
                    target = await self.policy.validate(target)
                    self.requests += 1
                    if self.requests > _REQUEST_LIMIT:
                        return False
                    headers = _scoped_headers({**self.headers, **source_headers}, target, _origin(self.url))
                    headers.update({"range": "bytes=0-4095", "accept-encoding": "identity"})
                    cookies = await self._cookies(target)
                    if cookies:
                        headers["cookie"] = cookies
                    client.cookies.clear()
                    async with client.stream("GET", target, headers=headers) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            target = _url(response.headers.get("location"), target)
                            continue
                        if (response.status_code not in {200, 206}
                                or response.headers.get("content-encoding", "identity") != "identity"):
                            return False
                        prefix = bytearray()
                        async for chunk in response.aiter_bytes(4096):
                            self.check()
                            prefix.extend(chunk)
                            self.account(len(chunk), len(prefix))
                            if len(prefix) >= 12:
                                return prefix[4:8] == b"ftyp"
                        return False
        except (httpx.HTTPError, ValueError):
            return False
        return False

    async def request(self, request, request_headers, *, resolve_post=False):
        self.check()
        target = _url(request.url, self.url)
        if not target:
            raise UnsupportedRequest("提取请求协议不受支持")
        method, data = request.method, request.data
        youtube = self.post_target and self.post_target.platform == "youtube"
        youtube_read = youtube and youtube_allowed_request(self.post_target, target, method, data)
        instagram_read = instagram_read_request(self.post_target, target, method, data)
        if self.read_only and method not in {"GET", "HEAD"} and not (youtube_read or instagram_read):
            self.needs_user = self.read_only_blocked = True
            raise UnsupportedRequest("只读提取已阻止写入请求，请在站点窗口中自行处理")
        if method not in {"GET", "HEAD", "POST"}:
            raise UnsupportedRequest("提取请求方法不受支持")
        if method == "POST":
            if not isinstance(data, bytes) or len(data) > 256 * 1024:
                raise UnsupportedRequest("提取请求内容不受支持")
            if not instagram_read:
                try:
                    json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    raise UnsupportedRequest("提取仅支持有界 JSON 请求") from None
        elif data:
            raise UnsupportedRequest("提取请求内容不受支持")
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            proxy=getattr(self.policy, "proxy_url", None),
            timeout=httpx.Timeout(10, connect=5),
        ) as client:
            for redirect in range(11):
                self.check()
                try:
                    if youtube and not youtube_allowed_request(self.post_target, target, method, data):
                        raise ValueError("YouTube 元数据请求超出当前视频范围")
                    if instagram_read and method == "POST" and not instagram_read_request(self.post_target, target, method, data):
                        raise ValueError("Instagram 元数据请求超出当前作品范围")
                    if self.post_target and not youtube and (
                        platform_for_url(target) != self.post_target.platform
                        or urlsplit(target).scheme != "https"
                    ):
                        raise ValueError("作品元数据重定向离开平台")
                    target = await self.policy.validate(target)
                except ValueError:
                    raise UnsupportedRequest("提取请求被站点网络规则拒绝") from None
                self.check()
                # A checked redirect already identifies the public Douyin post.
                # Its desktop player may have a different access policy from the
                # mobile publisher page used by the adapter. No player fetch is
                # needed just to resolve the ID. Return the canonical URL so an
                # iesdouyin share URL cannot send the adapter back to that player.
                if resolve_post and redirect and self.post_target and self.post_target.platform == "douyin":
                    resolved = short_target(target)
                    if resolved and resolved.platform == "douyin" and resolved.post_id:
                        return Response(io.BytesIO(b""), resolved.url, {})
                self.requests += 1
                if self.requests > _REQUEST_LIMIT:
                    self.limited = True
                    raise _BudgetExceeded("提取已达到请求数量上限")
                if self.html is not None and target == urldefrag(self.url)[0] and method == "GET":
                    body = self.html
                    self.account(len(body), len(body))
                    return Response(
                        io.BytesIO(body),
                        unsmuggle_url(request.url)[0],
                        {"Content-Type": "text/html; charset=utf-8"},
                    )
                headers = _scoped_headers(
                    {**self.headers, **request_headers}, target, _origin(self.url)
                )
                if youtube and _origin(target) == ("https", "www.youtube.com", 443):
                    for name, value in request_headers.items():
                        if (name.lower() in _YOUTUBE_HEADERS and isinstance(value, str)
                                and len(value) <= 4096
                                and not any(ord(c) < 32 or ord(c) == 127 for c in value)):
                            headers[name.lower()] = value
                if self.post_target and self.post_target.platform == "instagram" and urlsplit(target).hostname == "www.instagram.com":
                    for name, value in request_headers.items():
                        if (name.lower() in READ_HEADERS and isinstance(value, str) and len(value) <= 4096
                                and not any(ord(c) < 32 or ord(c) == 127 for c in value)):
                            headers[name.lower()] = value
                    headers["x-ig-app-id"] = "936619743392459"
                headers["accept-encoding"] = "identity"
                cookie = await self._cookies(target)
                if cookie:
                    headers["cookie"] = cookie
                    if instagram_read and method == "POST":
                        csrf = next((pair.partition("=")[2] for pair in cookie.split("; ") if pair.startswith("csrftoken=")), None)
                        if csrf:
                            headers["x-csrftoken"] = csrf
                self.check()
                client.cookies.clear()
                try:
                    async with client.stream(
                        method, target, headers=headers, content=data
                    ) as response:
                        self._capture_post_cookie(response)
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect == 10 or not response.headers.get("location"):
                                raise UnsupportedRequest("提取页面重定向次数超过限制")
                            target = _url(response.headers["location"], target)
                            if not target:
                                raise UnsupportedRequest("提取重定向地址不受支持")
                            if self.post_target:
                                before = urlsplit(str(response.url))
                                after = urlsplit(target)
                                if re.match(r"^/(?:accounts/(?:login|onetap)|login|challenge|checkpoint|passport)(?:/|$)", after.path):
                                    self.needs_user = True
                                    raise UnsupportedRequest("站点要求登录或人工验证")
                                same_endpoint = before.path.rstrip("/") == after.path.rstrip("/")
                                if not youtube and not same_endpoint and not short_target(target):
                                    raise UnsupportedRequest("作品页面重定向到了其他操作")
                            if response.status_code == 303 or (
                                response.status_code in {301, 302} and method == "POST"
                            ):
                                method, data = "GET", None
                            continue
                        if response.status_code in {401, 402, 403}:
                            self.needs_user = True
                        if (
                            response.headers.get("content-encoding", "identity").lower()
                            != "identity"
                        ):
                            # httpx decoders can allocate a large decompressed block
                            # before aiter_bytes applies its chunk size.
                            raise UnsupportedRequest("提取响应未遵守 identity 编码要求")
                        body = bytearray()
                        mime = response.headers.get("content-type", "").split(";", 1)[0]
                        sniff_only = mime.startswith(("video/", "audio/")) and "mpegurl" not in mime
                        async for chunk in response.aiter_bytes(64 * 1024):
                            self.check()
                            body.extend(chunk)
                            self.account(len(chunk), len(body))
                            if sniff_only:
                                break
                        wrapped = Response(
                            io.BytesIO(body), target, dict(response.headers), response.status_code
                        )
                        if response.status_code >= 400:
                            raise HTTPError(wrapped)
                        return wrapped
                except httpx.HTTPError:
                    raise RequestError("提取站点请求失败") from None
        raise UnsupportedRequest("提取页面重定向次数超过限制")


class FramekeepRH(RequestHandler):
    _SUPPORTED_URL_SCHEMES = ("http", "https")

    def __init__(self, bridge, **kwargs):
        super().__init__(**kwargs)
        self.bridge = bridge

    def _check_extensions(self, extensions):
        super()._check_extensions(extensions)
        for name in ("cookiejar", "timeout", "keep_header_casing"):
            extensions.pop(name, None)

    def _send(self, request):
        return self.bridge.call(self.bridge.request(request, self._get_headers(request)))


class _ManifestMetadata:
    def _parse_mpd_formats_and_subtitles(
        self, document, mpd_id=None, mpd_base_url="", mpd_url=None
    ):
        # Discovery must not expand attacker-controlled SegmentTimeline repeats.
        # The downloader's DASH adapter performs bounded parsing before transfer.
        if document.get("type") == "dynamic" or any(
            element.tag.rsplit("}", 1)[-1] == "ContentProtection" for element in document.iter()
        ):
            return [], {}
        return [{"url": mpd_url, "manifest_url": mpd_url, "protocol": "dash", "ext": "mp4"}], {}

    def _extract_mpd_formats_and_subtitles(self, url, video_id, *args, headers=None, **kwargs):
        return [
            {
                "url": url,
                "manifest_url": url,
                "protocol": "dash",
                "ext": "mp4",
                "http_headers": headers or {},
            }
        ], {}


class _GenericIE(_ManifestMetadata, GenericIE):
    @classmethod
    def ie_key(cls):
        return "Generic"


class _HTML5IE(_ManifestMetadata, HTML5MediaEmbedIE):
    @classmethod
    def ie_key(cls):
        return "HTML5MediaEmbed"


class _RestrictedYDL(YoutubeDL):
    def get_info_extractor(self, ie_key):
        if ie_key not in {"Generic", "HTML5MediaEmbed"}:
            raise ExtractorError("此嵌入提供方尚未启用", expected=True)
        return super().get_info_extractor(ie_key)


class ExtractorService:
    def __init__(self, browser, policy):
        self.browser, self.policy = browser, policy

    async def extract(
        self, site, page_url, *, headers=None, cancelled=lambda: False, max_entries=20, html=None,
        read_only=False, post_target=None,
    ):
        result = {
            "assets": [],
            "entries": [],
            "status": "unsupported",
            "message": "此页面暂未发现受支持的媒体",
            "extractor": "yt-dlp:generic",
        }
        if cancelled():
            result["message"] = "提取已取消"
            return result
        try:
            page_url = await self.policy.validate(page_url)
            max_entries = max(1, min(50 if post_target else 20, int(max_entries)))
        except (ValueError, TypeError, OverflowError):
            return result
        bridge = _Bridge(self.browser, self.policy, site, page_url, headers, cancelled, html, read_only=read_only)
        if post_target:
            bridge.post_target = post_target
            bridge.read_only = bridge.single_page = True
            result.update(extractor=f"yt-dlp:{post_target.platform}",
                          message="暂未取得这条作品的公开媒体，请检查链接或稍后重试")
        worker = asyncio.create_task(asyncio.to_thread(self._extract, bridge, max_entries, result))
        try:
            while not worker.done():
                if cancelled():
                    bridge.stop()
                    result["message"] = "提取已取消"
                    return {
                        **result,
                        "assets": list(result["assets"]),
                        "entries": list(result["entries"]),
                    }
                if time.monotonic() >= bridge.deadline:
                    bridge.limited = True
                    break
                await asyncio.wait({worker}, timeout=0.05)
            if worker.done():
                await worker
            if bridge.read_only_blocked:
                result.update(
                    status="needs-user",
                    reasonCode="read-only-blocked",
                    message="只读提取已阻止写入请求，请在站点窗口中自行处理后重新扫描",
                )
            elif bridge.needs_user:
                result.update(
                    status="needs-user", message="来源站点限制了本次访问，可能需要登录或人工验证；处理后可重试"
                )
            elif bridge.limited:
                result.update(
                    status="limited", message="已达到提取数量、时间或响应大小上限，结果可能不完整"
                )
            elif result["assets"] and result["status"] != "unsupported-drm":
                result.update(status="resolved", message="已通过 yt-dlp 解析页面媒体")
            if bridge.media_cookie and result["status"] in {"resolved", "limited"}:
                for asset in result["assets"]:
                    self.browser.remember_media_cookie(site["id"], asset["sourceUrl"], bridge.media_cookie)
            return {**result, "assets": list(result["assets"]), "entries": list(result["entries"])}
        finally:
            bridge.stop()
            # Do not abandon active transport tasks when the calling scan is cancelled.
            with bridge.lock:
                pending = list(bridge.pending)
            if pending:
                await asyncio.gather(
                    *(asyncio.wrap_future(future) for future in pending), return_exceptions=True
                )
            # Cancelling a to_thread task detaches the worker rather than stopping it.
            # HTTP calls and the per-extraction JS runtime observe bridge.stop(); wait
            # for their cleanup before releasing the scan's shared execution slot.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(worker)

    def _extract(self, bridge, limit, result):
        logger = _QuietLogger()
        params = {
            "quiet": True,
            "no_warnings": True,
            "logger": logger,
            "cachedir": False,
            "js_runtimes": {},
            "remote_components": [],
            "postprocessors": [],
            "skip_download": True,
            "check_formats": False,
            "proxy": "",
            "usenetrc": False,
            "extract_flat": True,
            "lazy_playlist": True,
            "socket_timeout": 10,
            "extractor_retries": 0,
            "playlist_items": f"1:{limit}",
            "dynamic_mpd": False,
        }
        if bridge.post_target and bridge.post_target.platform == "youtube":
            params.update(youtube_ydl_params())
        try:
            with _RestrictedYDL(params, auto_init=False) as ydl:
                ydl.add_info_extractor(_HTML5IE())
                ydl.add_info_extractor(_GenericIE())
                director = RequestDirector(logger)
                director.add_handler(FramekeepRH(bridge, logger=logger))
                ydl._request_director = director
                if bridge.post_target:
                    self._post(ydl, bridge, limit, result)
                    return
                seen_pages, seen_media = set(), set()

                def resolve(url, depth):
                    bridge.check()
                    if depth > 3 or url in seen_pages:
                        bridge.limited = True
                        return
                    seen_pages.add(url)
                    info = ydl.extract_info(url, download=False, process=False, ie_key="Generic")
                    visit(info, depth, url)

                def visit(info, depth, source_page):
                    bridge.check()
                    if not isinstance(info, dict):
                        return
                    if len(result["assets"]) >= limit:
                        bridge.limited = True
                        return
                    if info.get("_type") in {"playlist", "multi_video"}:
                        for index, entry in enumerate(islice(info.get("entries") or [], limit + 1)):
                            if index == limit:
                                bridge.limited = True
                                break
                            visit(entry, depth, source_page)
                        return
                    if info.get("_type") in {"url", "url_transparent"}:
                        target = _url(info.get("url"), source_page)
                        if target and info.get("ie_key") in {None, "Generic"}:
                            extension = urlsplit(target).path.lower().rsplit(".", 1)[-1]
                            if extension in _VIDEO_EXTENSIONS | {"m3u8", "mpd"}:
                                visit({**info, "_type": "video", "url": target}, depth, source_page)
                                return
                            if len(result["entries"]) < limit:
                                result["entries"].append(
                                    {"pageUrl": target, "title": str(info.get("title") or "")[:500]}
                                )
                            if not bridge.single_page:
                                resolve(target, depth + 1)
                        return
                    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
                        return
                    asset = self._asset(ydl, info, bridge, source_page)
                    if asset and asset["sourceUrl"] not in seen_media:
                        bridge.call(bridge.policy.validate(asset["sourceUrl"]))
                        bridge.check()
                        seen_media.add(asset["sourceUrl"])
                        result["assets"].append(asset)

                resolve(bridge.url, 0)
        except ShortAccessRequired:
            bridge.needs_user = True
        except (DownloadError, ExtractorError, RequestError, ValueError, TypeError, KeyError):
            # Upstream errors often include signed URLs, headers, or raw response
            # text; callers receive only the fixed messages above.
            return

    def _post(self, ydl, bridge, limit, result):
        target = bridge.post_target
        if target.platform == "youtube":
            self._youtube_post(ydl, bridge, target, result)
            return
        if not target.post_id:
            # Follow HTTP redirects through the guarded transport, then require a
            # concrete same-platform post before allowing any metadata extraction.
            response = bridge.call(bridge.request(Request(target.url), {}, resolve_post=target.platform == "douyin"))
            resolved = short_target(response.url)
            if not resolved or not resolved.post_id or resolved.platform != target.platform:
                result["message"] = "分享短链接未指向可识别的作品，请粘贴浏览器中的作品详情链接"
                return
            target = resolved
            bridge.post_target = resolved
            bridge.url = target.url
            bridge.headers["referer"] = target.url
        adapter = SHORT_EXTRACTORS[target.platform](ydl)
        if target.platform == "instagram":
            cookies = bridge.call(bridge._cookies(target.origin + "/"))
            adapter.has_session = any(pair.startswith("sessionid=") for pair in cookies.split("; "))
        try:
            entries = adapter.read_post(target)
        except ShortAccessRequired:
            raise
        except ShortMetadataUnavailable:
            result.update(status="unsupported", message="暂未取得公开媒体，请稍后重试")
            return
        except ShortMediaUnsupported:
            result.update(status="unsupported", message="该作品音视频描述暂不受支持，请使用站点中可完整播放的来源")
            return
        except (ExtractorError, DownloadError):
            entries = []
        if not entries and target.platform == "instagram" and not bridge.needs_user and not bridge.limited:
            entries = adapter.read_embed(target)
        if not entries and not bridge.needs_user and not bridge.limited:
            # A canonical social-share video can survive a platform parser change.
            # Require matching work IDs in both the final URL and the publisher metadata.
            html, handle = adapter._download_webpage_handle(target.url, target.post_id)
            declared = short_target(adapter._og_search_property("url", html, default=None))
            final = short_target(handle.url)
            video = adapter._og_search_video_url(html, default=None)
            if (declared and final and declared.platform == final.platform == target.platform
                    and declared.post_id == final.post_id == target.post_id and video):
                entries = [{"id": target.post_id, "url": video, "ext": "mp4",
                            "title": adapter._og_search_title(html, default="作品视频"),
                            "format_id": "og-video"}]
        for index, info in enumerate(islice(entries, limit + 1)):
            bridge.check()
            if index == limit:
                bridge.limited = True
                break
            if not isinstance(info, dict):
                continue
            if info.get("is_live") or info.get("has_drm"):
                if info.get("has_drm"):
                    result.update(status="unsupported-drm", message="该作品使用 DRM 保护")
                continue
            item_id = str(info.get("itemId") or info.get("id") or "")[:300]
            if info.get("kind") == "image":
                target_url = _url(info.get("url"), target.url)
                if not target_url:
                    continue
                asset = {"kind": "image", "engine": "http", "sourceUrl": target_url, "pageUrl": target.url,
                         "title": str(info.get("title") or "作品图片")[:500], "_headers": {"Referer": target.url},
                         "_protocol": "http", "_formatId": "picture", "thumbnail": target_url}
            else:
                candidates = info.get("formats") or []
                for fmt in candidates:
                    if fmt.get("_cleanCandidate"):
                        fmt["_playbackVerified"] = bridge.call(bridge.probe_playback(
                            fmt.get("url"), {**(info.get("http_headers") or {}),
                                            **(fmt.get("http_headers") or {})},
                        ))
                asset = self._asset(ydl, info, bridge, target.url)
                if not asset:
                    continue
            bridge.call(bridge.policy.validate(asset["sourceUrl"]))
            if audio := asset.get("_instagramAudio"):
                bridge.call(bridge.policy.validate(audio["url"]))
            asset["_resolver"] = f"yt-dlp:{target.platform}"
            asset["shortMedia"] = {"platform": target.platform, "postId": target.post_id,
                                  "itemId": item_id, "watermark": asset.pop("_watermark", "unknown")}
            result["assets"].append(asset)

    def _youtube_post(self, ydl, bridge, target, result):
        cookies = bridge.call(bridge.browser.cookies_for(bridge.site["id"], target.url))
        seed_youtube_cookies(ydl, cookies)
        try:
            entries = YouTubeIE(
                ydl, runtime_check=bridge.check, runtime_deadline=bridge.deadline,
            ).read_post(target)
            for info in islice(entries, 1):
                bridge.check()
                asset = youtube_asset(info, target, headers=bridge.headers)
                if asset:
                    for fmt in asset.get("_youtubeFormats", []):
                        bridge.call(bridge.policy.validate(fmt["url"]))
                    bridge.call(bridge.policy.validate(asset["sourceUrl"]))
                    result["assets"].append(asset)
        except YouTubeAccessRequired:
            bridge.needs_user = True
            return
        except YouTubeDRMUnsupported:
            result.update(status="unsupported-drm", message="该视频使用 DRM 保护，不能下载")
            return
        except YouTubeUnsupported:
            result.update(status="unsupported", message="此视频暂未提供可下载的完整音视频格式")
            return

    @staticmethod
    def _asset(ydl, info, bridge, source_page):
        formats = info.get("formats") or ([info] if info.get("url") else [])
        formats = [
            dict(fmt)
            for fmt in islice(formats, 501)
            if isinstance(fmt, dict) and not fmt.get("has_drm") and fmt.get("vcodec") != "none"
        ]
        if len(formats) > 500:
            bridge.limited = True
            return None
        supported = []
        for fmt in formats:
            if fmt.get("_cleanCandidate") and not fmt.get("_playbackVerified"):
                continue
            target = _url(fmt.get("manifest_url") or fmt.get("url"), source_page)
            if not target:
                continue
            extension = urlsplit(target).path.lower().rsplit(".", 1)[-1]
            protocol = fmt.get("protocol") or "https"
            if extension == "mpd" or protocol in {"dash", "http_dash_segments"}:
                fmt.update(_protocol="dash", _mime="application/dash+xml", _engine="yt-dlp")
            elif extension == "m3u8" or protocol in {"m3u8", "m3u8_native"}:
                fmt.update(_protocol="hls", _mime="application/vnd.apple.mpegurl", _engine="yt-dlp")
            elif (
                protocol in {"http", "https"} and (fmt.get("ext") or extension) in _VIDEO_EXTENSIONS
            ):
                fmt.update(
                    _protocol="http", _mime="video/" + (fmt.get("ext") or extension), _engine="http"
                )
            else:
                continue
            fmt["_target"] = target
            supported.append(fmt)
        if not supported:
            return None
        if bridge.post_target:
            # Prefer complete, playable renditions. A missing watermark flag is never proof.
            supported = [fmt for fmt in supported
                         if (fmt.get("acodec") != "none" or bridge.post_target.platform == "instagram")
                         and fmt.get("vcodec") != "bytevc2"]
            if not supported:
                return None
            rank = {"source-clean": 3, "playback": 2, "unknown": 1, "embedded": 0}
            for fmt in supported:
                fmt["_watermark"] = watermark_evidence(bridge.post_target.platform, fmt)
            best = max(rank[fmt["_watermark"]] for fmt in supported)
            supported = [fmt for fmt in supported if rank[fmt["_watermark"]] == best]
            compatible = [fmt for fmt in supported
                          if str(fmt.get("vcodec") or "").startswith(("h264", "avc1"))]
            if compatible:
                supported = compatible
        ydl.sort_formats({"formats": supported})
        selected = ydl._select_formats(supported, ydl.build_format_selector("best/bestvideo"))
        if not selected:
            return None
        fmt = selected[0]
        target = fmt["_target"]
        headers = _scoped_headers(
            {
                **bridge.headers,
                **(info.get("http_headers") or {}),
                **(fmt.get("http_headers") or {}),
            },
            target,
            _origin(bridge.url),
        )
        asset = {
            "sourceUrl": target,
            "pageUrl": source_page,
            "title": str(info.get("title") or "视频素材")[:500],
            "kind": "video",
            "engine": fmt["_engine"],
            "mime": fmt["_mime"],
            "_headers": headers,
            "_protocol": fmt["_protocol"],
            "locked": False,
            "category": "视频素材",
            "_resolver": "yt-dlp:generic",
            "_formatId": str(fmt.get("format_id") or "")[:100],
        }
        if bridge.post_target:
            asset["_watermark"] = fmt["_watermark"]
            if bridge.post_target.platform == "instagram":
                if type(fmt.get("_instagramAudioExpected")) is bool:
                    asset["_instagramAudioExpected"] = fmt["_instagramAudioExpected"]
                duration = fmt.get("_instagramDuration")
                if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
                    asset["_instagramDuration"] = duration
            if bridge.post_target.platform == "instagram" and fmt.get("_instagramAudio"):
                audio = fmt.get("_instagramAudio")
                audio_url = _url(audio.get("url"), source_page) if isinstance(audio, dict) else None
                if not audio_url or urlsplit(audio_url).scheme != "https":
                    return None
                asset.update(engine="yt-dlp", _protocol="instagram", _instagramAudioExpected=True,
                             _instagramAudio={"url": audio_url, "formatId": str(audio.get("formatId") or "")[:100],
                                              "language": str(audio.get("language") or "und")[:30]})
        for key in ("width", "height", "duration"):
            value = fmt.get(key) if fmt.get(key) is not None else info.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                asset[key] = value if key == "duration" else int(value)
                if key == "duration" and bridge.post_target and type(value) in (int, float) and value > 0:
                    asset["_shortDuration"] = value
        thumbnail = _url(info.get("thumbnail"), source_page)
        if not thumbnail:
            for candidate in islice(info.get("thumbnails") or [], 20):
                if isinstance(candidate, dict):
                    thumbnail = _url(candidate.get("url"), source_page) or thumbnail
        if thumbnail:
            asset["thumbnail"] = thumbnail
        return asset
