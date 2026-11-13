"""Process-wide admission slots shared by otherwise isolated workspaces."""

import asyncio
from contextlib import asynccontextmanager


class ResourceLimits:
    """Create once per server process and inject the same object into all queues."""

    def __init__(self, *, downloads=1, scans=1):
        if downloads < 1 or scans < 1:
            raise ValueError("任务并发上限必须大于零")
        self._slots = {
            "download": asyncio.Semaphore(downloads),
            "scan": asyncio.Semaphore(scans),
        }

    @asynccontextmanager
    async def slot(self, kind, *, check=None):
        semaphore = self._slots[kind]
        if check:
            check()
        acquisition = asyncio.create_task(semaphore.acquire())
        try:
            # Retain one waiter so polling cooperative controls preserves FIFO order.
            while not acquisition.done():
                if check:
                    check()
                await asyncio.wait({acquisition}, timeout=0.1)
            await acquisition
            if check:
                check()
            yield
        finally:
            if not acquisition.done():
                acquisition.cancel()
            await asyncio.gather(acquisition, return_exceptions=True)
            # Cancellation can race with a released slot before the caller resumes.
            if not acquisition.cancelled() and acquisition.exception() is None:
                semaphore.release()
