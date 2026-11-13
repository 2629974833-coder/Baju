"""Bounded same-origin series index discovery and grouped episode scanning."""

import asyncio
import hashlib
import json
import re
import time
from collections import deque
from copy import deepcopy
from urllib.parse import urljoin, urlsplit

import httpx

from backend.catalog_discovery import same_page
from backend.generic_catalog import same_origin_url
from backend.lustflix_directory import (
    DirectoryAccessRequired,
    _public_get,
    _published_series,
    _script_url,
    cover_urls,
    directory_route,
)
from backend.site_adapters import build_catalog

MAX_SHOWS = 500
MAX_EPISODES = 5000
MAX_OBSERVATIONS = 10000
MAX_INDEX_PAGES = 100
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,100}")

_INDEX_DOM = r"""() => {
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const shows = [], pages = [], pageCategories = {}, pageUrl = new URL(location.href);
  const links = [...document.querySelectorAll('a[href]')];
  const listLabel = /^(?:all (?:shows|series|dramas)|shows|series|dramas|剧集列表|剧集分类|剧集|全部剧集|所有剧集|所有剧|影集|剧库|劇集)$/i;
  const cardSelector = '[data-series-card],[data-show-card],.series-card,.show-card,.drama-card,[itemtype$="/TVSeries"]';
  const titleOf = anchor => (anchor.getAttribute('data-title') || anchor.getAttribute('aria-label') || anchor.getAttribute('title') ||
    anchor.querySelector('h2,h3,h4,[itemprop="name"]')?.textContent || anchor.innerText.trim() || anchor.querySelector('img[alt]')?.alt || '').trim().slice(0,200);
  for (const anchor of links.slice(0,4000)) {
    if (!shown(anchor)) continue;
    let target;
    try {target = new URL(anchor.href);} catch {continue;}
    if (target.origin !== pageUrl.origin || target.href === pageUrl.href) continue;
    const title = titleOf(anchor), label = (anchor.getAttribute('aria-label') || anchor.textContent || '').trim();
    const path = target.hash.startsWith('#/') ? target.hash.slice(1) : target.pathname;
    const root = anchor.parentElement?.closest('[data-series-list],[data-show-list],[aria-label],section');
    const rootLabel = root?.getAttribute('aria-label') || root?.querySelector('h1,h2')?.textContent || '';
    const paging = anchor.relList.contains('next') || /^(?:next(?: page)?|下一页|下一頁|下页|›|»)$/i.test(label);
    const categoryLink = anchor.hasAttribute('data-series-category') || /^\/(?:categories|category|genres|genre)\/[^/]+\/?$/i.test(path);
    if (categoryLink && !paging && label) {
      pages.push(target.href);
      pageCategories[target.href] = (anchor.getAttribute('data-series-category') || label).trim().slice(0,100);
      continue;
    }
    if (paging || listLabel.test(label) || /^\/(?:shows|series|dramas)\/?$/i.test(path)) {pages.push(target.href); continue;}
    const card = anchor.closest(cardSelector);
    const showPath = /^\/(?:series|show|shows|drama|dramas|tv-series)\/[^/]+\/?$/i.test(path);
    const publisher = /^(?:www\.)?lustflix\.tv$/i.test(target.hostname) && /^\/(?:s|watch)\/[\w-]+(?:\/\d+)?\/?$/.test(path);
    if (card && card !== anchor && !showPath && !publisher) {
      const primary = card.querySelector('a[itemprop="url"], a[data-series-link], h2 a, h3 a') ||
        [...card.querySelectorAll('a[href]')].find(a => a.querySelector('h2,h3,img')) || card.querySelector('a[href]');
      if (primary !== anchor) continue;
    }
    const scoped = root?.matches('[data-series-list],[data-show-list]') || listLabel.test(rootLabel.trim());
    if (!(card || showPath || publisher || scoped)) continue;
    const images = [...(card || anchor).querySelectorAll('img')].slice(0,3)
      .map(img => img.currentSrc || img.getAttribute('data-src') || img.src).filter(Boolean);
    const category = (card?.getAttribute('data-category') || anchor.getAttribute('data-category') ||
      root?.getAttribute('data-category') || '').slice(0,100);
    shows.push({url:target.href, title, covers:images, category});
  }
  const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
  let nodes = 0, bytes = 0;
  for (const script of scripts.slice(0,32)) {
    const text = script.textContent || '';
    bytes += text.length;
    if (bytes > 524288) break;
    let top;
    try {top=JSON.parse(text);} catch {continue;}
    const queue = [top];
    for(let i=0;i<queue.length && nodes++<5000;i++){
      const obj=queue[i];
      if (!obj || typeof obj!=='object') continue;
      if (Array.isArray(obj)) {for(const item of obj.slice(0,1000)) queue.push(item); continue;}
      const types=[].concat(obj['@type']||[]).map(s=>String(s).split(/[/#]/).pop());
      if (types.includes('TVSeries') || types.includes('CreativeWorkSeries')) {
        const raw = obj.url?.['@id'] || obj.url || obj['@id'];
        if(typeof raw==='string' && !raw.startsWith('#')) {
          try { const url=new URL(raw,location.href);
            if(url.origin===pageUrl.origin && url.href!==pageUrl.href)
              shows.push({url:url.href, title: typeof obj.name==='string'?obj.name.slice(0,200):'',
                categories:[].concat(obj.genre||[]).filter(x=>typeof x==='string').slice(0,10),
                covers:[].concat(obj.image||[]).slice(0,5).map(x=>typeof x==='string'? x : x?.url).filter(x=>typeof x==='string')});
          } catch {}
        }
      }
      for (const key of ['@graph','mainEntity','itemListElement','item','hasPart']) if(obj[key]) queue.push(obj[key]);
    }
  }
  return {shows:shows.slice(0,1000), pages:[...new Set(pages)].slice(0,100), pageCategories,
    limited:links.length>4000 || shows.length>1000 || pages.length>100 || nodes>=5000 || bytes>524288};
}"""


async def read_index_page(page, url, cancelled):
    """Collect card evidence and explicit pagination links while lazy rows become visible."""
    shows, links, categories = {}, set(), {}
    limited = False
    for step in range(8):
        if cancelled() or not same_page(page.url, url):
            return {"shows": [], "pages": [], "limited": True}
        data = await page.evaluate(_INDEX_DOM)
        for item in data["shows"]:
            target = same_origin_url(item.get("url"), url)
            if target:
                _merge_index_card(shows, target, item)
        links.update(href for href in data["pages"] if same_origin_url(href, url))
        categories.update(
            (href, label) for href, label in data["pageCategories"].items() if href in links
        )
        limited = limited or data["limited"]
        moved = await page.evaluate("""() => {
          const root=document.scrollingElement;
          if (!root || root.scrollTop + innerHeight >= root.scrollHeight - 2) return false;
          root.scrollTop += Math.max(300,innerHeight * .85); return true;
        }""")
        if not moved and (shows or links or step >= 2):
            break
        await page.wait_for_timeout(180)
    else:
        limited = True
    return {
        "shows": list(shows.values()),
        "pages": sorted(links),
        "pageCategories": categories,
        "limited": limited,
    }


def _merge_index_card(shows, url, item):
    row = shows.setdefault(url, {"url": url, "title": "", "covers": [], "categories": []})
    if not row["title"]:
        row["title"] = str(item.get("title") or "")[:200]
    categories = [item.get("category"), *item.get("categories", [])]
    row["categories"] = list(
        dict.fromkeys(
            [
                *row["categories"],
                *(
                    value.strip()[:100]
                    for value in categories
                    if isinstance(value, str) and value.strip()
                ),
            ]
        )
    )[:10]
    row["covers"] = list(
        dict.fromkeys(
            [*row["covers"], *(value for value in item.get("covers", []) if isinstance(value, str))]
        )
    )[:12]
    if item.get("catalog"):
        row["catalog"] = item["catalog"]
    return row


def _title(data):
    if isinstance(data, str):
        return data.strip()[:200]
    if isinstance(data, dict):
        return next(
            (
                data[k].strip()[:200]
                for k in ("hans", "zh-CN", "zh", "hant", "zh-TW", "en")
                if isinstance(data.get(k), str) and data[k].strip()
            ),
            "",
        )
    return ""


def _publisher_shows(url, bundle, live):
    by_slug = {}
    for ref in bundle:
        if (
            isinstance(ref, dict)
            and isinstance(ref.get("slug"), str)
            and isinstance(ref.get("id"), str)
        ):
            by_slug.setdefault(ref["slug"], []).append(ref)
    actual = {
        row["slug"]: row
        for row in live
        if isinstance(row, dict) and isinstance(row.get("slug"), str)
    }
    rows, used_ids = [], set()
    for slug in dict.fromkeys([*actual, *by_slug]):
        refs = by_slug.get(slug, [])
        if len(refs) > 1 or not _IDENTIFIER.fullmatch(slug):
            continue
        ref, item = (refs[0] if refs else {}), actual.get(slug, {})
        key = ref.get("id", slug)
        if not _IDENTIFIER.fullmatch(key) or key in used_ids:
            continue
        used_ids.add(key)
        page = url.split("#", 1)[0].rstrip("/") + f"/#/s/{key}"
        entries = []
        episode_rows = item.get("episodes") or []
        if not isinstance(episode_rows, list):
            episode_rows = []
        for episode in episode_rows[:1000]:
            n = episode.get("n") if isinstance(episode, dict) else None
            if type(n) is not int or not 1 <= n <= 1000:
                continue
            price = episode.get("priceCoins")
            gated = (
                episode.get("available") is False
                or (type(price) is int and price > 0)
                or (item.get("memberOnly") is True and not (n == 1 and price == 0))
            )
            entries.append(
                {
                    "url": page.split("#")[0] + f"#/watch/{key}/{n}",
                    "access": "needs-user" if gated else "unknown",
                    "title": _title(episode.get("title")),
                }
            )
        title = _title(item.get("title")) or _title(ref.get("title")) or key
        catalog = (
            build_catalog(page, entries, truncated=len(episode_rows) > 1000) if entries else None
        )
        if catalog:
            catalog.update(title=title, discoverySources=["site-directory"])
            for entry in catalog["entries"]:
                if entry["access"] == "needs-user":
                    entry["accessEvidence"] = "public-directory"
        category = item.get("category") or ref.get("genre") or []
        values = category if isinstance(category, list) else [category]
        categories = [label for value in values if (label := _title(value))]
        rows.append(
            {
                "url": page,
                "title": title,
                "categories": categories,
                "covers": cover_urls(item, ref),
                "catalog": catalog,
            }
        )
    return rows


async def read_publisher_index(url, policy):
    origin = "https://lustflix.tv"
    async with (
        asyncio.timeout(45),
        httpx.AsyncClient(
            timeout=8,
            follow_redirects=False,
            trust_env=False,
            proxy=getattr(policy, "proxy_url", None),
        ) as client,
    ):

        async def get(target):
            return await _public_get(client, policy, target, None)

        html = await get(origin + "/")
        bundle = _published_series(await get(_script_url(origin, html)))
        try:
            live = json.loads(await get(origin + "/api/tv/catalog"))
            if not isinstance(live, dict) or not isinstance(live.get("series"), list):
                raise ValueError("公开剧集索引格式改变")
        except (ValueError, httpx.HTTPError):
            return {"shows": _publisher_shows(url, bundle, []), "limited": True, "pages": []}
        return {
            "shows": _publisher_shows(url, bundle, live["series"]),
            "limited": bool(live.get("hasMore") or live.get("nextCursor")),
            "pages": [],
        }


async def scan_site(scanner, site, options, on_asset, on_progress, cancelled, *, one_show=False):
    """Read available indexes before spending the shared page budget on episode media."""
    start = await scanner.policy.validate(options["url"])
    limit = min(1000, max(1, int(options.get("limit", 100))))
    max_assets = min(1000, max(1, int(options.get("maxAssets", 1000))))
    kinds = options.get("kinds") or ["image", "video"]
    previous = options.get("_resumeIntake") or {}
    cursor = previous.get("_intakeCheckpoint") or {}
    origin = previous.get("siteCatalog") or {}
    prior_rows = origin.get("shows", [])
    if one_show and previous:
        prior_rows = [
            {
                "url": start,
                "title": (previous.get("catalog") or {}).get("title", start),
                "catalog": previous.get("catalog") or {"entries": []},
                "status": "pending",
                "pagesVisited": 0,
            }
        ]
    shows = {row["url"]: deepcopy(row) for row in prior_rows}
    cards = {
        key: {
            "url": key,
            "title": row.get("title", ""),
            "covers": row.get("_indexCovers", []),
            "categories": row.get("categories", []),
        }
        for key, row in shows.items()
    }
    reasons = set(cursor.get("permanentReasons", []))
    media = set(cursor.get("mediaKeys", []))
    observations = set(cursor.get("observationKeys", []))
    pages_with_media = set(cursor.get("pageMedia", []))
    # Old observations might have been inserted just before an interrupted checkpoint.
    prior_refs = [
        ids for group in previous.get("_intakeRefs", {}).values() for ids in group.values()
    ]
    media_padding = max(0, len({i for ids in prior_refs for i in ids}) - len(media))
    observation_padding = max(0, sum(map(len, prior_refs)) - len(observations))
    visited = 0
    prior_visits = cursor.get("totalPages", 0)
    total_limit = MAX_INDEX_PAGES + 2 * MAX_SHOWS + MAX_EPISODES
    limit = min(limit, max(0, total_limit - prior_visits))
    seen_index = set(cursor.get("seenIndex", []))
    if cursor:
        pending_index = dict(cursor.get("pendingIndex", {}))
    else:
        pending_index = {} if one_show else {start: []}
    done_dirs = set(cursor.get("doneDirectories", []))
    parent_dirs = dict(cursor.get("parentDirectories", {}))
    checked = {key: set(urls) for key, urls in cursor.get("checkedPages", {}).items()}
    episode_count = sum(len(row["catalog"].get("entries", [])) for row in shows.values())
    page_access = dict(previous.get("_pageAccess", {}))
    official_count = previous.get("_officialDirectoryCount")
    omitted = set(options.get("excludedEpisodeKeys", []))
    partial_messages = dict(cursor.get("partialMessages", {}))
    timeouts = dict(cursor.get("timeouts", {}))
    # Budget states are recalculated from this run's remaining work.
    for key, row in shows.items():
        row["status"] = "partial" if key in partial_messages else "pending"
        row["message"] = partial_messages.get(key, "")

    def token(*values):
        return hashlib.sha256("\0".join(values).encode()).hexdigest()[:32]

    def at_media_limit():
        return (
            len(media) + media_padding >= max_assets
            or len(observations) + observation_padding >= MAX_OBSERVATIONS
        )

    def pending_for(row):
        if options.get("catalogOnly"):
            return []
        if row["url"] not in done_dirs or "catalog-conflict" in row["catalog"].get("reasons", []):
            return []
        seen = checked.get(row["url"], set())
        return [
            entry["pageUrl"]
            for entry in row["catalog"].get("entries", [])
            if entry.get("access") != "needs-user"
            and entry["episodeKey"] not in omitted
            and entry["pageUrl"] not in seen
        ]

    def remaining_work():
        if prior_visits + visited >= total_limit:
            return False
        if pending_index and len(seen_index) < MAX_INDEX_PAGES:
            return True
        if episode_count < MAX_EPISODES and any(key not in done_dirs for key in shows):
            return True
        return not at_media_limit() and any(pending_for(row) for row in shows.values())

    def mark(reason):
        reasons.add(reason)

    def issue(row, reason, message):
        mark(reason)
        partial_messages[row["url"]] = message
        row.update(status="partial", message=message)

    async def report(message, status="running"):
        continuation = remaining_work()
        temporary = {"page-limit", "index-page-limit", "time-limit"}
        result = {
            "pagesVisited": prior_visits + visited,
            "runPagesVisited": visited,
            "assetsFound": len(media) + media_padding,
            "maxAssets": max_assets,
            "limited": bool(reasons),
            "message": message,
            "status": status,
            "canContinue": continuation,
            "_intakeCheckpoint": {
                "version": 1,
                "totalPages": prior_visits + visited,
                "pendingIndex": dict(pending_index),
                "seenIndex": sorted(seen_index),
                "doneDirectories": sorted(done_dirs),
                "parentDirectories": dict(parent_dirs),
                "checkedPages": {k: sorted(v) for k, v in checked.items()},
                "mediaKeys": sorted(media),
                "observationKeys": sorted(observations),
                "pageMedia": sorted(pages_with_media),
                "timeouts": dict(timeouts),
                "partialMessages": dict(partial_messages),
                "permanentReasons": sorted(reasons - temporary),
            },
        }
        if one_show:
            row = shows.get(start, {})
            if row.get("catalog"):
                result["catalog"] = row["catalog"]
            result["_pageAccess"] = dict(page_access)
            if official_count is not None:
                result["_officialDirectoryCount"] = official_count
        else:
            result["siteCatalog"] = {
                "shows": list(shows.values()),
                "indexPages": len(seen_index),
                "limited": bool(reasons),
                "reasons": sorted(reasons),
            }
        await on_progress(result)
        return result

    def add_show(item, source):
        nonlocal episode_count
        url = same_origin_url(item.get("url"), start)
        if not url:
            return
        if url not in shows and len(shows) >= MAX_SHOWS:
            mark("show-limit")
            return
        if url not in shows:
            shows[url] = {
                "url": url,
                "title": "",
                "categories": [],
                "catalog": {"entries": []},
                "status": "pending",
                "pagesVisited": 0,
                "sourceUrl": source,
            }
        row = shows[url]
        card = _merge_index_card(cards, url, item)
        if not row["title"] or row["title"] == url:
            row["title"] = card["title"] or url
        row["_indexCovers"] = card["covers"]
        row["categories"] = card["categories"]
        row["category"] = next(iter(card["categories"]), "")
        if item.get("catalog") and not row["catalog"].get("entries"):
            if item["catalog"].get("truncated"):
                mark("episode-limit")
            episode_count += store_catalog(row, item["catalog"], MAX_EPISODES - episode_count)
            done_dirs.add(url)
        return row

    def store_catalog(row, catalog, room):
        entries = catalog.get("entries", [])
        if len(entries) > room:
            entries = entries[:room]
            catalog = {
                **catalog,
                "entries": entries,
                "truncated": True,
                "reasons": [*catalog.get("reasons", []), "site-episode-limit"],
            }
            mark("episode-limit")
        row["catalog"] = catalog
        if catalog.get("title"):
            row["title"] = catalog["title"]
        return len(entries)

    async def remember(candidate, row):
        url = candidate["sourceUrl"]
        observed_page = candidate.get("_intakePageUrl") or candidate["pageUrl"]
        media_key = token(url)
        key = token(url, observed_page, row["url"])
        if key in observations:
            return
        if len(observations) + observation_padding >= MAX_OBSERVATIONS:
            mark("observation-limit")
            return
        if media_key not in media and len(media) + media_padding >= max_assets:
            mark("asset-limit")
            return
        # A shared cover can appear on several distinct episodes or shows.
        observations.add(key)
        media.add(media_key)
        pages_with_media.add(token(row["url"], observed_page, candidate["kind"]))
        await on_asset({**candidate, "_intakeGroupUrl": row["url"]})

    async def covers(row, values, source):
        if "image" not in kinds:
            return

        route = directory_route(row["url"])
        detail_page = (
            (row["url"].split("#", 1)[0].rstrip("/") + f"/#/s/{route[1]}") if route else source
        )
        for raw in values:
            if cancelled():
                return
            try:
                image = await scanner.policy.validate(urljoin(source, raw))
            except (ValueError, TypeError):
                continue
            await remember(
                {
                    "sourceUrl": image,
                    "kind": "image",
                    "engine": "http",
                    "pageUrl": source,
                    "title": f"{row['title']} · 封面",
                    "category": "图片素材",
                    "thumbnail": image,
                    "_intakePageUrl": detail_page,
                    "_headers": {"Referer": source},
                },
                row,
            )

    async def check(row, scan_options):
        nonlocal visited, official_count, episode_count
        opts = {
            "url": row["url"],
            "mode": "series",
            "depth": 0,
            "readOnly": True,
            "_captureCatalogImages": True,
            "kinds": kinds,
            "maxAssets": max_assets,
            **scan_options,
        }
        last_pages = 0
        published_at = 0.0

        def sync(child, *, terminal=False):
            nonlocal last_pages, visited, episode_count, official_count
            spent = max(int(terminal), child.get("pagesVisited", 0), last_pages)
            visited += spent - last_pages
            row["pagesVisited"] += spent - last_pages
            last_pages = spent
            if one_show and child.get("_officialDirectoryCount") is not None:
                official_count = child["_officialDirectoryCount"]
            current = child.get("catalog")
            if current:
                episode_count -= len(row["catalog"]["entries"])
                episode_count += store_catalog(row, current, MAX_EPISODES - episode_count)
            access = child.get("_pageAccess") or {}
            page_access.update(access)
            completed = checked.setdefault(row["url"], set())
            before = len(completed)
            for entry in row["catalog"].get("entries", []):
                if access.get(entry["pageUrl"]) in {"observed", "needs-user"}:
                    completed.add(entry["pageUrl"])
            if any(value == "needs-user" for value in access.values()):
                issue(row, "restricted", "部分页面需要站点授权")
                if opts.get("_directoryOnly") and child.get("status") == "needs-user":
                    done_dirs.add(row["url"])
                    parent_dirs.pop(row["url"], None)
            return len(completed) != before

        async def progress(child):
            nonlocal published_at
            changed = sync(child)
            now = time.monotonic()
            if changed or now - published_at >= 2:
                published_at = now
                await report(f"正在读取《{row['title']}》，本轮已读取 {visited} 页")

        result = await scanner.scan(
            site, opts, lambda item: remember(item, row), progress, cancelled
        )
        sync(result, terminal=True)
        if result.get("catalog"):
            await covers(row, result["catalog"].get("coverUrls", []), row["url"])
        status = result.get("status")
        if status not in {"completed", "timed-out", "cancelled"}:
            issue(
                row,
                "restricted" if status == "needs-user" else "page-failed",
                result.get("message") or "读取页面失败",
            )
        if result.get("limited"):
            issue(row, "asset-limit", "达到素材范围上限")
        if status == "completed" and result.get("catalog", {}).get("observedGapOrdinals"):
            issue(row, "incomplete-directory", "剧集目录仍有未枚举的内容")
        return result

    async def crawl():
        nonlocal visited
        if one_show:
            add_show({"url": start}, start)
        else:
            home = urlsplit(start)
            official = (
                home.scheme == "https"
                and home.netloc.lower() in {"lustflix.tv", "www.lustflix.tv"}
                and home.path in {"", "/"}
                and not home.query
                and home.fragment in {"", "/", "/home"}
            )
            while pending_index and len(seen_index) < MAX_INDEX_PAGES and visited < limit:
                if cancelled():
                    return await report("扫描已取消", "cancelled")
                url = next(iter(pending_index))
                inherited = pending_index[url]
                visited += 1
                if official and url == start:
                    try:
                        data = await read_publisher_index(url, scanner.policy)
                    except DirectoryAccessRequired as error:
                        pending_index.pop(url)
                        seen_index.add(url)
                        mark("restricted")
                        return await report(str(error), "needs-user")
                    except (httpx.HTTPError, ValueError, TimeoutError, RecursionError):
                        # A changed catalog can still expose genuine navigable cards in the DOM.
                        data = None
                    if data and data["shows"]:
                        for item in data["shows"]:
                            add_show(item, url)
                        if data["limited"]:
                            mark("index-incomplete")
                        pending_index.pop(url)
                        seen_index.add(url)
                        break

                async def ignore(_value):
                    pass

                result = await scanner.scan(
                    site,
                    {
                        "mode": "page",
                        "url": url,
                        "depth": 0,
                        "limit": 1,
                        "readOnly": True,
                        "_siteIndex": True,
                    },
                    ignore,
                    ignore,
                    cancelled,
                )
                data = result.get("index")
                if result.get("status") in {"timed-out", "cancelled"}:
                    if result["status"] == "timed-out":
                        slot = token("index", url)
                        timeouts[slot] = timeouts.get(slot, 0) + 1
                        if timeouts[slot] >= 2:
                            mark("index-incomplete")
                            pending_index.pop(url)
                            seen_index.add(url)
                    return await report(result["message"], result["status"])
                pending_index.pop(url)
                seen_index.add(url)
                if not data:
                    mark("index-incomplete")
                    if not shows and not pending_index:
                        return await report(
                            result.get("message", "无法读取站点目录"),
                            result.get("status", "failed"),
                        )
                    continue
                if data["limited"]:
                    mark("index-incomplete")
                for item in data["shows"]:
                    add_show({**item, "categories": [*item.get("categories", []), *inherited]}, url)
                for page in data["pages"]:
                    target = same_origin_url(page, start)
                    if target and target not in seen_index and target not in pending_index:
                        if len(pending_index) + len(seen_index) >= MAX_INDEX_PAGES:
                            mark("index-cap")
                            continue
                        explicit = data.get("pageCategories", {}).get(page)
                        pending_index[target] = [explicit] if explicit else inherited
                await report(f"已检查 {len(seen_index)} 个目录页，发现 {len(shows)} 部剧")
            if pending_index:
                mark("index-page-limit")
            if not shows:
                return await report(
                    "本次读取的页面未发现可识别的剧集目录，可直接输入剧集详情链接。", "completed"
                )
            await report(f"已从站点目录列出 {len(shows)} 部剧，正在读取选集")
            for row in shows.values():
                await covers(row, cards[row["url"]]["covers"], row["sourceUrl"])

        # Directory pages are read before any player. Persist a discovered parent link
        # even if the current budget ends between the episode card and its full directory.
        for row in shows.values():
            key = row["url"]
            if cancelled():
                return await report("扫描已取消", "cancelled")
            if key in done_dirs:
                continue
            if visited >= limit or episode_count >= MAX_EPISODES:
                mark("page-limit" if visited >= limit else "episode-limit")
                break
            while key not in done_dirs and visited < limit:
                target = parent_dirs.get(key, key)
                opts = {"url": target, "limit": 1, "_directoryOnly": True}
                if row["catalog"].get("entries"):
                    opts["catalog"] = row["catalog"]
                elif one_show and options.get("catalog"):
                    opts["catalog"] = options["catalog"]
                result = await check(row, opts)
                if result["status"] in {"timed-out", "cancelled"}:
                    mark("time-limit")
                    if result["status"] == "timed-out":
                        slot = token("directory", key, target)
                        timeouts[slot] = timeouts.get(slot, 0) + 1
                        if timeouts[slot] >= 2:
                            done_dirs.add(key)
                            parent_dirs.pop(key, None)
                            issue(row, "page-failed", "目录多次读取超时，可单独重新检查")
                    return await report(
                        result.get("message") or "目录读取中断，已保留进度", result["status"]
                    )
                parent = same_origin_url(result.get("_directoryLink"), start)
                if (
                    target == key
                    and parent
                    and not same_page(parent, key)
                    and result["status"] == "completed"
                ):
                    parent_dirs[key] = parent
                else:
                    done_dirs.add(key)
                    parent_dirs.pop(key, None)
                    if not row["catalog"].get("entries") and result["status"] == "completed":
                        issue(row, "unresolved-directory", "本页未识别到单集目录")
                await report(f"已检查《{row['title']}》的目录")
        active = deque()
        planned = {}
        for key, row in sorted(shows.items(), key=lambda item: len(checked.get(item[0], ()))):
            entries = row["catalog"].get("entries", [])
            if "catalog-conflict" in row["catalog"].get("reasons", []):
                issue(row, "catalog-conflict", "来源目录对应多个不同的选集列表")
                continue
            if any(entry.get("access") == "needs-user" for entry in entries):
                issue(row, "restricted", "部分单集需要站点授权")
            pending = pending_for(row)
            if pending:
                active.append((key, deque(dict.fromkeys(pending))))
            elif entries and row["status"] == "pending" and key in done_dirs:
                row["status"] = "completed"
        remaining = limit - visited
        while active and remaining > 0:
            key, urls = active.popleft()
            planned.setdefault(key, []).append(urls.popleft())
            remaining -= 1
            if urls:
                active.append((key, urls))
        if active or parent_dirs:
            mark("page-limit")
        for key, seeds in planned.items():
            if cancelled():
                return await report("扫描已取消", "cancelled")
            if at_media_limit():
                mark(
                    "asset-limit"
                    if len(media) + media_padding >= max_assets
                    else "observation-limit"
                )
                break
            row = shows[key]
            # A slow show must not spend the scanner's entire time allowance on one
            # enormous child batch. Share one browser context across a modest batch.
            for offset in range(0, len(seeds), 8):
                batch = seeds[offset : offset + 8]
                result = await check(
                    row,
                    {
                        "limit": len(batch),
                        "seedUrls": batch,
                        "catalog": row["catalog"],
                        "followCatalog": False,
                    },
                )
                completed = checked.setdefault(key, set())
                if result["status"] in {"timed-out", "cancelled"}:
                    if result["status"] == "timed-out":
                        stalled = next((page for page in batch if page not in completed), None)
                        if stalled:
                            slot = token(key, stalled)
                            timeouts[slot] = timeouts.get(slot, 0) + 1
                            if timeouts[slot] >= 2:
                                completed.add(stalled)
                                issue(row, "page-failed", "单集多次读取超时，可单独重新检查")
                    mark("time-limit")
                    return await report("本轮读取中断，已保留本轮完成的单集", result["status"])
                # A terminal scan has handled or explicitly rejected every requested seed;
                # never open an access-gated page a second time through a continuation.
                attempted = result.get(
                    "_attemptedPages", batch[: result.get("pagesVisited", len(batch))]
                )
                completed.update(page for page in attempted if page in batch)
                if result["status"] == "completed":
                    primary = "video" if "video" in kinds else "image"
                    if any(token(key, page, primary) not in pages_with_media for page in batch):
                        if key not in partial_messages:
                            issue(row, "unresolved-media", "部分已读取单集未发现所选类型的素材")
                    if "needs-user" in (result.get("_pageAccess") or {}).values():
                        issue(row, "restricted", "部分单集需要站点授权")
                if at_media_limit():
                    mark(
                        "asset-limit"
                        if len(media) + media_padding >= max_assets
                        else "observation-limit"
                    )
                    break
                await report(f"已检查《{row['title']}》的选集，本轮读取 {visited} 页")
        may_continue = remaining_work()
        for key, row in shows.items():
            if pending_for(row) or key not in done_dirs:
                hint = (
                    "仍有待检查页面，可继续本次拾取"
                    if may_continue
                    else "达到累计范围上限，可单独拾取此剧"
                )
                row.update(status="partial", message=hint)
            elif key not in partial_messages:
                row.update(status="completed", message="")
        if prior_visits + visited >= total_limit:
            mark("total-page-limit")
        if one_show and not shows[start]["catalog"].get("entries"):
            problem = partial_messages.get(start)
            if problem and "restricted" in reasons:
                return await report(problem, "needs-user")
            return await report(
                problem or "尚未列出选集目录，可继续检查已发现的目录页。", "completed"
            )
        total = sum(len(row["catalog"].get("entries", [])) for row in shows.values())
        message = f"已列出 {len(shows)} 部剧、{total} 集，发现 {len(media) + media_padding} 项素材"
        if remaining_work():
            message += "。仍有待检查的页面，可继续本次拾取"
        elif "restricted" in reasons:
            message += "。部分内容需要站点授权，可处理后单独复查"
        elif reasons:
            message += "。部分内容受来源或采集上限限制，可单独检查相应剧集"
        return await report(message, "completed")

    try:
        async with asyncio.timeout(min(1800, limit * 30 + 45)):
            if one_show and not shows:
                add_show({"url": start}, start)
            await report("正在读取剩余页面" if previous else "正在读取剧集目录")
            return await crawl()
    except TimeoutError:
        mark("time-limit")
        return await report("已达到扫描时限，已发现的剧集和素材可继续操作", "timed-out")
