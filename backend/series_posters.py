"""Server-side series artwork. Existing directories can recover their publisher's poster."""

import asyncio
import hashlib
import io
import time
from urllib.parse import urlsplit

import httpx

from backend.lustflix_directory import directory_route
from backend.series_artwork import artwork_urls
from backend.site_catalog import read_publisher_index


class SeriesPosters:
    def __init__(self, thumbnails):
        self.thumbs = thumbnails
        self.folder = thumbnails.data_dir / "series-posters"
        self._index = {}
        self._index_until = 0
        self._index_lock = asyncio.Lock()
        self._locks = {}
        self._failed = {}
        self._slots = asyncio.Semaphore(3)

    async def _publisher_covers(self, key):
        async with self._index_lock:
            if time.monotonic() >= self._index_until:
                try:
                    index = await read_publisher_index("https://lustflix.tv/", self.thumbs.policy)
                    self._index = {
                        route[1]: show["covers"]
                        for show in index["shows"]
                        if (route := directory_route(show["url"])) and show.get("covers")
                    }
                    self._index_until = time.monotonic() + 600
                except (ValueError, httpx.HTTPError, TimeoutError):
                    self._index_until = time.monotonic() + 30
        return self._index.get(key, [])

    @staticmethod
    def _recorded_covers(row, assets):
        return artwork_urls(row, assets)

    async def sources(self, row, assets):
        urls = self._recorded_covers(row, assets)
        route = directory_route(row["url"])
        if not route and row["catalog"].get("entries"):
            route = directory_route(row["catalog"]["entries"][0]["pageUrl"])
        if not urls and route and route[1] == row["catalog"].get("seriesKey"):
            urls = await self._publisher_covers(route[1])
        return urls

    async def _remote(self, row, urls, destination):
        import warnings

        from PIL import Image

        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Never give a partially-written JPEG to another request or a subsequent process.
        work = destination.with_suffix(".tmp")
        for url in urls[:4]:
            try:
                parts = urlsplit(url)
                if (
                    parts.scheme not in {"http", "https"}
                    or not parts.hostname
                    or parts.username
                    or parts.password
                ):
                    continue
                content = await self.thumbs._fetch(
                    {"id": row["id"], "siteId": row["siteId"], "_thumbnailUrl": url}, public=True
                )
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    await asyncio.to_thread(self.thumbs._image, io.BytesIO(content), work)
                work.replace(destination)
                return True
            except (
                ValueError,
                OSError,
                httpx.HTTPError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ):
                continue
            finally:
                work.unlink(missing_ok=True)
        return False

    async def get(self, row, assets):
        urls = self._recorded_covers(row, assets)
        # Hash both identity and known source paths; rotating a signed query alone does
        # not invalidate an existing rendering or put the token on disk.
        paths = [url.split("?", 1)[0] for url in urls]
        key = hashlib.sha256("\0".join([row["id"], *paths]).encode()).hexdigest()
        destination = self.folder / (key + ".jpg")
        if destination.is_file():
            return destination
        async with self._locks.setdefault(key, asyncio.Lock()), self._slots:
            if destination.is_file():
                return destination
            if self._failed.get(key, 0) <= time.monotonic():
                route = directory_route(row["url"])
                if not route and row["catalog"].get("entries"):
                    # Legacy directories may store /watch/<show> without an episode number.
                    route = directory_route(row["catalog"]["entries"][0]["pageUrl"])
                if not urls and route and route[1] == row["catalog"].get("seriesKey"):
                    urls = await self._publisher_covers(route[1])
                if urls:
                    try:
                        async with asyncio.timeout(20):
                            if await self._remote(row, urls, destination):
                                return destination
                    except TimeoutError:
                        pass
                self._failed[key] = time.monotonic() + 60
            local = {
                a["id"]: a
                for a in assets
                if a.get("_path") and a.get("siteId") == row["siteId"] and a.get("kind") == "video"
            }
            for entry in row["catalog"].get("entries", []):
                for identifier in [entry.get("downloadAssetId"), *entry.get("assetIds", [])]:
                    if identifier in local:
                        return await self.thumbs.get(local[identifier])
        return None
