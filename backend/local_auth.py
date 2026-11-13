"""A visible, disposable site browser. Export starts only on an explicit Save."""

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright
from websockets.asyncio.server import serve

from backend.browser import _cookies_for_url
from backend.chunlou import (
    AUTH_STORAGE_KEY,
    LEGACY_AUTH_STORAGE_KEY,
    canonical_source_url,
    restore_account_key,
    verify_account,
)
from backend.egress import EgressProxy
from backend.series import page_origin
from backend.session_refresh import finish_before_cancel as _finish_before_cancel
from backend.session_refresh import registered_session as _registered_session
from backend.tasks import Conflict


def _browser_binary():
    if sys.platform == "darwin":
        candidates = [
            Path(base) / app / "Contents" / "MacOS" / name
            for base in ("/Applications", Path.home() / "Applications")
            for app, name in (
                ("Google Chrome.app", "Google Chrome"),
                ("Microsoft Edge.app", "Microsoft Edge"),
                ("Chromium.app", "Chromium"),
            )
        ]
        return next((str(path) for path in candidates if os.access(path, os.X_OK)), None)
    if sys.platform.startswith("linux") and (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        for name in ("google-chrome", "microsoft-edge", "chromium", "chromium-browser"):
            if binary := shutil.which(name):
                return binary
    return None


def site_state(raw, url):
    origin = page_origin(url)
    if not origin:
        raise ValueError("授权站点网址无效")
    _, hostname, _ = origin
    root = hostname.removeprefix("www.")
    cookie_domains = {hostname, "." + hostname, "." + root}
    return {
        "cookies": [c for c in raw["cookies"] if c["domain"].lower() in cookie_domains],
        "origins": [o for o in raw["origins"] if page_origin(o["origin"]) == origin],
    }


def _valid_chunlou_login(state):
    return any(
        _registered_session(item["value"])
        for origin in state["origins"]
        for item in origin.get("localStorage", [])
        if item["name"] == AUTH_STORAGE_KEY
    )


class _NativeProxy(EgressProxy):
    """Chrome cannot inject Basic proxy auth via its command line. Authenticate its OS process group."""

    def __init__(self, policy):
        super().__init__(policy)
        self.group = None
        self.port = None
        self._checks = asyncio.Semaphore(8)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        try:
            peer = writer.get_extra_info("peername")
            if self._closing or not self.group or not peer or peer[0] != "127.0.0.1":
                return
            async with self._checks:
                if not await self._owned(peer[1]):
                    return
            await super()._handle(reader, writer)
        except (TimeoutError, OSError):
            pass
        finally:
            self._tasks.discard(task)
            self._writers.discard(writer)
            writer.close()

    async def _owned(self, source_port):
        child = await asyncio.create_subprocess_exec(
            shutil.which("lsof"),
            "-nP",
            "-a",
            "-g",
            str(self.group),
            f"-iTCP@127.0.0.1:{self.port}",
            "-Fn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(child.communicate(), 4)
        except (TimeoutError, asyncio.CancelledError):
            child.kill()
            await child.wait()
            raise
        name = f"n127.0.0.1:{source_port}->127.0.0.1:{self.port}".encode()
        return name in output.splitlines()

    async def _read_request(self, reader):
        method, target, headers = await super()._read_request(reader)
        headers["proxy-authorization"] = self._authorization
        return method, target, headers


def _guard_chrome(profile, read_fd, write_fd, args):
    """An independent guardian sees stdin EOF even if the panel process crashes."""
    import fcntl
    import select

    read_copy = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 5)
    write_copy = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, 5)
    os.close(read_fd)
    os.close(write_fd)
    os.dup2(read_copy, 3)
    os.dup2(write_copy, 4)
    os.close(read_copy)
    os.close(write_copy)
    chrome = None
    try:
        chrome = subprocess.Popen(
            args,
            pass_fds=(3, 4),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(3)
        os.close(4)
        print(chrome.pid, flush=True)
        while chrome.poll() is None:
            if select.select([0], [], [], 0.3)[0] and not os.read(0, 1):
                break
    finally:
        if chrome is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(chrome.pid, signal.SIGTERM)
            try:
                chrome.wait(5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(chrome.pid, signal.SIGKILL)
                chrome.wait()
        shutil.rmtree(profile, ignore_errors=True)


class _Window:
    def __init__(self, site_id, origin, policy):
        self.site_id, self.origin = site_id, origin
        self.proxy = _NativeProxy(policy)
        self.guard = None
        self.reader = self.writer = None
        self.profile = None
        self.sequence = 0

    async def start(self, binary, url):
        await self.proxy.start()
        self.proxy.port = self.proxy._server.sockets[0].getsockname()[1]
        self.profile = tempfile.mkdtemp(prefix="framekeep-local-site-")
        read_fd, to_chrome = os.pipe()
        from_chrome, write_fd = os.pipe()
        try:
            self.guard = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "backend.local_auth",
                self.profile,
                str(read_fd),
                str(write_fd),
                binary,
                f"--user-data-dir={self.profile}",
                "--remote-debugging-pipe",
                f"--proxy-server=http://127.0.0.1:{self.proxy.port}",
                "--proxy-bypass-list=<-loopback>",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                url,
                pass_fds=(read_fd, write_fd),
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            line = await asyncio.wait_for(self.guard.stdout.readline(), 8)
            self.proxy.group = int(line)
            loop = asyncio.get_running_loop()
            self.reader = asyncio.StreamReader(limit=32 * 1024**2)
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(self.reader),
                os.fdopen(from_chrome, "rb", buffering=0),
            )
            from_chrome = None
            self.read_transport = transport
            transport, protocol = await loop.connect_write_pipe(
                asyncio.streams.FlowControlMixin, os.fdopen(to_chrome, "wb", buffering=0)
            )
            to_chrome = None
            self.writer = asyncio.StreamWriter(transport, protocol, None, loop)
        finally:
            for fd in (read_fd, write_fd, to_chrome, from_chrome):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)

    async def close(self):
        if self.guard:
            self.guard.stdin.close()
            await self.guard.wait()
        if self.writer:
            self.writer.close()
        if self.reader and hasattr(self, "read_transport"):
            self.read_transport.close()
        await self.proxy.close()
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)

    async def _relay(self, ws, path):
        if ws.request.path != path:
            await ws.close(code=1008)
            return
        pending = {}
        attached = set()

        async def to_browser():
            async for command in ws:
                message = json.loads(command)
                self.sequence += 1
                pending[self.sequence] = message["id"]
                message["id"] = self.sequence
                self.writer.write(json.dumps(message).encode() + b"\0")
                await self.writer.drain()

        async def from_browser():
            while True:
                message = json.loads((await self.reader.readuntil(b"\0"))[:-1])
                if message.get("method") == "Target.attachedToTarget":
                    attached.add(message["params"]["sessionId"])
                elif message.get("method") == "Target.detachedFromTarget":
                    attached.discard(message["params"]["sessionId"])
                if "id" in message:
                    identifier = pending.pop(message["id"], None)
                    if identifier is None:
                        continue
                    message["id"] = identifier
                await ws.send(json.dumps(message))

        tasks = [asyncio.create_task(to_browser()), asyncio.create_task(from_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Unlike a browser-owned WebSocket, this private pipe stays open for the visible window.
            # Detach this client so a retry can attach to the same pages from a fresh context.
            cleanup = [
                (
                    "Target.setAutoAttach",
                    {"autoAttach": False, "waitForDebuggerOnStart": False, "flatten": True},
                )
            ]
            cleanup.extend(("Target.detachFromTarget", {"sessionId": sid}) for sid in attached)
            cleanup.extend(
                [("Target.setDiscoverTargets", {"discover": False}), ("Target.getTargets", {})]
            )
            for method, params in cleanup:
                self.sequence += 1
                self.writer.write(
                    json.dumps({"id": self.sequence, "method": method, "params": params}).encode()
                    + b"\0"
                )
            await self.writer.drain()
            try:
                async with asyncio.timeout(2):
                    while (
                        json.loads((await self.reader.readuntil(b"\0"))[:-1]).get("id")
                        != self.sequence
                    ):
                        pass
            except (TimeoutError, EOFError, ConnectionError):
                pass

    async def snapshot(self):
        path = "/" + secrets.token_urlsafe(32)
        async with (
            asyncio.timeout(20),
            serve(
                lambda ws: self._relay(ws, path),
                "127.0.0.1",
                0,
                origins=[None],
                compression=None,
                max_size=32 * 1024**2,
                close_timeout=1,
            ) as bridge,
            async_playwright() as playwright,
        ):
            port = bridge.sockets[0].getsockname()[1]
            browser = await playwright.chromium.connect_over_cdp(f"ws://127.0.0.1:{port}{path}")
            (ctx,) = browser.contexts
            if not any(page_origin(page.url) == self.origin for page in ctx.pages):
                raise ValueError("请先回到对应站点页面，再保存会话")
            # An attached existing tab predates Playwright's origin bookkeeping. Register
            # just the exact site origin with a local document; no site navigation is sent.
            source = next(page for page in ctx.pages if page_origin(page.url) == self.origin)
            origin = await source.evaluate("location.origin")
            if page_origin(origin) != self.origin:
                raise ValueError("站点页面已切换，请返回来源站点后重试")
            probe = await ctx.new_page()
            try:
                cdp = await ctx.new_cdp_session(probe)
                await cdp.send("Network.setBypassServiceWorker", {"bypass": True})
                await probe.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html", body="<title>保存授权</title>"
                    ),
                )
                await probe.goto(
                    origin + "/.framekeep-session-export", wait_until="domcontentloaded"
                )
                return await ctx.storage_state(indexed_db=True)
            finally:
                await probe.close()
                await source.bring_to_front()


def _chunlou_origin(origin):
    return bool(
        origin
        and origin[0] == "https"
        and origin[1] in {"chunlou.tv", "www.chunlou.tv"}
        and origin[2] == 443
    )


def _lata_origin(origin):
    return bool(
        origin
        and origin[0] == "https"
        and origin[1] in {"lata.casa", "www.lata.casa"}
        and origin[2] == 443
    )


def _system_origin(origin):
    return _chunlou_origin(origin) or _lata_origin(origin)


def _auth_source_url(url):
    url = canonical_source_url(url)
    if _lata_origin(page_origin(url)):
        return urlsplit(url)._replace(netloc="lata.casa").geturl()
    return url


async def _verify_lata_login(policy, state):
    """Check the website's own login flag after the user completes Google sign-in."""
    url = await policy.validate("https://lata.casa/community/feed")
    cookies = _cookies_for_url(state.get("cookies", []), url)
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
    try:
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=12,
                proxy=getattr(policy, "proxy_url", None),
            ) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
                raise ValueError("Lata 尚未确认账号登录，请完成 Google 登录并返回 Lata 后再保存")
            if response.status_code != 200:
                raise ValueError("Lata 账号状态暂时无法核对，请稍后重试保存")
            raw = bytearray()
            async for block in response.aiter_bytes(65536):
                if len(raw) + len(block) > 2 * 1024**2:
                    raise ValueError("Lata 账号回复过大，请稍后重试保存")
                raw.extend(block)
            try:
                result = json.loads(raw)
            except (ValueError, RecursionError):
                raise ValueError("Lata 未返回有效的账号状态，请稍后重试保存") from None
            if not isinstance(result, dict) or result.get("signedIn") is not True:
                raise ValueError("Lata 尚未确认账号登录，请完成 Google 登录并返回 Lata 后再保存")
    except (httpx.HTTPError, TimeoutError, OSError):
        raise ValueError("Lata 账号状态暂时无法核对，请检查网络后重试保存") from None


def _stop_system_chrome(chrome):
    """Discard the dedicated browser on Cancel or parent process death."""
    if chrome.poll() is None:
        chrome.terminate()
        try:
            chrome.wait(12)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(chrome.pid, signal.SIGKILL)
            chrome.wait()


def _park_system_chrome(chrome):
    if chrome.poll() is None:
        if sys.platform != "darwin":
            # A normal menu exit flushes the cookie database. SIGTERM can silently lose fresh cookies.
            return "close-manually"
        try:
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-l",
                    "JavaScript",
                    "-e",
                    "ObjC.import('AppKit'); $.NSRunningApplication.runningApplicationWithProcessIdentifier("
                    + str(chrome.pid)
                    + ").terminate",
                ],
                check=True,
                timeout=4,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            chrome.wait(10)
        except (OSError, subprocess.SubprocessError):
            return "close-manually"
    return "parked" if chrome.returncode == 0 else "interrupted"


def _guard_system_chrome(profile, binary, url):
    """Only this process owns the disposable GUI browser. Parent death closes its stdin."""
    chrome = None

    def launch(*, resume=False):
        args = [
            binary,
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if resume:
            args.append("--restore-last-session")
        args.extend(["--new-window", url])
        return subprocess.Popen(
            args,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    try:
        chrome = launch()
        print(chrome.pid, flush=True)
        for command in sys.stdin:
            if command == "park\n":
                status = "parked" if chrome is None else _park_system_chrome(chrome)
                if status != "close-manually":
                    chrome = None
                print(status, flush=True)
            elif command == "resume\n":
                if chrome is None or chrome.poll() is not None:
                    chrome = launch(resume=True)
                print(chrome.pid, flush=True)
            else:
                break
    finally:
        if chrome is not None:
            _stop_system_chrome(chrome)
        shutil.rmtree(Path(profile).parent, ignore_errors=True)


def _copy_storage(profile, output):
    """Copy only a closed, app-created Chrome profile's storage to an offline reader."""
    default = output / "Default"
    default.mkdir(parents=True, mode=0o700)
    for name in (
        "Local State",
        "Default/Local Storage",
        "Default/IndexedDB",
        "Default/Network/Cookies",
        "Default/Cookies",
    ):
        source = profile / name
        if source.is_symlink() or not source.exists():
            continue
        if not source.resolve().is_relative_to(profile.resolve()):
            raise ValueError("浏览器资料路径无效")
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=lambda directory, names: [
                    n for n in names if (Path(directory) / n).is_symlink()
                ],
            )
        else:
            shutil.copy2(source, target)
            if source.name == "Cookies":
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = source.with_name(source.name + suffix)
                    if sidecar.is_file() and not sidecar.is_symlink():
                        shutil.copy2(sidecar, target.with_name(target.name + suffix))
    # Keep session cookies across this offline read, with no open tabs to restore.
    (default / "Preferences").write_text(json.dumps({"session": {"restore_on_startup": 1}}))


class _SystemWindow:
    """An ordinary user-operated browser; only the offline copy is ever automated."""

    def __init__(self, site_id, origin):
        self.site_id, self.origin = site_id, origin
        self.guard = self.profile = self.root = self.pid = None
        self.binary = None
        self.parked = False

    async def start(self, binary, url):
        self.binary = binary
        self.root = Path(tempfile.mkdtemp(prefix="framekeep-system-site-"))
        self.profile = self.root / "user"
        default = self.profile / "Default"
        default.mkdir(parents=True, mode=0o700)
        (default / "Preferences").write_text(
            json.dumps(
                {
                    "session": {"restore_on_startup": 1},
                    "credentials_enable_service": False,
                    "profile": {"password_manager_enabled": False},
                }
            )
        )
        self.guard = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "backend.local_auth",
            "--system-window",
            str(self.profile),
            binary,
            url,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.pid = int(await asyncio.wait_for(self.guard.stdout.readline(), 8))

    async def _command(self, command):
        self.guard.stdin.write(command.encode() + b"\n")
        await self.guard.stdin.drain()
        line = await asyncio.wait_for(self.guard.stdout.readline(), 20)
        if not line:
            raise ValueError("专用浏览器已退出，请重新打开授权窗口")
        return line.strip()

    async def resume(self):
        if self.parked:
            self.pid = int(await self._command("resume"))
            self.parked = False

    async def snapshot(self):
        return await _finish_before_cancel(self._snapshot())

    async def _snapshot(self):
        self.parked = True
        status = await self._command("park")
        if status == b"close-manually":
            self.parked = False
            raise ValueError("请在这次专用浏览器的菜单中点「退出」，待窗口关闭后再点击保存会话")
        if status != b"parked":
            raise ValueError("浏览器未能正常关闭，已保留授权资料，请再次保存")
        # Chrome removes this symlink after its storage has been flushed (lexists also sees stale locks).
        async with asyncio.timeout(4):
            while os.path.lexists(self.profile / "SingletonLock"):
                await asyncio.sleep(0.05)
        with tempfile.TemporaryDirectory(dir=self.root, prefix="export-") as copy:
            await asyncio.to_thread(_copy_storage, self.profile, Path(copy))
            async with asyncio.timeout(30):
                return await self._read_offline(copy)

    async def _read_offline(self, copy):
        scheme, host, port = self.origin
        authority = host if port == (443 if scheme == "https" else 80) else f"{host}:{port}"
        url = f"{scheme}://{authority}/.framekeep-session-export"

        # Block restored tabs, browser background requests and service workers, even before routes attach.
        def deny(_reader, writer):
            writer.close()

        async with (
            await asyncio.start_server(deny, "127.0.0.1", 0) as sink,
            async_playwright() as pw,
        ):
            proxy_port = sink.sockets[0].getsockname()[1]
            ctx = await pw.chromium.launch_persistent_context(
                copy,
                executable_path=self.binary,
                headless=True,
                offline=True,
                service_workers="block",
                ignore_default_args=["--use-mock-keychain", "--password-store=basic"],
                proxy={"server": f"http://127.0.0.1:{proxy_port}"},
                args=[
                    "--restore-last-session",
                    "--proxy-bypass-list=<-loopback>",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            try:
                page = await ctx.new_page()
                await page.route(
                    url,
                    lambda route: route.fulfill(
                        content_type="text/html", body="<title>授权导出</title>"
                    ),
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=8000)
                return await ctx.storage_state(indexed_db=True)
            finally:
                await ctx.close()

    async def close(self):
        if self.guard:
            self.guard.stdin.close()
            await self.guard.wait()
        if self.root:
            shutil.rmtree(self.root, ignore_errors=True)


class LocalAuthService:
    """One user-operated authorization window per workspace."""

    lifetime = 60 * 60

    def __init__(self, policy, *, executable=None):
        self.policy = policy
        self.executable = str(executable) if executable is not None else _browser_binary()
        self._window = self._watcher = None
        self._lock = asyncio.Lock()

    def info(self, site_id, site_url=None):
        window = self._window
        origin = (
            page_origin(site_url)
            if site_url
            else (getattr(window, "origin", None) if window and window.site_id == site_id else None)
        )
        can_import = _chunlou_origin(origin)
        system = _system_origin(origin) if origin or site_url else site_id == "chunlou"
        available = bool(
            self.executable
            and os.access(self.executable, os.X_OK)
            and (system or shutil.which("lsof"))
            and callable(getattr(self.policy, "resolve", None))
            and (sys.platform == "darwin" or sys.platform.startswith("linux"))
        )
        active = bool(
            window
            and window.site_id == site_id
            and window.guard
            and window.guard.returncode is None
        )
        message = (
            "请在弹出的本机浏览器中完成站点操作，再回到面板保存会话"
            if active
            else "可以在本机浏览器中授权"
            if available
            else "需要本机图形桌面、Chrome 或 Edge 和 lsof"
        )
        if system:
            message = (
                "请在系统浏览器中完成人工验证和登录。点保存会话后，拾帧会关闭专用窗口并读取本站会话。"
                if available
                else "需要本机图形桌面和 Chrome 或 Edge"
            )
            if available and sys.platform.startswith("linux"):
                message = "请在系统浏览器中完成人工验证和登录，使用专用窗口菜单退出浏览器后，回到面板保存会话。"
            if available and _lata_origin(origin):
                message = (
                    "在本机浏览器点击 Continue with Google，完成登录并返回 Lata。保存时会核对本站登录状态。"
                    if sys.platform == "darwin"
                    else "完成 Google 登录并返回 Lata 后，用专用窗口菜单退出浏览器，再回面板保存会话。"
                )
        return {
            "available": available,
            "active": active,
            "open": active,
            "canImportAccount": can_import,
            "message": message,
            "mode": "system-browser" if system else "guarded-browser",
        }

    async def open(self, site, url=None):
        async with self._lock:
            info = self.info(site["id"], site["url"])
            if not info["available"]:
                raise ValueError(info["message"])
            target = _auth_source_url(url or site["url"])
            if page_origin(target) != page_origin(_auth_source_url(site["url"])):
                raise ValueError("授权页必须属于所选站点并使用相同协议和端口")
            target = await self.policy.validate(target)
            if self._window:
                if self.info(site["id"])["active"]:
                    return self.info(site["id"])
                if self._window.guard and self._window.guard.returncode is None:
                    raise ValueError("另一个站点的本机授权窗口已打开，请先保存或关闭")
                await self._close_window()
            origin = page_origin(target)
            window = self._window = (
                _SystemWindow(site["id"], origin)
                if _system_origin(origin)
                else _Window(site["id"], origin, self.policy)
            )
            try:
                await window.start(self.executable, target)
            except asyncio.CancelledError:
                await self._close_window()
                raise
            except Exception:
                await self._close_window()
                raise ValueError("本机浏览器启动失败，请检查桌面环境和浏览器安装") from None
            self._watcher = asyncio.create_task(self._watch(window))
            return self.info(site["id"])

    async def _watch(self, window):
        try:
            await asyncio.wait_for(window.guard.wait(), self.lifetime)
        except TimeoutError:
            pass
        async with self._lock:
            if self._window is window:
                await self._close_window()

    async def save(self, site, importer):
        async with self._lock:
            if not self.info(site["id"])["active"]:
                raise ValueError("本机授权窗口未打开，请重新打开")
            window = self._window
            try:
                try:
                    state = site_state(await window.snapshot(), _auth_source_url(site["url"]))
                except ValueError:
                    raise
                except Exception:
                    raise ValueError("读取本机站点会话失败，请等页面加载完成后重试") from None
                if not any(state.values()):
                    raise ValueError("尚未取得该站点的会话，请完成授权后再保存")
                if _chunlou_origin(window.origin):
                    restore_account_key(state, site["url"])
                    if not _valid_chunlou_login(state):
                        raise ValueError("春楼尚未取得已登录用户的有效会话，请完成账号登录后再保存")
                if _lata_origin(window.origin):
                    await _verify_lata_login(self.policy, state)
                await importer(site, state)
            except (Exception, asyncio.CancelledError):
                if isinstance(window, _SystemWindow):
                    await _finish_before_cancel(window.resume())
                raise
            await self._close_window()
            return {"saved": True, "open": False, "verified": False}

    async def import_account(self, site, payload, importer):
        if not _chunlou_origin(page_origin(site["url"])):
            raise ValueError("目前只支持导入春楼的账号授权")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"storageKey", "session"}
            or payload["storageKey"] not in (AUTH_STORAGE_KEY, LEGACY_AUTH_STORAGE_KEY)
            or not isinstance(payload["session"], str)
        ):
            raise ValueError("未收到春楼账号会话，请在已登录的春楼页面重新授权")
        async with self._lock:
            if self.info(site["id"], site["url"])["active"]:
                raise Conflict("此站点的专用授权窗口仍在使用，请先保存或取消该窗口，再确认导入")
            session = _registered_session(payload["session"])
            if not session:
                raise ValueError("春楼账号会话已失效或来源不符，请刷新已登录的春楼页面后重新授权")
            await verify_account(self.policy, session["access_token"], session["user"]["id"])
            item = {
                "name": AUTH_STORAGE_KEY,
                "value": json.dumps(session, separators=(",", ":")),
            }
            state = {
                "cookies": [],
                "origins": [{"origin": "https://chunlou.tv", "localStorage": [item]}],
            }

            result = await _finish_before_cancel(importer(site, state))
            return {**result, "accountVerified": True}

    async def _close_window(self):
        if self._watcher and self._watcher is not asyncio.current_task():
            self._watcher.cancel()
        self._watcher = None
        if self._window:
            window, self._window = self._window, None
            await _finish_before_cancel(window.close())

    async def cancel(self, site_id):
        async with self._lock:
            if self._window and self._window.site_id == site_id:
                await self._close_window()
            return self.info(site_id)

    async def close(self):
        async with self._lock:
            await self._close_window()


if __name__ == "__main__":
    if sys.argv[1] == "--system-window":
        _guard_system_chrome(*sys.argv[2:])
    else:
        _guard_chrome(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:])
