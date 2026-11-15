"""Local workspace ownership, storage headroom and inexpensive runtime diagnostics."""

import asyncio
import importlib.util
import os
import re
import shutil
import stat
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class WorkspaceBusy(ValueError):
    """The workspace already belongs to a running service or maintenance command."""


class WorkspaceLock:
    filename = ".workspace.lock"

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self._fd = None
        self._pid = None

    @property
    def held(self):
        return self._fd is not None and self._pid == os.getpid()

    def acquire(self):
        if self.held:
            return self
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = None
        try:
            lock_path = self.data_dir / self.filename
            if lock_path.is_symlink() or lock_path.is_junction():
                raise ValueError("工作区锁文件不能是链接")
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("工作区锁文件无效")
            if os.name == "nt":
                # Lock a stable byte; the OS releases it when the owner exits.
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise BlockingIOError() from None
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.fchmod(descriptor, 0o600)
            self._fd, self._pid = descriptor, os.getpid()
            return self
        except BlockingIOError:
            if descriptor is not None:
                os.close(descriptor)
            raise WorkspaceBusy("工作区正在被其他服务或维护任务使用，请先停止该服务") from None
        except (OSError, ValueError):
            if descriptor is not None:
                os.close(descriptor)
            raise ValueError("工作区锁文件不可用，请检查目录权限及符号链接") from None

    def release(self):
        descriptor, self._fd = self._fd, None
        if descriptor is not None:
            if self._pid == os.getpid():
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self._pid = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


class StoragePressure(ValueError):
    code = "storage-low"


class StorageGuard:
    def __init__(self, data_dir, min_free_bytes=512 * 1024**2, *, disk_usage=shutil.disk_usage):
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            raise ValueError("磁盘保留空间必须是非负整数")
        self.data_dir = Path(data_dir).resolve()
        self.min_free_bytes = min_free_bytes
        self._disk_usage = disk_usage

    def snapshot(self):
        try:
            disk = self._disk_usage(self.data_dir)
        except OSError:
            return {
                "totalBytes": 0,
                "usedBytes": 0,
                "freeBytes": 0,
                "minFreeBytes": self.min_free_bytes,
                "availableBytes": 0,
                "healthy": False,
                "code": "storage-unavailable",
            }
        return {
            "totalBytes": disk.total,
            "usedBytes": disk.used,
            "freeBytes": disk.free,
            "minFreeBytes": self.min_free_bytes,
            "availableBytes": max(0, disk.free - self.min_free_bytes),
            "healthy": disk.free >= self.min_free_bytes,
        }

    def check(self, required_bytes=0):
        if type(required_bytes) is not int or required_bytes < 0:
            raise ValueError("所需磁盘空间必须是非负整数")
        disk = self.snapshot()
        if not disk["healthy"] or disk["availableBytes"] < required_bytes:
            raise StoragePressure("磁盘可用空间不足，已停止写入；请释放空间后重试或继续任务")


async def dependency_status():
    """Inspect installed programs once; this does not launch browsers or access sites."""
    chromium = False
    try:
        from playwright.async_api import async_playwright

        async with asyncio.timeout(5), async_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            chromium = executable.is_file() and os.access(executable, os.X_OK)
            # Playwright's --only-shell Linux image intentionally omits full Chromium.
            if not chromium:
                installation = next(
                    (path for path in executable.parents if path.name.startswith("chromium-")),
                    None,
                )
                if installation:
                    chromium = any(
                        path.is_file() and os.access(path, os.X_OK)
                        for path in installation.parent.glob(
                            "chromium_headless_shell-*/chrome-*/chrome-headless-shell"
                        )
                    )
    except Exception:
        chromium = False
    return {
        "ffmpeg": {"available": shutil.which("ffmpeg") is not None},
        "ffprobe": {"available": shutil.which("ffprobe") is not None},
        "chromium": {"available": chromium},
        "pillow": {"available": importlib.util.find_spec("PIL") is not None},
    }


_WORKERS = ("download", "processing", "automation")

_STATUS_KEYS = {
    "queued": "queued",
    "running": "running",
    "paused": "paused",
    "failed": "failed",
    "completed": "completed",
    "needs-user": "needsUser",
    "cancelled": "cancelled",
}
_FAILURE_CODES = {
    "storage-low",
    "storage-unavailable",
    "processing-failed",
    "session-required",
    "access-required",
    "verification-required",
    "media-unavailable",
    "identity-mismatch",
    "ambiguous-media",
    "refresh-failed",
    "repeated-access-error",
    "unsupported-media",
}


def _queue_counts(rows):
    counts = Counter(row.get("status") for row in rows)
    return {value: counts[key] for key, value in _STATUS_KEYS.items()}


def _failure(row, kind):
    identifier = row.get("id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
        identifier = "unknown"
    at = (
        row.get("completedAt")
        or row.get("finishedAt")
        or row.get("updatedAt")
        or row.get("createdAt")
    )
    try:
        datetime.fromisoformat(at)
    except (TypeError, ValueError):
        at = None
    code = row.get("failureCode")
    return {
        "id": identifier,
        "type": kind,
        "status": row.get("status"),
        "code": code if code in _FAILURE_CODES else "task-failed",
        "at": at,
    }


class RuntimeMonitor:
    heartbeat_timeout = 15

    def __init__(self, data_dir, storage, *, dependencies=None, clock=time.monotonic):
        self.data_dir, self.storage = Path(data_dir).resolve(), storage
        self._dependencies = dependencies
        self._clock = clock
        self._started = None
        self._running = False
        self._beats = {}

    async def start(self):
        self._started, self._running = self._clock(), True
        if self._dependencies is None:
            self._dependencies = await dependency_status()

    async def stop(self):
        self._running = False

    def beat(self, name):
        if name not in _WORKERS:
            raise ValueError("未知工作线程")
        self._beats[name] = self._clock()

    def snapshot(self, state, *, workers):
        now = self._clock()
        dependencies = {
            key: {"available": bool((self._dependencies or {}).get(key, {}).get("available"))}
            for key in ("ffmpeg", "ffprobe", "chromium", "pillow")
        }
        database_available = isinstance(state, dict) and all(
            isinstance(state.get(key, []), list)
            and all(isinstance(row, dict) for row in state.get(key, []))
            for key in ("jobs", "processingJobs", "scans")
        )
        state = state if database_available else {}
        jobs = state.get("jobs", [])
        processing = list(state.get("processingJobs", []))
        processing.extend(row for row in jobs if row.get("type") in {"process", "processing"})
        downloads = [row for row in jobs if row.get("type") not in {"process", "processing"}]
        queues = {"download": downloads, "processing": processing, "scans": state.get("scans", [])}
        worker_status = {}
        for name in _WORKERS:
            age = max(0, now - self._beats[name]) if name in self._beats else None
            running = workers.get(name) is True and self._running
            worker_status[name] = {
                "running": running,
                "heartbeatAgeSeconds": round(age, 2) if age is not None else None,
                "healthy": running and age is not None and age <= self.heartbeat_timeout,
            }
        failures = [
            _failure(row, kind)
            for kind, rows in queues.items()
            for row in rows
            if row.get("status") in {"failed", "needs-user"}
        ]
        failures.sort(key=lambda row: row["at"] or "", reverse=True)
        disk = self.storage.snapshot()
        ready = (
            self._running
            and database_available
            and disk["healthy"]
            and all(item["available"] for item in dependencies.values())
            and all(item["healthy"] for item in worker_status.values())
        )
        return {
            "status": "ready" if ready else "degraded",
            "ready": ready,
            "uptimeSeconds": round(max(0, now - self._started), 2)
            if self._started is not None
            else 0,
            "database": {"available": database_available},
            "disk": disk,
            "dependencies": dependencies,
            "workers": worker_status,
            "queues": {kind: _queue_counts(rows) for kind, rows in queues.items()},
            "recentFailures": failures[:10],
        }
