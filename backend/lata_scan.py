"""Read Lata's published episode directory and the current account's player data."""

import asyncio

import httpx

from backend.classification import classify_asset
from backend.lata import read_index, read_page, route
from backend.lustflix_directory import DirectoryAccessRequired
from backend.site_adapters import merge_catalog


async def scan_lata(scanner, site, options, on_asset, on_progress, cancelled):
    start = options.get("url") or site["url"]
    catalog = options.get("catalog")
    visited = found = 0
    access, outcomes, attempted = {}, {}, []
    kinds = set(options.get("kinds") or ["image", "video"])
    maximum = max(1, min(1000, int(options.get("maxAssets", 250))))
    urls = options.get("seedUrls") or [start]
    urls = list(dict.fromkeys(urls))[: max(1, min(1000, int(options.get("limit", 100))))]
    directory_only = bool(
        options.get("_directoryOnly")
        or (
            options.get("mode") == "series"
            and not options.get("seedUrls")
            and options.get("followCatalog", True)
        )
    )

    async def report(status, message, **extra):
        result = dict(
            status=status,
            message=message,
            pagesVisited=visited,
            assetsFound=found,
            _pageAccess=dict(access),
            _episodeOutcomes=dict(outcomes),
            _attemptedPages=list(attempted),
            _recheckAttemptedUrls=list(attempted),
            limited=found >= maximum,
            **extra,
        )
        if catalog:
            result["catalog"] = catalog
        await on_progress(result)
        return result

    async def image(page, data):
        nonlocal found
        if "image" not in kinds or found >= maximum or not data["episode"].get("poster"):
            return
        poster = await scanner.policy.validate(data["episode"]["poster"])
        await on_asset(
            {
                "sourceUrl": poster,
                "pageUrl": page,
                "kind": "image",
                "engine": "http",
                "title": data["catalog"]["title"] + " · 封面",
                "thumbnail": poster,
                "category": "图片素材",
                "_headers": {"Referer": page},
                "_readOnly": True,
            }
        )
        found += 1

    try:
        if cancelled():
            return await report("cancelled", "扫描已取消")
        if route(start)[0] == "index":
            if not options.get("_siteIndex"):
                return await report("failed", "这是 Lata 站点目录，请使用「整站」拾取")
            index = await read_index(start, scanner.browser, site, scanner.policy)
            visited += 1
            if cancelled():
                return await report("cancelled", "扫描已取消")
            return await report("completed", "已读取 Lata 剧集索引", index=index)
        await report("running", "正在读取 Lata 的实际选集与播放权限")
        for url in urls:
            if cancelled():
                return await report("cancelled", "扫描已取消")
            data = await read_page(url, scanner.browser, site, scanner.policy)
            visited += 1
            if cancelled():
                return await report("cancelled", "扫描已取消")
            current = data["catalog"]
            if (
                catalog
                and catalog.get("seriesKey")
                and catalog["seriesKey"] != current["seriesKey"]
            ):
                raise ValueError("页面对应的剧集发生变化，请重新读取目录")
            catalog = merge_catalog(catalog, current)
            if directory_only:
                return await report(
                    "completed", f"已列出《{catalog['title']}》的 {len(catalog['entries'])} 集"
                )
            page, episode = data["url"], data["episode"]
            entry = next(row for row in current["entries"] if row["pageUrl"] == page)
            if entry["episodeKey"] in options.get("excludedEpisodeKeys", []):
                continue
            attempted.append(page)
            await image(page, data)
            if episode["locked"]:
                access[page] = "needs-user"
                outcome = {"status": "needs-user", "message": "站点要求登录或开通本集访问权限"}
            elif "video" not in kinds:
                outcome = {"status": "unresolved", "message": "本次仅提取图片"}
            elif not episode.get("hlsUrl"):
                outcome = {"status": "unresolved", "message": "站点尚未提供本集可用的播放地址"}
            elif found >= maximum:
                outcome = {"status": "unresolved", "message": "已达到本次素材数量上限"}
            else:
                source = await scanner.policy.validate(episode["hlsUrl"])
                asset = {
                    "sourceUrl": source,
                    "pageUrl": page,
                    "title": f"{catalog['title']} · 第 {entry['ordinal']} 集",
                    "kind": "video",
                    "engine": "yt-dlp",
                    "category": "视频素材",
                    "locked": False,
                    "thumbnail": episode.get("poster") or "",
                    "_headers": {"Referer": page},
                    "_readOnly": True,
                    "_episodeEvidence": {
                        "version": 1,
                        "adapterId": "generic",
                        "source": "observed-main-video",
                        "seriesKey": catalog["seriesKey"],
                        "episodeKey": entry["episodeKey"],
                        "ordinal": entry["ordinal"],
                        "pageUrl": page,
                        "sourceUrl": source,
                        "catalogUrl": catalog["catalogUrl"],
                    },
                }
                asset["classification"] = classify_asset(asset)
                await on_asset(asset)
                found += 1
                access[page] = "observed"
                outcome = {"status": "ready", "message": "已读取本集的正片播放地址"}
            outcomes[page] = outcome
            catalog = merge_catalog(
                catalog, current, observed_page=page, access=access.get(page, "unknown")
            )
            await report("running", f"已检查第 {entry['ordinal']} 集")
        gated = any(row["status"] == "needs-user" for row in outcomes.values())
        status = (
            "needs-user"
            if gated and not any(row["status"] == "ready" for row in outcomes.values())
            else "completed"
        )
        return await report(status, f"已检查 {len(attempted)} 集，发现 {found} 项素材")
    except asyncio.CancelledError:
        raise
    except DirectoryAccessRequired:
        return await report(
            "needs-user", "Lata 要求重新登录或验证，请在来源中完成 Google 登录并保存会话"
        )
    except (httpx.HTTPError, TimeoutError, OSError):
        return await report("failed", "Lata 连接暂时失败，请稍后重新扫描")
    except ValueError as error:
        return await report("failed", str(error))
