"""Isolated, user-operated browser sessions with encrypted local persistence."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from playwright.async_api import BrowserContext, Error, Frame, Page, Request, async_playwright

from backend.chunlou import restore_account_key
from backend.session_refresh import prepare_scan_state

VIEWPORT = {"width": 1280, "height": 800}
_SESSION_SAVE_TIMEOUT = 15
_SESSION_QUIET_PERIOD = 0.5


def _cookies_for_url(cookies, url):
    """Match persisted Playwright cookies without creating a browser context."""
    target = urlsplit(url)
    hostname, path, now = (target.hostname or "").lower(), target.path or "/", time.time()
    matched = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lower()
        base = domain.lstrip(".")
        if not base or not (
            hostname == base or (domain.startswith(".") and hostname.endswith("." + base))
        ):
            continue
        cookie_path = cookie.get("path") or "/"
        if path != cookie_path and not (
            path.startswith(cookie_path)
            and (cookie_path.endswith("/") or path[len(cookie_path) :].startswith("/"))
        ):
            continue
        if cookie.get("secure") and target.scheme != "https":
            continue
        try:
            expires = float(cookie.get("expires", -1))
        except (TypeError, ValueError):
            continue
        if expires != -1 and (not math.isfinite(expires) or expires <= now):
            continue
        matched.append(dict(cookie))
    return matched


@dataclass
class _PendingRequest:
    page: Page | None
    frame: Frame | None
    sequence: int
    navigation: bool


@dataclass
class _Session:
    context: BrowserContext
    active: Page | None = None
    tabs: dict[str, Page] = field(default_factory=dict)
    observed_media: OrderedDict = field(default_factory=OrderedDict)
    pending_requests: dict[Request, _PendingRequest] = field(default_factory=dict)
    request_sequence: int = 0
    request_epoch: int = 0
    network_changed: asyncio.Event = field(default_factory=asyncio.Event)
    document_markers: dict[Frame, str] = field(default_factory=dict)
    document_versions: dict[Frame, int] = field(default_factory=dict)
    navigation_boundaries: dict[Frame, tuple[Request, int]] = field(default_factory=dict)
    document_tasks: dict[Frame, asyncio.Task] = field(default_factory=dict)
    marker_key: str = field(default_factory=lambda: "__framekeep_document_" + uuid.uuid4().hex)

    def register(self, page: Page) -> None:
        if page not in self.tabs.values():
            self.tabs[uuid.uuid4().hex] = page
            page.on("framenavigated", self.frame_navigated)
            page.on("framedetached", self.frame_detached)
            page.on("close", self.page_closed)

    def page_closed(self, page):
        # Playwright does not guarantee requestfailed events when an entire page closes.
        removed = [
            request for request, record in self.pending_requests.items() if record.page is page
        ]
        for request in removed:
            del self.pending_requests[request]
        frames = (
            set(self.document_markers)
            | set(self.document_versions)
            | set(self.navigation_boundaries)
            | set(self.document_tasks)
        )
        for frame in frames:
            if frame.page is page:
                self._forget_frame(frame)
        if removed:
            self._network_change()

    def frame_navigated(self, frame):
        if frame.is_detached() or frame.page.is_closed():
            return
        version = self.document_versions.get(frame, 0) + 1
        self.document_versions[frame] = version
        boundary = self.navigation_boundaries.get(frame)
        self._network_change()
        task = asyncio.create_task(self._sync_document(frame, version, boundary))
        self.document_tasks[frame] = task

        def finished(completed):
            if self.document_tasks.get(frame) is completed:
                self.document_tasks.pop(frame, None)
            self.network_changed.set()

        task.add_done_callback(finished)

    async def _sync_document(self, frame, version, boundary):
        try:
            marker = await asyncio.wait_for(
                frame.evaluate(
                    """key => {
              const existing = Object.getOwnPropertyDescriptor(document, key);
              if (existing) return typeof existing.value === 'string' ? existing.value : null;
              const value = Date.now().toString(36) + Math.random().toString(36).slice(2);
              Object.defineProperty(document, key, {value});
              return value;
            }""",
                    self.marker_key,
                ),
                timeout=2,
            )
        except (Error, TimeoutError):
            return
        if (
            not isinstance(marker, str)
            or self.document_versions.get(frame) != version
            or self.navigation_boundaries.get(frame) != boundary
        ):
            return
        previous = self.document_markers.get(frame)
        self.document_markers[frame] = marker
        if previous is not None and previous != marker and boundary is not None:
            self._retire_read_requests(frame, before=boundary[1])
        if boundary is not None and (previous is None or previous != marker):
            self.navigation_boundaries.pop(frame, None)

    def _retire_read_requests(self, frame, before=None):
        obsolete = [
            request
            for request, record in self.pending_requests.items()
            if record.frame is frame
            and not record.navigation
            and request.method in {"GET", "HEAD"}
            and (before is None or record.sequence < before)
        ]
        for request in obsolete:
            del self.pending_requests[request]
        if obsolete:
            self._network_change()

    def frame_detached(self, frame):
        self._forget_frame(frame)
        self._retire_read_requests(frame)

    def _forget_frame(self, frame):
        self.document_markers.pop(frame, None)
        self.document_versions.pop(frame, None)
        self.navigation_boundaries.pop(frame, None)
        task = self.document_tasks.pop(frame, None)
        if task is not None:
            task.cancel()
        self.network_changed.set()

    async def ensure_document(self, frame):
        if frame not in self.document_markers and frame not in self.document_tasks:
            self.frame_navigated(frame)
        task = self.document_tasks.get(frame)
        if task is not None:
            await asyncio.shield(task)

    def _network_change(self):
        self.request_epoch += 1
        self.network_changed.set()

    def request_started(self, request):
        # Authentication is commonly a document navigation or an SPA fetch/XHR.
        # Images, video streams and WebSockets must not keep session saving blocked.
        if request.resource_type in {"document", "fetch", "xhr"}:
            try:
                frame = request.frame
                owner = frame.page
            except Error:
                frame, owner = None, None
            self.request_sequence += 1
            navigation = request.is_navigation_request()
            self.pending_requests[request] = _PendingRequest(
                owner, frame, self.request_sequence, navigation
            )
            if navigation and frame is not None:
                self.navigation_boundaries[frame] = (request, self.request_sequence)
            self._network_change()

    def request_finished(self, request):
        if request in self.pending_requests:
            del self.pending_requests[request]
            self._network_change()

    async def wait_for_settled_requests(self):
        while True:
            for owner in {record.page for record in self.pending_requests.values()}:
                if owner is not None and owner.is_closed():
                    self.page_closed(owner)
            epoch = self.request_epoch
            self.network_changed.clear()
            if self.pending_requests or self.document_tasks:
                await self.network_changed.wait()
                continue
            try:
                await asyncio.wait_for(self.network_changed.wait(), timeout=_SESSION_QUIET_PERIOD)
            except TimeoutError:
                if (
                    not self.pending_requests
                    and not self.document_tasks
                    and self.request_epoch == epoch
                ):
                    return epoch

    def observe_response(self, response) -> None:
        if not 200 <= response.status < 300:
            return
        url = response.url
        extension = urlsplit(url).path.rsplit(".", 1)[-1].lower()
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if extension in {"ts", "m4s", "cmfv", "cmfa", "aac", "vtt"}:
            return
        if not (
            mime.startswith(("image/", "video/"))
            or extension in {"m3u8", "mpd"}
            or mime
            in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}
        ):
            return
        try:
            page_url = response.request.frame.page.url
        except Error:
            return
        key = (page_url, url)
        # Keep only response metadata in memory; never response bodies or request credentials.
        self.observed_media[key] = {"url": url, "mime": mime, "pageUrl": page_url}
        self.observed_media.move_to_end(key)
        if len(self.observed_media) > 500:
            self.observed_media.popitem(last=False)

    def current(self) -> Page:
        self.tabs = {key: page for key, page in self.tabs.items() if not page.is_closed()}
        if self.active is None or self.active.is_closed():
            self.active = next(iter(self.tabs.values()), None)
        if self.active is None:
            raise ValueError("浏览器窗口已关闭，请重新打开登录窗口")
        return self.active


class BrowserService:
    def __init__(self, data_dir: Path, policy, *, max_open_sessions=None):
        if max_open_sessions is not None and (type(max_open_sessions) is not int or max_open_sessions < 1):
            raise ValueError("站点窗口上限必须是正整数")
        self.session_dir = Path(data_dir) / "sessions"
        self.policy = policy
        self.max_open_sessions = max_open_sessions
        self._playwright = None
        self._browser = None
        self._cipher: Fernet | None = None
        self._sessions: dict[str, _Session] = {}
        self._media_cookies = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._startup_lock = asyncio.Lock()
        self._session_creation_lock = asyncio.Lock()

    def _state_path(self, site_id: str) -> Path:
        name = hashlib.sha256(str(site_id).encode()).hexdigest()
        return self.session_dir / f"{name}.enc"

    @staticmethod
    def _private_write(path: Path, content: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def start(self):
        async with self._startup_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            self.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.session_dir.chmod(0o700)
            key_path = self.session_dir / ".key"
            if not key_path.exists():
                self._private_write(key_path, Fernet.generate_key())
            key_path.chmod(0o600)
            self._cipher = Fernet(key_path.read_bytes())
            self._playwright = await async_playwright().start()
            try:
                proxy = None
                if getattr(self.policy, "proxy_url", None):
                    target = urlsplit(self.policy.proxy_url)
                    proxy = {
                        "server": f"http://{target.hostname}:{target.port}",
                        "username": unquote(target.username or ""),
                        "password": unquote(target.password or ""),
                    }
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    proxy=proxy,
                    args=[
                        "--autoplay-policy=user-gesture-required",
                        "--disable-quic",
                        "--proxy-bypass-list=<-loopback>",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ],
                )
            except Exception:
                await self._playwright.stop()
                self._playwright = None
                raise RuntimeError(
                    "Chromium 启动失败，请运行 uv run playwright install chromium"
                ) from None

    async def close(self):
        self._media_cookies.clear()
        sessions, self._sessions = self._sessions, {}
        for session in sessions.values():
            await session.context.close()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _guard_request(self, route):
        try:
            await self.policy.validate(route.request.url)
        except (ValueError, OSError):
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _guard_socket(self, route):
        # WebSockets use the same public-network policy as HTTP requests.
        url = route.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        try:
            await self.policy.validate(url)
        except (ValueError, OSError):
            await route.close()
            return
        route.connect_to_server()

    def _saved_state(self, site_id):
        state_path = self._state_path(site_id)
        if not state_path.exists():
            return None
        try:
            return json.loads(self._cipher.decrypt(state_path.read_bytes()))
        except (InvalidToken, ValueError, OSError):
            raise ValueError("保存的登录状态无法解密，请先断开此站点再重新登录") from None

    def _save_state(self, site_id, state):
        encrypted = self._cipher.encrypt(json.dumps(state, separators=(",", ":")).encode())
        self._private_write(self._state_path(site_id), encrypted)

    async def create_scan_context(self, site):
        """Copy authorization without opening or navigating a user browser window."""
        site_id = str(site["id"])
        async with self._locks.setdefault(site_id, asyncio.Lock()):
            await self.start()
            session = self._sessions.get(site_id)
            state = (
                await session.context.storage_state(indexed_db=True)
                if session is not None
                else self._saved_state(site_id)
            )
            state = await prepare_scan_state(
                self.policy,
                site.get("url"),
                state,
                lambda: self._saved_state(site_id),
                lambda updated: self._save_state(site_id, updated),
            )
            context = await self._browser.new_context(
                viewport=VIEWPORT,
                storage_state=restore_account_key(state, site.get("url")),
                service_workers="block",
                accept_downloads=False,
            )
            context.set_default_timeout(15_000)
            context.set_default_navigation_timeout(30_000)
            try:
                await context.route("**/*", self._guard_request)
                await context.route_web_socket("**/*", self._guard_socket)
                return context
            except Exception:
                await context.close()
                raise

    async def open_session(self, site: dict):
        # The optional public-mode cap must also cover windows opened by downloads.
        if self.max_open_sessions is None:
            return await self._open_session(site)
        async with self._session_creation_lock:
            return await self._open_session(site)

    async def _open_session(self, site: dict):
        site_id = str(site["id"])
        async with self._locks.setdefault(site_id, asyncio.Lock()):
            await self.start()
            if site_id in self._sessions:
                session = self._sessions[site_id]
                if any(not page.is_closed() for page in session.context.pages):
                    return await self.session_info(site_id)
                await session.context.close()
                del self._sessions[site_id]
            if self.max_open_sessions is not None:
                for identifier, session in list(self._sessions.items()):
                    if not any(not page.is_closed() for page in session.context.pages):
                        del self._sessions[identifier]
                        await session.context.close()
                if len(self._sessions) >= self.max_open_sessions:
                    raise ValueError(
                        f"同时打开的站点窗口已达 {self.max_open_sessions} 个上限；"
                        "请先在「来源」中断开不用的站点后重试。断开会删除该站点已保存的登录会话。"
                    )
            url = await self.policy.validate(site["url"])
            state = self._saved_state(site_id)
            context = await self._browser.new_context(
                viewport=VIEWPORT,
                storage_state=restore_account_key(state, site.get("url")),
                service_workers="block",
                accept_downloads=False,
            )
            try:
                context.set_default_timeout(15_000)
                context.set_default_navigation_timeout(30_000)
                await context.route("**/*", self._guard_request)
                await context.route_web_socket("**/*", self._guard_socket)
                session = _Session(context)
                context.on("page", session.register)
                context.on("response", session.observe_response)
                context.on("request", session.request_started)
                context.on("requestfinished", session.request_finished)
                context.on("requestfailed", session.request_finished)
                self._sessions[site_id] = session
                page = await context.new_page()
                session.register(page)
                session.active = page
                await page.goto(url, wait_until="domcontentloaded")
                await session.ensure_document(page.main_frame)
            except (Exception, asyncio.CancelledError):
                self._sessions.pop(site_id, None)
                await context.close()
                raise
            return await self.session_info(site_id)

    async def session_info(self, site_id):
        session = self._sessions.get(str(site_id))
        result = {
            "open": False,
            "url": "",
            "title": "",
            "saved": self._state_path(site_id).exists(),
            "verified": False,
            "tabs": [],
            **VIEWPORT,
        }
        if session is None:
            return result
        try:
            active = session.current()
        except ValueError:
            return result
        tabs = []
        for tab_id, page in list(session.tabs.items()):
            try:
                title = await page.title()
            except Error:
                title = ""
            tabs.append({"id": tab_id, "title": title, "url": page.url})
            if page is active:
                result.update({"title": title, "url": page.url, "activeTabId": tab_id})
        return {**result, "open": True, "tabs": tabs}

    def _session(self, site_id) -> _Session:
        try:
            return self._sessions[str(site_id)]
        except KeyError:
            raise ValueError("请先打开此站点的浏览器登录窗口") from None

    async def screenshot(self, site_id) -> bytes:
        page = self._session(site_id).current()
        return await page.screenshot(type="jpeg", quality=75, timeout=15_000)

    async def interact(self, site_id, action: dict):
        async with self._locks.setdefault(str(site_id), asyncio.Lock()):
            return await self._interact(site_id, action)

    async def _interact(self, site_id, action: dict):
        session = self._session(site_id)
        page = session.current()
        action_type = action.get("type")
        if action_type == "click":
            x, y = float(action["x"]), float(action["y"])
            if not (math.isfinite(x) and math.isfinite(y) and 0 <= x < 1280 and 0 <= y < 800):
                raise ValueError("点击坐标超出浏览器画面")
            await page.mouse.click(x, y)
        elif action_type == "key":
            key = str(action.get("key", ""))
            if not key or len(key) > 100:
                raise ValueError("无效按键")
            await page.keyboard.press(key)
        elif action_type == "text":
            text = str(action.get("text", ""))
            if len(text) > 10_000:
                raise ValueError("单次输入过长")
            await page.keyboard.insert_text(text)
        elif action_type == "scroll":
            delta = float(action.get("deltaY", 0))
            if not math.isfinite(delta):
                raise ValueError("无效滚动距离")
            await page.mouse.wheel(0, max(-2400, min(2400, delta)))
        elif action_type == "navigate":
            url = await self.policy.validate(str(action.get("url", "")))
            await page.goto(url, wait_until="domcontentloaded")
            await session.ensure_document(page.main_frame)
        elif action_type == "tab":
            tab = session.tabs.get(str(action.get("tabId", "")))
            if tab is None or tab.is_closed():
                raise ValueError("浏览器标签页已关闭")
            session.active = tab
        else:
            raise ValueError("不支持的浏览器操作")
        return await self.session_info(site_id)

    async def save_session(self, site_id):
        async with self._locks.setdefault(str(site_id), asyncio.Lock()):
            session = self._session(site_id)
            phase = "网络等待"
            captures = 0
            try:
                async with asyncio.timeout(_SESSION_SAVE_TIMEOUT):
                    while True:
                        phase = "网络等待"
                        epoch = await session.wait_for_settled_requests()
                        phase = "存储导出"
                        captures += 1
                        state = await session.context.storage_state(indexed_db=True)
                        if not session.pending_requests and session.request_epoch == epoch:
                            break
            except (TimeoutError, Error):
                counts = {
                    kind: sum(request.resource_type == kind for request in session.pending_requests)
                    for kind in ("document", "fetch", "xhr")
                }
                raise ValueError(
                    f"保存未完成（阶段：{phase}；在途 document={counts['document']}、"
                    f"fetch={counts['fetch']}、xhr={counts['xhr']}；存储导出尝试 {captures} 次），"
                    "此次未保存，请等待页面稳定后重试"
                ) from None
            self._save_state(site_id, state)
            return await self.session_info(site_id)

    async def import_local_session(self, site, state, *, keep_chunlou_age=False, check_idle=None):
        """Replace this site's encrypted state only after Chromium accepts the captured snapshot."""
        site_id = str(site["id"])
        async with self._locks.setdefault(site_id, asyncio.Lock()):
            await self.start()
            if keep_chunlou_age:
                # The user may have previously confirmed the site's prompt in its own window.
                # Only restore that original timestamp. Cookies and other account data can belong
                # to someone else and must not be mixed into the incoming account.
                previous = self._saved_state(site_id) or {}
                prior_origin = next(
                    (o for o in previous.get("origins", []) if o.get("origin") == "https://chunlou.tv"),
                    {},
                )
                previous_age = next(
                    (item.get("value") for item in prior_origin.get("localStorage", [])
                     if item.get("name") == "age-verified-at"),
                    None,
                )
                if isinstance(previous_age, str) and re.fullmatch(r"[0-9]{13}", previous_age):
                    state["origins"][0]["localStorage"].append(
                        {"name": "age-verified-at", "value": previous_age}
                    )
            check = await self._browser.new_context(storage_state=state, service_workers="block")
            await check.close()
            if check_idle:
                check_idle()
            self._save_state(site_id, state)
            stale = self._sessions.pop(site_id, None)
            if stale:
                with contextlib.suppress(Error):
                    await stale.context.close()
            for key in list(self._media_cookies):
                if key[0] == site_id:
                    del self._media_cookies[key]
            return {"open": False, "saved": True, "verified": False}

    async def disconnect(self, site_id):
        async with self._locks.setdefault(str(site_id), asyncio.Lock()):
            session = self._sessions.pop(str(site_id), None)
            if session is not None:
                await session.context.close()
            self._state_path(site_id).unlink(missing_ok=True)
            for key in list(self._media_cookies):
                if key[0] == str(site_id):
                    del self._media_cookies[key]
        return await self.session_info(site_id)

    async def context_for(self, site_id) -> BrowserContext:
        return self._session(site_id).context

    def remember_media_cookie(self, site_id, url, cookie):
        """Keep a short-lived playback grant for its exact signed media URL only."""
        if cookie.get("name") != "tt_chain_token" or not _cookies_for_url([cookie], url):
            return
        key = (str(site_id), url)
        self._media_cookies[key] = dict(cookie)
        self._media_cookies.move_to_end(key)
        if len(self._media_cookies) > 256:
            self._media_cookies.popitem(last=False)

    async def cookies_for(self, site_id, url) -> list[dict]:
        await self.policy.validate(url)
        async with self._locks.setdefault(str(site_id), asyncio.Lock()):
            session = self._sessions.get(str(site_id))
            if session is not None:
                cookies = await session.context.cookies([url])
            else:
                await self.start()
                state = self._saved_state(site_id) or {}
                cookies = _cookies_for_url(state.get("cookies", []), url)
            key = (str(site_id), url)
            grant = self._media_cookies.get(key)
            if grant is None:
                return cookies
            if not _cookies_for_url([grant], url):
                self._media_cookies.pop(key, None)
                return cookies
            return [grant, *(item for item in cookies if item["name"] != grant["name"])]

    async def observed_media_for(self, site_id, page_url) -> list[dict]:
        page_url = await self.policy.validate(page_url)
        return [
            dict(item)
            for item in self._session(site_id).observed_media.values()
            if item["pageUrl"] == page_url
        ]
