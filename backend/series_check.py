"""One durable selection check, with an isolated read-only probe for each observed episode."""

from collections import Counter

import httpx
from playwright.async_api import Error as BrowserError


def _outcome(page, probe):
    exact = probe.get("_episodeOutcomes", {}).get(page)
    if exact:
        return dict(exact)
    access = probe.get("_pageAccess", {}).get(page)
    if access == "needs-user":
        return {"status": "needs-user", "message": "本集页面要求登录、解锁或验证"}
    if access == "observed":
        return {"status": "ready", "message": "已从本集页面确认正片"}
    status = probe.get("status")
    if status in {"unsupported-media", "unsupported-drm", "failed", "timed-out"}:
        return {
            "status": "failed" if status == "timed-out" else status,
            "message": probe["message"],
        }
    if status == "needs-user":
        return {"status": "access-pending", "message": probe["message"]}
    return {"status": "unresolved", "message": "本次未确认可访问的正片"}


async def _check_one(scanner, site, entry, catalog, session, on_asset, cancelled):
    url = entry["pageUrl"]
    found = 0
    # Each probe sees one page, so a redirect, timeout or per-page media cap
    # cannot hide the remainder of the selection. Progress belongs to the parent.
    probe_options = {
        "url": url,
        "mode": "series",
        "depth": 0,
        "limit": 1,
        "kinds": ["video"],
        "catalog": {**catalog, "entries": [dict(entry)]},
        "seedUrls": [url],
        "recheckUrls": [url],
        "followCatalog": False,
        "readOnly": True,
        "_seriesCheckSession": session,
    }
    last = {}
    write_failed = False

    async def observe(values):
        nonlocal last
        last = values

    async def collect(asset):
        nonlocal found, write_failed
        if asset.get("pageUrl") == url and asset.get("kind") == "video":
            try:
                await on_asset(asset)
            except Exception:
                write_failed = True
                raise
            found += 1

    try:
        probe = await scanner.scan(site, probe_options, collect, observe, cancelled) or last
    except (ValueError, OSError, httpx.RequestError, BrowserError):
        if write_failed:
            raise
        probe = {
            "status": "failed",
            "pagesVisited": last.get("pagesVisited", 0),
            "message": "本集连接或解析失败，可稍后重新检查",
        }
    return probe, found


async def scan_and_check(scanner, site, options, on_asset, on_progress, cancelled, *, buyer=None):
    catalog = options["catalog"]
    rows = {entry["pageUrl"]: entry for entry in catalog["entries"]}
    urls = options["seedUrls"]
    total = len(urls)
    outcomes, access, attempts, session = {}, {}, [], {}
    counts = Counter()
    visited = found = 0

    async def report(status, message):
        checked = len(outcomes)
        await on_progress(
            {
                "status": status,
                "message": message,
                "catalog": catalog,
                "checkTotal": total,
                "checkedCount": checked,
                "pagesQueued": total - checked,
                "pagesVisited": visited,
                "assetsFound": found,
                "_pageAccess": dict(access),
                "_recheckAttemptedUrls": list(attempts),
                "_episodeOutcomes": dict(outcomes),
            }
        )

    if buyer and not cancelled():
        await report("running", "正在核对选集的站点账号、金币余额和单集报价")
        await buyer.prepare(urls)
    for url in urls:
        if cancelled():
            break
        entry = rows[url]
        await report(
            "running", f"正在扫描并检查第 {entry['ordinal']} 集，已检查 {len(outcomes)}/{total} 集"
        )
        probe, added = await _check_one(scanner, site, entry, catalog, session, on_asset, cancelled)
        found += added
        visited += int(probe.get("pagesVisited", 0))
        if cancelled() or probe.get("status") == "cancelled":
            break
        outcome = _outcome(url, probe)
        if buyer and not probe.get("_stopBatch"):
            receipt = await buyer.check(entry, cancelled, playable=outcome["status"] == "ready")
            if cancelled():
                break
            if receipt:
                if (
                    receipt["status"] in {"purchased", "owned", "free"}
                    and outcome["status"] != "ready"
                ):
                    session.clear()
                    probe, added = await _check_one(
                        scanner, site, entry, catalog, session, on_asset, cancelled
                    )
                    found += added
                    visited += int(probe.get("pagesVisited", 0))
                    if cancelled() or probe.get("status") == "cancelled":
                        break
                    outcome = _outcome(url, probe)
                outcome["message"] = f"{outcome.get('message', '')}。{receipt['message']}"
        attempts.append(url)
        outcomes[url] = outcome
        counts[outcome["status"]] += 1
        fresh = probe.get("_pageAccess", {}).get(url)
        if fresh in {"observed", "needs-user"}:
            access[url] = fresh
            entry["access"] = fresh
            evidence = next(
                (
                    item.get("accessEvidence")
                    for item in (probe.get("catalog") or {}).get("entries", [])
                    if item["pageUrl"] == url
                ),
                None,
            )
            entry["accessEvidence"] = (
                "account-unlock"
                if fresh == "observed" and evidence == "account-unlock"
                else "player"
            )
        if probe.get("_stopBatch"):
            await report(
                probe["status"],
                f"{probe['message']}。已检查 {len(outcomes)}/{total} 集，还有 {total - len(outcomes)} 集未检查",
            )
            return
    else:
        unresolved = total - counts["ready"] - counts["needs-user"]
        await report(
            "completed",
            f"扫描并检查完成 {total}/{total} 集：已确认正片 {counts['ready']} 集，"
            f"需要站点权限 {counts['needs-user']} 集，待确认或检查异常 {unresolved} 集。",
        )
        return
    await report("cancelled", f"已取消，完成 {len(outcomes)}/{total} 集的检查")
