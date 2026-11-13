"""Anonymous, cookie-bound workspaces for the optional public deployment mode."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from backend.operations import (
    StorageGuard,
    StoragePressure,
    WorkspaceBusy,
    WorkspaceLock,
    dependency_status,
)

WORKSPACE_ID = re.compile(r"[a-f0-9]{32}")
BUSY_STATUSES = {"queued", "running"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_API_BODY_BYTES = 8 * 1024**2


@dataclass(frozen=True)
class PublicOptions:
    retention_seconds: int = 86400
    idle_unload_seconds: int = 60
    max_active_workspaces: int = 3
    max_workspaces: int = 64
    max_workspace_bytes: int = 2 * 1024**3
    max_total_bytes: int = 6 * 1024**3
    secure_cookie: bool = True
    cleanup_interval_seconds: int = 60
    new_workspaces_per_ip: int = 5
    allocation_window_seconds: int = 600

    def __post_init__(self):
        for name in (
            "retention_seconds",
            "idle_unload_seconds",
            "max_active_workspaces",
            "max_workspaces",
            "max_workspace_bytes",
            "max_total_bytes",
            "cleanup_interval_seconds",
            "new_workspaces_per_ip",
            "allocation_window_seconds",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if self.max_workspaces < self.max_active_workspaces:
            raise ValueError("总工作区上限不能小于活跃工作区上限")
        if type(self.secure_cookie) is not bool:
            raise ValueError("secure_cookie 必须为布尔值")

    @classmethod
    def from_env(cls):
        prefix = "FRAMEKEEP_PUBLIC_"

        def integer(key, default):
            return int(os.getenv(prefix + key, str(default)))

        secure = os.getenv(prefix + "SECURE_COOKIE", "1")
        if secure not in {"0", "1"}:
            raise ValueError("FRAMEKEEP_PUBLIC_SECURE_COOKIE 必须为 0 或 1")
        return cls(
            retention_seconds=integer("RETENTION_HOURS", 24) * 3600,
            idle_unload_seconds=integer("IDLE_SECONDS", 60),
            max_active_workspaces=integer("MAX_ACTIVE_WORKSPACES", 3),
            max_workspaces=integer("MAX_WORKSPACES", 64),
            max_workspace_bytes=integer("WORKSPACE_BYTES", 2 * 1024**3),
            max_total_bytes=integer("TOTAL_BYTES", 6 * 1024**3),
            secure_cookie=secure == "1",
        )


def directory_bytes(path):
    """Count owned files without following symlinks outside the workspace."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            item = Path(root) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except FileNotFoundError:
                pass  # A worker atomically published or removed a partial file.
    return total


class PublicStorageGuard:
    def __init__(self, original, workspace, manager):
        self.original, self.workspace, self.manager = original, Path(workspace), manager

    def snapshot(self):
        disk = self.original.snapshot()
        available = min(
            disk["availableBytes"],
            self.manager.options.max_workspace_bytes - directory_bytes(self.workspace),
            self.manager.options.max_total_bytes - directory_bytes(self.manager.root),
        )
        return {
            **disk,
            "availableBytes": max(0, available),
            "healthy": disk["healthy"] and available > 0,
        }

    def check(self, required_bytes=0):
        self.original.check(required_bytes)
        if (
            directory_bytes(self.workspace) + required_bytes
            >= self.manager.options.max_workspace_bytes
            or directory_bytes(self.manager.root) + required_bytes
            >= self.manager.options.max_total_bytes
        ):
            raise StoragePressure("临时工作区存储额度不足，请导出已有内容后稍后重试")


@dataclass
class Workspace:
    identifier: str
    path: Path
    app: object
    stack: contextlib.AsyncExitStack
    last_seen: float
    requests: int = 0


class PublicLimit(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


class WorkspaceManager:
    def __init__(self, settings, options, factory, resource_limits, clock):
        self.settings, self.options, self.factory = settings, options, factory
        self.resource_limits, self.clock = resource_limits, clock
        self.root = settings.data_dir / "workspaces"
        self.cookie_name = "__Host-framekeep" if options.secure_cookie else "framekeep_workspace"
        self.active = {}
        self.lock = asyncio.Lock()
        self.secret = None
        self.closing = False
        self.dependencies = {}
        self.sweeper = None
        self.storage = StorageGuard(settings.data_dir, settings.min_free_bytes)
        self.workspace_lock = WorkspaceLock(settings.data_dir)
        self.allocations = {}

    async def start(self):
        self.settings.prepare()
        self.workspace_lock.acquire()
        try:
            await self._start_owned()
        except BaseException:
            self.workspace_lock.release()
            raise

    async def _start_owned(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("公共工作区目录不能是符号链接")
        path = self.settings.data_dir / ".public-secret"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink():
                raise ValueError("公共模式密钥文件不能是符号链接")
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(secrets.token_bytes(32))
        path.chmod(0o600)
        self.secret = path.read_bytes()
        if len(self.secret) != 32:
            raise ValueError("公共模式密钥文件无效")
        self.dependencies = await dependency_status()
        await self.cleanup()
        self.sweeper = asyncio.create_task(self._sweep_loop())

    async def close(self):
        self.closing = True
        if self.sweeper:
            self.sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sweeper
        try:
            async with self.lock:
                for identifier in list(self.active):
                    await self._unload(identifier)
        finally:
            self.workspace_lock.release()

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(self.options.cleanup_interval_seconds)
            await self.cleanup()

    def token(self, identifier):
        signature = hmac.new(self.secret, identifier.encode(), hashlib.sha256).hexdigest()
        return identifier + "." + signature

    def identify(self, value):
        if not isinstance(value, str) or len(value) != 97:
            return None
        identifier, separator, signature = value.partition(".")
        if (
            not separator
            or not WORKSPACE_ID.fullmatch(identifier)
            or not re.fullmatch(r"[a-f0-9]{64}", signature)
        ):
            return None
        return identifier if hmac.compare_digest(self.token(identifier), value) else None

    def paths(self):
        return [
            path
            for path in self.root.iterdir()
            if WORKSPACE_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
        ]

    def _last_seen(self, path):
        try:
            value = json.loads((path / ".public.json").read_text())["lastSeen"]
            return float(value) if math.isfinite(float(value)) else self.clock()
        except (OSError, ValueError, KeyError, TypeError):
            return path.stat().st_mtime

    def _touch(self, tenant):
        tenant.last_seen = self.clock()
        temporary = tenant.path / ".public.json.tmp"
        temporary.write_text(json.dumps({"lastSeen": tenant.last_seen}))
        os.replace(temporary, tenant.path / ".public.json")

    @staticmethod
    def _state_busy(state):
        return any(
            item.get("status") in BUSY_STATUSES
            for key in ("jobs", "scans", "processingJobs")
            for item in state.get(key, [])
        )

    def _busy(self, tenant):
        if tenant.requests:
            return True
        state = tenant.app.state
        tasks = state.tasks
        return bool(
            tasks.active
            or tasks.scan_tasks
            or state.processing.active
            or self._state_busy(state.store.read())
        )

    async def _unload(self, identifier):
        tenant = self.active.pop(identifier)
        await tenant.stack.aclose()

    async def cleanup(self):
        async with self.lock:
            await self._cleanup_locked()

    async def _cleanup_locked(self):
        now = self.clock()
        for path in self.paths():
            tenant = self.active.get(path.name)
            if tenant and self._busy(tenant):
                continue
            last_seen = tenant.last_seen if tenant else self._last_seen(path)
            expired = now - last_seen >= self.options.retention_seconds
            if tenant and (expired or now - last_seen >= self.options.idle_unload_seconds):
                await self._unload(path.name)
            if expired:
                # An unloaded, expired queue is not running work. A separate process
                # may own this exact directory, so obtain its lease before removal.
                lease = WorkspaceLock(path)
                try:
                    lease.acquire()
                except WorkspaceBusy:
                    continue
                try:
                    await asyncio.to_thread(shutil.rmtree, path)
                finally:
                    lease.release()

    def _install_guard(self, app, path):
        state = app.state
        guard = PublicStorageGuard(state.storage, path, self)
        state.storage = guard
        state.tasks.storage_guard = guard
        state.tasks.downloader.storage_guard = guard
        state.processing.storage_guard = guard
        state.processing.processor.storage_guard = guard
        state.operations.storage = guard
        state.operations.monitor.storage = guard

    async def acquire(self, cookie, *, initialize, client_ip="unknown"):
        async with self.lock:
            if self.closing:
                raise PublicLimit(503, "服务正在重启，请稍后重试")
            await self._cleanup_locked()
            identifier = self.identify(cookie)
            path = self.root / identifier if identifier else None
            new_workspace = False
            if path is None or not path.is_dir() or path.is_symlink():
                if not initialize:
                    raise PublicLimit(401, "临时工作区已过期，请刷新面板重新开始")
                if len(self.paths()) >= self.options.max_workspaces:
                    raise PublicLimit(429, "临时工作区已满，请稍后重试")
                cutoff = self.clock() - self.options.allocation_window_seconds
                self.allocations = {
                    key: deque(t for t in stamps if t > cutoff)
                    for key, stamps in self.allocations.items()
                    if stamps[-1] > cutoff
                }
                if (
                    len(self.allocations.get(client_ip, ())) >= self.options.new_workspaces_per_ip
                    or len(self.allocations) >= 4096
                    and client_ip not in self.allocations
                ):
                    raise PublicLimit(429, "新建工作区过于频繁，请保留当前浏览器会话并稍后重试")
                self.storage.check()
                if directory_bytes(self.root) >= self.options.max_total_bytes:
                    raise PublicLimit(503, "公共存储暂时已满，请稍后重试")
                identifier = secrets.token_hex(16)
                path = self.root / identifier
                new_workspace = True
            if identifier not in self.active:
                if len(self.active) >= self.options.max_active_workspaces:
                    idle = sorted(
                        (t for t in self.active.values() if not self._busy(t)),
                        key=lambda t: t.last_seen,
                    )
                    if not idle:
                        raise PublicLimit(429, "服务正在处理其他任务，请稍后重试")
                    await self._unload(idle[0].identifier)
                tenant_settings = replace(
                    self.settings,
                    data_dir=path,
                    admin_token="",
                    max_download_bytes=min(
                        self.settings.max_download_bytes, self.options.max_workspace_bytes, 1024**3
                    ),
                )
                child = self.factory(tenant_settings, resource_limits=self.resource_limits)
                child.state.browser.max_open_sessions = 2
                child.state.store.mutate(lambda state: state["settings"].update(concurrency=1))
                self._install_guard(child, path)
                stack = contextlib.AsyncExitStack()
                try:
                    await stack.enter_async_context(child.router.lifespan_context(child))
                    await child.state.automation.close()
                except BaseException:
                    await stack.aclose()
                    raise
                self.active[identifier] = Workspace(identifier, path, child, stack, self.clock())
            tenant = self.active[identifier]
            if new_workspace:
                self.allocations.setdefault(client_ip, deque()).append(self.clock())
            tenant.requests += 1
            self._touch(tenant)
            return tenant

    async def release(self, tenant):
        async with self.lock:
            tenant.requests -= 1
            self._touch(tenant)

    def public_info(self):
        return {
            "publicMode": True,
            "retentionHours": self.options.retention_seconds / 3600,
            "processing": False,
            "automation": False,
        }

    def readiness(self):
        disk = self.storage.snapshot()
        ready = (
            not self.closing
            and disk["healthy"]
            and bool(self.dependencies)
            and all(item["available"] for item in self.dependencies.values())
        )
        ready = (
            ready
            and self.workspace_lock.held
            and self.sweeper is not None
            and not self.sweeper.done()
        )
        return {
            "ready": ready,
            "status": "ready" if ready else "degraded",
            "publicMode": True,
            "dependencies": self.dependencies,
        }


class PublicDispatcher:
    def __init__(self, manager):
        self.manager = manager
        self.static = StaticFiles(directory=manager.settings.static_dir, html=True)

    @staticmethod
    def _blocked(path):
        return (
            any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in (
                    "/api/maintenance",
                    "/api/rules",
                    "/api/processes",
                    "/api/operations",
                    "/api/settings",
                )
            )
            or "/local-auth" in path
        )

    @staticmethod
    def _same_origin(scope):
        headers = Headers(scope=scope)
        expected = f"{scope['scheme']}://{headers.get('host', '')}"
        origin = headers.get("origin")
        fetch = headers.get("sec-fetch-site")
        if origin is not None and origin != expected:
            return False
        if fetch in {"cross-site", "same-site"}:
            return False
        return scope["method"] in SAFE_METHODS or origin == expected or fetch == "same-origin"

    @staticmethod
    def _allocates(method, path):
        if method != "POST":
            return False
        if path in {"/api/sites", "/api/scans", "/api/series", "/api/intake", "/api/downloads"}:
            return True
        return bool(
            re.fullmatch(r"/api/(?:series|scans)/[^/]+/(?:scan|episodes|downloads)", path)
            or re.fullmatch(r"/api/intake/[^/]+/continue", path)
        )

    @staticmethod
    def _browser_request(method, path):
        return (
            method == "GET"
            and path.endswith(("/thumbnail", "/poster", "/download-options", "/session/screenshot"))
        ) or (
            method == "POST"
            and bool(re.fullmatch(r"/api/sites/[^/]+/session(?:/input|/save)?", path))
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path, method = scope["path"], scope["method"]
        if not path.startswith("/api/"):
            async def send_static(message):
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    media_type = headers.get("content-type", "").split(";", 1)[0]
                    # Conditional responses omit Content-Type, so recognize panel
                    # paths too, including directory indexes served at "/".
                    if (path.lower().endswith(("/", ".html", ".htm", ".js", ".mjs", ".css"))
                            or media_type in {"text/html", "text/css", "text/javascript", "application/javascript"}):
                        headers["Cache-Control"] = "no-store"
                await send(message)

            return await self.static(scope, receive, send_static)
        manager = self.manager

        async def respond(data, status=200):
            response = JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)

        if method in {"GET", "HEAD"}:
            if path == "/api/public-info":
                return await respond(manager.public_info())
            if path == "/api/health":
                return await respond({"status": "ok", "mode": "real", "publicMode": True})
            if path == "/api/ready":
                result = manager.readiness()
                return await respond(result, 200 if result["ready"] else 503)
            if path == "/api/capabilities":
                return await respond(
                    {
                        **manager.public_info(),
                        "mode": "real",
                        "version": 1,
                        "maxScanPages": 100,
                        "maxDownloadBytes": min(
                            manager.settings.max_download_bytes,
                            manager.options.max_workspace_bytes,
                            1024**3,
                        ),
                        "processingMethods": {},
                        "extractor": "yt-dlp-generic",
                        "downloadProtocols": ["http", "hls-vod", "dash-static"],
                        "episodeCatalog": True,
                        "intakeScopes": ["site", "series", "single"],
                        "shortPostPlatforms": ["douyin", "tiktok", "instagram", "youtube"],
                        "chunlouFeed": True,
                        "chunlouSystemBrowserAuth": False,
                        "accessRecovery": "once-exact-page",
                    }
                )
        if self._blocked(path):
            return await respond({"detail": "公共模式不提供此管理功能"}, 403)
        if not self._same_origin(scope):
            return await respond({"detail": "请从本站面板操作"}, 403)
        request = Request(scope, receive)
        if method not in SAFE_METHODS:
            series_check = (
                method == "POST" and path.startswith("/api/series/") and path.endswith("/scan")
            )
            limit = min(MAX_API_BODY_BYTES, 65536) if series_check else MAX_API_BODY_BYTES
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    return await respond({"detail": "请求过大"}, 413)
                body.extend(chunk)
            if series_check:
                with contextlib.suppress(ValueError):
                    payload = json.loads(body)
                    if isinstance(payload, dict) and payload.get("autoUnlock") is not None:
                        return await respond({"detail": "公共模式不执行付费解锁"}, 403)
            delivered = False
            original_receive = receive

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = replay
        try:
            tenant = await manager.acquire(
                request.cookies.get(manager.cookie_name),
                initialize=path == "/api/state" and method == "GET",
                client_ip=(scope.get("client") or ("unknown", 0))[0],
            )
        except PublicLimit as error:
            return await respond({"detail": error.detail}, error.status)
        except StoragePressure as error:
            return await respond({"detail": str(error)}, 503)
        try:
            if self._allocates(method, path):
                try:
                    tenant.app.state.storage.check()
                except StoragePressure as error:
                    return await respond({"detail": str(error)}, 503)
            cookie_response = JSONResponse({})
            cookie_response.set_cookie(
                manager.cookie_name,
                manager.token(tenant.identifier),
                max_age=manager.options.retention_seconds,
                httponly=True,
                secure=manager.options.secure_cookie,
                samesite="lax",
                path="/",
            )
            cookie = next(
                value for name, value in cookie_response.raw_headers if name == b"set-cookie"
            )

            async def tenant_send(message):
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [*message.get("headers", []), (b"set-cookie", cookie)],
                    }
                await send(message)

            if path == "/api/state" and method == "GET":
                result = tenant.app.state.store.public_state()
                result.update(
                    manager.public_info(), seriesArchiveAvailable=True, coinUnlockSiteIds=[]
                )
                return await JSONResponse(result, headers={"Cache-Control": "no-store"})(
                    scope, receive, tenant_send
                )
            if self._browser_request(method, path):
                messages = []

                async def buffer(message):
                    messages.append(message)

                try:
                    async with (
                        asyncio.timeout(60),
                        manager.resource_limits.slot("scan", check=tenant.app.state.storage.check),
                    ):
                        await tenant.app(scope, receive, buffer)
                        tenant.app.state.storage.check()
                except StoragePressure as error:
                    return await respond({"detail": str(error)}, 503)
                except TimeoutError:
                    return await respond({"detail": "站点操作繁忙或超时，请稍后重试"}, 429)
                for message in messages:
                    await tenant_send(message)
            else:
                await tenant.app(scope, receive, tenant_send)
        finally:
            await asyncio.shield(manager.release(tenant))


def create_public_app(
    settings, *, options=None, app_factory=None, clock=time.time, resource_limits=None
):
    if app_factory is None:
        from backend.app import create_app

        app_factory = create_app
    if resource_limits is None:
        from backend.resource_limits import ResourceLimits

        resource_limits = ResourceLimits(downloads=1, scans=1)
    manager = WorkspaceManager(
        settings, options or PublicOptions.from_env(), app_factory, resource_limits, clock
    )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = Starlette(routes=[Mount("/", app=PublicDispatcher(manager))], lifespan=lifespan)
    app.state.manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    return app
