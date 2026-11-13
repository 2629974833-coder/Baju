"""Durable CPU processing queue; originals and registered versions are immutable."""

import asyncio
import contextlib
import re
import shutil
import time

from backend.downloader import DownloadCancelled, DownloadPaused
from backend.processing import normalize_processing_options
from backend.store import new_id, record, timestamp
from backend.tasks import Conflict, Control, find, safe_file


class ProcessingQueue:
    def __init__(self, store, processor, *, storage_guard=None, monitor=None):
        self.store, self.processor = store, processor
        self.storage_guard, self.monitor = storage_guard, monitor
        self.data_dir = store.path.parent.resolve()
        self.active = {}
        self.maintenance = False
        self._scheduler = None
        self._closing = False

    async def start(self):
        def recover(state):
            for job in state.setdefault("processingJobs", []):
                if job["status"] == "running":
                    job.update(
                        status="paused",
                        phase="paused",
                        error="服务重启中断处理；继续时从原件重新处理",
                    )

        self.store.mutate(recover)
        self._closing = False
        self._scheduler = asyncio.create_task(self._schedule())

    async def close(self):
        self._closing = True
        if self._scheduler:
            self._scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler
        for control, _ in self.active.values():
            control.paused = True
        tasks = [task for _, task in self.active.values()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _has_result(self, asset, options):
        for version in (asset or {}).get("versions", []):
            if version.get("options") != options or version.get("status") != "completed":
                continue
            try:
                safe_file(self.data_dir, version.get("_path", ""))
                return True
            except FileNotFoundError:
                pass
        return False

    def enqueue(self, ids, options):
        if self.maintenance or self._closing:
            raise Conflict("维护期间不能创建处理任务，请稍后重试")
        options = normalize_processing_options(options)
        if self.storage_guard:
            self.storage_guard.check()

        def add(state):
            jobs = state.setdefault("processingJobs", [])
            result = {"added": 0, "blocked": 0, "duplicates": 0}
            for identifier in dict.fromkeys(ids):
                asset = find(state["assets"], identifier)
                if not asset or asset.get("status") != "downloaded" or (
                    "motion" in options and asset.get("kind") != "video"
                ) or (
                    options.get("method") == "chunlou-glyph"
                    and (asset.get("kind") != "video" or asset.get("siteId") != "chunlou")
                ):
                    result["blocked"] += 1
                    continue
                try:
                    safe_file(self.data_dir, asset.get("_path", ""))
                except FileNotFoundError:
                    result["blocked"] += 1
                    continue
                matching_jobs = any(
                    job["assetId"] == identifier
                    and job["options"] == options
                    and (
                        job["status"] in {"queued", "running", "paused"} or job["id"] in self.active
                    )
                    for job in jobs
                )
                if matching_jobs or self._has_result(asset, options):
                    result["duplicates"] += 1
                    continue
                jobs.append(
                    {
                        "id": new_id("process"),
                        "assetId": identifier,
                        "type": "process",
                        "status": "queued",
                        "progress": 0,
                        "phase": "queued",
                        "options": options,
                        "createdAt": timestamp(),
                        "versionId": None,
                    }
                )
                result["added"] += 1
            if result["added"]:
                record(state, f"已加入 {result['added']} 个 CPU 处理任务", "process")
            return result

        return self.store.mutate(add)

    async def delete_job(self, identifier):
        while True:
            if self.maintenance:
                raise Conflict("备份维护期间不能修改处理任务")
            active = self.active.get(identifier)
            if not active:
                break
            control, worker = active
            control.cancelled = True
            await asyncio.shield(worker)

        def remove(state):
            jobs = state["processingJobs"]
            job = find(jobs, identifier)
            if job is None:
                return {"id": identifier, "deleted": False}
            if job["status"] != "completed":
                try:
                    self._cleanup_attempt(job, state=state)
                except OSError:
                    raise Conflict("无法清理任务的临时文件，请检查目录权限后重试") from None
            jobs.remove(job)
            return {"id": identifier, "deleted": True}

        return self.store.mutate(remove)

    def job_action(self, identifier, action):
        if self.maintenance:
            raise Conflict("备份维护期间不能修改处理任务")
        allowed = {
            "pause": {"queued", "running"},
            "resume": {"paused"},
            "retry": {"failed", "cancelled"},
            "cancel": {"queued", "running", "paused", "failed"},
        }
        if action not in allowed:
            raise ValueError("不支持的处理动作")

        def change(state):
            job = find(state.setdefault("processingJobs", []), identifier)
            if not job:
                raise KeyError(identifier)
            if job["status"] not in allowed[action]:
                raise Conflict("当前处理状态不支持此操作")
            if action in {"resume", "retry"}:
                if identifier in self.active:
                    raise Conflict("处理任务正在停止，请稍后继续")
                if self._has_result(find(state["assets"], job["assetId"]), job["options"]):
                    raise Conflict("相同参数的成品已存在，无需重复处理历史任务")
                if any(
                    other["id"] != identifier
                    and other["assetId"] == job["assetId"]
                    and other["options"] == job["options"]
                    and other["status"] in {"queued", "running", "paused"}
                    for other in state["processingJobs"]
                ):
                    raise Conflict("相同参数已有处理任务，请继续现有任务")
            if identifier in self.active:
                control = self.active[identifier][0]
                if action == "pause":
                    control.paused = True
                if action == "cancel":
                    control.cancelled = True
            status = {
                "pause": "paused",
                "resume": "queued",
                "retry": "queued",
                "cancel": "cancelled",
            }[action]
            job.update(status=status, phase=status, error="", failureCode="")
            return dict(job)

        result = self.store.mutate(change)
        if action == "cancel" and identifier not in self.active:
            self._cleanup_attempt(result)
        return result

    async def _schedule(self):
        while not self._closing:
            if self.monitor:
                self.monitor.beat("processing")
            if not self.active and not self.maintenance:
                job = next(
                    (
                        item
                        for item in self.store.read().get("processingJobs", [])
                        if item["status"] == "queued"
                    ),
                    None,
                )
                if job:
                    control = Control()
                    task = asyncio.create_task(self._process(job["id"], control))
                    self.active[job["id"]] = (control, task)
            await asyncio.sleep(0.2)

    def _update(self, identifier, **values):
        def update(state):
            job = find(state["processingJobs"], identifier)
            if job:
                job.update(values)

        self.store.mutate(update)

    def _cleanup_attempt(self, job, *, state=None):
        version_id, asset_id = job.get("versionId"), job["assetId"]
        if not version_id:
            return
        if not all(
            re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in (version_id, asset_id)
        ):
            raise ValueError("处理版本路径无效")
        if state is None:
            state = self.store.read()
        if any(
            version.get("id") == version_id
            for asset in state["assets"]
            for version in asset.get("versions", [])
        ):
            return
        folder = self.data_dir / "assets" / asset_id / "versions" / version_id
        if not folder.resolve().is_relative_to(self.data_dir):
            raise ValueError("处理版本路径越界")
        if any(path.is_symlink() for path in (folder, *folder.parents) if path != self.data_dir):
            raise ValueError("处理版本不能使用符号链接")
        if folder.exists():
            shutil.rmtree(folder)

    async def _process(self, identifier, control):
        job = find(self.store.read()["processingJobs"], identifier)
        try:
            control.check()
            if self.storage_guard:
                self.storage_guard.check()
            self._cleanup_attempt(job)
            version_id = new_id("version")
            job.update(versionId=version_id)
            self._update(
                identifier,
                status="running",
                phase="processing",
                versionId=version_id,
                progress=0,
                startedAt=timestamp(),
                error="",
                failureCode="",
            )
            asset = find(self.store.read()["assets"], job["assetId"])
            if not asset or asset.get("status") != "downloaded":
                raise ValueError("原始文件尚未下载")
            safe_file(self.data_dir, asset.get("_path", ""))
            last_write = 0

            async def progress(values):
                nonlocal last_write
                control.check()
                if self.storage_guard:
                    self.storage_guard.check()
                if time.monotonic() - last_write >= 0.5:
                    allowed = {
                        key: value
                        for key, value in values.items()
                        if key in {"progress", "phase", "stage", "processedSeconds"}
                    }
                    self._update(identifier, **allowed)
                    last_write = time.monotonic()

            result = await self.processor.process(
                asset, version_id, job["options"], control, progress
            )
            control.check()
            path = safe_file(self.data_dir, result["path"])
            expected = self.data_dir / "assets" / asset["id"] / "versions" / version_id
            if path.parent != expected:
                raise ValueError("处理输出不属于当前版本")

            def complete(state):
                control.check()
                current_job = find(state["processingJobs"], identifier)
                if current_job["status"] != "running":
                    raise DownloadPaused()
                current_asset = find(state["assets"], asset["id"])
                labels = {"add": "加水印", "remove": "固定区域去水印", "both": "去水印并加水印"}
                if job["options"].get("motion"):
                    labels.update(remove="移动区域去水印", both="移动区域去水印并加水印")
                if job["options"].get("method") == "chunlou-glyph":
                    labels["remove"] = "春楼飘字精修" if job["options"].get("revision") == 2 else "春楼飘字修复"
                version = {
                    "id": version_id,
                    "kind": "processed",
                    "status": "completed",
                    "label": labels[job["options"]["mode"]],
                    "createdAt": timestamp(),
                    "_path": result["path"],
                    "fileUrl": f"/api/assets/{asset['id']}/versions/{version_id}/file",
                    "sizeBytes": result["size"],
                    "options": job["options"],
                    **{
                        key: result[key]
                        for key in (
                            "sha256",
                            "sourceSha256",
                            "mime",
                            "width",
                            "height",
                            "duration",
                            "audio",
                            "audioTracks",
                            "processor",
                            "removalMethod",
                            "glyphFrames",
                            "totalFrames",
                        )
                        if key in result
                    },
                }
                current_asset.setdefault("versions", []).append(version)
                current_asset["processedAt"] = timestamp()
                current_job.update(
                    status="completed",
                    phase="completed",
                    progress=100,
                    finishedAt=timestamp(),
                    error="",
                )
                record(
                    state, f"已生成独立成品：{current_asset.get('title', asset['id'])}", "process"
                )

            self.store.mutate(complete)
        except (Exception, asyncio.CancelledError) as error:
            if control.cancelled or isinstance(error, DownloadCancelled):
                status, code, message = "cancelled", "cancelled", "处理已取消，原件保留"
            elif control.paused or isinstance(error, (DownloadPaused, asyncio.CancelledError)):
                status, code, message = "paused", "interrupted", "处理已暂停；继续时从原件重新处理"
            elif getattr(error, "code", "") == "storage-low":
                status, code, message = "paused", "storage-low", str(error)
            else:
                status, code = "failed", "processing-failed"
                message = (
                    str(error)[:400]
                    if isinstance(error, ValueError)
                    else "处理失败，请检查原件、CPU 处理依赖及磁盘空间后重试"
                )
            self._update(
                identifier,
                status=status,
                phase=status,
                failureCode=code,
                error=message,
                finishedAt=timestamp(),
            )
            with contextlib.suppress(OSError, ValueError):
                self._cleanup_attempt(job)
        finally:
            self.active.pop(identifier, None)
