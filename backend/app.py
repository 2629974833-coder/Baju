"""Same-origin web panel and real media API. Run a single Uvicorn worker."""

import asyncio
import base64
import ipaddress
import json
import os
import secrets
import unicodedata
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.archive import SeriesArchiveResponse, download_filename, prepare_series_archive
from backend.automation import AutomationService
from backend.automation_api import create_automation_router
from backend.browser import BrowserService
from backend.classification import automatic_classification, manual_classification
from backend.coin_unlock import purchase_adapter, supports_coin_unlock
from backend.config import Settings
from backend.downloader import Downloader
from backend.egress import EgressProxy
from backend.extractor import ExtractorService
from backend.intake import intake_archive, intake_results, intake_scan
from backend.lata import canonical_source_url
from backend.local_auth import LocalAuthService
from backend.lustflix_directory import directory_route
from backend.network import NetworkPolicy
from backend.operations import RuntimeMonitor, StorageGuard, StoragePressure, WorkspaceLock
from backend.operations_api import OperationsService
from backend.processing import Processor
from backend.processing_api import create_processing_router
from backend.processing_tasks import ProcessingQueue
from backend.scanner import Scanner
from backend.series import (
    dismiss_series,
    observed_series_id,
    page_origin,
    series_library,
    series_titles,
)
from backend.series_posters import SeriesPosters
from backend.short_media import short_target
from backend.site_adapters import select_episode_asset, update_catalog
from backend.store import Store, new_id, record, timestamp
from backend.tasks import Conflict, TaskManager, find, host_of, safe_file
from backend.thumbnails import ThumbnailService


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiteInput(Payload):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=8192)


class ScanInput(Payload):
    siteId: str | None = None
    url: str = Field(min_length=1, max_length=8192)
    depth: int = Field(default=1, ge=0, le=5)
    limit: int = Field(default=20, ge=1, le=100)
    kinds: list[Literal["image", "video"]] = Field(
        default=["image", "video"], min_length=1, max_length=2
    )


def clean_series_title(value: str):
    if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"}
           or unicodedata.bidirectional(char) in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
           for char in value):
        raise ValueError("剧名须为不含控制字符的单行文本")
    return value.strip()


SeriesTitle = Annotated[str, Field(max_length=100), AfterValidator(clean_series_title)]


class SeriesRenameInput(Payload):
    title: SeriesTitle


class SeriesInput(Payload):
    customTitle: SeriesTitle = ""
    siteId: str | None = None
    url: str = Field(min_length=1, max_length=8192)
    limit: int = Field(default=100, ge=1, le=1000)


class IntakeInput(SeriesInput):
    scope: Literal["site", "series", "single"]
    kinds: list[Literal["image", "video"]] = Field(default=["video", "image"], min_length=1, max_length=2)
    catalogOnly: bool = Field(default=False, strict=True)
    autoDownload: bool = Field(default=False, strict=True)


class IntakeContinueInput(Payload):
    limit: int | None = Field(default=None, ge=1, le=1000, strict=True)


class IntakeArchiveInput(Payload):
    ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=10000)
    groupKey: str | None = Field(default=None, min_length=1, max_length=8192)
    episodeKey: str | None = Field(default=None, min_length=1, max_length=8192)


class DownloadOptionsInput(Payload):
    maxHeight: int | None = Field(default=None, ge=144, le=4320, strict=True)
    audioLanguage: str = Field(
        default="auto", min_length=2, max_length=35, strict=True,
        pattern=r"^(?:auto|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)$",
    )


class DownloadInput(Payload):
    ids: list[str] = Field(min_length=1, max_length=1000)
    options: DownloadOptionsInput = Field(default_factory=DownloadOptionsInput)


class EpisodeInput(Payload):
    episodeKeys: list[str] = Field(min_length=1, max_length=100)


class EpisodeDownloadInput(EpisodeInput):
    options: DownloadOptionsInput = Field(default_factory=DownloadOptionsInput)


class SeriesSelectionInput(Payload):
    episodeKeys: list[str] | None = Field(default=None, min_length=1, max_length=1000)


class CoinUnlockInput(Payload):
    maxCoins: int | None = Field(default=None, strict=True, ge=1, le=9_007_199_254_740_991)


class SeriesScanInput(SeriesSelectionInput):
    autoUnlock: CoinUnlockInput | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    recheckAccess: bool = Field(default=False, strict=True)
    scanAndCheck: bool = Field(default=False, strict=True)


class SeriesDownloadInput(SeriesSelectionInput):
    options: DownloadOptionsInput = Field(default_factory=DownloadOptionsInput)
    resolveMissing: bool = Field(default=False, strict=True)


class JobInput(Payload):
    action: Literal["pause", "resume", "retry", "cancel"]
    options: DownloadOptionsInput | None = None


class AssetInput(Payload):
    favorite: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=30)
    classification: Literal["preview", "episode", "audio", "unknown", "automatic"] | None = None


class DismissSelectionInput(Payload):
    ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=10000
    )


class DismissSeriesInput(DismissSelectionInput):
    episodeKeys: list[Annotated[str, Field(min_length=1, max_length=8192)]] | None = Field(
        default=None, min_length=1, max_length=1000
    )


class DismissScanCatalog(Payload):
    scope: Literal["catalog"]


class DismissScanEpisodes(Payload):
    scope: Literal["episodes"]
    episodeKeys: list[Annotated[str, Field(min_length=1, max_length=8192)]] = Field(
        min_length=1, max_length=1000
    )


class DismissScanGroup(Payload):
    scanIds: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=10000
    )


class SettingsInput(Payload):
    concurrency: int = Field(ge=1, le=4)


class BrowserInput(Payload):
    type: Literal["click", "key", "text", "scroll", "navigate", "tab"]
    x: float = Field(default=0, ge=0, lt=1280)
    y: float = Field(default=0, ge=0, lt=800)
    key: str = Field(default="", max_length=100)
    text: str = Field(default="", max_length=10000)
    deltaY: float = Field(default=0, ge=-10000, le=10000)
    url: str = Field(default="", max_length=8192)
    tabId: str = Field(default="", max_length=100)


def create_app(settings=None, *, browser=None, scanner=None, downloader=None, policy=None,
               processor=None, resource_limits=None):
    settings = settings or Settings()
    settings.prepare()
    workspace_lock = WorkspaceLock(settings.data_dir)
    with workspace_lock:
        store = Store(settings.data_dir / "workspace.sqlite3")
    storage = StorageGuard(settings.data_dir, settings.min_free_bytes)
    monitor = RuntimeMonitor(settings.data_dir, storage)
    policy = policy or NetworkPolicy(settings.allow_private_hosts, settings.trust_fake_dns)
    egress = EgressProxy(policy) if isinstance(policy, NetworkPolicy) else None
    browser = browser or BrowserService(settings.data_dir, policy)
    scanner = scanner or Scanner(browser, policy, ExtractorService(browser, policy))
    downloader = downloader or Downloader(
        settings.data_dir, browser, policy, settings.max_download_bytes, storage_guard=storage
    )
    tasks = TaskManager(store, browser, scanner, downloader, storage_guard=storage, monitor=monitor,
                        purchase_factory=lambda site: purchase_adapter(site, browser, policy),
                        resource_limits=resource_limits)
    processing = ProcessingQueue(store, processor or Processor(settings.data_dir, storage_guard=storage),
                                 storage_guard=storage, monitor=monitor)
    automation = AutomationService(store, tasks)
    operations = OperationsService(settings, store, workspace_lock, tasks, processing, browser,
                                   monitor, storage, automation=automation)
    local_auth = LocalAuthService(policy)
    operations.local_auth = local_auth
    thumbnails = ThumbnailService(settings.data_dir, browser, policy)
    posters = SeriesPosters(thumbnails)
    inspection_slots = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(app):
        workspace_lock.acquire()
        try:
            if egress:
                await egress.start()
                policy.proxy_url = egress.url
            await monitor.start()
            await tasks.start()
            await processing.start()
            await automation.start()
            monitor.beat("automation")
            monitor.beat("download")
            monitor.beat("processing")
            yield
        finally:
            try:
                await operations.wait_for_backup()
                await automation.close()
                await processing.close()
                await tasks.close()
                await local_auth.close()
                await browser.close()
                await monitor.stop()
                if egress:
                    await egress.close()
                    policy.proxy_url = None
            finally:
                workspace_lock.release()

    app = FastAPI(
        title="Framekeep", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.store, app.state.tasks, app.state.browser = store, tasks, browser
    app.state.local_auth = local_auth
    app.state.automation = automation
    app.state.processing, app.state.operations, app.state.storage = processing, operations, storage

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        if settings.admin_token:
            authorized = False
            try:
                scheme, value = request.headers.get("authorization", "").split(" ", 1)
                userpass = base64.b64decode(value, validate=True).decode()
                password = userpass.split(":", 1)[1]
                authorized = scheme.lower() == "basic" and secrets.compare_digest(
                    password.encode(), settings.admin_token.encode()
                )
            except (ValueError, IndexError, UnicodeError):
                pass
            if not authorized:
                return Response(
                    "需要管理员登录",
                    401,
                    headers={"WWW-Authenticate": 'Basic realm="Framekeep", charset="UTF-8"'},
                )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if (origin and origin != expected) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse({"detail": "拒绝跨站操作，请从本地面板操作"}, 403)
        writes = (request.url.path.startswith("/api/")
                  and not request.url.path.startswith("/api/maintenance/")
                  and (request.method not in {"GET", "HEAD", "OPTIONS"}
                       or request.url.path.endswith(("/thumbnail", "/poster", "/download-options"))))
        if operations.maintenance and (writes or request.method not in {"GET", "HEAD", "OPTIONS"}):
            return JSONResponse({"detail": "备份维护中，请稍后重试"}, 503)
        if writes:
            operations.writing_requests += 1
        try:
            response = await call_next(request)
        finally:
            if writes:
                operations.writing_requests -= 1
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                # Native form exports need the panel's origin for the same-origin write check.
                "Referrer-Policy": "same-origin" if request.url.path in {"/", "/index.html"} else "no-referrer",
                "X-Frame-Options": "SAMEORIGIN" if request.scope.get("endpoint") is export_intake_file else "DENY",
            }
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({"detail": str(exc)[:400]}, 400)

    @app.exception_handler(StoragePressure)
    async def storage_handler(request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, 507)

    app.include_router(create_processing_router(store, processing, settings))
    app.include_router(operations.router())
    app.include_router(create_automation_router(store, automation, policy))

    def site_for(identifier):
        site = find(store.read()["sites"], identifier)
        if not site:
            raise HTTPException(404, "找不到站点")
        return site

    def asset_for(identifier):
        asset = find(store.read()["assets"], identifier)
        if not asset:
            raise HTTPException(404, "找不到素材")
        return asset

    async def browser_call(callback, *args):
        try:
            return await callback(*args)
        except (ValueError, HTTPException):
            raise
        except Exception:
            raise HTTPException(502, "浏览器操作失败，站点可能超时或需要人工处理，请重试") from None

    @app.get("/api/capabilities")
    async def capabilities():
        return {
            "mode": "real",
            "version": 1,
            "maxScanPages": 100,
            "maxDownloadBytes": settings.max_download_bytes,
            "processing": True,
            "processingMethods": {"image": "pillow", "video": "ffmpeg", "gpu": False,
                                  "removal": "fixed-region", "removalMethods": ["fixed-region", "keyframes", "chunlou-glyph"], "concurrency": 1},
            "extractor": "yt-dlp-generic",
            "downloadProtocols": ["http", "hls-vod", "dash-static"],
            "episodeCatalog": True,
            "automation": True,
            "intakeScopes": ["site", "series", "single"],
            "shortPostPlatforms": ["douyin", "tiktok", "instagram", "youtube"],
            "chunlouFeed": True,
            "chunlouSystemBrowserAuth": True,
            "accessRecovery": "once-exact-page",
        }

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": "real"}

    @app.get("/api/state")
    async def state():
        result = store.public_state()
        result.update(seriesArchiveAvailable=True,
                      coinUnlockSiteIds=[site["id"] for site in result["sites"] if supports_coin_unlock(site)])
        return result

    async def add_site(name, url, *, exact_origin=False):
        url = await policy.validate(canonical_source_url(url))
        parsed = urlsplit(url)
        domain = parsed.hostname
        if exact_origin:
            # Opening a newly added source for login should go to its homepage.
            url = parsed._replace(path="/", query="", fragment="").geturl()
        same_source = page_origin if exact_origin else host_of
        sites = store.read()["sites"]
        if page_origin(url) == ("https", "chunlou.tv", 443):
            # Older workspaces can contain a second www source. Reuse its saved account.
            matches = [
                site for site in sites
                if page_origin(canonical_source_url(site["url"])) == ("https", "chunlou.tv", 443)
            ]
            saved = (site for site in matches if site.get("sessionSaved"))
            existing = max(saved, key=lambda s: s.get("updated", ""), default=next(iter(matches), None))
        else:
            existing = next((site for site in sites if same_source(site["url"]) == same_source(url)), None)
        if existing:
            return existing
        site = {
            "id": new_id("site"),
            "name": name.strip() or domain,
            "domain": domain,
            "url": url,
            "initials": (name.strip() or domain)[:2].upper(),
            "auth": "disconnected",
            "access": "尚未保存会话",
            "updated": timestamp(),
            "count": 0,
            "sessionSaved": False,
            "verified": False,
        }
        store.mutate(lambda state: state["sites"].append(site))
        return site

    @app.post("/api/sites")
    async def create_site(data: SiteInput):
        return await add_site(data.name, data.url)

    @app.post("/api/sites/{identifier}/session")
    async def open_session(identifier: str):
        return await browser_call(browser.open_session, site_for(identifier))

    @app.get("/api/sites/{identifier}/session")
    async def session_info(identifier: str):
        site_for(identifier)
        return await browser_call(browser.session_info, identifier)

    @app.get("/api/sites/{identifier}/session/screenshot")
    async def screenshot(identifier: str):
        site_for(identifier)
        return Response(await browser_call(browser.screenshot, identifier), media_type="image/jpeg")

    @app.post("/api/sites/{identifier}/session/input")
    async def session_input(identifier: str, data: BrowserInput):
        site_for(identifier)
        return await browser_call(browser.interact, identifier, data.model_dump())

    @app.post("/api/sites/{identifier}/session/save")
    async def save_session(identifier: str):
        site_for(identifier)
        result = await browser_call(browser.save_session, identifier)

        mark_session_saved(identifier)
        return result

    def mark_session_saved(identifier):
        def save(state):
            find(state["sites"], identifier).update(
                auth="connected",
                sessionSaved=True,
                verified=False,
                access="会话已保存，权限待实际访问验证",
                updated=timestamp(),
            )
            record(state, "已加密保存站点会话，未推断账号权限", "site")

        store.mutate(save)

    def desktop_request(request):
        try:
            peer_is_local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            peer_is_local = False
        return (peer_is_local and request.url.hostname in {"127.0.0.1", "localhost", "::1"}
                and os.getenv("FRAMEKEEP_HOST", "127.0.0.1") in {"127.0.0.1", "localhost", "::1"}
                and not any(key in request.headers for key in ("forwarded", "x-forwarded-for", "x-forwarded-host")))

    def local_site(request, identifier):
        if not desktop_request(request):
            raise HTTPException(403, "请在拾帧所在电脑通过 localhost 或 127.0.0.1 使用本机授权")
        return site_for(identifier)

    def require_site_idle(identifier):
        current = store.read()
        active_assets = {a["id"] for a in current["assets"] if a.get("siteId") == identifier}
        if (any(j["assetId"] in active_assets and j["status"] in {"running", "queued"} for j in current["jobs"])
                or any(s["siteId"] == identifier and s["status"] in {"running", "queued"} for s in current["scans"])):
            raise Conflict("请先暂停该站点的下载并结束扫描，再更换授权会话")

    @app.get("/api/sites/{identifier}/local-auth")
    async def local_info(identifier: str, request: Request):
        site = site_for(identifier)
        if not desktop_request(request):
            return {"available": False, "active": False, "canImportAccount": False, "message": "本机授权需要从拾帧所在电脑访问"}
        return local_auth.info(identifier, site["url"])

    @app.post("/api/sites/{identifier}/local-auth")
    async def open_local(identifier: str, request: Request):
        site = local_site(request, identifier)
        url = site["url"]
        if page_origin(url) in {("https", "chunlou.tv", 443), ("https", "www.chunlou.tv", 443)}:
            url = urlsplit(url)._replace(path="/auth", query="r=/feed", fragment="").geturl()
        elif page_origin(url) in {("https", "lata.casa", 443), ("https", "www.lata.casa", 443)}:
            url = "https://lata.casa/login"
        return await local_auth.open(site, url)

    @app.post("/api/sites/{identifier}/local-auth/save")
    async def save_local(identifier: str, request: Request):
        site = local_site(request, identifier)
        require_site_idle(identifier)

        async def install(source, snapshot):
            require_site_idle(identifier)
            result = await browser.import_local_session(source, snapshot)
            mark_session_saved(identifier)
            return result

        return await local_auth.save(site, install)

    @app.post("/api/sites/{identifier}/local-auth/import")
    async def import_account(identifier: str, request: Request):
        site = local_site(request, identifier)
        if request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "请从本机面板确认导入")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "请从春楼账号授权面板提交")
        require_site_idle(identifier)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 65536:
                raise HTTPException(413, "春楼账号授权数据超过大小限制")
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            raise HTTPException(400, "春楼账号授权数据无效，请重新授权") from None

        async def install(source, snapshot):
            try:
                result = await browser.import_local_session(
                    source, snapshot, keep_chunlou_age=True, check_idle=lambda: require_site_idle(identifier)
                )
                mark_session_saved(identifier)
                return result
            except Conflict:
                raise
            except Exception:
                # Browser diagnostics may include submitted storage-state values.
                raise HTTPException(502, "春楼已确认账号，拾帧未能确认本机会话保存，请稍后重试") from None

        return await local_auth.import_account(site, payload, install)

    @app.delete("/api/sites/{identifier}/local-auth")
    async def cancel_local(identifier: str, request: Request):
        site = local_site(request, identifier)
        await local_auth.cancel(identifier)
        return local_auth.info(identifier, site["url"])

    @app.delete("/api/sites/{identifier}/session")
    async def disconnect(identifier: str):
        site_for(identifier)
        current = store.read()
        active_assets = {a["id"] for a in current["assets"] if a["siteId"] == identifier}
        if any(
            j["assetId"] in active_assets and j["status"] in {"running", "queued"}
            for j in current["jobs"]
        ) or any(
            s["siteId"] == identifier and s["status"] in {"running", "queued"}
            for s in current["scans"]
        ):
            raise Conflict("请先暂停下载并结束扫描，再清除会话")
        await local_auth.cancel(identifier)
        result = await browser_call(browser.disconnect, identifier)
        store.mutate(
            lambda state: find(state["sites"], identifier).update(
                auth="disconnected",
                sessionSaved=False,
                verified=False,
                access="尚未保存会话",
                updated=timestamp(),
            )
        )
        return result

    @app.post("/api/scans")
    async def scan(data: ScanInput):
        url = await policy.validate(canonical_source_url(data.url))
        site = site_for(data.siteId) if data.siteId else await add_site(urlsplit(url).hostname, url)
        if host_of(url) != host_of(site["url"]):
            raise ValueError("扫描 URL 必须属于所选站点，请切换来源或添加新站点")
        return tasks.start_scan(site, {**data.model_dump(), "url": url})

    @app.post("/api/scans/{identifier}/cancel")
    async def cancel_scan(identifier: str):
        return tasks.cancel_scan(identifier)

    @app.post("/api/series/scan-notices/dismiss")
    async def dismiss_scan_notices(data: DismissScanGroup):
        selected = set(data.scanIds)

        def dismiss(state):
            scans = [scan for scan in state["scans"] if scan["id"] in selected]
            if len(scans) != len(selected):
                raise HTTPException(404, "部分扫描记录不存在，请刷新后重试")
            for scan in scans:
                if scan.get("status") in {"queued", "running"}:
                    raise Conflict("扫描正在进行，请等结束后再清除提示")
                if scan.get("mode") not in {"series", "site"} and not observed_series_id(scan):
                    raise ValueError("所选记录不属于剧集库")
                has_warning = scan.get("status") in {"failed", "needs-user", "cancelled", "timed-out", "unsupported-drm", "unsupported-media"} or (
                    scan.get("status") == "completed"
                    and (scan.get("error") or not (scan.get("catalog") or {}).get("entries"))
                )
                if not has_warning:
                    raise ValueError("这条扫描没有可清除的提示")
            pending = [scan for scan in scans if not scan.get("seriesNoticeDismissed")]
            for scan in pending:
                scan["seriesNoticeDismissed"] = True
            return len(pending)

        return {"dismissed": store.mutate(dismiss)}

    @app.post("/api/scans/discovery-dismiss-group")
    async def dismiss_discovery_group(data: DismissScanGroup):
        selected = set(data.scanIds)

        def dismiss(state):
            scans = [scan for scan in state["scans"] if scan["id"] in selected]
            if len(scans) != len(selected):
                raise HTTPException(404, "部分扫描记录不存在，请刷新后重试")
            if any(scan.get("status") in {"queued", "running"} for scan in scans):
                raise HTTPException(409, "所选目录包含进行中的扫描，请等扫描结束后再移除")
            groups = series_library(state, include_dismissed=True)
            if not any(selected.issubset(group["sourceScanIds"]) for group in groups):
                raise ValueError("只能一起移除属于同一部剧的有效扫描目录")
            visible = [scan for scan in scans if not scan.get("discoveryCatalogDismissed")]
            for scan in visible:
                scan["discoveryCatalogDismissed"] = True
            return len(visible)

        return {"dismissed": store.mutate(dismiss)}

    @app.post("/api/scans/{identifier}/discovery-dismiss")
    async def dismiss_discovery_catalog(
        identifier: str, data: Annotated[DismissScanCatalog | DismissScanEpisodes, Field(discriminator="scope")]
    ):
        def dismiss(state):
            scan = find(state["scans"], identifier)
            if not scan:
                raise HTTPException(404, "找不到扫描记录")
            entries = (scan.get("catalog") or {}).get("entries", [])
            if not entries:
                raise ValueError("本次扫描没有剧集目录")

            known = {entry["episodeKey"] for entry in entries}
            hidden = set(scan.get("discoveryDismissedEpisodeKeys", []))
            whole = scan.get("discoveryCatalogDismissed", False)
            if data.scope == "catalog":
                count = int(not whole)
                scan["discoveryCatalogDismissed"] = True
            else:
                selected = set(data.episodeKeys)
                if selected - known:
                    raise ValueError("只能移除本次扫描目录中已观察到的剧集")
                added = selected - hidden if not whole else set()
                count = len(added)
                if added:
                    hidden.update(added)
                    scan["discoveryDismissedEpisodeKeys"] = sorted(hidden)
                    if known <= hidden:
                        scan["discoveryCatalogDismissed"] = True
            return {
                "dismissed": count,
                "discoveryCatalogDismissed": scan.get("discoveryCatalogDismissed", False),
                "discoveryDismissedEpisodeKeys": scan.get("discoveryDismissedEpisodeKeys", []),
            }

        return store.mutate(dismiss)

    def selected_series(identifier, keys=None):
        current = store.read()
        series = find(series_library(current), identifier)
        if not series:
            raise HTTPException(404, "找不到剧集目录")
        entries = series["catalog"]["entries"]
        if keys is not None:
            selected = set(keys)
            if selected - {entry["episodeKey"] for entry in entries}:
                raise ValueError("只能选择此剧已观察到的剧集页面")
            entries = [entry for entry in entries if entry["episodeKey"] in selected]
        return current, series, entries

    @app.put("/api/series/{identifier}/title")
    async def rename_series(identifier: str, data: SeriesRenameInput):
        def rename(state):
            row = find(series_library(state), identifier)
            if not row:
                raise HTTPException(404, "找不到剧集目录")
            state.setdefault("_seriesTitles", {})[identifier] = data.title
            return {"title": data.title or row["sourceTitle"], "customTitle": data.title}

        return store.mutate(rename)

    @app.post("/api/series/dismiss")
    async def dismiss_series_selection(data: DismissSeriesInput):
        if "episodeKeys" in data.model_fields_set and data.episodeKeys is None:
            raise ValueError("请选择要移除的单集")
        count = store.mutate(lambda state: dismiss_series(state, data.ids, data.episodeKeys))
        return {"dismissed": count}

    async def start_series(data: SeriesInput, *, intake=False):
        url = await policy.validate(canonical_source_url(data.url))
        site = site_for(data.siteId) if data.siteId else await add_site(urlsplit(url).hostname, url, exact_origin=intake)
        if host_of(url) != host_of(site["url"]):
            raise ValueError("剧集链接必须属于所选站点")
        if intake and page_origin(url) != page_origin(canonical_source_url(site["url"])):
            raise ValueError("整剧链接须与来源使用相同的协议、域名和端口")
        options = {"url": url, "mode": "series", "depth": 0, "limit": data.limit, "kinds": ["video"]}
        if intake:
            options.update(intakeScope="series", requestedUrl=url, kinds=data.kinds, readOnly=True, _captureCatalogImages=True)
        if data.customTitle:
            options["customTitle"] = data.customTitle
        current = store.read()
        rows = [row for row in series_library(current, include_dismissed=True)
                if row["siteId"] == site["id"] and page_origin(row["url"]) == page_origin(url)]
        route = directory_route(url)
        matched = next((row for row in rows if (
            route and row["catalog"].get("adapterId") == "lustflix"
            and row["catalog"].get("seriesKey") == route[1]
        ) or row["url"] == url or any(
            entry["pageUrl"] == url for entry in row["catalog"]["entries"]
        )), None)
        hidden = set(current.get("_dismissedSeries", []))
        omissions = current.get("_dismissedEpisodes", {})
        def hidden_ids(row):
            return hidden.intersection([row["id"], *row.get("_seriesAliasIds", [])])
        if matched and not hidden_ids(matched):
            excluded = omissions.get(matched["id"], [])
            if any(entry["pageUrl"] == url and entry["episodeKey"] in excluded
                   for entry in matched["catalog"]["entries"]):
                raise Conflict("此单集已从剧集目录移除，请使用保留的剧集链接")
            options.update(catalog=matched["catalog"], excludedEpisodeKeys=excluded)
        else:
            candidates = [row for row in ([matched] if matched else rows) if hidden_ids(row)]
            if candidates:
                # Snapshot only previously removed series. A removal made after this
                # submission wins even when an older scan finishes afterwards.
                options["restoreCandidates"] = set().union(*(hidden_ids(row) for row in candidates))
                options["knownRestoreTarget"] = next(iter(hidden_ids(matched))) if matched else None
                # Suppress implicit cached seeds: recovery requires a fresh directory.
                options["catalog"] = {}
                excluded = set()
                blocked_pages = set()
                for row in candidates:
                    omitted = set(omissions.get(row["id"], ()))
                    for entry in row["catalog"]["entries"]:
                        if entry["episodeKey"] in omitted or entry["access"] == "needs-user":
                            excluded.add(entry["episodeKey"])
                            blocked_pages.add(entry["pageUrl"])
                options["excludedEpisodeKeys"] = sorted(excluded)
                if matched and url in blocked_pages and not directory_route(url):
                    # A removed/locked episode can identify its show, but should not
                    # cause its player to open. Use an observed retained entry instead.
                    pages = [matched["url"], *(e["pageUrl"] for e in matched["catalog"]["entries"])]
                    options["url"] = next((page for page in pages if page not in blocked_pages), None)
                    if not options["url"]:
                        raise Conflict("没有可安全读取的已知入口，请提供该剧的目录页链接")
        return tasks.start_scan(site, options)

    @app.post("/api/series")
    async def create_series(data: SeriesInput):
        return await start_series(data)

    @app.post("/api/intake")
    async def create_intake(data: IntakeInput):
        if data.autoDownload and data.scope != "single":
            raise HTTPException(422, "自动下载仅适用于单集或单条作品")
        if data.scope == "series":
            return await start_series(data, intake=True)
        url = await policy.validate(canonical_source_url(data.url))
        site_url = url
        page = url
        if data.scope == "single":
            target = short_target(url)
            if target:
                site_url = target.origin
            route = directory_route(url)
            if route:
                if not route[2]:
                    raise ValueError("这个链接指向整部剧，请选择整部剧或粘贴具体某一集的播放链接")
                page = url.split("#", 1)[0].rstrip("/") + f"/#/watch/{route[1]}/{route[2]}"
        site = site_for(data.siteId) if data.siteId else await add_site(urlsplit(site_url).hostname, site_url, exact_origin=True)
        site_origin, target_origin = page_origin(site["url"]), page_origin(site_url)
        if (not site_origin or not target_origin or host_of(site_url) != host_of(site["url"])
                or site_origin[0] != target_origin[0] or site_origin[2] != target_origin[2]):
            raise ValueError("链接与所选来源不匹配，请检查域名、协议和端口")
        return tasks.start_scan(site, {"mode": data.scope, "intakeScope": data.scope,
            "url": page, "requestedUrl": url, "depth": 0, "kinds": data.kinds,
            "autoDownload": data.autoDownload,
            "catalogOnly": data.catalogOnly and data.scope == "site",
            "limit": 1 if data.scope == "single" else data.limit, "readOnly": True})

    @app.get("/api/intake/{identifier}/results")
    async def get_intake_results(identifier: str):
        state = store.read()
        scan = intake_scan(state, identifier)
        result = intake_results(scan, state)
        child = next((s for s in state["scans"] if s.get("continuedFrom") == identifier), None)
        result.update(previousScanId=scan.get("continuedFrom"), nextScanId=child["id"] if child else None,
                      canContinue=not child and scan.get("status") not in {"queued", "running"}
                      and scan.get("canContinue") is True)
        return result

    @app.post("/api/intake/{identifier}/continue")
    async def continue_intake(identifier: str, data: IntakeContinueInput):
        state = store.read()
        parent = intake_scan(state, identifier)
        child = next((s for s in state["scans"] if s.get("continuedFrom") == identifier), None)
        if child:
            return {"id": child["id"], "status": child["status"], "intakeScope": child["intakeScope"]}
        scope = parent["intakeScope"]
        if (scope not in {"site", "series"} or parent["status"] in {"queued", "running"}
                or not parent.get("canContinue")):
            raise Conflict("本次拾取没有可继续读取的目录或单集")
        options = {"mode": scope, "intakeScope": scope, "readOnly": True, "depth": 0,
                   "catalogOnly": parent.get("catalogOnly", False),
                   "url": parent["url"], "requestedUrl": parent.get("requestedUrl", parent["url"]),
                   "limit": data.limit or parent["limit"], "kinds": parent.get("intakeKinds", ["video", "image"]),
                   "_captureCatalogImages": True, "_resumeIntake": parent}
        if scope == "series":
            series_id = observed_series_id(parent)
            if series_id in state.get("_dismissedSeries", []):
                raise Conflict("此剧已移除，重新拾取该剧即可恢复")
            options.update(catalog=parent.get("catalog") or {}, customTitle=parent.get("_suggestedTitle"),
                           excludedEpisodeKeys=state.get("_dismissedEpisodes", {}).get(series_id, []))

        def carry_observations(_state, scan):
            scan["continuedFrom"] = identifier
            for field in ("_intakeRefs", "_intakeCheckpoint", "siteCatalog", "canContinue"):
                if field in parent:
                    scan[field] = parent[field]

        return tasks.start_scan(site_for(parent["siteId"]), options, on_created=carry_observations)

    @app.post("/api/intake/{identifier}/archive")
    async def export_intake(identifier: str, data: IntakeArchiveInput):
        if data.episodeKey and not data.groupKey:
            raise ValueError("导出指定单集时需要同时选择所属剧集")
        state = store.read()
        plan = intake_archive(state, intake_scan(state, identifier), data.ids,
                              settings.data_dir, data.groupKey, data.episodeKey)
        return SeriesArchiveResponse(plan, headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(plan["name"], safe="")})

    @app.post("/api/intake/{identifier}/archive-file")
    async def export_intake_file(identifier: str, request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "请从拾取结果页导出")
        # Stream the attachment directly through the browser's download manager.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4 * 1024 * 1024:
                raise HTTPException(413, "所选素材过多，请分批导出")
            body.extend(chunk)
        try:
            fields = parse_qs(body.decode(), strict_parsing=True, max_num_fields=1)
            data = IntakeArchiveInput.model_validate_json(fields["payload"][0])
        except (KeyError, IndexError, ValueError):
            raise HTTPException(422, "导出选项无效，请刷新拾取结果后重试") from None
        return await export_intake(identifier, data)

    @app.post("/api/series/{identifier}/scan")
    async def continue_series(identifier: str, data: SeriesScanInput):
        current, source, entries = selected_series(identifier, data.episodeKeys)
        site = site_for(source["siteId"])
        skipped, remaining, rechecked = [], [], []
        if data.autoUnlock is not None:
            if not data.scanAndCheck or not supports_coin_unlock(site) or source["catalog"].get("adapterId") != site["id"]:
                raise ValueError("金币解锁仅适用于已接入站点的扫描并检查所选集")
        if data.scanAndCheck:
            if data.episodeKeys is None or data.recheckAccess:
                raise ValueError("扫描并检查需要明确选集，请使用单一的检查方式")
            available = entries
        elif data.recheckAccess:
            available = entries if data.episodeKeys is not None else [
                entry for entry in entries if entry.get("access") == "needs-user"
            ]
            # Retry unknown/oldest attempts first, so persistent gates cannot
            # occupy every limited batch and starve later selected episodes.
            last_attempt = {}
            for rank, prior in enumerate(reversed(current["scans"]), 1):
                prior_catalog = prior.get("catalog", {})
                if (prior.get("siteId") == source["siteId"]
                        and all(prior_catalog.get(key) == source["catalog"].get(key)
                                for key in ("adapterId", "seriesKey"))):
                    last_attempt.update({url: rank for url in prior.get("_recheckAttemptedUrls", [])})
            available = sorted(available, key=lambda entry: (
                last_attempt.get(entry["pageUrl"], 0), entry["ordinal"]
            ))
            remaining = [entry["episodeKey"] for entry in available[data.limit:]]
            available = available[:data.limit]
            rechecked = [entry["episodeKey"] for entry in available]
        else:
            skipped = [entry["episodeKey"] for entry in entries if entry.get("access") == "needs-user"]
            available = [entry for entry in entries if entry.get("access") != "needs-user"]
            if data.episodeKeys is None:
                available = [entry for entry in available if entry.get("mediaStatus") in {"unresolved", "ambiguous"}]
        urls = list(dict.fromkeys(entry["pageUrl"] for entry in available))
        catalog = source["catalog"]
        # Revisit the observed catalog page when there are gaps in the directory itself.
        if entries and not urls and not data.recheckAccess and data.episodeKeys is None and (
            catalog.get("expectedCount") is None or catalog.get("observedGapOrdinals")
            or (catalog.get("missingCount") or 0) > 0
        ):
            urls = [source["url"]]
        if not urls:
            raise Conflict("当前没有待检查集数；处理站点权限后可使用「重新检查权限」")
        for url in urls:
            await policy.validate(url)
            if page_origin(url) != page_origin(source["url"]):
                raise ValueError("剧集页面必须与目录同源")
        excluded = current.get("_dismissedEpisodes", {}).get(identifier, [])
        if excluded:
            catalog = find(series_library(current, include_dismissed=True), identifier)["catalog"]
            excluded_keys, selected_urls = set(excluded), set(urls)
            if any(
                entry["episodeKey"] in excluded_keys and entry["pageUrl"] in selected_urls
                for entry in catalog["entries"]
            ):
                raise Conflict("目录入口已从选集中移除，请使用保留的剧集链接")
        result = tasks.start_scan(site, {
            "excludedEpisodeKeys": excluded,
            "url": urls[0], "seedUrls": urls, "mode": "series", "depth": 0,
            "limit": len(urls) if data.scanAndCheck else data.limit,
            **({"autoUnlock": data.autoUnlock.model_dump(exclude_none=True)} if data.autoUnlock is not None else {}),
            "scanAndCheck": data.scanAndCheck, "kinds": ["video"], "catalog": catalog,
            "followCatalog": not bool(available),
            "recheckUrls": urls if data.recheckAccess else [],
        })
        return {**result, "skippedEpisodeKeys": skipped,
                "recheckedEpisodeKeys": rechecked, "remainingEpisodeKeys": remaining}

    def enqueue_episodes(current, source, entries, options):
        assets = [asset for asset in current["assets"] if asset["siteId"] == source["siteId"]]
        ids, unavailable, details = [], [], []
        for entry in entries:
            selected = select_episode_asset(entry, assets)
            if selected["state"] not in {"ready", "downloaded"}:
                unavailable.append(entry["episodeKey"])
                details.append({"episodeKey": entry["episodeKey"], "reason": selected["state"]})
                continue
            ids.append(selected["assetId"])
        result = {"added": 0, "blocked": 0, "duplicates": 0}
        if ids:
            result = tasks.enqueue(list(dict.fromkeys(ids)), options)
        return {**result, "unavailable": unavailable, "unavailableDetails": details}

    @app.post("/api/series/{identifier}/downloads")
    async def download_series(identifier: str, data: SeriesDownloadInput):
        current, source, entries = selected_series(identifier, data.episodeKeys)
        covers = await posters.sources(source, current["assets"])
        for url in covers:
            await policy.validate(url)
        options = data.options.model_dump()
        assets = [a for a in current["assets"] if a.get("siteId") == source["siteId"]]
        pending = [entry for entry in entries if select_episode_asset(entry, assets)["state"] not in {"ready", "downloaded"}]
        directory_pending = source.get("directoryPending", False)
        if data.resolveMissing and (pending or directory_pending):
            urls = [source["url"]] if directory_pending else list(dict.fromkeys(entry["pageUrl"] for entry in pending))
            for url in urls:
                await policy.validate(url)
                if page_origin(url) != page_origin(source["url"]):
                    raise ValueError("剧集页面必须与目录同源")
            scan = tasks.start_scan(site_for(source["siteId"]), {
                "url": urls[0], "seedUrls": urls, "mode": "series", "depth": 0,
                "limit": len(urls), "kinds": ["video"], "catalog": source["catalog"],
                "scanAndCheck": not directory_pending, "followCatalog": directory_pending, "readOnly": True,
                "resolveDirectory": directory_pending,
                "seriesDownload": {"seriesId": identifier, "episodeKeys": None if directory_pending else [e["episodeKey"] for e in entries],
                                   "options": options, "covers": covers,
                                   "phase": "directory" if directory_pending else "episodes"},
            })
            return {"added": 0, "blocked": 0, "duplicates": 0, "unavailable": [],
                    "unavailableDetails": [], "scanId": scan["id"], "resolving": True}
        result = store.mutate(lambda state: tasks.enqueue_series_in_state(
            state, identifier, data.episodeKeys, options, covers=covers,
        ))
        return {**result, "resolving": False}

    @app.get("/api/series/{identifier}/episode-file")
    async def export_episode(identifier: str, episodeKey: str):
        current, series, entries = selected_series(identifier, [episodeKey])
        if not entries[0].get("downloaded"):
            raise Conflict("请先保存这集的原件，再导出")
        catalog = {**series["catalog"], "entries": entries, "title": series["title"]}
        plan = prepare_series_archive(
            catalog, current["assets"], site_for(series["siteId"]), settings.data_dir,
            resolved_catalog=True,
        )
        video_name = next((item.get("file") for item in plan["manifest"]["entries"]
                           if item["episodeKey"] == episodeKey), None)
        original = next((item for item in plan["files"] if item["name"] == video_name), None)
        if not original:
            raise HTTPException(404, "这集的原件已被移动或删除，请重新下载")
        return FileResponse(original["path"], filename=original["name"].rsplit("/", 1)[-1])

    @app.get("/api/series/{identifier}/archive")
    async def archive_series(identifier: str):
        current, source, _ = selected_series(identifier)
        return archive_response(source, current, resolved_catalog=True, custom_title=source["customTitle"])

    def selected_episodes(identifier, keys):
        current = store.read()
        scan = find(current["scans"], identifier)
        if not scan:
            raise HTTPException(404, "找不到扫描目录")
        catalog = update_catalog(
            scan.get("catalog", {}),
            [asset for asset in current["assets"] if asset["siteId"] == scan["siteId"]],
        )
        scan = {**scan, "catalog": catalog}
        entries = {entry["episodeKey"]: entry for entry in catalog.get("entries", [])}
        keys = list(dict.fromkeys(keys))
        if any(key not in entries for key in keys):
            raise ValueError("只能选择本次扫描已观察到的剧集页面")
        return current, scan, [entries[key] for key in keys]

    @app.post("/api/scans/{identifier}/episodes")
    async def scan_episodes(identifier: str, data: EpisodeInput):
        _, source, entries = selected_episodes(identifier, data.episodeKeys)
        site = site_for(source["siteId"])
        skipped = [entry["episodeKey"] for entry in entries if entry.get("access") == "needs-user"]
        urls = list(dict.fromkeys(
            entry["pageUrl"] for entry in entries if entry.get("access") != "needs-user"
        ))
        if not urls:
            raise Conflict("所选剧集均需要站点权限，请先自行解锁，再扫描对应的精确页面")
        for url in urls:
            await policy.validate(url)
            if host_of(url) != host_of(site["url"]):
                raise ValueError("剧集页面必须属于当前站点")
        result = tasks.start_scan(
            site,
            {
                "url": urls[0],
                "seedUrls": urls,
                "mode": "series",
                "followCatalog": False,
                "depth": 0,
                "limit": len(urls),
                "kinds": ["video"],
                "catalog": source["catalog"],
            },
        )
        return {**result, "skippedEpisodeKeys": skipped}

    @app.post("/api/scans/{identifier}/downloads")
    async def download_episodes(identifier: str, data: EpisodeDownloadInput):
        current, source, entries = selected_episodes(identifier, data.episodeKeys)
        return enqueue_episodes(current, source, entries, data.options.model_dump())

    @app.get("/api/scans/{identifier}/archive")
    async def series_archive(identifier: str):
        current = store.read()
        scan = find(current["scans"], identifier)
        if not scan:
            raise HTTPException(404, "找不到扫描目录")
        custom_title = series_titles(current).get(observed_series_id(scan), "")
        return archive_response(scan, current, custom_title=custom_title)

    def archive_response(scan, current, *, resolved_catalog=False, custom_title=""):
        catalog = scan.get("catalog", {})
        if custom_title:
            catalog = {**catalog, "title": custom_title}
        plan = prepare_series_archive(
            catalog, current["assets"],
            site_for(scan["siteId"]), settings.data_dir, resolved_catalog=resolved_catalog,
        )
        if not plan["files"]:
            raise Conflict("此目录尚无可导出的已保存剧集，请先下载或检查本地文件")
        return SeriesArchiveResponse(
            plan,
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(plan["name"])},
        )

    @app.post("/api/downloads")
    async def downloads(data: DownloadInput):
        return tasks.enqueue(data.ids, data.options.model_dump())

    @app.get("/api/assets/{identifier}/download-options")
    async def download_options(identifier: str):
        asset = asset_for(identifier)
        try:
            async with asyncio.timeout(20), inspection_slots:
                site = site_for(asset["siteId"])
                if asset.get("engine") == "yt-dlp" and host_of(site["url"]) != "lustflix.tv":
                    await browser_call(browser.open_session, site)
                return await downloader.describe_options(asset)
        except TimeoutError:
            raise HTTPException(504, "读取可用画质和音轨超时，请稍后重试") from None

    @app.post("/api/jobs/{identifier}/action")
    async def job_action(identifier: str, data: JobInput):
        options = data.options.model_dump() if data.options is not None else None
        try:
            return tasks.job_action(identifier, data.action, options)
        except KeyError:
            raise HTTPException(404, "找不到任务") from None

    @app.post("/api/jobs/pause")
    async def pause_all():
        return tasks.pause_all()

    @app.post("/api/assets/dismiss")
    async def dismiss_assets(data: DismissSelectionInput):
        selected = set(data.ids)

        def dismiss(state):
            count = 0
            for asset in state["assets"]:
                if asset["id"] in selected and not asset.get("dismissedFromDiscovery"):
                    asset["dismissedFromDiscovery"] = True
                    count += 1
            return count

        return {"dismissed": store.mutate(dismiss)}

    @app.patch("/api/assets/{identifier}")
    async def patch_asset(identifier: str, data: AssetInput):
        asset_for(identifier)
        changes = data.model_dump(exclude_none=True)
        if "tags" in changes:
            if any(len(tag) > 80 for tag in changes["tags"]):
                raise ValueError("标签过长")
            changes["tags"] = list(
                dict.fromkeys(tag.strip() for tag in changes["tags"] if tag.strip())
            )

        def update(state):
            asset = find(state["assets"], identifier)
            classification = changes.pop("classification", None)
            if classification:
                if asset.get("kind") != "video":
                    raise ValueError("内容识别仅用于视频素材")
                changes["classification"] = (
                    automatic_classification(asset)
                    if classification == "automatic"
                    else manual_classification(classification)
                )
            asset.update(changes)

        store.mutate(update)
        return {"updated": True}

    @app.get("/api/assets/{identifier}/file")
    async def asset_file(identifier: str, download: bool = False):
        asset = asset_for(identifier)
        try:
            path = safe_file(settings.data_dir, asset.get("_path", ""))
        except FileNotFoundError:
            raise HTTPException(404, "文件尚未下载或已被移动") from None
        return FileResponse(
            path,
            media_type=asset.get("mime", "application/octet-stream"),
            filename=download_filename(asset, site_for(asset["siteId"])) if download else None,
        )

    @app.get("/api/series/{identifier}/poster")
    async def series_poster(identifier: str):
        current, series, _ = selected_series(identifier)
        path = await posters.get(series, current["assets"])
        if not path:
            raise HTTPException(404, "此剧暂无可用封面")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/assets/{identifier}/thumbnail")
    async def thumbnail(identifier: str):
        asset = asset_for(identifier)
        if not (asset.get("_path") or asset.get("_readOnly") or asset.get("shortMedia")) and asset.get("_thumbnailUrl"):
            site = site_for(asset["siteId"])
            if site.get("sessionSaved"):
                try:
                    await browser_call(browser.open_session, site)
                except HTTPException:
                    return FileResponse(
                        settings.static_dir / "assets/favicon.svg", media_type="image/svg+xml"
                    )
        path = await thumbnails.get(asset)
        return FileResponse(
            path or settings.static_dir / "assets/favicon.svg",
            media_type="image/jpeg" if path else "image/svg+xml",
        )

    @app.patch("/api/settings")
    async def patch_settings(data: SettingsInput):
        store.mutate(
            lambda state: state["settings"].update(
                concurrency=data.concurrency, autoProcess=False, preserveOriginal=True
            )
        )
        return store.read()["settings"]

    app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="panel")
    return app
