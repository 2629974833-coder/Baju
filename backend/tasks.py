"""Small persistent task queue for a single local/server process."""

import asyncio
import contextlib
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from backend.classification import classify_asset
from backend.coin_unlock import CoinUnlockBatch, empty_summary
from backend.downloader import DownloadAccessRequired, DownloadCancelled, DownloadPaused
from backend.selection import normalize_selection
from backend.series import (
    observed_series_id,
    restore_scanned_series,
    series_identifier,
    series_library,
    series_observations,
)
from backend.series_artwork import ensure_artwork_assets
from backend.series_check import scan_and_check
from backend.site_adapters import (
    build_catalog,
    refresh_identity_matches,
    scan_episode_outcomes,
    select_episode_asset,
    stable_asset_identity,
    update_catalog,
)
from backend.store import new_id, public_fields, record, timestamp


class Conflict(ValueError):
    pass


_RECOVERY_MESSAGES = {
    "session-required": "会话需要更新，请打开原站点窗口登录后重试",
    "access-required": "原内容要求访问权限，请在站点窗口中确认后重试",
    "verification-required": "原页面要求人工验证，请在站点窗口中完成后重试",
    "media-unavailable": "原页面未发现可匹配媒体，请打开原页面播放后重新扫描",
    "identity-mismatch": "刷新结果与原剧集或媒体不一致，已停止替换，请重新扫描原页面",
    "ambiguous-media": "原页面有多个可能匹配的媒体，无法安全选择，请重新扫描原页面",
    "refresh-failed": "原媒体链接刷新未完成，请检查会话并重新扫描原页面",
    "repeated-access-error": "刷新一次后媒体仍拒绝访问，请在站点窗口检查会话和权限后重试",
    "unsupported-media": "原页面媒体格式或保护方式不受支持，请检查原页面",
}


class _RecoveryRequired(ValueError):
    def __init__(self, code):
        self.code = code if code in _RECOVERY_MESSAGES else "refresh-failed"
        super().__init__(_RECOVERY_MESSAGES[self.code])


@dataclass
class Control:
    paused: bool = False
    cancelled: bool = False

    def check(self):
        if self.cancelled:
            raise DownloadCancelled()
        if self.paused:
            raise DownloadPaused()


def find(items, identifier):
    return next((item for item in items if item["id"] == identifier), None)


def duration_label(value):
    if isinstance(value, (int, float)) and value > 0:
        seconds = int(value)
        return f"{seconds // 60}:{seconds % 60:02}"
    return value if isinstance(value, str) else "—"


class TaskManager:
    progress_interval = 0.5
    refresh_timeout = 35

    def __init__(self, store, browser, scanner, downloader, *, storage_guard=None, monitor=None, purchase_factory=None,
                 resource_limits=None):
        self.resource_limits = resource_limits
        self.purchase_factory = purchase_factory
        self.store, self.browser, self.scanner, self.downloader = (
            store,
            browser,
            scanner,
            downloader,
        )
        self.active = {}
        self.scan_tasks = {}
        self.scan_controls = {}
        self._scheduler = None
        self._closing = False
        self._pending_progress = {}
        self.storage_guard, self.monitor = storage_guard, monitor
        self.maintenance = False

    async def start(self):
        self.store.recover_interrupted()
        def finish_ready(state):
            for scan in state["scans"]:
                self._complete_series_downloads(state, scan)
        self.store.mutate(finish_ready)
        self._scheduler = asyncio.create_task(self._schedule())

    async def close(self):
        self._closing = True
        if self._scheduler:
            self._scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler
        for control, _ in self.active.values():
            control.paused = True
        for control in self.scan_controls.values():
            control.cancelled = True
        tasks = [task for _, task in self.active.values()] + list(self.scan_tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=3)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _schedule(self):
        while not self._closing:
            if self.monitor:
                self.monitor.beat("download")
            if self.maintenance:
                await asyncio.sleep(0.2)
                continue
            state = self.store.read()
            slots = max(0, state["settings"]["concurrency"] - len(self.active))
            reserved = {job["assetId"] for job in state["jobs"] if job["id"] in self.active}
            for job in state["jobs"]:
                if not slots:
                    break
                if job["status"] != "queued" or job["assetId"] in reserved:
                    continue
                control = Control()
                task = asyncio.create_task(self._download(job["id"], control))
                self.active[job["id"]] = (control, task)
                reserved.add(job["assetId"])
                slots -= 1
            await asyncio.sleep(0.2)

    def _asset_reserved(self, state, asset_id, exclude=None):
        return any(
            job["id"] != exclude
            and job["assetId"] == asset_id
            and (job["status"] in {"queued", "running", "paused", "needs-user"} or job["id"] in self.active)
            for job in state["jobs"]
        )

    def enqueue(self, ids, options=None):
        return self.store.mutate(lambda state: self.enqueue_in_state(state, ids, options))

    def enqueue_series_in_state(self, state, identifier, keys, options, *, covers=()):
        source = find(series_library(state), identifier)
        if not source:
            raise Conflict("此剧已移除，未继续添加下载")
        source["catalog"]["coverUrls"] = list(dict.fromkeys([
            *source["catalog"].get("coverUrls", []), *covers,
        ]))
        selected = set(keys) if keys is not None else None
        entries = [e for e in source["catalog"]["entries"]
                   if selected is None or e["episodeKey"] in selected]
        assets = [a for a in state["assets"] if a.get("siteId") == source["siteId"]]
        ids, unavailable, details = [], [], []
        for entry in entries:
            choice = select_episode_asset(entry, assets)
            if choice["state"] in {"ready", "downloaded"}:
                ids.append(choice["assetId"])
            else:
                unavailable.append(entry["episodeKey"])
                details.append({"episodeKey": entry["episodeKey"], "reason": choice["state"]})
        artwork_ids = ensure_artwork_assets(state, source)
        ids.extend(artwork_ids)
        result = self.enqueue_in_state(state, list(dict.fromkeys(ids)), options)
        return {**result, "unavailable": unavailable, "unavailableDetails": details,
                "artworkCount": len(artwork_ids)}

    def _complete_series_downloads(self, state, scan):
        plan = scan.get("_seriesDownload")
        if not plan or scan.get("status") != "completed" or "downloadSummary" in scan:
            return
        if plan.get("phase") == "directory":
            scan.update(status="failed", message="目录已读取，单集检查未完成，请重新点击下载本剧")
            return
        try:
            scan["downloadSummary"] = self.enqueue_series_in_state(
                state, plan["seriesId"], plan.get("episodeKeys"), plan["options"],
                covers=plan.get("covers", []),
            )
        except ValueError as error:
            scan["downloadSummary"] = {"added": 0, "blocked": 0, "duplicates": 0,
                                       "unavailable": [], "unavailableDetails": [], "error": str(error)}

    def _complete_single_intake_downloads(self, state, scan):
        if (self._closing or scan.get("_singleIntakeDownload") is not True
                or scan.get("intakeScope") != "single" or scan.get("mode") != "single"
                or scan.get("status") != "completed" or "downloadSummary" in scan):
            return
        empty = {"added": 0, "blocked": 0, "duplicates": 0}
        if (scan.get("limited") or (scan.get("catalog") or {}).get("truncated")
                or (scan.get("extraction") or {}).get("status") == "limited"):
            scan["downloadSummary"] = {**empty, "error": "解析结果不完整，未自动下载；请检查素材后手动下载"}
            return
        # Only observations recorded by this scan can enter its download batch.
        observed = {identifier for pages in scan.get("_intakeRefs", {}).values()
                    for identifiers in pages.values() for identifier in identifiers}
        kinds = scan.get("intakeKinds", [])
        ids = [asset["id"] for asset in state["assets"]
               if asset["id"] in observed and asset.get("siteId") == scan["siteId"]
               and asset.get("kind") in kinds]
        try:
            scan["downloadSummary"] = self.enqueue_in_state(state, ids)
        except ValueError as error:
            scan["downloadSummary"] = {**empty, "error": str(error)[:400]}

    def enqueue_in_state(self, state, ids, options=None, *, rule=None):
        """Admit downloads inside the caller's transaction, including an automation checkpoint."""
        if self.maintenance or self._closing:
            raise Conflict("维护期间不能创建下载任务")
        if self.storage_guard:
            self.storage_guard.check()
        selection = normalize_selection(options)

        result = {"added": 0, "blocked": 0, "duplicates": 0}
        for asset_id in dict.fromkeys(ids):
            asset = find(state["assets"], asset_id)
            if not asset or asset.get("locked") or asset.get("unsupported"):
                result["blocked"] += 1
            elif asset["status"] == "downloaded" or self._asset_reserved(state, asset_id):
                result["duplicates"] += 1
            else:
                job = {
                    "id": new_id("job"),
                    "assetId": asset_id,
                    "type": "download",
                    "status": "queued",
                    "progress": 0,
                    "downloadOptions": dict(selection),
                    "createdAt": timestamp(),
                }
                if rule:
                    job.update(ruleId=rule["id"], _ruleUrl=rule["url"], _ruleSiteId=rule["siteId"])
                state["jobs"].append(job)
                result["added"] += 1
        if result["added"]:
            record(state, f"已加入 {result['added']} 个真实下载任务", "download")
        return result

    def job_action(self, job_id, action, options=None):
        if self.maintenance:
            raise Conflict("维护期间不能修改下载任务")
        if options is not None and action != "retry":
            raise Conflict("只能对已停止的任务修改选项并重试")
        selection = normalize_selection(options) if options is not None else None

        def change(state):
            job = find(state["jobs"], job_id)
            if not job:
                raise KeyError("找不到任务")
            allowed = {
                "pause": {"queued", "running"},
                "resume": {"paused"},
                "retry": {"failed", "cancelled", "needs-user"},
                "cancel": {"queued", "running", "paused", "failed", "needs-user"},
            }
            if job["status"] not in allowed[action]:
                raise Conflict("当前任务状态不支持此操作")
            # A finishing transfer owns its status until it has closed its file handles.
            if action in {"resume", "retry"} and job_id in self.active:
                raise Conflict("任务正在停止，请稍后继续")
            if action in {"resume", "retry"}:
                if self._asset_reserved(state, job["assetId"], exclude=job_id):
                    raise Conflict("此素材已有下载任务，请继续现有任务")
                asset = find(state["assets"], job["assetId"])
                if asset and asset["status"] == "downloaded":
                    raise Conflict("此素材已下载并校验，无需重试历史任务")
            if job_id in self.active:
                control = self.active[job_id][0]
                if action == "pause":
                    control.paused = True
                if action == "cancel":
                    control.cancelled = True
            job.update(self._pending_progress.get(job_id, {}))
            job["status"] = {
                "pause": "paused",
                "resume": "queued",
                "retry": "queued",
                "cancel": "cancelled",
            }[action]
            if action in {"resume", "retry"}:
                job.update(error="")
            if action == "retry":
                job.update(failureCode="", recoveryAction="", refreshCount=0)
                if selection is not None and selection != job.get("downloadOptions"):
                    job["downloadOptions"] = selection
                    job.pop("selection", None)
                    job.update(
                        progress=0, downloadedBytes=0, totalBytes=0, resumedBytes=0,
                        completedSegments=0, totalSegments=0, phase="queued",
                    )
            return job

        result = self.store.mutate(change)
        self._pending_progress.pop(job_id, None)
        if (
            action == "cancel"
            and job_id not in self.active
            and self.downloader
            and not self._asset_reserved(self.store.read(), result["assetId"], exclude=job_id)
        ):
            self.downloader.discard_partial(result["assetId"])
        return result

    def pause_all(self):
        count = 0
        for job in self.store.read()["jobs"]:
            if job["status"] in {"queued", "running"}:
                self.job_action(job["id"], "pause")
                count += 1
        return {"paused": count}

    def _job_update(self, identifier, **values):
        pending = self._pending_progress.get(identifier, {})

        def update(state):
            job = find(state["jobs"], identifier)
            if job:
                job.update(pending)
                job.update(values)
                if job.get("ruleId") and values.get("failureCode") in {
                    "session-required", "access-required", "verification-required", "repeated-access-error"
                }:
                    rule = find(state["rules"], job["ruleId"])
                    if (rule and rule["enabled"] and rule["autoDownload"]
                            and rule["siteId"] == job.get("_ruleSiteId")
                            and rule["url"] == job.get("_ruleUrl")):
                        rule.update(enabled=False, nextRunAt=None, error=values["error"])
                        if rule["status"] == "running":
                            rule["_downloadAccessError"] = values["error"]
                        else:
                            rule["status"] = "needs-user"

        self.store.mutate(update)
        self._pending_progress.pop(identifier, None)

    async def _resolve_asset(self, site, asset, control):
        control.check()
        if not hasattr(self.scanner, "resolve_asset"):
            raise _RecoveryRequired("media-unavailable")
        operation = asyncio.create_task(self.scanner.resolve_asset(
            site, asset, lambda: control.cancelled or control.paused
        ))
        try:
            async with asyncio.timeout(self.refresh_timeout):
                while not operation.done():
                    control.check()
                    await asyncio.wait({operation}, timeout=0.1)
                control.check()
                return await operation
        except (DownloadCancelled, DownloadPaused, asyncio.CancelledError):
            raise
        except Exception:
            raise _RecoveryRequired("refresh-failed") from None
        finally:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    @staticmethod
    def _refresh_fields(existing, candidate):
        # Watermark evidence describes the media bytes selected at download time.
        if existing.get("status") == "downloaded" and (existing.get("shortMedia") or candidate.get("shortMedia")):
            return
        # Never import paths, IDs, user labels or task state from a new extraction.
        for key in ("sourceUrl", "engine", "_protocol", "_extractor", "_mediaLabel", "_episodeEvidence",
                    "_readOnly", "shortMedia", "_resolver", "_formatId", "_youtubeFormats", "_youtubeDuration", "_shortDuration",
                    "_instagramAudio", "_instagramDuration", "_instagramAudioExpected",
                    "_chunlouEpisode", "_chunlouDuration", "_lustflixGrant"):
            if key in candidate:
                existing[key] = candidate[key]
        existing["_headers"] = candidate.get("_headers", {})
        existing["_observedMime"] = candidate.get("mime", "")
        if existing.get("status") != "downloaded" and candidate.get("mime"):
            existing["mime"] = candidate["mime"]
        identity = stable_asset_identity(existing)
        if identity:
            existing["_sourceIdentity"] = identity
        if existing.get("kind") == "video":
            existing["classification"] = classify_asset(existing)

    async def _download_with_refresh(self, identifier, site, asset, control, progress):
        job = find(self.store.read()["jobs"], identifier)
        selection = normalize_selection(job.get("downloadOptions"))
        for attempt in range(2):
            control.check()
            try:
                return await self.downloader.download(
                    {**asset, "_downloadOptions": dict(selection)}, control, progress
                )
            except DownloadAccessRequired:
                control.check()
                job = find(self.store.read()["jobs"], identifier)
                if attempt or job.get("refreshCount", 0) >= 1:
                    raise _RecoveryRequired("repeated-access-error") from None
            self._job_update(identifier, phase="refreshing", refreshCount=1)
            refresh_asset = {**asset, "_readOnly": True} if job.get("ruleId") else asset
            result = await self._resolve_asset(site, refresh_asset, control)
            control.check()
            if not isinstance(result, dict) or result.get("status") != "resolved":
                code = result.get("reasonCode") if isinstance(result, dict) else None
                raise _RecoveryRequired(code)
            candidate = result.get("candidate")
            if not refresh_identity_matches(asset, candidate):
                raise _RecoveryRequired("identity-mismatch")

            def replace_source(state):
                current = find(state["assets"], asset["id"])
                self._refresh_fields(current, candidate)
                return current

            asset = self.store.mutate(replace_source)
            await progress({"phase": "downloading", "retryInSeconds": 0})

    def _resource_slot(self, kind, control):
        if self.resource_limits is None:
            return contextlib.nullcontext()
        return self.resource_limits.slot(kind, check=control.check)

    async def _download(self, identifier, control):
        try:
            state = self.store.read()
            job = find(state["jobs"], identifier)
            async with self._resource_slot("download", control):
                control.check()
                state = self.store.read()
                job = find(state["jobs"], identifier)
                asset = find(state["assets"], job["assetId"])
                site = find(state["sites"], asset["siteId"])
                if self.storage_guard:
                    self.storage_guard.check()
                self._job_update(identifier, status="running", error="", failureCode="", recoveryAction="")
                if not (job.get("ruleId") or asset.get("_readOnly") or asset.get("shortMedia")) and host_of(site["url"]) != "lustflix.tv":
                    await self.browser.open_session(site)
                control.check()
                last_write = 0
                last_events = {}
                event_keys = {
                    "phase",
                    "resumedBytes",
                    "retryCount",
                    "retryInSeconds",
                    "resumeSupported",
                    "resumeMessage",
                    "selection",
                }

                async def progress(values):
                    nonlocal last_write
                    if self.storage_guard and values.get("phase") != "completed":
                        self.storage_guard.check()
                    if not control.paused and not control.cancelled:
                        self._pending_progress.setdefault(identifier, {}).update(values)
                        events = {key: values[key] for key in event_keys if key in values}
                        changed = any(last_events.get(key) != value for key, value in events.items())
                        now = time.monotonic()
                        if changed or now - last_write >= self.progress_interval:
                            self._job_update(identifier)
                            last_write = now
                        last_events.update(events)

                result = await self._download_with_refresh(identifier, site, asset, control, progress)
                control.check()

                def complete(state):
                    job = find(state["jobs"], identifier)
                    job.update(self._pending_progress.get(identifier, {}))
                    asset = find(state["assets"], job["assetId"])
                    asset.update(
                        status="downloaded",
                        _path=result["path"],
                        sizeMB=round(result["size"] / 1024**2, 2),
                        sizeBytes=result["size"],
                        sha256=result["sha256"],
                        mime=result["mime"],
                        width=result.get("width", 0),
                        height=result.get("height", 0),
                        duration=duration_label(result.get("duration")),
                        fileUrl=f"/api/assets/{asset['id']}/file",
                        downloadedAt=timestamp(),
                    )
                    asset["versions"] = [
                        {
                            "id": "original",
                            "label": "原始文件",
                            "createdAt": timestamp(),
                            "path": asset["fileUrl"],
                        }
                    ]
                    job.update(status="completed", progress=100, completedAt=timestamp(), error="")
                    if result.get("selection"):
                        job["selection"] = result["selection"]
                        asset["downloadSelection"] = result["selection"]
                    for scan in state["scans"]:
                        if scan.get("catalog") and scan.get("siteId") == asset["siteId"]:
                            scan["catalog"] = update_catalog(
                                scan["catalog"], [item for item in state["assets"]
                                                  if item["siteId"] == asset["siteId"]]
                            )
                    record(state, f"已保存并校验：{asset['title']}", "download")

                self.store.mutate(complete)
        except (Exception, asyncio.CancelledError) as error:
            if control.cancelled or isinstance(error, DownloadCancelled):
                if self.downloader:
                    self.downloader.discard_partial(job["assetId"])
                self._job_update(identifier, status="cancelled", error="任务已取消")
            elif control.paused or isinstance(error, (DownloadPaused, asyncio.CancelledError)):
                self._job_update(
                    identifier, status="paused", error="任务已暂停；继续时校验并恢复可用进度"
                )
            elif getattr(error, "code", "") == "storage-low":
                self._job_update(identifier, status="paused", phase="paused",
                                 error=str(error), failureCode="storage-low")
            elif isinstance(error, _RecoveryRequired):
                action = "open-session" if error.code in {
                    "session-required", "access-required", "verification-required", "repeated-access-error"
                } else "rescan-exact-page"
                self._job_update(identifier, status="needs-user", phase="needs-user", error=str(error),
                                 failureCode=error.code, recoveryAction=action)
            else:
                detail = (
                    str(error)[:400]
                    if isinstance(error, ValueError)
                    else "下载失败：站点连接、会话或媒体解析不可用，请检查授权后重试"
                )
                self._job_update(identifier, status="failed", error=detail)
        finally:
            self._pending_progress.pop(identifier, None)
            self.active.pop(identifier, None)

    def start_scan(self, site, options, *, on_created=None):
        if self.maintenance or self._closing:
            raise Conflict("维护期间不能创建扫描任务")
        if self.storage_guard:
            self.storage_guard.check()
        if self.scan_tasks:
            raise Conflict("已有扫描正在运行，请等待完成或先取消")
        options = dict(options)
        current = self.store.read()
        if options.get("mode") == "site":
            options["restoreSiteCandidates"] = set(current.get("_dismissedSeries", []))
        if options.get("mode") not in {"site", "single"} and not isinstance(options.get("catalog"), dict):
            target = build_catalog(options["url"], [])
            if target.get("seriesKey"):
                target_origin = origin_of(options["url"])
                options["catalog"] = target
                for scan in current["scans"]:
                    catalog = scan.get("catalog", {})
                    if (
                        scan.get("siteId") == site["id"]
                        and catalog.get("seriesKey") == target["seriesKey"]
                        and catalog.get("adapterId") == target["adapterId"]
                        and origin_of(scan.get("url", "")) == target_origin
                        and all(
                            origin_of(entry["pageUrl"]) == target_origin
                            for entry in catalog.get("entries", [])
                        )
                    ):
                        options["catalog"] = catalog
                        break
        identifier = new_id("scan")
        scan = {
            "id": identifier,
            "siteId": site["id"],
            "url": options["url"],
            "mode": options.get("mode", "page"),
            "limit": options.get("limit", 20),
            "status": "queued",
            "pagesVisited": 0,
            "assetsFound": 0,
            "createdAt": timestamp(),
            "message": "等待开始",
        }
        if options.get("scanAndCheck"):
            scan.update(checkTotal=len(options["seedUrls"]), checkedCount=0)
            if "autoUnlock" in options:
                scan["unlockSummary"] = empty_summary(options["autoUnlock"].get("maxCoins"))
        if options.get("seriesDownload"):
            scan["downloadAfterScan"] = True
        if isinstance(options.get("catalog"), dict):
            scan["catalog"] = update_catalog(
                options["catalog"], [asset for asset in current["assets"]
                                     if asset.get("siteId") == site["id"]]
            )
        if options.get("intakeScope"):
            scan["intakeScope"] = options["intakeScope"]
            scan["intakeKinds"] = list(options["kinds"])
            scan["requestedUrl"] = options.get("requestedUrl", options["url"])
            scan.setdefault("downloadAfterScan", options.get("autoDownload") is True
                            and options["intakeScope"] == "single" and scan["mode"] == "single")
            if options.get("catalogOnly"):
                scan["catalogOnly"] = True
        stored_scan = dict(scan)
        if scan.get("intakeScope") == "single" and scan.get("downloadAfterScan") is True:
            stored_scan["_singleIntakeDownload"] = True
        if options.get("seriesDownload"):
            stored_scan["_seriesDownload"] = options["seriesDownload"]
        if options.get("customTitle"):
            stored_scan["_suggestedTitle"] = options["customTitle"]
        def create(state):
            if on_created:
                on_created(state, stored_scan)
            state["scans"].insert(0, stored_scan)

        self.store.mutate(create)
        control = Control()
        self.scan_controls[identifier] = control
        self.scan_tasks[identifier] = asyncio.create_task(
            self._scan(identifier, site, options, control)
        )
        self.scan_tasks[identifier].add_done_callback(lambda task: self._finish_scan(identifier))
        return public_fields(scan)

    def _finish_scan(self, identifier):
        self.store.mutate(
            lambda state: find(state["scans"], identifier).update(finishedAt=timestamp())
        )
        self.scan_controls.pop(identifier, None)
        self.scan_tasks.pop(identifier, None)

    def cancel_scan(self, identifier):
        control = self.scan_controls.get(identifier)
        if not control:
            raise Conflict("此扫描已结束")
        control.cancelled = True
        self.store.mutate(
            lambda state: find(state["scans"], identifier).update(
                status="cancelled", message="扫描已取消，已发现素材保留"
            )
        )
        self.scan_tasks[identifier].cancel()
        return {"cancelled": True}

    async def _scan(self, identifier, site, options, control):
        async def progress(values):
            def update(state):
                scan = find(state["scans"], identifier)
                previous_status = scan["status"]
                scan.update(values)
                if scan.get("catalog"):
                    scan["catalog"] = update_catalog(
                        scan["catalog"], [asset for asset in state["assets"]
                                          if asset.get("siteId") == site["id"]],
                        episode_outcomes=scan_episode_outcomes(scan),
                    )
                if values.get("status") == "completed" and options.get("restoreCandidates") and not control.cancelled:
                    restored_id = restore_scanned_series(state, scan, options)
                    if restored_id:
                        scan["restoredSeriesId"] = restored_id
                        record(state, "已重新扫描并恢复剧集目录", "scan")
                    elif options.get("knownRestoreTarget") in state.get("_dismissedSeries", []):
                        message = "本次未能确认原剧目录，剧集仍已移除。请使用剧集详情页或其他集的链接重试。"
                        scan.update(status="failed", message=message, error=message)
                if values.get("status") == "completed" and options.get("restoreSiteCandidates") and not control.cancelled:
                    observed = set()
                    for row in series_observations({"scans": [scan]}):
                        found = observed_series_id(row) or (series_identifier(row) if row.get("_indexOnly") else None)
                        if found:
                            observed.update([found, *row.get("_seriesAliasIds", [])])
                    restored = observed & options["restoreSiteCandidates"]
                    state["_dismissedSeries"] = [key for key in state.get("_dismissedSeries", []) if key not in restored]
                if (values.get("status") == "completed" and not control.cancelled
                        and previous_status in {"queued", "running", "completed"}):
                    self._complete_single_intake_downloads(state, scan)
            self.store.mutate(update)

        async def asset_found(candidate):
            def save(state):
                identity = stable_asset_identity(candidate)
                stable = hashlib.sha256(
                    (site["id"] + "\0" + (identity or candidate["sourceUrl"])).encode()
                ).hexdigest()[:32]
                asset_id = "asset_" + stable
                existing = find(state["assets"], asset_id)
                if not existing and identity:
                    # Old workspaces retain their original IDs and verified file locations.
                    matches = [asset for asset in state["assets"]
                               if asset.get("siteId") == site["id"]
                               and stable_asset_identity(asset) == identity]
                    existing = next((asset for asset in matches if asset.get("status") == "downloaded"),
                                    matches[0] if matches else None)
                if options.get("intakeScope"):
                    scan = find(state["scans"], identifier)
                    group_url = candidate.pop("_intakeGroupUrl", "")
                    page_url = candidate.pop("_intakePageUrl", None) or candidate.get("pageUrl", options["url"])
                    pages = scan.setdefault("_intakeRefs", {}).setdefault(group_url, {})
                    ids = pages.setdefault(page_url, [])
                    current_id = existing["id"] if existing else asset_id
                    if current_id not in ids:
                        ids.append(current_id)
                    if options["intakeScope"] == "single" and candidate.get("title"):
                        scan.setdefault("workTitle", candidate["title"])
                    candidate["_readOnly"] = True
                if rule_id := options.get("ruleId"):
                    rule = find(state["rules"], rule_id)
                    scan = find(state["scans"], identifier)
                    current_id = existing["id"] if existing else asset_id
                    if current_id not in scan.setdefault("_observedAssetIds", []):
                        scan["_observedAssetIds"].append(current_id)
                    if not existing and rule:
                        rule.setdefault("_pendingAssetIds", []).append(current_id)
                if existing:
                    self._refresh_fields(existing, candidate)
                    existing["_mediaLabel"] = candidate.get("_mediaLabel", "")
                    existing["pageUrl"] = candidate.get("pageUrl", existing.get("pageUrl", ""))
                    if existing.get("kind") == "video":
                        existing["classification"] = classify_asset(existing)
                    return
                if len(state["assets"]) >= 10000:
                    raise ValueError("素材库已达到本版本的 10000 条容量上限")
                thumbnail = candidate.pop("thumbnail", "")
                if candidate.get("kind") == "video":
                    candidate["classification"] = classify_asset(candidate)
                state["assets"].append(
                    {
                        **candidate,
                        **({"_sourceIdentity": identity} if identity else {}),
                        "id": asset_id,
                        "siteId": site["id"],
                        "date": timestamp()[:10],
                        "duration": duration_label(candidate.get("duration")),
                        "sizeMB": 0,
                        "status": "discovered",
                        "locked": bool(candidate.get("locked")),
                        "tags": [],
                        "favorite": False,
                        "versions": [],
                        "_thumbnailUrl": thumbnail,
                        "thumbnail": f"/api/assets/{asset_id}/thumbnail",
                    }
                )
                target = find(state["sites"], site["id"])
                target.update(
                    count=sum(a["siteId"] == site["id"] for a in state["assets"]),
                    updated=timestamp(),
                )

            self.store.mutate(save)

        try:
            if self.resource_limits is not None:
                await progress({"status": "queued", "message": "等待服务器扫描资源"})
            async with self._resource_slot("scan", control):
                await progress({"status": "running"})
                if options.get("resolveDirectory"):
                    await self.scanner.scan(site, {**options, "catalog": {}, "seedUrls": [],
                                                  "_directoryOnly": True}, asset_found, progress,
                                            lambda: control.cancelled)
                    current = find(self.store.read()["scans"], identifier)
                    if control.cancelled or current.get("status") != "completed":
                        return
                    catalog = current.get("catalog") or {}
                    resolved_id = observed_series_id(current)
                    if not resolved_id:
                        raise ValueError("尚未读取到此剧的单集目录，请重新扫描目录后下载")
                    options.update(catalog=catalog, seedUrls=[e["pageUrl"] for e in catalog["entries"]],
                                   scanAndCheck=True, followCatalog=False)
                    options["seriesDownload"]["seriesId"] = resolved_id
                    options["seriesDownload"]["phase"] = "episodes"
                    self.store.mutate(lambda state: find(state["scans"], identifier).update(
                        _seriesDownload=options["seriesDownload"], status="running",
                        resolvedSeriesId=resolved_id,
                        checkTotal=len(options["seedUrls"]), checkedCount=0,
                    ))
                if options.get("scanAndCheck"):
                    buyer = None
                    if "autoUnlock" in options:
                        buyer = CoinUnlockBatch(self.store, identifier, site, self.purchase_factory(site),
                                                options["autoUnlock"].get("maxCoins"))
                    await scan_and_check(self.scanner, site, options, asset_found, progress, lambda: control.cancelled,
                                         buyer=buyer)
                else:
                    await self.scanner.scan(site, options, asset_found, progress, lambda: control.cancelled)
                current = find(self.store.read()["scans"], identifier)
                if current["status"] in {"running", "queued"}:
                    await progress({"status": "completed", "message": "扫描完成"})
                if not control.cancelled and not self._closing and options.get("seriesDownload"):
                    self.store.mutate(lambda state: self._complete_series_downloads(state, find(state["scans"], identifier)))
        except (asyncio.CancelledError, DownloadCancelled):
            await progress({"status": "cancelled", "message": "扫描已取消，已发现素材保留"})
        except ValueError as error:
            await progress(
                {"status": "failed", "error": str(error)[:400], "message": str(error)[:400]}
            )
        except Exception:
            await progress(
                {
                    "status": "failed",
                    "error": "站点连接或页面解析失败，请稍后重试",
                    "message": "站点连接或页面解析失败",
                }
            )


def safe_file(data_dir, relative):
    path = (Path(data_dir) / relative).resolve()
    if not path.is_relative_to(Path(data_dir).resolve() / "assets") or not path.is_file():
        raise FileNotFoundError("文件不存在")
    return path


def host_of(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def origin_of(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port
