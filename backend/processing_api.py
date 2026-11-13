"""Processing and immutable original/finished-version file endpoints."""

from collections import defaultdict
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.archive import SeriesArchiveResponse, safe_component, unique_name
from backend.series import series_library
from backend.tasks import Conflict, find, safe_file


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessInput(_Input):
    ids: list[str] = Field(min_length=1, max_length=1000)
    options: dict = Field(default_factory=dict)


class ProcessAction(_Input):
    action: str


class LibraryArchiveInput(_Input):
    ids: list[str] = Field(min_length=1, max_length=1000)
    variant: str = "latest"


def _library_series_folders(state, assets):
    """Use the same primary-before-candidate episode membership as the library view."""
    videos = {a["id"]: a["siteId"] for a in assets if a["kind"] == "video"}
    if not videos:
        return {}, "Framekeep"
    primary, related = defaultdict(list), defaultdict(list)
    for series in series_library(state):
        owner = (series["id"], series["title"])
        for entry in series["catalog"]["entries"]:
            chosen = entry.get("downloadAssetId")
            for identifier in set(entry.get("assetIds", ())) | {chosen}:
                if identifier in videos and videos[identifier] == series["siteId"]:
                    matches = primary if identifier == chosen else related
                    matches[identifier].append(owner)
    owners = {}
    for identifier in videos:
        matches = primary.get(identifier) or related.get(identifier, ())
        if len(matches) == 1:
            owners[identifier] = matches[0]

    has_other = len(owners) < len(assets)
    groups = set(owners.values())
    top_count = len(groups) + int(has_other)
    reserved = {"manifest.json": 1} if top_count > 1 else {}
    if has_other:
        reserved["framekeep"] = 1
    names = {
        owner: unique_name(safe_component(owner[1], "剧集"), "", reserved)
        for owner in sorted(groups)
    }
    manifest_folder = next(iter(names.values()), "Framekeep") if top_count == 1 else ""
    return {key: names[owner] for key, owner in owners.items()}, manifest_folder


def create_processing_router(store, queue, settings):
    router = APIRouter()

    def asset_for(identifier):
        asset = find(store.read()["assets"], identifier)
        if not asset:
            raise HTTPException(404, "找不到素材")
        return asset

    @router.post("/api/processes")
    async def process(data: ProcessInput):
        return queue.enqueue(data.ids, data.options)

    @router.post("/api/processes/{identifier}/action")
    async def action(identifier: str, data: ProcessAction):
        if data.action == "delete":
            return await queue.delete_job(identifier)
        try:
            return queue.job_action(identifier, data.action)
        except KeyError:
            raise HTTPException(404, "找不到处理任务") from None

    @router.get("/api/assets/{identifier}/versions/{version_id}/file")
    async def version_file(identifier: str, version_id: str, download: bool = False):
        asset = asset_for(identifier)
        version = asset if version_id == "original" else find(asset.get("versions", []), version_id)
        if not version or (version_id != "original" and version.get("status") != "completed"):
            raise HTTPException(404, "处理版本不存在或尚未完成")
        try:
            path = safe_file(settings.data_dir, version.get("_path", ""))
        except FileNotFoundError:
            raise HTTPException(404, "版本文件已被移动或不存在") from None
        name = safe_component(asset.get("title")) + "_" + safe_component(version_id) + path.suffix
        return FileResponse(
            path,
            media_type=version.get("mime", "application/octet-stream"),
            filename=name if download else None,
        )

    @router.post("/api/library/archive-file")
    async def archive_file(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "请从资料库导出")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4 * 1024 * 1024:
                raise HTTPException(413, "所选素材过多，请分批导出")
            body.extend(chunk)
        try:
            fields = parse_qs(body.decode(), strict_parsing=True, max_num_fields=1)
            data = LibraryArchiveInput.model_validate_json(fields["payload"][0])
        except (KeyError, IndexError, ValueError):
            raise HTTPException(422, "导出选项无效，请刷新资料库后重试") from None
        return await archive(data)

    @router.post("/api/library/archive")
    async def archive(data: LibraryArchiveInput):
        if data.variant not in {"latest", "original"}:
            raise ValueError("导出范围仅支持最新成品或原件")
        state = store.read()
        saved, items = [], []
        for identifier in dict.fromkeys(data.ids):
            asset = find(state["assets"], identifier)
            if not asset:
                raise ValueError("只能导出素材库中的已知素材")
            version = asset
            version_id = "original"
            if data.variant == "latest":
                versions = [
                    item
                    for item in asset.get("versions", [])
                    if item.get("kind") == "processed" and item.get("status") == "completed"
                ]
                if not versions:
                    items.append({"assetId": identifier, "state": "no-processed-version"})
                    continue
                version = versions[-1]
                version_id = version["id"]
            try:
                path = safe_file(settings.data_dir, version.get("_path", ""))
            except FileNotFoundError:
                items.append(
                    {"assetId": identifier, "versionId": version_id, "state": "missing-file"}
                )
                continue
            item = {
                "assetId": identifier,
                "versionId": version_id,
                "state": "saved",
                "sha256": version.get("sha256"),
                "bytes": path.stat().st_size,
            }
            items.append(item)
            saved.append((asset, path, item))
        if not saved:
            raise Conflict("所选范围没有可导出的文件，请先完成处理或选择原件")

        folders, manifest_folder = _library_series_folders(state, [asset for asset, _, _ in saved])
        files, used_names = [], {}
        for asset, path, item in saved:
            folder = folders.get(asset["id"])
            if folder is None:
                site = find(state["sites"], asset["siteId"]) or {}
                category = "图片" if asset["kind"] == "image" else "视频"
                folder = "/".join(["Framekeep", safe_component(site.get("name"), "site"), category])
            stem = "_".join(
                [
                    safe_component(asset.get("title"))[:60],
                    safe_component(asset["id"])[-16:],
                    safe_component(item["versionId"])[-16:],
                ]
            )
            item["file"] = unique_name(f"{folder}/{stem}", path.suffix, used_names)
            files.append({"path": path, "name": item["file"]})
        plan = {
            "folder": manifest_folder,
            "name": "Framekeep-" + data.variant + ".zip",
            "files": files,
            "manifest": {"version": 1, "variant": data.variant, "items": items},
        }
        return SeriesArchiveResponse(
            plan,
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(plan["name"])},
        )

    return router
