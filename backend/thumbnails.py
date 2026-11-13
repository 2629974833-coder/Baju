"""Bounded, authenticated thumbnail proxy; remote media URLs never load in the panel."""

import asyncio
import contextlib
import io
import time

import httpx
from PIL import Image

from backend.tasks import safe_file


class ThumbnailService:
    def __init__(self, data_dir, browser, policy):
        self.data_dir, self.browser, self.policy = data_dir, browser, policy
        self._locks = {}
        self._failed = {}
        self._slots = asyncio.Semaphore(3)

    async def get(self, asset):
        identifier = asset["id"]
        folder = self.data_dir / "assets" / identifier
        destination = folder / "thumbnail.jpg"
        if destination.is_file():
            return destination
        if self._failed.get(identifier, 0) > time.monotonic():
            return None
        async with self._locks.setdefault(identifier, asyncio.Lock()), self._slots:
            if destination.is_file():
                return destination
            folder.mkdir(parents=True, exist_ok=True)
            try:
                async with asyncio.timeout(20):
                    if asset.get("_path"):
                        path = safe_file(self.data_dir, asset["_path"])
                        if asset["kind"] == "image":
                            await asyncio.to_thread(self._image, path, destination)
                        else:
                            process = await asyncio.create_subprocess_exec(
                                "ffmpeg",
                                "-nostdin",
                                "-v",
                                "error",
                                "-i",
                                str(path),
                                "-frames:v",
                                "1",
                                "-vf",
                                "scale=480:-2",
                                "-y",
                                str(destination),
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            try:
                                if await process.wait() != 0:
                                    raise ValueError("缩略图生成失败")
                            finally:
                                if process.returncode is None:
                                    process.kill()
                                    await process.wait()
                    elif asset.get("_thumbnailUrl"):
                        content = await self._fetch(asset)
                        await asyncio.to_thread(self._image, io.BytesIO(content), destination)
                    else:
                        return None
                return destination if destination.is_file() else None
            except (Exception, asyncio.CancelledError):
                destination.unlink(missing_ok=True)
                self._failed[identifier] = time.monotonic() + 30
                return None

    @staticmethod
    def _image(source, destination):
        with Image.open(source) as image:
            if image.width * image.height > 50_000_000:
                raise ValueError("图片尺寸超限")
            image.thumbnail((480, 480))
            image.convert("RGB").save(destination, "JPEG", quality=78)

    async def _fetch(self, asset, *, public=False):
        url = asset["_thumbnailUrl"]
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=False,
            trust_env=False,
            proxy=getattr(self.policy, "proxy_url", None),
        ) as client:
            for _ in range(6):
                url = await self.policy.validate(url)
                headers = {
                    key: value
                    for key, value in asset.get("_headers", {}).items()
                    if key.lower() in {"referer", "user-agent"}
                }
                # Clear the client's redirect cookie jar: only this site's browser session
                # decides which cookies belong to each target URL.
                client.cookies.clear()
                if not public:
                    with contextlib.suppress(ValueError):
                        cookies = await self.browser.cookies_for(asset["siteId"], url)
                        if cookies:
                            headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers["location"]))
                        continue
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").startswith("image/"):
                        raise ValueError("非图片响应")
                    chunks, count = [], 0
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > 8 * 1024**2:
                            raise ValueError("缩略图文件过大")
                        chunks.append(chunk)
                    return b"".join(chunks)
            raise ValueError("重定向次数过多")
