"""Bounded discovery from explicit, visible episode lists on ordinary websites."""

import asyncio
import time

from backend.catalog_discovery import same_page
from backend.structured_catalog import (
    JSON_LD_SNAPSHOT,
    MAX_BYTES,
    MAX_SCRIPTS,
    metadata_for,
    parse_structured_catalog,
    same_origin_url,
)

_VIEW = r"""action => {
  const visible = element => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  const roots = [...document.querySelectorAll('[aria-label]')].filter(element => visible(element) &&
    /^(episodes|episode list|剧集|选集|劇集)$/i.test(element.getAttribute('aria-label') || ''));
  const up = [...document.querySelectorAll('a[rel~="up"][href]')].find(visible);
  const groups = roots.map((root,index) => {
    const anchors = [...root.querySelectorAll('a[href]')].filter(visible);
    const links = [], counts = [], errors = [];
    for (const anchor of anchors.slice(0,1001)) {
      const holder = anchor.closest('[data-episode-number],[data-ordinal]');
      const numbers = [];
      for (const node of [anchor, holder && root.contains(holder) ? holder : null].filter(Boolean)) {
        for (const attr of ['data-episode-number','data-ordinal']) {
          const raw=node.getAttribute(attr);
          if(raw !== null) numbers.push(/^\d+$/.test(raw.trim()) ? Number(raw) : -1);
        }
      }
      const text = (anchor.innerText || '').trim();
      for (const match of text.matchAll(/\bEpisode\s+(\d+)\b|第\s*(\d+)\s*集/gi)) numbers.push(Number(match[1] || match[2]));
      const unique=[...new Set(numbers)];
      if(unique.length > 1 || unique.some(number => number < 1 || number > 1000)) {errors.push('ordinal');continue;}
      if(unique.length!==1) continue;
      links.push({url:anchor.href,ordinal:unique[0],title:text.slice(0,200),
        access: /members only|requires? (?:coins|credits)|unlock for|付费解锁|付費解鎖/i.test(text) ? 'needs-user' : 'unknown'});
    }
    const count=Number(root.getAttribute('data-episode-count'));
    if(Number.isInteger(count) && count>0 && count<=1000) counts.push(count);
    for(const heading of root.querySelectorAll('h1,h2,h3,h4,[role="heading"]')) {
      const match=(heading.innerText||'').trim().match(/^(?:(\d+)\s+Episodes|Episodes\s*\(?\s*(\d+)\s*\)?|共\s*(\d+)\s*集)$/i);
      if(visible(heading) && match) counts.push(Number(match[1]||match[2]||match[3]));
    }
    const scrolls=[root,...root.querySelectorAll('*')].slice(0,3000).filter(element => visible(element) &&
      /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight>element.clientHeight+2);
    const pagers=[...root.querySelectorAll('button,[role="button"]')].filter(element => element.tagName!=='A' &&
      visible(element) && !element.disabled && element.getAttribute('aria-disabled')!=='true' &&
      /^(load more(?: episodes)?|more episodes|next page|下一页|下一頁|加载更多(?:剧集)?|載入更多)$/i.test(
        (element.getAttribute('aria-label')||element.title||element.innerText||'').trim()));
    if(action?.group===index) {
      if(action.kind==='scroll') {
        const element=scrolls[action.index];
        if(element && Math.abs(element.scrollTop-action.from)<2)
          element.scrollTop=Math.min(element.scrollHeight-element.clientHeight,element.scrollTop+Math.max(40,element.clientHeight*.75));
      } else if(action.kind==='page') pagers[action.index]?.click();
    }
    return {index,links,counts,errors,truncated:anchors.length>1000,
      catalogUrl:root.getAttribute('data-series-url') || up?.href || null,
      title:root.getAttribute('data-series-title') || document.querySelector('h1')?.innerText || document.title,
      scrolls:scrolls.map((element,index)=>({index,top:element.scrollTop,max:element.scrollHeight-element.clientHeight})),
      pagers:pagers.map((_,index)=>({index}))};
  });
  const cover = document.querySelector('meta[property="og:image"],meta[name="twitter:image"]');
  return {groups,catalogLink:up?.href || null,poster:cover?.content || null};
}"""


async def _discover_dom_catalog(
    page, page_url, previous, cancelled, *, max_steps=20, max_seconds=8
):
    """Follow only a unique labelled list; never synthesize an episode URL."""
    previous = previous if (previous or {}).get("adapterId") == "generic" else None
    metadata = (
        {key: previous.get(key) for key in ("seriesKey", "catalogUrl", "title")}
        if previous and previous.get("seriesKey")
        else None
    )
    entries, ordinals, counts, reasons = {}, {}, set(), []
    catalog_link, limited = None, False
    poster = None
    last, stalls, action = None, 0, None
    try:
        async with asyncio.timeout(max_seconds):
            for _ in range(max_steps):
                if cancelled() or not same_page(page.url, page_url):
                    limited = True
                    break
                if action:
                    await page.evaluate(_VIEW, action)
                    await page.wait_for_timeout(100)
                    if not same_page(page.url, page_url):
                        reasons.append("route-changed")
                        break
                view = await page.evaluate(_VIEW, None)
                catalog_link = (
                    same_origin_url(view.get("catalogLink"), page_url)
                    if view.get("catalogLink")
                    else None
                )
                candidates = []
                for group in view["groups"]:
                    links = []
                    for link in group["links"]:
                        url = same_origin_url(link["url"], page_url)
                        if url:
                            links.append({**link, "url": url})
                    if links or group["errors"]:
                        candidates.append({**group, "links": links})
                signatures = {
                    tuple(sorted({(link["ordinal"], link["url"]) for link in group["links"]}))
                    for group in candidates
                }
                if len(signatures) > 1 or any(group["errors"] for group in candidates):
                    metadata = metadata or metadata_for(page_url)
                    reasons.append("catalog-conflict")
                    entries.clear()
                    break
                if not candidates:
                    break
                identities = {
                    same_origin_url(candidate["catalogUrl"], page_url)
                    for candidate in candidates
                    if candidate.get("catalogUrl")
                }
                if None in identities or len(identities) > 1:
                    metadata = metadata or metadata_for(page_url)
                    reasons.append("catalog-conflict")
                    entries.clear()
                    break
                explicit = next(iter(identities), None)
                # Equivalent visible lists may be separate desktop/mobile controls.
                # Use one control root, while retaining every count and access claim.
                group = {
                    **candidates[0],
                    "links": [link for candidate in candidates for link in candidate["links"]],
                    "counts": [count for candidate in candidates for count in candidate["counts"]],
                    "truncated": any(candidate["truncated"] for candidate in candidates),
                }
                if metadata:
                    known = (
                        {entry["pageUrl"] for entry in previous.get("entries", [])}
                        if previous
                        else set(entries)
                    )
                    if explicit and explicit != metadata["catalogUrl"]:
                        reasons.append("catalog-conflict")
                        break
                    if (
                        previous
                        and page_url != metadata["catalogUrl"]
                        and not explicit
                        and not any(link["url"] in known for link in group["links"])
                    ):
                        break
                else:
                    metadata = metadata_for(explicit or page_url, group["title"])
                if metadata["catalogUrl"] == page_url and page_url not in {link["url"] for link in group["links"]}:
                    poster = view.get("poster")
                for link in group["links"]:
                    ordinal, url = link["ordinal"], link["url"]
                    if (ordinal in ordinals and ordinals[ordinal] != url) or (
                        url in entries and entries[url]["ordinal"] != ordinal
                    ):
                        reasons.append("catalog-conflict")
                        break
                    ordinals[ordinal] = url
                    if url not in entries or link["access"] == "needs-user":
                        entries[url] = link
                if "catalog-conflict" in reasons:
                    entries.clear()
                    break
                counts.update(count for count in group["counts"] if 0 < count <= 1000)
                limited = limited or group["truncated"]
                if len(entries) >= 1000:
                    limited = True
                    reasons.append("catalog-limit")
                    break
                signature = (
                    tuple(entries),
                    tuple((item["top"], item["max"]) for item in group["scrolls"]),
                )
                stalls = stalls + 1 if action and signature == last else 0
                if stalls >= 2:
                    limited = True
                    reasons.append("catalog-stalled")
                    break
                last = signature
                scroll = next(
                    (item for item in group["scrolls"] if item["top"] < item["max"] - 2), None
                )
                if scroll:
                    action = {
                        "kind": "scroll",
                        "group": group["index"],
                        "index": scroll["index"],
                        "from": scroll["top"],
                    }
                elif group["pagers"]:
                    action = {
                        "kind": "page",
                        "group": group["index"],
                        "index": group["pagers"][0]["index"],
                    }
                else:
                    break
            else:
                limited = True
                reasons.append("catalog-limit")
    except TimeoutError:
        limited = True
        reasons.append("catalog-limit")
    if len(counts) > 1:
        reasons.append("conflicting-counts")
    return {
        "links": list(entries.values()),
        "coverUrls": [poster] if poster and entries else [],
        "expectedCount": next(iter(counts)) if len(counts) == 1 else None,
        "linksTruncated": limited,
        "reasons": list(dict.fromkeys(reasons)),
        "seriesMetadata": metadata,
        "catalogLink": catalog_link,
    }


def _combine_catalogs(dom, structured, page_url):
    """Agree on identity and episode URLs before combining independent evidence."""
    dom_sources = ["dom"] if dom["links"] else []
    sources = [*dom_sources, *structured.get("discoverySources", [])]
    metadata = dom.get("seriesMetadata") or structured.get("seriesMetadata")
    reasons = list(dict.fromkeys([*dom["reasons"], *structured["reasons"]]))

    def conflict():
        return {
            **dom,
            "links": [],
            "expectedCount": None,
            "seriesMetadata": metadata or metadata_for(page_url),
            "reasons": list(dict.fromkeys([*reasons, "catalog-conflict"])),
            "discoverySources": sources,
        }

    if "catalog-conflict" in reasons:
        return conflict()
    if not structured["links"]:
        if dom_sources:
            return {
                **dom,
                "discoverySources": dom_sources,
                "structuredWarnings": structured["reasons"],
            }
        return {
            **dom,
            "reasons": reasons,
            "linksTruncated": dom["linksTruncated"] or structured["linksTruncated"],
            "discoverySources": [],
            "seriesMetadata": metadata,
        }
    if (
        dom.get("seriesMetadata")
        and structured.get("seriesMetadata")
        and dom["seriesMetadata"]["catalogUrl"] != structured["seriesMetadata"]["catalogUrl"]
    ):
        return conflict()
    entries, ordinals = {}, {}
    for link in [*dom["links"], *structured["links"]]:
        url, ordinal = link["url"], link["ordinal"]
        if (ordinal in ordinals and ordinals[ordinal] != url) or (
            url in entries and entries[url]["ordinal"] != ordinal
        ):
            return conflict()
        ordinals[ordinal] = url
        old = entries.get(url)
        if old is None or link.get("access") == "needs-user":
            entries[url] = dict(link)
    counts = {
        value
        for value in (dom.get("expectedCount"), structured.get("expectedCount"))
        if value is not None
    }
    if len(counts) > 1:
        reasons.append("conflicting-counts")
    warnings = []
    if dom_sources:
        warnings = [
            reason
            for reason in structured["reasons"]
            if reason in {"structured-invalid", "structured-limit"}
        ]
        reasons = [reason for reason in reasons if reason not in warnings]
    return {
        "links": sorted(entries.values(), key=lambda item: item["ordinal"]),
        "expectedCount": next(iter(counts))
        if len(counts) == 1 and "conflicting-counts" not in reasons
        else None,
        "linksTruncated": dom["linksTruncated"] or structured["linksTruncated"],
        "reasons": list(dict.fromkeys(reasons)),
        "seriesMetadata": metadata,
        "catalogLink": structured.get("catalogLink") or dom.get("catalogLink"),
        "discoverySources": sources,
        "structuredWarnings": warnings,
        "coverUrls": structured.get("coverUrls") or dom.get("coverUrls", []),
    }


async def discover_catalog(page, page_url, previous, cancelled, *, max_steps=20, max_seconds=8):
    """Read page JSON only; the remaining budget belongs to existing DOM controls."""
    started = time.monotonic()
    try:
        async with asyncio.timeout(max_seconds):
            snapshot = await page.evaluate(
                JSON_LD_SNAPSHOT, {"maxScripts": MAX_SCRIPTS, "maxBytes": MAX_BYTES}
            )
    except TimeoutError:
        return {
            "links": [],
            "expectedCount": None,
            "linksTruncated": True,
            "reasons": ["structured-limit"],
            "seriesMetadata": None,
            "catalogLink": None,
            "discoverySources": [],
        }
    structured = parse_structured_catalog(snapshot["scripts"], page_url)
    if snapshot["limited"]:
        structured = {
            **structured,
            "links": [],
            "expectedCount": None,
            "seriesMetadata": None,
            "linksTruncated": True,
            "reasons": ["structured-limit"],
            "discoverySources": [],
        }
    dom = await _discover_dom_catalog(
        page,
        page_url,
        previous,
        cancelled,
        max_steps=max_steps,
        max_seconds=max(0.001, max_seconds - (time.monotonic() - started)),
    )
    if cancelled() or not same_page(page.url, page_url):
        return {**dom, "linksTruncated": True, "discoverySources": ["dom"] if dom["links"] else []}
    return _combine_catalogs(dom, structured, page_url)
