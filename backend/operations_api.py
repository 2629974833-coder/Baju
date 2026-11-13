"""Admin diagnostics and quiescent online snapshots; restore remains offline."""

import asyncio
import contextlib
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.backup import create_backup, list_backups, verify_backup
from backend.store import new_id, record, timestamp
from backend.tasks import Conflict


class MaintenanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationsService:
    def __init__(self, settings, store, lock, tasks, processing, browser, monitor, storage, *, automation=None):
        self.settings, self.store, self.lock = settings, store, lock
        self.tasks, self.processing, self.browser = tasks, processing, browser
        self.monitor, self.storage = monitor, storage
        self.automation = automation
        self.local_auth = None
        self.maintenance = False
        self.writing_requests = 0
        self._backup_task = None

    def snapshot(self):
        try:
            state = self.store.read()
        except Exception:
            state = None
        result = self.monitor.snapshot(
            state,
            workers={
                "automation": bool(self.automation and self.automation.is_running),
                "download": bool(self.tasks._scheduler and not self.tasks._scheduler.done()),
                "processing": bool(
                    self.processing._scheduler and not self.processing._scheduler.done()
                ),
            },
        )
        result["maintenance"] = self.maintenance
        if self.maintenance:
            result.update(ready=False, status="maintenance")
        return result

    async def wait_for_backup(self):
        task = self._backup_task
        if task:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            with contextlib.suppress(Exception, asyncio.CancelledError):
                task.result()

    async def backup(self):
        if self.maintenance:
            raise Conflict("已有备份正在进行")
        state = self.store.read()
        if self.local_auth and any(self.local_auth.info(s["id"]).get("active") for s in state["sites"]):
            raise Conflict("请先保存或关闭本机授权窗口，再创建一致备份")
        pending = [*state["jobs"], *state.get("processingJobs", []), *state["scans"]]
        if (
            any(item["status"] in {"queued", "running"} for item in pending)
            or self.tasks.active
            or self.tasks.scan_tasks
            or self.processing.active
            or self.writing_requests
        ):
            raise Conflict("请先暂停下载和处理、结束扫描及其他写入，再创建一致备份")
        self.storage.check()
        self.maintenance = self.tasks.maintenance = self.processing.maintenance = True
        self._backup_task = asyncio.create_task(self._create_backup(state))
        self._backup_task.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return await asyncio.shield(self._backup_task)

    async def _create_backup(self, state):
        # This service-owned task, not the HTTP request, owns the maintenance gate.
        try:
            if hasattr(self.browser, "session_info"):
                for site in state["sites"]:
                    info = await self.browser.session_info(site["id"])
                    if info.get("open"):
                        await self.browser.save_session(site["id"])

                        def saved(current, identifier=site["id"]):
                            item = next(
                                value for value in current["sites"] if value["id"] == identifier
                            )
                            item.update(sessionSaved=True, updated=timestamp())

                        self.store.mutate(saved)
            output = self.settings.data_dir / "backups" / new_id("backup")
            result = await asyncio.to_thread(
                create_backup,
                self.settings.data_dir,
                output,
                workspace_lock=self.lock,
                storage_guard=self.storage,
            )
            self.store.mutate(lambda current: record(current, "已创建并校验工作区备份", "backup"))
            return result
        finally:
            self._backup_task = None
            self.maintenance = self.tasks.maintenance = self.processing.maintenance = False

    def router(self):
        router = APIRouter()

        @router.get("/api/operations")
        async def operations():
            return self.snapshot()

        @router.get("/api/ready")
        async def ready():
            result = self.snapshot()
            return JSONResponse(result, status_code=200 if result["ready"] else 503)

        @router.get("/api/maintenance/backups")
        async def backups():
            return {"backups": await asyncio.to_thread(list_backups, self.settings.data_dir)}

        @router.post("/api/maintenance/backups")
        async def backup(_data: MaintenanceInput):
            return await self.backup()

        @router.post("/api/maintenance/backups/{identifier}/verify")
        async def verify(identifier: str, _data: MaintenanceInput):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
                raise HTTPException(404, "备份不存在")
            folder = self.settings.data_dir / "backups" / identifier
            if not folder.is_dir() or folder.is_symlink():
                raise HTTPException(404, "备份不存在")
            return await asyncio.to_thread(verify_backup, folder)

        return router
