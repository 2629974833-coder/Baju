"""Named copies for export; original asset paths are never renamed or overwritten."""

import io
import json
import re
import threading
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

import anyio
from starlette.responses import StreamingResponse

from backend.series import series_identifier
from backend.series_artwork import artwork_assets, artwork_urls
from backend.site_adapters import build_catalog, select_episode_asset, update_catalog
from backend.tasks import safe_file

_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
_CHUNK_BYTES = 64 * 1024


def safe_component(value, fallback="untitled"):
    value = unicodedata.normalize("NFC", str(value or ""))
    value = "".join(
        "_" if char in '/\\:*?"<>|' or unicodedata.category(char).startswith("C") else char
        for char in value
    )
    value = re.sub(r"\s+", " ", value).strip(" ._")
    value = value.encode("utf-8")[:120].decode("utf-8", errors="ignore").strip(" .")
    if not value:
        value = fallback
    return "_" + value if _RESERVED.match(value) else value


def unique_name(stem, suffix, used):
    name = stem + suffix
    serial = used.get(name.casefold(), 0)
    while name.casefold() in used:
        serial += 1
        name = f"{stem} ({serial}){suffix}"
    used[(stem + suffix).casefold()] = max(1, serial)
    used[name.casefold()] = 1
    return name


def download_filename(asset, site, entry=None, catalog=None):
    extension = Path(asset.get("_path", "")).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        extension = ".mp4" if asset.get("kind") == "video" else ".bin"
    observed = build_catalog(asset.get("pageUrl", ""), [{"url": asset.get("pageUrl", "")}])
    catalog = catalog or observed
    if entry is None and observed.get("entries"):
        entry = observed["entries"][0]
    prefix = [safe_component(site.get("name") or site.get("id"), "site")]
    if catalog.get("seriesKey") and entry and type(entry.get("ordinal")) is int:
        prefix.append(safe_component(catalog.get("title") or catalog["seriesKey"]))
        suffix = [f"E{entry['ordinal']:03d}"]
    else:
        prefix.append(safe_component(asset.get("title")))
        suffix = [safe_component(asset.get("id"))[-16:]]
    height = asset.get("height")
    if asset.get("kind") == "video" and type(height) is int and 0 < height < 100000:
        suffix.append(f"{height}p")
    tail = "_" + "_".join(suffix) + extension
    budget = max(1, 240 - len(tail.encode("utf-8")))
    stem = "_".join(prefix).encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return stem + tail


def prepare_series_archive(catalog, assets, site, data_dir, *, resolved_catalog=False):
    assets = [asset for asset in assets if asset.get("siteId") == site["id"]]
    if not resolved_catalog:
        catalog = update_catalog(catalog, assets)
    folder = safe_component(catalog.get("title") or catalog.get("seriesKey"), "series")
    by_id = {asset["id"]: asset for asset in assets}
    files, entries, names = [], [], set()
    for entry in catalog.get("entries", []):
        item = {
            "episodeKey": entry["episodeKey"],
            "ordinal": entry["ordinal"],
            "state": entry.get("mediaStatus", "unresolved"),
        }
        # A local original remains exportable when the remote account expires.
        selected = select_episode_asset({**entry, "access": "unknown"}, assets)
        asset = by_id.get(selected["assetId"])
        if asset and selected["state"] == "downloaded":
            try:
                path = safe_file(data_dir, asset.get("_path", ""))
            except FileNotFoundError:
                item["state"] = "missing-file"
            else:
                name = download_filename(asset, site, entry, catalog)
                if name.casefold() in names:
                    name = f"{Path(name).stem}_{safe_component(asset['id'])[-16:]}{path.suffix}"
                names.add(name.casefold())
                archive_path = folder + "/" + name
                files.append({"path": path, "name": archive_path})
                item.update(
                    state="downloaded",
                    file=archive_path,
                    sha256=asset.get("sha256"),
                    bytes=path.stat().st_size,
                )
        entries.append(item)
    saved_episodes = len(files)
    row = {"siteId": site["id"], "url": catalog.get("catalogUrl") or "", "catalog": catalog}
    row["id"] = series_identifier(row)
    pictures = artwork_assets(row, assets)
    artwork = []
    for ordinal, url in enumerate(artwork_urls(row, assets), 1):
        item = {"ordinal": ordinal, "state": "not-downloaded"}
        asset = next((a for a in pictures if a.get("sourceUrl") == url
                      and a.get("status") == "downloaded" and a.get("_path")), None)
        if asset:
            try:
                path = safe_file(data_dir, asset["_path"])
            except FileNotFoundError:
                item["state"] = "missing-file"
            else:
                suffix = path.suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                    suffix = ".bin"
                stem = "海报" if ordinal == 1 else f"海报_{ordinal:02d}"
                name = stem + suffix
                if name.casefold() in names:
                    name = f"{stem}_{safe_component(asset['id'])[-16:]}{suffix}"
                names.add(name.casefold())
                archive_path = folder + "/" + name
                files.append({"path": path, "name": archive_path})
                item.update(state="downloaded", file=archive_path, bytes=path.stat().st_size,
                            sha256=asset.get("sha256"), assetId=asset["id"])
        artwork.append(item)
    if not artwork:
        artwork.append({"state": "unavailable", "message": "站点尚未提供此剧海报"})
    manifest = {
        "version": 1,
        "site": site.get("name"),
        "seriesKey": catalog.get("seriesKey"),
        "title": catalog.get("title", ""),
        "expectedCount": catalog.get("expectedCount"),
        "enumeratedCount": len(entries),
        "savedCount": saved_episodes,
        "allSaved": catalog.get("status") == "complete-observed" and saved_episodes == len(entries),
        "artwork": artwork,
        "savedArtworkCount": sum(item["state"] == "downloaded" for item in artwork),
        "allArtworkSaved": all(item["state"] == "downloaded" for item in artwork),
        "missingEpisodeKeys": catalog.get("missingEpisodeKeys", []),
        "observedGapOrdinals": catalog.get("observedGapOrdinals", []),
        "reasons": catalog.get("reasons", []),
        "entries": entries,
    }
    if catalog.get("dismissedEpisodeCount"):
        manifest["dismissedEpisodeCount"] = catalog["dismissedEpisodeCount"]
    return {
        "name": folder + ".zip",
        "folder": folder,
        "manifest": manifest,
        "files": files,
    }


class _ZipSink(io.RawIOBase):
    """Unseekable bounded output, allowing zipfile data descriptors per member."""

    def __init__(self):
        self.buffer = bytearray()
        self.position = 0

    def writable(self):
        return True

    def tell(self):
        return self.position

    def write(self, value):
        self.buffer.extend(value)
        self.position += len(value)
        return len(value)

    def drain(self):
        while self.buffer:
            block = bytes(self.buffer[:_CHUNK_BYTES])
            del self.buffer[:_CHUNK_BYTES]
            yield block


def stream_series_archive(plan):
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        manifest = json.dumps(plan["manifest"], ensure_ascii=False, indent=2).encode("utf-8")
        manifest_path = str(PurePosixPath(plan["folder"]) / "manifest.json")
        with archive.open(manifest_path, "w", force_zip64=True) as member:
            for start in range(0, len(manifest), _CHUNK_BYTES):
                member.write(manifest[start : start + _CHUNK_BYTES])
                yield from sink.drain()
        yield from sink.drain()
        for item in plan["files"]:
            with (
                item["path"].open("rb") as source,
                archive.open(item["name"], "w", force_zip64=True) as member,
            ):
                while block := source.read(_CHUNK_BYTES):
                    member.write(block)
                    yield from sink.drain()
            yield from sink.drain()
    yield from sink.drain()


class _ArchiveIterator:
    """Serialize closing with an in-flight file read in Starlette's worker pool."""

    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            return next(self.stream)

    def close(self):
        with self.lock:
            self.stream.close()


class SeriesArchiveResponse(StreamingResponse):
    """Close the open source file even when a client leaves between body chunks."""

    def __init__(self, plan, *, headers=None):
        self._archive_stream = _ArchiveIterator(stream_series_archive(plan))
        super().__init__(self._archive_stream, media_type="application/zip", headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._archive_stream.close)
