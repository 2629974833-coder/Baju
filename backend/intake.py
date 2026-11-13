"""Views and named exports for the assets observed by a single user submission."""

from collections import defaultdict
from urllib.parse import urlsplit

from fastapi import HTTPException

from backend.archive import safe_component, unique_name
from backend.series import series_identifier, series_titles
from backend.tasks import Conflict, find, safe_file


def intake_scan(state, identifier):
    scan = find(state["scans"], identifier)
    if not scan or not scan.get("intakeScope"):
        raise HTTPException(404, "找不到这次拾取记录")
    return scan


def intake_results(scan, state):
    scope = scan["intakeScope"]
    start = scan.get("requestedUrl") or scan["url"]
    refs = scan.get("_intakeRefs", {})
    index = scan.get("siteCatalog") or {}
    if scope == "site":
        shows = index.get("shows", [])
    else:
        catalog = (scan.get("catalog") or {}) if scope == "series" else {}
        title = (
            scan.get("_suggestedTitle")
            or catalog.get("title")
            or scan.get("workTitle")
            or ("当前单集或作品" if scope == "single" else "当前剧集")
        )
        shows = [{"url": catalog.get("catalogUrl") or start, "title": title, "catalog": catalog}]
    titles = series_titles(state) if scope != "single" else {}
    saved_by_page = defaultdict(list)
    for asset in state["assets"]:
        if (
            asset.get("siteId") == scan["siteId"]
            and asset.get("status") == "downloaded"
            and asset.get("_path")
            and asset.get("pageUrl")
            and asset.get("kind") in scan.get("intakeKinds", ["image", "video"])
        ):
            saved_by_page[asset["pageUrl"]].append(asset["id"])
    groups = []
    for show in shows:
        url = show["url"]
        catalog = show.get("catalog") or {}
        episodes = []
        show_assets = {}
        pages = defaultdict(list)
        for entry in catalog.get("entries", []):
            episode = {
                "key": entry["episodeKey"],
                "title": entry.get("title", ""),
                "ordinal": entry.get("ordinal"),
                "url": entry["pageUrl"],
                "access": entry.get("access", "unknown"),
                "assetIds": [],
            }
            for field in ("mediaStatus", "scanMessage", "accessEvidence", "scanStatus"):
                if entry.get(field):
                    episode[field] = entry[field]
            episodes.append(episode)
            pages[entry["pageUrl"]].append(episode)
        # Single-show submissions own every observation. A site submission only
        # owns a show's media when the source supplied that particular association.
        observations = [refs.get(url, {})] if scope == "site" else refs.values()
        for observed_pages in observations:
            for page, ids in observed_pages.items():
                targets = pages.get(page)
                if targets:
                    for episode in targets:
                        episode["assetIds"] = list(dict.fromkeys([*episode["assetIds"], *ids]))
                else:
                    show_assets.update(dict.fromkeys(ids))
        known_pages = {url}
        if scope == "single":
            known_pages.add(scan["url"])
        local_ids = set()
        for page in known_pages - pages.keys():
            for identifier in saved_by_page.get(page, []):
                if identifier not in show_assets:
                    show_assets[identifier] = None
                    local_ids.add(identifier)
        for episode in episodes:
            for identifier in saved_by_page.get(episode["url"], []):
                if identifier not in episode["assetIds"]:
                    episode["assetIds"].append(identifier)
                    local_ids.add(identifier)
        categories = show.get("categories") or show.get("category") or []
        if isinstance(categories, str):
            categories = [categories]
        identifier = series_identifier({"siteId": scan["siteId"], "url": url, "catalog": catalog})
        title = titles.get(identifier) or show.get("title") or url
        groups.append(
            {
                "key": url,
                "url": url,
                "title": title,
                "category": "、".join(categories),
                "categories": categories,
                "assetIds": list(show_assets),
                "episodes": episodes,
                "status": show.get("status", scan["status"]),
                "message": show.get("message", ""),
                "expectedCount": catalog.get("expectedCount"),
                "localOnlyIds": sorted(local_ids),
            }
        )
    return {
        "scanId": scan["id"],
        "scope": scope,
        "url": start,
        "status": scan["status"],
        "message": scan.get("error") or scan.get("message", ""),
        "downloadAfterScan": scan.get("downloadAfterScan") is True,
        "downloadSummary": scan.get("downloadSummary"),
        "groups": groups,
        "limited": bool(
            index.get("limited")
            or scan.get("limited")
            or (scan.get("catalog") or {}).get("truncated")
        ),
        "reasons": index.get("reasons", []),
        "pagesVisited": scan.get("pagesVisited", 0),
        "runPagesVisited": scan.get("runPagesVisited", scan.get("pagesVisited", 0)),
    }


def _archive_sections(group):
    yield None, "剧集素材", group["assetIds"]
    used = {}
    for episode in group["episodes"]:
        ordinal = episode["ordinal"]
        label = f"E{ordinal:03d}" if type(ordinal) is int else "单集"
        if episode["title"]:
            label += " " + episode["title"]
        yield episode["key"], unique_name(safe_component(label), "", used), episode["assetIds"]


def intake_archive(state, scan, identifiers, data_dir, group_key=None, episode_key=None):
    groups = intake_results(scan, state)["groups"]
    selected = set(identifiers)
    assets = {
        a["id"]: a
        for a in state["assets"]
        if a.get("siteId") == scan["siteId"] and a["id"] in selected
    }
    saved_groups, items, matched = [], [], set()
    for group in groups:
        if group_key is not None and group_key != group["key"]:
            continue
        members = []
        used_names = {}
        for key, label, ids in _archive_sections(group):
            if episode_key is not None and key != episode_key:
                continue
            for identifier in ids:
                if identifier not in selected:
                    continue
                matched.add(identifier)
                asset = assets.get(identifier)
                item = {
                    "assetId": identifier,
                    "group": group["title"],
                    "groupKey": group["key"],
                    "episodeKey": key,
                }
                items.append(item)
                if not asset or asset.get("status") != "downloaded":
                    item["status"] = "not-saved"
                    continue
                try:
                    path = safe_file(data_dir, asset.get("_path", ""))
                except FileNotFoundError:
                    item["status"] = "missing-file"
                    continue
                kind = "视频" if asset["kind"] == "video" else "图片"
                stem = safe_component(asset.get("title") or kind, kind)
                suffix = safe_component(path.suffix.lstrip("."), "bin")
                name = unique_name(f"{label}/{kind}/{stem}", "." + suffix, used_names)
                members.append((path, name, item))
                item.update(status="saved", sha256=asset.get("sha256"), bytes=path.stat().st_size)
        if members:
            saved_groups.append((safe_component(group["title"], "剧集"), members))
    if selected != matched:
        raise ValueError("所选素材不属于这次拾取或所选剧集，请刷新后再导出")
    if not saved_groups:
        raise Conflict("所选素材还没有可导出的文件，请先下载")
    folder = saved_groups[0][0] if len(saved_groups) == 1 else ""
    used_titles = {} if folder else {"manifest.json": 1}
    files = []
    for title, members in saved_groups:
        group_folder = unique_name(title, "", used_titles)
        for path, relative, item in members:
            item["file"] = f"{group_folder}/{relative}"
            files.append({"path": path, "name": item["file"]})
    title = folder or safe_component(urlsplit(scan["url"]).hostname, "Framekeep")
    return {
        "files": files,
        "folder": folder,
        "name": title + ".zip",
        "manifest": {"version": 1, "scope": scan["intakeScope"], "items": items},
    }
